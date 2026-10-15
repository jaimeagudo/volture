"""Pure control logic: keeps house + car power under the contracted limit.

The Shelly clamp sits on the main feed, so its reading already includes the car.
No I/O here: feed it (time, reading, local hour) and it returns a Command when something changes.
Every tunable lives in Config, filled from the `volture:` block of secrets.yaml (see settings.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# A Shelly voltage outside this range is treated as a glitch: fall back to Config.fallback_voltage_v.
PLAUSIBLE_VOLTAGE_V = (180.0, 260.0)


@dataclass(frozen=True)
class Config:
    contracted_power_w: float = 4600.0  # utility limit (20 A at 230 V): never exceed
    headroom_w: float = 230.0           # margin below it for loads that switch on between readings
    fallback_voltage_v: float = 230.0   # used when the Shelly gives no plausible voltage
    min_amps: int = 5                   # lowest current the car accepts
    max_amps: int = 32                  # charger / car maximum
    ramp_up_step_amps: int = 1          # amps added per ramp-up step
    ramp_up_every_s: float = 15.0       # minimum time between ramp-up steps
    resume_after_s: float = 60.0        # power must stay low this long before restarting
    stop_after_invalid_s: float = 30.0  # no trustworthy reading for this long -> stop
    # Off-peak window in local time (settings.timezone), whole hours 0-23. Charging only inside
    # [start, end); it may cross midnight (22 -> 7). Both None: charge any time.
    offpeak_start_hour: Optional[int] = None
    offpeak_end_hour: Optional[int] = None

    @property
    def working_limit_w(self) -> float:
        return self.contracted_power_w - self.headroom_w


@dataclass(frozen=True)
class Reading:
    power_w: float
    valid: bool = True
    voltage_v: Optional[float] = None


@dataclass(frozen=True)
class Command:
    charging: bool
    amps: int


def in_offpeak(cfg: Config, local_hour: Optional[int]) -> bool:
    """True when charging is allowed at this local hour (always, if no window or hour unknown)."""
    start, end = cfg.offpeak_start_hour, cfg.offpeak_end_hour
    if start is None or end is None or local_hour is None or start == end:
        return True
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end  # window crosses midnight


def effective_voltage(cfg: Config, reading: Reading) -> float:
    low, high = PLAUSIBLE_VOLTAGE_V
    if reading.voltage_v is not None and low <= reading.voltage_v <= high:
        return reading.voltage_v
    return cfg.fallback_voltage_v


class Controller:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config()
        self.charging = False
        self.amps = 0
        self.last_change = -math.inf
        self.low_since: Optional[float] = None
        self.invalid_since: Optional[float] = None

    def adopt(self, charging: bool, amps: int) -> None:
        """Align the controller's belief with the car's reported state (see confirm.py)."""
        self.charging, self.amps = charging, (amps if charging else 0)
        # A stale "low power since" would restart charging at once instead of after resume_after_s.
        self.low_since = None

    def _set(self, amps: int, now: Optional[float] = None) -> Command:
        self.charging, self.amps = True, amps
        if now is not None:
            self.last_change = now
        return Command(True, amps)

    def _stop(self, now: float) -> Command:
        self.charging, self.amps = False, 0
        self.last_change = now
        return Command(False, 0)

    def tick(self, now: float, reading: Optional[Reading],
             local_hour: Optional[int] = None) -> Optional[Command]:
        c = self.cfg
        allowed = in_offpeak(c, local_hour)
        if reading is None or not reading.valid:
            self.low_since = None
            if self.invalid_since is None:
                self.invalid_since = now
            if not self.charging:
                return None
            if not allowed or now - self.invalid_since >= c.stop_after_invalid_s:
                return self._stop(now)
            if self.amps > c.min_amps:
                return self._set(c.min_amps, now)
            return None
        self.invalid_since = None
        p = reading.power_w
        volts = effective_voltage(c, reading)

        if not self.charging:
            # Over the contracted power while we believe the car is stopped: the car may be
            # charging without us (already charging at startup, or started from the Tesla app).
            # A stop to an idle car is harmless, so send one, at most every ramp_up_every_s.
            # Cost: if the house alone exceeds the limit, the car gets (and may wake for) repeated stops.
            if p > c.contracted_power_w:
                self.low_since = None
                if now - self.last_change >= c.ramp_up_every_s:
                    return self._stop(now)
                return None
            # Only restart inside the off-peak window and if house + minimum charge fits the ceiling.
            if allowed and p + c.min_amps * volts <= c.working_limit_w:
                if self.low_since is None:
                    self.low_since = now
                if now - self.low_since >= c.resume_after_s:
                    self.low_since = None
                    return self._set(c.min_amps, now)
            else:
                self.low_since = None
            return None

        if not allowed:
            return self._stop(now)

        if p > c.working_limit_w:
            delta = math.ceil((p - c.working_limit_w) / volts)
            target = self.amps - delta
            if target < c.min_amps:
                if self.amps == c.min_amps and p > c.contracted_power_w:
                    return self._stop(now)
                target = c.min_amps
            return self._set(target, now) if target != self.amps else None

        step = min(c.ramp_up_step_amps, c.max_amps - self.amps)
        if (step > 0
                and p + step * volts <= c.working_limit_w
                and now - self.last_change >= c.ramp_up_every_s):
            return self._set(self.amps + step, now)
        return None

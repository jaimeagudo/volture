"""Pure control logic: keeps house + car power under the contracted limit.

The Shelly clamp sits on the main feed, so its reading already includes the car.
No I/O here: feed it (time, reading) and it returns a Command when something changes.
Every tunable lives in Config, filled from the `volture:` block of secrets.yaml (see secrets.py).
Volture never starts a charge: the owner does (Tesla app, the car's schedule). While the car
charges, Volture uses every amp that fits under the contracted power. The only start it ever
sends resumes a charge Volture itself stopped to protect the contracted power.
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
    headroom_w: float = 0.0             # margin below it for loads that switch on between readings
    fallback_voltage_v: float = 230.0   # used when the Shelly gives no plausible voltage
    min_amps: int = 5                   # lowest current the car accepts
    max_amps: int = 32                  # charger / car maximum
    ramp_up_step_amps: int = 1          # amps added per ramp-up step
    ramp_up_every_s: float = 15.0       # minimum time between ramp-up steps
    resume_after_s: float = 60.0        # power must stay low this long before restarting
    stop_after_invalid_s: float = 30.0  # no trustworthy reading for this long -> stop (if stop_without_reading)
    # Without a trustworthy reading: False (default) sends nothing, the car carries on as it is (a faulty
    # Shelly must never stop a charge); True drops to min_amps, then stops after stop_after_invalid_s.
    stop_without_reading: bool = False

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
        # True only after Volture itself stopped a charge (power over the limit, no reading):
        # the one case where it may start the car again.
        self.paused_by_us = False

    def follow_user(self, charging: bool, amps: int) -> None:
        """A change made outside Volture (Tesla app, schedule, the car itself): take it as is.

        A start is taken over (amps rise to whatever fits); a stop stays a stop.
        """
        self.adopt(charging, amps)
        self.paused_by_us = False

    def adopt(self, charging: bool, amps: int) -> None:
        """Align the controller's belief with the car's reported state (see confirm.py)."""
        self.charging, self.amps = charging, (amps if charging else 0)
        # A stale "low power since" would restart charging at once instead of after resume_after_s.
        self.low_since = None

    def _set(self, amps: int, now: float) -> Command:
        self.charging, self.amps, self.paused_by_us = True, amps, False
        self.last_change = now
        return Command(True, amps)

    def _stop(self, now: float, resumable: bool) -> Command:
        """resumable: Volture stopped a charge it knew of, so it may restart it later."""
        self.charging, self.amps, self.paused_by_us = False, 0, resumable
        self.last_change = now
        return Command(False, 0)

    def tick(self, now: float, reading: Optional[Reading],
             car_can_charge: bool = True) -> Optional[Command]:
        """car_can_charge: False when the car is unplugged or at its battery charge limit."""
        c = self.cfg
        if reading is None or not reading.valid:
            self.low_since = None
            if self.invalid_since is None:
                self.invalid_since = now
            if not self.charging or not c.stop_without_reading:
                return None
            if now - self.invalid_since >= c.stop_after_invalid_s:
                return self._stop(now, resumable=True)
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
            # Not resumable: Volture never starts a charge it did not know about.
            # Cost: if the house alone exceeds the limit, the car gets (and may wake for) repeated stops.
            if p > c.contracted_power_w:
                self.low_since = None
                if now - self.last_change >= c.ramp_up_every_s:
                    return self._stop(now, resumable=self.paused_by_us)
                return None
            # Restart only a charge Volture stopped itself, if the car can take charge and
            # house + minimum charge fits the ceiling.
            if (self.paused_by_us and car_can_charge
                    and p + c.min_amps * volts <= c.working_limit_w):
                if self.low_since is None:
                    self.low_since = now
                if now - self.low_since >= c.resume_after_s:
                    self.low_since = None
                    return self._set(c.min_amps, now)
            else:
                self.low_since = None
            return None

        # Basic maths, whatever the Shelly says: the car alone must never draw more than the
        # contracted power (4600 W / 230 V = 20 A). Guards against a reading that misses the car.
        amps_cap = min(c.max_amps, math.floor(c.contracted_power_w / volts))
        if p > c.working_limit_w:
            delta = math.ceil((p - c.working_limit_w) / volts)
            target = min(self.amps - delta, amps_cap)
            if target < c.min_amps:
                if self.amps == c.min_amps and p > c.contracted_power_w:
                    return self._stop(now, resumable=True)
                target = c.min_amps
            return self._set(target, now) if target != self.amps else None

        if self.amps > amps_cap:
            return self._set(amps_cap, now)
        step = min(c.ramp_up_step_amps, amps_cap - self.amps)
        if (step > 0
                and p + step * volts <= c.working_limit_w
                and now - self.last_change >= c.ramp_up_every_s):
            return self._set(self.amps + step, now)
        return None

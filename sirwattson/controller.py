"""Pure control logic: keeps house + car power under the contracted limit.

The Shelly clamp sits on the main feed, so its reading already includes the car.
No I/O here: feed it (time, reading) and it returns a Command when something changes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    voltage_v: float = 230.0
    working_limit_w: float = 4370.0     # target ceiling (19 A)
    emergency_limit_w: float = 4600.0   # contracted power (20 A)
    min_amps: int = 5
    max_amps: int = 32
    ramp_up_every_s: float = 15.0
    resume_after_s: float = 60.0
    stop_after_invalid_s: float = 30.0


@dataclass
class Reading:
    power_w: float
    valid: bool = True


@dataclass
class Command:
    charging: bool
    amps: int


class Controller:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config()
        self.charging = False
        self.amps = 0
        self.last_change = -math.inf
        self.low_since: Optional[float] = None
        self.invalid_since: Optional[float] = None

    def _set(self, amps: int, now: Optional[float] = None) -> Command:
        self.charging, self.amps = True, amps
        if now is not None:
            self.last_change = now
        return Command(True, amps)

    def _stop(self, now: float) -> Command:
        self.charging, self.amps = False, 0
        self.last_change = now
        return Command(False, 0)

    def tick(self, now: float, reading: Optional[Reading]) -> Optional[Command]:
        c = self.cfg
        if reading is None or not reading.valid:
            self.low_since = None
            if self.invalid_since is None:
                self.invalid_since = now
            if not self.charging:
                return None
            if now - self.invalid_since >= c.stop_after_invalid_s:
                return self._stop(now)
            if self.amps > c.min_amps:
                return self._set(c.min_amps, now)
            return None
        self.invalid_since = None
        p = reading.power_w

        if not self.charging:
            # Only restart if house + minimum charge still fits under the ceiling.
            if p + c.min_amps * c.voltage_v <= c.working_limit_w:
                if self.low_since is None:
                    self.low_since = now
                if now - self.low_since >= c.resume_after_s:
                    self.low_since = None
                    return self._set(c.min_amps, now)
            else:
                self.low_since = None
            return None

        if p > c.working_limit_w:
            delta = math.ceil((p - c.working_limit_w) / c.voltage_v)
            target = self.amps - delta
            if target < c.min_amps:
                if self.amps == c.min_amps and p > c.emergency_limit_w:
                    return self._stop(now)
                target = c.min_amps
            return self._set(target, now) if target != self.amps else None

        if (p <= c.working_limit_w - c.voltage_v
                and self.amps < c.max_amps
                and now - self.last_change >= c.ramp_up_every_s):
            return self._set(self.amps + 1, now)
        return None

"""Model 3 + house simulator. Single-phase, power = amps * voltage."""
from __future__ import annotations

from .controller import Command


class SimCar:
    def __init__(self, voltage_v: float = 230.0, delay_s: float = 2.0):
        self.voltage_v, self.delay_s = voltage_v, delay_s
        self.charging, self.amps = False, 0
        self._pending: list[tuple[float, Command]] = []

    def command(self, cmd: Command, now: float) -> None:
        self._pending.append((now + self.delay_s, cmd))

    def advance(self, now: float) -> None:
        due = [c for t, c in self._pending if t <= now]
        self._pending = [(t, c) for t, c in self._pending if t > now]
        for c in due:
            self.charging = c.charging
            self.amps = c.amps if c.charging else 0

    @property
    def power_w(self) -> float:
        return self.amps * self.voltage_v if self.charging else 0.0


class World:
    def __init__(self, base_w: float = 650.0, voltage_v: float = 230.0):
        self.base_w = base_w
        self.voltage_v = voltage_v
        self.car = SimCar(voltage_v)
        self.shelly_valid = True
        self.shelly_online = True

    def total_w(self, now: float) -> float:
        self.car.advance(now)
        return self.base_w + self.car.power_w

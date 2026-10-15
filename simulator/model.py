"""Model 3 + house simulator. Single-phase, power = amps * voltage."""
from __future__ import annotations

from volture.controller import Command


class SimCar:
    def __init__(self, voltage_v: float = 230.0, delay_s: float = 2.0):
        self.voltage_v, self.delay_s = voltage_v, delay_s
        self.charging, self.amps = False, 0
        # False: commands are silently lost, like a car out of Bluetooth range.
        self.reachable = True
        self.plugged = True
        self.battery_pct, self.limit_pct = 50.0, 80.0  # set by hand on the page; no charging curve
        self._pending: list[tuple[float, Command]] = []

    @property
    def can_charge(self) -> bool:
        return self.plugged and self.battery_pct < self.limit_pct

    def _obey(self, cmd: Command) -> None:
        # Like the real car: a start is refused when unplugged or at the charge limit.
        self.charging = cmd.charging and self.can_charge
        self.amps = cmd.amps if self.charging else 0

    def settle(self) -> None:
        """The car stops by itself when unplugged or when the battery reaches its limit."""
        if self.charging and not self.can_charge:
            self.charging, self.amps = False, 0

    def command(self, cmd: Command, now: float) -> None:
        if self.reachable:
            self._pending.append((now + self.delay_s, cmd))

    def force(self, cmd: Command) -> None:
        """A change made outside volture (e.g. the Tesla app): immediate, regardless of reachability."""
        self._pending.clear()
        self._obey(cmd)

    def advance(self, now: float) -> None:
        due = [c for t, c in self._pending if t <= now]
        self._pending = [(t, c) for t, c in self._pending if t > now]
        for c in due:
            self._obey(c)
        self.settle()

    @property
    def power_w(self) -> float:
        return self.amps * self.voltage_v if self.charging else 0.0


class World:
    def __init__(self, base_w: float = 650.0, voltage_v: float = 230.0):
        self.base_w = base_w
        self.car = SimCar(voltage_v)
        self.shelly_valid = True
        self.shelly_online = True

    @property
    def voltage_v(self) -> float:
        return self.car.voltage_v

    @voltage_v.setter
    def voltage_v(self, value: float) -> None:
        self.car.voltage_v = value  # one grid voltage for the house meter and the car

    def total_w(self, now: float) -> float:
        self.car.advance(now)
        return self.base_w + self.car.power_w

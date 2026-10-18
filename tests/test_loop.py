"""The C++ control cycle (esp32/controller.h, Loop) driving the simulated car, as on the ESP32.

Python's equivalent lives in volture/main.py (async, with I/O), so these scenarios run on C++ only.
Every 5 s: the car's reported state + a Shelly reading go in; the commands that come out are sent
to the simulated car, which applies them after its 2 s delay (or never, when unreachable).
"""
import pytest

from conftest import CliProcess, build_cli, car_token
from simulator.model import World
from volture.confirm import CarState
from volture.controller import Command

POLL_S = 5


@pytest.fixture
def cli():
    binary = build_cli()
    if binary is None:
        pytest.skip("no C++ compiler (c++) on PATH: C++ port not tested")
    process = CliProcess(binary)
    yield process
    process.close()


def parse_cmd(token):
    if token == "-":
        return None
    charging, amps = token.split(",")
    return Command(charging == "1", int(amps))


class Device:
    """The ESP32's loop: read the car and the Shelly, step the C++ Loop, send what it returns."""

    def __init__(self, cli, world, control=True, reading_misses_car=False):
        self.cli, self.world, self.control, self.t = cli, world, control, 0
        self.reading_misses_car = reading_misses_car  # like a fake Shelly that never sees the car
        self.cycles = []

    def run(self, seconds):
        for _ in range(seconds // POLL_S):
            self.t += POLL_S
            car = self.world.car
            car.advance(self.t)
            power = self.world.base_w if self.reading_misses_car else self.world.total_w(self.t)
            observed = CarState(car.charging, car.amps, car.plugged, car.battery_pct, car.limit_pct)
            reply = self.cli.ask(f"step {self.t} {power} 1 {self.world.voltage_v} {car_token(observed)} "
                                 f"{1 if self.control else 0}").split()
            resend, command, event, reason, dry_run = parse_cmd(reply[0]), parse_cmd(reply[1]), *reply[2:]
            self.cycles.append((self.t, event, reason, command))
            for cmd in (resend, command):
                if cmd is not None and dry_run == "0":
                    car.command(cmd, self.t)


def test_never_starts_a_charge(cli):
    device = Device(cli, World(650))
    device.run(600)
    assert not device.world.car.charging


def test_takes_over_an_app_start_and_ramps_to_the_ceiling(cli):
    world = World(650)
    world.car.force(Command(True, 5))
    device = Device(cli, world)
    device.run(600)
    assert world.car.amps == 17                         # default headroom 0: (4600 - 650) / 230 = 17.2
    assert world.total_w(device.t) <= 4600
    assert any(event == "confirmed" for _, event, _, _ in device.cycles)


def test_stops_on_a_spike_and_resumes(cli):
    world = World(650)
    world.car.force(Command(True, 5))
    device = Device(cli, world)
    device.run(600)
    world.base_w = 5000
    device.run(60)
    assert not world.car.charging
    world.base_w = 650
    device.run(120)
    assert world.car.charging


def test_unreachable_car_is_resent_then_adopted(cli):
    world = World(650)
    world.car.force(Command(True, 5))
    world.car.reachable = False                         # commands are silently lost
    device = Device(cli, world)
    device.run(600)                                     # each ramp step is a new command: give-up comes after
    events = [event for _, event, _, _ in device.cycles]
    assert "resend" in events and "gave_up_adopted" in events
    assert world.car.amps == 5


def test_reading_that_misses_the_car_stops_at_20_amps(cli):
    # The 2026-10-15 incident: a fake Shelly stuck at 4000 W let the car reach 22 A.
    world = World(4000)
    world.car.force(Command(True, 5))
    device = Device(cli, world, reading_misses_car=True)
    device.run(900)
    assert world.car.amps <= 20


def test_dry_run_sends_nothing_but_shows_the_decision(cli):
    world = World(650)
    world.car.force(Command(True, 5))
    device = Device(cli, world, control=False)
    device.run(120)
    assert world.car.amps == 5                          # untouched
    assert any(reason == "ramp" and command == Command(True, 6) for _, _, reason, command in device.cycles)

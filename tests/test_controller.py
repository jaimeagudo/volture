"""Control scenarios. Each test runs twice, against Python and against the C++ port (see conftest.py)."""
from dataclasses import replace

from volture.confirm import Action, CarState, Pending
from volture.controller import Command, Config, Reading

from simulator.model import World


class Harness:
    def __init__(self, backend, base_w=650.0, cfg=None, voltage_v=230.0):
        self.world, self.ctrl, self.t = World(base_w=base_w, voltage_v=voltage_v), backend.Controller(cfg), 0.0
        self.totals = []

    def run(self, seconds, tick=5):
        for i in range(seconds):
            self.t += 1
            total = self.world.total_w(self.t)
            self.totals.append((self.t, total))
            if i % tick == 0:
                online = self.world.shelly_online
                r = Reading(total, self.world.shelly_valid, self.world.voltage_v) if online else None
                cmd = self.ctrl.tick(self.t, r)
                if cmd:
                    self.world.car.command(cmd, self.t)

    def start_from_app(self, amps=5):
        """The owner starts charging (Tesla app or the car's schedule); Volture sees the car's report."""
        self.car.force(Command(True, amps))
        self.ctrl.follow_user(True, amps)

    @property
    def car(self):
        return self.world.car

    def total(self):
        return self.world.total_w(self.t)


# ---- Power limit ----

def test_never_starts_a_charge(backend):
    h = Harness(backend, 650)
    h.run(600)
    assert not h.car.charging                  # plenty of room, but nobody started it


def test_ramps_to_ceiling(backend):
    h = Harness(backend, 650)
    h.start_from_app(5)
    h.run(400)
    assert h.car.charging
    assert h.car.amps == 16                    # (4370-650)/230 = 16.2
    assert h.total() <= 4370


def test_respects_other_loads(backend):
    h = Harness(backend, 2000)
    h.start_from_app()
    h.run(400)
    assert h.car.amps == 10
    assert h.total() <= 4370


def test_spike_stops_then_resumes(backend):
    h = Harness(backend, 650)
    h.start_from_app()
    h.run(400)
    spike_t = h.t
    h.world.base_w = 3500
    h.run(60)
    assert not h.car.charging
    late = [p for t, p in h.totals if t > spike_t + 20]
    assert max(late) <= 4600
    h.world.base_w = 650
    h.run(100)
    assert h.car.charging                      # Volture stopped it, so Volture resumes it
    assert h.car.amps >= 5
    assert h.total() <= 4370


def test_stops_when_minimum_does_not_fit(backend):
    h = Harness(backend, 3800)                 # 3800 + 5*230 = 4950 W > 4600
    h.start_from_app()
    h.run(300)
    assert not h.car.charging
    assert h.total() <= 4600


def test_stops_charging_it_did_not_start(backend):
    # Car already charging at 32 A when volture starts, before its first report reaches Volture:
    # the controller believes it is stopped, but must still stop it once over the contracted power.
    h = Harness(backend, 650)
    h.car.charging, h.car.amps = True, 32       # 650 + 32*230 = 8010 W
    h.run(30)
    assert not h.car.charging
    assert h.total() <= 4600
    h.run(300)
    assert not h.car.charging                  # never knew of that charge: never restarts it


def test_invalid_reading_fails_safe(backend):
    h = Harness(backend, 650)
    h.start_from_app()
    h.run(400)
    h.world.shelly_valid = False
    h.run(10)
    assert h.car.amps == 5
    h.run(40)
    assert not h.car.charging


def test_shelly_offline_fails_safe(backend):
    h = Harness(backend, 650)
    h.start_from_app()
    h.run(400)
    h.world.shelly_online = False
    h.run(50)
    assert not h.car.charging


def test_resume_waits_after_adopting(backend):
    ctrl = backend.Controller()
    ctrl.follow_user(True, 5)
    assert ctrl.tick(0, Reading(5000)) == Command(False, 0)   # over the limit at 5 A: stop
    ctrl.tick(10, Reading(650))                # low power: resume timer starts
    ctrl.adopt(False, 0)                       # car report at t=60 restarts the timer
    assert ctrl.tick(80, Reading(650)) is None
    assert ctrl.tick(140, Reading(650)) == Command(True, 5)


def test_never_above_contract_even_if_the_reading_misses_the_car(backend):
    # A Shelly reading stuck at 650 W (e.g. it does not see the car): 4600 W / 230 V = 20 A, never more.
    ctrl = backend.Controller()
    ctrl.follow_user(True, 5)
    amps = [cmd.amps for t in range(0, 3000, 5) if (cmd := ctrl.tick(t, Reading(650))) is not None]
    assert max(amps) == 20


def test_app_start_above_contract_is_cut(backend):
    ctrl = backend.Controller()
    ctrl.follow_user(True, 32)                 # 32 A from the app, reading misses the car
    assert ctrl.tick(0, Reading(650)) == Command(True, 20)


def test_settings_drive_the_algorithm(backend):
    # A 3.45 kW contract, 2 A steps, 16 A charger: same algorithm, different installation.
    cfg = Config(contracted_power_w=3450, headroom_w=230, max_amps=16, ramp_up_step_amps=2)
    h = Harness(backend, 650, cfg)
    h.start_from_app()
    h.run(400)
    assert h.car.amps == 11                    # 5, 7, 9, 11; 13 A would exceed 3220 W
    assert h.total() <= 3220


def test_uses_measured_voltage(backend):
    # At 210 V each amp is worth less power, so more amps fit under the same ceiling.
    h = Harness(backend, 650, voltage_v=210)
    h.start_from_app()
    h.run(400)
    assert h.car.amps == 17                    # (4370 - 650) / 210 = 17.7
    assert h.total() <= 4370


def test_implausible_voltage_falls_back(backend):
    cfg = Config()
    assert backend.effective_voltage(cfg, Reading(650, True, 12.0)) == cfg.fallback_voltage_v
    assert backend.effective_voltage(cfg, Reading(650, True, None)) == cfg.fallback_voltage_v
    assert backend.effective_voltage(cfg, Reading(650, True, 241.5)) == 241.5


# ---- Confirming commands from the car's reported state ----

PENDING = Pending(Command(True, 10), sent_at=0.0)


def test_confirmed_when_car_matches(backend):
    assert backend.reconcile(PENDING, CarState(True, 10), 3, 15, 3) == (Action.CONFIRMED, None)


def test_waits_then_resends_then_adopts(backend):
    stale = CarState(False, 0)
    action, p = backend.reconcile(PENDING, stale, 5, 15, 3)
    assert action == Action.WAIT
    action, p = backend.reconcile(p, stale, 16, 15, 3)
    assert (action, p.attempts) == (Action.RESEND, 2)
    action, p = backend.reconcile(p, stale, 32, 15, 3)
    assert (action, p.attempts) == (Action.RESEND, 3)
    assert backend.reconcile(p, stale, 48, 15, 3) == (Action.ADOPT, None)


def test_gives_up_without_state(backend):
    assert backend.reconcile(replace(PENDING, attempts=3), None, 20, 15, 3) == (Action.UNKNOWN, None)


# ---- The owner starts and stops; Volture only sets the amps ----

def test_stop_from_app_is_never_overridden(backend):
    h = Harness(backend, 650)
    h.start_from_app()
    h.run(200)
    assert h.car.charging
    h.ctrl.follow_user(False, 0)               # stopped from the Tesla app
    h.car.force(Command(False, 0))
    h.run(600)
    assert not h.car.charging


def test_start_from_app_is_taken_over(backend):
    h = Harness(backend, 650)
    h.start_from_app(10)                       # started at 10 A from the app
    h.run(400)
    assert h.car.amps == 16                    # every amp that fits, whatever the app said


def test_power_limit_beats_the_app(backend):
    h = Harness(backend, 650)
    h.start_from_app(32)                       # 650 + 32*230 = 8010 W
    h.run(30)
    assert h.total() <= 4370


def test_stop_from_app_cancels_a_resume(backend):
    ctrl = backend.Controller()
    ctrl.follow_user(True, 5)
    ctrl.tick(0, Reading(5000))                # Volture stops it: over the limit
    ctrl.follow_user(False, 0)                 # then the owner stops it in the app too
    ctrl.tick(10, Reading(650))
    assert ctrl.tick(600, Reading(650)) is None


def test_no_resume_when_car_cannot_charge(backend):
    ctrl = backend.Controller()
    ctrl.follow_user(True, 5)
    ctrl.tick(0, Reading(5000))                # Volture stops it: over the limit
    ctrl.tick(10, Reading(650), car_can_charge=False)
    assert ctrl.tick(600, Reading(650), car_can_charge=False) is None


def test_can_charge(backend):
    assert not backend.can_charge(CarState(False, 0, plugged=False))
    assert not backend.can_charge(CarState(False, 0, True, battery_pct=80, limit_pct=80))
    assert backend.can_charge(CarState(False, 0, True, battery_pct=79, limit_pct=80))
    assert backend.can_charge(None)            # unknown never blocks: the car refuses itself


def test_external_change(backend):
    stopped, at10, at16 = CarState(False, 0), CarState(True, 10), CarState(True, 16)
    ours = Pending(Command(True, 16), 0)
    assert not backend.external_change(ours, at10, at16)      # became what Volture asked
    assert not backend.external_change(ours, at10, at10)      # not applied yet: no change
    assert backend.external_change(ours, at10, stopped)       # stopped from the app meanwhile
    assert backend.external_change(None, stopped, at10)       # started from the app
    assert not backend.external_change(None, None, at10)      # first report after startup
    assert not backend.external_change(None, CarState(True, 10, battery_pct=50),
                                       CarState(True, 10, battery_pct=51))  # battery % only


def test_sim_car_refuses_beyond_its_limit():
    # The simulator itself, not the control logic: runs once.
    w = World()
    w.car.battery_pct, w.car.limit_pct = 80, 80
    w.car.force(Command(True, 16))
    assert not w.car.charging

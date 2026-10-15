import unittest
from dataclasses import replace

from volture.confirm import Action, CarState, Pending, can_charge, external_change, reconcile
from volture.controller import (
    Command,
    Config,
    Controller,
    Reading,
    effective_voltage,
    in_offpeak,
)

from simulator.model import World


class Harness:
    def __init__(self, base_w=650.0, cfg=None, voltage_v=230.0):
        self.world, self.ctrl, self.t = World(base_w=base_w, voltage_v=voltage_v), Controller(cfg), 0.0
        self.totals = []
        self.local_hour = None

    def run(self, seconds, tick=5):
        for i in range(seconds):
            self.t += 1
            total = self.world.total_w(self.t)
            self.totals.append((self.t, total))
            if i % tick == 0:
                online = self.world.shelly_online
                r = Reading(total, self.world.shelly_valid, self.world.voltage_v) if online else None
                cmd = self.ctrl.tick(self.t, r, self.local_hour)
                if cmd:
                    self.world.car.command(cmd, self.t)

    @property
    def car(self):
        return self.world.car

    def total(self):
        return self.world.total_w(self.t)


class ControllerTests(unittest.TestCase):
    def test_ramps_to_ceiling(self):
        h = Harness(650)
        h.run(400)
        self.assertTrue(h.car.charging)
        self.assertEqual(h.car.amps, 16)          # (4370-650)/230 = 16.2
        self.assertLessEqual(h.total(), 4370)

    def test_respects_other_loads(self):
        h = Harness(2000)
        h.run(400)
        self.assertEqual(h.car.amps, 10)
        self.assertLessEqual(h.total(), 4370)

    def test_spike_stops_then_resumes(self):
        h = Harness(650)
        h.run(400)
        spike_t = h.t
        h.world.base_w = 3500
        h.run(60)
        self.assertFalse(h.car.charging)
        late = [p for t, p in h.totals if t > spike_t + 20]
        self.assertLessEqual(max(late), 4600)
        h.world.base_w = 650
        h.run(100)
        self.assertTrue(h.car.charging)
        self.assertGreaterEqual(h.car.amps, 5)
        self.assertLessEqual(h.total(), 4370)

    def test_no_restart_if_minimum_does_not_fit(self):
        h = Harness(3800)                          # 3800 + 5*230 > 4370
        h.run(300)
        self.assertFalse(h.car.charging)

    def test_stops_charging_it_did_not_start(self):
        # Car already charging at 32 A when volture starts (or started from the Tesla app):
        # the controller believes it is stopped, but must still stop it once over the contracted power.
        h = Harness(650)
        h.car.charging, h.car.amps = True, 32       # 650 + 32*230 = 8010 W
        h.run(30)
        self.assertFalse(h.car.charging)
        self.assertLessEqual(h.total(), 4600)

    def test_invalid_reading_fails_safe(self):
        h = Harness(650)
        h.run(400)
        h.world.shelly_valid = False
        h.run(10)
        self.assertEqual(h.car.amps, 5)
        h.run(40)
        self.assertFalse(h.car.charging)

    def test_shelly_offline_fails_safe(self):
        h = Harness(650)
        h.run(400)
        h.world.shelly_online = False
        h.run(50)
        self.assertFalse(h.car.charging)


    def test_adopted_stop_waits_before_restarting(self):
        ctrl = Controller()
        ctrl.tick(0, Reading(650))                 # low power: resume timer starts
        ctrl.adopt(True, 32)                       # car started from the app
        ctrl.adopt(False, 0)                       # ...and stopped from the app 100 s later
        self.assertIsNone(ctrl.tick(100, Reading(650)))
        self.assertEqual(ctrl.tick(160, Reading(650)), Command(True, 5))

    def test_settings_drive_the_algorithm(self):
        # A 3.45 kW contract, 2 A steps, 16 A charger: same algorithm, different installation.
        cfg = Config(contracted_power_w=3450, headroom_w=230, max_amps=16, ramp_up_step_amps=2)
        h = Harness(650, cfg)
        h.run(400)
        self.assertEqual(h.car.amps, 11)          # 5, 7, 9, 11; 13 A would exceed 3220 W
        self.assertLessEqual(h.total(), 3220)

    def test_uses_measured_voltage(self):
        # At 210 V each amp is worth less power, so more amps fit under the same ceiling.
        h = Harness(650, voltage_v=210)
        h.run(400)
        self.assertEqual(h.car.amps, 17)          # (4370 - 650) / 210 = 17.7
        self.assertLessEqual(h.total(), 4370)

    def test_implausible_voltage_falls_back(self):
        cfg = Config()
        reading = Reading(650, True, 12.0)
        self.assertEqual(effective_voltage(cfg, reading), cfg.fallback_voltage_v)

    def test_offpeak_window_across_midnight(self):
        cfg = Config(offpeak_start_hour=22, offpeak_end_hour=7)
        self.assertEqual([h for h in range(24) if in_offpeak(cfg, h)], [0, 1, 2, 3, 4, 5, 6, 22, 23])
        self.assertTrue(in_offpeak(Config(), 15))  # no window: always allowed

    def test_charges_only_inside_offpeak(self):
        h = Harness(650, Config(offpeak_start_hour=0, offpeak_end_hour=8))
        h.local_hour = 12
        h.run(300)
        self.assertFalse(h.car.charging)          # never starts outside the window
        h.local_hour = 1
        h.run(300)
        self.assertTrue(h.car.charging)
        h.local_hour = 8
        h.run(20)
        self.assertFalse(h.car.charging)          # stopped as the window closes


class ConfirmTests(unittest.TestCase):
    def setUp(self):
        self.Action, self.CarState, self.reconcile = Action, CarState, reconcile  # short names below
        self.pending = Pending(Command(True, 10), sent_at=0.0)

    def test_confirmed_when_car_matches(self):
        action, left = self.reconcile(self.pending, self.CarState(True, 10), 3, 15, 3)
        self.assertEqual((action, left), (self.Action.CONFIRMED, None))

    def test_waits_then_resends_then_adopts(self):
        stale = self.CarState(False, 0)
        action, p = self.reconcile(self.pending, stale, 5, 15, 3)
        self.assertEqual(action, self.Action.WAIT)
        action, p = self.reconcile(p, stale, 16, 15, 3)
        self.assertEqual((action, p.attempts), (self.Action.RESEND, 2))
        action, p = self.reconcile(p, stale, 32, 15, 3)
        self.assertEqual((action, p.attempts), (self.Action.RESEND, 3))
        action, p = self.reconcile(p, stale, 48, 15, 3)
        self.assertEqual((action, p), (self.Action.ADOPT, None))

    def test_gives_up_without_state(self):
        action, left = self.reconcile(replace(self.pending, attempts=3), None, 20, 15, 3)
        self.assertEqual((action, left), (self.Action.UNKNOWN, None))



class TeslaAppPriorityTests(unittest.TestCase):
    def test_stop_from_app_is_never_overridden(self):
        h = Harness(650)
        h.run(200)
        self.assertTrue(h.car.charging)
        h.ctrl.follow_user(False, 0)               # stopped from the Tesla app
        h.car.force(Command(False, 0))
        h.run(600)
        self.assertFalse(h.car.charging)

    def test_app_amps_are_a_ceiling(self):
        h = Harness(650)
        h.ctrl.follow_user(True, 10)               # started at 10 A from the app
        h.car.force(Command(True, 10))
        h.run(400)
        self.assertEqual(h.car.amps, 10)           # room for 16 A, but the app said 10

    def test_power_limit_beats_the_app(self):
        h = Harness(650)
        h.ctrl.follow_user(True, 32)
        h.car.force(Command(True, 32))             # 650 + 32*230 = 8010 W
        h.run(30)
        self.assertLessEqual(h.total(), 4370)

    def test_app_start_ignores_offpeak_window(self):
        h = Harness(650, Config(offpeak_start_hour=0, offpeak_end_hour=8))
        h.local_hour = 12
        h.ctrl.follow_user(True, 10)
        h.car.force(Command(True, 10))
        h.run(100)
        self.assertTrue(h.car.charging)

    def test_unplug_hands_control_back(self):
        ctrl = Controller()
        ctrl.follow_user(False, 0)
        ctrl.clear_user()
        ctrl.tick(0, Reading(650))
        self.assertEqual(ctrl.tick(60, Reading(650)), Command(True, 5))

    def test_no_start_when_car_cannot_charge(self):
        ctrl = Controller()
        ctrl.tick(0, Reading(650), car_can_charge=False)
        self.assertIsNone(ctrl.tick(600, Reading(650), car_can_charge=False))

    def test_can_charge(self):
        self.assertFalse(can_charge(CarState(False, 0, plugged=False)))
        self.assertFalse(can_charge(CarState(False, 0, True, battery_pct=80, limit_pct=80)))
        self.assertTrue(can_charge(CarState(False, 0, True, battery_pct=79, limit_pct=80)))
        self.assertTrue(can_charge(None))          # unknown never blocks: the car refuses itself

    def test_external_change(self):
        stopped, at10, at16 = CarState(False, 0), CarState(True, 10), CarState(True, 16)
        ours = Pending(Command(True, 16), 0)
        self.assertFalse(external_change(ours, at10, at16))   # became what Volture asked
        self.assertFalse(external_change(ours, at10, at10))   # not applied yet: no change
        self.assertTrue(external_change(ours, at10, stopped))  # stopped from the app meanwhile
        self.assertTrue(external_change(None, stopped, at10))  # started from the app
        self.assertFalse(external_change(None, None, at10))    # first report after startup
        self.assertFalse(external_change(None, CarState(True, 10, battery_pct=50),
                                         CarState(True, 10, battery_pct=51)))  # battery % only

    def test_sim_car_refuses_beyond_its_limit(self):
        w = World()
        w.car.battery_pct, w.car.limit_pct = 80, 80
        w.car.force(Command(True, 16))
        self.assertFalse(w.car.charging)


if __name__ == "__main__":
    unittest.main()

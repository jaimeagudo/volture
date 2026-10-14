import unittest

from sirwattson.controller import Controller, Reading
from sirwattson.sim import World


class Harness:
    def __init__(self, base_w=650.0):
        self.world, self.ctrl, self.t = World(base_w=base_w), Controller(), 0.0
        self.totals = []

    def run(self, seconds, tick=5):
        for i in range(seconds):
            self.t += 1
            total = self.world.total_w(self.t)
            self.totals.append((self.t, total))
            if i % tick == 0:
                online = self.world.shelly_online
                r = Reading(total, self.world.shelly_valid) if online else None
                cmd = self.ctrl.tick(self.t, r)
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
        # Car already charging at 32 A when SirWattson starts (or started from the Tesla app):
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


if __name__ == "__main__":
    unittest.main()

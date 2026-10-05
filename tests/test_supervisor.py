import tempfile
import unittest
from pathlib import Path

from swarm.ledger import Ledger
from swarm.registry import Registry
from swarm.supervisor import BreakerState, CircuitBreaker, Supervisor

ROOT = Path(__file__).resolve().parents[1]


class TestCircuitBreaker(unittest.TestCase):
    def test_trips_after_threshold(self):
        br = CircuitBreaker(threshold=3, cooldown_s=10)
        self.assertFalse(br.record(False, now=0))
        self.assertFalse(br.record(False, now=1))
        self.assertTrue(br.record(False, now=2))
        self.assertEqual(br.state, BreakerState.OPEN)

    def test_open_blocks_until_cooldown_then_half_opens(self):
        br = CircuitBreaker(threshold=1, cooldown_s=10)
        br.record(False, now=0)
        self.assertFalse(br.allow(now=5))
        self.assertTrue(br.allow(now=11))
        self.assertEqual(br.state, BreakerState.HALF_OPEN)

    def test_success_closes_breaker(self):
        br = CircuitBreaker(threshold=1, cooldown_s=10)
        br.record(False, now=0)
        br.allow(now=11)  # half-open
        br.record(True, now=12)
        self.assertEqual(br.state, BreakerState.CLOSED)
        self.assertEqual(br.failures, 0)


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = Registry.load(ROOT / "swarm" / "cabinet.json")
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.restarted = []

    def tearDown(self):
        self.tmp.cleanup()

    def _supervisor(self, probe, restarter=None):
        return Supervisor(
            self.registry,
            self.ledger,
            state_path=self.root / "logs" / "state.json",
            prober=probe,
            restarter=restarter,
        )

    def test_all_healthy(self):
        sup = self._supervisor(lambda e: True)
        report = sup.tick()
        self.assertTrue(report["healthy"])
        self.assertEqual(report["unhealthy"], [])

    def test_failures_trip_breaker_and_trigger_restart(self):
        sup = self._supervisor(lambda e: False, restarter=lambda e: self.restarted.append(e.id) or True)
        for _ in range(3):
            report = sup.tick()
        self.assertTrue(report["breaker_open"])
        self.assertIn(report["breaker_open"][0], self.restarted)
        self.assertGreaterEqual(self.ledger.count("breaker_open"), 1)

    def test_open_breaker_skips_probe(self):
        calls = {"n": 0}

        def probe(e):
            calls["n"] += 1
            return False

        sup = self._supervisor(probe)
        for _ in range(3):
            sup.tick()
        before = calls["n"]
        sup.tick()  # breaker open → skipped, no probe
        self.assertEqual(calls["n"], before)
        self.assertTrue(self.ledger.query(type="health_skip"))

    def test_state_persists_across_instances(self):
        sup = self._supervisor(lambda e: False)
        for _ in range(3):
            sup.tick()
        reopened = self._supervisor(lambda e: True)
        self.assertTrue(any(br.state == BreakerState.CLOSED or br.state == BreakerState.OPEN
                            for br in reopened._breakers.values()))

    def test_backoff_grows_and_caps(self):
        sup = self._supervisor(lambda e: False)
        self.assertEqual(sup.backoff_for(1), 5.0)
        self.assertEqual(sup.backoff_for(2), 10.0)
        self.assertLessEqual(sup.backoff_for(20), 300.0)


if __name__ == "__main__":
    unittest.main()

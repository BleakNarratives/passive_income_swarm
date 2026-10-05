import json
import tempfile
import unittest
from pathlib import Path

from swarm.breaker import (
    MODE_OPEN,
    MODE_STRICT,
    IngestBreaker,
    guarded_submit_raw,
)
from swarm.contracts import Envelope
from swarm.whorl import WhorlBus


class TestIngestBreaker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bus = WhorlBus(self.root / "bus.jsonl")
        self.breaker = IngestBreaker(self.root / "circuit_breaker.json", bus=self.bus)

    def tearDown(self):
        self.tmp.cleanup()

    def test_trips_on_third_failure(self):
        self.assertFalse(self.breaker.record_violation("schema", "bad 1"))
        self.assertFalse(self.breaker.record_violation("digest", "bad 2"))
        # The 3rd schema/digest failure locks the bus.
        self.assertTrue(self.breaker.record_violation("schema", "bad 3"))
        self.assertTrue(self.breaker.is_locked())
        self.assertEqual(self.breaker.mode, MODE_STRICT)
        self.assertEqual(self.breaker.status()["violations_in_window"], 3)

    def test_trip_flushes_staged_buffer(self):
        env = Envelope.build("scout", "telemetry", {"x": 1})
        self.bus.stage(env)
        for i in range(3):
            self.breaker.record_violation("schema", f"bad {i}")
        self.assertTrue(self.breaker.is_locked())
        # Staged state was flushed, not lost.
        self.assertEqual(len(self.bus.records()), 1)

    def test_preflight_release_is_manual(self):
        for i in range(3):
            self.breaker.record_violation("schema", f"bad {i}")
        self.assertTrue(self.breaker.is_locked())
        self.breaker.preflight_pass(by="operator")
        self.assertFalse(self.breaker.is_locked())
        self.assertEqual(self.breaker.mode, MODE_OPEN)
        self.assertEqual(self.breaker.status()["violations_in_window"], 0)

    def test_state_survives_reload(self):
        for i in range(3):
            self.breaker.record_violation("schema", f"bad {i}")
        reloaded = IngestBreaker(self.root / "circuit_breaker.json")
        self.assertTrue(reloaded.is_locked())

    def test_window_prunes_old_violations(self):
        breaker = IngestBreaker(self.root / "w.json", window_s=10, bus=self.bus)
        breaker.record_violation("schema", "a", now=0)
        breaker.record_violation("schema", "b", now=5)
        breaker.record_violation("schema", "c", now=100)  # prunes a and b
        self.assertEqual(breaker.status()["violations_in_window"], 1)
        self.assertFalse(breaker.is_locked())

    def test_guarded_submit_counts_unverified(self):
        good = json.dumps(Envelope.build("scout", "telemetry", {"x": 1}).to_sidecar())
        self.assertIsNotNone(guarded_submit_raw(self.bus, self.breaker, good))
        for _ in range(3):
            self.assertIsNone(guarded_submit_raw(self.bus, self.breaker, "{bad json"))
        self.assertTrue(self.breaker.is_locked())
        self.assertEqual(self.breaker.status()["violations_in_window"], 3)

    def test_status_shape(self):
        status = self.breaker.status()
        self.assertEqual(
            set(status), {"mode", "locked", "locked_at", "violations_in_window", "threshold", "window_s"}
        )


if __name__ == "__main__":
    unittest.main()

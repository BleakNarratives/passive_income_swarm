import calendar
import tempfile
import time
import unittest
from pathlib import Path

from swarm.contracts import Envelope, PayloadError
from swarm.ledger import Ledger
from swarm.whorl import WhorlBus


def epoch(ts):
    return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))


class TestWhorlBus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def test_emit_and_read(self):
        env = self.bus.emit("augur", "intel", {"text": "hello"}, confidence=0.9)
        records = self.bus.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].id, env.id)
        self.assertEqual(self.ledger.count("bus_signal"), 1)

    def test_high_confidence_filter(self):
        self.bus.emit("a", "intel", {"x": 1}, confidence=0.4)
        self.bus.emit("a", "intel", {"x": 2}, confidence=0.95)
        high = self.bus.high_confidence(0.75)
        self.assertEqual(len(high), 1)
        self.assertAlmostEqual(high[0].confidence, 0.95)

    def test_bad_envelope_rejected(self):
        with self.assertRaises(PayloadError):
            self.bus.publish(Envelope(emitter="x", kind="intel", payload={"a": 1}, content_sha256="0" * 64))

    def test_drain_respects_cursor(self):
        self.bus.emit("a", "telemetry", {"n": 1})
        first = self.bus.drain()
        self.assertEqual(len(first), 1)
        self.assertEqual(self.bus.drain(), [])
        self.bus.emit("a", "telemetry", {"n": 2})
        self.assertEqual(len(self.bus.drain()), 1)
        self.bus.seek(0)
        self.assertEqual(len(self.bus.drain()), 2)

    def test_pheromone_deposit_and_decay(self):
        env = self.bus.deposit("augur", "niche-ai-agents", strength=1.0, half_life_s=1000)
        now = epoch(env.created)
        fresh = self.bus.pheromone_map(now=now)
        self.assertAlmostEqual(fresh["niche-ai-agents"], 1.0, places=3)
        decayed = self.bus.pheromone_map(now=now + 1000)
        self.assertAlmostEqual(decayed["niche-ai-agents"], 0.5, places=3)
        gone = self.bus.pheromone_map(now=now + 10000)
        self.assertLess(gone["niche-ai-agents"], 0.2)

    def test_pheromones_sum_on_same_trail(self):
        e1 = self.bus.deposit("a", "trail", strength=1.0, half_life_s=10000)
        self.bus.deposit("b", "trail", strength=1.0, half_life_s=10000)
        total = self.bus.pheromone_map(now=epoch(e1.created))["trail"]
        self.assertAlmostEqual(total, 2.0, places=2)

    def test_hottest_ranks_trails(self):
        e = self.bus.deposit("a", "cold", strength=0.2, half_life_s=10000)
        self.bus.deposit("a", "hot", strength=1.0, half_life_s=10000)
        ranked = self.bus.hottest()
        self.assertEqual(ranked[0]["trail"], "hot")


if __name__ == "__main__":
    unittest.main()

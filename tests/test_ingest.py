import tempfile
import unittest
from pathlib import Path

from swarm.breaker import IngestBreaker
from swarm.discovery import ScoutRegistry
from swarm.incentives import ScoutIncentives
from swarm.ingest import Ingestor
from swarm.ledger import Ledger
from swarm.lineage import LineageGate
from swarm.skills import SkillStore
from swarm.whorl import WhorlBus

LIVE = "projects/agents/scouts/alpha"


class TestIngestor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)
        self.store = SkillStore(self.root / "skills" / "registry.json", ledger=self.ledger)
        self.incentives = ScoutIncentives(store=self.store)
        self.ingestor = Ingestor(
            self.bus, self.ledger, gate=LineageGate(), store=self.store, incentives=self.incentives
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_skill_update_from_live_lineage_is_applied(self):
        self.bus.emit("scout", "skill_update", {"skill_id": "fresh_skill", "name": "Fresh"}, confidence=0.9, lineage=LIVE)
        result = self.ingestor.run_once()
        self.assertEqual(result["accepted"], 1)
        self.assertIsNotNone(self.store.get("fresh_skill"))

    def test_skill_update_from_archive_rejected(self):
        self.bus.emit("scout", "skill_update", {"skill_id": "dead_skill"}, confidence=0.9, lineage="._archive/old")
        result = self.ingestor.run_once()
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertIsNone(self.store.get("dead_skill"))
        self.assertEqual(self.ledger.count("payload_rejected"), 1)

    def test_skill_update_from_unknown_lineage_fails_closed(self):
        self.bus.emit("scout", "skill_update", {"skill_id": "ghost"}, confidence=0.9, lineage="random/notes")
        result = self.ingestor.run_once()
        self.assertEqual(result["accepted"], 0)
        self.assertIsNone(self.store.get("ghost"))

    def test_telemetry_from_unknown_lineage_accepted(self):
        self.bus.emit("scout", "telemetry", {"x": 1}, confidence=0.5, lineage="random/notes")
        result = self.ingestor.run_once()
        self.assertEqual(result["accepted"], 1)

    def test_confidence_floor(self):
        ingestor = Ingestor(self.bus, self.ledger, gate=LineageGate(), min_confidence=0.8)
        self.bus.emit("scout", "telemetry", {"x": 1}, confidence=0.2, lineage=LIVE)
        result = ingestor.run_once()
        self.assertEqual(result["rejected"], 1)
        self.assertIn("below confidence floor", result["rejected_reasons"])

    def test_intel_is_scored_for_incentive(self):
        self.bus.emit(
            "augur", "intel",
            {"subject": "niche", "text": "rare quantum logistics niche", "signals": {"revenue": 0.9, "niche": 1.0}},
            confidence=0.9, lineage=LIVE,
        )
        result = self.ingestor.run_once()
        self.assertEqual(len(result["rewards"]), 1)
        self.assertEqual(self.ledger.count("incentive"), 1)
        self.assertGreater(result["rewards"][0]["total"], 0.0)

    def test_intel_from_dead_lineage_rejected(self):
        self.bus.emit("augur", "intel", {"text": "x"}, confidence=0.9, lineage="UNPACKED/dump")
        result = self.ingestor.run_once()
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(self.ledger.count("incentive"), 0)

    def test_intel_with_trail_deposits_pheromone(self):
        self.bus.emit(
            "augur", "intel", {"text": "hot trail find", "trail": "niche-x"},
            confidence=0.9, lineage=LIVE,
        )
        self.ingestor.run_once()
        self.assertIn("niche-x", self.bus.pheromone_map())

    def test_second_run_is_idempotent_on_empty_bus(self):
        self.bus.emit("scout", "telemetry", {"x": 1}, confidence=0.5, lineage=LIVE)
        self.ingestor.run_once()
        again = self.ingestor.run_once()
        self.assertEqual(again["accepted"], 0)

    def test_strict_mode_rejects_unregistered_emitter(self):
        breaker = IngestBreaker(self.root / "cb.json", bus=self.bus)
        registry = ScoutRegistry(path=self.root / "SCOUT.json", quarantine_dir=self.root / "q")
        ingestor = Ingestor(
            self.bus, self.ledger, gate=LineageGate(), store=self.store,
            incentives=self.incentives, breaker=breaker, registry=registry,
        )
        self.bus.emit("ghost", "telemetry", {"x": 1}, confidence=0.5, lineage=LIVE)
        breaker.trip("test lock", bus=self.bus)
        result = ingestor.run_once()
        self.assertEqual(result["mode"], "STRICT_INGEST_ONLY")
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(self.ledger.count("feral_emitter"), 1)

    def test_strict_mode_accepts_registered_emitter(self):
        breaker = IngestBreaker(self.root / "cb2.json", bus=self.bus)
        registry = ScoutRegistry(path=self.root / "SCOUT2.json", quarantine_dir=self.root / "q2")
        registry.register("augur", digest="abc")
        ingestor = Ingestor(
            self.bus, self.ledger, gate=LineageGate(), store=self.store,
            incentives=self.incentives, breaker=breaker, registry=registry,
        )
        self.bus.emit("augur", "telemetry", {"x": 1}, confidence=0.5, lineage=LIVE)
        breaker.trip("lock", bus=self.bus)
        result = ingestor.run_once()
        self.assertEqual(result["mode"], "STRICT_INGEST_ONLY")
        self.assertEqual(result["accepted"], 1)


if __name__ == "__main__":
    unittest.main()

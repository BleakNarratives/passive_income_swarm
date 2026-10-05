import json
import os
import tempfile
import types
import unittest
from pathlib import Path

from swarm.breaker import IngestBreaker
from swarm.contracts import Envelope
from swarm.discovery import ScoutRegistry
from swarm.graft import GraftGuard
from swarm.ledger import Ledger
from swarm.lineage import PURPOSE_SELF_MODIFY, LineageGate, deprecate_module
from swarm.whorl import WhorlBus


def _bad_line(payload):
    """A raw bus record whose content_sha256 does not match its payload."""
    return json.dumps(
        {
            "schema": "swarm.payload/1",
            "id": "bad",
            "emitter": "feral",
            "kind": "telemetry",
            "content_sha256": "0" * 64,
            "confidence": 0.5,
            "payload": payload,
        }
    )


class TestBusContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)
        self.breaker = IngestBreaker(self.root / "circuit_breaker.json", bus=self.bus)
        self.bus.attach_breaker(self.breaker)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_accepts_valid_digest(self):
        self.bus.emit("scout", "telemetry", {"a": 1}, confidence=0.9)
        result = self.bus.ingest()
        self.assertEqual(result["mode"], "OPEN")
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["rejected"], 0)

    def test_ingest_rejects_bad_digest(self):
        with self.bus.path.open("a", encoding="utf-8") as fh:
            fh.write(_bad_line({"a": 1}) + "\n")
        result = self.bus.ingest()
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertTrue(result["errors"])

    def test_ingest_counts_violations_and_locks_on_third(self):
        with self.bus.path.open("a", encoding="utf-8") as fh:
            for i in range(3):
                fh.write(_bad_line({"n": i}) + "\n")
        result = self.bus.ingest()
        self.assertEqual(result["rejected"], 3)
        self.assertTrue(self.breaker.is_locked())

    def test_ingest_refuses_when_locked(self):
        self.breaker.trip("test")
        result = self.bus.ingest()
        self.assertEqual(result["mode"], "STRICT_INGEST_ONLY")
        self.assertEqual(result["accepted"], 0)


class TestAdaptiveAbsorb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)
        memory = self.root / "adaptive_memory.json"
        memory.write_text(
            json.dumps({"modules": {"legacy": {"pheromone_density": 2.5, "incentives": 1.0,
                                               "revenue_msat": 500, "runs": 3}}}),
            encoding="utf-8",
        )
        self.guard = GraftGuard(bus=self.bus, ledger=self.ledger, adaptive_memory_path=memory)

    def tearDown(self):
        self.tmp.cleanup()

    def test_absorb_carries_density_and_yield(self):
        result = self.guard.absorb_adaptive_memory("legacy", "new")
        self.assertTrue(result["absorbed"])
        self.assertAlmostEqual(result["density"], 2.5)
        self.assertAlmostEqual(result["yield"], 1.5)
        self.assertGreater(self.bus.pheromone_by_emitter().get("new", 0.0), 0.0)
        self.assertEqual(self.ledger.count("adaptive_memory_absorbed"), 1)

    def test_absent_module_absorbs_nothing(self):
        result = self.guard.absorb_adaptive_memory("ghost", "new")
        self.assertFalse(result["absorbed"])
        self.assertEqual(self.ledger.count("adaptive_memory_absorbed"), 0)


class TestDeprecate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)
        memory = self.root / "adaptive_memory.json"
        memory.write_text(
            json.dumps({"modules": {"legacy": {"pheromone_density": 1.0, "incentives": 0.5,
                                               "revenue_msat": 0, "runs": 1}}}),
            encoding="utf-8",
        )
        self.guard = GraftGuard(bus=self.bus, ledger=self.ledger, adaptive_memory_path=memory)
        self.gate = LineageGate()

    def tearDown(self):
        self.tmp.cleanup()

    def test_deprecate_migrates_then_denies(self):
        self.bus.deposit("legacy", "trail", strength=3.0, half_life_s=100000)
        result = deprecate_module("legacy", "new", self.guard, gate=self.gate, ledger=self.ledger)
        self.assertTrue(result["deprecated"])
        self.assertGreater(result["strength_moved"], 0.0)
        self.assertTrue(result["absorbed"]["absorbed"])
        self.assertEqual(self.ledger.count("module_deprecated"), 1)
        self.assertEqual(self.ledger.count("pheromone_merge"), 1)
        # The deprecated module is now denied by the gate.
        self.assertFalse(self.gate.permit("legacy/scout.py", PURPOSE_SELF_MODIFY))
        self.assertEqual(self.gate.decision("legacy/scout.py", PURPOSE_SELF_MODIFY).classification, "deprecated")

    def test_deprecate_requires_a_guard(self):
        with self.assertRaises(ValueError):
            deprecate_module("a", "b", None)


class TestGatePreserve(unittest.TestCase):
    """merge_pheromones() must run before the gate evaluates a module."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)
        self.guard = GraftGuard(bus=self.bus, ledger=self.ledger)
        self.gate = LineageGate()
        self.gate.attach_guard(self.guard, preserve_into="live_lineage")

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_runs_before_evaluation(self):
        self.bus.deposit("legacy", "trail", strength=2.0, half_life_s=100000)
        self.gate.check("legacy/scout.py", PURPOSE_SELF_MODIFY, ledger=self.ledger)
        self.assertGreater(self.bus.pheromone_by_emitter().get("live_lineage", 0.0), 0.0)
        self.assertEqual(self.ledger.count("pheromone_merge"), 1)

    def test_preserve_runs_once_per_module(self):
        self.bus.deposit("legacy", "trail", strength=2.0, half_life_s=100000)
        self.gate.check("legacy/scout.py", PURPOSE_SELF_MODIFY, ledger=self.ledger)
        self.gate.check("legacy/other.py", PURPOSE_SELF_MODIFY, ledger=self.ledger)
        self.assertEqual(self.ledger.count("pheromone_merge"), 1)

    def test_no_guard_is_a_noop(self):
        gate = LineageGate()
        decision = gate.check("legacy/scout.py", PURPOSE_SELF_MODIFY, ledger=self.ledger)
        self.assertEqual(self.ledger.count("pheromone_merge"), 0)
        self.assertIn(decision.classification, ("unknown", "allow"))


class TestDiscoverySync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reg = ScoutRegistry(
            path=self.root / "SCOUT_REGISTRY.json",
            quarantine_dir=self.root / "quarantine",
        )
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def test_sync_registers_existing_allowed_scripts(self):
        scouts = self.root / "projects" / "agents" / "scouts"
        scouts.mkdir(parents=True, exist_ok=True)
        (scouts / "s1.exs").write_text("IO.puts(1)\n", encoding="utf-8")
        (scouts / "s2.js").write_text("console.log(1)\n", encoding="utf-8")
        os.chdir(self.root)

        entities = [
            types.SimpleNamespace(kind="scout", entrypoint="projects/agents/scouts/s1.exs"),
            types.SimpleNamespace(kind="agent", entrypoint="projects/agents/scouts/s2.js"),
            types.SimpleNamespace(kind="scout", entrypoint="projects/agents/scouts/missing.exs"),
            types.SimpleNamespace(kind="daemon", entrypoint="-m swarm.cli supervise"),
        ]
        registered = self.reg.sync_entities(entities)
        scripts = {e["script"] for e in registered}
        self.assertEqual(scripts, {"projects/agents/scouts/s1.exs", "projects/agents/scouts/s2.js"})
        self.assertTrue(self.reg.is_registered("projects/agents/scouts/s1.exs"))


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from swarm.ledger import Ledger
from swarm.registry import Registry
from swarm.scouts import ScoutEngine
from swarm.skills import Skill, SkillStore

ROOT = Path(__file__).resolve().parents[1]


class TestScoutEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.registry = Registry.load(ROOT / "swarm" / "cabinet.json")
        self.store = SkillStore(self.root / "skills" / "registry.json", ledger=self.ledger)
        self.engine = ScoutEngine(self.store, self.ledger, registry=self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_proposes_skill_for_repeated_success(self):
        for _ in range(4):
            self.ledger.append("complete", {"entity": "thoth", "ok": True}, actor="worker")
        findings = self.engine.discover(min_occurrences=3)
        subjects = [f.subject for f in findings]
        self.assertIn("auto.thoth", subjects)
        finding = next(f for f in findings if f.subject == "auto.thoth")
        self.assertIn("llm.generate", finding.payload["capabilities"])

    def test_discover_apply_registers_emergent_skill(self):
        for _ in range(3):
            self.ledger.append("complete", {"entity": "molt", "ok": True})
        self.engine.discover(min_occurrences=3, apply=True)
        skill = self.store.get("auto.molt")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.kind, "emergent")

    def test_discover_does_not_duplicate_existing_skill(self):
        self.store.register(Skill(id="auto.thoth", name="Thoth", kind="native"))
        for _ in range(3):
            self.ledger.append("complete", {"entity": "thoth", "ok": True})
        subjects = [f.subject for f in self.engine.discover(min_occurrences=3)]
        self.assertNotIn("auto.thoth", subjects)

    def test_discover_flags_repeated_failures(self):
        for _ in range(3):
            self.ledger.append("complete", {"entity": "dvm_worker", "ok": False})
        findings = self.engine.discover(min_occurrences=3)
        self.assertTrue(any(f.kind == "propose_remediation" for f in findings))

    def test_evaluate_recommends_retire_for_low_score(self):
        store = SkillStore(self.root / "skills" / "e.json", retire_after=3, retire_below=0.5,
                           auto_certify=False)
        store.register(Skill(id="bad", name="Bad"))
        for _ in range(4):
            store.record_run("bad", 0.2)
        engine = ScoutEngine(store, self.ledger)
        findings = engine.evaluate()
        self.assertTrue(any(f.kind == "retire" and f.subject == "bad" for f in findings))

    def test_audit_finds_undeclared_capability(self):
        self.ledger.append("dispense", {"payload": {"capabilities": ["quantum.teleport"]}})
        findings = self.engine.audit_capabilities()
        self.assertTrue(any(f.kind == "capability_gap" and f.subject == "quantum.teleport" for f in findings))

    def test_run_sweep_returns_all_buckets(self):
        self.ledger.append("dispense", {"payload": {"capabilities": ["x.y"]}})
        result = self.engine.run()
        self.assertEqual(set(result.keys()), {"discovered", "evaluated", "capability_audit"})


if __name__ == "__main__":
    unittest.main()

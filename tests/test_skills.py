import tempfile
import unittest
from pathlib import Path

from swarm.ledger import Ledger
from swarm.skills import MasteryGate, Skill, SkillError, SkillStore


class TestSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.store = SkillStore(self.root / "skills" / "registry.json", ledger=self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_and_persist(self):
        self.store.register(Skill(id="s1", name="One"))
        reloaded = SkillStore(self.root / "skills" / "registry.json")
        self.assertIsNotNone(reloaded.get("s1"))

    def test_duplicate_register_rejected(self):
        self.store.register(Skill(id="s1", name="One"))
        with self.assertRaises(SkillError):
            self.store.register(Skill(id="s1", name="One"))

    def test_ewma_score_moves_toward_reward(self):
        self.store.register(Skill(id="s1", name="One"))
        self.store.record_run("s1", 1.0)
        first = self.store.get("s1").score
        self.assertAlmostEqual(first, 1.0)
        for _ in range(20):
            self.store.record_run("s1", 0.5)
        self.assertLess(self.store.get("s1").score, 0.7)
        self.assertGreater(self.store.get("s1").score, 0.5)

    def test_reward_bounds_enforced(self):
        self.store.register(Skill(id="s1", name="One"))
        with self.assertRaises(SkillError):
            self.store.record_run("s1", 1.5)

    def test_failure_caps_reward(self):
        self.store.register(Skill(id="s1", name="One"))
        skill = self.store.record_run("s1", 1.0, ok=False)
        self.assertEqual(skill.score, 0.25)
        self.assertEqual(skill.failures, 1)

    def test_level_promotes_with_volume_and_score(self):
        self.store.register(Skill(id="s1", name="One"))
        for _ in range(10):
            self.store.record_run("s1", 0.9)
        self.assertIn(self.store.get("s1").level, ("ADEPT", "EXPERT", "MASTER", "CERTIFIED"))

    def test_certification_gate_blocks_weak_and_passes_strong(self):
        gate = MasteryGate(min_runs=5, min_score=0.8, max_stdev=0.2)
        store = SkillStore(self.root / "skills" / "gate.json", gate=gate, auto_certify=False)
        store.register(Skill(id="weak", name="Weak"))
        for _ in range(6):
            store.record_run("weak", 0.4)
        self.assertFalse(store.certify("weak")["passed"])

        store.register(Skill(id="strong", name="Strong"))
        for _ in range(6):
            store.record_run("strong", 0.9)
        self.assertTrue(store.certify("strong")["passed"])
        self.assertTrue(store.get("strong").certified)

    def test_low_score_flagged_for_review(self):
        store = SkillStore(
            self.root / "skills" / "flag.json",
            retire_after=3,
            retire_below=0.5,
            auto_certify=False,
        )
        store.register(Skill(id="bad", name="Bad"))
        for _ in range(4):
            store.record_run("bad", 0.2)
        self.assertTrue(store.get("bad").flagged)

    def test_propose_is_idempotent_and_versions(self):
        skill = self.store.propose("auto.dvm_worker", "Auto", ["net.nostr"], {"discovered_by": "skill_scout"})
        self.assertEqual(skill.kind, "emergent")
        again = self.store.propose("auto.dvm_worker", "Auto", ["net.nostr"], {"discovered_by": "skill_scout"})
        self.assertEqual(again.version, 2)

    def test_leaderboard_and_summary(self):
        self.store.register(Skill(id="a", name="A"))
        self.store.register(Skill(id="b", name="B"))
        for _ in range(5):
            self.store.record_run("a", 0.9)
            self.store.record_run("b", 0.3)
        self.assertEqual(self.store.leaderboard()[0].id, "a")
        self.assertEqual(self.store.summary()["total"], 2)
        self.assertEqual(self.ledger.count("skill_run"), 10)


if __name__ == "__main__":
    unittest.main()

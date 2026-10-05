import tempfile
import unittest
from pathlib import Path

from swarm.incentives import ScoutIncentives
from swarm.ledger import Ledger
from swarm.skills import Skill, SkillStore


class TestIncentives(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.store = SkillStore(self.root / "skills" / "registry.json", ledger=self.ledger)
        self.store.register(Skill(id="market_analysis", name="Market Analysis", capabilities=["llm.generate"]))
        self.inc = ScoutIncentives(store=self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_novel_text_beats_known_text(self):
        known = self.inc.novelty("market analysis llm generate")
        novel = self.inc.novelty("quantum photonics arbitrage microstructure")
        self.assertGreater(novel, known)

    def test_rarity_needs_fit_and_rewards_rare_tokens(self):
        self.inc.fit(["common common common", "common other", "common third"])
        common = self.inc.rarity("common common common")
        rare = self.inc.rarity("exotic zebra quasar")
        self.assertGreater(rare, common)

    def test_marketability_weights_present_signals_only(self):
        strong = self.inc.marketability({"revenue": 1.0, "niche": 1.0, "demand": 1.0})
        weak = self.inc.marketability({"revenue": 0.1})
        self.assertGreater(strong, weak)
        self.assertEqual(self.inc.marketability(None), 0.0)

    def test_outside_box_bonus_raises_total(self):
        text = "entirely unprecedented suborbital logistics niche"
        base = ScoutIncentives(store=self.store, outside_box_threshold=1.1).score("s", text, lineage_ok=True)
        boosted = self.inc.score("s", text, lineage_ok=True)
        self.assertTrue(boosted.outside_box)
        self.assertGreater(boosted.total, base.total)

    def test_dead_lineage_zeroes_reward(self):
        found = self.inc.score("s", "amazing rare intel", lineage_ok=False)
        self.assertEqual(found.total, 0.0)
        self.assertFalse(found.outside_box)

    def test_total_bounds(self):
        found = self.inc.score("s", "rare", signals={"revenue": 1.0}, confidence=1.0)
        self.assertGreaterEqual(found.total, 0.0)
        self.assertLessEqual(found.total, 1.0)

    def test_record_books_ledger_and_skill_score(self):
        self.store.register(Skill(id="scout_find", name="Scout Find"))
        found = self.inc.score("scout_find", "extremely rare niche signal", confidence=0.9)
        self.inc.record(found, ledger=self.ledger, store=self.store)
        self.assertEqual(self.ledger.count("incentive"), 1)
        self.assertEqual(self.store.get("scout_find").runs, 1)


if __name__ == "__main__":
    unittest.main()

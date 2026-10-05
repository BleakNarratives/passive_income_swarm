import json
import tempfile
import unittest
from pathlib import Path

from swarm.graft import GraftGuard
from swarm.ledger import Ledger
from swarm.whorl import WhorlBus


class TestGraftGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)
        self.guard = GraftGuard(bus=self.bus, ledger=self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loser_dominant_by_pheromone_requires_merge(self):
        self.bus.deposit("loser_mod", "trail-a", strength=3.0, half_life_s=100000)
        decision = self.guard.pre_graft("winner_mod", "loser_mod")
        self.assertTrue(decision.loser_dominant)
        self.assertTrue(decision.merge_required)
        self.assertIn("merge required", decision.reason)

    def test_winner_dominant_is_safe(self):
        self.bus.deposit("winner_mod", "trail-a", strength=5.0, half_life_s=100000)
        decision = self.guard.pre_graft("winner_mod", "loser_mod")
        self.assertFalse(decision.loser_dominant)
        self.assertFalse(decision.merge_required)
        self.assertIn("safe to graft", decision.reason)

    def test_inspection_reads_yield_stats_from_ledger(self):
        self.ledger.append("incentive", {"subject": "loser_mod", "total": 0.8}, actor="scout")
        self.ledger.append(
            "complete", {"entity": "loser_mod", "ok": True, "result": {"revenue_msat": 1000}}, actor="worker"
        )
        signal = self.guard.inspect("loser_mod")
        self.assertAlmostEqual(signal.incentives, 0.8)
        self.assertEqual(signal.revenue_msat, 1000.0)
        self.assertEqual(signal.runs, 1)
        self.assertAlmostEqual(signal.yield_value, 1.8)

    def test_pre_graft_records_inspection(self):
        self.guard.pre_graft("w", "l")
        self.assertEqual(self.ledger.count("graft_inspect"), 2)

    def test_execute_merges_before_calling_graft(self):
        self.bus.deposit("loser_mod", "trail-a", strength=3.0, half_life_s=100000)
        calls = []
        out = self.guard.execute("winner_mod", "loser_mod", lambda: calls.append(True) or "grafted")
        self.assertEqual(calls, [True])
        self.assertTrue(out["decision"]["merged"])
        self.assertEqual(out["result"], "grafted")
        # Signal was carried onto the winner.
        self.assertGreater(self.bus.pheromone_by_emitter().get("winner_mod", 0.0), 0.0)
        self.assertEqual(self.ledger.count("pheromone_merge"), 1)
        self.assertEqual(self.ledger.count("graft_executed"), 1)

    def test_merge_moves_strength(self):
        self.bus.deposit("loser_mod", "t1", strength=2.0, half_life_s=100000)
        self.bus.deposit("loser_mod", "t2", strength=1.0, half_life_s=100000)
        moved = self.guard.merge_pheromones("loser_mod", "winner_mod")
        self.assertAlmostEqual(moved, 3.0, places=2)

    def test_adaptive_memory_is_folded_in(self):
        memory = self.root / "adaptive_memory.json"
        memory.write_text(
            json.dumps({"modules": {"modx": {"pheromone_density": 2.0, "incentives": 1.0,
                                             "revenue_msat": 500, "runs": 3}}}),
            encoding="utf-8",
        )
        guard = GraftGuard(bus=self.bus, ledger=self.ledger, adaptive_memory_path=memory)
        signal = guard.inspect("modx")
        self.assertEqual(signal.pheromone_density, 2.0)
        self.assertEqual(signal.incentives, 1.0)
        self.assertEqual(signal.revenue_msat, 500.0)
        self.assertEqual(signal.runs, 3)

    def test_execute_without_loser_signal_is_clean_graft(self):
        self.bus.deposit("winner_mod", "t", strength=4.0, half_life_s=100000)
        out = self.guard.execute("winner_mod", "loser_mod", lambda: "ok")
        self.assertFalse(out["decision"]["merged"])
        self.assertEqual(self.ledger.count("pheromone_merge"), 0)


if __name__ == "__main__":
    unittest.main()

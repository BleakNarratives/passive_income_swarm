"""The core_framework reconciliation boundary, pinned.

Two things are being tested, and the first one is the reason the file exists:

1. `GraftGuard` must see signal in the `adaptive_memory.json` that
   `core_framework/shared/adaptive_memory.py` actually writes. It used to read
   only `{"modules": ...}` and report zero for every module against the real
   file — a guard that silently allows the exact graft it exists to block.
2. `swarm/hooks.py` must report the five hooks honestly, including the two whose
   named targets never existed.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from swarm.cli import main
from swarm.graft import GraftGuard
from swarm.hooks import ACTION, HOOKS, NO_TARGET, WIRED, probe, summary, to_json
from swarm.ledger import Ledger
from swarm.whorl import WhorlBus


class TestCoreFrameworkMemoryShape(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.bus = WhorlBus(self.root / "bus.jsonl", ledger=self.ledger)
        self.memory = self.root / "adaptive_memory.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload):
        self.memory.write_text(json.dumps(payload), encoding="utf-8")

    def _guard(self):
        return GraftGuard(bus=self.bus, ledger=self.ledger, adaptive_memory_path=self.memory)

    def _real_shape(self):
        return {
            "version": 1,
            "half_life_seconds": 604800.0,
            "exploration_rate": 0.1,
            "records": {
                "sec::debt covenant": {
                    "data_source": "sec",
                    "query_pattern": "debt covenant",
                    "pheromone": 2.5,
                    "runs": 4,
                },
                "sec::leverage": {
                    "data_source": "sec",
                    "query_pattern": "leverage",
                    "pheromone": 4.0,
                    "runs": 6,
                },
                "news::distress": {
                    "data_source": "news",
                    "query_pattern": "distress",
                    "pheromone": 1.0,
                    "runs": 2,
                },
            },
        }

    def test_reads_the_shape_core_framework_actually_writes(self):
        self._write(self._real_shape())
        sec = self._guard().inspect("sec")
        self.assertEqual(sec.pheromone_density, 4.0)  # strongest trail, not the sum
        self.assertEqual(sec.runs, 10)  # summed across the source's queries
        self.assertEqual(sec.incentives, 0.0)  # the file carries no money data
        self.assertEqual(sec.revenue_msat, 0.0)

    def test_a_real_memory_file_can_still_demand_a_merge(self):
        """The anti-lobotomy guarantee survives the shape change."""
        self._write(self._real_shape())
        decision = self._guard().pre_graft("winner_mod", "sec")
        self.assertTrue(decision.loser_dominant)
        self.assertTrue(decision.merge_required)

    def test_absorb_moves_real_density_onto_the_winner(self):
        self._write(self._real_shape())
        result = self._guard().absorb_adaptive_memory("sec", "winner_mod")
        self.assertTrue(result["absorbed"])
        self.assertEqual(result["density"], 4.0)
        self.assertGreater(self.bus.pheromone_by_emitter().get("winner_mod", 0.0), 0.0)

    def test_documented_modules_shape_still_works(self):
        self._write(
            {
                "modules": {
                    "modx": {
                        "pheromone_density": 2.0,
                        "incentives": 1.0,
                        "revenue_msat": 500,
                        "runs": 3,
                    }
                }
            }
        )
        signal = self._guard().inspect("modx")
        self.assertEqual(signal.pheromone_density, 2.0)
        self.assertEqual(signal.incentives, 1.0)
        self.assertEqual(signal.revenue_msat, 500.0)
        self.assertEqual(signal.runs, 3)

    def test_records_without_data_source_fall_back_to_the_key_prefix(self):
        self._write(
            {
                "records": {
                    "elixir::trail": {"pheromone": 1.5, "runs": 2},
                }
            }
        )
        self.assertEqual(self._guard().inspect("elixir").runs, 2)

    def test_a_file_with_neither_shape_reports_nothing(self):
        self._write({"version": 1, "half_life_seconds": 10})
        signal = self._guard().inspect("anything")
        self.assertEqual(signal.pheromone_density, 0.0)
        self.assertEqual(signal.runs, 0)

    def test_corrupt_memory_is_ignored_not_fatal(self):
        self.memory.write_text("{not json", encoding="utf-8")
        self.assertEqual(self._guard().inspect("anything").runs, 0)


class TestHookProbe(unittest.TestCase):
    def test_the_checklist_still_has_five_hooks(self):
        self.assertEqual(len(HOOKS), 5)
        self.assertEqual(
            {h.id for h in HOOKS},
            {"graft_guard", "adaptive_memory", "domino_preflight", "scout_registry", "tests"},
        )

    def test_without_a_checkout_every_hook_is_honest(self):
        states = {r.hook: r.state for r in probe(None)}
        self.assertEqual(states["adaptive_memory"], NO_TARGET)
        self.assertEqual(states["domino_preflight"], NO_TARGET)
        self.assertEqual(states["tests"], ACTION)  # the suite is always actionable
        self.assertEqual(len(states), 5)

    def test_a_marker_in_the_checkout_reads_as_wired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "domino.sh").write_text(
                "preflight() {\n  python3 -c 'pass'\n  swarmctl breaker preflight\n}\n",
                encoding="utf-8",
            )
            states = {r.hook: r.state for r in probe(root)}
            self.assertEqual(states["domino_preflight"], WIRED)

    def test_an_existing_target_without_a_call_needs_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shared").mkdir()
            (root / "shared" / "adaptive_memory.py").write_text(
                "class AdaptiveQueryMemory:\n    pass\n", encoding="utf-8"
            )
            states = {r.hook: r.state for r in probe(root)}
            self.assertEqual(states["adaptive_memory"], ACTION)

    def test_prose_that_mentions_a_hook_is_not_a_wiring(self):
        """A document claiming the integration exists is the failure mode."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "NOTES.md").write_text(
                'The payload "records" are wired; run swarmctl breaker preflight first.\n',
                encoding="utf-8",
            )
            states = {r.hook: r.state for r in probe(root)}
            self.assertEqual(states["adaptive_memory"], NO_TARGET)
            self.assertEqual(states["domino_preflight"], NO_TARGET)

    def test_a_named_file_that_never_existed_is_not_a_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            states = {r.hook: r.state for r in probe(Path(tmp))}
            self.assertEqual(states["scout_registry"], NO_TARGET)
            self.assertEqual(states["graft_guard"], NO_TARGET)

    def test_summary_and_json_are_well_formed(self):
        reports = probe(None)
        counts = summary(reports)
        self.assertEqual(sum(counts.values()), 5)
        payload = json.loads(to_json(reports, None))
        self.assertEqual(len(payload["hooks"]), 5)
        self.assertEqual(payload["summary"], counts)
        for row in payload["hooks"]:
            self.assertIn(row["state"], {WIRED, ACTION, NO_TARGET})


class TestHooksCli(unittest.TestCase):
    def _run(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["hooks", *argv])
        return code, buffer.getvalue()

    def test_json_output_exits_zero(self):
        code, out = self._run("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["hooks"]), 5)

    def test_strict_exits_one_while_a_hook_needs_action(self):
        code, _ = self._run("--strict")
        self.assertEqual(code, 1)

    def test_human_output_names_the_missing_targets(self):
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("no target", out)
        self.assertIn("adaptive_memory", out)
        self.assertIn("swarmctl breaker preflight", out)


if __name__ == "__main__":
    unittest.main()

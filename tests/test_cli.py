import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from swarm.cli import main

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = str(ROOT / "swarm" / "cabinet.json")


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(["--root", self.root, "--manifest", MANIFEST, *args])
        return code, buf.getvalue()

    def test_cabinet_renders(self):
        code, out = self.run_cli("cabinet", "--stock")
        self.assertEqual(code, 0)
        self.assertIn("CABINET", out)
        self.assertIn("dvm_worker", out)

    def test_pull_and_tickets(self):
        code, out = self.run_cli("pull", "B3")
        self.assertEqual(code, 0)
        self.assertIn("thoth", out)
        code, out = self.run_cli("tickets")
        self.assertEqual(code, 0)
        self.assertIn("thoth", out)

    def test_pull_bad_payload_is_error(self):
        code, out = self.run_cli("pull", "B3", "--payload", "{nope}")
        self.assertEqual(code, 2)

    def test_pull_unknown_slot_is_error(self):
        code, out = self.run_cli("pull", "Z9")
        self.assertEqual(code, 1)

    def test_skills_seed_record_certify(self):
        code, _ = self.run_cli("skills", "seed")
        self.assertEqual(code, 0)
        code, out = self.run_cli("skills", "record", "forecast", "0.9")
        self.assertEqual(code, 0)
        self.assertIn("forecast", out)
        code, out = self.run_cli("skills", "leaderboard")
        self.assertEqual(code, 0)

    def test_forecast_inline_series(self):
        code, out = self.run_cli("forecast", "--series", "1,2,3,4,5", "--horizon", "2")
        self.assertEqual(code, 0)
        self.assertIn("predictions", out.lower().replace("next", "predictions"))

    def test_forecast_empty_metric_is_safe(self):
        code, out = self.run_cli("forecast", "revenue")
        self.assertEqual(code, 0)
        self.assertIn("nothing to forecast", out)

    def test_doctor_valid(self):
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("registry valid", out)

    def test_supervise(self):
        code, out = self.run_cli("supervise", "--ticks", "1")
        self.assertEqual(code, 0)
        self.assertIn("healthy", out)

    def test_scout_run(self):
        code, out = self.run_cli("scout", "run")
        self.assertEqual(code, 0)

    def test_demo_end_to_end(self):
        code, out = self.run_cli("demo")
        self.assertEqual(code, 0)
        self.assertIn("demo complete", out)

    def test_contract_emit_and_validate(self):
        code, out = self.run_cli(
            "contract", "emit", "--emitter", "scout", "--kind", "intel",
            "--payload", '{"text": "x"}', "--confidence", "0.9", "--lineage", "projects/agents/scouts/a",
        )
        self.assertEqual(code, 0)
        self.assertIn("content_sha256", out)
        code, out = self.run_cli("contract", "validate", "--json", '{"emitter": "x"}')
        self.assertEqual(code, 1)
        self.assertIn("rejected", out)

    def test_bus_commands(self):
        self.run_cli("contract", "emit", "--emitter", "scout", "--kind", "intel",
                     "--payload", '{"t": 1}', "--confidence", "0.9")
        code, out = self.run_cli("bus", "tail")
        self.assertEqual(code, 0)
        self.assertIn("intel", out)
        code, out = self.run_cli("bus", "high", "--min", "0.8")
        self.assertEqual(code, 0)
        self.assertIn("intel", out)

    def test_lineage_gate(self):
        code, _ = self.run_cli("lineage", "src/skill/a", "--self-modify")
        self.assertEqual(code, 0)
        code, out = self.run_cli("lineage", "._archive/x", "--self-modify")
        self.assertEqual(code, 1)
        self.assertIn("dead lineage", out)

    def test_incentive_score(self):
        code, out = self.run_cli("incentive", "--text", "rare niche quantum find",
                                 "--signals", '{"revenue": 0.9}')
        self.assertEqual(code, 0)
        self.assertIn("novelty", out)

    def test_relay_sync(self):
        code, out = self.run_cli("relay")
        self.assertEqual(code, 0)
        self.assertIn("SWARM_RELAY_SYNC", out)

    def test_ingest_loop_applies_gated_skill_update(self):
        self.run_cli("contract", "emit", "--emitter", "scout", "--kind", "skill_update",
                     "--payload", '{"skill_id": "ingested_skill", "name": "Ingested", "capabilities": ["llm.generate"]}',
                     "--confidence", "0.9", "--lineage", "projects/agents/scouts/alpha")
        code, out = self.run_cli("ingest")
        self.assertEqual(code, 0)
        self.assertIn('"accepted": 1', out)
        code, out = self.run_cli("skills", "list")
        self.assertIn("ingested_skill", out)


if __name__ == "__main__":
    unittest.main()

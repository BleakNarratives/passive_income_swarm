import hashlib
import tempfile
import unittest
from pathlib import Path

from swarm.discovery import FERAL, REGISTERED, ScoutRegistry


class TestScoutRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reg = ScoutRegistry(
            path=self.root / "SCOUT_REGISTRY.json",
            quarantine_dir=self.root / "quarantine",
        )
        self.script = self.root / "src" / "skill" / "scout_a.py"
        self.script.parent.mkdir(parents=True, exist_ok=True)
        self.script.write_text("print('hi')\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_on_clean_run_records_digest(self):
        entry = self.reg.record_clean_run(str(self.script))
        expected = hashlib.sha256(b"print('hi')\n").hexdigest()
        self.assertEqual(entry["content_sha256"], expected)
        self.assertEqual(entry["schema"], "swarm.payload/1")
        self.assertEqual(self.reg.classify(str(self.script)), REGISTERED)

    def test_clean_runs_increment(self):
        self.reg.record_clean_run(str(self.script))
        entry = self.reg.record_clean_run(str(self.script))
        self.assertEqual(entry["clean_runs"], 2)

    def test_unregistered_execution_is_feral_and_quarantined(self):
        gate = self.reg.gate_execution(str(self.script))
        self.assertEqual(gate["status"], FERAL)
        self.assertIsNotNone(gate["quarantined"])
        self.assertTrue(Path(gate["quarantined"]).exists())
        self.assertIn(FERAL, gate["quarantined"])

    def test_changed_bytes_are_not_trusted(self):
        self.reg.record_clean_run(str(self.script))
        self.script.write_text("print('tampered')\n", encoding="utf-8")
        new_digest = ScoutRegistry.digest_file(self.script)
        self.assertFalse(self.reg.is_registered(str(self.script), new_digest))

    def test_digest_match_check(self):
        entry = self.reg.record_clean_run(str(self.script))
        self.assertTrue(self.reg.is_registered(str(self.script), entry["content_sha256"]))
        self.assertFalse(self.reg.is_registered(str(self.script), "0" * 64))

    def test_persistence_across_reload(self):
        self.reg.record_clean_run(str(self.script))
        reloaded = ScoutRegistry(path=self.root / "SCOUT_REGISTRY.json",
                                 quarantine_dir=self.root / "quarantine")
        self.assertTrue(reloaded.is_registered(str(self.script)))

    def test_emitter_id_can_be_registered_directly(self):
        self.reg.register("augur", digest="abc123")
        self.assertTrue(self.reg.is_registered("augur"))
        self.assertEqual(self.reg.classify("augur"), REGISTERED)

    def test_allowed_roots(self):
        self.assertTrue(self.reg.in_allowed_roots("src/skill/x.py"))
        self.assertTrue(self.reg.in_allowed_roots("projects/agents/scouts/y.exs"))
        self.assertFalse(self.reg.in_allowed_roots("random/z.js"))

    def test_missing_file_still_quarantines(self):
        gate = self.reg.gate_execution(str(self.root / "nope" / "ghost.elixir"))
        self.assertEqual(gate["status"], FERAL)
        self.assertTrue(Path(gate["quarantined"]).exists())

    def test_summary(self):
        self.reg.record_clean_run(str(self.script))
        self.assertEqual(self.reg.summary()["registered"], 1)


if __name__ == "__main__":
    unittest.main()

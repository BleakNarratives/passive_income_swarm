import tempfile
import unittest
from pathlib import Path

from swarm.ledger import Ledger
from swarm.lineage import PURPOSE_SELF_MODIFY, PURPOSE_TELEMETRY, LineageGate


class TestLineageGate(unittest.TestCase):
    def setUp(self):
        self.gate = LineageGate()
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "logs" / "swarm.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowed_roots(self):
        for path in ("src/skill/foo.py", "projects/agents/scouts/alpha.md", "./src/skill/x"):
            self.assertEqual(self.gate.classify(path), "allow", path)
            self.assertTrue(self.gate.permit(path, PURPOSE_SELF_MODIFY))

    def test_archive_denied(self):
        for path in ("._archive/old.py", "src/skill/._archive/x", "deep/._archive/y"):
            self.assertEqual(self.gate.classify(path), "archive", path)
            self.assertFalse(self.gate.permit(path, PURPOSE_SELF_MODIFY))
            self.assertFalse(self.gate.permit(path, PURPOSE_TELEMETRY))

    def test_unpacked_denied(self):
        for path in ("UNPACKED/dump/x", "foo/UNPACKED/bar"):
            self.assertEqual(self.gate.classify(path), "unpacked", path)
            self.assertFalse(self.gate.permit(path, PURPOSE_SELF_MODIFY))

    def test_unknown_telemetry_allowed_self_modify_denied(self):
        path = "random/notes.md"
        self.assertEqual(self.gate.classify(path), "unknown")
        self.assertTrue(self.gate.permit(path, PURPOSE_TELEMETRY))
        self.assertFalse(self.gate.permit(path, PURPOSE_SELF_MODIFY))

    def test_empty_path_fails_closed(self):
        self.assertFalse(self.gate.permit("", PURPOSE_SELF_MODIFY))
        self.assertEqual(self.gate.classify(""), "unknown")

    def test_decision_has_reason(self):
        d = self.gate.decision("._archive/x", PURPOSE_SELF_MODIFY)
        self.assertIn("dead lineage", d.reason)
        self.assertFalse(d.permitted)

    def test_check_logs_to_ledger(self):
        self.gate.check("src/skill/a", PURPOSE_SELF_MODIFY, ledger=self.ledger)
        self.gate.check("._archive/b", PURPOSE_SELF_MODIFY, ledger=self.ledger)
        self.assertEqual(self.ledger.count("lineage_gate"), 2)

    def test_backslash_paths_normalized(self):
        self.assertTrue(self.gate.permit("src\\skill\\foo.py", PURPOSE_SELF_MODIFY))


if __name__ == "__main__":
    unittest.main()

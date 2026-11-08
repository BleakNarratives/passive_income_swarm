"""A guard nobody starts is decoration. These tests read the unit file."""

import tempfile
import unittest
from pathlib import Path

from swarm.unit_audit import ABSENT, GUARDED, UNGUARDED, audit, find_units, format_report, parse_unit

REPO = Path(__file__).resolve().parent.parent


class ParseTests(unittest.TestCase):
    def test_sections_and_comments(self):
        text = """
[Unit]
Description=thing
# ExecStart=/usr/bin/ignored

[Service]
ExecStart=/opt/run_paid_worker.sh
EnvironmentFile=/etc/dvm.env
"""
        parsed = parse_unit(text)
        self.assertEqual(parsed["Unit"]["Description"], "thing")
        self.assertEqual(parsed["Service"]["ExecStart"], "/opt/run_paid_worker.sh")
        self.assertNotIn("ignored", str(parsed))

    def test_repeated_keys_do_not_hide_exec_start(self):
        parsed = parse_unit(
            "[Service]\nExecStartPre=/bin/true\nExecStart=/opt/run_paid_worker.sh\n"
        )
        self.assertEqual(parsed["Service"]["ExecStart"], "/opt/run_paid_worker.sh")
        self.assertEqual(parsed["Service"]["ExecStartPre"], "/bin/true")


class AuditTests(unittest.TestCase):
    def _unit(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "dvm-worker.service"
        path.write_text(body, encoding="utf-8")
        return path

    def test_missing_file_is_absent_not_guarded(self):
        result = audit("/nonexistent/definitely-not-here.service")
        self.assertEqual(result["verdict"], ABSENT)
        self.assertFalse(result["exists"])

    def test_wrapper_is_guarded(self):
        path = self._unit(
            "[Service]\nExecStart=/home/x/passive_income_swarm/nostr_dvm/run_paid_worker.sh\n"
            "EnvironmentFile=/home/x/passive_income_swarm/nostr_dvm/dvm.env\n"
        )
        result = audit(path)
        self.assertEqual(result["verdict"], GUARDED)
        self.assertTrue(result["uses_wrapper"])
        self.assertFalse(result["bypasses_wrapper"])

    def test_direct_worker_is_unguarded(self):
        path = self._unit("[Service]\nExecStart=/usr/bin/python3 dvm_worker.py\n")
        result = audit(path)
        self.assertEqual(result["verdict"], UNGUARDED)
        self.assertTrue(result["bypasses_wrapper"])
        self.assertIn("guard is not in this unit", format_report(result))

    def test_a_comment_mentioning_the_wrapper_does_not_count(self):
        path = self._unit(
            "# ExecStart=run_paid_worker.sh\n[Service]\nExecStart=/usr/bin/python3 paid_mode_guard.py\n"
        )
        self.assertEqual(audit(path)["verdict"], UNGUARDED)

    def test_wrapper_mentioned_only_in_environment_is_not_guarded(self):
        path = self._unit(
            '[Service]\nEnvironment="WRAPPER=run_paid_worker.sh"\nExecStart=/usr/bin/python3 paid_mode_guard.py\n'
        )
        self.assertEqual(audit(path)["verdict"], UNGUARDED)

    def test_missing_exec_start_is_absent(self):
        path = self._unit("[Service]\nRestart=always\n")
        self.assertEqual(audit(path)["verdict"], ABSENT)

    def test_shipped_unit_is_guarded(self):
        """The unit we tell people to install must pass our own audit."""
        result = audit(REPO / "deploy" / "dvm-worker.service")
        self.assertEqual(result["verdict"], GUARDED, result)
        self.assertIn("run_paid_worker.sh", result["exec_start"])
        self.assertTrue(result["environment_file"], "the shipped unit must name an EnvironmentFile")
        self.assertIn("dvm.env", result["environment_file"])

    def test_find_units_tolerates_a_missing_directory(self):
        self.assertEqual(find_units(Path("/nonexistent")), [])


if __name__ == "__main__":
    unittest.main()

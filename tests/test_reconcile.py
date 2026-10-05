import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from swarm.cli import main
from swarm.lineage import ALLOWED_ROOTS, DENIED_ROOTS, declared_roots, reconcile_with_proofs
from swarm.whorl import WhorlBus, reconcile_bus_path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = str(ROOT / "swarm" / "cabinet.json")


class TestLineageReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_absent_is_not_drift(self):
        report = reconcile_with_proofs(self.root / "missing.md")
        self.assertEqual(report["status"], "source_absent")
        self.assertFalse(report["present"])
        self.assertEqual(report["drift"], [])
        self.assertEqual(report["active_allowed"], list(ALLOWED_ROOTS))

    def test_ok_when_doc_matches(self):
        doc = self.root / "PROOFS_AND_GATES.md"
        doc.write_text(
            "Live lineage: src/skill/ and projects/agents/scouts/.\n"
            "Never ingest from ._archive/ or UNPACKED/.\n",
            encoding="utf-8",
        )
        report = reconcile_with_proofs(doc)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["drift"], [])

    def test_drift_when_doc_omits_a_root(self):
        doc = self.root / "PROOFS_AND_GATES.md"
        doc.write_text("Only src/skill/ and ._archive/.\n", encoding="utf-8")
        report = reconcile_with_proofs(doc)
        self.assertEqual(report["status"], "drift")
        issues = {(d["root"], d["issue"]) for d in report["drift"]}
        self.assertIn(("projects/agents/scouts/", "active but not in PROOFS"), issues)
        self.assertIn(("UNPACKED", "active but not in PROOFS"), issues)

    def test_drift_when_active_root_overridden(self):
        doc = self.root / "PROOFS_AND_GATES.md"
        doc.write_text("src/skill/ projects/agents/scouts/ ._archive/ UNPACKED/\n", encoding="utf-8")
        report = reconcile_with_proofs(doc, allowed=("src/skill/", "extra/root/"))
        self.assertEqual(report["status"], "drift")
        self.assertTrue(any(d["root"] == "extra/root/" for d in report["drift"]))

    def test_declared_roots_extraction(self):
        found = declared_roots("see src/skill/foo and UNPACKED/dump")
        self.assertIn("src/skill/", found["allowed"])
        self.assertIn("UNPACKED", found["denied"])
        self.assertNotIn("projects/agents/scouts/", found["allowed"])


class TestWhorlReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_expected_path_is_ok(self):
        report = reconcile_bus_path(self.root / "bus.jsonl")
        self.assertEqual(report["status"], "ok")
        self.assertIsNone(report["expected"])

    def test_matching_expected_path(self):
        report = reconcile_bus_path("/srv/bus.jsonl", "/srv/bus.jsonl")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["drift"], [])

    def test_mismatched_path_is_drift(self):
        report = reconcile_bus_path("bus.jsonl", "/srv/core/bus.jsonl")
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["drift"][0]["issue"], "bus path differs")

    def test_janus_capabilities_present(self):
        report = reconcile_bus_path("bus.jsonl")
        self.assertTrue(all(report["capabilities"].values()))

    def test_describe_shape(self):
        desc = WhorlBus(self.root / "bus.jsonl").describe()
        self.assertIn("path", desc)
        self.assertTrue(desc["capabilities"]["flush"])


class TestCliReconcile(unittest.TestCase):
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

    def test_reconcile_source_absent_exit_0(self):
        code, out = self.run_cli("reconcile")
        self.assertEqual(code, 0)
        self.assertIn("source_absent", out)

    def test_reconcile_drift_exit_1(self):
        code, out = self.run_cli("reconcile", "--expected-bus-path", "/srv/core/bus.jsonl")
        self.assertEqual(code, 1)
        self.assertIn("drift", out)

    def test_reconcile_with_matching_proofs(self):
        doc = Path(self.root) / "PROOFS_AND_GATES.md"
        doc.write_text("src/skill/ projects/agents/scouts/ ._archive/ UNPACKED/\n", encoding="utf-8")
        code, out = self.run_cli("reconcile", "--proofs", str(doc))
        self.assertEqual(code, 0)
        self.assertIn('"status": "ok"', out)

    def test_global_root_override_is_honored(self):
        doc = Path(self.root) / "PROOFS_AND_GATES.md"
        doc.write_text("src/skill/ projects/agents/scouts/ ._archive/ UNPACKED/\n", encoding="utf-8")
        # --allowed-root is global, so it must precede the subcommand. Narrowing
        # the active roots means the doc's other root is now unattended -> drift.
        code, out = self.run_cli("--allowed-root", "src/skill/", "reconcile", "--proofs", str(doc))
        self.assertEqual(code, 1)
        self.assertIn("projects/agents/scouts/", out)
        self.assertIn("in PROOFS but not active", out)


if __name__ == "__main__":
    unittest.main()

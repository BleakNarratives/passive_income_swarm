import calendar
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from swarm.entities import Entity
from swarm.ledger import Ledger
from swarm.ps import format_report, report, resolve_entity


def make_entity(**overrides):
    values = {"id": "thing", "kind": "daemon", "display": "Thing"}
    values.update(overrides)
    return Entity(**values)


def fake_which(present=("python3", "bash", "sh")):
    return lambda name: f"/usr/bin/{name}" if name in present else None


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "run.sh").write_text("#!/bin/sh\n")
        (self.root / "bin").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_persona_is_not_runnable(self):
        available, reason = resolve_entity(
            make_entity(kind="persona", entrypoint=""), self.root, which=fake_which()
        )
        self.assertIsNone(available)
        self.assertIn("persona", reason)

    def test_missing_entrypoint_is_unavailable(self):
        available, reason = resolve_entity(make_entity(), self.root, which=fake_which())
        self.assertFalse(available)
        self.assertIn("no entrypoint", reason)

    def test_existing_script_with_runtime_is_available(self):
        available, reason = resolve_entity(
            make_entity(runtime="bash", entrypoint="scripts/run.sh"),
            self.root,
            which=fake_which(),
        )
        self.assertTrue(available)
        self.assertIn("bash", reason)

    def test_missing_runtime_is_unavailable(self):
        available, reason = resolve_entity(
            make_entity(runtime="elixir", entrypoint="scripts/run.sh"),
            self.root,
            which=fake_which(),
        )
        self.assertFalse(available)
        self.assertIn("runtime missing: elixir", reason)

    def test_missing_file_is_unavailable(self):
        available, reason = resolve_entity(
            make_entity(runtime="bash", entrypoint="scripts/gone.sh"),
            self.root,
            which=fake_which(),
        )
        self.assertFalse(available)
        self.assertIn("missing", reason)

    def test_module_entrypoint_is_resolved(self):
        available, reason = resolve_entity(
            make_entity(runtime="python", entrypoint="-m swarm.cli doctor"),
            self.root,
            which=fake_which(),
        )
        self.assertTrue(available)
        self.assertIn("swarm.cli", reason)

    def test_unknown_module_is_unavailable(self):
        available, reason = resolve_entity(
            make_entity(runtime="python", entrypoint="-m swarm.not_a_module"),
            self.root,
            which=fake_which(),
        )
        self.assertFalse(available)
        self.assertIn("module not found", reason)

    def test_compiled_artifact_must_be_executable(self):
        artifact = self.root / "bin" / "forge"
        artifact.write_text("binary")
        available, reason = resolve_entity(
            make_entity(runtime="cpp", entrypoint="bin/forge"), self.root, which=fake_which()
        )
        self.assertFalse(available)
        self.assertIn("not executable", reason)

        os.chmod(artifact, 0o755)
        available, _ = resolve_entity(
            make_entity(runtime="cpp", entrypoint="bin/forge"), self.root, which=fake_which()
        )
        self.assertTrue(available)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "run.sh").write_text("#!/bin/sh\n")
        self.ledger_path = self.root / "logs" / "swarm.jsonl"
        self.ledger_path.parent.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_events(self, events):
        with self.ledger_path.open("w", encoding="utf-8") as handle:
            for index, event in enumerate(events, start=1):
                handle.write(json.dumps({"seq": index, **event}) + "\n")

    def test_heartbeat_makes_an_entity_active_and_observed(self):
        now = calendar.timegm(time.strptime("2026-11-08T22:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        self._write_events(
            [
                {
                    "ts": "2026-11-08T21:59:30Z",
                    "type": "heartbeat",
                    "actor": "runner",
                    "data": {},
                }
            ]
        )
        registry = [make_entity(id="runner", runtime="bash", entrypoint="scripts/run.sh")]
        payload = report(
            self.root,
            registry,
            Ledger(self.ledger_path),
            processes=[],
            now=now,
            which=fake_which(),
        )
        row = payload["entities"][0]
        self.assertTrue(row["active"])
        self.assertIn("heartbeat", row["active_reason"])
        self.assertTrue(row["observed_recent"])
        self.assertEqual(payload["process_scan"], "injected")

    def test_old_heartbeat_is_observed_but_not_active(self):
        now = calendar.timegm(time.strptime("2026-11-08T22:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        self._write_events(
            [{"ts": "2026-11-08T20:00:00Z", "type": "heartbeat", "actor": "old", "data": {}}]
        )
        registry = [make_entity(id="old", runtime="bash", entrypoint="scripts/run.sh")]
        payload = report(
            self.root,
            registry,
            Ledger(self.ledger_path),
            processes=[],
            now=now,
            which=fake_which(),
        )
        row = payload["entities"][0]
        self.assertFalse(row["active"])
        self.assertTrue(row["observed_recent"])
        self.assertEqual(row["observed_age_s"], 7200)

    def test_process_table_makes_an_entity_active(self):
        ledger = Ledger(self.ledger_path)
        registry = [make_entity(id="runner", runtime="bash", entrypoint="scripts/run.sh")]
        payload = report(
            self.root,
            registry,
            ledger,
            processes=[(4242, "bash scripts/run.sh --loop")],
            now=time.time(),
            which=fake_which(),
        )
        row = payload["entities"][0]
        self.assertTrue(row["active"])
        self.assertEqual(row["active_reason"], "pid 4242")

    def test_similar_filename_is_not_a_process_match(self):
        (self.root / "scripts" / "api.py").write_text("# api\n")
        registry = [make_entity(id="api", runtime="python", entrypoint="scripts/api.py")]
        payload = report(
            self.root,
            registry,
            Ledger(self.ledger_path),
            processes=[(999, "python my_api.py")],
            now=time.time(),
            which=fake_which(),
        )
        self.assertFalse(payload["entities"][0]["active"])

    def test_entity_named_in_data_is_observed(self):
        now = calendar.timegm(time.strptime("2026-11-08T22:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        self._write_events(
            [
                {
                    "ts": "2026-11-08T21:00:00Z",
                    "type": "health",
                    "actor": "supervisor",
                    "data": {"entity": "watched"},
                }
            ]
        )
        registry = [make_entity(id="watched", runtime="bash", entrypoint="scripts/run.sh")]
        payload = report(
            self.root,
            registry,
            Ledger(self.ledger_path),
            processes=[],
            now=now,
            which=fake_which(),
        )
        self.assertIsNotNone(payload["entities"][0]["observed_at"])

    def test_summary_separates_persona_missing_and_available(self):
        (self.root / "scripts" / "ok.sh").write_text("#!/bin/sh\n")
        registry = [
            make_entity(id="ok", runtime="bash", entrypoint="scripts/ok.sh"),
            make_entity(id="gone", runtime="bash", entrypoint="scripts/gone.sh"),
            make_entity(id="child", kind="subagent"),
            make_entity(id="voice", kind="persona"),
        ]
        payload = report(
            self.root,
            registry,
            Ledger(self.ledger_path),
            processes=[],
            now=time.time(),
            which=fake_which(),
        )
        summary = payload["summary"]
        self.assertEqual(summary["declared"], 4)
        self.assertEqual(summary["available"], 1)
        self.assertEqual(summary["unavailable"], 2)
        self.assertEqual(summary["not_runnable"], 1)
        self.assertEqual(summary["active"], 0)
        self.assertEqual(summary["observed"], 0)

    def test_report_is_plain_and_says_never(self):
        registry = [make_entity(id="quiet", runtime="bash", entrypoint="scripts/run.sh")]
        payload = report(
            self.root,
            registry,
            Ledger(self.ledger_path),
            processes=[],
            now=time.time(),
            which=fake_which(),
        )
        text = format_report(payload)
        self.assertIn("ENTITY", text)
        self.assertIn("quiet", text)
        self.assertIn("never", text)
        self.assertIn("1 declared", text)
        self.assertNotIn("\033", text)


if __name__ == "__main__":
    unittest.main()

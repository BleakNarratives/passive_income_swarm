import json
import tempfile
import unittest
from pathlib import Path

from swarm.capabilities import (
    SCHEMA,
    CapabilityRegistry,
    format_report,
    machine_id,
    state_from,
)


class StateFromTests(unittest.TestCase):
    def test_unserved_capability_is_blocked(self):
        self.assertEqual(state_from([], {})[0], "blocked")

    def test_served_but_unprobed_is_available(self):
        self.assertEqual(state_from(["c1"], {})[0], "available")

    def test_live_probe_is_tested(self):
        state, detail = state_from(["c1"], {"c1": {"status": "live", "detail": "ok 12ms"}})
        self.assertEqual(state, "tested")
        self.assertEqual(detail, "ok 12ms")

    def test_degraded_probe_is_tested_with_reason(self):
        state, detail = state_from(["c1"], {"c1": {"status": "degraded"}})
        self.assertEqual(state, "tested")
        self.assertIn("degraded", detail)

    def test_unconfigured_and_unreachable_are_blocked(self):
        self.assertEqual(
            state_from(["c1"], {"c1": {"status": "unconfigured", "detail": "missing KEY"}})[0],
            "blocked",
        )
        self.assertEqual(
            state_from(["c1"], {"c1": {"status": "unreachable"}})[0],
            "blocked",
        )


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = CapabilityRegistry(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_verified_requires_a_successful_command(self):
        with self.assertRaises(ValueError):
            self.registry.record("llm.generate", "verified")
        with self.assertRaises(ValueError):
            self.registry.record("llm.generate", "verified", exit_code=1)

    def test_verified_with_exit_zero_is_accepted(self):
        entry = self.registry.record("llm.generate", "verified", exit_code=0, output="ok")
        self.assertEqual(entry["state"], "verified")
        self.assertEqual(len(entry["evidence"]), 1)
        self.assertTrue(entry["current"])

    def test_state_only_ever_rises(self):
        self.registry.record("pay.lnurl", "verified", exit_code=0)
        self.registry.record("pay.lnurl", "blocked", detail="key removed")
        self.assertEqual(self.registry.get("pay.lnurl")["state"], "verified")
        self.assertEqual(
            self.registry.get("pay.lnurl")["detail"], "latest blocked: key removed"
        )

    def test_evidence_is_capped(self):
        for index in range(30):
            self.registry.record("net.http", "available", detail=str(index))
        self.assertEqual(len(self.registry.get("net.http")["evidence"]), 20)

    def test_evidence_records_machine_and_commit(self):
        entry = self.registry.record("fs.read", "available")
        evidence = entry["evidence"][-1]
        self.assertEqual(evidence["machine"], machine_id())
        self.assertIn("commit", evidence)

    def test_another_commit_is_historical(self):
        entry = self.registry.record("fs.write", "tested", commit="deadbee")
        self.assertFalse(entry["current"])

    def test_run_records_success_and_failure(self):
        ok = self.registry.run("shell.exec", "printf hello")
        self.assertEqual(ok["state"], "verified")
        self.assertEqual(ok["exit_code"], 0)
        self.assertEqual(len(ok["output_sha256"]), 64)

        bad = self.registry.run("net.http", "printf nope >&2; exit 3")
        self.assertEqual(bad["state"], "blocked")
        self.assertEqual(bad["exit_code"], 3)
        self.assertNotEqual(bad["output_sha256"], ok["output_sha256"])

    def test_run_failure_does_not_erase_verified(self):
        self.registry.run("shell.exec", "exit 0")
        self.registry.run("shell.exec", "exit 9")
        self.assertEqual(self.registry.get("shell.exec")["state"], "verified")

    def test_import_accepts_a_list_and_never_downgrades(self):
        self.registry.record("llm.generate", "verified", exit_code=0)
        result = self.registry.import_entries(
            [{"id": "llm.generate", "state": "blocked"}, {"id": "repo.gh", "state": "tested"}],
            source="import:chromebook",
        )
        self.assertEqual(result["imported"], 2)
        self.assertEqual(self.registry.get("llm.generate")["state"], "verified")
        self.assertEqual(self.registry.get("repo.gh")["state"], "tested")
        self.assertEqual(
            self.registry.get("repo.gh")["evidence"][-1]["source"], "import:chromebook"
        )

    def test_import_accepts_a_capabilities_mapping(self):
        self.registry.import_entries(
            {"capabilities": {"market.search": {"state": "available", "detail": "unverified"}}}
        )
        self.assertEqual(self.registry.get("market.search")["state"], "available")

    def test_import_unknown_state_becomes_unknown(self):
        self.registry.import_entries([{"id": "x.y", "state": "probably"}])
        self.assertEqual(self.registry.get("x.y")["state"], "unknown")

    def test_save_and_load_round_trip(self):
        self.registry.record("data.ledger", "available", detail="on disk")
        path = self.registry.save()
        raw = json.loads(path.read_text())
        self.assertEqual(raw["schema"], SCHEMA)
        loaded = CapabilityRegistry.load(self.root)
        self.assertEqual(loaded.get("data.ledger")["detail"], "on disk")

    def test_sync_uses_the_audit_and_the_cached_probe(self):
        class FakeRegistry:
            capabilities = {"a.b": {}, "c.d": {}}

            def __iter__(self):
                return iter(())

        class FakeConnections:
            def audit(self, registry):
                return {
                    "coverage": {
                        "a.b": {"connections": ["c1"], "declared_by": ["agent_a"]},
                        "c.d": {"connections": [], "declared_by": []},
                    }
                }

        result = self.registry.sync(
            registry=FakeRegistry(),
            connections=FakeConnections(),
            cached={"checked_at": "2026-01-01T00:00:00Z", "connections": [{"id": "c1", "status": "live", "detail": "ok"}]},
        )
        self.assertEqual(result["synced"], 2)
        self.assertEqual(self.registry.get("a.b")["state"], "tested")
        self.assertEqual(self.registry.get("a.b")["declared_by"], ["agent_a"])
        self.assertEqual(self.registry.get("c.d")["state"], "blocked")
        self.assertTrue(Path(result["path"]).exists())

    def test_report_is_plain_and_names_the_state(self):
        self.registry.record("llm.generate", "tested", detail="probe ok")
        text = format_report(self.registry)
        self.assertIn("llm.generate", text)
        self.assertIn("tested", text)
        self.assertIn("probe ok", text)
        self.assertNotIn("\033", text)


if __name__ == "__main__":
    unittest.main()

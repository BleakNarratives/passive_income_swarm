"""Tests for the capability connection layer.

No network: every probe is injected, and the one test that touches the shipped
cabinet only runs its offline audit.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from swarm.connections import (
    STATUS_DEGRADED,
    STATUS_LIVE,
    STATUS_UNBOUND,
    STATUS_UNCONFIGURED,
    STATUS_UNREACHABLE,
    STATUS_UNVERIFIED,
    Connection,
    ConnectionRegistry,
    Probe,
    build_connections,
    format_report,
    local_embedding,
)
from swarm.entities import Entity
from swarm.registry import Registry

ROOT = Path(__file__).resolve().parent.parent


def probe_ok(detail="ok", meta=None):
    def probe(timeout=8.0):
        return Probe(True, detail, 1.5, dict(meta or {}))
    return probe


def probe_fail(detail="refused"):
    def probe(timeout=8.0):
        return Probe(False, detail, 2.0)
    return probe


def probe_raises(exc):
    def probe(timeout=8.0):
        raise exc
    return probe


def exploded(timeout=8.0):
    raise AssertionError("probe must not be called")


def make_connection(conn_id="c1", caps=("market.search",), **kwargs):
    kwargs.setdefault("probe", probe_ok())
    return Connection(
        id=conn_id,
        label=kwargs.pop("label", conn_id),
        provider=kwargs.pop("provider", "test"),
        capabilities=tuple(caps),
        **kwargs,
    )


def registry_with(capabilities, entities=()):
    return Registry(
        entities=[Entity(id=eid, kind="scout", display=eid, capabilities=list(caps))
                  for eid, caps in entities],
        capabilities=capabilities,
    )


class AuditTests(unittest.TestCase):
    def test_capability_with_no_connection_is_an_orphan(self):
        reg = ConnectionRegistry()
        audit = reg.audit(registry_with({"market.search": {}}, [("lead_scout", ["market.search"])]))
        self.assertEqual(len(audit["orphans"]), 1)
        self.assertEqual(audit["orphans"][0]["capability"], "market.search")
        self.assertIn("lead_scout", audit["orphans"][0]["declared_by"])
        self.assertEqual(audit["coverage"]["market.search"]["status"], STATUS_UNBOUND)

    def test_binding_removes_the_orphan(self):
        reg = ConnectionRegistry([make_connection("gh", ("market.search",))])
        audit = reg.audit(registry_with({"market.search": {}}, [("lead_scout", ["market.search"])]))
        self.assertEqual(audit["orphans"], [])
        self.assertEqual(audit["coverage"]["market.search"]["connections"], ["gh"])

    def test_catalog_capability_needs_no_owner_to_be_reported(self):
        reg = ConnectionRegistry()
        audit = reg.audit(registry_with({"repo.gh": {}}, []))
        self.assertEqual(audit["orphans"][0]["declared_by"], [])
        self.assertEqual(audit["orphans"][0]["capability"], "repo.gh")

    def test_audit_calls_no_probe(self):
        reg = ConnectionRegistry([make_connection("c1", ("net.http",), probe=exploded)])
        # Would raise AssertionError if audit probed.
        audit = reg.audit(registry_with({"net.http": {}}))
        self.assertEqual(audit["orphans"], [])

    def test_status_is_unverified_before_probing(self):
        reg = ConnectionRegistry([make_connection("c1", ("net.http",), probe=exploded)])
        report = reg.report(registry_with({"net.http": {}}))
        self.assertEqual(report["connections"][0]["status"], STATUS_UNVERIFIED)
        self.assertFalse(report["probed"])


class CheckTests(unittest.TestCase):
    def test_live(self):
        conn = make_connection(probe=probe_ok())
        self.assertEqual(conn.check()["status"], STATUS_LIVE)

    def test_degraded_metadata(self):
        conn = make_connection(probe=probe_ok(meta={"degraded": True}))
        self.assertEqual(conn.check()["status"], STATUS_DEGRADED)

    def test_unreachable(self):
        conn = make_connection(probe=probe_fail())
        self.assertEqual(conn.check()["status"], STATUS_UNREACHABLE)

    def test_missing_env_is_unconfigured_and_never_probes(self):
        conn = make_connection(required_env=("SOME_TOKEN",), probe=exploded)
        out = conn.check(env={})
        self.assertEqual(out["status"], STATUS_UNCONFIGURED)
        self.assertEqual(out["missing_env"], ["SOME_TOKEN"])

    def test_present_env_probes(self):
        conn = make_connection(required_env=("SOME_TOKEN",), probe=probe_ok())
        out = conn.check(env={"SOME_TOKEN": "x"})
        self.assertEqual(out["status"], STATUS_LIVE)
        self.assertEqual(out["missing_env"], [])

    def test_probe_exception_becomes_unreachable(self):
        conn = make_connection(probe=probe_raises(RuntimeError("dns exploded")))
        out = conn.check()
        self.assertEqual(out["status"], STATUS_UNREACHABLE)
        self.assertIn("RuntimeError", out["detail"])

    def test_secret_value_never_appears_in_a_report(self):
        secret = "tok_live_do_not_print_9f3a"
        conn = make_connection(required_env=("SOME_TOKEN",), probe=probe_ok())
        reg = ConnectionRegistry([conn])
        report = reg.report(
            registry_with({"market.search": {}}),
            probe=True,
            env={"SOME_TOKEN": secret},
        )
        blob = json.dumps(report)
        self.assertNotIn(secret, blob)
        self.assertIn("SOME_TOKEN", blob)


class AggregationTests(unittest.TestCase):
    def test_live_connection_wins_over_unconfigured(self):
        reg = ConnectionRegistry(
            [
                make_connection("paid_search", ("market.search",),
                                required_env=("PAID_TOKEN",), probe=exploded),
                make_connection("free_search", ("market.search",), probe=probe_ok()),
            ]
        )
        report = reg.report(registry_with({"market.search": {}}), probe=True, env={})
        self.assertEqual(report["coverage"]["market.search"]["status"], STATUS_LIVE)

    def test_all_unconfigured_rolls_up_to_unconfigured(self):
        reg = ConnectionRegistry(
            [
                make_connection("a", ("market.search",), required_env=("A_TOKEN",), probe=exploded),
                make_connection("b", ("market.search",), required_env=("B_TOKEN",), probe=exploded),
            ]
        )
        report = reg.report(registry_with({"market.search": {}}), probe=True, env={})
        self.assertEqual(report["coverage"]["market.search"]["status"], STATUS_UNCONFIGURED)

    def test_all_failing_rolls_up_to_unreachable(self):
        reg = ConnectionRegistry([make_connection("a", ("market.search",), probe=probe_fail())])
        report = reg.report(registry_with({"market.search": {}}), probe=True)
        self.assertEqual(report["coverage"]["market.search"]["status"], STATUS_UNREACHABLE)

    def test_orphan_still_reported_in_a_full_report(self):
        reg = ConnectionRegistry([make_connection("a", ("net.http",))])
        report = reg.report(registry_with({"net.http": {}, "shell.exec": {}}), probe=True)
        caps = [o["capability"] for o in report["orphans"]]
        self.assertEqual(caps, ["shell.exec"])
        self.assertEqual(report["summary"]["unbound"], 1)


class PersistenceTests(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        reg = ConnectionRegistry([make_connection("a", ("net.http",), probe=probe_ok())])
        report = reg.report(registry_with({"net.http": {}}), probe=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "connections.json"
            written = ConnectionRegistry.save(report, path)
            self.assertTrue(written.exists())
            loaded = ConnectionRegistry.load(path)
        self.assertEqual(loaded["connections"][0]["status"], STATUS_LIVE)

    def test_load_of_missing_or_broken_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ConnectionRegistry.load(Path(tmp) / "nope.json"))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json")
            self.assertIsNone(ConnectionRegistry.load(bad))


class EmbeddingTests(unittest.TestCase):
    def test_deterministic_and_normalised(self):
        a = local_embedding("paid nostr jobs over lightning")
        b = local_embedding("paid nostr jobs over lightning")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in a)), 1.0, places=4)

    def test_different_text_differs(self):
        self.assertNotEqual(local_embedding("alpha beta"), local_embedding("gamma delta"))

    def test_empty_text_is_a_zero_vector(self):
        self.assertEqual(sum(abs(v) for v in local_embedding("")), 0.0)


class ShippedCabinetTests(unittest.TestCase):
    """The real manifest must have no dead capabilities."""

    def test_every_declared_capability_has_a_connection(self):
        registry = Registry.load(ROOT / "swarm" / "cabinet.json")
        connections = build_connections(ROOT)
        audit = connections.audit(registry)
        self.assertEqual(
            audit["orphans"], [],
            f"declared but unserved: {[o['capability'] for o in audit['orphans']]}",
        )
        self.assertTrue(audit["coverage"], "cabinet declared no capabilities at all")

    def test_shipped_set_covers_the_cabinet_catalog(self):
        registry = Registry.load(ROOT / "swarm" / "cabinet.json")
        connections = build_connections(ROOT)
        for capability in registry.capabilities:
            with self.subTest(capability=capability):
                self.assertTrue(
                    connections.for_capability(capability),
                    f"{capability} has no connection",
                )

    def test_offline_report_is_well_formed(self):
        registry = Registry.load(ROOT / "swarm" / "cabinet.json")
        report = build_connections(ROOT).report(registry, probe=False)
        self.assertFalse(report["probed"])
        self.assertGreaterEqual(report["summary"]["connections"], 10)
        text = format_report(report)
        self.assertIn("connections", text)
        self.assertNotIn("nwc.secret", text)
        # No credential value should ever appear in a rendered report.
        self.assertNotIn("nostr+walletconnect", text)


if __name__ == "__main__":
    unittest.main()

"""The console's facts must reach the bus exactly once per change."""

import json
import tempfile
import unittest
from pathlib import Path

from swarm.bridge import EMITTER, SIGNAL_IDS, collect, export, publish, signals
from swarm.contracts import Envelope


def _settle(root, job_id="job-a", fee=20_000, serve=False):
    from nostr_dvm.settlement import SettlementLedger

    path = root / "nostr_dvm" / "logs" / "settlements.db"
    ledger = SettlementLedger(path)
    ledger.bind(
        job_id=job_id,
        requester="r",
        kind=5002,
        fee_msat=fee,
        bolt11="",
        verify_url=f"https://wallet.test/verify/{job_id}",
    )
    ledger.mark_settled(job_id, "p")
    if serve:
        ledger.claim(job_id)
        ledger.mark_served(job_id, "answer")
    ledger.close()


class BridgeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "nostr_dvm" / "logs").mkdir(parents=True)
        (self.root / "logs").mkdir()
        self.bus = self.root / "bus.jsonl"
        self.state = self.root / "logs" / "bridge-state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _publish(self, **kwargs):
        kwargs.setdefault("bus_path", self.bus)
        kwargs.setdefault("state_path", self.state)
        return publish(self.root, **kwargs)

    def _records(self):
        if not self.bus.exists():
            return []
        return [
            json.loads(line)
            for line in self.bus.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class SignalShapeTests(BridgeBase):
    def test_collect_returns_snapshot_and_readiness(self):
        state = collect(self.root)
        self.assertIn("snapshot", state)
        self.assertIn("readiness", state)
        self.assertIn("observed_at", state)

    def test_expected_signal_ids_are_present(self):
        ids = {s.id for s in signals(collect(self.root))}
        self.assertTrue(ids)
        self.assertTrue(ids.issubset(set(SIGNAL_IDS)), ids - set(SIGNAL_IDS))

    def test_payloads_carry_no_timestamps(self):
        # Volatile fields would make every digest differ and defeat dedupe.
        for sig in signals(collect(self.root)):
            self.assertNotIn("observed_at", sig.payload)
            self.assertNotIn("generated_at", sig.payload)

    def test_money_facts_are_reported(self):
        _settle(self.root, fee=42_000, serve=True)
        by_id = {s.id: s for s in signals(collect(self.root))}
        self.assertEqual(by_id["money.settled"].payload["earned_msat"], 42_000)
        self.assertEqual(by_id["money.settled"].payload["served"], 1)

    def test_a_paid_unserved_job_is_high_confidence_and_deposits_a_pheromone(self):
        _settle(self.root, serve=False)
        by_id = {s.id: s for s in signals(collect(self.root))}
        self.assertEqual(by_id["money.owed"].payload["unserved_paid"], 1)
        self.assertEqual(by_id["money.owed"].confidence, 1.0)
        self.assertIn("action_required", by_id["money.owed"].tags)
        self.assertIn("pheromone.money_owed", by_id)
        self.assertEqual(by_id["pheromone.money_owed"].payload["trail"], "money:owed")

    def test_no_owed_means_no_pheromone(self):
        _settle(self.root, serve=True)
        ids = {s.id for s in signals(collect(self.root))}
        self.assertNotIn("pheromone.money_owed", ids)

    def test_readiness_gate_is_never_optimistic(self):
        gate = {s.id: s for s in signals(collect(self.root))}["readiness.gate"]
        self.assertFalse(gate.payload["payments_safe"])

    def test_a_broken_snapshot_degrades_instead_of_raising(self):
        result = signals({"snapshot": {"error": "boom"}})
        self.assertEqual(len(result), 1)
        self.assertLess(result[0].confidence, 0.5)


class PublishTests(BridgeBase):
    def test_first_run_publishes_contract_clean_envelopes(self):
        result = self._publish()
        self.assertGreater(len(result["published"]), 4)
        self.assertEqual(result["errors"], [])
        records = self._records()
        self.assertEqual(len(records), len(result["published"]))
        for raw in records:
            env = Envelope.from_dict(raw)  # enforces the P2 digest
            self.assertEqual(env.emitter, EMITTER)

    def test_second_run_with_no_change_publishes_nothing(self):
        self._publish()
        before = len(self._records())
        result = self._publish()
        self.assertEqual(result["published"], [])
        self.assertEqual(len(result["unchanged"]), result["signals"])
        self.assertEqual(len(self._records()), before)

    def test_a_change_republishes_only_what_changed(self):
        self._publish()
        before = len(self._records())
        _settle(self.root, serve=True)
        result = self._publish()
        ids = {row["id"] for row in result["published"]}
        self.assertIn("money.settled", ids)
        self.assertNotIn("worker.mode", ids)
        self.assertLess(len(result["published"]), result["signals"])
        self.assertGreater(len(self._records()), before)

    def test_force_republishes_everything(self):
        self._publish()
        result = self._publish(force=True)
        self.assertEqual(len(result["published"]), result["signals"])
        self.assertEqual(result["unchanged"], [])

    def test_dry_run_writes_nothing_at_all(self):
        result = self._publish(dry_run=True)
        self.assertTrue(result["published"])
        self.assertFalse(self.bus.exists())
        self.assertFalse(self.state.exists())

    def test_state_file_records_counts(self):
        self._publish()
        self._publish(force=True)
        data = json.loads(self.state.read_text())
        self.assertIn("money.settled", data)
        self.assertEqual(data["money.settled"]["count"], 2)

    def test_export_is_written_and_readable(self):
        result = self._publish()
        path = Path(result["exported"])
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text())
        self.assertEqual(payload["schema"], "console.telemetry/1")
        self.assertIn("readiness", payload)
        self.assertIn("money", payload)

    def test_no_secret_reaches_the_bus_or_the_export(self):
        (self.root / "keys").mkdir()
        (self.root / "keys" / "nwc.secret").write_text("nostr+walletconnect://SUPERSECRET")
        (self.root / "nostr_dvm" / "dvm.env").write_text(
            "DVM_PAID_ONLY=1\nGROQ_API_KEY=FILETOPSEKRET\n"
        )
        result = self._publish()
        blob = self.bus.read_text() + Path(result["exported"]).read_text()
        self.assertNotIn("SUPERSECRET", blob)
        self.assertNotIn("FILETOPSEKRET", blob)

    def test_concurrent_publish_is_serialised_by_the_bus(self):
        # Two publishes in a row still produce parseable, valid lines.
        self._publish()
        self._publish(force=True)
        for raw in self._records():
            Envelope.from_dict(raw)


class ExportTests(BridgeBase):
    def test_export_can_be_called_directly(self):
        path = export(self.root, "out/telemetry.json")
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text())["schema"], "console.telemetry/1")


if __name__ == "__main__":
    unittest.main()

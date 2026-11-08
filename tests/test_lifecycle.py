"""Logic verification and rail verification must never be confused."""

import json
import tempfile
import unittest
from pathlib import Path

from swarm.lifecycle import (
    LOGIC_VERIFIED,
    NOT_VERIFIED,
    RAIL_VERIFIED,
    evaluate,
    format_report,
    rail_proof,
    self_test,
)


class SelfTestTests(unittest.TestCase):
    def test_self_test_observes_every_invariant(self):
        result = self_test()
        self.assertTrue(result["ok"], result)
        steps = {s["step"]: s for s in result["steps"]}
        self.assertTrue(steps["refuse_service_before_payment"]["ok"])
        self.assertTrue(steps["single_use_claim"]["ok"])
        self.assertTrue(steps["serve_once"]["ok"])
        self.assertTrue(steps["integrity"]["ok"])

    def test_self_test_says_what_it_does_not_prove(self):
        result = self_test()
        self.assertIn("wallet", result["does_not_prove"])
        self.assertNotIn("wallet", result["proves"].lower())

    def test_self_test_leaves_no_database_behind(self):
        before = set(Path(tempfile.gettempdir()).glob("lifecycle-*"))
        self_test()
        after = set(Path(tempfile.gettempdir()).glob("lifecycle-*"))
        self.assertEqual(before, after)


class RailProofTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "logs").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _proof(self, **overrides):
        payload = {
            "kind": "rail",
            "job_id": "job-1",
            "payment_hash": "11" * 32,
            "preimage": "22" * 32,
            "fee_msat": 20_000,
            "settled_at": "2026-11-08T10:00:00Z",
        }
        payload.update(overrides)
        (self.root / "logs" / "lifecycle-proof.json").write_text(json.dumps(payload))
        return rail_proof(self.root)

    def test_no_proof_file_is_not_verified(self):
        result = rail_proof(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("lifecycle-proof.json", result["reason"])

    def test_a_clean_proof_is_verified(self):
        result = self._proof()
        self.assertTrue(result["ok"], result)

    def test_preimage_is_never_echoed(self):
        result = self._proof()
        self.assertNotIn("22" * 32, json.dumps(result))

    def test_bad_hash_is_rejected(self):
        self.assertFalse(self._proof(payment_hash="nope")["ok"])

    def test_preimage_equal_to_hash_is_rejected(self):
        self.assertFalse(self._proof(preimage="11" * 32)["ok"])

    def test_zero_fee_is_rejected(self):
        self.assertFalse(self._proof(fee_msat=0)["ok"])

    def test_wrong_kind_is_rejected(self):
        self.assertFalse(self._proof(kind="logic")["ok"])

    def test_corrupt_file_is_rejected_without_raising(self):
        (self.root / "logs" / "lifecycle-proof.json").write_text("{not json")
        result = rail_proof(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable", result["reason"])

    def test_proof_naming_a_ledger_without_a_settled_row_is_rejected(self):
        from nostr_dvm.settlement import SettlementLedger

        ledger_path = self.root / "logs" / "settlements.db"
        ledger = SettlementLedger(ledger_path)
        ledger.bind(
            job_id="job-1",
            requester="r",
            kind=5002,
            fee_msat=20_000,
            bolt11="",
            verify_url="https://wallet.test/verify/1",
        )
        ledger.close()
        result = self._proof(ledger="logs/settlements.db", job_id="job-1")
        self.assertFalse(result["ok"])
        self.assertIn("no settled row", result["reason"])


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_logic_only_is_not_rail_verified(self):
        report = evaluate(self.root)
        self.assertEqual(report["state"], LOGIC_VERIFIED)
        self.assertTrue(report["logic_verified"])
        self.assertFalse(report["rail_verified"])

    def test_rail_proof_upgrades_the_state(self):
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "logs" / "lifecycle-proof.json").write_text(
            json.dumps(
                {
                    "kind": "rail",
                    "job_id": "j",
                    "payment_hash": "aa" * 32,
                    "preimage": "bb" * 32,
                    "fee_msat": 1,
                    "settled_at": "2026-11-08T10:00:00Z",
                }
            )
        )
        report = evaluate(self.root)
        self.assertEqual(report["state"], RAIL_VERIFIED)

    def test_report_states_the_gap_in_words(self):
        text = format_report(evaluate(self.root))
        self.assertIn("real payment  : not verified", text)
        self.assertIn("does not prove", text)

    def test_never_verified_when_the_machine_is_broken(self):
        report = evaluate(self.root, run_self_test=False)
        self.assertEqual(report["state"], NOT_VERIFIED)


if __name__ == "__main__":
    unittest.main()

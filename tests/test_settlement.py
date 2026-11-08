"""Deterministic tests for the durable, job-bound settlement ledger.

No network, no relays, no inference: everything here is the money invariant.
"""

import tempfile
import unittest
from pathlib import Path

from nostr_dvm.settlement import (
    STATUS_EXPIRED,
    STATUS_INVOICED,
    STATUS_SERVED,
    STATUS_SETTLED,
    InvoiceConflict,
    SettlementLedger,
    bolt11_payment_hash,
    input_digest,
)

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def fake_bolt11(payment_hash_hex: str, amount: str = "50u") -> str:
    """Build a decodable BOLT11-shaped invoice carrying a known payment hash."""
    bits = format(int(payment_hash_hex, 16), "0256b").ljust(260, "0")
    groups = [int(bits[i:i + 5], 2) for i in range(0, 260, 5)]
    # tag 1 (payment hash), length 52 encoded as 10 bits, then 52 groups
    values = [1, 52 // 32, 52 % 32] + groups + [0] * 6  # trailing zeros = checksum
    return f"lnbc{amount}1" + "".join(CHARSET[v] for v in values)


class Bolt11Tests(unittest.TestCase):
    def test_payment_hash_round_trips(self):
        expected = "ab" * 32
        self.assertEqual(bolt11_payment_hash(fake_bolt11(expected)), expected)

    def test_garbage_invoice_has_no_hash(self):
        self.assertEqual(bolt11_payment_hash("not-an-invoice"), "")
        self.assertEqual(bolt11_payment_hash(""), "")
        self.assertEqual(bolt11_payment_hash("lnbc1qqqqqqq"), "")

    def test_input_digest_is_stable_and_distinct(self):
        self.assertEqual(input_digest("hello"), input_digest("hello"))
        self.assertNotEqual(input_digest("hello"), input_digest("hello "))


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settlements.db"
        self.ledger = SettlementLedger(self.path, default_timeout_s=60)

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def bind_job(self, job_id="job-a", fee=50_000, hash_hex=None, now=1000.0):
        hash_hex = hash_hex or ("%064x" % (abs(hash(job_id)) % (2 ** 256)))
        return self.ledger.bind(
            job_id=job_id,
            requester="pk" + job_id,
            kind=5002,
            fee_msat=fee,
            bolt11=fake_bolt11(hash_hex),
            verify_url=f"https://wallet.test/verify/{job_id}",
            input_sha256=input_digest("payload-" + job_id),
            timeout_s=60,
            now=now,
        )


class BindingTests(LedgerFixture):
    def test_bind_is_idempotent_per_job(self):
        first = self.bind_job()
        second = self.bind_job()  # same job id asks again
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(first.verify_url, second.verify_url)
        self.assertEqual(self.ledger.counters()["requested"], 1)

    def test_invoice_cannot_be_rebound_to_another_job(self):
        first = self.bind_job("job-a", hash_hex="11" * 32)
        with self.assertRaises(InvoiceConflict):
            self.ledger.bind(
                job_id="job-b",
                requester="pk-b",
                kind=5002,
                fee_msat=50_000,
                bolt11=first.bolt11,
                verify_url=first.verify_url,
                input_sha256=input_digest("payload-job-b"),
                timeout_s=60,
            )

    def test_payment_hash_is_recorded(self):
        row = self.bind_job("job-a", hash_hex="22" * 32)
        self.assertEqual(row.payment_hash, "22" * 32)
        self.assertIsNotNone(self.ledger.by_payment_hash("22" * 32))


class SettlementTests(LedgerFixture):
    def test_settlement_is_reported_once(self):
        self.bind_job("job-a")
        self.assertTrue(self.ledger.mark_settled("job-a", "preimage-1", now=1001.0))
        # A second verify response is not a second payment.
        self.assertFalse(self.ledger.mark_settled("job-a", "preimage-2", now=1002.0))
        row = self.ledger.get("job-a")
        self.assertEqual(row.status, STATUS_SETTLED)
        self.assertEqual(row.preimage, "preimage-1")
        self.assertEqual(self.ledger.counters()["earned_msat"], 50_000)

    def test_claim_is_single_use(self):
        self.bind_job("job-a")
        self.ledger.mark_settled("job-a", "p")
        self.assertTrue(self.ledger.claim("job-a"))
        self.assertFalse(self.ledger.claim("job-a"))
        self.assertEqual(self.ledger.get("job-a").status, "serving")

    def test_claim_requires_settlement(self):
        self.bind_job("job-a")
        self.assertFalse(self.ledger.claim("job-a"))
        self.assertEqual(self.ledger.get("job-a").status, STATUS_INVOICED)

    def test_release_returns_job_to_settled(self):
        self.bind_job("job-a")
        self.ledger.mark_settled("job-a", "p")
        self.ledger.claim("job-a")
        self.ledger.release("job-a", "inference blew up")
        row = self.ledger.get("job-a")
        self.assertEqual(row.status, STATUS_SETTLED)
        self.assertIn("inference blew up", row.last_error)
        self.assertEqual(self.ledger.counters()["unserved_paid"], 1)

    def test_served_caches_output_and_counts_income_once(self):
        self.bind_job("job-a", fee=20_000)
        self.ledger.mark_settled("job-a", "p")
        self.ledger.claim("job-a")
        self.ledger.mark_served("job-a", "the answer", result_event="evt-1", now=1003.0)
        row = self.ledger.get("job-a")
        self.assertEqual(row.status, STATUS_SERVED)
        self.assertEqual(row.output, "the answer")
        counters = self.ledger.counters()
        self.assertEqual(counters["served"], 1)
        self.assertEqual(counters["earned_msat"], 20_000)
        self.assertEqual(counters["unserved_paid"], 0)

    def test_expiry_then_reuse_of_the_same_invoice(self):
        self.bind_job("job-a")
        self.ledger.mark_expired("job-a")
        self.assertEqual(self.ledger.get("job-a").status, STATUS_EXPIRED)
        self.assertTrue(self.ledger.retry_without_new_invoice("job-a", now=2000.0))
        self.assertEqual(self.ledger.get("job-a").status, STATUS_INVOICED)
        # Still exactly one invoice for this job.
        self.assertEqual(self.ledger.counters()["requested"], 1)

    def test_failed_job_is_terminal(self):
        self.ledger.mark_failed("job-x", "rail down")
        self.assertEqual(self.ledger.get("job-x").status, "failed")
        self.assertFalse(self.ledger.claim("job-x"))


class DurabilityTests(LedgerFixture):
    def test_survives_restart_and_keeps_invoice(self):
        self.bind_job("job-a")
        self.ledger.mark_settled("job-a", "preimage")
        original = self.ledger.get("job-a")
        self.ledger.close()

        reopened = SettlementLedger(self.path, default_timeout_s=60)
        try:
            row = reopened.get("job-a")
            self.assertIsNotNone(row)
            self.assertEqual(row.status, STATUS_SETTLED)
            self.assertEqual(row.verify_url, original.verify_url)
            # The claim still works exactly once after the restart.
            self.assertTrue(reopened.claim("job-a"))
            self.assertFalse(reopened.claim("job-a"))
        finally:
            reopened.close()
        # Re-open so tearDown's close() has a live connection.
        self.ledger = SettlementLedger(self.path, default_timeout_s=60)

    def test_audit_trail_records_every_transition(self):
        self.bind_job("job-a")
        self.ledger.mark_settled("job-a", "p")
        self.ledger.claim("job-a")
        self.ledger.release("job-a", "boom")
        states = [t["to"] for t in self.ledger.trail("job-a")]
        self.assertEqual(
            states, [STATUS_INVOICED, STATUS_SETTLED, "serving", STATUS_SETTLED]
        )


class ReportingTests(LedgerFixture):
    def test_income_buckets_and_requesters(self):
        self.bind_job("job-a", fee=50_000)
        self.ledger.mark_settled("job-a", "p", now=1000.0)
        self.bind_job("job-b", fee=20_000)
        self.ledger.mark_settled("job-b", "p", now=1000.0)
        self.assertEqual(self.ledger.counters()["earned_msat"], 70_000)
        days = self.ledger.income_by_day(days=7, now=1000.0)
        self.assertEqual(len(days), 7)
        self.assertEqual(sum(d["msat"] for d in days), 70_000)
        top = self.ledger.by_requester()
        self.assertEqual(len(top), 2)
        self.assertEqual(top[0]["msat"], 50_000)

    def test_preimage_is_masked_in_exports(self):
        self.bind_job("job-a")
        self.ledger.mark_settled("job-a", "deadbeefdeadbeef")
        masked = self.ledger.get("job-a").to_dict()["preimage"]
        self.assertTrue(masked.endswith("…"))
        self.assertNotIn("deadbeefdeadbeef", masked)


class IntegrityReportTests(LedgerFixture):
    """The console shows these, so they must not be decorative."""

    def test_policy_checks_hold_with_no_ledger_at_all(self):
        from nostr_dvm.settlement import policy_checks

        checks = policy_checks()
        self.assertGreaterEqual(len(checks), 4)
        self.assertTrue(all(c["ok"] for c in checks))
        ids = {c["id"] for c in checks}
        self.assertIn("amount_tag_not_trusted", ids)
        self.assertIn("rail_fail_closed", ids)

    def test_integrity_is_clean_on_a_healthy_ledger(self):
        self.bind_job("job-a")
        self.ledger.mark_settled("job-a", "p")
        self.ledger.claim("job-a")
        self.ledger.mark_served("job-a", "an answer")
        checks = self.ledger.integrity()
        self.assertTrue(all(c["ok"] for c in checks), [c for c in checks if not c["ok"]])

    def test_integrity_catches_service_without_payment(self):
        self.bind_job("job-a")
        # Force the impossible state directly: served with no settlement.
        self.ledger.mark_served("job-a", "an answer")
        by_id = {c["id"]: c for c in self.ledger.integrity()}
        self.assertFalse(by_id["no_unpaid_service"]["ok"])
        self.assertIn("violation", by_id["no_unpaid_service"]["detail"])

    def test_integrity_catches_served_job_without_output(self):
        self.bind_job("job-a")
        self.ledger.mark_settled("job-a", "p")
        self.ledger.mark_served("job-a", "")
        by_id = {c["id"]: c for c in self.ledger.integrity()}
        self.assertFalse(by_id["served_has_output"]["ok"])


if __name__ == "__main__":
    unittest.main()

"""Deterministic tests for the paid job state machine.

Every case the handoff asked for is here: unpaid rejection, rail failure,
settled payment for the matching job, cross-job replay, duplicate delivery,
relay disconnect / restart, and inference failure. No network, no relays,
no inference provider.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from nostr_dvm.paid_flow import (
    OUTCOME_CONFLICT,
    OUTCOME_IN_FLIGHT,
    OUTCOME_INFER_ERROR,
    OUTCOME_PREVIOUSLY_FAILED,
    OUTCOME_RAIL_ERROR,
    OUTCOME_REPLAY,
    OUTCOME_SERVED,
    OUTCOME_TIMEOUT,
    serve_paid_job,
)
from nostr_dvm.settlement import SettlementLedger, input_digest

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bolt11(hash_hex: str) -> str:
    bits = format(int(hash_hex, 16), "0256b").ljust(260, "0")
    groups = [int(bits[i:i + 5], 2) for i in range(0, 260, 5)]
    values = [1, 52 // 32, 52 % 32] + groups + [0] * 6
    return "lnbc50u1" + "".join(CHARSET[v] for v in values)


class Invoice:
    def __init__(self, bolt11_value, verify_url, amount_msat):
        self.bolt11 = bolt11_value
        self.verify_url = verify_url
        self.amount_msat = amount_msat


class FakeRail:
    """LNURL-pay stand-in with per-invoice settlement control."""

    def __init__(self):
        self.invoices = {}
        self.invoice_calls = 0
        self.fail_invoice = False
        self.fail_verify = False

    def invoice(self, amount_msat, comment=""):
        if self.fail_invoice:
            raise RuntimeError("wallet unreachable")
        self.invoice_calls += 1
        url = f"https://wallet.test/verify/{self.invoice_calls}"
        value = bolt11("%064x" % (self.invoice_calls * 7919))
        self.invoices[url] = False
        return Invoice(value, url, amount_msat)

    def check_settled(self, verify_url):
        if self.fail_verify:
            raise RuntimeError("verify endpoint timeout")
        return (bool(self.invoices.get(verify_url)), "preimage-" + verify_url[-1])

    def pay(self, verify_url):
        self.invoices[verify_url] = True


def job(job_id="a" * 64, pubkey="pk" * 32, text="summarize this", tags=None):
    import json

    return {
        "id": job_id,
        "pubkey": pubkey,
        "kind": 5002,
        "content": json.dumps({"input": text}),
        "tags": tags or [],
    }


class FlowFixture(unittest.IsolatedAsyncioTestCase):
    fees = {5002: 50_000, 5003: 20_000}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "settlements.db"
        self.ledger = SettlementLedger(self.db, default_timeout_s=120)
        self.rail = FakeRail()
        self.notifs = []
        self.delivered = []
        self.inferences = 0
        self.clock = [1000.0]

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    async def fake_infer(self, kind, content):
        self.inferences += 1
        return "answer for " + content

    async def notify(self, status, body="", extra=()):
        self.notifs.append((status, body, tuple(extra)))

    async def deliver(self, output):
        self.delivered.append(output)

    async def fake_sleep(self, seconds):
        # Advance fake time instead of sleeping; keeps the suite instant.
        self.clock[0] += seconds

    def statuses(self):
        return [n[0] for n in self.notifs]

    async def run_job(self, event, infer=None, timeout_s=12, poll_s=3):
        return await serve_paid_job(
            event=event,
            kind=event["kind"],
            rail=self.rail,
            ledger=self.ledger,
            infer=infer or self.fake_infer,
            notify=self.notify,
            deliver=self.deliver,
            fee_msat=self.fees[event["kind"]],
            timeout_s=timeout_s,
            poll_s=poll_s,
            clock=lambda: self.clock[0],
            sleep=self.fake_sleep,
        )


class UnpaidTests(FlowFixture):
    async def test_unpaid_job_is_invoiced_then_never_served(self):
        outcome = await self.run_job(job())
        self.assertEqual(outcome.outcome, OUTCOME_TIMEOUT)
        self.assertIn("payment-required", self.statuses())
        self.assertIn("payment-timeout", self.statuses())
        self.assertEqual(self.inferences, 0)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.ledger.counters()["served"], 0)

    async def test_amount_tag_is_not_payment(self):
        # A requester claiming a huge amount must still be invoiced.
        outcome = await self.run_job(job(tags=[["amount", "999999999"]]))
        self.assertEqual(outcome.outcome, OUTCOME_TIMEOUT)
        self.assertEqual(self.inferences, 0)
        self.assertEqual(self.rail.invoice_calls, 1)

    async def test_timeout_reuses_the_same_invoice(self):
        event = job()
        await self.run_job(event)
        first = self.ledger.get(event["id"])
        await self.run_job(event)  # redelivery after expiry
        self.assertEqual(self.rail.invoice_calls, 1, "must not mint a second invoice")
        current = self.ledger.get(event["id"])
        self.assertEqual(current.bolt11, first.bolt11)


class RailFailureTests(FlowFixture):
    async def test_invoice_failure_fails_closed(self):
        self.rail.fail_invoice = True
        outcome = await self.run_job(job())
        self.assertEqual(outcome.outcome, OUTCOME_RAIL_ERROR)
        self.assertEqual(self.inferences, 0)
        self.assertIn("payment-error", self.statuses())
        self.assertEqual(self.ledger.counters()["earned_msat"], 0)

    async def test_verify_failure_fails_closed(self):
        self.rail.fail_verify = True
        outcome = await self.run_job(job(), timeout_s=30)
        self.assertEqual(outcome.outcome, OUTCOME_RAIL_ERROR)
        self.assertEqual(self.inferences, 0, "an unverified payment must never serve")
        self.assertIn("payment-error", self.statuses())

    async def test_missing_rail_fails_closed(self):
        self.rail = None
        outcome = await serve_paid_job(
            event=job(),
            kind=5002,
            rail=None,
            ledger=self.ledger,
            infer=self.fake_infer,
            notify=self.notify,
            deliver=self.deliver,
            fee_msat=50_000,
            clock=lambda: self.clock[0],
            sleep=self.fake_sleep,
        )
        self.assertEqual(outcome.outcome, OUTCOME_RAIL_ERROR)
        self.assertEqual(self.inferences, 0)

    async def test_failed_job_does_not_remint_on_redelivery(self):
        self.rail.fail_invoice = True
        event = job()
        await self.run_job(event)
        self.rail.fail_invoice = False
        outcome = await self.run_job(event)
        self.assertEqual(outcome.outcome, OUTCOME_PREVIOUSLY_FAILED)
        self.assertEqual(self.rail.invoice_calls, 0)


class PaymentTests(FlowFixture):
    async def test_settled_payment_for_matching_job_serves(self):
        event = job()
        # Pay the invoice the gate mints. The poll sees settlement.
        original_invoice = self.rail.invoice

        def invoice_then_pay(amount_msat, comment=""):
            inv = original_invoice(amount_msat, comment)
            self.rail.pay(inv.verify_url)
            return inv

        self.rail.invoice = invoice_then_pay
        outcome = await self.run_job(event)
        self.assertEqual(outcome.outcome, OUTCOME_SERVED)
        self.assertEqual(self.inferences, 1)
        self.assertEqual(self.delivered, ["answer for summarize this"])
        row = self.ledger.get(event["id"])
        self.assertEqual(row.status, "served")
        self.assertTrue(row.preimage)
        self.assertEqual(self.ledger.counters()["earned_msat"], 50_000)

    async def test_cross_job_settlement_cannot_unlock_a_different_job(self):
        paid = job(job_id="b" * 64, text="cheap summarize")
        paid["kind"] = 5003  # 20 sats

        # Only the FIRST invoice ever settles. The second must stay unpaid and
        # must not inherit any credit from the first.
        original = self.rail.invoice
        calls = {"n": 0}

        def pay_first_only(amount_msat, comment=""):
            inv = original(amount_msat, comment)
            calls["n"] += 1
            if calls["n"] == 1:
                self.rail.pay(inv.verify_url)
            return inv

        self.rail.invoice = pay_first_only
        first = await self.run_job(paid)
        self.assertEqual(first.outcome, OUTCOME_SERVED)

        # A second, unrelated, more expensive job must not be served off the
        # settlement that belongs to the first.
        other = job(job_id="c" * 64, text="expensive generation")
        second = await self.run_job(other)
        self.assertEqual(second.outcome, OUTCOME_TIMEOUT)
        self.assertEqual(self.inferences, 1, "the settled job must not fund another")
        self.assertEqual(self.ledger.counters()["earned_msat"], 20_000)

    def _paying_invoice(self):
        original = self.rail.invoice

        def wrapped(amount_msat, comment=""):
            inv = original(amount_msat, comment)
            self.rail.pay(inv.verify_url)
            return inv

        return wrapped

    async def test_duplicate_delivery_infers_once_and_replays(self):
        event = job()
        self.rail.invoice = self._paying_invoice()
        first = await self.run_job(event)
        second = await self.run_job(event)  # same event id, second relay
        self.assertEqual(first.outcome, OUTCOME_SERVED)
        self.assertEqual(second.outcome, OUTCOME_REPLAY)
        self.assertEqual(self.inferences, 1)
        self.assertEqual(self.rail.invoice_calls, 1)
        self.assertEqual(self.delivered, ["answer for summarize this"] * 2)

    async def test_concurrent_duplicate_loses_the_claim(self):
        event = job()
        self.rail.invoice = self._paying_invoice()
        await self.run_job(event)
        # Simulate a crash between claim and delivery: force the row back to
        # "serving" and confirm a second delivery does not infer.
        self.ledger._db.execute(
            "UPDATE invoices SET status='serving' WHERE job_id=?", (event["id"],)
        )
        self.ledger._db.commit()
        outcome = await self.run_job(event)
        self.assertEqual(outcome.outcome, OUTCOME_IN_FLIGHT)
        self.assertEqual(self.inferences, 1)


class RestartTests(FlowFixture):
    async def test_paid_job_survives_worker_restart(self):
        event = job()
        self.rail.invoice = self._paying_invoice_for(event)
        outcome = await self.run_job(event)
        self.assertEqual(outcome.outcome, OUTCOME_SERVED)

        # "Restart": new connection to the same database, fresh in-memory state.
        self.ledger.close()
        self.ledger = SettlementLedger(self.db, default_timeout_s=120)
        replay = await self.run_job(event)
        self.assertEqual(replay.outcome, OUTCOME_REPLAY)
        self.assertEqual(self.inferences, 1, "a restart must not re-serve or re-charge")

    def _paying_invoice_for(self, event):
        original = self.rail.invoice

        def wrapped(amount_msat, comment=""):
            inv = original(amount_msat, comment)
            self.rail.pay(inv.verify_url)
            return inv

        return wrapped

    async def test_settled_unserved_job_serves_after_reconnect_without_new_invoice(self):
        event = job()
        # Invoice and settlement happen, then the process dies before inference.
        async def boom(kind, content):
            raise RuntimeError("worker died")

        self.rail.invoice = self._paying_invoice_for(event)
        first = await self.run_job(event, infer=boom)
        self.assertEqual(first.outcome, OUTCOME_INFER_ERROR)
        self.assertEqual(self.ledger.counters()["unserved_paid"], 1)

        # Reconnect: same event arrives again. It must serve from the already
        # settled invoice, not ask for payment twice.
        self.ledger.close()
        self.ledger = SettlementLedger(self.db, default_timeout_s=120)
        second = await self.run_job(event)
        self.assertEqual(second.outcome, OUTCOME_SERVED)
        self.assertEqual(self.rail.invoice_calls, 1)
        self.assertEqual(self.ledger.counters()["earned_msat"], 50_000)


class InferenceFailureTests(FlowFixture):
    async def test_inference_failure_keeps_money_and_leaves_job_retryable(self):
        event = job()
        self.rail.invoice = self._pay_inline()
        calls = {"n": 0}

        async def flaky(kind, content):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("upstream 502")
            return "second try worked"

        self.rail.invoice = self._paying_invoice_and_track()
        first = await self.run_job(event, infer=flaky)
        self.assertEqual(first.outcome, OUTCOME_INFER_ERROR)
        self.assertEqual(self.delivered, [])
        row = self.ledger.get(event["id"])
        self.assertEqual(row.status, "settled", "claim released for retry")
        self.assertIn("upstream 502", row.last_error)
        self.assertEqual(self.ledger.counters()["earned_msat"], 50_000)

        # Retry serves without a second invoice or a second charge.
        second = await self.run_job(event, infer=flaky)
        self.assertEqual(second.outcome, OUTCOME_SERVED)
        self.assertEqual(self.delivered, ["second try worked"])
        self.assertEqual(self.rail.invoice_calls, 1)
        self.assertEqual(self.ledger.counters()["earned_msat"], 50_000)

    def _pay_inline(self):
        return self.rail.invoice

    def _paying_invoice_and_track(self):
        original = self.rail.invoice

        def wrapped(amount_msat, comment=""):
            inv = original(amount_msat, comment)
            self.rail.pay(inv.verify_url)
            return inv

        return wrapped

    async def test_empty_completion_is_an_inference_failure(self):
        event = job()
        self.rail.invoice = self._paying_invoice_and_track()

        async def empty(kind, content):
            return "   "

        outcome = await self.run_job(event, infer=empty)
        self.assertEqual(outcome.outcome, OUTCOME_INFER_ERROR)
        self.assertEqual(self.delivered, [])


class IntegrityTests(FlowFixture):
    async def test_payload_changed_under_the_same_id_is_refused(self):
        event = job()
        await self.run_job(event)  # invoices the job
        tampered = job(text="a different, more expensive prompt")
        outcome = await self.run_job(tampered)
        self.assertEqual(outcome.outcome, "input_mismatch")
        self.assertEqual(self.inferences, 0)

    async def test_invoice_cannot_be_reused_across_jobs(self):
        first = job(job_id="d" * 64)
        original = self.rail.invoice
        holder = {"inv": None}

        def mint_once(amount_msat, comment=""):
            # First job: mint and pay. Every later job is handed the SAME
            # invoice, as a broken or hostile rail would.
            if holder["inv"] is None:
                holder["inv"] = original(amount_msat, comment)
                self.rail.pay(holder["inv"].verify_url)
            return holder["inv"]

        self.rail.invoice = mint_once
        self.assertEqual((await self.run_job(first)).outcome, OUTCOME_SERVED)

        second = job(job_id="e" * 64, text="second job")
        outcome = await self.run_job(second)
        self.assertEqual(outcome.outcome, OUTCOME_CONFLICT)
        self.assertEqual(self.inferences, 1, "a reused invoice must never serve")
        self.assertEqual(self.ledger.counters()["earned_msat"], 50_000)

    def _paying_invoice(self):
        original = self.rail.invoice

        def wrapped(amount_msat, comment=""):
            inv = original(amount_msat, comment)
            self.rail.pay(inv.verify_url)
            return inv

        return wrapped

    async def test_digest_matches_extracted_input(self):
        event = job(text="exact text")
        await self.run_job(event)
        row = self.ledger.get(event["id"])
        self.assertEqual(row.input_sha256, input_digest("exact text"))


if __name__ == "__main__":
    unittest.main()

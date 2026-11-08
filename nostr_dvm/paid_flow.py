#!/usr/bin/env python3
"""
paid_flow.py — the paid job state machine
=========================================

DNA_TAG: ORIGIN=BleakNarratives/passive_income_swarm | PILLAR=nostr_dvm

This is the money path, isolated from Nostr plumbing and inference clients so
it can be exercised deterministically with fakes. The worker supplies four
collaborators; nothing here imports websockets, coincurve or requests.

    rail    -> invoice(fee_msat, comment=...) / check_settled(verify_url)
    ledger  -> SettlementLedger (durable, job-bound, single-use claims)
    infer   -> run_inference(kind, content) -> str
    notify  -> publish a kind-7000 status feedback event
    deliver -> publish the kind-6002/6003 result event

FAIL-CLOSED RULES
-----------------
1. An ``amount`` tag on the request is never consulted. Only a verified
   settlement recorded in the durable ledger can open the inference gate.
2. Every rail exception ends the job as ``payment_rail_error``. There is no
   "serve it anyway" path — that is how the old gate gave away free inference.
3. One invoice per job id, ever. A retry re-publishes the existing BOLT11 and
   re-opens its poll window; it never mints a second invoice, so a customer who
   is mid-payment cannot be double-charged.
4. The service claim is single-use and keyed by job id, so paying for a cheap
   job can never unlock a different, more expensive one.
5. A served job replays its stored output. A paid-but-unsuccessful job stays
   visible as paid-and-unserved instead of silently vanishing.

Money is confirmed once, in one place: ``SettlementLedger.mark_settled``.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

try:  # running as a script: the worker directory is on sys.path
    from settlement import (  # type: ignore
        STATUS_EXPIRED,
        STATUS_INVOICED,
        STATUS_SERVED,
        STATUS_SETTLED,
        STATUS_SERVING,
        InvoiceConflict,
        SettlementLedger,
        input_digest,
    )
except ImportError:  # imported as a package (tests, dashboard, swarm)
    from .settlement import (  # type: ignore
        STATUS_EXPIRED,
        STATUS_INVOICED,
        STATUS_SERVED,
        STATUS_SETTLED,
        STATUS_SERVING,
        InvoiceConflict,
        SettlementLedger,
        input_digest,
    )

__all__ = ["PaidOutcome", "serve_paid_job", "extract_input"]

#: Outcome vocabulary. Stable strings — the dashboard and the job ledger match on
#: these, so treat them as a contract.
OUTCOME_SERVED = "served_paid"
OUTCOME_REPLAY = "replayed_served"
OUTCOME_IN_FLIGHT = "in_progress"
OUTCOME_TIMEOUT = "payment_timeout"
OUTCOME_RAIL_ERROR = "payment_rail_error"
OUTCOME_CONFLICT = "invoice_conflict"
OUTCOME_MISMATCH = "input_mismatch"
OUTCOME_INFER_ERROR = "inference_error"
OUTCOME_PREVIOUSLY_FAILED = "failed_previously"
OUTCOME_EMPTY = "empty_input"


@dataclass
class PaidOutcome:
    """What happened to one paid job. Logged verbatim to jobs.jsonl."""

    outcome: str
    job_id: str
    fee_msat: int = 0
    preimage: str = ""
    output: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job_id": self.job_id,
            "fee_msat": self.fee_msat,
            "preimage": self.preimage[:16] if self.preimage else "",
            "output_chars": len(self.output),
            "detail": self.detail,
        }


def extract_input(event: Dict[str, Any]) -> str:
    """NIP-90 request payload: JSON ``{"input": ...}`` when present, else raw."""
    raw = event.get("content") or ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(payload, dict):
        value = payload.get("input", "")
        return value if isinstance(value, str) else str(value or "")
    return raw


async def _invoke(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call sync or async collaborators without blocking the event loop."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))
    if inspect.isawaitable(result):
        return await result
    return result


async def _poll_settlement(
    rail: Any,
    verify_url: str,
    *,
    deadline: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    max_rail_errors: int,
) -> tuple:
    """Poll the LNURL verify endpoint. Returns (settled, preimage, error)."""
    errors = 0
    while True:
        remaining = deadline - clock()
        try:
            settled, preimage = await _invoke(rail.check_settled, verify_url)
            errors = 0
        except Exception as exc:  # rail unreachable / malformed response
            errors += 1
            if errors >= max_rail_errors:
                return False, None, f"{type(exc).__name__}: {exc}"
            if clock() + poll_s >= deadline:
                return False, None, f"{type(exc).__name__}: {exc}"
            await sleep(poll_s)
            continue
        if settled:
            return True, preimage, ""
        if remaining <= poll_s:
            # Final confirmation before declaring a timeout: an invoice paid
            # right at the deadline must not be dropped.
            try:
                settled, preimage = await _invoke(rail.check_settled, verify_url)
            except Exception as exc:
                return False, None, f"{type(exc).__name__}: {exc}"
            return settled, preimage, ""
        await sleep(poll_s)


async def serve_paid_job(
    *,
    event: Dict[str, Any],
    kind: int,
    rail: Any,
    ledger: SettlementLedger,
    infer: Callable[[int, str], Any],
    notify: Callable[..., Any],
    deliver: Callable[[str], Any],
    fee_msat: int,
    timeout_s: float = 120.0,
    poll_s: float = 3.0,
    max_rail_errors: int = 3,
    clock: Callable[[], float] = time.time,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> PaidOutcome:
    """Serve one paid-mode job, or explain precisely why it was not served."""
    sleep = sleep or asyncio.sleep
    job_id = str(event.get("id") or "")
    requester = str(event.get("pubkey") or "")
    content = extract_input(event)
    digest = input_digest(content)

    if not job_id:
        return PaidOutcome(OUTCOME_EMPTY, job_id="", detail="event has no id")
    if not content.strip():
        await notify("error", "empty input")
        return PaidOutcome(OUTCOME_EMPTY, job_id=job_id, detail="empty input")

    record = ledger.get(job_id)

    # ── Already delivered: replay the stored answer. No new invoice, no charge,
    #    no inference spend.
    if record is not None and record.status == STATUS_SERVED:
        await deliver(record.output or "")
        return PaidOutcome(
            OUTCOME_REPLAY,
            job_id=job_id,
            fee_msat=record.fee_msat,
            preimage=record.preimage or "",
            output=record.output or "",
            detail="already served — replaying stored result",
        )

    # ── A rail failure is terminal. Re-minting on redelivery would let a broken
    #    rail loop forever and would hide the fault from the operator.
    if record is not None and record.status == "failed":
        return PaidOutcome(
            OUTCOME_PREVIOUSLY_FAILED,
            job_id=job_id,
            detail=record.last_error or "invoice could not be issued",
        )

    # ── NIP-01 event ids are content-addressed; a mismatch means corruption or
    #    a forged id. Either way this payload is not the job that was invoiced.
    if record is not None and record.input_sha256 and record.input_sha256 != digest:
        await notify("error", "request id does not match its invoiced payload")
        return PaidOutcome(
            OUTCOME_MISMATCH,
            job_id=job_id,
            detail="payload changed under the same event id",
        )

    # ── Invoice (once per job), then wait for sats.
    if record is None:
        if rail is None:
            await notify("payment-error", "payment rail unavailable; job not served")
            ledger.mark_failed(job_id, "payment rail unavailable")
            return PaidOutcome(
                OUTCOME_RAIL_ERROR, job_id=job_id, detail="payment rail unavailable"
            )
        try:
            invoice = await _invoke(
                rail.invoice, int(fee_msat), f"DVM job {job_id[:12]}"
            )
        except Exception as exc:
            await notify("payment-error", "payment-error: invoice could not be issued")
            ledger.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
            return PaidOutcome(
                OUTCOME_RAIL_ERROR,
                job_id=job_id,
                detail=f"invoice failed: {type(exc).__name__}: {exc}",
            )

        bolt11 = getattr(invoice, "bolt11", "") or ""
        verify_url = getattr(invoice, "verify_url", "") or ""
        if not bolt11 or not verify_url:
            await notify("payment-error", "payment-error: invoice missing verify handle")
            ledger.mark_failed(job_id, "invoice lacked bolt11 or verify url")
            return PaidOutcome(
                OUTCOME_RAIL_ERROR,
                job_id=job_id,
                detail="rail returned an unusable invoice",
            )
        try:
            record = ledger.bind(
                job_id=job_id,
                requester=requester,
                kind=kind,
                fee_msat=int(fee_msat),
                bolt11=bolt11,
                verify_url=verify_url,
                input_sha256=digest,
                timeout_s=timeout_s,
                now=clock(),
            )
        except InvoiceConflict as exc:
            await notify("payment-error", "payment-error: invoice already in use")
            return PaidOutcome(OUTCOME_CONFLICT, job_id=job_id, detail=str(exc))
    elif record.status == STATUS_EXPIRED:
        # Reuse the existing invoice and give it one more poll window.
        if not ledger.retry_without_new_invoice(job_id, now=clock()):
            return PaidOutcome(
                OUTCOME_TIMEOUT, job_id=job_id, fee_msat=record.fee_msat,
                detail="invoice expired and could not be reopened",
            )
        record = ledger.get(job_id) or record

    paid_already = record.is_paid or record.status in (STATUS_SETTLED, STATUS_SERVING)

    if not paid_already:
        # Re-publish the *same* invoice so a reconnecting requester can pay it.
        amount = int(record.fee_msat or fee_msat)
        await notify(
            "payment-required",
            json.dumps({"bolt11": record.bolt11, "amount_msat": amount}),
            (("amount", str(amount)),),
        )
        deadline = clock() + float(timeout_s)
        settled, preimage, error = await _poll_settlement(
            rail,
            record.verify_url,
            deadline=deadline,
            poll_s=poll_s,
            clock=clock,
            sleep=sleep,
            max_rail_errors=max_rail_errors,
        )
        if error:
            # Fail closed: never infer on an unverified payment.
            await notify("payment-error", "payment-error: verification unavailable")
            return PaidOutcome(
                OUTCOME_RAIL_ERROR,
                job_id=job_id,
                fee_msat=amount,
                detail=error,
            )
        if not settled:
            ledger.mark_expired(job_id)
            await notify("payment-timeout", "", (("amount", str(amount)),))
            return PaidOutcome(
                OUTCOME_TIMEOUT,
                job_id=job_id,
                fee_msat=amount,
                detail="invoice not settled within the poll window",
            )
        ledger.mark_settled(job_id, preimage, now=clock())
        record = ledger.get(job_id) or record
    else:
        preimage = record.preimage or ""

    # ── Single-use, job-bound claim. A duplicate delivery loses this race.
    if not ledger.claim(job_id):
        current = ledger.get(job_id)
        if current is not None and current.status == STATUS_SERVED:
            await deliver(current.output or "")
            return PaidOutcome(
                OUTCOME_REPLAY, job_id=job_id, fee_msat=current.fee_msat,
                output=current.output or "", detail="served by a concurrent delivery",
            )
        if current is not None and current.status == STATUS_SERVING:
            return PaidOutcome(
                OUTCOME_IN_FLIGHT, job_id=job_id, fee_msat=current.fee_msat,
                detail="another delivery is serving this job",
            )
        return PaidOutcome(
            OUTCOME_MISMATCH, job_id=job_id, detail="claim refused in state "
            f"{current.status if current else 'missing'}",
        )

    await notify("processing", "")
    try:
        output = await _invoke(infer, kind, content)
        if not isinstance(output, str):
            output = str(output or "")
        if not output.strip():
            raise RuntimeError("empty completion")
    except Exception as exc:
        # Paid but undelivered. Release the claim so a retry can serve it, and
        # keep the row visible: this is real money owed to a real customer.
        ledger.release(job_id, f"{type(exc).__name__}: {exc}")
        await notify("error", f"inference-error: {type(exc).__name__}")
        return PaidOutcome(
            OUTCOME_INFER_ERROR,
            job_id=job_id,
            fee_msat=int(record.fee_msat or fee_msat),
            preimage=preimage or "",
            detail=f"{type(exc).__name__}: {exc}",
        )

    await deliver(output)
    ledger.mark_served(job_id, output, now=clock())
    return PaidOutcome(
        OUTCOME_SERVED,
        job_id=job_id,
        fee_msat=int(record.fee_msat or fee_msat),
        preimage=preimage or "",
        output=output,
    )

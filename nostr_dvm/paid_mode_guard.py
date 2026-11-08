#!/usr/bin/env python3
"""
paid_mode_guard.py — fail-closed launcher for the NIP-90 worker
===============================================================

DNA_TAG: ORIGIN=BleakNarratives/passive_income_swarm | PILLAR=nostr_dvm

WHAT CHANGED AND WHY
--------------------
An earlier version of this file monkey-patched the running worker: it zeroed
``extract_amount_msat``, forced ``PAID_ONLY``, and let a **global
settlement-credit counter** unlock inference. That was wrong in a way that
matters with real money:

* the credit was not bound to a job, so settling a 20-sat summarization could
  unlock a 50-sat generation;
* the counter lived in memory, so a restart forgot the customer had paid;
* patching a live module from outside its own call path is untestable and
  invisible to the code it is supposed to protect.

The paid path now lives where it belongs — ``paid_flow.serve_paid_job`` over
``settlement.SettlementLedger`` — and enforces job binding, single-use claims
and idempotent replay in the worker itself. This module is therefore reduced to
what a launcher should be: **preflight, then run**. If the preflight cannot
prove the rail and the inference credentials are present, it refuses to start
rather than serving work it cannot bill.
"""

from __future__ import annotations

import os
import sys

__all__ = ["preflight", "main", "PreflightError"]

REQUIRED_ENV = {
    # Paid mode is not optional when launched through this file.
    "DVM_PAID_ONLY": "1",
}


class PreflightError(RuntimeError):
    """Raised when the worker cannot be started without risking unpaid work."""


def preflight(worker) -> dict:
    """Verify the worker can bill before it is allowed to serve.

    Returns a small report (useful in logs and in the dashboard). Raises
    :class:`PreflightError` — never returns a "probably fine" verdict.
    """
    problems = []

    if not getattr(worker, "PAID_ONLY", False):
        problems.append("DVM_PAID_ONLY is not enabled in the worker module")
    if getattr(worker, "_PAYMENT_RAIL", None) is None:
        problems.append(
            "LNURL payment rail did not initialise — refusing to serve unbillable jobs"
        )
    if not getattr(worker, "GROQ_KEY", None):
        problems.append("GROQ_API_KEY is missing — inference would fail after payment")

    ledger_factory = getattr(worker, "settlement_ledger", None)
    if ledger_factory is None:
        problems.append("worker does not expose settlement_ledger()")
        ledger = None
    else:
        try:
            ledger = ledger_factory()
            ledger.counters()  # prove the store is writable, not just present
        except Exception as exc:  # pragma: no cover - disk/permission failure
            problems.append(f"settlement ledger unusable: {type(exc).__name__}: {exc}")
            ledger = None

    if problems:
        raise PreflightError("; ".join(problems))

    return {
        "paid_only": True,
        "rail": True,
        "groq_key": True,
        "settlement_db": str(getattr(ledger, "path", "?")),
    }


def main() -> None:
    # The worker reads these at import time.
    for key, value in REQUIRED_ENV.items():
        os.environ[key] = value

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    try:
        import dvm_worker as worker  # noqa: WPS433 (deliberate late import)
        report = preflight(worker)
    except Exception as exc:
        raise SystemExit(f"[PAID-GUARD] preflight failed closed: {exc}") from exc

    print(
        "[PAID-GUARD] preflight ok — job-bound settlement enforced "
        f"(ledger={report['settlement_db']})",
        flush=True,
    )
    import asyncio

    asyncio.run(worker.main())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fail-closed launcher guard for the existing NIP-90 worker.

The upstream worker currently trusts an incoming `amount` tag as proof of
payment, and can continue if invoice creation/checking raises. This wrapper
forces every request through its LNURL invoice flow and permits inference only
after the configured rail reports settlement. It does not create demand or
guarantee earnings; it prevents unpaid jobs from consuming inference.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading


class SettlementCredits:
    """Thread-safe count of independently verified invoice settlements."""

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def add(self) -> None:
        with self._lock:
            self._count += 1

    def consume(self) -> bool:
        with self._lock:
            if self._count < 1:
                return False
            self._count -= 1
            return True


def install_guard(worker) -> SettlementCredits:
    """Patch the existing worker in-process; no changes to its job protocol."""
    if not getattr(worker, "_PAYMENT_RAIL", None):
        raise RuntimeError("LNURL payment rail did not initialize; refusing to serve")
    if not getattr(worker, "GROQ_KEY", None):
        raise RuntimeError("GROQ_API_KEY is missing; refusing to start")

    credits = SettlementCredits()
    rail = worker._PAYMENT_RAIL
    original_check = rail.check_settled

    def checked_settlement(verify_url):
        settled, preimage = original_check(verify_url)
        if settled:
            credits.add()
        return settled, preimage

    rail.check_settled = checked_settlement

    # An untrusted NIP-90 amount tag is a claim, not a payment receipt.
    worker.extract_amount_msat = lambda _event: 0
    worker.PAID_ONLY = True

    original_inference = worker.run_inference

    def paid_inference(kind, content):
        if not credits.consume():
            raise PermissionError("No verified settlement credit; inference blocked")
        return original_inference(kind, content)

    worker.run_inference = paid_inference
    return credits


def main() -> None:
    # These are read by dvm_worker at import time. Do not allow env to disable
    # the payment gate or permit multiple simultaneous invoice/inference paths.
    os.environ["DVM_PAID_ONLY"] = "1"
    os.environ["DVM_MAX_INFLIGHT"] = "1"
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    try:
        import dvm_worker as worker
        install_guard(worker)
    except Exception as exc:
        raise SystemExit(f"Paid worker preflight failed closed: {exc}") from exc

    print("[PAID-GUARD] enabled: amount tags ignored; inference requires verified settlement", flush=True)
    asyncio.run(worker.main())


if __name__ == "__main__":
    main()

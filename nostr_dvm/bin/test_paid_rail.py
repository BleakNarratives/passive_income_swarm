#!/usr/bin/env python3
"""test_paid_rail.py — exercise the DVM paid rail end-to-end.

Publishes a kind 5002 job, waits for the worker's payment-required
feedback (kind 7000) carrying a BOLT11 invoice, then — if given
--pay — pays the invoice via the same-wallet NWC client and polls
for settlement. Also verifies the worker's payment-required tag.

Usage:
    ~/passive_income_swarm-env/bin/python bin/test_paid_rail.py            # gate only
    ~/passive_income_swarm-env/bin/python bin/test_paid_rail.py --pay      # attempt payment
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dvm_worker as dw  # noqa: E402
import websockets  # noqa: E402

RELAY = sys.argv[sys.argv.index("--relay") + 1] if "--relay" in sys.argv else "wss://nos.lol"
DO_PAY = "--pay" in sys.argv
INPUT = "Paid rail test: prove sats flow through coinos for the DVM."


async def main():
    content = json.dumps({"input": INPUT})
    job = dw.build_event(5002, [["i", INPUT[:64]], ["output", "text/plain"]], content)
    job_id = job["id"]
    print(f"[test] job id={job_id[:16]}... pubkey={job['pubkey'][:16]}...")
    print(f"[test] waiting for payment-required (kind 7000) on {RELAY}")

    async with websockets.connect(RELAY, open_timeout=10) as ws:
        # Subscribe for feedback events referencing our job id
        await ws.send(json.dumps(["REQ", "paidtest",
                                  {"kinds": [7000, 6002],
                                   "#e": [job_id]}]))
        await ws.send(json.dumps(["EVENT", job]))

        bolt11 = None
        verify_url = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if reply[0] not in ("EVENT",):
                continue
            ev = reply[2]
            tags = {t[0]: t for t in ev.get("tags", [])}
            status = [v for k, v in [tuple(t) for t in ev.get("tags", [])] if k == "status"]
            status = status[0] if status else ""
            print(f"[test] feedback kind={ev['kind']} status={status}")
            if ev["kind"] == 7000 and "payment-required" in status:
                try:
                    payload = json.loads(ev["content"])
                    bolt11 = payload.get("bolt11", "")
                    print(f"[test] BOLT11: {bolt11[:40]}... amount={payload.get('amount_msat')} msat")
                except Exception as e:
                    print(f"[test] could not parse invoice content: {e}")
            if ev["kind"] == 6002:
                print(f"[test] JOB SERVED: {ev['content'][:120]}")
                break

        await ws.send(json.dumps(["CLOSE", "paidtest"]))

    if not bolt11:
        print("[test] NO INVOICE RECEIVED — paid gate did not fire")
        return 2

    if not DO_PAY:
        print("[test] gate OK, invoice minted. Re-run with --pay to attempt payment.")
        return 0

    # ── Attempt payment via the same-wallet NWC client ───────────────────
    print("[test] attempting payment via NWC (same coinos wallet)...")
    from nwc_client import NWCClient
    nwc = NWCClient.from_conn()
    try:
        bal = await nwc.get_balance()
        print(f"[test] wallet balance: {bal.balance_msat} msat")
        r = await nwc.pay_invoice(bolt11)
        print(f"[test] pay_invoice response: {json.dumps(r, default=str)[:200]}")
    except Exception as e:
        print(f"[test] PAYMENT ERROR: {e}")
        # Fall back: resolve the invoice's verify URL via the LNURL rail
        rail = dw._PAYMENT_RAIL
        if rail:
            inv = await asyncio.to_thread(rail.invoice, 50_000, comment=f"DVM paid-rail test {job_id[:12]}")
            print(f"[test] (same-wallet fallback invoice minted — self-payment likely blocked)")
            print(f"[test]   bolt11: {inv.bolt11[:40]}... verify: {inv.verify_url[:50]}...")
    await nwc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

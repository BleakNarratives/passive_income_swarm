# nostr_dvm

**Live NIP-90 Data Vending Machine — serves AI jobs over Nostr, earns sats per job.**

## What it does

Listens for NIP-90 job requests on four public relays (kinds 5002 text-generation,
5003 text-summarization), runs them through Groq inference, signs results with
BIP340 schnorr, publishes back to Nostr. Paid jobs carry an `amount` tag in
millisats and are logged separately for earnings audit.

## Layout

```
dvm_worker.py      THE WORKER — everything live lives here
bin/               legacy Moto-era skeletons (superseded, kept for reference)
logs/jobs.jsonl    job ledger — every request, outcome, latency, msats
```

## Operations

```bash
./run_paid_worker.sh                   # paid mode (preflight, fail-closed)
python3 -m dashboard.server            # operator console, 127.0.0.1:8787
systemctl --user status dvm-worker     # is it serving?
journalctl --user -u dvm-worker -f     # live tail (jobs + relay OKs)
tail -f logs/jobs.jsonl                # structured job ledger
~/passive_income_swarm-env/bin/python dvm_worker.py   # manual run (foreground)
```

Service: `~/.config/systemd/user/dvm-worker.service` (enabled, Restart=always).
Venv: `~/passive_income_swarm-env` (needs websockets + coincurve + requests).

## Revenue status

| Piece | Status |
|-------|--------|
| Job intake | LIVE — real traffic on public relays |
| Inference | LIVE — Groq free tier, fallback chain |
| Signing/publish | VERIFIED — relays accept our events |
| Payment collection | **LIVE and job-bound** — LNURL-pay; see [PAID_MODE.md](PAID_MODE.md) |
| Settlement ledger | **DURABLE** — `logs/settlements.db`, one invoice per job, single-use claims |

Reputation mode (default) serves unpaid jobs to build relay presence. Flip
`DVM_PAID_ONLY=1` to enable the full pay-to-play loop:

1. One real BOLT11 invoice is minted per job and **bound to that job id**
   (50 sats text-gen, 20 sats summarize, both env-overridable)
2. `payment-required` feedback is published with the bolt11
3. The coinos.io verify endpoint is polled every `DVM_PAYMENT_POLL` seconds,
   with a final confirmation check at the deadline
4. Settled → the job takes a **single-use claim** and is served; preimage recorded
5. Timeout → `payment-timeout` feedback; the same invoice is reused on any retry,
   never re-minted
6. Rail error → **not served**. There is no fall-through.

A requester's `amount` tag is logged as `claimed_amount_msat` and never treated
as payment. Every state lives in `logs/settlements.db`, so a restart or a relay
reconnect cannot double-serve or double-charge.

Env vars: `DVM_PAID_ONLY=1`, `DVM_PAYMENT_TIMEOUT=120`, `DVM_PAYMENT_POLL=3`,
`DVM_FEE_5002_MSAT=50000`, `DVM_FEE_5003_MSAT=20000`.
Payment rail: `payment_rail.py`. Settlement: `settlement.py` + `paid_flow.py`.
Run it guarded: `./run_paid_worker.sh` (refuses to start if it cannot bill).
NWC client: `nwc_client.py` (preserved for future NIP-44 upgrade).

*BleakNarratives // rebuilt live 2026-08-24, payment rail 2026-08-25*

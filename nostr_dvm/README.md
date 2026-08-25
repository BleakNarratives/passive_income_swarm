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
| Payment collection | **LIVE** — LNURL-pay via coinos.io (bleaknarratives@coinos.io) |

Reputation mode (default) serves unpaid jobs to build relay presence. Flip
`DVM_PAID_ONLY=1` to enable the full pay-to-play loop:

1. Worker generates a real BOLT11 invoice (50 sats text-gen, 20 sats summarize)
2. Publishes `payment-required` feedback to Nostr with the bolt11
3. **Polls the coinos.io verify endpoint** every `DVM_PAYMENT_POLL` seconds
4. If paid within `DVM_PAYMENT_TIMEOUT` seconds → serves the job, logs preimage
5. If timeout → publishes `payment-timeout` feedback, skips the job

Env vars: `DVM_PAID_ONLY=1`, `DVM_PAYMENT_TIMEOUT=120`, `DVM_PAYMENT_POLL=3`.
Payment rail: `payment_rail.py`. NWC client: `nwc_client.py` (preserved for
future NIP-44 upgrade).

*BleakNarratives // rebuilt live 2026-08-24, payment rail 2026-08-25*

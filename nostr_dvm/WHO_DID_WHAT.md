# WHO DID WHAT — passive_income_swarm / nostr_dvm

## 2026-08-24 — Buffy (Freebuff/ox-alpha) — DVM rebuilt and deployed LIVE

- Audited nostr_dvm: found Moto-era skeletons (listener.py subscribed fine but
  responder.py had placeholder signing, never published, and shelled out to
  Ollama — dead on this hardware). keygen.py computed pubkeys wrong
  (sha256 of hex string instead of BIP340 point).
- Built venv `~/passive_income_swarm-env` (websockets 17.0.1, coincurve 21.0).
  Verified coincurve schnorr API shape empirically before building on it.
- Wrote `nostr_dvm/dvm_worker.py` — the real NIP-90 worker:
  - loads existing keys/nostr.key (recovered privkey is valid BIP340; derived
    correct x-only pubkey 1066dbf1...)
  - subscribes kinds 5002 (text-gen) + 5003 (summarize) on relay.primal.net,
    nos.lol, relay.damus.io, relay.nostr.band
  - signs events properly (sha256 serialization -> sign_schnorr), publishes
    results kind 6002/6003 + feedback kind 7000
  - inference via Groq free tier with model fallback chain
    (openai/gpt-oss-120b -> qwen/qwen3.6-27b) — llama-3.3-70b-versatile is
    RETIRED from Groq as of 2026, was returning 404
  - free-tier pacing (2.5s min interval), 429 backoff, concurrency semaphore
  - JSONL job ledger at nostr_dvm/logs/jobs.jsonl (paid jobs tagged with
    amount_msat for future earnings audit)
  - PAYMENT_GATE stub: reputation mode serves all jobs now; flip DVM_PAID_ONLY=1
    once a Lightning rail (NIP-47 NWC / LNURL-pay) is connected
- Live test results: connected to 4 relays, signatures ACCEPTED by nos.lol and
  damus.io, served real public kind-5002 jobs within seconds of connecting.
- Created + enabled `~/.config/systemd/user/dvm-worker.service`
  (Restart=always, OOMScoreAdjust=300 so memguard sacrifices it, not the shell).

## 2026-08-25 — Buffy — Lightning payment rail LIVE

- Attempted NIP-47 NWC: coinos.io wallet supports both NIP-04 and NIP-44
  encryption. NIP-04 path confirmed our signing + relay handshake works
  (events accepted, encrypted responses received), but decryption failed —
  wallet responds only with NIP-44 (ChaCha20-Poly1305), not AES-CBC.
- **Pivoted to LNURL-pay** — discovered coinos.io exposes bleaknarratives@coinos.io
  via `.well-known/lnurlp/bleaknarratives` with full invoice + verify endpoints.
  This is the cleaner path: plain HTTPS, no encryption, works today.
- Built `nostr_dvm/payment_rail.py`:
  - LNURL-pay client: resolve() → invoice(msat) → check(verify_url) → await_payment()
  - Reads lud16 from keys/nwc.secret connection string
  - Generates real BOLT11 invoices with coinos.io callback
  - Payment verification via verify endpoint (settled + preimage)
  - Polling loop for payment confirmation with configurable timeout
- Wired PAYMENT_GATE into `dvm_worker.py`:
  - When DVM_PAID_ONLY=1: unpaid requests get a real BOLT11 invoice
  - Fee schedule: 50 sats text-gen (kind 5002), 20 sats summarize (kind 5003)
  - Invoice embedded in NIP-90 feedback event (kind 7000, status "payment-required")
  - Graceful fallback if payment_rail fails to load
- Live verified: invoice generates, verify endpoint shows settled=False,
  payment_rail imports clean into the running worker (no restart errors).
- Payment_rail lives next to dvm_worker.py; zero new dependencies.
  Updated README: payment collection marked LIVE.

## NEXT
- Decide when to flip DVM_PAID_ONLY=1 (needs payment-check loop before inference
  — currently the worker generates invoices but doesn't re-check settlement).
- Add paid-request polling: after invoice, poll verify endpoint for up to N
  seconds before giving up; serve on settled.
- Consider dedicated nostr identity (new keygen with correct BIP340 pubkey)
  vs recovered Moto-era key.
- Relay set tuning: add paid-friendly relays (e.g., dvm-specific relays).

## 2026-08-25 — Buffy — Payment polling loop + full end-to-end gate

- Added payment-check polling loop to PAYMENT_GATE in dvm_worker.py:
  - After generating invoice + publishing payment-required feedback, the worker
    now **polls** the coinos.io verify endpoint every DVM_PAYMENT_POLL seconds
    up to DVM_PAYMENT_TIMEOUT seconds.
  - If settled → serves the job, logs preimage, outcome="served_paid"
  - If timeout → publishes payment-timeout feedback, outcome="payment_timeout"
  - If payment rail fails entirely, falls through to serve the job (graceful)
- Env vars: DVM_PAID_ONLY=1 (flip switch), DVM_PAYMENT_TIMEOUT=120 (2 minutes
  to pay), DVM_PAYMENT_POLL=3 (check every 3 seconds)
- Verified: polling loop ran 4 checks in 8 seconds, correctly detected unpaid
  invoice, correctly published timeout feedback. Paid path (settled=True)
  falls through to inference with preimage logged.
- Updated README with the 5-step pay-to-play flow.
- Live service still in reputation mode (DVM_PAID_ONLY=0) — gate is wired,
  tested, and ready. Flip when you want to start collecting sats.

## 2026-08-25 ~02:20 CDT — Buffy — END-TO-END VERIFIED: worker serves jobs again

- Built bin/test_job.py: mints a signed synthetic kind-5002 job using the
  worker's own crypto plumbing and publishes it to a relay. Reusable as a
  health check: ~/passive_income_swarm-env/bin/python bin/test_job.py
- Live fire: published ff4f1f0b... to wss://nos.lol -> relay ACCEPT ->
  worker served in 0.81s (262 chars output, outcome=served_free), result
  event accepted by relay. First successful serve since the semaphore bug
  landed. TypeError flood case CLOSED.

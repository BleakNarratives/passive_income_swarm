# Paid mode

How the DVM takes money, and what it refuses to do.

## Run

```sh
./nostr_dvm/run_paid_worker.sh          # preflight, then serve paid jobs
python3 -m dashboard.server             # watch it at http://127.0.0.1:8787
```

`run_paid_worker.sh` sets `DVM_PAID_ONLY=1` and runs `paid_mode_guard.py`, which
**refuses to start** unless the LNURL rail initialised, `GROQ_API_KEY` is present
and the settlement ledger is writable. There is no "start anyway" flag: serving
work you cannot bill is the failure this exists to prevent.

## The flow for one job

1. A kind 5002/5003 request arrives.
2. The gate looks the job id up in the settlement ledger.
   * **already served** → re-publish the stored answer. No invoice, no inference, no charge.
   * **already invoiced** → re-publish *that* BOLT11 and poll it again.
   * **new** → mint one invoice and bind it to this job id.
3. Publish `payment-required` (kind 7000) carrying the BOLT11 and the fee.
4. Poll the LNURL verify endpoint until settlement or timeout. One final check
   runs at the deadline so a payment that lands on the boundary is not dropped.
5. On settlement, take the single-use claim (`settled → serving`).
6. Run inference. On success: publish the result and mark served. On failure:
   release the claim, keep the money recorded, publish an error.

## What changed, and why it matters

| Before | Now |
|---|---|
| `amount` tag on the request counted as paid | the tag is logged as `claimed_amount_msat` and never authorises anything |
| rail exception → "fall through to serve" | rail exception → `payment_rail_error`, no inference |
| global settlement-credit counter | per-job claim, single use, `BEGIN IMMEDIATE` |
| in-memory only | WAL SQLite at `nostr_dvm/logs/settlements.db` |
| a paid-but-failed job vanished | it stays in the ledger as paid-and-undelivered and shows up on the console |
| new invoice on every redelivery | exactly one invoice per job id, ever |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DVM_PAID_ONLY` | `0` | forced to `1` by the launcher |
| `DVM_FEE_5002_MSAT` | `50000` | text generation, 50 sats |
| `DVM_FEE_5003_MSAT` | `20000` | summarization, 20 sats |
| `DVM_PAYMENT_TIMEOUT` | `120` | seconds to wait for sats |
| `DVM_PAYMENT_POLL` | `3` | seconds between verify checks |
| `DVM_MAX_INFLIGHT` | `16` | global concurrency |
| `DVM_MAX_PER_PUBKEY` | `2` | per-requester concurrency |

## Verify it yourself

```sh
bash ./run_tests.sh                       # 220 tests, incl. 41 on the money path
swarmctl breaker status
python3 -c "from nostr_dvm.settlement import SettlementLedger as S; \
l=S('nostr_dvm/logs/settlements.db'); print(l.counters()); print(l.integrity()); l.close()"
```

The console's **integrity** panel runs those same invariant queries against the
live database, so you can see the guarantee instead of trusting this file.

## Honest limits

* This makes billing fail closed. It does not create buyers, guarantee relay
  traffic, or promise income.
* One invoice per job is deliberate. A requester who never pays cannot be charged
  twice, and an expired invoice is reused rather than re-minted.
* If inference fails after payment, the money is recorded and the job is retried
  on redelivery. Refunds are a manual, human decision — nothing here silently
  refunds or writes off a debt.
* Do not run the free-mode worker and the paid-mode worker with the same signing
  key at the same time.

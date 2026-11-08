# HANDOFF.md — swarm control plane + paid rail

**Status: 449/449 tests pass (`bash ./run_tests.sh`).**

This file replaces an earlier handoff that claimed "complete and verified" while
the money path was unsound. Two things were true at once: the control plane was
well built and well tested, and the paid execution path could serve unpaid work.
Both are recorded here so the next agent does not have to rediscover them.

## Current surface (2026-11-08)

Since the paid rail and connections work, four operator surfaces landed and are
tested:

| Surface | Where | What it does |
| --- | --- | --- |
| Schedule health | `swarm/scheduler.py`, `swarmctl cron status\|tune\|install` | parses the cron block it installed, reports silent/starved/thrashing schedules, tunes with a deadband and dwell |
| One-line install | `tools/install.sh` | clone or fast-forward, then bootstrap; safe to re-run |
| Portal | `tools/portal.sh` | one bounded block in the shell: location, health dot, agent counts; no animation, no background work |
| Capability evidence | `swarm/capabilities.py`, `swarmctl capabilities` | states each capability with dated, machine-scoped evidence; imports another list upward only |

`docs/CAPABILITIES.md` covers the evidence model. `docs/SYNTAX.md` §11 preserves
the portal, NAT and coordination decisions.

**`swarmctl ps` now draws those lines.** It reads the process table and the
ledger and reports each entity as declared, available, active and recently
observed. Measured in this workspace: **34 declared, 18 available, 4 n/a
(personas), 12 unavailable** (6 entrypoints absent, 6 subagents with none), **0
active, 0 observed**. That is the honest state — the roster is not a fleet, and
no process was invented to make the table look busy.

**Next:** whichever capability or entity the operator actually needs running.
No new architecture before that.

## The paid rail (2026-11-08)

The prior paid path had three defects that made it unsafe with real money:

1. A requester-supplied `amount` tag was treated as proof of payment.
2. Payment-rail exceptions "fell through to serve" — a rail failure is not a payment.
3. Settlement was a **global in-memory credit counter**, so settling a 20-sat
   job could unlock a 50-sat job, and a restart forgot the customer had paid.

It is now a durable, job-bound ledger with a single-use claim.

| Layer | Module | Responsibility |
|---|---|---|
| Money state | `nostr_dvm/settlement.py` | SQLite ledger: one invoice per job, BOLT11 payment-hash binding, single-use `claim()`, idempotent replay, live invariant checks |
| Money logic | `nostr_dvm/paid_flow.py` | The paid job state machine. Fail-closed, no third-party imports, injectable clock/sleep |
| Worker | `nostr_dvm/dvm_worker.py` | Delegates every paid-mode job to `paid_flow`; fails closed at boot if the rail is absent |
| Launcher | `nostr_dvm/paid_mode_guard.py` | Preflight, then run. No monkey-patching |
| Console | `dashboard/` | Live operator console (stdlib HTTP + SSE), read-only by default |

### Invariants now enforced and tested

* **I1 job binding** — a settlement authorises exactly the job it names.
* **I2 no invoice reuse** — a verify URL or BOLT11 payment hash may appear in one row only.
* **I3 single use** — `claim()` is `BEGIN IMMEDIATE`; a duplicate relay delivery loses the race.
* **I4 idempotent replay** — a served job returns its cached output; no second inference, no second charge.
* **I5 durable** — WAL SQLite; restart/reconnect cannot resurrect a spent invoice.
* **I6 visible failure** — an inference failure releases the claim, keeps the money, and lands on the "paid, undelivered" surface.

### Verified here

`tests/test_settlement.py` (24) and `tests/test_paid_flow.py` (17) cover: unpaid
rejection, `amount`-tag rejection, invoice reuse across jobs, rail failure at
invoice time, rail failure at verify time, settled payment for the matching job,
cross-job replay, duplicate delivery, concurrent duplicate claim, worker restart
mid-job, reconnect with settled-but-unserved money, inference failure then retry,
empty completion, payload tampering, and preimage masking.

### NOT verified here

* No live relay, Nostr signing or Groq call: `coincurve` and `websockets` are not
  installed in this workspace, so `dvm_worker.py` itself is exercised only by
  import-free tests. The logic it calls is fully covered.
* No real sat has been paid through this path in this workspace. `coinos.io` was
  never contacted. Settlement is proven against a fake rail, not a wallet.
* The running systemd unit still points at whatever it pointed at before. Flipping
  it to `run_paid_worker.sh` is the operator's call.

## Connections (2026-11-08)

`cabinet.json` declared eleven capabilities (`llm.generate`, `pay.lnurl`,
`market.search`, `repo.gh`, …) that nothing verified. A declaration is not a
connection, so `swarm/connections.py` now binds each capability to a provider
with a probe and **fails the audit when a declared capability has no connection**.

```sh
swarmctl connections                 # offline coverage + orphans
swarmctl connections --probe --save  # live probes, cached for the console
```

Ten connections cover all eleven capabilities. Live-probed in this workspace:
relays 3/3 answer TCP 443, coinos LNURL resolved (165ms), local embedder, fs,
shell, ledger and stdlib HTTP all `live`; GitHub `degraded` (unauthenticated,
60 req/h); Groq and Webz.io `unconfigured` because no credential exists here.
`llm.embed` is now a real deterministic hashing embedder instead of a promise.

Probes are `urllib`/`socket`/`subprocess` only. They never raise, and a report
never carries a credential value — only the name of the missing variable. There
is a regression test for that.

Also fixed: the worker's relay dedup set was wiped entirely on overflow, which
could re-serve old events on a busy relay. It now evicts one id at a time.

## Readiness (2026-11-08)

`MRD.txt` is the human-only list. `swarmctl readiness` is the same list, executable:
every money-path precondition, each tagged `human` or `machine`, with the exact
command that clears it. It exits non-zero until the path is actually clear.

```sh
swarmctl readiness          # full check, including relays
swarmctl readiness --fast   # no network
```

It catches one thing a checklist cannot: `keys/nwc.secret` is hashed and compared
against a pinned fingerprint of the version that was committed publicly. A
rotation that was forgotten reads as `blocked — still the credential that was
public since 2026-04-30` instead of silently passing. The credential itself is
never printed, stored or transmitted; only the fingerprint is kept.

`market.search` no longer depends on a paid index. `swarm/market_search.py` queries
Hacker News (Algolia) and SEC EDGAR full-text — both keyless, both verified
answering 200 — and `public_search` is registered as a `market.search` connection,
so the capability reads `live` with no credential configured at all:

```sh
swarmctl market --query "debt covenant" --json
```

`requirements.txt` now declares the two non-stdlib runtime dependencies
(`coincurve`, `websockets`). Everything else in this repo, including the full test
suite, is stdlib.

## Security

`keys/nwc.secret` was tracked in a **public** repository since 2026-04-30. It is
a `nostr+walletconnect://` URI containing a live `secret=`. `.gitignore` listed
it, which does nothing for an already-tracked file. See **`SECURITY.md`** for the
rotation and history-purge steps. The file has been removed from the index
(content preserved on disk); the wallet connection should be treated as
compromised until rotated.

## Open items

1. Rotate the NWC connection in coinos and purge the path from git history
   (`SECURITY.md`).
2. ~~Wire `core_framework` through the five reconciliation hooks.~~ Done — the
   checkout was read and `swarmctl hooks --core-root <path>` now reports the
   truth. It found a real bug: the anti-lobotomy guard parsed
   `{"modules": ...}` while `core_framework` writes `{"records": ...}`, so
   against the real file it saw **zero signal for every module** and would have
   approved a graft that discarded the loser's pheromones. Both shapes are read
   now, pinned by `tests/test_hooks.py`. Two hooks (`graft_guard`,
   `scout_registry`) name files that were never written; two (`domino_preflight`,
   `tests`) still need a caller in the other repository.
3. Point the systemd unit at `run_paid_worker.sh` when paid mode is actually wanted,
   and set `DVM_FEE_*` deliberately before advertising prices.

*BleakNarratives // handoff*

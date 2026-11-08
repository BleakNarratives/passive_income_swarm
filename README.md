# Passive Income Swarm

**FastAPI gateway for automated income generation — Mrs. Higgins, Thoth, and swarm agents.**

A microservices architecture that routes tasks to specialized agents for automated income generation: content creation, market analysis, lead generation, and service delivery.

---

## Architecture

```
api.py              FastAPI gateway — routes tasks to agents
arena/              Agent marketplace and task dispatch
analysis/           Market analysis and opportunity detection
control/            Swarm control plane
curation/           Content curation pipeline
dna_mutation/       Agent evolution and optimization
```

## Quick Start

```bash
pip install fastapi uvicorn
python api.py
# API runs on http://localhost:8000
```

## Swarm Control Plane

The ecosystem — every daemon, scout, agent, sub-agent and persona — is
organised by the control plane in `swarm/` and described by one editable
manifest, `swarm/cabinet.json`. Each occupant has a slot (`B3`); you pull the
slot to run it. Everything writes to one crash-safe ledger; scouts mine that
ledger to grow self-improving skills, and a predictive engine turns it into
forecasts. The core is standard-library only, so it runs on the box that runs
the income.

```bash
bash ./swarmctl doctor      # validate manifest + ledger + skills
bash ./swarmctl connections # capability coverage; --probe to verify live
bash ./swarmctl hooks --core-root ../core_framework  # what is actually wired
bash ./swarmctl readiness   # money-path preconditions + who can clear each
bash ./swarmctl market --query "debt covenant"  # keyless demand-signal search
bash ./swarmctl cabinet     # print the cabinet (slots)
bash ./swarmctl pull B3     # dispense Thoth
bash ./swarmctl demo        # end-to-end smoke run
bash ./run_tests.sh         # 248 stdlib unit tests (45 on the money path)
```

See **[docs/SWARM.md](docs/SWARM.md)** for the architecture: the cabinet, the
skill mastery/certification ladder that replaces SKILL.md and MCP hoarding, the
scout loop, the forecast engine and its roadmap, and how to extend it.

### Integration relays

Three contracts wire the swarm to `core_framework` and the Whorl Bus:

- **Relay 1** — every emitter speaks a validated payload envelope with a
  `content_sha256` (P2) digest; ad-hoc JSON is rejected (`swarm/contracts.py`).
- **Relay 2** — telemetry and pheromones go to one `WhorlBus` (`bus.jsonl`),
  not scattered sidecars, with a decaying pheromone attention layer
  (`swarm/whorl.py`).
- **Relay 3** — self-improvement is gated to live lineage only: updates are
  accepted from `src/skill/` and `projects/agents/scouts/`, refused from
  `._archive/` and `UNPACKED/` (`swarm/lineage.py`, enforced in `swarm/ingest.py`).

Scouts are rewarded super-linearly for **novel, rare and marketable** intel via
`swarm/incentives.py`. See **[docs/INTEGRATION.md](docs/INTEGRATION.md)** for the
exact schema, the single `core_framework` adapter boundary, and the
reconciliation checklist.

### Janus Guard

Three protections on top of the relays, all in stdlib Python:

- **Anti-lobotomy** — a winner/loser code graft runs a pre-graft state
  inspection and merges the loser's pheromone trails into the winner *before*
  anything is archived, so historical signal is never sacrificed for clean
  syntax (`swarm/graft.py`).
- **Hard-lock breaker** — failed schema validations and unverified signatures
  are counted; past threshold the bus flushes its buffer and locks into
  `STRICT_INGEST_ONLY` until a manual preflight passes (`swarm/breaker.py`).
- **Dynamic auto-discovery** — scripts register their `content_sha256` on first
  clean run; unregistered execution is tagged `FERAL_UNREGISTERED` and
  quarantined (`swarm/discovery.py`).

Scouts and agents are **polyglot** (Python, Elixir, Node, Deno, C++ and more) —
the launcher table and byte-level hashing make the control plane
language-agnostic. See **[docs/JANUS.md](docs/JANUS.md)**.

### Connections

`swarm/connections.py` binds every capability the cabinet declares to a real
provider with a live probe, so a manifest can no longer claim `market.search`
while nothing behind it can search:

```bash
swarmctl connections                 # coverage + orphans, no network
swarmctl connections --probe --save  # live reachability, cached for the console
```

The exit code is `1` when a declared capability is unbound, or when a configured
connection is unreachable. `doctor` reports orphans too. Probes are plain
`urllib`/`socket`/`subprocess` — no SDK, no `pip install` — and a report never
contains a credential value, only the name of a missing variable. See
**[docs/CONNECTIONS.md](docs/CONNECTIONS.md)**.

## Paid rail (NIP-90 DVM)

`nostr_dvm/` serves AI jobs over Nostr and takes sats for them. The money path
is durable, **job-bound** and fail-closed:

- one invoice per job, ever — a retry re-publishes the same BOLT11 instead of
  charging again;
- inference opens only after the LNURL verify endpoint reports settlement *for
  that job id* — a request's `amount` tag is a claim, never a receipt;
- a payment-rail error means the job is **not** served (the old gate fell
  through and served it anyway);
- a single-use claim (`BEGIN IMMEDIATE`) means a duplicate relay delivery cannot
  run inference twice, and a restart cannot resurrect a spent invoice;
- a paid-but-failed job stays visible as *paid, undelivered* rather than
  vanishing.

```bash
./nostr_dvm/run_paid_worker.sh   # preflight, then serve paid jobs
python3 -m dashboard.server      # operator console on 127.0.0.1:8787
```

See **[nostr_dvm/PAID_MODE.md](nostr_dvm/PAID_MODE.md)** for the flow and
**[SECURITY.md](SECURITY.md)** for the credential remediation this repository
still needs.

## Operator console

`dashboard/` is a live console for the swarm and its money: stdlib HTTP plus
Server-Sent Events, no build step, no dependencies. Money funnel with counted
drop-off, an invoice wall with depleting countdowns, a daily-settled chart with a
backtested 7-day projection (model and RMSE shown), the cabinet as a 6 × 6 grid,
the job stream, pheromone trails, and an **integrity** panel that runs the money
invariants as SQL against the live ledger.

Read-only by default; mutating actions require `DASHBOARD_TOKEN`. See
**[docs/DASHBOARD.md](docs/DASHBOARD.md)**.

## Stack

- **FastAPI** — async gateway
- **Pydantic** — request/response models
- **Swarm agents** — Mrs. Higgins (content), Thoth (analysis)
- **Control plane** — `swarm/` (stdlib only): registry, vending machine, skills, scouts, forecast, supervisor

## API Key Management

Store keys in `.env` (DO NOT COMMIT).

---

*BleakNarratives // 2026*

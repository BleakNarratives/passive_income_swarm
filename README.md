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
bash ./swarmctl cabinet     # print the cabinet (slots)
bash ./swarmctl pull B3     # dispense Thoth
bash ./swarmctl demo        # end-to-end smoke run
bash ./run_tests.sh         # 153 stdlib unit tests
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

## Stack

- **FastAPI** — async gateway
- **Pydantic** — request/response models
- **Swarm agents** — Mrs. Higgins (content), Thoth (analysis)
- **Control plane** — `swarm/` (stdlib only): registry, vending machine, skills, scouts, forecast, supervisor

## API Key Management

Store keys in `.env` (DO NOT COMMIT).

---

*BleakNarratives // 2026*

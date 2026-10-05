# SWARM.md — the control plane

How the daemons, scouts, agents, sub-agents and personas in this repo are
organised into one operable system, and how to grow it without it collapsing
into a pile of SKILL.md files and one-off tool connectors.

---

## 1. The idea in one paragraph

The ecosystem is a **cabinet**. Every occupant — a daemon, a scout, an agent, a
sub-agent, a persona — has a glass-front **slot** (`B3`), a kind, a role, the
skills it uses and the capability ports it needs. A single manifest describes
all of them. You adjust the machine by editing that manifest, not by hunting
through code. You run something by **pulling its slot**, which stages a durable
job ticket. Everything that happens is written to one **ledger**. **Scouts**
read the ledger, **skills** improve themselves against it, the **supervisor**
keeps the daemons upright, and the **predictive engine** turns the ledger into
forecasts. That is the whole machine.

```
                         ┌──────────────────────────────────────────┐
   operator ──pull B3──▶ │  VendingMachine   (swarm/vending.py)      │
                         └───────────────┬──────────────────────────┘
                                         │ stages ticket
                                         ▼
                  control/pending/*.job.json  ──claim──▶  worker  ──complete──▶ control/outbox
                                         │                                   │
                                         ▼                                   ▼
                         ┌───────────────────────────────────────────────────────┐
                         │            LEDGER  (swarm/ledger.py)  logs/swarm.jsonl │
                         │   dispense · claim · complete · skill_run · health · … │
                         └───────┬───────────────┬───────────────┬───────────────┘
                                 │               │               │
                    ┌────────────▼───┐  ┌────────▼───────┐  ┌────▼──────────────┐
                    │  SkillStore    │  │ ScoutEngine    │  │ PredictiveEngine  │
                    │ self-improving │◀─│ discover /     │  │ models · backtest │
                    │ mastery · cert │  │ evaluate/audit │  │ registry · anomaly│
                    └────────────────┘  └────────────────┘  └───────────────────┘
                                 │
                    ┌────────────▼────────────────────────────┐
                    │  Supervisor (breakers · backoff)        │
                    └─────────────────────────────────────────┘
```

---

## 2. Module map

| Module | Responsibility |
|---|---|
| `swarm/ledger.py` | Append-only, fsync-durable JSONL event log. The **only** source of truth. |
| `swarm/entities.py` | `Entity` — one shape for daemon / scout / agent / sub-agent / persona. |
| `swarm/registry.py` | Loads `swarm/cabinet.json`, validates it, assigns slot addresses. |
| `swarm/cabinet.json` | The manifest. Edit this to add/disable/re-slot occupants. |
| `swarm/vending.py` | The control plane: render the cabinet, pull a slot, stage a ticket. |
| `swarm/skills.py` | Self-improving skills: EWMA scoring, mastery ladder, certification gate. |
| `swarm/scouts.py` | Discovery / evaluation / capability audit. Findings, not silent mutations. |
| `swarm/forecast.py` | Forecasting models, metrics, walk-forward backtest, model registry, anomaly detection. |
| `swarm/supervisor.py` | Health probes, restart backoff, per-daemon circuit breakers. |
| `swarm/cli.py` | `swarmctl` — one command surface over all of it. |
| `swarm/api.py` | Optional FastAPI surface (imports cleanly without FastAPI installed). |

Zero third-party dependencies in the core. That is deliberate: the box that runs
the income has to boot the brain.

---

## 3. Occupant kinds

| Kind | Runs? | Meaning | Examples |
|---|---|---|---|
| `daemon` | supervised | long-running process | `dvm_worker`, `payment_rail`, `supervisor` |
| `scout` | yes | goes and looks; proposes | `truthsleuth`, `dominance_scout`, `augur` |
| `agent` | yes | persona-driven worker | `mrs_higgins`, `thoth`, `molt` |
| `subagent` | yes (scoped) | delegated child of an agent | `thoth_forecast`, `molt_pricing` |
| `persona` | **no** | reusable voice bound to agents | `persona_higgins`, `persona_bleak` |

Slot codes: columns are letters, rows are numbers (`B3`). `swarmctl cabinet`
prints the glass front; `swarmctl cabinet --stock` lists every occupant.

---

## 4. Skills: replacing SKILL.md and MCP hoarding

### The problem with SKILL.md
Prose skills describe intent and can never report outcome. You cannot rank them,
retire them, or prove one works.

### The problem with MCP hoarding
Every agent re-declares its own tool connectors. Providers drift, credentials
multiply, and no one knows which agent can reach what.

### What replaces both
A skill here is a structured record:

- **executable steps** (`steps`), declared I/O (`inputs` / `outputs`);
- **capability ports** — the *only* place an external tool is named
  (`llm.generate`, `net.nostr`, `pay.lnurl`, …), resolved once by the registry
  and shared by every skill that declares them;
- **a living score** — an EWMA over per-run rewards in `[0, 1]`;
- **provenance** — which scout discovered it, from what evidence.

### The mastery ladder

```
NOVICE → APPRENTICE → ADEPT → EXPERT → MASTER → CERTIFIED
```

Promotion is derived from `runs`, `score` and `stdev` (see `LADDER` in
`swarm/skills.py`). `CERTIFIED` is **not** a rung you climb by volume — it is a
separate gate (`MasteryGate`) requiring minimum volume, minimum score and
stability, and it records its evidence. "It works" is a claim; certification is
a receipt.

### The loop

1. A scout finds repeated behaviour in the ledger worth formalising and
   `propose`s an *emergent* skill.
2. Runs report rewards; the score moves; the level is re-derived.
3. A mastered skill is sent to the gate and, if it passes, certified.
4. A skill whose score rots gets flagged and, on review, retired.

Scouts **recommend**; nothing is applied unless the operator passes `--apply` or
acts. That separation is what keeps an autonomous loop from mutating production
at 3am.

---

## 5. The predictive engine

`swarm/forecast.py` is the analytical core the rest of the machine feeds.

**Models** — naive, seasonal-naive, moving average, EWMA, Holt's damped linear,
OLS linear trend, and a ridge autoregression over lag features. All implement
`Forecaster.forecast(series, horizon)`.

**Evaluation** — MAE, RMSE, MAPE, sMAPE, R², and expanding-window
walk-forward backtesting. Model selection is the *measured* winner, never a
guess.

**Registry** — every model's backtest score is persisted under
`data/models/registry.json`, so "why this model?" has an auditable answer.

**Anomalies** — trailing-window z-score detection; a flat baseline makes any
deviation maximally anomalous rather than hidden.

**Bridge** — `PredictiveEngine.series_from_events` projects the ledger into a
series (revenue per completion, reward trajectory, event counts).

### Roadmap (interface is frozen so these drop in without touching callers)
- exogenous-driver features (relay health, time-of-day demand, price);
- probabilistic intervals (quantile / conformal) instead of point forecasts;
- a learned sequence model behind the same `Forecaster` interface;
- per-entity model routing (Augur picks the model per opportunity class).

---

## 6. Supervision — taking a beating

Per daemon: consecutive failures, restart count, last success, and a
`CircuitBreaker`. The breaker opens after N consecutive failures, stops hammering
the failing dependency, and half-opens after a cooldown to send a single probe.
A failed half-open probe re-opens; a success closes. Restart backoff is
exponential and capped. State persists to `logs/supervisor_state.json`.

Probes and restarters are **injected**, so supervision is unit-testable and
never restarts production during a test.

---

## 7. Operating it

```bash
bash ./swarmctl doctor                     # validate manifest + ledger + skills
bash ./swarmctl cabinet --stock            # the glass front
bash ./swarmctl pull B3                    # dispense Thoth
bash ./swarmctl pull augur '{"horizon":24}' # dispense with a payload
bash ./swarmctl skills leaderboard
bash ./swarmctl scout run                  # discover / evaluate / audit (recommend)
bash ./swarmctl scout run --apply          # ...and register the emergent skills
bash ./swarmctl forecast reward --horizon 12
bash ./swarmctl supervise --ticks 3
bash ./swarmctl demo                       # end-to-end smoke
bash ./run_tests.sh                        # stdlib unittest, no pytest
```

Long-running daemons should be managed by systemd user units (the DVM worker
already is). The supervisor here is the in-swarm health/restart brain; the two
compose.

---

## 8. Extending it

**Add an occupant** — append to `swarm/cabinet.json` (assign a `slot` or let it
auto-assign), run `swarmctl doctor`.

**Add a capability** — declare it in `cabinet.json → capabilities` with its
provider and required env vars. That single declaration is what every skill
using it inherits; the scout's capability audit will flag anyone exercising an
undeclared capability.

**Add a model** — subclass `Forecaster`, implement `forecast`, add it to
`DEFAULT_MODELS`. Backtesting and selection pick it up automatically.

**Add a skill** — `swarmctl skills seed`-style registration, or let a scout
`propose` it from evidence.

---

## 9. Honest status

**Built and tested (179 stdlib unit tests):** ledger durability, registry
validation and slot addressing, vending/dispense idempotency, the skill
self-improvement loop and certification gate, scout discovery/evaluation/audit,
all forecast models + metrics + backtest + registry + anomaly detection,
supervisor breakers and backoff, the three integration relays (payload
contract + P2 digest, WhorlBus + pheromones, lineage gate + gated ingestion),
the scout incentive model, the Janus Guard (anti-lobotomy graft inspection +
pheromone merge, hard-lock breaker, dynamic scout registry auto-discovery), and
the full CLI/API surface. Polyglot runtimes: Python, Elixir, Node, Deno, C++,
and more via the launcher table or an explicit interpreter.

**Wired but not yet live:** daemons are described in the manifest; actually
supervising the real services under systemd is an operator step. The predictive
engine is a strong classical forecasting core — the learned/probabilistic
extensions are roadmap, not shipped.

*BleakNarratives // swarm control plane v0.1*

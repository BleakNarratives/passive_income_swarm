# JANUS.md — state preservation, hard lock, auto-discovery

The Janus Guard additions: three mechanisms that protect historical signal, fail
hard on contract violations, and keep the scout registry self-maintaining
instead of hand-edited.

> **Status:** `humane_cannibal.py`, `adaptive_memory.json`,
> `SCOUT_REGISTRY.json`, `circuit_breaker.json`, `domino.sh` and any SQLite
> ledgers are **not in this workspace** — they live in `core_framework`. The
> mechanisms below are implemented against the live sources this repo has and
> the exact names/paths you specified. Each section ends with its adapter
> boundary.

---

## Polyglot reality

The swarm is **not** Python-only. Scouts and agents are written in Elixir, JS,
Deno/TS, C++ and more. So nothing in the control plane assumes a language:

- `Entity.runtime` + `Entity.interpreter` describe how to launch anything, and
  `swarm/vending.py` resolves a launcher table:

  | runtime | command |
  |---|---|
  | `python` | `python3 <entry>` |
  | `elixir` / `exs` | `elixir <entry>` |
  | `node` / `js` | `node <entry>` |
  | `deno` | `deno run --allow-all <entry>` |
  | `cpp` / `c` / `binary` | `<entry>` (compiled artifact, no interpreter) |
  | `rust`, `go`, `ruby`, `erlang`, `bun`, `sh` | see `VendingMachine._LAUNCHERS` |
  | *any* + `interpreter` | `<interpreter> <entry>` (explicit override wins) |

- Discovery hashes **bytes** (`sha256` of the file), so a `.exs`, `.js`, `.ts`
  or compiled binary registers and quarantines identically.
- The lineage gate is path-based, so it is language-agnostic.

Six polyglot occupants are already in `swarm/cabinet.json` (`whorl_sensor`
Elixir, `market_watch` Node, `scout_forge` C++, `edge_probe` Deno,
`beam_analyst` Elixir, `wasm_gate` binary).

---

## 1. State preservation over code grafting (Anti-Lobotomy)

> "BEFORE any winner/loser code graft occurs in `humane_cannibal.py`, HC MUST run
> a pre-graft state inspection against `adaptive_memory.json` and SQLite
> ledgers. If the loser holds higher-density pheromone trails or active yield
> statistics than the winner, HC MUST execute `merge_pheromones()` BEFORE
> deprecating or archiving the loser."

**Implemented in** `swarm/graft.py`.

`GraftGuard.execute(winner, loser, graft_fn, ...)` is the enforced path:

1. `pre_graft()` inspects both modules — pheromone density (decayed, from the
   WhorlBus) and yield (incentives + revenue + runs, from the ledger), folding
   in `adaptive_memory.json` if present.
2. If the loser is dominant on any of pheromone density / incentives / revenue,
   `merge_pheromones()` **re-deposits the loser's trails under the winner**
   before `graft_fn` is ever called.
3. Only then does `graft_fn` run. Ledger events: `graft_inspect`,
   `pheromone_merge`, `graft_executed`.

A graft is about syntax; the loser earned signal. Clean code that discards it is
a lobotomy, so the inspection is a precondition, not a suggestion.

```bash
bash ./swarmctl graft inspect --winner new_scan --loser legacy_scan
bash ./swarmctl graft execute --winner new_scan --loser legacy_scan
```

**Adapter boundary:** wire `GraftGuard.execute` into `humane_cannibal.py`'s
deprecate/archive path. `adaptive_memory.json` shape is
`{"modules": {"<mod>": {"pheromone_density", "incentives", "revenue_msat", "runs"}}}`.
If HC's SQLite ledger is the yield source, point `inspect()` at it there.

---

## 2. Hard-lock circuit breaker (Janus Guard)

> "If >3 unverified sidecars or invalid schema payloads attempt to touch
> WhorlBus within a single session window, the bus auto-flushes uncommitted
> buffer states and locks into `STRICT_INGEST_ONLY` mode until a manual
> `domino.sh preflight` passes."

**Implemented in** `swarm/breaker.py`.

- `IngestBreaker` keeps a rolling window (`window_s`) of violations — failed
  schema validations and unverified SHA256 signatures. It trips once the count
  **reaches the threshold** (default 3, so the 3rd failure locks the bus).
- On trip it calls `bus.flush()` first, so staged/uncommitted state is committed
  and never lost, then sets `STRICT_INGEST_ONLY` and persists to
  `circuit_breaker.json`.
- `guarded_publish()` / `guarded_submit_raw()` are the instrumented bus entry
  points that feed the counter.
- In `STRICT_INGEST_ONLY`, `Ingestor` accepts only **registered** emitters;
  anything else is tagged `FERAL_UNREGISTERED`, logged (`feral_emitter`) and
  rejected.
- Nothing unlocks automatically. `IngestBreaker.preflight_pass()` is the manual
  release — the `domino.sh preflight` equivalent.

```bash
bash ./swarmctl breaker status
bash ./swarmctl breaker preflight      # manual release
```

**Adapter boundary:** `domino.sh preflight` should call
`IngestBreaker.preflight_pass()` (or `swarmctl breaker preflight`) once its own
checks pass. If the policy engine lives elsewhere, keep the `circuit_breaker.json`
schema and swap the counter feed.

---

## 3. Dynamic registry auto-discovery

> "Any script executing in `src/skill/` or `projects/agents/scouts/` MUST
> register its `content_sha256` digest and schema version to
> `~/.config/freebuff/SCOUT_REGISTRY.json` upon its first clean run.
> Unregistered execution attempts are automatically tagged `FERAL_UNREGISTERED`
> and routed to `/tmp/quarantine/`."

**Implemented in** `swarm/discovery.py`.

- `ScoutRegistry.record_clean_run(script)` registers the digest + schema on the
  first clean exit. No human edits the registry list.
- `gate_execution(script)` returns `REGISTERED` or tags `FERAL_UNREGISTERED` and
  copies the offender to the quarantine dir (default `/tmp/quarantine/`).
- A changed script has a new digest and is therefore untrusted again — a silent
  edit is a new, unproven organism.

```bash
bash ./swarmctl discovery register projects/agents/scouts/whorl_sensor.exs
bash ./swarmctl discovery check    projects/agents/scouts/whorl_sensor.exs
```

Default registry path is `~/.config/freebuff/SCOUT_REGISTRY.json`;
override with `--registry-path` and `--quarantine-dir`.

**Adapter boundary:** register on clean exit wherever `core_framework` runs a
scout — one `record_clean_run` call at the point the process returns 0.

---

## Reconciliation checklist

1. `humane_cannibal.py` → call `GraftGuard.execute` in the deprecate/archive path.
2. `adaptive_memory.json` → confirm the `modules` shape, or point `inspect()` at
   the SQLite ledger.
3. `domino.sh preflight` → call `IngestBreaker.preflight_pass` on success.
4. `SCOUT_REGISTRY.json` → confirm the path; call `record_clean_run` on clean exit.
5. Re-run `bash ./run_tests.sh` — 179 tests, all stdlib.

### Runnable reconciliation (whorl.py + lineage.py)

Items 3 and 4 are automated so reconciliation is a command, not a code edit.
`swarm/lineage.py::reconcile_with_proofs` and
`swarm/whorl.py::reconcile_bus_path` compare the **active** roots / bus path
against `PROOFS_AND_GATES.md` and the real `bus.jsonl` location:

```bash
bash ./swarmctl reconcile                              # source_absent → exit 0
bash ./swarmctl reconcile --proofs PROOFS_AND_GATES.md # ok / drift (exit 1)
bash ./swarmctl reconcile --expected-bus-path /srv/core/bus.jsonl
```

Both the roots and the bus path are overridable at the CLI, so matching
`core_framework` needs **no source change**:

```bash
bash ./swarmctl --bus-path /srv/core/bus.jsonl \
     --allowed-root src/skill/ --denied-root UNPACKED \
     reconcile --proofs PROOFS_AND_GATES.md
```

Status values: `ok`, `drift` (with the exact differing roots/paths, exit 1), or
`source_absent` (the document is not in this workspace yet, exit 0 — the active
roots are used as-is).

*BleakNarratives // Janus Guard v1*

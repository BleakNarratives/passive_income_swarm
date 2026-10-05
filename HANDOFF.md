# HANDOFF.md — swarm control plane

**Status: complete and verified.** 153/153 tests pass (`bash ./run_tests.sh`),
`compileall` clean, no runtime state left in the repo.

## What landed (all stdlib Python, runs on bare Python 3.10)

| Area | Module | Notes |
|---|---|---|
| Event substrate | `swarm/ledger.py` | append-only, fsync-durable JSONL |
| Occupants | `swarm/entities.py`, `swarm/registry.py`, `swarm/cabinet.json` | 34 occupants, slot-addressed |
| Control plane | `swarm/vending.py` | pull a slot → staged durable ticket |
| Skills | `swarm/skills.py` | EWMA score, mastery ladder, certification gate |
| Scouts | `swarm/scouts.py` | discover / evaluate / capability audit |
| Forecast | `swarm/forecast.py` | 7 models, metrics, walk-forward backtest, registry |
| Supervision | `swarm/supervisor.py` | health, backoff, circuit breakers |
| Relay 1 | `swarm/contracts.py` | envelope + `content_sha256` P2 digest |
| Relay 2 | `swarm/whorl.py` | `bus.jsonl`, pheromone deposit/decay |
| Relay 3 | `swarm/lineage.py`, `swarm/ingest.py` | live-lineage gate + gated ingestion |
| Incentives | `swarm/incentives.py` | novelty / rarity / marketability |
| Anti-lobotomy | `swarm/graft.py` | pre-graft inspection + `merge_pheromones()` |
| Janus Guard | `swarm/breaker.py` | `circuit_breaker.json`, `STRICT_INGEST_ONLY` |
| Auto-discovery | `swarm/discovery.py` | `SCOUT_REGISTRY.json`, `FERAL_UNREGISTERED` |
| CLI / API | `swarm/cli.py` (`swarmctl`), `swarm/api.py` | optional FastAPI |

Docs: `docs/SWARM.md` (architecture), `docs/INTEGRATION.md` (relays),
`docs/JANUS.md` (Janus Guard). Polyglot via the launcher table in
`swarm/vending.py` + `Entity.interpreter`.

## Verified live
`relay` → `contract emit` → `ingest` → `bus pheromones`; `graft inspect/execute`
(merge fires); `breaker` trip → `preflight`; `discovery register/check` (feral
quarantined).

## Open items (need `core_framework`, not in this workspace)
`humane_cannibal.py`, `adaptive_memory.json`, `domino.sh`, SQLite ledgers are
absent. Each has exactly **one** documented integration call in
`docs/JANUS.md` §Reconciliation:
1. Call `GraftGuard.execute` in HC's deprecate/archive path.
2. Call `IngestBreaker.preflight_pass()` when `domino.sh preflight` passes.
3. Call `ScoutRegistry.record_clean_run()` on scout exit 0.
4. Confirm `PROOFS_AND_GATES.md` roots match `ALLOWED_ROOTS`/`DENIED_ROOTS`.
5. Diff `Envelope.to_sidecar()` against the real `ScoutLoader.write_sidecar`.

## Gotchas
- Default `SCOUT_REGISTRY.json` lives at `~/.config/freebuff/`; override with
  `--registry-path` / `--quarantine-dir`.
- Ceiling: `tsc -b --noEmit` is inapplicable (Python repo).
- Nothing committed — Freebuff Changes panel owns delivery.

*BleakNarratives // handoff*

# Operator console

A live view of the swarm and its money, with no build step and no dependencies.

```sh
python3 -m dashboard.server                 # http://127.0.0.1:8787
python3 -m dashboard.server --port 9000     # another port
python3 -m dashboard.server --host 0.0.0.0  # off-loopback: think first
```

Standard library only: `http.server` plus Server-Sent Events. Nothing to
install, no bundler, no `node_modules`, no CDN. It boots on the same bare Python
the daemon runs on.

## What it reads

| Source | Panel |
|---|---|
| `nostr_dvm/logs/settlements.db` | settled sats, funnel, invoice wall, integrity, requesters, projection |
| `nostr_dvm/logs/jobs.jsonl` | job stream and outcome histogram |
| `logs/swarm.jsonl` | event types, per-occupant last-seen |
| `swarm/cabinet.json` | the 6 × 6 cabinet |
| `bus.jsonl` | decaying pheromone trails |
| `circuit_breaker.json` | Janus Guard state |
| `logs/connections.json` | connection status and capability coverage (last probe) |

Reading is **read-only and side-effect free**. The console does not create a
ledger, seed a skill, register a scout or open a database it was not asked to
open; a missing source renders as "not available" with the reason.

## The panels, and what each one is actually proving

**Money funnel.** Requested → invoiced → settled → served, with the drop-off
counted at each edge. A funnel that only shows totals hides the leak; this one
names it (`−14 expired unpaid`).

**Invoice wall.** Every open invoice with a depleting countdown ring, so an
operator can see money in flight rather than a single "pending" number.

**Daily settled + projection.** Fourteen days of settled millisats, then a
seven-day projection from the swarm's own model family (`swarm/forecast.py`).
The model is chosen by expanding-window walk-forward backtest, and the panel
prints the winning model *and its RMSE*. With too little history it refuses to
draw a line and says why. A projection without its error is a decoration.

**Integrity.** Runs the money invariants as SQL against the live ledger: no
invoice bound to two jobs, nothing served without a confirmed settlement, no
settlement ordering violation, every served job has a stored result to replay.
Two further checks — `amount` tags are never trusted, rail errors never serve —
are enforced in code and labelled as such rather than faked as queries.

**Cabinet.** All 36 slots, coloured by occupant kind, marked when that occupant
appears in the swarm ledger.

**Connections / capability coverage.** Every capability the cabinet declares,
rolled up from the providers that actually serve it, with the detail and latency
of the last probe. The console never probes on a timer — it shows the last cached
report and when it was taken, because six providers hit every two seconds is not
monitoring, it is load.

**Pheromone trails / skills / requesters.** Decayed attention on the WhorlBus,
the skill mastery leaderboard, and who is actually paying.

**Command palette.** `⌘K` / `ctrl K`. Jumps panels, refreshes, and runs
allowlisted operator actions.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the console |
| GET | `/api/state` | full snapshot as JSON |
| GET | `/api/stream` | Server-Sent Events, one snapshot every 2s |
| GET | `/api/actions` | what this instance allows, and whether it is writable |
| GET | `/api/job/<event_id>` | the state-transition trail for one job |
| GET | `/healthz` | liveness |
| POST | `/api/action` | run an allowlisted action (token required) |

## Actions and their gate

With no `DASHBOARD_TOKEN`, the console is **read-only** and says so in the
footer. Setting it enables:

| Action | Command |
|---|---|
| `doctor` | `python -m swarm.cli doctor` |
| `breaker_preflight` | `python -m swarm.cli breaker preflight` |
| `seed_skills` | `python -m swarm.cli skills seed` |
| `scout_discover` | `python -m swarm.cli scout discover` |
| `probe_connections` | `python -m swarm.cli connections --probe --save` |
| `export` | write the settlement ledger to `settlements_export.jsonl` |

Actions run as an argument list (never a shell), one at a time, with a 60s
timeout. The console cannot be turned into a remote shell.

```sh
DASHBOARD_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  python3 -m dashboard.server
```

The browser prompts for the token once and keeps it in `localStorage`.

## Security posture

* Binds loopback by default.
* No secret is ever placed in a response: the wallet connection string is
  reduced to a boolean (`rail_configured`), and payment preimages are masked at
  the ledger boundary.
* Mutating operations require the token; the absence of a token is a refusal,
  not a default-allow.

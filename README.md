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

## Syntax (direction, not built)

The longer-term direction is an environment rather than a bigger repo: Syntax
composes the pieces above instead of merging them — a distributed intelligence
layer, specialist agents that delegate and challenge each other, shared memory
through NAT, replaceable models, and a recognisable identity layer. **None of it
is implemented here.** The boundary, the reuse inventory, the public/private
split and the dependency-ordered sequence are recorded in
[docs/SYNTAX.md](docs/SYNTAX.md), and that document is the thing to read before
any feature in this area starts.

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

## Is it safe to charge?

`swarmctl readiness` answers that in six ordered stages, because *the switch is
on* and *this machine can safely take money* are different claims:

| stage | what it needs | owner |
|---|---|---|
| `configured` | a config source on disk, not just shell exports | human |
| `dependencies` | `coincurve`, `websockets` importable | machine |
| `signing_identity` | a key that can sign an event | human |
| `wallet_rotated` | a credential that is **not** the leaked one, mode `600` | human |
| `lifecycle_verified` | the state machine proven, and ideally a real settlement | machine |
| `authorized` | `DVM_PRODUCTION_AUTHORIZED=1`, and stage 5 already passed | human |

`payments_safe` is true only when all six are `ok`. There is also a legacy
`ready` flag meaning "no blocking precondition" — it is deliberately weaker, and
setting `DVM_PAID_ONLY=1` can never make a box safe on its own.

```bash
swarmctl readiness                       # stages, then the granular checks
swarmctl lifecycle self-test             # invoice → paid → served, in-process
swarmctl lifecycle status                # + whether a real payment was ever observed
swarmctl unit audit --live               # does systemd actually run the guard?
```

The lifecycle is split on purpose: the self-test proves the state machine
without a wallet, and `logs/lifecycle-proof.json` (a real payment hash and
preimage) is the only thing that proves the rail. The report prints both and
never conflates them.

`swarmctl unit audit` exists because a guard nobody starts is decoration. It
parses `ExecStart` rather than grepping — `grep run_paid_worker` also matches a
comment, an `Environment=` value, or a log path. `deploy/dvm-worker.service` is
the unit that passes it.

## Terminal console

```bash
swarmctl tui            # curses; no browser, no port, no dependency
swarmctl tui --once     # one frame, for CI or `watch`
```

Same snapshot as the web console, so the two cannot disagree about the money:
stage evidence, sats settled, what is owed to a customer, connections, and the
job stream. Keys: `r` refresh, `s` lifecycle self-test, `p` probe connections,
`q` quit.

## Wiring the console into the swarm

The console's facts used to be visible only to a human looking at a screen.
`swarmctl bridge` projects them onto the WhorlBus as contract-clean envelopes, so
scouts, engines and the ingestor see the same numbers without scraping the
dashboard.

```bash
swarmctl bridge --dry-run     # what would publish, and why
swarmctl bridge               # publish only what changed
swarmctl bridge --watch       # keep publishing on an interval
```

Signals published today:

| signal | kind | confidence |
|---|---|---|
| `money.settled` | telemetry | 1.0 |
| `money.owed` | intel | 1.0 when a customer paid and got nothing |
| `ledger.integrity` | telemetry | 1.0, with the failing check ids |
| `worker.mode` | telemetry | 1.0, with its config source |
| `readiness.gate` | telemetry | 1.0, with the blocked stages |
| `connections.coverage` | telemetry | 0.8 |
| `forecast.projection` | telemetry | 0.55 — it is a guess with a measured error |
| `jobs.histogram` | telemetry | 0.9 |
| `pheromone.money_owed` | pheromone | scales with the backlog |

Three things make it safe to run on a timer:

- **Idempotent.** The digest is computed over facts only, so a console refreshing
every few seconds does not flood the bus. Re-running with nothing changed
publishes nothing; `--force` republishes deliberately.
- **Contract-clean.** Every signal goes through `Envelope.build`, so it carries a
valid P2 digest and cannot be the ad-hoc JSON Relay 1 rejects.
- **Secret-free.** No signal contains a credential, a preimage or a key — there is
 a test that writes one and greps the bus and the export for it.

Anything that cannot consume a bus gets the same facts as a file:
`logs/console-telemetry.json` (`console.telemetry/1`), written on every publish.

```bash
swarmctl bridge                       # publish + export
swarmctl --bus-path bus.jsonl ingest  # the rest of the swarm consumes it
```

## Cron health, and a schedule that settles

Cron fails silently: a typo'd command, a missing interpreter, an expired
credential — all of it looks like an empty log and a growing feeling that
something is wrong. `swarmctl cron status` reads the installed block, every
per-job artifact and the last line of each, and says which jobs are installed,
stale, failing or never ran.

```bash
swarmctl cron status              # what is scheduled, what ran, what broke
swarmctl cron tune                # the cadence this climate calls for
swarmctl cron tune --apply        # ..and install it
swarmctl cron tune --market "..." # also take a market reading (network)
```

The cadence is not cron-of-the-week. It moves when the facts move, and uses
three devices so it converges instead of hunting:

- **A ladder, not a continuum.** Intervals come from a small ordered set
  (`1, 2, 5, 10, 15, 30` min for the hot jobs, `15, 30, 60, 120, 240` for the
  probes), and pressure is bucketed onto it. A small change in the climate lands
  on the same rung, so nothing moves for nothing.
- **A deadband.** A candidate has to be 1.6× faster or slower than what is
  installed before it is worth touching.
- **Dwell.** After a change, nothing else moves for six hours — unless the
  situation is critical: a paid customer is owed work, or a ledger invariant is
  failing. Those bypass the wait, because they are the reason the schedule
  exists.

Climate is measured from what the box already knows, not from vibes: jobs in the
last hour, the owed backlog, integrity failures, degraded connections, `intel`
envelopes on the bus, and optionally the keyless market probe. A quiet box rests
at bridge/ingest every 30 min, probes every 4 h, readiness every 2 h, and stays
there. State lives in `logs/schedule-state.json` (untracked).

`tools/bootstrap.sh` installs that resting cadence plus an hourly
`swarmctl cron tune --apply`, so the schedule corrects itself without anyone
watching it. When a job stops producing output, `swarmctl cron status` is the
first thing to run.

First run on a machine — one line, with the installer versioned in the repo
rather than pasted from a chat:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/BleakNarratives/passive_income_swarm/main/tools/install.sh)
```

It clones to `~/passive_income_swarm`, or fast-forwards an existing checkout to
`main`, then hands off to `tools/bootstrap.sh`. Safe to re-run: a dirty or
diverged checkout is left alone, and bootstrap replaces its own cron block
instead of stacking a second one. `--repo PATH` installs elsewhere, `--no-cron`
leaves the crontab alone, `--dry-run` prints the block and changes nothing.

Offline, or if you would rather see every step, the two commands it replaces:

```bash
git clone https://github.com/BleakNarratives/passive_income_swarm.git ~/passive_income_swarm
bash ~/passive_income_swarm/tools/bootstrap.sh
```

Then `swarmctl cron status` answers "is anything broken?"

## The portal

Sourcing `tools/portal.sh` puts a door on the checkout: land in the repo root
and the shell prints the branch and commit, a swarm health dot, cron health,
whether the box is safe to charge, the README's roadmap, and the next useful
command. No daemon and no network — it reads git, the crontab,
`logs/console-telemetry.json`, `logs/readiness.json`, `cabinet.json` and
`README.md`.

The layout is deliberately plain: no frames, no rules, no animation, every line
clipped to the terminal width, so it reads the same in a 40-column tmux pane as
in a full window. `portal --agents` prints the cabinet roster one agent per line
for when you want the roster rather than the summary.

```bash
bash tools/portal.sh --install     # add the hook to ~/.zshrc or ~/.bashrc
# or, for one shell only:
. ~/passive_income_swarm/tools/portal.sh

portal              # the full render, from anywhere
portal --agents     # the cabinet roster, one agent per line
portal -a           # every section, including files and agents
portal --health -v  # the stats behind the health dot
portal --ascii      # ASCII glyphs only
portal -q           # banner and dot only
```

The dot is the quiet part: `● ok`, `◐ degraded`, `✕ failing`, `○ unknown`,
computed from the telemetry the bridge already exports — degraded connections,
inference errors, ledger integrity, and telemetry age (stale after 48 h). It
costs one file read and one process, so the common case is a single character
rather than another paragraph. `--health -v` opens it up:

It rides `PROMPT_COMMAND` (bash) or `chpwd_functions` (zsh) rather than
redefining `cd`, and fires once per entry into the root, so it does not turn
every prompt into a report. Settings are a single letter string:

| setting | meaning |
|---|---|
| `SWARM_PORTAL_FLAGS="srnh"` | default sections — `s` cron, `r` roadmap, `n` next, `h` swarm health; `e` agents, `l` files |
| `SWARM_PORTAL_ASCII=1` | never emit a non-ASCII glyph (also `portal --ascii`) |
| `SWARM_PORTAL_WIDTH=80` | clip to this width instead of `$COLUMNS` |
| `SWARM_PORTAL=0` | disable the hook for this shell |
| `SWARM_PORTAL_ROOT` | point at a different checkout |

## Stack

- **FastAPI** — async gateway
- **Pydantic** — request/response models
- **Swarm agents** — Mrs. Higgins (content), Thoth (analysis)
- **Control plane** — `swarm/` (stdlib only): registry, vending machine, skills, scouts, forecast, supervisor

## API Key Management

Store keys in `.env` (DO NOT COMMIT).

---

*BleakNarratives // 2026*

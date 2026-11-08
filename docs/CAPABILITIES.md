# Capabilities with evidence

A manifest can say an agent needs `llm.generate`. It cannot say that anything
here can actually generate anything. The capability registry closes that gap:
one record per capability, with the observations behind its state.

It is not a fourth registry. `swarm/cabinet.json` declares intent,
`swarm/connections.py` binds intent to a provider and probes it, and
`swarm/capabilities.py` records what has been observed — on which machine, at
which commit, from which command.

## States

| state | means |
| --- | --- |
| `verified` | a command ran and exited 0 on this machine; its output hash is stored |
| `tested` | a live or degraded probe exercised the provider successfully |
| `available` | an interface is bound and configured, but nothing has run it |
| `delegated` | another system is responsible for it; recorded, not exercised |
| `blocked` | a real barrier: missing key, unreachable provider, no binding |
| `unknown` | nothing established yet |

States only move up. A later failed probe does not erase an earlier verified
result; it appends evidence and leaves the state alone. That is deliberate:
a blocked payment rail should stay visible as `blocked` in the reason, without
pretending yesterday's working run never happened.

`verified` is refused unless a command exited 0. There is no flag that asserts
verification without a run.

## Evidence

Each entry in `logs/capabilities.json` records:

```json
{
  "source": "command",
  "state": "verified",
  "detail": "exit 0",
  "at": "2026-11-08T22:10:04Z",
  "machine": "chromebook",
  "commit": "abbc123",
  "command": "curl -fsS https://api.example/health",
  "exit_code": 0,
  "output_sha256": "<sha256 of combined stdout+stderr>"
}
```

Output is hashed, not stored: evidence should not become a place where tokens
or customer data accumulate. The newest 20 observations per capability are
kept.

An entry from another machine, or from a commit that is not `HEAD`, is marked
`[historical]` in the report. It is still evidence of what happened then; it is
not evidence of what works now.

## Commands

```bash
./swarmctl capabilities                  # current states
./swarmctl capabilities --json           # same, for other tools
./swarmctl capabilities --strict         # exit 1 if anything is blocked
./swarmctl capabilities sync             # rebuild from cabinet + last probe report
./swarmctl capabilities record llm.generate --state available --detail "key present"
./swarmctl capabilities record llm.generate --command "curl -fsS .../health"
./swarmctl capabilities import ~/chromebook-capabilities.json --source chromebook
```

`sync` is offline: it reads the cabinet, the connection audit and
`logs/connections.json`. Run `./swarmctl connections --probe` first if the
cached report is stale; the registry will not touch the network on its own.

`record --command` runs exactly the command given, captures its exit code and
output hash, and stores `verified` on 0 or `blocked` otherwise. Nothing else in
this module executes a capability.

## Importing the Chromebook list

The registry on the other machine can be merged before the two are reconciled:

```bash
./swarmctl capabilities import ./chromebook-capabilities.json --source chromebook
```

Accepted shapes:

```json
[{"id": "llm.generate", "state": "tested", "detail": "local ollama"}]
```

```json
{"capabilities": {"llm.generate": {"state": "tested", "detail": "local ollama"}}}
```

Unknown states become `unknown`; a foreign `verified` without a run is kept as
evidence but cannot overwrite a state that was earned here, because imports
also only move up. Comparing the two registries is then a diff of states and
`declared_by`, not a merge conflict: either system can be ahead on a capability
without either being erased.

The Chromebook codebase is not in this repository. Treat its list as an
external source, not as truth; re-run the command behind any state before
promoting it to `verified` here.

## What this does not do

- It does not probe on its own, fetch credentials, or infer capability from a
  filename. Capability is a claim until something observes it.
- It does not decide which registry wins. It keeps both observations, scoped
  and dated, so that decision can be made from a diff.
- It is not the Syntax subsystem. `docs/SYNTAX.md` describes the direction;
  this is step 1, the evidence layer, and nothing more.

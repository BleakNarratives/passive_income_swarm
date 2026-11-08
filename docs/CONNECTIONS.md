# Connections

Every capability in `swarm/cabinet.json` is bound to a provider that can be
**probed**. A declaration is not a connection; this layer is what makes the
difference observable.

```sh
swarmctl connections                 # coverage + orphans, no network
swarmctl connections --probe         # real reachability checks
swarmctl connections --probe --save  # cache the report for the console
swarmctl connections --json          # machine-readable
```

Exit code is `1` when a declared capability has nothing serving it, or when a
probed connection is configured but unreachable. That makes it usable in CI and
in a pre-deploy gate. `--allow-degraded` downgrades that to `0`.

## What exists

| Connection | Capabilities | Needs | Probe |
|---|---|---|---|
| `groq` | `llm.generate` | `GROQ_API_KEY` | `GET /openai/v1/models` |
| `local_embed` | `llm.embed` | — | runs the embedder, checks determinism + norm |
| `nostr_relays` | `net.nostr` | — | TCP 443 to the three relays |
| `stdlib_http` | `net.http` | — | interpreter capability |
| `local_fs` | `fs.read`, `fs.write` | — | root readable + writable |
| `local_shell` | `shell.exec` | — | spawns a python subprocess |
| `ledger` | `data.ledger` | — | `logs/` writable |
| `lnurl` | `pay.lnurl` | `keys/nwc.secret` or `LNURL_LUD16` | resolves `.well-known/lnurlp` |
| `github` | `repo.gh`, `market.search` | `GITHUB_TOKEN` *(optional)* | `GET /rate_limit` |
| `webz_news` | `market.search` | `WEBZ_API_TOKEN` | provider search endpoint |

`github` is deliberately **degraded, not broken**, when unauthenticated: the
public API works at 60 requests/hour, which is enough for occasional scout runs
and not enough for a fleet. The report says so instead of pretending otherwise.

`webz_news` is the optional upgrade for demand-signal scouts. It stays
`unconfigured` until a token exists, and `WEBZ_API_BASE` lets you point at a
different endpoint without touching code. Its endpoint shape should be confirmed
against the provider's current docs before you rely on it.

## Statuses

| Status | Means |
|---|---|
| `live` | probe succeeded just now |
| `degraded` | reachable but limited (e.g. unauthenticated GitHub) |
| `unconfigured` | a required variable is missing — **the probe was not run** |
| `unreachable` | configured, but the probe failed |
| `unverified` | configured, not probed (offline report) |
| `unbound` | a capability nothing serves — the defect this layer exists to catch |

Capability roll-up is generous but documented: a capability is `live` if any
connection serving it is live, `unconfigured` only if every one of them is merely
missing credentials.

## Design constraints

* **Stdlib only.** Probes use `urllib`/`socket`/`subprocess`; there is no SDK,
  no `pip install`, and nothing to pin.
* **Probes never raise.** A dead provider is a status, not a crash.
* **No secret is ever in a report.** Only the *name* of a missing variable is
  reported, never a value, and token-bearing URLs are not echoed. There is a
  regression test for exactly this.
* **Offline by default.** `swarmctl connections` and `doctor` touch no network.
  The console never probes on a timer; it reads `logs/connections.json`, which an
  explicit `--save` run (or the console's `probe_connections` action, which needs
  `DASHBOARD_TOKEN`) writes.

## Adding a connection

```python
from swarm.connections import Connection, Probe

def _probe_acme(timeout):
    return Probe(True, "HTTP 200")   # or Probe(False, "refused")

Connection(
    id="acme",
    label="ACME API",
    provider="acme",
    capabilities=("market.search",),
    probe=_probe_acme,
    required_env=("ACME_API_KEY",),
    docs="https://acme.example/docs",
)
```

Register it in `build_connections()`. Then run `swarmctl connections` — the audit
will tell you immediately whether the capability it claims is really covered, and
`swarmctl doctor` will start failing if it ever isn't.

# INTEGRATION.md — the three relays

How the `core_framework` handoff maps onto this swarm, what is implemented and
tested, and the single adapter boundary that needs reconciling once
`core_framework` is reachable.

> **Status up front:** `core_framework` is **not present in this workspace**.
> There is no `ScoutLoader.write_sidecar`, `PROOFS_AND_GATES.md`, `WhorlBus`,
> `sync_hints.py` or `domino.sh` on this filesystem, and this repo tracks only
> `passive_income_swarm` (`main`). So the field names and the P2 digest
> definition below are **this repo's frozen contract**, chosen to satisfy the
> directive text. They are not copied from a file I could not read. When the
> framework lands, reconcile exactly the one method marked *ADAPTER* below.

---

## Relay 1 — Standardize payload contracts

> "Standardize emitter schema to match `ScoutLoader.write_sidecar` signature.
> Payload MUST include `content_sha256` (P2 verification). Reject all ad-hoc
> JSON without a valid P2 digest."

**Implemented in** `swarm/contracts.py`.

An emitter no longer sends a loose dict. It sends an `Envelope`:

| Field | Required | Notes |
|---|---|---|
| `schema` | yes | `"swarm.payload/1"` |
| `id` | auto | short hex |
| `emitter` | yes | agent/scout id |
| `kind` | yes | `telemetry` · `pheromone` · `intel` · `skill_update` |
| `created` | auto | UTC |
| `content_sha256` | **yes** | 64 lowercase hex chars — the P2 digest |
| `confidence` | default 0.5 | `[0, 1]` |
| `lineage` | no | source path — feeds Relay 3 |
| `tags` | no | free-form |
| `payload` | yes | JSON object |

**P2 digest (the definition this repo uses):**

```
content_sha256 = sha256( json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) )
```

Key order is irrelevant; any byte of drift changes the digest. Defined in
`swarm/contracts.py::content_digest`.

**Rejection is real.** `Envelope.from_dict` refuses ad-hoc JSON missing
`schema`/`emitter`/`kind`/`payload`/`content_sha256`; `Envelope.validate`
refuses a digest mismatch, an unknown `kind`, a bad `schema`, or a
`confidence` outside `[0, 1]`. The `WhorlBus` validates on publish, and the
`Ingestor` re-checks the confidence floor on consume.

### ⚠ ADAPTER — the one thing to reconcile

`swarm/contracts.py::Envelope.to_sidecar` is the single projection point onto
`ScoutLoader.write_sidecar`'s expected shape. Nothing else in the repo depends
on core_framework's field names. When you can read the real signature, edit that
one method (and, only if the framework's canonicalization differs, swap
`content_digest`).

---

## Relay 2 — Routing via WhorlBus

> "Stop file-based sidecar polling. Direct all telemetry and pheromone emissions
> to WhorlBus. Monitor `bus.jsonl` for high-confidence ScoutAgent signals."

**Implemented in** `swarm/whorl.py`.

- One append-only, fsync-durable `bus.jsonl` at the project root.
- `WhorlBus.publish(envelope)` / `.emit(...)` / `.deposit(...)`.
- `records()`, `tail(n)`, `of_kind(kind)`, `high_confidence(min)`.
- `drain()` is cursor-based, so consumers read *new* signals, not the whole file
  — this is the "monitor `bus.jsonl`" loop.
- `watch(poll_s)` is a generator convenience.

**Pheromone layer** — a *novel mechanic* rather than a rewrite of message
passing: a scout deposits decaying strength on a **trail** (a niche, a rival
pattern, a customer segment). `pheromone_map()` decays each deposit by
`0.5 ** (age / half_life_s)` and sums by trail; `hottest(n)` ranks them. Valuable
trails recruit other scouts while hot and fade on their own if nobody
corroborates. That is how the swarm decides where to spend attention without a
central scheduler.

Try it: `swarmctl bus tail`, `swarmctl bus high --min 0.8`, `swarmctl bus pheromones`.

---

## Relay 3 — Gated self-improvement

> "Implement `PROOFS_AND_GATES.md` logic. Do NOT ingest or self-modify based on
> data from `._archive/` or `UNPACKED/`. Only accept updates from `src/skill/`
> or `projects/agents/scouts/` (live lineage confirmed)."

**Implemented in** `swarm/lineage.py` + enforced in `swarm/ingest.py`.

- `ALLOWED_ROOTS = ("src/skill/", "projects/agents/scouts/")`
- `DENIED_ROOTS = ("._archive", "UNPACKED")` — matched as path **segments**
  anywhere in the path.
- Fail-closed: an **unknown** source may send read-only telemetry, but can
  **never** drive self-modification.
- Every decision is written to the ledger as `lineage_gate` for audit.

Routing rules in `Ingestor`:

| `kind` | Purpose | Gate |
|---|---|---|
| `skill_update` | self_modify | must be **live lineage**, else rejected |
| `intel` | telemetry | unknown allowed but labelled; scored for incentive |
| `telemetry` / `pheromone` | telemetry | unknown allowed; recorded |

Try it: `swarmctl lineage src/skill/x --self-modify` (exit 0),
`swarmctl lineage ._archive/x --self-modify` (exit 1).

---

## The incentives layer (your ask: reward the weird, rare and sellable)

**Implemented in** `swarm/incentives.py`. A find's reward is:

```
total = 0.35·novelty + 0.25·rarity + 0.30·marketability + 0.10·confidence
        × (1 + 0.35)  when novelty ≥ 0.60          ← outside-the-box bonus
        × 0              when lineage is not live   ← no reward for dead lineage
```

- **novelty** — 1 − max Jaccard against everything the swarm already knows
  (skills, capabilities, registry occupants).
- **rarity** — mean inverse document frequency against a fitted corpus.
- **marketability** — weighted demand signals: `revenue, niche, demand,
  uniqueness, exclusivity, freshness`.

A novel find is rewarded **super-linearly**, so scouts are explicitly
incentivised to chase rare, niche, marketable intel instead of farming the same
safe obvious thing. Rewards book to the ledger (`incentive`) and, when the
subject is a registered skill, straight into its score — so incentives drive
skill mastery too.

Try it: `swarmctl incentive --text "..." --signals '{"revenue":0.9}'`.

---

## Running the gated loop

```bash
bash ./swarmctl relay                                   # print directives + emit SWARM_RELAY_SYNC
bash ./swarmctl contract emit --emitter augur --kind intel \
     --payload '{"text":"rare niche","trail":"niche-x","signals":{"revenue":0.9}}' \
     --confidence 0.9 --lineage projects/agents/scouts/augur
bash ./swarmctl ingest                                  # drain the bus through the gates
bash ./swarmctl bus pheromones                          # where attention is pooling
```

---

## Steps that must run on **your** machine (I cannot)

These are yours to run where `core_framework` and your config live — this
workspace has neither:

```bash
python3 ~/core_framework/bootstrap/sync_hints.py     # contract generator
cat << 'EOF' >> ~/.config/freebuff/sessions.jsonl
{"timestamp_utc": $(date +%s), "event": "SWARM_RELAY_SYNC", "contract": "ScoutLoader.write_sidecar", "bus": "WhorlBus", "require_sha256": true}
EOF
curl -s http://127.0.0.1:7071/health                 # AI Gateway
./domino.sh preflight
```

`swarmctl relay` emits the equivalent `SWARM_RELAY_SYNC` event into this repo's
WhorlBus so the swarm side of that handshake is satisfied without touching your
shell config.

---

## Reconciliation checklist (when core_framework is reachable)

1. Diff `Envelope.to_sidecar()` against the real `ScoutLoader.write_sidecar`.
2. If its canonicalization differs, replace `content_digest`.
3. Confirm `PROOFS_AND_GATES.md`'s roots match `ALLOWED_ROOTS` / `DENIED_ROOTS`;
   adjust the two constants if not.
4. Point the ingestor's bus path at the real `bus.jsonl` if it lives elsewhere.
5. Re-run `bash ./run_tests.sh` — 153 tests, all stdlib.

*BleakNarratives // integration relays v1*

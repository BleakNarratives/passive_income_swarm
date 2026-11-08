# SYNTAX.md — the direction, recorded

**Status: architecture direction only.** Nothing here is implemented, authorised
for implementation, or a reason to merge repositories. It exists so the next
round of work starts from the same boundary instead of rediscovering it.
Source: handoff note, 2026-11-08. Revisit before any feature in this area grows
legs.

---

## 1. The reversal

Don't build an operating system and squeeze intelligence into it. Build an
intelligent environment that can operate an operating system.

So Syntax is the **experience and coordination environment**, not a model with a
window. The intelligence is distributed across agents and services that talk
through interfaces this repo already has: envelopes, the bus, the ledger.

---

## 2. Layers

| Layer | What it is | Owns |
|---|---|---|
| Experience | Syntax | terminal, portal, console, TUI, Grim identity |
| Intelligence | MotherBrain, ModMind | context, objectives, strategy, coordination |
| Agents | specialists | one responsibility each; may recruit subagents |
| Infrastructure | NAT/NatHub, bus, ledger, artifacts | state, history, transport, provenance |
| Applications | RepoSherpa, ShipWrekD, Tailor | navigation, workshop, opportunity → delivery |

Rules that follow from the split:

- **Models are replaceable workers.** Memory, tools, permissions and accumulated
  knowledge belong to the system, never to the provider.
- **Grim is the personality of the environment**, not the intelligence and not a
  floating chatbot mascot.
- **Syntax composes; it does not absorb.** A wing can be replaced without
  renovating the mansion.

---

## 3. Reuse before replacement

These already work. The direction must consume them; replacing one needs
evidence that the replacement is better, not merely tidier.

| Component | Where | Role in the direction |
|---|---|---|
| Cabinet roster | `swarm/cabinet.json` | occupants, slots, capabilities — 34 today: 5 agent, 10 daemon, 4 persona, 8 scout, 7 subagent |
| Envelopes | `swarm/contracts.py` | signed, digest-checked cross-agent messages |
| Bus | `bus.jsonl`, `swarm/ingest.py` | transport and gated consumption |
| Ledger | `swarm/ledger.py`, `logs/swarm.jsonl` | append-only record of what happened |
| Evidence of truth | `swarm/readiness.py`, `swarm/lifecycle.py`, `nostr_dvm/settlement.py` | stages, rail proof, money that actually moved |
| Schedule and projections | `swarm/scheduler.py`, `swarm/bridge.py` | periodic facts, deduped onto the bus |
| Views | `tools/portal.sh`, `swarm/tui.py`, `dashboard/` | three projections of one snapshot |

The cabinet is already the closest thing to a capability map. It is a manifest,
not a runtime registry — that gap is step 1 below.

---

## 4. Agents: discover, delegate, disagree, verify

- **Discover** through a capability registry with evidence states: available,
  tested, verified, delegated, blocked. "Available" means *proven on this
  machine at this commit*, with the command and output hash stored.
- **Delegate** with a task envelope: objective, inputs, acceptance test,
  budget, deadline. A subagent returns evidence, not prose.
- **Disagree** with a challenge envelope that cites the evidence it disputes.
  Skeptic and Tribunal roles make disagreement routine rather than personal.
- **Verify** independently: a claim is a hypothesis until a separate verifier
  reproduces it. The verifier must not be the agent that produced the work.
- **Preserve** before closing: the Archivist writes the artifact and its
  provenance record.

Invariants belong in the coordinator, not in prompts:

1. one writer per path (leases),
2. plan before act,
3. evidence over assertion.

---

## 5. Shared memory and artifacts

NAT is the memory: content-addressed objects, an append-only journal,
**intent-named checkpoints**, and a single `last-good` pointer so recovery works
even if the journal is corrupt. Five verbs — capture, diff, restore, remix,
reconcile. Git stays an adapter for GitHub, not the mental model.

Every artifact records: id, intent, originator, contributors, agents and tools
involved, the diff, evidence (command plus output hash), test status, delivery
location, and whether it produced income. That is deliberately the same shape as
a settlement record — attribution belongs in the ledger, not in chat history.

NatHub is exchange and synchronization. It is not a second source of truth on a
single machine.

---

## 6. Public OS Builder vs private system

**Public:** installer and bootstrap, portal, scheduler, contracts, ledger
primitives, NAT core, adapter interfaces, capability-registry schema, this
document.

**Private:** the specific roster, MotherBrain policy, ModMind strategy, Grim
identity assets, commercial pipelines, credentials.

The rule: the builder installs and configures each capability independently; the
private system composes them into the full experience. Nothing private is
required for the public pieces to run, and no public view depends on a private
service to render.

---

## 7. What stays independent

RepoSherpa, ShipWrekD, NAT, ModMind/MotherBrain, Tailor and Grim remain separate
projects with their own lifecycles and, where they exist, their own repositories.
They communicate through:

- **envelopes on the bus** for events and claims,
- **HTTP** only where a genuine request/response is needed,
- **files** for artifacts, checkpoints and snapshots.

No shared mutable database across projects. No renaming working systems to make
a diagram tidy. No forced monorepo.

---

## 8. Dependency-aware sequence

0. **Freeze this boundary.** No merging, no renames.
1. **Capability registry with evidence** — everything else quotes it.
2. **Artifact/provenance record + NAT core** (capture/diff/restore) and the
   `last-good` pointer.
3. **Path leases and the plan-before-act gate** in the coordinator.
4. **Bus topics** for delegation, challenge and verification.
5. **RepoSherpa as the first coordinator adapter** — read, plan, verify.
6. **Experience:** per-agent `ps`, then views over the same snapshot.
7. **NatHub sync**; then ShipWrekD, ModMind and Tailor as separate front-ends.
8. **Grim identity last.** It decorates the experience; it never becomes the
   intelligence.

Every step must ship without breaking cron, the console or the portal. A step
that requires all the others first is not a step, it is a rewrite.

---

## 9. Non-goals

- Merging repositories into one codebase.
- Renaming working projects for architectural neatness.
- A shared database between independent services.
- Depending on a single model provider.
- An animated mascot standing in for intelligence.
- Building any of this before the boundary is agreed.

---

## 10. Open questions

- Where does objective state live — MotherBrain, the ledger, or both with one
  declared owner?
- NAT object format and dedupe policy across projects?
- How is "verified" evidence trusted across machines and commits?
- What budget and permission model governs subagent recruitment?
- Which parts of MotherBrain are policy (public) and which are configuration
  (private)?

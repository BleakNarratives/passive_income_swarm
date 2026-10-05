"""
swarm.ingest — the gated ingestion loop
=======================================

Where the three relays meet. The ingestor drains the WhorlBus, and for every
envelope it:

1. **Relay 1** — the envelope already passed P2 digest verification on the bus
   (ad-hoc JSON never made it this far); confidence floors are enforced here.
2. **Relay 3** — the lineage gate decides whether this source may drive
   self-modification. ``skill_update`` requires live lineage; telemetry may pass
   from unknown sources but is labelled as such.
3. **Relay 2** — accepted signals are routed: ``skill_update`` becomes a skill,
   ``intel`` is scored for incentive and can drop a pheromone, ``telemetry`` and
   ``pheromone`` are booked.

Every accept and every reject is written to the ledger. The loop is
drain-and-return, never a daemon, so it composes with systemd, cron or the
supervisor.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .breaker import IngestBreaker
from .contracts import Envelope
from .discovery import FERAL, ScoutRegistry
from .incentives import ScoutIncentives
from .ledger import Ledger
from .lineage import PURPOSE_SELF_MODIFY, PURPOSE_TELEMETRY, LineageGate
from .skills import SkillStore
from .whorl import WhorlBus

__all__ = ["Ingestor"]


class Ingestor:
    """Consumes the bus, enforces the gates, routes what survives."""

    def __init__(
        self,
        bus: WhorlBus,
        ledger: Ledger,
        gate: Optional[LineageGate] = None,
        store: Optional[SkillStore] = None,
        incentives: Optional[ScoutIncentives] = None,
        min_confidence: float = 0.0,
        breaker: Optional[IngestBreaker] = None,
        registry: Optional[ScoutRegistry] = None,
    ):
        self.bus = bus
        self.ledger = ledger
        self.gate = gate or LineageGate()
        self.store = store
        self.incentives = incentives
        self.min_confidence = float(min_confidence)
        self.breaker = breaker
        self.registry = registry

    def run_once(self) -> Dict[str, Any]:
        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        rewards: List[Dict[str, Any]] = []
        strict = bool(self.breaker and self.breaker.is_locked())

        for env in self.bus.drain():
            # -- Janus Guard: STRICT_INGEST_ONLY refuses unregistered emitters
            if strict and self.registry is not None and not self.registry.is_registered(env.emitter):
                self.ledger.append(
                    "feral_emitter",
                    {"emitter": env.emitter, "id": env.id, "tag": FERAL, "mode": "STRICT_INGEST_ONLY"},
                    actor="ingest",
                )
                rejected.append(self._reject(env, f"STRICT_INGEST_ONLY: {FERAL} emitter '{env.emitter}'"))
                continue

            # -- confidence floor
            if env.confidence < self.min_confidence:
                rejected.append(self._reject(env, "below confidence floor"))
                continue

            # -- lineage gate
            purpose = PURPOSE_SELF_MODIFY if env.kind == "skill_update" else PURPOSE_TELEMETRY
            decision = self.gate.check(env.lineage, purpose, ledger=self.ledger)
            if not decision.permitted:
                rejected.append(self._reject(env, decision.reason))
                continue

            # -- routing
            if env.kind == "skill_update":
                self._apply_skill_update(env)
            elif env.kind == "intel":
                reward = self._score_intel(env, decision.permitted)
                if reward is not None:
                    rewards.append(reward.to_dict())
                self._maybe_deposit(env)
            # telemetry and pheromone need no further routing

            accepted.append(
                {"id": env.id, "kind": env.kind, "emitter": env.emitter, "lineage": env.lineage}
            )
            self.ledger.append(
                "payload_ingested",
                {"id": env.id, "kind": env.kind, "emitter": env.emitter, "lineage": env.lineage},
                actor="ingest",
            )

        return {
            "mode": "STRICT_INGEST_ONLY" if strict else "OPEN",
            "accepted": len(accepted),
            "rejected": len(rejected),
            "accepted_ids": [a["id"] for a in accepted],
            "rejected_ids": [r["id"] for r in rejected],
            "rejected_reasons": [r["reason"] for r in rejected],
            "rewards": rewards,
        }

    # -- helpers -----------------------------------------------------------
    def _reject(self, env: Envelope, reason: str) -> Dict[str, Any]:
        self.ledger.append(
            "payload_rejected",
            {"id": env.id, "kind": env.kind, "emitter": env.emitter, "lineage": env.lineage, "reason": reason},
            actor="ingest",
        )
        return {"id": env.id, "kind": env.kind, "reason": reason}

    def _apply_skill_update(self, env: Envelope) -> None:
        if self.store is None:
            return
        p = env.payload
        skill_id = str(p.get("skill_id") or p.get("id") or "")
        if not skill_id:
            return
        self.store.propose(
            skill_id=skill_id,
            name=str(p.get("name") or skill_id),
            capabilities=list(p.get("capabilities") or []),
            provenance={
                "discovered_by": env.emitter,
                "source": "whorlbus",
                "lineage": env.lineage,
                "id": env.id,
            },
            description=str(p.get("description") or ""),
            steps=list(p.get("steps") or []),
        )

    def _score_intel(self, env: Envelope, lineage_ok: bool) -> Optional[Any]:
        if self.incentives is None:
            return None
        text = str(env.payload.get("text") or env.payload.get("summary") or env.payload)
        signals = env.payload.get("signals") if isinstance(env.payload.get("signals"), dict) else None
        find = self.incentives.score(
            subject=str(env.payload.get("subject") or env.id),
            text=text,
            signals=signals,
            confidence=env.confidence,
            lineage_ok=lineage_ok,
        )
        return self.incentives.record(find, ledger=self.ledger, store=self.store, actor=env.emitter)

    def _maybe_deposit(self, env: Envelope) -> None:
        trail = env.payload.get("trail")
        if not trail:
            return
        self.bus.deposit(
            emitter=env.emitter,
            trail=str(trail),
            strength=float(env.payload.get("strength", env.confidence)),
            lineage=env.lineage,
            note="ingested intel",
        )

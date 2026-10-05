"""
swarm.scouts — autonomous discoverers
=====================================

Scouts are the part of the ecosystem that goes and looks. They read the ledger
— the only source of truth — and turn it into decisions:

* **discover** mines repeated, successful behaviour that no skill covers yet and
  proposes an emergent skill for it. Skills are grown from evidence, not
  written by hand.
* **evaluate** reads each skill's score history and decides who should be
  watched, retired or sent to the certification gate.
* **audit** finds capabilities the swarm exercises but never declared — the
  leakage that makes tool inventories rot.

Everything a scout concludes is a :class:`Finding`: a recommendation plus a
confidence. Scouts recommend; the operator (or an explicit ``--apply``) acts.
Keeping the two separate is what stops an autonomous loop from quietly
mutating production at 3am.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .ledger import Ledger
from .registry import Registry
from .skills import SkillStore

__all__ = ["ScoutEngine", "Finding"]


@dataclass
class Finding:
    kind: str
    subject: str
    detail: str
    confidence: float
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _slope(values: List[float]) -> float:
    """Least-squares slope over a short series; 0.0 if too short."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / denom


class ScoutEngine:
    """Deterministic scout behaviours over the ledger + skill store."""

    def __init__(
        self,
        store: SkillStore,
        ledger: Ledger,
        registry: Optional[Registry] = None,
        forecast: Optional[Any] = None,
    ):
        self.store = store
        self.ledger = ledger
        self.registry = registry
        self.forecast = forecast

    # -- discovery ---------------------------------------------------------
    def discover(self, min_occurrences: int = 3, apply: bool = False) -> List[Finding]:
        """Mine completed runs for behaviour worth formalising into a skill."""
        findings: List[Finding] = []
        successes: Dict[str, int] = {}
        failures: Dict[str, int] = {}
        for event in self.ledger.query(type="complete"):
            entity = str(event.data.get("entity") or event.actor)
            if event.data.get("ok", True):
                successes[entity] = successes.get(entity, 0) + 1
            else:
                failures[entity] = failures.get(entity, 0) + 1

        known = {s.id for s in self.store.all()}
        for entity, count in sorted(successes.items(), key=lambda kv: -kv[1]):
            if count < min_occurrences:
                continue
            skill_id = f"auto.{entity}"
            if skill_id in known:
                continue
            confidence = min(0.95, 0.4 + 0.05 * count)
            finding = Finding(
                kind="propose_skill",
                subject=skill_id,
                detail=f"{count} successful runs by '{entity}' with no covering skill",
                confidence=confidence,
                payload={
                    "entity": entity,
                    "capabilities": self._capabilities_for(entity),
                    "provenance": {"discovered_by": "skill_scout", "source": "ledger.complete"},
                },
            )
            findings.append(finding)
            if apply:
                self._apply_proposal(finding)

        for entity, count in sorted(failures.items(), key=lambda kv: -kv[1]):
            if count < min_occurrences:
                continue
            findings.append(
                Finding(
                    kind="propose_remediation",
                    subject=f"auto.remediate.{entity}",
                    detail=f"{count} failed runs by '{entity}' suggest a missing recovery skill",
                    confidence=min(0.9, 0.3 + 0.06 * count),
                    payload={"entity": entity},
                )
            )
        return findings

    def _capabilities_for(self, entity_id: str) -> List[str]:
        if self.registry:
            entity = self.registry.get(entity_id)
            if entity:
                return list(entity.capabilities)
        return []

    def _apply_proposal(self, finding: Finding) -> None:
        self.store.propose(
            skill_id=finding.subject,
            name=finding.subject.replace("auto.", "").replace(".", " ").title(),
            capabilities=list(finding.payload.get("capabilities", [])),
            provenance=dict(finding.payload.get("provenance", {})),
            description=finding.detail,
        )

    # -- evaluation --------------------------------------------------------
    def evaluate(self, watch_stdev: float = 0.25) -> List[Finding]:
        """Score every skill; recommend watch / retire / certify."""
        findings: List[Finding] = []
        for skill in self.store.all():
            if skill.retired:
                continue
            hist = skill.score_history[-20:]
            trend = _slope(hist) if len(hist) >= 3 else 0.0
            conf = min(0.95, 0.3 + 0.02 * skill.runs)

            if skill.flagged or skill.score < self.store.retire_below:
                findings.append(
                    Finding(
                        kind="retire",
                        subject=skill.id,
                        detail=(
                            f"score {skill.score:.3f} below floor {self.store.retire_below} "
                            f"after {skill.runs} runs (trend {trend:+.3f})"
                        ),
                        confidence=conf,
                        payload={"score": round(skill.score, 4), "trend": round(trend, 4)},
                    )
                )
            elif skill.level == "MASTER" and not skill.certified:
                findings.append(
                    Finding(
                        kind="certify_ready",
                        subject=skill.id,
                        detail="mastered; ready for the certification gate",
                        confidence=conf,
                        payload={"score": round(skill.score, 4)},
                    )
                )
            elif skill.stdev() > watch_stdev:
                findings.append(
                    Finding(
                        kind="watch",
                        subject=skill.id,
                        detail=f"unstable: stdev {skill.stdev():.3f} over recent runs",
                        confidence=conf,
                        payload={"stdev": round(skill.stdev(), 4), "trend": round(trend, 4)},
                    )
                )
        return findings

    # -- capability audit --------------------------------------------------
    def audit_capabilities(self) -> List[Finding]:
        """Flag capabilities exercised by the swarm but never declared."""
        declared = set(self.store_engine_capabilities())
        if self.registry:
            declared |= set(self.registry.capabilities.keys())
        seen: Dict[str, int] = {}
        for event in self.ledger.query(type="dispense"):
            for cap in (event.data.get("payload") or {}).get("capabilities", []) or []:
                seen[str(cap)] = seen.get(str(cap), 0) + 1
        findings: List[Finding] = []
        for cap, count in sorted(seen.items(), key=lambda kv: -kv[1]):
            if cap not in declared:
                findings.append(
                    Finding(
                        kind="capability_gap",
                        subject=cap,
                        detail=f"used {count}x but has no declared provider",
                        confidence=min(0.9, 0.4 + 0.05 * count),
                        payload={"uses": count},
                    )
                )
        return findings

    def store_engine_capabilities(self) -> List[str]:
        caps: List[str] = []
        for skill in self.store.all():
            caps.extend(skill.capabilities)
        return caps

    # -- orchestration -----------------------------------------------------
    def run(self, apply: bool = False, min_occurrences: int = 3) -> Dict[str, List[Finding]]:
        """Full scout sweep: discover, evaluate, audit."""
        return {
            "discovered": self.discover(min_occurrences=min_occurrences, apply=apply),
            "evaluated": self.evaluate(),
            "capability_audit": self.audit_capabilities(),
        }

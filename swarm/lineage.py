"""
swarm.lineage — the lineage gate (Relay 3)
=========================================

> "SWARM ORCHESTRATOR: Implement PROOFS_AND_GATES.md logic in agent
>  self-improvement loop. Do NOT ingest or self-modify based on data from
>  ._archive/ or UNPACKED/ directories. Only accept updates from src/skill/ or
>  projects/agents/scouts/ (live lineage confirmed)."

The rule, in code: **self-modification is fail-closed.** A skill update only
lands if it can prove live lineage. Archived trees and unpacked dumps are dead
lineage — they may be read for context, but they can never rewrite a skill.

This is the equivalent of ``PROOFS_AND_GATES.md``'s gate section. If that file
turns out to define different roots or a proof format, only the constants and
:meth:`LineageGate.decision` below need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import Ledger

__all__ = [
    "LineageGate",
    "LineageDecision",
    "ALLOWED_ROOTS",
    "DENIED_ROOTS",
    "declared_roots",
    "reconcile_with_proofs",
]

#: Roots that constitute live lineage for self-improvement.
ALLOWED_ROOTS = ("src/skill/", "projects/agents/scouts/")

#: Dead-lineage markers. Matched as path *segments* anywhere in the path.
DENIED_ROOTS = ("._archive", "UNPACKED")

#: Purposes a caller can request.
PURPOSE_SELF_MODIFY = "self_modify"
PURPOSE_TELEMETRY = "telemetry"


@dataclass
class LineageDecision:
    path: str
    purpose: str
    classification: str  # allow | archive | unpacked | unknown
    permitted: bool
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "purpose": self.purpose,
            "classification": self.classification,
            "permitted": self.permitted,
            "reason": self.reason,
        }


class LineageGate:
    """Decides whether a path may feed telemetry or rewrite a skill."""

    def __init__(
        self,
        allowed: Optional[tuple] = None,
        denied: Optional[tuple] = None,
        deprecated: Optional[list] = None,
    ):
        self.allowed = tuple(allowed or ALLOWED_ROOTS)
        self.denied = tuple(denied or DENIED_ROOTS)
        #: Modules retired after a state migration; their paths are denied.
        self.deprecated = set(deprecated or [])
        #: Optional state-preservation hook: merge a module's pheromones into a
        #: sink *before* the gate evaluates it, so no historical decay is lost.
        self._guard: Any = None
        self._preserve_into: Optional[str] = None
        self._preserved: set = set()

    def attach_guard(self, guard: Any, preserve_into: Optional[str] = None) -> None:
        """Attach the state-preservation hook used ahead of gate evaluation."""
        self._guard = guard
        self._preserve_into = preserve_into

    def _preserve(self, path: str) -> Optional[Dict[str, Any]]:
        """Merge a module's pheromones/adaptive memory before evaluating it.

        Runs at most once per module per gate instance so repeated evaluations
        do not double-count decayed strength.
        """
        if self._guard is None or not self._preserve_into:
            return None
        segments = self._segments(path)
        if not segments:
            return None
        module = segments[0]
        if module == self._preserve_into or module in self._preserved:
            return None
        self._preserved.add(module)
        moved = self._guard.merge_pheromones(module, self._preserve_into)
        absorbed = self._guard.absorb_adaptive_memory(module, self._preserve_into)
        return {"module": module, "into": self._preserve_into, "moved": moved, "absorbed": absorbed}

    def mark_deprecated(self, module: str) -> None:
        """Deny a module after its state has been migrated away."""
        self.deprecated.add(str(module))

    # -- normalization -----------------------------------------------------
    @staticmethod
    def normalize(path: str) -> str:
        p = str(path or "").strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        return p

    def _segments(self, path: str) -> List[str]:
        return [seg for seg in self.normalize(path).split("/") if seg]

    # -- decision ----------------------------------------------------------
    def classify(self, path: str) -> str:
        norm = self.normalize(path)
        if not norm:
            return "unknown"
        for seg in self._segments(norm):
            if seg in self.deprecated:
                return "deprecated"
            for dead in self.denied:
                if seg == dead or seg.startswith(dead + ".") or seg.startswith(dead + " "):
                    return "archive" if dead == "._archive" else "unpacked"
        for root in self.allowed:
            if norm == root.rstrip("/") or norm.startswith(root):
                return "allow"
        return "unknown"

    def decision(self, path: str, purpose: str = PURPOSE_SELF_MODIFY) -> LineageDecision:
        classification = self.classify(path)
        if classification == "deprecated":
            return LineageDecision(
                path=path,
                purpose=purpose,
                classification="deprecated",
                permitted=False,
                reason="deprecated after state migration",
            )
        if classification in ("archive", "unpacked"):
            dead = "._archive" if classification == "archive" else "UNPACKED"
            return LineageDecision(
                path=path,
                purpose=purpose,
                classification=classification,
                permitted=False,
                reason=f"dead lineage: path touches '{dead}/'",
            )
        if classification == "allow":
            return LineageDecision(
                path=path, purpose=purpose, classification="allow", permitted=True,
                reason="live lineage confirmed",
            )
        # unknown: read-only telemetry may pass; self-modification fails closed.
        permitted = purpose == PURPOSE_TELEMETRY
        return LineageDecision(
            path=path,
            purpose=purpose,
            classification="unknown",
            permitted=permitted,
            reason=(
                "unknown lineage — telemetry-only, not a self-modification source"
                if permitted
                else "unknown lineage — self-modification fail-closed"
            ),
        )

    def permit(self, path: str, purpose: str = PURPOSE_SELF_MODIFY) -> bool:
        return self.decision(path, purpose).permitted

    def check(self, path: str, purpose: str = PURPOSE_SELF_MODIFY, ledger: Optional[Ledger] = None) -> LineageDecision:
        """Preserve state, then evaluate and (optionally) record the decision.

        ``merge_pheromones()`` runs before the gate decides so a filter or
        deprecation never discards historical decay/yield.
        """
        preserved = self._preserve(path)
        decided = self.decision(path, purpose)
        if ledger is not None:
            event = decided.to_dict()
            if preserved is not None:
                event["preserved"] = preserved
            ledger.append("lineage_gate", event, actor="lineage")
        return decided


# ---------------------------------------------------------------------------
# Reconciliation against PROOFS_AND_GATES.md (checklist item 3)
# ---------------------------------------------------------------------------

def declared_roots(text: str) -> Dict[str, List[str]]:
    """Find which canonical roots a PROOFS_AND_GATES.md text actually names."""
    found_allowed = sorted({c for c in ALLOWED_ROOTS if c in text})
    found_denied = sorted({c for c in DENIED_ROOTS if c in text})
    return {"allowed": found_allowed, "denied": found_denied}


def reconcile_with_proofs(
    path: str | Path = "PROOFS_AND_GATES.md",
    allowed: Optional[tuple] = None,
    denied: Optional[tuple] = None,
) -> Dict[str, Any]:
    """Compare the active lineage roots against PROOFS_AND_GATES.md.

    Returns ``status`` of ``ok``, ``drift`` (with the exact differing roots) or
    ``source_absent`` when the document is not present. Never raises, so it is
    safe to run in a preflight.
    """
    active_allowed = tuple(allowed or ALLOWED_ROOTS)
    active_denied = tuple(denied or DENIED_ROOTS)
    p = Path(path)
    report: Dict[str, Any] = {
        "source": str(p),
        "active_allowed": list(active_allowed),
        "active_denied": list(active_denied),
    }
    if not p.exists():
        report.update({"present": False, "status": "source_absent", "drift": []})
        return report
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        report.update({"present": False, "status": "source_absent", "drift": []})
        return report

    declared = declared_roots(text)
    drift: List[Dict[str, str]] = []
    for root in declared["allowed"]:
        if root not in active_allowed:
            drift.append({"root": root, "kind": "allowed", "issue": "in PROOFS but not active"})
    for root in active_allowed:
        if root not in declared["allowed"]:
            drift.append({"root": root, "kind": "allowed", "issue": "active but not in PROOFS"})
    for root in declared["denied"]:
        if root not in active_denied:
            drift.append({"root": root, "kind": "denied", "issue": "in PROOFS but not active"})
    for root in active_denied:
        if root not in declared["denied"]:
            drift.append({"root": root, "kind": "denied", "issue": "active but not in PROOFS"})

    report.update(
        {
            "present": True,
            "declared": declared,
            "drift": drift,
            "status": "drift" if drift else "ok",
        }
    )
    return report


def deprecate_module(loser: str, winner: str, guard: Any, gate: Optional[LineageGate] = None,
                     ledger: Optional[Ledger] = None) -> Dict[str, Any]:
    """Migrate pheromone/yield state, then deprecate a scout module.

    State preservation is a precondition: the loser's bus pheromones are
    merged into the winner *and* its ``adaptive_memory.json`` decay/yield is
    absorbed before the module is marked deprecated. Refuses without a guard.
    """
    if guard is None:
        raise ValueError("deprecation requires a GraftGuard for state migration")
    decision = guard.pre_graft(winner, loser)
    moved = guard.merge_pheromones(loser, winner)
    absorbed = guard.absorb_adaptive_memory(loser, winner)
    if gate is not None:
        gate.mark_deprecated(loser)
    if ledger is not None:
        ledger.append(
            "module_deprecated",
            {
                "loser": loser,
                "winner": winner,
                "strength_moved": moved,
                "absorbed": absorbed,
                "loser_dominant": decision.loser_dominant,
            },
            actor="lineage",
        )
    return {
        "loser": loser,
        "winner": winner,
        "strength_moved": moved,
        "absorbed": absorbed,
        "loser_dominant": decision.loser_dominant,
        "deprecated": True,
    }

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
from typing import Dict, List, Optional

from .ledger import Ledger

__all__ = ["LineageGate", "LineageDecision", "ALLOWED_ROOTS", "DENIED_ROOTS"]

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

    def __init__(self, allowed: Optional[tuple] = None, denied: Optional[tuple] = None):
        self.allowed = tuple(allowed or ALLOWED_ROOTS)
        self.denied = tuple(denied or DENIED_ROOTS)

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
            for dead in self.denied:
                if seg == dead or seg.startswith(dead + ".") or seg.startswith(dead + " "):
                    return "archive" if dead == "._archive" else "unpacked"
        for root in self.allowed:
            if norm == root.rstrip("/") or norm.startswith(root):
                return "allow"
        return "unknown"

    def decision(self, path: str, purpose: str = PURPOSE_SELF_MODIFY) -> LineageDecision:
        classification = self.classify(path)
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
        """Evaluate and (optionally) record the gate decision for audit."""
        decided = self.decision(path, purpose)
        if ledger is not None:
            ledger.append("lineage_gate", decided.to_dict(), actor="lineage")
        return decided

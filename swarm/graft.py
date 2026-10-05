"""
swarm.graft — the anti-lobotomy rule
====================================

> "BEFORE any winner/loser code graft occurs in ``humane_cannibal.py``, HC MUST
>  run a pre-graft state inspection against ``adaptive_memory.json`` and SQLite
>  ledgers. If the loser module holds higher-density pheromone trails or active
>  yield statistics than the winner, HC MUST execute a state merge
>  (``merge_pheromones()``) BEFORE deprecating or archiving the loser. Never
>  sacrifice historical signal for clean syntax."

A code graft is only ever about *syntax*. The reason a loser module is worth
keeping around is that it earned signal — pheromone density, incentives, revenue
— and clean code that throws that away is a lobotomy. This module makes the
inspection a precondition, not a suggestion:

* :meth:`GraftGuard.execute` **refuses to call the graft function** until the
  state inspection has run, and it performs the pheromone merge first whenever
  the loser is the stronger signal carrier.

ADAPTER BOUNDARY
----------------
``humane_cannibal.py`` and ``adaptive_memory.json`` are **not in this workspace**.
So state is read from the live sources this repo actually has (WhorlBus
pheromones + the ledger's yield events), and ``adaptive_memory.json`` is read
*if present* with the documented shape below. Wire :meth:`GraftGuard.execute`
into HC's deprecate/archive path — that is the one integration point.

``adaptive_memory.json`` shape (optional):

    {"modules": {"<module>": {"pheromone_density": 0.0,
                              "incentives": 0.0,
                              "revenue_msat": 0.0,
                              "runs": 0}}}
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .ledger import Ledger
from .whorl import WhorlBus

__all__ = ["ModuleSignal", "GraftDecision", "GraftGuard", "GraftBlocked"]


class GraftBlocked(RuntimeError):
    """Raised when a graft is attempted without a passing pre-graft inspection."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class ModuleSignal:
    module: str
    pheromone_density: float = 0.0
    incentives: float = 0.0
    revenue_msat: float = 0.0
    runs: int = 0
    ok_runs: int = 0

    @property
    def yield_value(self) -> float:
        """Composite yield: incentives plus revenue in sats."""
        return self.incentives + (self.revenue_msat / 1000.0)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["yield_value"] = round(self.yield_value, 4)
        return d


@dataclass
class GraftDecision:
    winner: str
    loser: str
    winner_signal: ModuleSignal
    loser_signal: ModuleSignal
    loser_dominant: bool
    merge_required: bool
    merged: bool
    allowed: bool
    reason: str
    inspected: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "winner": self.winner,
            "loser": self.loser,
            "winner_signal": self.winner_signal.to_dict(),
            "loser_signal": self.loser_signal.to_dict(),
            "loser_dominant": self.loser_dominant,
            "merge_required": self.merge_required,
            "merged": self.merged,
            "allowed": self.allowed,
            "reason": self.reason,
            "inspected": self.inspected,
        }


class GraftGuard:
    """Enforces state preservation around winner/loser code grafts."""

    def __init__(
        self,
        bus: Optional[WhorlBus] = None,
        ledger: Optional[Ledger] = None,
        adaptive_memory_path: Optional[str | Path] = None,
    ):
        self.bus = bus
        self.ledger = ledger
        self.adaptive_memory_path = Path(adaptive_memory_path) if adaptive_memory_path else None
        self._inspections: Dict[str, ModuleSignal] = {}

    # -- adaptive memory (optional external source) ------------------------
    def _adaptive(self) -> Dict[str, Dict[str, Any]]:
        if not self.adaptive_memory_path or not self.adaptive_memory_path.exists():
            return {}
        try:
            raw = json.loads(self.adaptive_memory_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        modules = raw.get("modules") if isinstance(raw, dict) else None
        return dict(modules or {})

    # -- inspection --------------------------------------------------------
    def inspect(self, module: str, record: bool = True) -> ModuleSignal:
        """Read a module's live signal: pheromones, incentives and yield."""
        signal = ModuleSignal(module=module)

        if self.bus is not None:
            signal.pheromone_density = round(self.bus.pheromone_by_emitter().get(module, 0.0), 4)

        if self.ledger is not None:
            for event in self.ledger.query(type="incentive"):
                if event.actor == module or event.data.get("subject") == module:
                    signal.incentives += float(event.data.get("total", 0.0))
            for event in self.ledger.query(type="complete"):
                if event.data.get("entity") != module:
                    continue
                signal.runs += 1
                if event.data.get("ok", True):
                    signal.ok_runs += 1
                result = event.data.get("result") or {}
                signal.revenue_msat += float(result.get("revenue_msat", 0.0))

        # Fold in external adaptive memory if it exists (sum, max density).
        ext = self._adaptive().get(module)
        if ext:
            signal.pheromone_density = max(signal.pheromone_density, float(ext.get("pheromone_density", 0.0)))
            signal.incentives += float(ext.get("incentives", 0.0))
            signal.revenue_msat += float(ext.get("revenue_msat", 0.0))
            signal.runs += int(ext.get("runs", 0))

        signal.incentives = round(signal.incentives, 4)
        self._inspections[module] = signal
        if record and self.ledger is not None:
            self.ledger.append("graft_inspect", signal.to_dict(), actor="graft_guard")
        return signal

    # -- decision ----------------------------------------------------------
    def pre_graft(self, winner: str, loser: str) -> GraftDecision:
        """The mandatory pre-graft inspection. Compares signal carriers."""
        w = self.inspect(winner)
        l = self.inspect(loser)
        loser_dominant = (
            l.pheromone_density > w.pheromone_density
            or l.incentives > w.incentives
            or l.revenue_msat > w.revenue_msat
        )
        if loser_dominant:
            reason = (
                f"loser '{loser}' carries stronger signal "
                f"(pheromones {l.pheromone_density} vs {w.pheromone_density}, "
                f"yield {l.yield_value} vs {w.yield_value}) — merge required before archiving"
            )
            return GraftDecision(winner, loser, w, l, True, True, False, True, reason)
        reason = (
            f"winner '{winner}' already dominates "
            f"(pheromones {w.pheromone_density} vs {l.pheromone_density}, "
            f"yield {w.yield_value} vs {l.yield_value}) — safe to graft"
        )
        return GraftDecision(winner, loser, w, l, False, False, False, True, reason)

    # -- the merge ---------------------------------------------------------
    def merge_pheromones(self, loser: str, winner: str) -> float:
        """Re-deposit the loser's pheromone trails under the winner's name.

        Historical signal is carried forward, never discarded. Returns the total
        decayed strength moved.
        """
        if self.bus is None:
            return 0.0
        moved = 0.0
        deposits: Dict[str, float] = {}
        for env in self.bus.of_kind("pheromone"):
            if env.emitter != loser:
                continue
            trail = str(env.payload.get("trail") or "")
            if not trail:
                continue
            strength = float(env.payload.get("strength", 1.0))
            half_life = float(env.payload.get("half_life_s") or 3600.0)
            deposits[trail] = deposits.get(trail, 0.0) + strength
            moved += strength
        for trail, strength in deposits.items():
            self.bus.deposit(
                emitter=winner,
                trail=trail,
                strength=strength,
                note=f"merged from {loser}",
                lineage="projects/agents/scouts/graft",
            )
        if self.ledger is not None:
            self.ledger.append(
                "pheromone_merge",
                {"loser": loser, "winner": winner, "trails": len(deposits), "strength_moved": round(moved, 4)},
                actor="graft_guard",
            )
        return round(moved, 4)

    # -- the enforced path -------------------------------------------------
    def execute(
        self,
        winner: str,
        loser: str,
        graft_fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run the graft *only* after inspection, merging first when required.

        This is what ``humane_cannibal.py`` must call instead of carrying out the
        graft directly. If the inspection shows the loser dominant, the merge
        happens before ``graft_fn`` is ever invoked.
        """
        decision = self.pre_graft(winner, loser)
        if decision.merge_required:
            decision.merged = True
            decision.reason += f" | merged {self.merge_pheromones(loser, winner)} strength into '{winner}'"
        result = graft_fn(*args, **kwargs)
        decision.allowed = True
        if self.ledger is not None:
            self.ledger.append("graft_executed", decision.to_dict(), actor="graft_guard")
        return {"decision": decision.to_dict(), "result": result}

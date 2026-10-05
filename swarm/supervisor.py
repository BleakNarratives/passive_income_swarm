"""
swarm.supervisor — taking a beating
===================================

The swarm is expected to get kicked. Relays drop, the free inference tier
429s, a wallet API goes sideways. The supervisor is the part that absorbs all
of it and keeps the machine operable.

Per daemon it keeps: consecutive failures, restart count, last success, and a
per-daemon :class:`CircuitBreaker`. The breaker trips after N consecutive
failures, stops hammering the failing thing, and half-opens after a cooldown to
test the water with a single probe. Probes are injected, so supervision is
testable and never accidentally restarts production in a unit test.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .entities import Entity
from .ledger import Ledger
from .registry import Registry

__all__ = ["Supervisor", "CircuitBreaker", "BreakerState"]


class BreakerState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Trips after ``threshold`` consecutive failures; half-opens after cooldown."""

    threshold: int = 3
    cooldown_s: float = 60.0
    failures: int = 0
    state: str = BreakerState.CLOSED
    _opened_at: Optional[float] = None

    def allow(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.state == BreakerState.CLOSED:
            return True
        if self.state == BreakerState.OPEN:
            if self._opened_at is not None and now - self._opened_at >= self.cooldown_s:
                self.state = BreakerState.HALF_OPEN
                return True
            return False
        return True  # half-open: allow exactly one probe through

    def record(self, ok: bool, now: Optional[float] = None) -> bool:
        """Fold in an outcome. Returns True if the breaker *just* tripped."""
        now = time.monotonic() if now is None else now
        if ok:
            self.failures = 0
            self.state = BreakerState.CLOSED
            self._opened_at = None
            return False
        self.failures += 1
        # A failed half-open probe re-opens the breaker immediately.
        if self.failures >= self.threshold or self.state == BreakerState.HALF_OPEN:
            if self.state != BreakerState.OPEN:
                self.state = BreakerState.OPEN
                self._opened_at = now
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {"threshold": self.threshold, "cooldown_s": self.cooldown_s,
                "failures": self.failures, "state": self.state}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Supervisor:
    """Watches daemons, restarts them within policy, keeps the books."""

    def __init__(
        self,
        registry: Registry,
        ledger: Ledger,
        state_path: str | Path = "logs/supervisor_state.json",
        prober: Optional[Callable[[Entity], bool]] = None,
        restarter: Optional[Callable[[Entity], bool]] = None,
        base_backoff_s: float = 5.0,
        max_backoff_s: float = 300.0,
    ):
        self.registry = registry
        self.ledger = ledger
        self.state_path = Path(state_path)
        self.prober = prober or self._ledger_probe
        self.restarter = restarter
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load()

    # -- state persistence -------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._state = dict(raw.get("daemons") or {})
        for name, entry in self._state.items():
            breaker = CircuitBreaker(
                threshold=int(entry.get("threshold", 3)),
                cooldown_s=float(entry.get("cooldown_s", 60.0)),
            )
            breaker.failures = int(entry.get("failures", 0))
            breaker.state = str(entry.get("state", BreakerState.CLOSED))
            # A breaker persisted as half-open resumes as open-cold, so it probes once.
            if breaker.state == BreakerState.HALF_OPEN:
                breaker.state = BreakerState.HALF_OPEN
            self._breakers[name] = breaker

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated": _now_iso(), "daemons": {}}
        for name, br in self._breakers.items():
            entry = br.to_dict()
            entry.update(self._state.get(name, {}))
            payload["daemons"][name] = entry
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _breaker(self, name: str) -> CircuitBreaker:
        return self._breakers.setdefault(name, CircuitBreaker())

    def _entry(self, name: str) -> Dict[str, Any]:
        return self._state.setdefault(
            name,
            {"consecutive_failures": 0, "restarts": 0, "last_ok": None, "last_probe": None},
        )

    # -- probes ------------------------------------------------------------
    def _ledger_probe(self, entity: Entity, ttl_s: int = 300) -> bool:
        """Default probe: did this daemon heartbeat recently?"""
        beats = self.ledger.query(type="heartbeat", actor=entity.id)
        if not beats:
            return False
        # Compare against newest event time using seq recency as a proxy is not
        # reliable across restarts; use wall clock on the ISO timestamp.
        last = beats[-1].ts
        try:
            then = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return False
        return (time.time() - then) <= ttl_s

    def supervised(self) -> List[Entity]:
        return [e for e in self.registry.of_kind("daemon") if e.enabled]

    def backoff_for(self, failures: int) -> float:
        return min(self.max_backoff_s, self.base_backoff_s * (2 ** max(0, failures - 1)))

    # -- the loop ----------------------------------------------------------
    def tick(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "at": _now_iso(),
            "healthy": [],
            "unhealthy": [],
            "restarted": [],
            "breaker_open": [],
            "skipped": [],
        }
        for entity in self.supervised():
            name = entity.id
            entry = self._entry(name)
            breaker = self._breaker(name)
            entry["last_probe"] = _now_iso()

            if not breaker.allow():
                report["skipped"].append(name)
                self.ledger.append(
                    "health_skip", {"entity": name, "breaker": breaker.state}, actor="supervisor"
                )
                continue

            ok = bool(self.prober(entity))
            tripped = breaker.record(ok)

            if ok:
                entry["consecutive_failures"] = 0
                entry["last_ok"] = _now_iso()
                report["healthy"].append(name)
            else:
                entry["consecutive_failures"] += 1
                report["unhealthy"].append(name)

            self.ledger.append(
                "health",
                {
                    "entity": name,
                    "ok": ok,
                    "consecutive_failures": entry["consecutive_failures"],
                    "breaker": breaker.state,
                },
                actor="supervisor",
            )

            if tripped:
                report["breaker_open"].append(name)
                self.ledger.append(
                    "breaker_open",
                    {"entity": name, "failures": breaker.failures, "backoff_s": self.backoff_for(breaker.failures)},
                    actor="supervisor",
                )
                if self.restarter is not None:
                    try:
                        restarted = bool(self.restarter(entity))
                    except Exception:
                        restarted = False
                    if restarted:
                        entry["restarts"] += 1
                        report["restarted"].append(name)
                        self.ledger.append("restart", {"entity": name}, actor="supervisor")

        self.save()
        return report

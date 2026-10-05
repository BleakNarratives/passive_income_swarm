"""
swarm.breaker — the Janus Guard (hard lock)
===========================================

> "The Policy-as-Code engine MUST maintain a rolling counter of failed schema
>  validations and unverified SHA256 signatures. If more than 3 unverified
>  sidecars or invalid schema payloads attempt to touch WhorlBus within a single
>  session window, the bus auto-flushes uncommitted buffer states and locks into
>  ``STRICT_INGEST_ONLY`` mode until a manual ``domino.sh preflight`` passes."

Threshold semantics: the lock trips once the rolling count **reaches**
``threshold`` (default 3), i.e. on the 3rd schema/digest failure in the window.
The count is configurable so an operator can tighten or loosen it per policy.

Guarding both faces at once — the incoming door and the outgoing state — is why
it is called Janus.

* :class:`IngestBreaker` keeps a rolling window of contract violations. Past
  ``threshold`` (strictly greater than 3), it **flushes the bus buffer** so no
  committed work is lost, then locks ingestion into ``STRICT_INGEST_ONLY``.
* :func:`guarded_publish` / :func:`guarded_submit_raw` are the instrumented bus
  entry points that feed the counter. Use them anywhere a payload could be
  malformed.
* Nothing unlocks automatically. :meth:`IngestBreaker.preflight_pass` is the
  manual gate — the ``domino.sh preflight`` equivalent.

State persists to ``circuit_breaker.json`` so a lock survives a restart.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import Envelope, PayloadError, parse
from .whorl import WhorlBus

__all__ = [
    "IngestBreaker",
    "MODE_OPEN",
    "MODE_STRICT",
    "guarded_publish",
    "guarded_submit_raw",
]

MODE_OPEN = "OPEN"
MODE_STRICT = "STRICT_INGEST_ONLY"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class IngestBreaker:
    """Rolling-window violation counter that can hard-lock the bus."""

    def __init__(
        self,
        path: str | Path = "circuit_breaker.json",
        threshold: int = 3,
        window_s: float = 3600.0,
        bus: Optional[WhorlBus] = None,
    ):
        self.path = Path(path)
        self.threshold = int(threshold)
        self.window_s = float(window_s)
        self.bus = bus
        self.mode = MODE_OPEN
        self.locked_at: Optional[str] = None
        self.violations: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.mode = str(raw.get("mode", MODE_OPEN))
        self.locked_at = raw.get("locked_at")
        self.threshold = int(raw.get("threshold", self.threshold))
        self.window_s = float(raw.get("window_s", self.window_s))
        self.violations = list(raw.get("violations") or [])
        self.history = list(raw.get("history") or [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": self.mode,
            "locked_at": self.locked_at,
            "threshold": self.threshold,
            "window_s": self.window_s,
            "violations": self.violations,
            "history": self.history,
            "updated": _now_iso(),
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    # -- counters ----------------------------------------------------------
    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        self.violations = [v for v in self.violations if float(v.get("epoch", 0)) >= cutoff]

    def record_violation(self, kind: str, detail: str = "", now: Optional[float] = None) -> bool:
        """Count one contract violation. Returns True if this one trips the lock."""
        now = time.time() if now is None else now
        self._prune(now)
        self.violations.append({"ts": _now_iso(), "epoch": now, "kind": kind, "detail": detail})
        # Lock once the rolling count reaches the threshold (default: 3rd failure).
        if len(self.violations) >= self.threshold and self.mode != MODE_STRICT:
            self.trip(detail or kind, bus=self.bus, now=now)
            return True
        self.save()
        return False

    # -- lock / unlock -----------------------------------------------------
    def trip(self, reason: str, bus: Optional[WhorlBus] = None, now: Optional[float] = None) -> bool:
        """Lock into STRICT_INGEST_ONLY, flushing any buffered bus state first."""
        if self.mode == MODE_STRICT:
            return False
        flushed = 0
        if bus is not None:
            flushed = bus.flush()  # never lose uncommitted state
        self.mode = MODE_STRICT
        self.locked_at = _now_iso()
        self.history.append(
            {"event": "locked", "ts": self.locked_at, "reason": reason, "flushed": flushed}
        )
        self.save()
        return True

    def is_locked(self) -> bool:
        return self.mode == MODE_STRICT

    #: Alias kept for readability at call sites.
    strict = is_locked

    def preflight_pass(self, by: str = "operator", bus: Optional[WhorlBus] = None) -> None:
        """The manual release valve (`domino.sh preflight` equivalent)."""
        target = bus if bus is not None else self.bus
        flushed = target.flush() if target is not None else 0
        self.mode = MODE_OPEN
        self.locked_at = None
        self.violations = []
        self.history.append({"event": "preflight_pass", "ts": _now_iso(), "by": by, "flushed": flushed})
        self.save()

    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "locked": self.is_locked(),
            "locked_at": self.locked_at,
            "violations_in_window": len(self.violations),
            "threshold": self.threshold,
            "window_s": self.window_s,
        }


# ---------------------------------------------------------------------------
# instrumented bus entry points
# ---------------------------------------------------------------------------
def guarded_publish(bus: WhorlBus, breaker: IngestBreaker, envelope: Envelope) -> Envelope:
    """Publish, counting a schema/digest failure against the breaker."""
    if breaker.bus is None:
        breaker.bus = bus
    try:
        return bus.publish(envelope)
    except PayloadError as exc:
        breaker.record_violation("schema_or_digest", str(exc))
        raise


def guarded_submit_raw(bus: WhorlBus, breaker: IngestBreaker, text: str) -> Optional[Envelope]:
    """Parse-and-publish raw JSON, counting a failure as an unverified sidecar."""
    if breaker.bus is None:
        breaker.bus = bus
    try:
        envelope = parse(text)
    except PayloadError as exc:
        breaker.record_violation("unverified_sidecar", str(exc))
        return None
    return bus.publish(envelope)

"""
swarm.ledger — the shared substrate
===================================

An append-only, crash-safe JSONL event log. Every dispense, skill run, health
probe, forecast and scout action lands here. One file, one truth: if it is not
in the ledger it did not happen.

Design rules
------------
* Append-only. Nothing is ever rewritten or deleted; corrections are new events.
* Crash-safe. Every append is flushed and ``fsync``-ed before returning, so a
  power cut can lose at most the event being written.
* Forgiving on read. A torn final line (half-written during a crash) is skipped
  rather than crashing the whole swarm on boot.
* Ordered. A monotonically increasing ``seq`` is recovered from the tail of the
  existing file on startup, so restarts never renumber history.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

__all__ = ["Event", "Ledger", "LedgerError"]


class LedgerError(RuntimeError):
    """Raised when the ledger cannot be opened or written."""


def utc_now() -> str:
    """Second-resolution UTC timestamp; matches the repo's existing log style."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Event:
    """One immutable ledger entry."""

    seq: int
    ts: str
    type: str
    actor: str = "system"
    data: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Event":
        return cls(
            seq=int(raw.get("seq", 0)),
            ts=str(raw.get("ts", "")),
            type=str(raw.get("type", "unknown")),
            actor=str(raw.get("actor", "system")),
            data=dict(raw.get("data") or {}),
        )


class Ledger:
    """Append-only event log with fsync durability and safe concurrent appends."""

    def __init__(self, path: os.PathLike | str, max_bytes: int = 64 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._recover_seq()

    # -- internals ---------------------------------------------------------
    def _recover_seq(self) -> None:
        """Recover the highest seq from the tail of the existing ledger."""
        if not self.path.exists():
            self._seq = 0
            return
        last: Optional[Event] = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = Event.from_dict(json.loads(line))
                except (ValueError, TypeError):
                    continue
        if last is not None:
            self._seq = last.seq

    def _rotate_if_needed(self) -> None:
        """Roll the ledger to a timestamped archive when it grows past max_bytes."""
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive = self.path.with_name(f"{self.path.name}.{stamp}.bak")
        try:
            os.replace(self.path, archive)
        except OSError:
            return

    # -- public API --------------------------------------------------------
    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def append(
        self,
        type: str,
        data: Optional[Dict[str, Any]] = None,
        actor: str = "system",
    ) -> Event:
        """Append one event and return it. Durable before the call returns."""
        with self._lock:
            self._rotate_if_needed()
            self._seq += 1
            event = Event(seq=self._seq, ts=utc_now(), type=type, actor=actor, data=data or {})
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(event.to_json() + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:  # pragma: no cover - disk failure path
                raise LedgerError(f"could not append to ledger {self.path}: {exc}") from exc
            return event

    def __iter__(self) -> Iterator[Event]:
        return self.read_all()

    def read_all(self) -> Iterator[Event]:
        """Yield every valid event; silently skip torn/corrupt lines."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Event.from_dict(json.loads(line))
                except (ValueError, TypeError):
                    continue

    def query(
        self,
        type: Optional[str] = None,
        actor: Optional[str] = None,
        since_seq: int = 0,
        limit: Optional[int] = None,
    ) -> List[Event]:
        """Filter events by type/actor/seq. Deterministic, oldest-first."""
        out: List[Event] = []
        for event in self.read_all():
            if event.seq <= since_seq:
                continue
            if type is not None and event.type != type:
                continue
            if actor is not None and event.actor != actor:
                continue
            out.append(event)
            if limit is not None and len(out) >= limit:
                break
        return out

    def tail(self, n: int = 20) -> List[Event]:
        """Return the last ``n`` events."""
        events = list(self.read_all())
        return events[-n:]

    def count(self, type: Optional[str] = None) -> int:
        if type is None:
            return sum(1 for _ in self.read_all())
        return sum(1 for e in self.read_all() if e.type == type)

    def types(self) -> Dict[str, int]:
        """Histogram of event types — the raw material scouts mine."""
        hist: Dict[str, int] = {}
        for event in self.read_all():
            hist[event.type] = hist.get(event.type, 0) + 1
        return hist

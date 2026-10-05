"""
swarm.whorl — the WhorlBus (Relay 2)
====================================

> "ALL SWARM AGENTS: Stop file-based sidecar polling. Direct all telemetry and
>  pheromone emissions to WhorlBus. Monitor bus.jsonl for high-confidence
>  ScoutAgent signals."

One ordered bus. Agents no longer scatter sidecar files and hope someone polls
them; they publish :class:`~swarm.contracts.Envelope` records to a single
durable ``bus.jsonl`` and consume it by cursor.

Two signal families ride the bus:

* **telemetry / intel / skill_update** — ordinary emissions.
* **pheromone** — a decaying deposit left on a *trail* (e.g. a market niche, a
  rival pattern, a customer segment). Strength halves every ``half_life_s``, so
  a valuable find recruits other scouts while the trail is hot and fades on its
  own if nobody corroborates it. This is the swarm's attention mechanism.

The bus is append-only and fsync-durable, same discipline as the ledger.
"""

from __future__ import annotations

import calendar
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .contracts import Envelope, PayloadError
from .ledger import Ledger

__all__ = ["WhorlBus", "bus_now"]


def bus_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(ts: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return time.time()


class WhorlBus:
    """Append-only, durable signal bus with a pheromone attention layer."""

    def __init__(
        self,
        path: str | Path = "bus.jsonl",
        ledger: Optional[Ledger] = None,
        max_bytes: int = 64 * 1024 * 1024,
    ):
        self.path = Path(path)
        self.ledger = ledger
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._cursor = 0  # number of records this consumer has already drained
        self._buffer: List[Envelope] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- publishing --------------------------------------------------------
    def publish(self, envelope: Envelope) -> Envelope:
        """Validate and durably append an envelope to the bus."""
        envelope.validate()
        with self._lock:
            self._rotate_if_needed()
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(envelope.to_sidecar(), sort_keys=True) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:  # pragma: no cover - disk failure path
                raise PayloadError(f"could not publish to bus {self.path}: {exc}") from exc
        if self.ledger:
            self.ledger.append(
                "bus_signal",
                {
                    "id": envelope.id,
                    "kind": envelope.kind,
                    "emitter": envelope.emitter,
                    "confidence": envelope.confidence,
                    "lineage": envelope.lineage,
                },
                actor=envelope.emitter,
            )
        return envelope

    def emit(
        self,
        emitter: str,
        kind: str,
        payload: Dict[str, Any],
        confidence: float = 0.5,
        lineage: str = "",
        tags: Optional[List[str]] = None,
    ) -> Envelope:
        """Build (computing the P2 digest) and publish in one call."""
        return self.publish(
            Envelope.build(emitter, kind, payload, confidence=confidence, lineage=lineage, tags=tags)
        )

    def deposit(
        self,
        emitter: str,
        trail: str,
        strength: float = 1.0,
        half_life_s: float = 3600.0,
        lineage: str = "",
        note: str = "",
    ) -> Envelope:
        """Drop a pheromone on a trail."""
        return self.emit(
            emitter=emitter,
            kind="pheromone",
            payload={
                "trail": trail,
                "strength": float(strength),
                "half_life_s": float(half_life_s),
                "note": note,
            },
            confidence=min(1.0, max(0.0, strength)),
            lineage=lineage,
            tags=[f"trail:{trail}"],
        )

    def _rotate_if_needed(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        try:
            os.replace(self.path, self.path.with_name(f"{self.path.name}.{stamp}.bak"))
        except OSError:
            return

    # -- consuming ---------------------------------------------------------
    def records(self) -> List[Envelope]:
        """Parse every envelope; torn or non-conforming lines are skipped."""
        out: List[Envelope] = []
        if not self.path.exists():
            return out
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Envelope.from_dict(json.loads(line)))
                except (ValueError, TypeError):
                    continue
        return out

    def drain(self) -> List[Envelope]:
        """Return envelopes published since the previous drain (cursor-based)."""
        with self._lock:
            all_records = self.records()
            new = all_records[self._cursor :]
            self._cursor = len(all_records)
            return new

    def seek(self, position: int) -> None:
        """Move the consumer cursor (e.g. to replay from the start with 0)."""
        self._cursor = max(0, int(position))

    def of_kind(self, kind: str) -> List[Envelope]:
        return [e for e in self.records() if e.kind == kind]

    def high_confidence(self, min_confidence: float = 0.75) -> List[Envelope]:
        """The ScoutAgent signals worth acting on."""
        return [e for e in self.records() if e.confidence >= min_confidence]

    def tail(self, n: int = 20) -> List[Envelope]:
        return self.records()[-n:]

    def watch(self, poll_s: float = 1.0, iterations: Optional[int] = None) -> Iterator[Envelope]:
        """Generator over new signals — the 'monitor bus.jsonl' loop."""
        count = 0
        while iterations is None or count < iterations:
            for record in self.drain():
                count += 1
                yield record
            time.sleep(poll_s)

    # -- pheromone layer ---------------------------------------------------
    def pheromone_map(self, half_life_s: Optional[float] = None, now: Optional[float] = None) -> Dict[str, float]:
        """Decayed strength per trail, summed across all deposits."""
        now = time.time() if now is None else now
        trails: Dict[str, float] = {}
        for env in self.of_kind("pheromone"):
            trail = str(env.payload.get("trail") or "")
            if not trail:
                continue
            strength = float(env.payload.get("strength", 1.0))
            hl = float(half_life_s or env.payload.get("half_life_s") or 3600.0)
            age = max(0.0, now - _epoch(env.created))
            decayed = strength * math.pow(0.5, age / hl) if hl > 0 else strength
            trails[trail] = trails.get(trail, 0.0) + decayed
        return trails

    def hottest(self, n: int = 5) -> List[Dict[str, Any]]:
        """Trails ranked by current decayed strength."""
        ranked = sorted(self.pheromone_map().items(), key=lambda kv: -kv[1])
        return [{"trail": t, "strength": round(s, 4)} for t, s in ranked[:n]]

    def pheromone_by_emitter(self, now: Optional[float] = None, half_life_s: float = 3600.0) -> Dict[str, float]:
        """Decayed pheromone density attributed to the emitter that deposited it.

        This is the 'higher-density pheromone trails' measure the anti-lobotomy
        rule compares before a winner/loser graft.
        """
        now = time.time() if now is None else now
        out: Dict[str, float] = {}
        for env in self.of_kind("pheromone"):
            strength = float(env.payload.get("strength", 1.0))
            hl = float(env.payload.get("half_life_s") or half_life_s)
            age = max(0.0, now - _epoch(env.created))
            decayed = strength * math.pow(0.5, age / hl) if hl > 0 else strength
            out[env.emitter] = out.get(env.emitter, 0.0) + decayed
        return out

    # -- buffered commit (used by the Janus Guard hard-lock) ---------------
    def stage(self, envelope: Envelope) -> None:
        """Buffer an envelope in memory instead of committing it immediately."""
        envelope.validate()
        with self._lock:
            self._buffer.append(envelope)

    def flush(self) -> int:
        """Commit and fsync every staged envelope. Returns how many were written.

        The hard-lock breaker calls this so a trip never loses buffered state.
        """
        with self._lock:
            staged, self._buffer = self._buffer, []
        for env in staged:
            self.publish(env)
        return len(staged)

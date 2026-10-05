"""
swarm.vending — the machine
===========================

The analog front end of the control plane. The swarm is a cabinet; every
occupant has a glass-front slot; you pull a slot and a run comes out. No
ceremony, no config archaeology — one address, one dispense.

    swarmctl cabinet          # look at the glass
    swarmctl pull B3          # dispense Thoth
    swarmctl pull augur '{"horizon": 24}'

What "dispense" means
---------------------
A pull never executes anything in-process. It *stages* a job:

1. resolve the slot to an occupant,
2. refuse occupants that are disabled or not runnable (personas),
3. write an immutable job ticket to ``control/pending/<ticket>.job.json``,
4. append a ``dispense`` event to the ledger.

A supervised worker later claims the ticket and writes its result to
``control/outbox``. Staging-first is what makes the machine sturdy: a pull is a
single durable write, so a crash between "pulled" and "ran" loses nothing and
never double-runs (tickets are idempotent).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .entities import Entity
from .ledger import Ledger
from .registry import Registry

__all__ = ["VendingMachine", "Ticket", "VendError"]

_STATUS_OPEN = "pending"
_STATUS_CLAIMED = "claimed"
_STATUS_DONE = "complete"
_STATUS_FAILED = "failed"


class VendError(RuntimeError):
    """Raised when a pull cannot be satisfied."""


@dataclass
class Ticket:
    id: str
    slot: str
    entity_id: str
    kind: str
    action: str
    command: str
    status: str = _STATUS_OPEN
    created: str = ""
    idem: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    path: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Ticket":
        return cls(**{k: raw[k] for k in raw if k in cls.__dataclass_fields__})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class VendingMachine:
    """Resolve a slot, dispense a run, keep the books."""

    def __init__(
        self,
        registry: Registry,
        ledger: Ledger,
        root: str | Path = ".",
        cell_width: int = 15,
    ):
        self.registry = registry
        self.ledger = ledger
        self.root = Path(root)
        self.cell_width = cell_width
        self.pending_dir = self.root / "control" / "pending"
        self.outbox_dir = self.root / "control" / "outbox"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- inspection --------------------------------------------------------
    def stock(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = []
        for entity in sorted(self.registry, key=lambda e: (e.slot or "")):
            if kind and entity.kind != kind:
                continue
            rows.append(
                {
                    "slot": entity.slot,
                    "id": entity.id,
                    "kind": entity.kind,
                    "runtime": entity.runtime,
                    "display": entity.display,
                    "enabled": entity.enabled,
                    "runnable": entity.is_runnable,
                    "skills": entity.skills,
                    "capabilities": entity.capabilities,
                }
            )
        return rows

    def render_cabinet(self) -> str:
        """ASCII glass front. This is the thing an operator eyeballs."""
        slot_map = self.registry.slot_map()
        w = self.cell_width
        cols = list(self.registry.columns)
        rows = self.registry.rows
        out: List[str] = []
        header = " " * 4 + (" " * (w + 1)).join(c.center(w) for c in cols)
        out.append(f"  CABINET  {len(cols)}x{rows}   pull with:  swarmctl pull <CODE>")
        out.append("")
        out.append(header)
        top = "    ┌" + "┬".join("─" * w for _ in cols) + "┐"
        mid = "    ├" + "┼".join("─" * w for _ in cols) + "┤"
        bot = "    └" + "┴".join("─" * w for _ in cols) + "┘"
        for r in range(1, rows + 1):
            out.append(top if r == 1 else mid)
            cells = []
            for c in cols:
                code = f"{c}{r}"
                entity = slot_map.get(code)
                if entity is None:
                    cells.append("· empty".ljust(w))
                else:
                    label = f"{code} {entity.display}"
                    if not entity.enabled:
                        label = f"{code} [off] {entity.display}"
                    cells.append(label[:w].ljust(w))
            out.append("  %d │%s│" % (r, "│".join(cells)))
            row2 = []
            for c in cols:
                code = f"{c}{r}"
                entity = slot_map.get(code)
                if entity is None:
                    row2.append(" " * w)
                else:
                    sub = f"{entity.kind}"
                    row2.append(sub[:w].ljust(w))
            out.append("    │%s│" % "│".join(row2))
        out.append(bot)
        return "\n".join(out)

    # -- dispensing --------------------------------------------------------
    #: How to launch each runtime. Polyglot by design: the swarm's scouts and
    #: agents are not all Python, so the launcher is a table, not an if-chain.
    _LAUNCHERS = {
        "python": "{py} {entry}",
        "python3": "{py} {entry}",
        "bash": "bash {entry}",
        "sh": "sh {entry}",
        "shell": "{entry}",
        "node": "node {entry}",
        "js": "node {entry}",
        "javascript": "node {entry}",
        "deno": "deno run --allow-all {entry}",
        "bun": "bun {entry}",
        "elixir": "elixir {entry}",
        "exs": "elixir {entry}",
        "erlang": "escript {entry}",
        "ruby": "ruby {entry}",
        "go": "go run {entry}",
        "rust": "cargo run --manifest-path {entry}",
        # Compiled artifacts run directly (path to the built binary).
        "cpp": "{entry}",
        "c": "{entry}",
        "binary": "{entry}",
        "exe": "{entry}",
    }

    def _command_for(self, entity: Entity) -> str:
        entry = entity.entrypoint.strip()
        if not entry:
            return ""
        # An explicit interpreter always wins (custom VMs, wasm hosts, etc.).
        if entity.interpreter:
            return f"{entity.interpreter} {entry}"
        # ``-m module`` invocations only make sense for Python.
        if entity.runtime in ("python", "python3") and entry.startswith("-m"):
            return f"{sys.executable} {entry}"
        template = self._LAUNCHERS.get(entity.runtime)
        return template.format(py=sys.executable, entry=entry) if template else entry

    def pull(
        self,
        token: str,
        payload: Optional[Dict[str, Any]] = None,
        idem: Optional[str] = None,
        force: bool = False,
    ) -> Ticket:
        """Dispense one run for the slot/id in ``token``. Idempotent on ``idem``."""
        with self._lock:
            if idem:
                existing = self._find_by_idem(idem)
                if existing is not None:
                    return existing

            entity = self.registry.resolve(token)
            if entity is None:
                raise VendError(f"no slot or occupant matches '{token}'")
            if not entity.enabled and not force:
                raise VendError(f"slot {entity.slot} ({entity.id}) is disabled; use force to override")
            if not entity.is_runnable and not force:
                raise VendError(
                    f"slot {entity.slot} ({entity.id}) is a {entity.kind} and is not runnable"
                )

            ticket = Ticket(
                id=uuid.uuid4().hex[:12],
                slot=entity.slot or "??",
                entity_id=entity.id,
                kind=entity.kind,
                action=entity.role or entity.display,
                command=self._command_for(entity),
                created=_now(),
                idem=idem,
                payload=dict(payload or {}),
            )
            path = self.pending_dir / f"{ticket.id}.job.json"
            path.write_text(ticket.to_json(), encoding="utf-8")
            ticket.path = str(path)

            self.ledger.append(
                "dispense",
                {
                    "ticket": ticket.id,
                    "slot": ticket.slot,
                    "entity": ticket.entity_id,
                    "kind": ticket.kind,
                    "command": ticket.command,
                    "payload": ticket.payload,
                },
                actor="vending",
            )
            return ticket

    def _find_by_idem(self, idem: str) -> Optional[Ticket]:
        for p in sorted(self.pending_dir.glob("*.job.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("idem") == idem:
                return Ticket.from_dict(data)
        return None

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.pending_dir / f"{ticket_id}.job.json"

    def load_ticket(self, ticket_id: str) -> Optional[Ticket]:
        path = self._ticket_path(ticket_id)
        if not path.exists():
            return None
        try:
            return Ticket.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def _save(self, ticket: Ticket) -> None:
        path = self._ticket_path(ticket.id)
        path.write_text(ticket.to_json(), encoding="utf-8")

    def claim(self, ticket_id: str, worker: str = "worker") -> Ticket:
        """Mark a pending ticket as being worked. Raises if it is not claimable."""
        with self._lock:
            ticket = self.load_ticket(ticket_id)
            if ticket is None:
                raise VendError(f"unknown ticket {ticket_id}")
            if ticket.status != _STATUS_OPEN:
                raise VendError(f"ticket {ticket_id} is {ticket.status}, cannot claim")
            ticket.status = _STATUS_CLAIMED
            self._save(ticket)
            self.ledger.append("claim", {"ticket": ticket.id, "worker": worker}, actor=worker)
            return ticket

    def complete(
        self,
        ticket_id: str,
        result: Optional[Dict[str, Any]] = None,
        ok: bool = True,
    ) -> Ticket:
        """Finish a ticket and move its outcome to the outbox."""
        with self._lock:
            ticket = self.load_ticket(ticket_id)
            if ticket is None:
                raise VendError(f"unknown ticket {ticket_id}")
            ticket.status = _STATUS_DONE if ok else _STATUS_FAILED
            ticket.result = result or {}
            self._save(ticket)
            (self.outbox_dir / f"{ticket.id}.result.json").write_text(
                ticket.to_json(), encoding="utf-8"
            )
            self.ledger.append(
                "complete",
                {"ticket": ticket.id, "entity": ticket.entity_id, "ok": ok, "result": ticket.result},
                actor="vending",
            )
            return ticket

    def pending(self) -> List[Ticket]:
        out: List[Ticket] = []
        for p in sorted(self.pending_dir.glob("*.job.json")):
            try:
                ticket = Ticket.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            if ticket.status == _STATUS_OPEN:
                out.append(ticket)
        return out

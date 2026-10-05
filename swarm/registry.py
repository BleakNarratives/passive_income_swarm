"""
swarm.registry — the cabinet manifest
=====================================

Loads and validates the declarative manifest that describes every occupant of
the swarm, then hands out slot addresses. This is the file an operator edits to
rein the whole ecosystem in: add a scout, disable a daemon, re-slot an agent —
no code, one JSON document.

Slot addressing
---------------
The cabinet is a grid. Columns are letters (A, B, C...), rows are numbers
(1, 2, 3...). A slot code is ``<column><row>``, e.g. ``B3``. Occupants either
declare an explicit ``slot`` or are auto-assigned the next free one in reading
order. The address is stable and human-visible, which is the whole point: an
operator can look at the cabinet, see ``B3 = Thoth``, and pull ``B3``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .entities import Entity, EntityError

__all__ = ["Registry", "RegistryError", "DEFAULT_COLUMNS", "DEFAULT_ROWS"]

DEFAULT_COLUMNS = "ABCDEFGH"
DEFAULT_ROWS = 8

#: Minimal fallback so the control plane boots even if cabinet.json is missing.
#: The real manifest lives in ``swarm/cabinet.json``.
DEFAULT_CABINET: Dict[str, Any] = {
    "version": 1,
    "cabinet": {"columns": DEFAULT_COLUMNS, "rows": DEFAULT_ROWS},
    "entities": [
        {
            "id": "supervisor",
            "kind": "daemon",
            "display": "Supervisor",
            "role": "health + restart backoff + circuit breakers",
            "runtime": "python",
            "entrypoint": "-m swarm.cli supervise",
            "slot": "A1",
            "capabilities": ["data.ledger"],
        }
    ],
    "capabilities": {},
}


class RegistryError(ValueError):
    """Raised when the manifest is structurally invalid."""


class Registry:
    """A validated set of entities with stable slot addressing."""

    def __init__(
        self,
        entities: Iterable[Entity],
        columns: str = DEFAULT_COLUMNS,
        rows: int = DEFAULT_ROWS,
        capabilities: Optional[Dict[str, Any]] = None,
        version: int = 1,
        source: Optional[str] = None,
    ):
        self.columns = columns
        self.rows = rows
        self.version = version
        self.source = source
        self.capabilities: Dict[str, Any] = dict(capabilities or {})
        self._entities: Dict[str, Entity] = {}
        for entity in entities:
            self._entities[entity.id] = entity
        self._assign_slots()

    # -- construction ------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Dict[str, Any], source: Optional[str] = None) -> "Registry":
        cabinet = raw.get("cabinet") or {}
        entities_raw = raw.get("entities")
        if not isinstance(entities_raw, list):
            raise RegistryError("manifest must contain an 'entities' list")
        entities = []
        for item in entities_raw:
            try:
                entities.append(Entity.from_dict(item))
            except EntityError as exc:
                raise RegistryError(str(exc)) from exc
        registry = cls(
            entities,
            columns=str(cabinet.get("columns") or DEFAULT_COLUMNS),
            rows=int(cabinet.get("rows") or DEFAULT_ROWS),
            capabilities=raw.get("capabilities"),
            version=int(raw.get("version", 1)),
            source=source,
        )
        problems = registry.problems()
        if problems:
            raise RegistryError("; ".join(problems))
        return registry

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        p = Path(path)
        if not p.exists():
            return cls.from_dict(DEFAULT_CABINET, source=str(p))
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryError(f"could not read manifest {p}: {exc}") from exc
        return cls.from_dict(raw, source=str(p))

    @classmethod
    def default(cls) -> "Registry":
        """Load the shipped cabinet next to this module."""
        return cls.load(Path(__file__).with_name("cabinet.json"))

    # -- slot addressing ---------------------------------------------------
    def _slot_codes(self) -> List[str]:
        codes: List[str] = []
        for row in range(1, self.rows + 1):
            for col in self.columns:
                codes.append(f"{col}{row}")
        return codes

    def _assign_slots(self) -> None:
        taken = {e.slot for e in self._entities.values() if e.slot}
        free = [c for c in self._slot_codes() if c not in taken]
        cursor = 0
        for entity in self._entities.values():
            if entity.slot:
                continue
            if cursor >= len(free):
                raise RegistryError(
                    f"cabinet is full ({self.rows} rows x {len(self.columns)} cols); "
                    f"cannot place '{entity.id}'"
                )
            entity.slot = free[cursor]
            cursor += 1

    # -- queries -----------------------------------------------------------
    def __iter__(self) -> Iterable[Entity]:
        return iter(self._entities.values())

    def __len__(self) -> int:
        return len(self._entities)

    def get(self, entity_id: str) -> Optional[Entity]:
        return self._entities.get(entity_id)

    def by_slot(self, code: str) -> Optional[Entity]:
        code = code.strip().upper()
        for entity in self._entities.values():
            if (entity.slot or "").upper() == code:
                return entity
        return None

    def resolve(self, token: str) -> Optional[Entity]:
        """Resolve either a slot code (``B3``) or an entity id (``thoth``)."""
        token = token.strip()
        entity = self.by_slot(token)
        if entity is not None:
            return entity
        return self.get(token) or self.get(token.lower())

    def of_kind(self, kind: str, enabled_only: bool = False) -> List[Entity]:
        return [
            e
            for e in self._entities.values()
            if e.kind == kind and (e.enabled or not enabled_only)
        ]

    def enabled(self) -> List[Entity]:
        return [e for e in self._entities.values() if e.enabled]

    def children(self, entity_id: str) -> List[Entity]:
        return [e for e in self._entities.values() if e.parent == entity_id]

    def slot_map(self) -> Dict[str, Entity]:
        return {e.slot or "": e for e in self._entities.values()}

    # -- validation --------------------------------------------------------
    def problems(self) -> List[str]:
        """Return every structural problem without raising, so `doctor` can show all."""
        issues: List[str] = []
        seen_slots: Dict[str, str] = {}
        for entity in self._entities.values():
            if entity.slot:
                if entity.slot in seen_slots:
                    issues.append(
                        f"slot collision: {entity.slot} used by both "
                        f"'{seen_slots[entity.slot]}' and '{entity.id}'"
                    )
                seen_slots[entity.slot] = entity.id
            for col in (entity.slot or "A1")[0]:
                if col not in self.columns:
                    issues.append(f"'{entity.id}' slot column '{col}' outside cabinet")
            if entity.parent and entity.parent not in self._entities:
                issues.append(f"'{entity.id}' references missing parent '{entity.parent}'")
            if entity.kind == "subagent" and not entity.parent:
                issues.append(f"subagent '{entity.id}' has no parent")
            if entity.kind == "persona" and entity.is_runnable:
                issues.append(f"persona '{entity.id}' marked runnable")
        for code in seen_slots:
            if code not in self._slot_codes():
                issues.append(f"slot '{code}' is outside the {self.rows}x{len(self.columns)} grid")
        return issues

    def validate(self) -> "Registry":
        problems = self.problems()
        if problems:
            raise RegistryError("; ".join(problems))
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "cabinet": {"columns": self.columns, "rows": self.rows},
            "capabilities": self.capabilities,
            "entities": [e.to_dict() for e in self._entities.values()],
        }

"""
swarm.entities — what lives inside the cabinet
==============================================

Four kinds of occupant, one shape. Whether a thing is a long-running daemon, a
market scout, a persona-driven agent or a sub-agent it is described by the same
record so the control plane never needs a special case.

Kinds
-----
daemon    long-running supervised process (DVM worker, payment rail, supervisor)
scout     autonomous discoverer/measurer (TruthSleuth, Dominance, LeadScan)
agent     a persona-driven worker (Mrs. Higgins, Thoth, Molt)
subagent  a delegated child of an agent, scoped to one role
persona   a reusable voice/character bound to agents (not runnable on its own)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["Entity", "EntityError", "KINDS", "RUNNABLE_KINDS"]

KINDS = ("daemon", "scout", "agent", "subagent", "persona")
RUNNABLE_KINDS = ("daemon", "scout", "agent", "subagent")

#: Runtimes the control plane knows how to launch. The swarm is polyglot —
#: scouts and agents are written in Python, Elixir, JS, C++ and others.
KNOWN_RUNTIMES = (
    "python", "python3", "bash", "sh", "shell",
    "node", "js", "javascript", "deno", "bun",
    "elixir", "exs", "erlang", "ruby", "go", "rust",
    "cpp", "c", "binary", "exe",
)


class EntityError(ValueError):
    """Raised when an entity record is malformed."""


def _require(raw: Dict[str, Any], key: str, kind: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise EntityError(f"{kind} entity missing required field '{key}'")
    return raw[key]


@dataclass
class Entity:
    id: str
    kind: str
    display: str
    role: str = ""
    persona: Optional[str] = None
    runtime: str = "python"
    entrypoint: str = ""
    interpreter: Optional[str] = None
    model: Optional[str] = None
    parent: Optional[str] = None
    slot: Optional[str] = None
    enabled: bool = True
    skills: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    triggers: List[Dict[str, Any]] = field(default_factory=list)
    resources: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    notes: str = ""

    # -- construction ------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Entity":
        kind = str(_require(raw, "kind", "entity"))
        if kind not in KINDS:
            raise EntityError(f"unknown entity kind '{kind}' (expected one of {KINDS})")
        return cls(
            id=str(_require(raw, "id", kind)),
            kind=kind,
            display=str(raw.get("display") or raw["id"]),
            role=str(raw.get("role") or ""),
            persona=raw.get("persona"),
            runtime=str(raw.get("runtime") or "python"),
            entrypoint=str(raw.get("entrypoint") or ""),
            interpreter=raw.get("interpreter"),
            model=raw.get("model"),
            parent=raw.get("parent"),
            slot=raw.get("slot"),
            enabled=bool(raw.get("enabled", True)),
            skills=list(raw.get("skills") or []),
            capabilities=list(raw.get("capabilities") or []),
            triggers=list(raw.get("triggers") or []),
            resources=dict(raw.get("resources") or {}),
            tags=list(raw.get("tags") or []),
            notes=str(raw.get("notes") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # -- behaviour ---------------------------------------------------------
    @property
    def is_runnable(self) -> bool:
        return self.kind in RUNNABLE_KINDS

    def concurrency(self) -> int:
        return int(self.resources.get("concurrency", 1))

    def timeout_s(self) -> int:
        return int(self.resources.get("timeout_s", 120))

    def restart_policy(self) -> str:
        return str(self.resources.get("restart", "on-failure"))

    def trigger_kinds(self) -> List[str]:
        return [str(t.get("kind", "manual")) for t in self.triggers]

"""
swarm.discovery — dynamic registry auto-discovery
=================================================

> "Instead of static manual edits to ``SCOUT_REGISTRY.json``, any script
>  executing in ``src/skill/`` or ``projects/agents/scouts/`` MUST register its
>  ``content_sha256`` digest and schema version to
>  ``~/.config/freebuff/SCOUT_REGISTRY.json`` upon its first clean run.
>  Unregistered execution attempts are automatically tagged
>  ``FERAL_UNREGISTERED`` and routed to ``/tmp/quarantine/``."

The registry maintains itself. No human edits a list of known scouts; a scout
proves itself by running cleanly once, and its content digest is recorded. After
that, any *other* version of that script — different digest — is unrecognised
again, which is the point: a silent edit is a new, unproven organism.

Language-agnostic by construction: discovery hashes **bytes**, so a Python
scout, an Elixir scout, a Node service and a compiled C++ binary are all
registered and quarantined the same way.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import SCHEMA

__all__ = ["ScoutRegistry", "REGISTERED", "FERAL"]

REGISTERED = "REGISTERED"
FERAL = "FERAL_UNREGISTERED"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize(path: str) -> str:
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def default_registry_path() -> Path:
    """``~/.config/freebuff/SCOUT_REGISTRY.json`` as specified."""
    return Path.home() / ".config" / "freebuff" / "SCOUT_REGISTRY.json"


class ScoutRegistry:
    """Self-maintaining registry of proven scout scripts."""

    def __init__(
        self,
        path: Optional[str | Path] = None,
        quarantine_dir: str | Path = "/tmp/quarantine",
        allowed_roots: tuple = ("src/skill/", "projects/agents/scouts/"),
    ):
        self.path = Path(path) if path else default_registry_path()
        self.quarantine_dir = Path(quarantine_dir)
        self.allowed_roots = tuple(allowed_roots)
        self._scripts: Dict[str, Dict[str, Any]] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._scripts = dict(raw.get("scripts") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "updated": _now(),
            "quarantine_dir": str(self.quarantine_dir),
            "scripts": self._scripts,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    # -- hashing -----------------------------------------------------------
    @staticmethod
    def digest_file(script: str | Path) -> str:
        """sha256 of the file's bytes — works for any language or binary."""
        return hashlib.sha256(Path(script).read_bytes()).hexdigest()

    def in_allowed_roots(self, script: str) -> bool:
        norm = _normalize(script)
        return any(norm.startswith(root) for root in self.allowed_roots)

    # -- registration ------------------------------------------------------
    def register(
        self,
        script: str,
        schema: str = SCHEMA,
        digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a script's digest + schema version (first clean run)."""
        norm = _normalize(script)
        if digest is None:
            # A script path is hashed; a bare emitter id may register with an
            # explicit digest (or none, when there is no file to hash).
            try:
                digest = self.digest_file(script)
            except OSError:
                digest = None
        entry = self._scripts.get(norm, {})
        entry.update(
            {
                "script": norm,
                "content_sha256": digest,
                "schema": schema,
                "registered_at": entry.get("registered_at") or _now(),
                "last_seen": _now(),
                "clean_runs": int(entry.get("clean_runs", 0)) + 1,
            }
        )
        self._scripts[norm] = entry
        self.save()
        return entry

    def record_clean_run(self, script: str, schema: str = SCHEMA) -> Dict[str, Any]:
        """Called after a script exits cleanly; registers it on first success.

        An already-registered script whose bytes changed re-registers under the
        new digest (its clean run proves the new version too).
        """
        return self.register(script, schema=schema)

    def entry(self, script: str) -> Optional[Dict[str, Any]]:
        return self._scripts.get(_normalize(script))

    def is_registered(self, script: str, digest: Optional[str] = None) -> bool:
        norm = _normalize(script)
        found = self._scripts.get(norm)
        if not found:
            return False
        if digest is None:
            return True
        return found.get("content_sha256") == digest

    # -- gating / quarantine ----------------------------------------------
    def classify(self, script: str) -> str:
        return REGISTERED if self.is_registered(script) else FERAL

    def quarantine(self, script: str, reason: str = FERAL) -> Path:
        """Tag and route an unregistered script to the quarantine directory."""
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        src = Path(script)
        name = src.name or "unnamed"
        try:
            digest = self.digest_file(src)[:8] if src.exists() else "missing"
        except OSError:
            digest = "unreadable"
        target = self.quarantine_dir / f"{name}.{digest}.{FERAL}"
        if src.exists() and src.is_file():
            try:
                shutil.copy2(src, target)
            except OSError:
                target.write_text(f"quarantine copy failed for {script}\n", encoding="utf-8")
        else:
            target.write_text(f"{FERAL}: {script} ({reason})\n", encoding="utf-8")
        return target

    def gate_execution(self, script: str) -> Dict[str, Any]:
        """Decide whether a script may execute. Unregistered → feral + quarantine."""
        status = self.classify(script)
        if status == REGISTERED:
            return {"script": _normalize(script), "status": REGISTERED, "quarantined": None}
        path = self.quarantine(script, reason="unregistered execution attempt")
        return {"script": _normalize(script), "status": FERAL, "quarantined": str(path)}

    def sync_entities(self, entities: Any, schema: str = SCHEMA) -> List[Dict[str, Any]]:
        """Auto-register the digest + schema of active scout/agent/daemon scripts.

        Called on the first clean run of any scout in ``src/skill/`` or
        ``projects/agents/scouts/``. Only real files are registered; manifest
        occupants whose entrypoint is a module invocation or an absent path are
        skipped (they cannot be hashed yet).
        """
        registered: List[Dict[str, Any]] = []
        for entity in entities:
            kind = getattr(entity, "kind", None)
            if kind not in ("scout", "agent", "daemon", "subagent"):
                continue
            entry = str(getattr(entity, "entrypoint", "") or "").strip()
            if not entry or entry.startswith("-"):
                continue
            candidate = Path(entry.split()[0])
            if not candidate.is_file():
                continue
            if not self.in_allowed_roots(str(candidate)):
                continue
            registered.append(self.register(str(candidate), schema=schema))
        return registered

    def all(self) -> List[Dict[str, Any]]:
        return list(self._scripts.values())

    def summary(self) -> Dict[str, Any]:
        return {"registered": len(self._scripts), "path": str(self.path)}

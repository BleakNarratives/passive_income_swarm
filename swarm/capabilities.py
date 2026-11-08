"""
swarm.capabilities — capabilities with evidence
================================================

The cabinet declares what *should* be possible. :mod:`swarm.connections` proves
which provider serves each capability. This module is the third leg: a durable
record of what has actually been observed, on which machine, at which commit.

A capability is not "available" because a manifest says so. States:

    verified   a command ran and its result was confirmed on this machine
    tested     a probe exercised the interface successfully
    available  an interface exists and is configured, but nothing has run it
    delegated  another system is responsible for it
    blocked    a real barrier: missing key, unreachable provider, no binding
    unknown    nothing established yet

Evidence is append-only and scoped. Every entry records the machine, the commit
and the time, so an entry from another checkout is *historical*, not current —
which is what stops "it worked last week" from being treated as proof today.

Nothing here executes a capability on its own. ``run`` only runs a command the
operator supplied, captures its exit code and output hash, and records the
result. Importing another system's list is deliberate: the Chromebook registry
can be loaded as evidence for comparison without overwriting anything proven
here, because states only ever move up.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "SCHEMA",
    "FILE",
    "STATES",
    "RANK",
    "CapabilityRegistry",
    "state_from",
    "format_report",
    "machine_id",
    "git_commit",
]

SCHEMA = "swarm.capabilities/1"
FILE = "logs/capabilities.json"

STATES = ("verified", "tested", "available", "delegated", "blocked", "unknown")

#: Higher wins. A record can raise a state, never lower it; a blocked probe does
#: not erase a verified result, it just becomes the newest piece of evidence.
RANK = {
    "unknown": 0,
    "blocked": 1,
    "delegated": 2,
    "available": 3,
    "tested": 4,
    "verified": 5,
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(iso: str) -> Optional[float]:
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _age(iso: str) -> str:
    stamp = _epoch(iso)
    if stamp is None:
        return "never"
    seconds = max(0, int(time.time() - stamp))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def machine_id() -> str:
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def git_commit(root: str | Path = ".") -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def state_from(
    connection_ids: Iterable[str],
    rows_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[str, str]:
    """Map the connections serving a capability to a state and a reason.

    Pure: takes ids plus a cached per-connection report, touches nothing.
    """
    served = [str(cid) for cid in (connection_ids or [])]
    if not served:
        return "blocked", "no connection serves it"
    statuses = []
    details = []
    for cid in served:
        row = rows_by_id.get(cid) or {}
        status = str(row.get("status") or "")
        if status:
            statuses.append(status)
            if row.get("detail"):
                details.append(str(row["detail"]))
    if not statuses:
        return "available", "configured, not probed"
    if "live" in statuses:
        return "tested", details[0] if details else "probe ok"
    if "degraded" in statuses:
        return "tested", "probe degraded" + (f": {details[0]}" if details else "")
    if "unverified" in statuses:
        return "available", "configured, not probed"
    for status in statuses:
        if status == "unconfigured":
            return "blocked", details[0] if details else "missing configuration"
        if status == "unreachable":
            return "blocked", details[0] if details else "unreachable"
    return "blocked", details[0] if details else statuses[0]


class CapabilityRegistry:
    """A file-backed record of capabilities and the evidence behind them."""

    def __init__(
        self,
        root: str | Path = ".",
        *,
        path: Optional[str | Path] = None,
        entries: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.root = Path(root)
        self.path = Path(path) if path else self.root / FILE
        self.capabilities: Dict[str, Dict[str, Any]] = dict(entries or {})

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, root: str | Path = ".", *, path: Optional[str | Path] = None) -> "CapabilityRegistry":
        registry = cls(root, path=path)
        try:
            raw = json.loads(registry.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return registry
        if isinstance(raw, dict) and isinstance(raw.get("capabilities"), dict):
            registry.capabilities = dict(raw["capabilities"])
        return registry

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA,
            "updated_at": _now(),
            "capabilities": self.capabilities,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return self.path

    # -- entries -----------------------------------------------------------
    def ensure(self, capability: str) -> Dict[str, Any]:
        entry = self.capabilities.get(capability)
        if entry is None:
            entry = {
                "id": capability,
                "state": "unknown",
                "detail": "",
                "evidence": [],
                "connections": [],
                "declared_by": [],
            }
            self.capabilities[capability] = entry
        return entry

    def get(self, capability: str) -> Optional[Dict[str, Any]]:
        return self.capabilities.get(capability)

    def record(
        self,
        capability: str,
        state: str,
        *,
        source: str = "manual",
        detail: str = "",
        command: str = "",
        exit_code: Optional[int] = None,
        output: str = "",
        at: Optional[str] = None,
        machine: Optional[str] = None,
        commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append evidence and raise the state; never lower it.

        ``verified`` is refused unless a command actually exited 0 — a claim
        without a successful run is not verification.
        """
        if state not in STATES:
            raise ValueError(f"unknown capability state: {state}")
        if state == "verified" and exit_code != 0:
            raise ValueError("verified requires a command that exited 0")

        entry = self.ensure(capability)
        stamp = at or _now()
        machine = machine or machine_id()
        commit = commit if commit is not None else git_commit(self.root)
        evidence = {
            "source": source,
            "state": state,
            "detail": detail,
            "at": stamp,
            "machine": machine,
            "commit": commit,
            "command": command,
            "exit_code": exit_code,
            "output_sha256": (
                hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
                if output
                else ""
            ),
        }
        previous = str(entry.get("state") or "unknown")
        entry["evidence"] = (entry.get("evidence") or [])[-19:] + [evidence]
        if RANK[state] > RANK.get(previous, 0):
            entry["state"] = state
        if detail:
            # Keep the earned state and the newest observation distinguishable:
            # "verified" beside a failed run reads as a contradiction otherwise.
            entry["detail"] = detail if RANK[state] >= RANK[previous] else f"latest {state}: {detail}"
        entry["updated_at"] = stamp
        entry["current"] = machine == machine_id() and (
            not commit or commit == git_commit(self.root)
        )
        return entry

    # -- execution (explicit command only) ---------------------------------
    def run(
        self,
        capability: str,
        command: str,
        *,
        timeout: float = 120.0,
        cwd: Optional[str | Path] = None,
        detail: str = "",
    ) -> Dict[str, Any]:
        """Run an operator-supplied command as evidence. Never raises on failure."""
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd or self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            code = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            code, output = 124, f"timed out after {timeout}s"
        except OSError as exc:
            code, output = 127, f"{type(exc).__name__}: {exc}"

        state = "verified" if code == 0 else "blocked"
        entry = self.record(
            capability,
            state,
            source="command",
            command=command,
            exit_code=code,
            output=output,
            detail=detail or ("exit 0" if code == 0 else f"exit {code}"),
        )
        return {
            "capability": capability,
            "state": state,
            "exit_code": code,
            "output_sha256": entry["evidence"][-1]["output_sha256"],
            "output": output[-800:],
            "entry": entry,
        }

    # -- derivation from the existing audit --------------------------------
    def sync(
        self,
        *,
        registry: Any = None,
        connections: Any = None,
        cached: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Rebuild states from the cabinet + connection audit, then save.

        Offline: uses the last probe report on disk, never the network.
        """
        if registry is None or connections is None:
            from .connections import build_connections
            from .registry import Registry

            if registry is None:
                manifest = self.root / "swarm" / "cabinet.json"
                registry = Registry.load(manifest) if manifest.exists() else Registry.default()
            if connections is None:
                connections = build_connections(self.root)

        audit = connections.audit(registry)
        cached = cached if cached is not None else self._cached_report()
        rows = {str(row.get("id")): row for row in (cached.get("connections") or [])}

        synced = 0
        for capability, coverage in (audit.get("coverage") or {}).items():
            served = list(coverage.get("connections") or [])
            state, detail = state_from(served, rows)
            entry = self.record(capability, state, source="sync", detail=detail)
            entry["connections"] = served
            entry["declared_by"] = list(coverage.get("declared_by") or [])
            synced += 1
        self.save()
        return {
            "synced": synced,
            "probed_at": cached.get("checked_at") if isinstance(cached, dict) else None,
            "summary": self.summary(),
            "path": str(self.path),
        }

    def _cached_report(self) -> Dict[str, Any]:
        try:
            from .connections import ConnectionRegistry

            return ConnectionRegistry.load(self.root / "logs" / "connections.json") or {}
        except Exception:  # noqa: BLE001 - a missing cache is not an error
            return {}

    # -- importing another system's list -----------------------------------
    def import_entries(self, data: Any, *, source: str = "import") -> Dict[str, Any]:
        """Merge a foreign registry as evidence. Never lowers a state.

        Accepts a list of ``{id, state, detail}``, or a mapping with a
        ``capabilities`` key (dict or list). Unknown states become ``unknown``.
        """
        items: List[Dict[str, Any]] = []
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            capabilities = data.get("capabilities")
            if isinstance(capabilities, list):
                items = [item for item in capabilities if isinstance(item, dict)]
            elif isinstance(capabilities, dict):
                for capability_id, value in capabilities.items():
                    if isinstance(value, dict):
                        items.append(dict(value, id=capability_id))
                    else:
                        items.append({"id": capability_id, "state": str(value)})
            elif data.get("id"):
                items = [data]

        imported = 0
        for item in items:
            capability_id = item.get("id") or item.get("capability")
            if not capability_id:
                continue
            state = str(item.get("state") or "unknown").strip().lower()
            if state not in STATES:
                state = "unknown"
            detail = str(item.get("detail") or item.get("note") or "")
            self.record(str(capability_id), state, source=source, detail=detail)
            imported += 1
        self.save()
        return {
            "imported": imported,
            "source": source,
            "summary": self.summary(),
            "path": str(self.path),
        }

    # -- reporting ---------------------------------------------------------
    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self.capabilities.values():
            state = str(entry.get("state") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        return {"capabilities": len(self.capabilities), **{s: counts.get(s, 0) for s in STATES}}


def format_report(registry: CapabilityRegistry, *, width: int = 44) -> str:
    """A plain table. No frames, no colour, no animation."""
    summary = registry.summary()
    parts = [f"{state} {summary.get(state, 0)}" for state in STATES if summary.get(state, 0)]
    lines = [
        f"capabilities: {summary['capabilities']}   " + "  ".join(parts),
        "",
        f"{'CAPABILITY':<24}{'STATE':<11}{'EVIDENCE':<10}DETAIL",
    ]
    for capability in sorted(registry.capabilities):
        entry = registry.capabilities[capability]
        evidence = entry.get("evidence") or []
        last = evidence[-1] if evidence else {}
        age = _age(str(last.get("at") or ""))
        detail = str(entry.get("detail") or "")
        if len(detail) > width:
            detail = detail[: width - 3] + "..."
        historical = "" if entry.get("current") else "  [historical]"
        lines.append(
            f"{capability:<24}{str(entry.get('state') or 'unknown'):<11}{age:<10}{detail}{historical}"
        )
    return "\n".join(lines)

"""
swarm.ps — declared is not running
==================================

The cabinet declares 34 entities. That is a manifest, a roster, an intention.
It is not a fleet. This module answers four separate questions per entity and
refuses to collapse them:

    declared   is it in the manifest?
    available  can its runtime and entrypoint be resolved on this machine?
    active     is there a live process, or a heartbeat inside the window?
    observed   when did the ledger last carry evidence of it running?

The distinction is the point. A daemon can be declared, available and idle; a
scout can be unavailable because its runtime is absent; a persona is declared
but never independently runnable. None of those are the same as "up".

Evidence used here is real and local: the process table (``ps``), the ledger,
and the files on disk. Nothing is probed over the network and no state is
invented. If the process table cannot be read, that is reported as
``process_scan: unavailable`` rather than treated as "nothing is running".
"""

from __future__ import annotations

import calendar
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .entities import Entity
from .ledger import Event, Ledger, utc_now
from .registry import Registry

__all__ = ["report", "format_report", "resolve_entity", "scan_processes"]

#: Runtime -> the binary that can launch a file entrypoint. Empty means the
#: entrypoint is a compiled artifact and is executed directly.
_RUNTIME_BIN: Dict[str, str] = {
    "python": "python3",
    "python3": "python3",
    "bash": "bash",
    "sh": "sh",
    "shell": "sh",
    "node": "node",
    "js": "node",
    "javascript": "node",
    "deno": "deno",
    "bun": "bun",
    "elixir": "elixir",
    "exs": "elixir",
    "erlang": "erl",
    "ruby": "ruby",
    "go": "go",
    "rust": "rustc",
    "cpp": "",
    "c": "",
    "binary": "",
    "exe": "",
}

_MODULE = re.compile(r"^-m\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")


def _epoch(ts: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def _age(seconds: Optional[int]) -> str:
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def resolve_entity(
    entity: Entity,
    root: str | Path,
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Tuple[Optional[bool], str]:
    """Can this entity's runtime and entrypoint be resolved here?

    Returns ``(True|False|None, reason)``. ``None`` means the entity is not
    independently runnable (a persona), which is different from broken.
    """
    which = which or shutil.which
    root = Path(root)

    if not entity.is_runnable:
        return None, "persona: not independently runnable"

    entrypoint = (entity.entrypoint or "").strip()
    if not entrypoint:
        return False, "no entrypoint"

    runtime = (entity.runtime or "python").lower()
    module = _MODULE.match(entrypoint)
    if module:
        name = module.group(1)
        binary = entity.interpreter or _RUNTIME_BIN.get(runtime, runtime)
        if binary and not which(binary):
            if not (binary.startswith("python") and sys.executable):
                return False, f"runtime missing: {binary}"
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        return (True, f"module {name}") if found else (False, f"module not found: {name}")

    if entrypoint.startswith("-"):
        return False, f"unsupported entrypoint: {entrypoint}"

    path = Path(entrypoint)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return False, f"missing {entrypoint}"

    binary = entity.interpreter or _RUNTIME_BIN.get(runtime)
    if binary:
        if not which(binary):
            return False, f"runtime missing: {binary}"
        return True, f"{binary} · {entrypoint}"

    if not os.access(path, os.X_OK):
        return False, f"not executable: {entrypoint}"
    return True, entrypoint


def scan_processes() -> Tuple[List[Tuple[int, str]], str]:
    """One process-table snapshot. Returns ``(rows, "ok"|"unavailable")``."""
    binary = shutil.which("ps")
    if not binary:
        return [], "unavailable"
    try:
        proc = subprocess.run(
            [binary, "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return [], "unavailable"
    if proc.returncode != 0:
        return [], "unavailable"

    rows: List[Tuple[int, str]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), parts[1]))
    return rows, "ok"


def _process_for(entity: Entity, processes: Sequence[Tuple[int, str]]) -> Optional[int]:
    entrypoint = (entity.entrypoint or "").strip()
    if not entrypoint:
        return None
    if entrypoint.startswith("-m "):
        for pid, args in processes:
            if entrypoint in args:
                return pid
        return None
    # Match the basename on a boundary, not as a loose substring: "api.py"
    # must not be reported active because some editor has "my_api.py" open.
    name = Path(entrypoint).name
    pattern = re.compile(rf"(?:^|[\s/]){re.escape(name)}(?=$|[\s\"'])")
    for pid, args in processes:
        if pattern.search(args):
            return pid
    return None


def _evidence(events: Iterable[Event], entity_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Last observation and last heartbeat for one entity, from the ledger."""
    observed: Optional[str] = None
    heartbeat: Optional[str] = None
    for event in events:
        named = (
            event.actor == entity_id
            or str(event.data.get("entity") or "") == entity_id
            or str(event.data.get("emitter") or "") == entity_id
        )
        if named:
            observed = event.ts
            if event.type == "heartbeat":
                heartbeat = event.ts
    return observed, heartbeat


def report(
    root: str | Path,
    registry: Registry,
    ledger: Optional[Ledger] = None,
    *,
    processes: Optional[Sequence[Tuple[int, str]]] = None,
    now: Optional[float] = None,
    observed_window_s: float = 86400.0,
    active_window_s: float = 300.0,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Build the four-column report. Read-only; never starts or stops anything."""
    root = Path(root)
    ledger = ledger or Ledger(root / "logs" / "swarm.jsonl")
    now = time.time() if now is None else float(now)

    if processes is None:
        processes, scan = scan_processes()
    else:
        scan = "injected"
    events = list(ledger.read_all())

    rows: List[Dict[str, Any]] = []
    for entity in registry:
        available, available_reason = resolve_entity(entity, root, which=which)
        observed_at, heartbeat_at = _evidence(events, entity.id)

        observed_age: Optional[int] = None
        if observed_at:
            stamp = _epoch(observed_at)
            if stamp is not None:
                observed_age = max(0, int(now - stamp))

        heartbeat_age: Optional[int] = None
        if heartbeat_at:
            stamp = _epoch(heartbeat_at)
            if stamp is not None:
                heartbeat_age = max(0, int(now - stamp))

        active = False
        active_reason = ""
        pid = _process_for(entity, processes)
        if pid is not None:
            active = True
            active_reason = f"pid {pid}"
        elif heartbeat_age is not None and heartbeat_age <= active_window_s:
            active = True
            active_reason = f"heartbeat {heartbeat_age}s"

        rows.append(
            {
                "id": entity.id,
                "display": entity.display,
                "slot": entity.slot,
                "kind": entity.kind,
                "parent": entity.parent,
                "declared": True,
                "available": available,
                "available_reason": available_reason,
                "active": active,
                "active_reason": active_reason,
                "observed_at": observed_at,
                "observed_age_s": observed_age,
                "observed_recent": observed_age is not None and observed_age <= observed_window_s,
            }
        )

    summary = {
        "declared": len(rows),
        "runnable": sum(1 for row in rows if row["available"] is not None),
        "available": sum(1 for row in rows if row["available"] is True),
        "unavailable": sum(1 for row in rows if row["available"] is False),
        "not_runnable": sum(1 for row in rows if row["available"] is None),
        "active": sum(1 for row in rows if row["active"]),
        "observed": sum(1 for row in rows if row["observed_recent"]),
        "ever_observed": sum(1 for row in rows if row["observed_at"] is not None),
    }
    return {
        "checked_at": utc_now(),
        "root": str(root),
        "process_scan": scan,
        "observed_window_s": observed_window_s,
        "active_window_s": active_window_s,
        "entities": rows,
        "summary": summary,
    }


def format_report(payload: Dict[str, Any]) -> str:
    """Plain, bounded output. No colour, no bars, no animation."""
    summary = payload["summary"]
    hours = payload["observed_window_s"] / 3600.0
    lines = [
        f"{'ENTITY':<22}{'SLOT':<5}{'KIND':<9}{'DECL':<6}{'AVAIL':<7}{'ACTIVE':<8}{'OBSERVED':<10}DETAIL",
    ]
    for row in payload["entities"]:
        available = "yes" if row["available"] is True else ("n/a" if row["available"] is None else "no")
        active = "yes" if row["active"] else "-"
        observed = "never" if row["observed_at"] is None else _age(row["observed_age_s"])
        if row["active"]:
            detail = row["active_reason"]
        elif row["available"] is not True:
            detail = row["available_reason"]
        else:
            detail = ""
        lines.append(
            f"{row['id'][:21]:<22}{str(row['slot'] or '-'):<5}{row['kind']:<9}"
            f"{'yes':<6}{available:<7}{active:<8}{observed:<10}{detail[:48]}"
        )
    lines.append("")
    lines.append(
        f"{summary['declared']} declared · {summary['available']} available · "
        f"{summary['not_runnable']} n/a · {summary['unavailable']} unavailable · "
        f"{summary['active']} active · {summary['observed']} observed in {hours:g}h · "
        f"process scan {payload['process_scan']}"
    )
    return "\n".join(lines)

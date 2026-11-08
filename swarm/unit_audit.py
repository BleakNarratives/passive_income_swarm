"""
swarm.unit_audit — does the thing that actually runs on the box use the guard?
==============================================================================

`run_paid_worker.sh` is only worth writing if it is what systemd starts. It is
entirely possible to have a correct guard, a correct launcher, and a unit file
that still executes `dvm_worker.py` directly — in which case the guard is
decoration and the worker happily serves unpaid jobs.

So this module reads the unit file itself. It parses `ExecStart` rather than
grepping, because `grep run_paid_worker` matches a commented-out line, an
`Environment=` value, or the path of a log file, and reports success for all
three.

Read-only. It never imports, starts or stops a service.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "GUARD_SCRIPT",
    "DIRECT_ENTRYPOINTS",
    "parse_unit",
    "audit",
    "find_units",
    "format_report",
]

#: The wrapper that sets DVM_PAID_ONLY and runs the fail-closed guard.
GUARD_SCRIPT = "run_paid_worker.sh"

#: Entry points that bypass the wrapper. Starting any of these directly means
#: paid mode depends on whatever environment happens to be set, not on policy.
DIRECT_ENTRYPOINTS = ("paid_mode_guard.py", "dvm_worker.py")

GUARDED = "guarded"
UNGUARDED = "unguarded"
ABSENT = "absent"

#: Where a user unit normally lives. Only used to suggest a path.
DEFAULT_UNIT = "~/.config/systemd/user/dvm-worker.service"


def parse_unit(text: str) -> Dict[str, Dict[str, str]]:
    """Parse a systemd unit into ``{section: {key: value}}``.

    Only what auditing needs: last key wins, comments are dropped, `%` specifiers
    are left alone, and repeated keys become a space-joined list so several
    `ExecStartPre=` lines do not hide the real `ExecStart=`.
    """
    sections: Dict[str, Dict[str, str]] = {}
    current = "Unit"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        bucket = sections.setdefault(current, {})
        if key in bucket and value:
            bucket[key] = bucket[key] + " " + value
        elif key not in bucket or value:
            bucket[key] = value
    return sections


def _exec_tokens(value: str) -> List[str]:
    """Split an ExecStart value into tokens, keeping `-`/`@`/`+` prefixes off."""
    return [tok.strip("\"'") for tok in value.split() if tok.strip("\"'")]


def audit(path: str | Path, *, live: bool = False) -> Dict[str, Any]:
    """Audit one unit file. Never raises; a missing file is ``absent``."""
    target = Path(path).expanduser()
    result: Dict[str, Any] = {
        "path": str(target),
        "exists": target.is_file(),
        "verdict": ABSENT,
        "exec_start": "",
        "environment_file": "",
        "uses_wrapper": False,
        "bypasses_wrapper": False,
        "notes": [],
    }
    if not target.is_file():
        result["notes"].append(
            f"no unit at this path — write one from deploy/dvm-worker.service, "
            f"or pass --path (default is {DEFAULT_UNIT})"
        )
        return result

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["notes"].append(f"unreadable: {type(exc).__name__}")
        return result

    sections = parse_unit(text)
    service = sections.get("Service", {})
    exec_start = service.get("ExecStart", "")
    result["exec_start"] = exec_start
    result["environment_file"] = service.get("EnvironmentFile", "")

    tokens = _exec_tokens(exec_start)
    joined = " ".join(tokens)
    result["uses_wrapper"] = any(tok.endswith(GUARD_SCRIPT) for tok in tokens)
    result["bypasses_wrapper"] = (
        not result["uses_wrapper"]
        and any(tok.endswith(entry) for tok in tokens for entry in DIRECT_ENTRYPOINTS)
    )

    if result["uses_wrapper"]:
        result["verdict"] = GUARDED
    elif result["bypasses_wrapper"]:
        result["verdict"] = UNGUARDED
        result["notes"].append(
            "ExecStart runs the worker (or the guard) directly, so DVM_PAID_ONLY "
            "comes from the unit's environment instead of from policy"
        )
    elif exec_start:
        result["notes"].append(
            f"ExecStart does not reference {GUARD_SCRIPT} or a known worker entry point"
        )
    else:
        result["notes"].append("no ExecStart= in [Service] — this unit starts nothing")

    if not result["environment_file"] and result["verdict"] == GUARDED:
        result["notes"].append(
            "no EnvironmentFile= — the launcher will use code defaults for fees "
            "and will not see GROQ_API_KEY"
        )

    if live:
        result["systemctl"] = _systemctl_exec(target.stem)
    return result


def _systemctl_exec(unit: str) -> Dict[str, Any]:
    """Ask systemd what it *actually* has loaded, if systemd is available."""
    binary = shutil.which("systemctl")
    if not binary:
        return {"available": False, "reason": "systemctl not on PATH"}
    try:
        proc = subprocess.run(
            [binary, "--user", "show", unit, "-p", "ExecStart", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": type(exc).__name__}
    if proc.returncode != 0:
        return {
            "available": False,
            "reason": (proc.stderr or "unit not loaded").strip()[:200],
        }
    return {"available": True, "exec_start": proc.stdout.strip()[:500]}


def find_units(home: Optional[Path] = None) -> List[Path]:
    """Unit files that plausibly belong to this project, in load order."""
    base = (home or Path.home()) / ".config" / "systemd" / "user"
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.service"))


def format_report(result: Dict[str, Any]) -> str:
    verdict = result.get("verdict", ABSENT)
    icon = {GUARDED: "✓", UNGUARDED: "✗", ABSENT: "?"}[verdict]
    lines = [f"{icon} {verdict}: {result.get('path')}"]
    if result.get("exec_start"):
        lines.append(f"  ExecStart      {result['exec_start']}")
    if result.get("environment_file"):
        lines.append(f"  EnvironmentFile {result['environment_file']}")
    if verdict == UNGUARDED:
        lines.append("  the paid-mode guard is not in this unit's start path")
    for note in result.get("notes", []):
        lines.append(f"  note: {note}")
    live = result.get("systemctl") or {}
    if live.get("available"):
        lines.append(f"  systemd        {live.get('exec_start', '')}")
    elif live:
        lines.append(f"  systemd        unavailable ({live.get('reason', '')})")
    return "\n".join(lines)

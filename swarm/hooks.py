"""
swarm.hooks — the five core_framework reconciliation hooks, as data
====================================================================

`docs/JANUS.md` ends with a five-item reconciliation checklist, written before
anyone had read `core_framework`. Now that it has been read, two of those items
name files that do not exist and two name files that do, under different names.
Guessing at that boundary is how integrations rot, so this module records the
mapping as data and probes it:

===============  =================================  ======================
hook             real target in core_framework      state
===============  =================================  ======================
graft_guard      `bootstrap/cannibal_context.py`     no target (see below)
adaptive_memory  `shared/adaptive_memory.py`         wired (shape B reader)
domino_preflight `bin/domino.sh`                     action required
scout_registry   *(none)*                            no target
tests            `bash ./run_tests.sh` (this repo)    action required
===============  =================================  ======================

`humane_cannibal.py` does not exist in `core_framework`; the nearest thing is
`bootstrap/cannibal_context.py`, which bootstraps session context and has no
deprecate/archive path to hook into. `SCOUT_REGISTRY.json` is likewise absent —
`core_framework`'s registry is `shared/kernel_bus.py::bus_registry`, a
description of bus roles, not a digest registry of executed scripts. Reporting
those honestly beats inventing an integration that would never fire.

The probe is read-only: it looks for marker strings in the checkout and never
imports, executes or mutates anything from another repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["Hook", "HookReport", "HOOKS", "probe", "WIRED", "ACTION", "NO_TARGET"]

WIRED = "wired"
ACTION = "action_required"
NO_TARGET = "no_target"

# Directories that are never a source of truth about the framework.
_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", "output", "logs"}


@dataclass(frozen=True)
class Hook:
    id: str
    title: str
    target: str
    markers: tuple
    command: str
    note: str = ""


HOOKS: tuple[Hook, ...] = (
    Hook(
        id="graft_guard",
        title="call GraftGuard.execute in the deprecate/archive path",
        target="",
        markers=("GraftGuard", "pre_graft", "merge_pheromones"),
        command="",
        note=(
            "humane_cannibal.py was never written. The nearest file, "
            "bootstrap/cannibal_context.py, is a session banner with no "
            "deprecate/archive path, so there is nothing to hook. The guard is "
            "ready (swarm/graft.py) and belongs where a module is retired."
        ),
    ),
    Hook(
        id="adaptive_memory",
        title="read the adaptive_memory.json shape core_framework actually writes",
        target="shared/adaptive_memory.py",
        markers=('"records"', "records", "YieldRecord"),
        command="",
        note=(
            "GraftGuard now reads the real {'records': {...}} shape as well as the "
            "documented {'modules': {...}} one; module identity is data_source."
        ),
    ),
    Hook(
        id="domino_preflight",
        title="call IngestBreaker.preflight_pass when domino.sh preflight succeeds",
        target="bin/domino.sh",
        markers=("breaker preflight", "swarmctl breaker", "preflight_pass"),
        command="swarmctl breaker preflight",
        note=(
            "domino.sh has a preflight stage but nothing calls back into the swarm, "
            "so a locked bus never learns the operator's checks passed."
        ),
    ),
    Hook(
        id="scout_registry",
        title="call ScoutRegistry.record_clean_run on a clean scout exit",
        target="",
        markers=("record_clean_run", "SCOUT_REGISTRY"),
        command="swarmctl discovery register <script>",
        note=(
            "no counter-designate in core_framework: SCOUT_REGISTRY.json does not "
            "exist and bin/scout_vehicle.py never registers. Either core_framework "
            "starts calling record_clean_run, or the registry only covers scripts "
            "run through this repo."
        ),
    ),
    Hook(
        id="tests",
        title="run the suite and report the real number",
        target="",
        markers=(),
        command="bash ./run_tests.sh",
        note="",
    ),
)


@dataclass
class HookReport:
    hook: str
    title: str
    state: str
    target: str = ""
    evidence: str = ""
    command: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hook": self.hook,
            "title": self.title,
            "state": self.state,
            "target": self.target,
            "evidence": self.evidence,
            "command": self.command,
            "note": self.note,
        }


def _search(root: Path, markers: tuple, prefer: Optional[str] = None) -> Optional[str]:
    """Return the first *executable* file containing any marker, or None.

    Only ``.py`` and ``.sh`` count. A hook is wired when code calls it; a
    document that mentions the name is a claim about wiring, and this module
    exists to stop treating claims as evidence.

    ``prefer`` is checked first so the evidence names the file that actually
    matters instead of whichever unrelated module happened to use the word.
    """
    if not markers:
        return None
    ordered: list[Path] = []
    if prefer:
        preferred = Path(root) / prefer
        if preferred.is_file():
            ordered.append(preferred)
    for path in sorted(root.rglob("*")):
        if path not in ordered:
            ordered.append(path)
    for path in ordered:
        if not path.is_file() or path.suffix not in {".py", ".sh"}:
            continue
        if any(part in _SKIP for part in path.parts):
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(marker in text for marker in markers):
            try:
                return str(path.relative_to(root))
            except ValueError:
                return str(path)
    return None


def probe(core_root: Optional[str | Path] = None) -> List[HookReport]:
    """Read-only status of every reconciliation hook. Never raises."""
    root = Path(core_root) if core_root else None
    present = bool(root and root.is_dir())
    reports: List[HookReport] = []

    for hook in HOOKS:
        if hook.id == "tests":
            reports.append(
                HookReport(
                    hook=hook.id,
                    title=hook.title,
                    state=ACTION,
                    evidence="the suite is the only proof that a hook change is safe",
                    command=hook.command,
                    note=hook.note,
                )
            )
            continue

        if not present:
            reports.append(
                HookReport(
                    hook=hook.id,
                    title=hook.title,
                    state=NO_TARGET,
                    target=hook.target,
                    evidence="core_framework checkout not supplied (pass --core-root)",
                    command=hook.command,
                    note=hook.note,
                )
            )
            continue

        target_state = NO_TARGET
        evidence = ""
        if hook.target:
            if (Path(root) / hook.target).exists():
                target_state = ACTION
                evidence = f"{hook.target} exists, no call to this repo yet"
            else:
                evidence = f"{hook.target} is not in this checkout"

        # A marker in executable code means somebody already wired it.
        found = _search(Path(root), hook.markers, hook.target or None)
        if found:
            target_state = WIRED
            evidence = f"found {hook.markers[0]} in {found}"

        reports.append(
            HookReport(
                hook=hook.id,
                title=hook.title,
                state=target_state,
                target=hook.target,
                evidence=evidence,
                command=hook.command,
                note=hook.note,
            )
        )
    return reports


def summary(reports: List[HookReport]) -> Dict[str, int]:
    counts = {WIRED: 0, ACTION: 0, NO_TARGET: 0}
    for report in reports:
        counts[report.state] = counts.get(report.state, 0) + 1
    return counts


def to_json(reports: List[HookReport], core_root: Optional[str] = None) -> str:
    return json.dumps(
        {
            "core_root": str(core_root) if core_root else None,
            "summary": summary(reports),
            "hooks": [r.to_dict() for r in reports],
        },
        indent=2,
        sort_keys=True,
    )

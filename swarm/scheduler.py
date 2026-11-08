"""
swarm.scheduler — cron health, and a cadence that settles
=========================================================

Two problems, one module.

**Health.** Cron fails silently by design: a typo'd command, a missing
interpreter, an expired credential all end up as an empty log and a growing
feeling that something is wrong. :func:`status` reads the installed crontab, the
per-job log artifacts and the last line of each, and says which jobs are
installed, stale or failing.

**Cadence.** "Run more often when it matters" is easy to say and easy to get
wrong: the naive version chases noise, rewriting the schedule every minute,
which is worse than a fixed interval. So this does three things that make it
converge instead of oscillate:

* **A ladder, not a continuum.** Intervals are chosen from a small ordered set
  (1, 2, 5, 10, 15, 30, 60 min…). Small changes in the climate land on the same
  rung, so nothing moves for nothing.
* **A deadband.** A candidate interval has to be at least 1.5× faster or 1.5×
  slower than the current one to be worth touching.
* **Dwell time.** After a change, nothing else moves for ``DWELL_S`` (6 h)
  unless the situation is critical — a paid customer is owed work, or the
  ledger's invariants are failing. Those bypass the wait, because they are the
  reason the schedule exists.

The result is a schedule that finds a resting place and stays there, and moves
when the facts move — a moving point it can track without hunting around it.

Climate is measured from what this system already knows, not from vibes: job
traffic, the owed backlog, integrity failures, degraded connections, scout intel
on the bus, and optionally a keyless market probe (`--market QUERY`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "JOBS",
    "MARK_BEGIN",
    "MARK_END",
    "DWELL_S",
    "DEADBAND",
    "climate",
    "status",
    "tune",
    "render_block",
    "parse_block",
    "format_report",
    "health_report",
    "SchedulerError",
    "STATE_FILE",
]

MARK_BEGIN = "# >>> passive_income_swarm >>>"
MARK_END = "# <<< passive_income_swarm <<<"

STATE_FILE = "logs/schedule-state.json"

#: Minimum time between cadence changes, unless something is critical.
DWELL_S = 6 * 3600

#: A change must be at least this factor faster/slower to be worth making.
DEADBAND = 1.6


class SchedulerError(RuntimeError):
    """Crontab could not be read or written."""


@dataclass(frozen=True)
class Job:
    id: str
    label: str
    #: Intervals in minutes, fastest first. The ladder is the anti-thrash device.
    ladder: Sequence[int]
    #: Which artifact proves the job ran, relative to the repo root.
    artifact: str
    #: CLI subcommand, when it differs from the job id (probe → connections).
    cli: str = ""
    #: Extra CLI arguments.
    args: str = ""
    #: Redirect stdout of this job somewhere useful instead of a log.
    redirect: str = ""
    #: Climate axis that makes this job urgent, and how much it matters.
    axis: str = "demand"


JOBS: Sequence[Job] = (
    Job("bridge", "console facts → bus", (1, 2, 5, 10, 15, 30), "logs/cron-bridge.log", axis="demand"),
    Job("ingest", "drain the bus", (1, 2, 5, 10, 15, 30), "logs/cron-ingest.log", axis="intel"),
    Job(
        "probe",
        "live connection probes",
        (15, 30, 60, 120, 240),
        "logs/cron-connections.log",
        cli="connections",
        args="--probe --save",
        axis="trouble",
    ),
    Job(
        "readiness",
        "readiness snapshot",
        (15, 30, 60, 120),
        "logs/readiness.json",
        args="--fast --json",
        redirect="logs/readiness.json",
        axis="trouble",
    ),
    Job(
        "tune",
        "re-tune this schedule",
        (60,),
        "logs/cron-tune.log",
        cli="cron",
        args="tune --apply",
        axis="none",
    ),
)

_JOBS_BY_ID = {job.id: job for job in JOBS}
_JOB_TAG = re.compile(r"#\s*job:([a-z_]+)\s*$")
_MINUTE_FIELD = re.compile(r"^(?:\*/(\d+)|(\d+)|\*)$")


# ---------------------------------------------------------------------------
# crontab plumbing
# ---------------------------------------------------------------------------
def read_crontab() -> str:
    binary = shutil.which("crontab")
    if not binary:
        raise SchedulerError("crontab is not installed on this machine")
    try:
        proc = subprocess.run([binary, "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SchedulerError(f"crontab -l failed: {type(exc).__name__}") from exc
    # crontab -l exits 1 when the table is empty; that is not an error here.
    if proc.returncode != 0 and "no crontab" not in (proc.stderr or "").lower():
        if proc.returncode != 1:
            raise SchedulerError(f"crontab -l failed: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout or ""


def write_crontab(text: str) -> None:
    binary = shutil.which("crontab")
    if not binary:
        raise SchedulerError("crontab is not installed on this machine")
    try:
        proc = subprocess.run([binary, "-"], input=text, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SchedulerError(f"crontab - failed: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise SchedulerError(f"crontab - failed: {(proc.stderr or '').strip()[:200]}")


def cron_daemon() -> str:
    """"running" | "not_running" | "unknown" — a silent cron is the common failure."""
    if not shutil.which("pgrep"):
        return "unknown"
    for name in ("cron", "crond"):
        try:
            proc = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return "running"
    return "not_running"


# ---------------------------------------------------------------------------
# block parsing / rendering
# ---------------------------------------------------------------------------
def _interval_from_line(line: str) -> Optional[int]:
    """Read the interval (minutes) out of the minute field of a cron line."""
    field = line.split()[0] if line.split() else ""
    match = _MINUTE_FIELD.match(field)
    if not match:
        return None
    if field == "*":
        return 1
    if match.group(1):
        return int(match.group(1))
    if match.group(2):
        # "17" or "0" means once an hour, except hour fields are checked below.
        return 60
    return None


def parse_block(text: str) -> Dict[str, Dict[str, Any]]:
    """Find ``# job:<id>`` tagged lines and return their schedule."""
    out: Dict[str, Dict[str, Any]] = {}
    for line in (text or "").splitlines():
        match = _JOB_TAG.search(line)
        if not match:
            continue
        job_id = match.group(1)
        interval = _interval_from_line(line)
        # "0 */2 * * *" is every 2 hours; the minute field is 0.
        fields = line.split()
        if interval == 60 and len(fields) > 1 and "/" in fields[1]:
            try:
                interval = 60 * int(fields[1].split("/")[1])
            except (ValueError, IndexError):
                interval = 60
        out[job_id] = {"line": line, "interval": interval or 60}
    return out


def _schedule_for(interval: int) -> str:
    if interval < 60:
        return f"*/{max(1, int(interval))} * * * *"
    hours = max(1, int(round(interval / 60)))
    return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"


def render_block(
    repo: str | Path,
    python: str,
    intervals: Dict[str, int],
    *,
    flock: Optional[str] = None,
) -> str:
    """Render the managed crontab block. Every line carries ``# job:<id>``."""
    repo = str(repo)
    lines = [
        f"{MARK_BEGIN} (managed by swarmctl cron tune — edit there, not here)",
        "SHELL=/bin/bash",
        "PATH=/usr/local/bin:/usr/bin:/bin",
    ]
    for job in JOBS:
        interval = int(intervals.get(job.id, job.ladder[-1]))
        command = f"{python} -m swarm.cli {job.cli or job.id}"
        if job.args:
            command += f" {job.args}"
        if job.redirect:
            # stderr must never land in the file stdout is rewriting, or the JSON
            # readiness snapshot becomes "{...}Traceback (most recent call last)".
            stderr = job.artifact if job.artifact != job.redirect else f"logs/cron-{job.id}.log"
            command += f" > {repo}/{job.redirect} 2>> {repo}/{stderr}"
        else:
            command += f" >> {repo}/{job.artifact} 2>&1"
        prefix = f"cd {repo} && "
        if flock and job.id != "readiness":
            prefix += f"{flock} -n /tmp/pis-{job.id}.lock "
        lines.append(f"{_schedule_for(interval)} {prefix}{command}  # job:{job.id}")
    lines.append(MARK_END)
    return "\n".join(lines) + "\n"


def _strip_block(text: str) -> str:
    keep: List[str] = []
    skipping = False
    for line in (text or "").splitlines():
        if line.startswith(MARK_BEGIN) or MARK_BEGIN in line:
            skipping = True
            continue
        if MARK_END in line:
            skipping = False
            continue
        if not skipping:
            keep.append(line)
    while keep and not keep[-1].strip():
        keep.pop()
    return "\n".join(keep)


def _merge(existing: str, block: str) -> str:
    base = _strip_block(existing)
    return (base + "\n" if base else "") + block


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
_ERROR_MARKERS = ("traceback", "error:", "exception", "failed", "command not found", "no such file")


def _last_line(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def status(
    root: str | Path = ".",
    *,
    crontab_text: Optional[str] = None,
    now: Optional[float] = None,
    daemon: Optional[str] = None,
) -> Dict[str, Any]:
    """Per-job health: installed, last run, stale, failing. Never raises."""
    base = Path(root)
    now = time.time() if now is None else float(now)
    problems: List[str] = []
    try:
        text = read_crontab() if crontab_text is None else crontab_text
        readable = True
    except SchedulerError as exc:
        text, readable = "", False
        problems.append(str(exc))

    installed = parse_block(text) if readable else {}
    rows: List[Dict[str, Any]] = []
    for job in JOBS:
        entry = installed.get(job.id)
        artifact = base / job.artifact
        exists = artifact.is_file()
        age = (now - artifact.stat().st_mtime) if exists else None
        interval = (entry or {}).get("interval") or job.ladder[-1]
        last = _last_line(artifact) if exists else ""

        if not entry:
            state = "not_installed"
        elif not exists:
            state = "never_run"
        elif last and any(marker in last.lower() for marker in _ERROR_MARKERS):
            state = "failing"
        elif age is not None and age > max(3 * interval * 60, 900):
            state = "stale"
        else:
            state = "ok"

        if entry and state in ("failing", "stale", "never_run"):
            problems.append(f"{job.id}: {state}")
        if state == "not_installed":
            problems.append(f"{job.id}: not in the crontab")
        rows.append(
            {
                "id": job.id,
                "label": job.label,
                "state": state,
                "interval_min": interval,
                "installed_interval": (entry or {}).get("interval"),
                "last_run_s": None if age is None else round(age, 1),
                "last_line": last,
            }
        )

    return {
        "available": readable,
        "daemon": daemon if daemon is not None else (cron_daemon() if readable else "unknown"),
        "installed": bool(installed),
        "jobs": rows,
        "problems": problems,
        "healthy": readable and not problems,
    }


def health_report(report: Dict[str, Any]) -> str:
    if not report.get("available"):
        lines = ["cron: could not read the crontab"]
        for problem in report.get("problems") or []:
            lines.append(f"  ! {problem}")
        lines.append("  install with: bash tools/bootstrap.sh")
        return "\n".join(lines)

    mark = {"ok": "ok  ", "stale": "STALE", "failing": "FAIL", "never_run": "NEVER", "not_installed": "NONE"}
    lines = [
        f"daemon: {report.get('daemon')}   installed: {'yes' if report.get('installed') else 'no'}   "
        f"healthy: {'yes' if report.get('healthy') else 'no'}",
        "",
        f"{'JOB':<11}{'STATE':<7}{'EVERY':<8}{'LAST RUN':<12}DETAIL",
    ]
    for row in report.get("jobs") or []:
        age = row.get("last_run_s")
        last = "never" if age is None else f"{int(age)}s ago"
        detail = "" if row["state"] == "ok" else (row.get("last_line") or "")[:70]
        lines.append(
            f"{row['id']:<11}{mark.get(row['state'], row['state']):<7}"
            f"{str(row['interval_min']) + 'm':<8}{last:<12}{detail}"
        )
    if report.get("daemon") == "not_running":
        lines.append("")
        lines.append("no cron daemon is running — entries will never fire")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# climate
# ---------------------------------------------------------------------------
def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def climate(
    root: str | Path = ".",
    *,
    snapshot: Optional[Dict[str, Any]] = None,
    market_query: Optional[str] = None,
    now: Optional[float] = None,
    timeout: float = 6.0,
) -> Dict[str, Any]:
    """Take the temperature: traffic, backlog, trouble, scout intel, market."""
    base = Path(root)
    now = time.time() if now is None else float(now)
    if snapshot is None:
        try:
            from dashboard.feed import snapshot as build

            snapshot = build(base)
        except Exception:  # noqa: BLE001
            snapshot = {}

    money = snapshot.get("money") or {}
    counters = money.get("counters") or {}
    jobs = ((snapshot.get("jobs") or {}).get("recent")) or []

    def _ts(job):
        try:
            return float(job.get("ts") or 0)
        except (TypeError, ValueError):
            return 0.0

    recent = [j for j in jobs if now - _ts(j) <= 3600 and _ts(j) > 0]
    demand = _clamp(len(recent) / 12.0)
    owed = int(counters.get("unserved_paid", 0) or 0)
    backlog = _clamp(owed / 3.0)
    failures = [c for c in (money.get("integrity") or []) if not c.get("ok")]
    degraded = int(((snapshot.get("connections") or {}).get("summary") or {}).get("degraded", 0) or 0)
    trouble = _clamp(0.6 * len(failures) + 0.2 * degraded)

    intel, intel_conf = 0, 0.0
    try:
        from swarm.contracts import Envelope

        bus = base / "bus.jsonl"
        if bus.is_file():
            cutoff = now - 6 * 3600
            for line in bus.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
                if '"intel"' not in line:
                    continue
                try:
                    env = Envelope.from_dict(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
                if env.kind != "intel":
                    continue
                try:
                    stamp = time.mktime(time.strptime(env.created, "%Y-%m-%dT%H:%M:%SZ"))
                except (ValueError, TypeError):
                    stamp = now
                if stamp >= cutoff:
                    intel += 1
                    intel_conf += env.confidence
    except Exception:  # noqa: BLE001
        pass
    intel_pressure = _clamp(intel / 10.0)

    market: Dict[str, Any] = {"queried": False}
    if market_query:
        try:
            from .market_search import search

            report = search(market_query, timeout=timeout)
            hits = len(report.get("hits") or [])
            market = {
                "queried": True,
                "query": market_query,
                "hits": hits,
                "sources_ok": sum(1 for s in (report.get("sources") or {}).values() if s.get("ok")),
            }
            intel_pressure = _clamp(intel_pressure + min(0.3, hits / 30.0))
        except Exception as exc:  # noqa: BLE001
            market = {"queried": True, "query": market_query, "error": type(exc).__name__}

    temperature = _clamp(0.45 * demand + 0.25 * backlog + 0.20 * trouble + 0.10 * intel_pressure)
    return {
        "temperature": round(temperature, 3),
        "demand": round(demand, 3),
        "backlog": round(backlog, 3),
        "backlog_jobs": owed,
        "trouble": round(trouble, 3),
        "integrity_failures": len(failures),
        "degraded_connections": degraded,
        "intel": intel,
        "intel_mean_confidence": round(intel_conf / intel, 2) if intel else 0.0,
        "intel_pressure": round(intel_pressure, 3),
        "jobs_last_hour": len(recent),
        "critical": bool(owed or failures),
        "market": market,
    }


# ---------------------------------------------------------------------------
# tuning
# ---------------------------------------------------------------------------
def _pressure_for(job: Job, measures: Dict[str, Any]) -> float:
    if job.axis == "none":
        return 0.0
    if job.axis == "trouble":
        return _clamp(0.75 * measures["trouble"] + 0.25 * measures["intel_pressure"])
    if job.axis == "intel":
        return _clamp(max(measures["intel_pressure"], measures["backlog"]))
    return _clamp(max(measures["demand"], measures["backlog"], 0.5 * measures["trouble"]))


def _rung(job: Job, pressure: float) -> int:
    """Quantise pressure onto the ladder. High pressure picks a faster rung.

    A step function, not a linear interpolation on purpose: interpolating meant a
    0.2 "trouble" reading moved the schedule two rungs, which is the churn this
    module exists to avoid. With buckets, a pressure has to cross a real boundary
    to change anything.
    """
    ladder = list(job.ladder)
    steps = len(ladder)
    bucket = min(steps - 1, max(0, int(_clamp(pressure) * steps)))
    return int(ladder[steps - 1 - bucket])


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def tune(
    root: str | Path = ".",
    *,
    apply: bool = False,
    market_query: Optional[str] = None,
    now: Optional[float] = None,
    state_path: Optional[str] = None,
    crontab_text: Optional[str] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    write=None,
    python: Optional[str] = None,
    dwell_s: int = DWELL_S,
) -> Dict[str, Any]:
    """Decide the cadence; with ``apply``, install it. Converges by construction."""
    base = Path(root)
    now = time.time() if now is None else float(now)
    python = python or shutil.which("python3") or "python3"
    flock = "flock" if shutil.which("flock") else None
    state_file = Path(state_path) if state_path else base / STATE_FILE
    state = _load_state(state_file)
    measures = climate(base, snapshot=snapshot, market_query=market_query, now=now)

    try:
        existing = read_crontab() if crontab_text is None else crontab_text
        readable = True
    except SchedulerError:
        existing, readable = "", False
    installed = parse_block(existing)

    decisions: List[Dict[str, Any]] = []
    intervals: Dict[str, int] = {}
    changed = False
    for job in JOBS:
        current = int(installed.get(job.id, {}).get("interval") or state.get(job.id, {}).get("interval") or job.ladder[-1])
        desired = _rung(job, _pressure_for(job, measures))
        last_changed = float(state.get(job.id, {}).get("changed_at", 0) or 0)

        action, reason = "held", ""
        if desired == current:
            reason = f"{measures['temperature']:.2f} climate sits on the {current}m rung"
        else:
            ratio = max(desired / current, current / desired) if current else 99
            if ratio < DEADBAND:
                reason = f"{current}→{desired}m is inside the {DEADBAND}× deadband"
            elif not measures["critical"] and now - last_changed < dwell_s:
                remaining = int((dwell_s - (now - last_changed)) / 60)
                reason = f"dwell: {remaining}m before another move (not critical)"
            else:
                action = "change"
                reason = (
                    f"climate {measures['temperature']:.2f} "
                    f"(demand {measures['demand']:.2f}, backlog {measures['backlog_jobs']}, "
                    f"trouble {measures['trouble']:.2f}) → {desired}m"
                )
                changed = True

        intervals[job.id] = desired if action == "change" else current
        decisions.append(
            {
                "id": job.id,
                "current": current,
                "desired": desired,
                "applied": intervals[job.id] if action == "change" else current,
                "action": action,
                "reason": reason,
            }
        )

    block = render_block(base, python, intervals, flock=flock)
    written = False
    error = ""
    if apply and changed:
        try:
            (write or write_crontab)(_merge(existing, block))
            written = True
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
    elif apply and not changed:
        # Nothing moved, but make sure the block exists and is current.
        if not installed:
            try:
                (write or write_crontab)(_merge(existing, block))
                written = True
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"

    if apply and written:
        for decision in decisions:
            if decision["action"] == "change":
                entry = state.get(decision["id"], {})
                state[decision["id"]] = {
                    "interval": decision["applied"],
                    "changed_at": now,
                    "previous": decision["current"],
                    "history": (entry.get("history") or [])[-4:]
                    + [{"at": int(now), "from": decision["current"], "to": decision["applied"]}],
                }
            else:
                state.setdefault(decision["id"], {})["interval"] = decision["applied"]
        state["last_tune"] = int(now)
        state["temperature"] = measures["temperature"]
        _save_state(state_file, state)

    return {
        "at": int(now),
        "climate": measures,
        "decisions": decisions,
        "changed": changed,
        "applied": written,
        "readable": readable,
        "state_file": str(state_file),
        "block": block,
        "error": error,
    }


def format_report(result: Dict[str, Any]) -> str:
    m = result.get("climate") or {}
    lines = [
        f"climate {m.get('temperature', 0):.2f}  "
        f"(demand {m.get('demand', 0):.2f} · jobs/h {m.get('jobs_last_hour', 0)} · "
        f"backlog {m.get('backlog_jobs', 0)} · trouble {m.get('trouble', 0):.2f} · "
        f"intel {m.get('intel', 0)})",
    ]
    if m.get("critical"):
        lines.append("  critical: someone is owed work or the ledger is failing — dwell bypassed")
    market = m.get("market") or {}
    if market.get("queried"):
        if market.get("error"):
            lines.append(f"  market: probe failed ({market['error']})")
        else:
            lines.append(f"  market: {market.get('hits', 0)} hit(s) for '{market.get('query')}'")
    lines.append("")
    lines.append(f"{'JOB':<11}{'NOW':<7}{'WANT':<7}{'ACTION':<8}REASON")
    for row in result.get("decisions") or []:
        lines.append(
            f"{row['id']:<11}{str(row['current']) + 'm':<7}{str(row['desired']) + 'm':<7}"
            f"{row['action']:<8}{row['reason']}"
        )
    lines.append("")
    if result.get("error"):
        lines.append(f"could not write crontab: {result['error']}")
    elif result.get("applied"):
        lines.append("crontab updated.")
    elif result.get("changed"):
        lines.append("changes recommended — run with --apply to install them.")
    else:
        lines.append("holding: nothing worth changing.")
    return "\n".join(lines)

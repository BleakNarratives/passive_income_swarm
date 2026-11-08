#!/usr/bin/env python3
"""
dashboard.feed — one JSON snapshot of everything worth watching
===============================================================

Read-only by design. Nothing in this module writes to the repository, creates a
ledger, seeds a skill or registers a scout: opening the console must never
change the system it is reporting on.

Sources, all optional and independently degradable:

    nostr_dvm/logs/settlements.db   the money — job-bound invoices and claims
    nostr_dvm/logs/jobs.jsonl       the worker's own job ledger
    logs/swarm.jsonl                control-plane events
    swarm/cabinet.json              the cabinet (occupants and slots)
    bus.jsonl                       pheromone trails (decayed)
    circuit_breaker.json            Janus Guard state

The forecast is not decorative: it runs the swarm's own walk-forward
backtest over daily settled millisats and reports the error, so a projection is
never shown without the evidence for how wrong it has been.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["snapshot", "read_jobs", "outcome_histogram", "funnel"]

TERMINAL_OUTCOMES = {
    "served_free": "served",
    "served_paid": "served",
    "replayed_served": "replayed",
    "payment_timeout": "unpaid",
    "payment_rail_error": "rail_error",
    "invoice_conflict": "refused",
    "input_mismatch": "refused",
    "inference_error": "error",
}

#: Statuses that mean a customer paid and has nothing (yet).
OWED_STATUSES = ("settled", "serving")


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _tail_lines(path: Path, limit: int) -> List[str]:
    """Read the last ``limit`` non-empty lines of a file without loading it all."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, max(4096, limit * 512))
            fh.seek(size - block)
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return lines[-limit:]


def _parse_jsonl(lines: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue  # tolerate a torn final line after a crash
        if isinstance(value, dict):
            out.append(value)
    return out


def read_jobs(path: Path, limit: int = 60) -> List[Dict[str, Any]]:
    """Most recent job records from the worker's JSONL ledger, oldest first."""
    return _parse_jsonl(_tail_lines(path, limit))


def outcome_histogram(jobs: List[Dict[str, Any]]) -> Dict[str, int]:
    hist: Dict[str, int] = {}
    for job in jobs:
        outcome = str(job.get("outcome") or "unknown")
        hist[outcome] = hist.get(outcome, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def funnel(counters: Dict[str, int]) -> List[Dict[str, Any]]:
    """Requested → invoiced → settled → served, with the drop-off at each step."""
    requested = max(1, int(counters.get("requested", 0)))
    steps = [
        ("requested", int(counters.get("requested", 0)), "jobs seen in paid mode"),
        ("invoiced", int(counters.get("requested", 0)) - int(counters.get("failed", 0)),
         "invoice minted"),
        ("settled", int(counters.get("settled", 0)), "sats confirmed on-chain"),
        ("served", int(counters.get("served", 0)), "answer published to Nostr"),
    ]
    out: List[Dict[str, Any]] = []
    previous: Optional[int] = None
    for name, value, note in steps:
        drop = None if previous is None else max(0, previous - value)
        out.append(
            {
                "step": name,
                "count": value,
                "pct_of_requested": round(100.0 * value / requested, 1),
                "dropped": drop,
                "note": note,
            }
        )
        previous = value
    return out


def _money_snapshot(settlement_db: Path, now: float) -> Dict[str, Any]:
    """Open the settlement ledger read-only-ish and summarise it.

    The ledger is opened normally (SQLite needs a writable connection to run
    WAL hygiene) but this function only reads. If the file is absent the
    dashboard says so instead of inventing zeroes.
    """
    if not settlement_db.exists():
        # No money has moved yet, but the guarantees still hold. Show the code
        # invariants rather than an empty panel.
        try:
            from nostr_dvm.settlement import policy_checks

            integrity = policy_checks()
        except Exception:
            integrity = []
        return {
            "available": False,
            "path": str(settlement_db),
            "reason": "no settlement ledger yet — nothing has been invoiced",
            "integrity": integrity,
        }

    try:
        from nostr_dvm.settlement import SettlementLedger
    except Exception as exc:  # pragma: no cover - import guard
        return {"available": False, "path": str(settlement_db), "reason": str(exc)}

    ledger = None
    try:
        ledger = SettlementLedger(settlement_db)
        counters = ledger.counters()
        open_invoices = [row.to_dict() for row in ledger.open_invoices(now)][:24]
        owed = [row.to_dict() for row in ledger.unserved_paid()][:24]
        recent = [row.to_dict() for row in ledger.recent(24)]
        income = ledger.income_by_day(days=14, now=now)
        integrity = ledger.integrity()
        requesters = ledger.by_requester(8)
    except Exception as exc:  # pragma: no cover - corrupt database
        return {"available": False, "path": str(settlement_db), "reason": str(exc)}
    finally:
        if ledger is not None:
            ledger.close()

    return {
        "available": True,
        "path": str(settlement_db),
        "counters": counters,
        "open_invoices": open_invoices,
        "owed": owed,
        "recent": recent,
        "income": income,
        "integrity": integrity,
        "requesters": requesters,
        "funnel": funnel(counters),
    }


def _forecast(income: List[Dict[str, Any]], horizon: int = 7) -> Dict[str, Any]:
    """Daily-settled-msat projection from the swarm's own model family."""
    series = [float(bucket.get("msat", 0)) for bucket in income]
    if len(series) < 8 or not any(series):
        return {"available": False, "reason": "not enough settled history to project"}
    try:
        from swarm.forecast import select_best, backtest
    except Exception as exc:  # pragma: no cover
        return {"available": False, "reason": str(exc)}

    try:
        model, board = select_best(series, horizon=1)
        predictions = model.forecast(series, horizon)
        score = backtest(model, series, 1)
        board_rows = sorted(
            [row for row in board["board"] if row.get("rmse") is not None],
            key=lambda row: row["rmse"],
        )[:5]
    except Exception as exc:  # pragma: no cover - degenerate series
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    return {
        "available": True,
        "model": model.name,
        "params": model.describe(),
        "horizon_days": horizon,
        "predictions_msat": [int(max(0.0, p)) for p in predictions],
        "last_observed_msat": int(max(0.0, series[-1])),
        "backtest": score,
        "board": [
            {"model": row["model"], "rmse": row.get("rmse"), "mae": row.get("mae")}
            for row in board_rows
        ],
        "honesty": (
            "Backtest is expanding-window walk-forward on daily settled msat. "
            "A short or flat history makes any projection weak — read RMSE, not the line."
        ),
    }


def _swarm_snapshot(root: Path) -> Dict[str, Any]:
    """Control-plane state, read directly from files (no Context side effects)."""
    cabinet_path = root / "swarm" / "cabinet.json"
    cabinet = _read_json(cabinet_path, {}) or {}
    entities = []
    for entity in cabinet.get("entities", []):
        if not isinstance(entity, dict):
            continue
        entities.append(
            {
                "id": entity.get("id", ""),
                "display": entity.get("display", entity.get("id", "")),
                "slot": entity.get("slot", ""),
                "kind": entity.get("kind", ""),
                "runtime": entity.get("runtime", ""),
                "tags": entity.get("tags", []),
                "role": entity.get("role", ""),
            }
        )

    ledger_events = _parse_jsonl(_tail_lines(root / "logs" / "swarm.jsonl", 400))
    types: Dict[str, int] = {}
    actors: Dict[str, float] = {}
    revenue_events: List[float] = []
    for event in ledger_events:
        types[str(event.get("type", "?"))] = types.get(str(event.get("type", "?")), 0) + 1
        actor = str(event.get("actor") or "system")
        actors[actor] = max(actors.get(actor, 0.0), 0.0)
        if event.get("type") == "complete":
            try:
                revenue_events.append(float(event["data"]["result"]["revenue_msat"]))
            except (KeyError, TypeError, ValueError):
                pass

    # Last time each occupant appeared in the ledger (from the tail we read).
    last_seen: Dict[str, str] = {}
    for event in ledger_events:
        actor = str(event.get("actor") or "")
        if actor:
            last_seen[actor] = str(event.get("ts") or "")
    for entity in entities:
        entity["last_seen"] = last_seen.get(entity["id"], "")

    breaker = _read_json(root / "circuit_breaker.json", {}) or {}

    pheromones: List[Dict[str, Any]] = []
    try:
        from swarm.whorl import WhorlBus

        pheromones = WhorlBus(root / "bus.jsonl").hottest(6)
    except Exception:
        pheromones = []

    skills: Dict[str, Any] = {"available": False}
    registry = _read_json(root / "skills" / "registry.json", None)
    if isinstance(registry, dict):
        entries = registry.get("skills", registry)
        if isinstance(entries, dict):
            rows = list(entries.values())
        elif isinstance(entries, list):
            rows = entries
        else:
            rows = []
        ranked = sorted(
            [r for r in rows if isinstance(r, dict)],
            key=lambda r: float(r.get("score", 0) or 0),
            reverse=True,
        )[:8]
        skills = {
            "available": True,
            "count": len(rows),
            "leaderboard": [
                {
                    "id": r.get("id", "?"),
                    "level": r.get("level", "?"),
                    "score": round(float(r.get("score", 0) or 0), 3),
                    "runs": int(r.get("runs", 0) or 0),
                    "certified": bool(r.get("certified")),
                }
                for r in ranked
            ],
        }

    return {
        "cabinet": {"columns": cabinet.get("cabinet", {}).get("columns", "ABCDEF"),
                    "rows": cabinet.get("cabinet", {}).get("rows", 6),
                    "entities": entities},
        "ledger": {"events_in_tail": len(ledger_events), "types": types,
                   "revenue_events": len(revenue_events)},
        "breaker": {
            "mode": breaker.get("mode", "OPEN"),
            "locked": breaker.get("mode") == "STRICT_INGEST_ONLY",
            "violations_in_window": len(breaker.get("violations") or []),
            "threshold": breaker.get("threshold", 3),
            "locked_at": breaker.get("locked_at"),
        },
        "pheromones": pheromones,
        "skills": skills,
    }


def _connections_snapshot(root: Path) -> Dict[str, Any]:
    """Last cached connection report. The console never probes on its own.

    Probing is an explicit operator action (or a CLI run) because a snapshot
    every two seconds must not hammer six providers.
    """
    path = root / "logs" / "connections.json"
    report = None
    try:
        import json as _json

        report = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    if not isinstance(report, dict):
        return {
            "available": False,
            "path": str(path),
            "reason": "never probed — run `swarmctl connections --probe --save`",
        }
    report = dict(report)
    report["available"] = True
    report["path"] = str(path)
    return report


def _truthy(raw: Any) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _dvm_env(root: Path) -> Dict[str, str]:
    """Read ``nostr_dvm/dvm.env`` the way systemd's EnvironmentFile does.

    The console is a separate process from the worker, so ``os.environ`` here
    says nothing about what the worker was configured to do — it reported
    "reputation mode" on a box that had been charging for days. Reading the file
    is the only way the header can tell the truth. The live environment wins when
    both are present, because a shell export is the narrower, more deliberate act.
    """
    values: Dict[str, str] = {}
    try:
        text = (root / "nostr_dvm" / "dvm.env").read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(name: str, file_values: Dict[str, str], default: str = "") -> tuple:
    """Return (value, source), preferring the live environment."""
    live = os.environ.get(name)
    if live:
        return live, "environment"
    if file_values.get(name):
        return file_values[name], "nostr_dvm/dvm.env"
    return default, ""


def _worker_snapshot(root: Path, jobs: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    last = jobs[-1] if jobs else {}
    last_ts = float(last.get("ts") or 0)
    file_values = _dvm_env(root)
    paid_live = _truthy(os.environ.get("DVM_PAID_ONLY", ""))
    paid_file = _truthy(file_values.get("DVM_PAID_ONLY", ""))
    if paid_live and paid_file:
        source = "environment + nostr_dvm/dvm.env"
    elif paid_live:
        source = "environment"
    elif paid_file:
        source = "nostr_dvm/dvm.env"
    else:
        source = "unset"
    try:
        fees = {
            5002: int(_setting("DVM_FEE_5002_MSAT", file_values, "50000")[0]),
            5003: int(_setting("DVM_FEE_5003_MSAT", file_values, "20000")[0]),
        }
    except (TypeError, ValueError):
        fees = {5002: 50000, 5003: 20000}
    return {
        # What the worker is *configured* to do — this is what the header shows.
        "paid_mode": paid_live or paid_file,
        "paid_mode_source": source,
        # What this console process can see. Both are kept, separately, because
        # conflating them is the bug this replaced.
        "paid_mode_env": paid_live,
        "paid_mode_file": paid_file,
        "fees_msat": fees,
        "rail_configured": (root / "keys" / "nwc.secret").exists(),
        "signing_key_present": (root / "keys" / "nostr.key").exists(),
        # Same reasoning as paid_mode: systemd supplies this, the console does not.
        "groq_key_present": bool(_setting("GROQ_API_KEY", file_values)[0]),
        "jobs_log": str(root / "nostr_dvm" / "logs" / "jobs.jsonl"),
        "last_job_ts": last_ts,
        "seconds_since_last_job": round(max(0.0, now - last_ts), 1) if last_ts else None,
        "relay_count": len(
            [
                line
                for line in (
                    "wss://relay.primal.net", "wss://nos.lol", "wss://relay.damus.io"
                )
            ]
        ),
    }


def snapshot(
    root: str | Path = ".",
    *,
    settlement_db: Optional[str | Path] = None,
    jobs_log: Optional[str | Path] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the whole console payload in one call."""
    root = Path(root).resolve()
    now = time.time() if now is None else float(now)
    settlement_path = Path(settlement_db) if settlement_db else (
        root / "nostr_dvm" / "logs" / "settlements.db"
    )
    jobs_path = Path(jobs_log) if jobs_log else (
        root / "nostr_dvm" / "logs" / "jobs.jsonl"
    )

    jobs = read_jobs(jobs_path, 60)
    money = _money_snapshot(settlement_path, now)
    income = money.get("income") or []

    return {
        "generated_at": now,
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "root": str(root),
        "money": money,
        "forecast": _forecast(income, horizon=7),
        "jobs": {
            "available": bool(jobs),
            "recent": list(reversed(jobs[-40:])),
            "histogram": outcome_histogram(jobs),
            "total_in_tail": len(jobs),
        },
        "worker": _worker_snapshot(root, jobs, now),
        "connections": _connections_snapshot(root),
        "swarm": _swarm_snapshot(root),
    }

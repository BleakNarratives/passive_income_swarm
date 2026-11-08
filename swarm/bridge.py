"""
swarm.bridge — the console's facts, on the bus
==============================================

The console knows things the rest of the swarm does not: how many sats actually
settled, which customers paid and got nothing, whether the ledger's invariants
still hold, which readiness stage is blocking. Until now that knowledge was only
visible to a human looking at a screen.

This module turns a snapshot into :class:`~swarm.contracts.Envelope` records and
publishes them to the WhorlBus, so scouts, engines and the ingestor see the same
facts without scraping the dashboard.

Three properties matter more than the feature:

* **Contract-clean.** Every signal is built through ``Envelope.build``, so it
  carries a valid P2 digest and cannot be the "ad-hoc JSON" Relay 1 forbids.
* **Idempotent.** A signal is published only when its *facts* change. A console
  refreshing every three seconds must not become a firehose; the digest is
  computed over the payload with volatile fields excluded, and remembered in
  ``logs/bridge-state.json``.
* **Honest.** Signals carry what is known and nothing more. A projection is
  tagged low-confidence, an integrity failure is high-confidence, and no signal
  ever contains a credential, a preimage or a secret.

Plug-and-play for things that cannot read a bus: ``--export`` writes a stable
``logs/console-telemetry.json`` with the same facts.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import Envelope, content_digest

__all__ = [
    "Signal",
    "collect",
    "signals",
    "publish",
    "export",
    "format_report",
    "SIGNAL_IDS",
    "EMITTER",
    "STATE_FILE",
    "EXPORT_FILE",
]

#: Who these signals are attributed to. Stable, because consumers filter on it.
EMITTER = "dashboard"

#: Lineage label: these facts originate from the console's own modules.
LINEAGE = "swarm/bridge.py"

STATE_FILE = "logs/bridge-state.json"
EXPORT_FILE = "logs/console-telemetry.json"

SIGNAL_IDS = (
    "money.settled",
    "money.owed",
    "ledger.integrity",
    "worker.mode",
    "readiness.gate",
    "connections.coverage",
    "forecast.projection",
    "jobs.histogram",
    "pheromone.money_owed",
)


@dataclass
class Signal:
    """One publishable fact. ``payload`` must contain facts only, no timestamps."""

    id: str
    kind: str
    payload: Dict[str, Any]
    confidence: float = 1.0
    tags: List[str] = field(default_factory=list)

    def digest(self) -> str:
        return content_digest(self.payload)


def collect(
    root: str | Path = ".",
    *,
    settlement_db: Optional[str] = None,
    jobs_log: Optional[str] = None,
) -> Dict[str, Any]:
    """Snapshot + readiness in one call. Never raises."""
    base = Path(root)
    state: Dict[str, Any] = {"root": str(base.resolve())}
    try:
        from dashboard.feed import snapshot

        state["snapshot"] = snapshot(base, settlement_db=settlement_db, jobs_log=jobs_log)
    except Exception as exc:  # noqa: BLE001
        state["snapshot"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        from .readiness import readiness

        state["readiness"] = readiness(base, probe_network=False)
    except Exception as exc:  # noqa: BLE001
        state["readiness"] = {"error": f"{type(exc).__name__}: {exc}"}
    state["observed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return state


def signals(state: Dict[str, Any]) -> List[Signal]:
    """Project a snapshot into the facts other agents can act on."""
    snap = state.get("snapshot") or {}
    report = state.get("readiness") or {}
    money = snap.get("money") or {}
    counters = money.get("counters") or {}
    out: List[Signal] = []

    if snap.get("error"):
        out.append(
            Signal(
                "money.settled",
                "telemetry",
                {"available": False, "error": str(snap["error"])[:200]},
                confidence=0.2,
                tags=["money", "degraded"],
            )
        )
        return out

    owed = int(counters.get("unserved_paid", 0) or 0)
    out.append(
        Signal(
            "money.settled",
            "telemetry",
            {
                "available": bool(money.get("available")),
                "earned_msat": int(counters.get("earned_msat", 0) or 0),
                "settled": int(counters.get("settled", 0) or 0),
                "served": int(counters.get("served", 0) or 0),
                "requested": int(counters.get("requested", 0) or 0),
                "open_invoices": int(counters.get("invoiced", 0) or 0),
            },
            confidence=1.0,
            tags=["money", "revenue"],
        )
    )

    owed_ids = [
        str(row.get("job_id", ""))
        for row in (money.get("owed") or [])
        if row.get("job_id")
    ][:24]
    out.append(
        Signal(
            "money.owed",
            "intel",
            {"unserved_paid": owed, "job_ids": owed_ids},
            # A customer who paid and received nothing is the highest-signal
            # fact in this system; it should recruit attention, not wait to be
            # noticed on a dashboard.
            confidence=1.0 if owed else 0.4,
            tags=["money", "owed"] + (["action_required"] if owed else []),
        )
    )

    failures = [c.get("id") for c in (money.get("integrity") or []) if not c.get("ok")]
    out.append(
        Signal(
            "ledger.integrity",
            "telemetry",
            {
                "checked": len(money.get("integrity") or []),
                "failures": sorted(str(f) for f in failures),
            },
            confidence=1.0,
            tags=["money", "integrity"] + (["violation"] if failures else []),
        )
    )

    worker = snap.get("worker") or {}
    out.append(
        Signal(
            "worker.mode",
            "telemetry",
            {
                "paid_mode": bool(worker.get("paid_mode")),
                "source": str(worker.get("paid_mode_source") or "unset"),
                "rail_configured": bool(worker.get("rail_configured")),
                "fee_msat": {
                    str(k): int(v) for k, v in (worker.get("fees_msat") or {}).items()
                },
            },
            confidence=1.0,
            tags=["money", "config"],
        )
    )

    stages = report.get("stages") or []
    blocked = [
        {"id": s.get("id"), "state": s.get("state"), "owner": s.get("owner")}
        for s in stages
        if s.get("state") not in ("ok",)
    ]
    out.append(
        Signal(
            "readiness.gate",
            "telemetry",
            {
                "payments_safe": bool(report.get("payments_safe")),
                "stages_ok": int(report.get("stages_ok", 0) or 0),
                "stages_total": int(report.get("stages_total", len(stages)) or 0),
                "blocked": blocked,
            },
            confidence=1.0,
            tags=["readiness"],
        )
    )

    conns = snap.get("connections") or {}
    if conns.get("available") is False:
        out.append(
            Signal(
                "connections.coverage",
                "telemetry",
                {"available": False},
                confidence=0.3,
                tags=["connections", "unprobed"],
            )
        )
    else:
        summary = conns.get("summary") or {}
        out.append(
            Signal(
                "connections.coverage",
                "telemetry",
                {
                    "available": True,
                    "connections": int(summary.get("connections", 0) or 0),
                    "capabilities": int(summary.get("capabilities", 0) or 0),
                    "unbound": int(summary.get("unbound", 0) or 0),
                    "degraded": int(summary.get("degraded", 0) or 0),
                },
                confidence=0.8,
                tags=["connections"],
            )
        )

    forecast = snap.get("forecast") or {}
    if forecast.get("available"):
        total = sum(int(x or 0) for x in (forecast.get("predictions_msat") or []))
        out.append(
            Signal(
                "forecast.projection",
                "telemetry",
                {
                    "horizon_days": int(forecast.get("horizon_days", 0) or 0),
                    "projected_msat": total,
                    "model": str(forecast.get("model") or ""),
                },
                # A projection is a guess with a measured error, and says so.
                confidence=0.55,
                tags=["money", "forecast"],
            )
        )

    histogram = (snap.get("jobs") or {}).get("histogram") or {}
    out.append(
        Signal(
            "jobs.histogram",
            "telemetry",
            {"outcomes": {str(k): int(v) for k, v in sorted(histogram.items())}},
            confidence=0.9,
            tags=["jobs"],
        )
    )

    if owed:
        out.append(
            Signal(
                "pheromone.money_owed",
                "pheromone",
                {
                    "trail": "money:owed",
                    "strength": min(1.0, 0.4 + 0.1 * owed),
                    "half_life_s": 3600.0,
                    "note": f"{owed} paid job(s) undelivered",
                },
                confidence=min(1.0, 0.4 + 0.1 * owed),
                tags=["trail:money:owed"],
            )
        )
    return out


def _read_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def publish(
    root: str | Path = ".",
    *,
    bus_path: Optional[str] = None,
    settlement_db: Optional[str] = None,
    jobs_log: Optional[str] = None,
    state_path: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    export_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish changed signals to the bus. Returns what happened, never raises."""
    base = Path(root)
    state = collect(base, settlement_db=settlement_db, jobs_log=jobs_log)
    sigs = signals(state)

    state_file = Path(state_path) if state_path else base / STATE_FILE
    prior = _read_state(state_file)
    bus_file = Path(bus_path) if bus_path else base / "bus.jsonl"

    published: List[Dict[str, Any]] = []
    unchanged: List[str] = []
    errors: List[str] = []

    bus = None
    if not dry_run:
        try:
            from .whorl import WhorlBus

            bus = WhorlBus(bus_file)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"bus unavailable: {type(exc).__name__}: {exc}")

    for sig in sigs:
        digest = sig.digest()
        record = prior.get(sig.id) or {}
        if not force and record.get("digest") == digest:
            unchanged.append(sig.id)
            continue
        payload = dict(sig.payload)
        payload["observed_at"] = state["observed_at"]
        try:
            envelope = Envelope.build(
                EMITTER,
                sig.kind,
                payload,
                confidence=sig.confidence,
                lineage=LINEAGE,
                tags=list(sig.tags),
            )
        except Exception as exc:  # noqa: BLE001 - bad payload is a finding
            errors.append(f"{sig.id}: {type(exc).__name__}: {exc}")
            continue
        if not dry_run and bus is not None:
            try:
                bus.publish(envelope)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{sig.id}: publish failed: {type(exc).__name__}: {exc}")
                continue
            prior[sig.id] = {
                "digest": digest,
                "published_at": state["observed_at"],
                "envelope": envelope.id,
                "count": int(record.get("count", 0) or 0) + 1,
            }
        published.append(
            {
                "id": sig.id,
                "kind": sig.kind,
                "envelope": envelope.id,
                "confidence": envelope.confidence,
            }
        )

    if not dry_run:
        try:
            _write_state(state_file, prior)
        except OSError as exc:
            errors.append(f"could not record bridge state: {type(exc).__name__}")

    exported = None
    if export_to or (not dry_run and export_to is not False):
        target = Path(export_to) if export_to else base / EXPORT_FILE
        try:
            exported = str(export(base, target, state=state))
        except OSError as exc:
            errors.append(f"export failed: {type(exc).__name__}")

    return {
        "observed_at": state["observed_at"],
        "bus": str(bus_file),
        "state_file": str(state_file),
        "signals": len(sigs),
        "published": published,
        "unchanged": unchanged,
        "errors": errors,
        "dry_run": dry_run,
        "exported": exported,
    }


def export(root: str | Path = ".", path: str | Path = EXPORT_FILE, *, state=None) -> Path:
    """Write the facts as a plain JSON file for consumers that can't read a bus."""
    base = Path(root)
    state = state or collect(base)
    snap = state.get("snapshot") or {}
    report = state.get("readiness") or {}
    target = Path(path) if Path(path).is_absolute() else base / path
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "console.telemetry/1",
        "observed_at": state.get("observed_at"),
        "root": state.get("root"),
        "money": {
            "available": bool((snap.get("money") or {}).get("available")),
            "counters": (snap.get("money") or {}).get("counters") or {},
            "integrity_failures": [
                c.get("id")
                for c in ((snap.get("money") or {}).get("integrity") or [])
                if not c.get("ok")
            ],
        },
        "worker": {
            k: (snap.get("worker") or {}).get(k)
            for k in ("paid_mode", "paid_mode_source", "rail_configured")
        },
        "readiness": {
            "payments_safe": bool(report.get("payments_safe")),
            "stages_ok": report.get("stages_ok"),
            "stages_total": report.get("stages_total"),
            "stages": [
                {"id": s.get("id"), "state": s.get("state"), "owner": s.get("owner")}
                for s in (report.get("stages") or [])
            ],
        },
        "connections": ((snap.get("connections") or {}).get("summary") or {}),
        "forecast": {
            "available": bool((snap.get("forecast") or {}).get("available")),
            "horizon_days": (snap.get("forecast") or {}).get("horizon_days"),
            "model": (snap.get("forecast") or {}).get("model"),
        },
        "jobs": ((snap.get("jobs") or {}).get("histogram") or {}),
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
    return target


def format_report(result: Dict[str, Any]) -> str:
    lines = [
        f"{result.get('signals', 0)} signals · "
        f"{len(result.get('published') or [])} published · "
        f"{len(result.get('unchanged') or [])} unchanged"
        + (" (dry run)" if result.get("dry_run") else ""),
        f"bus   : {result.get('bus')}",
        f"state : {result.get('state_file')}",
    ]
    for row in result.get("published") or []:
        lines.append(f"  ↑ {row['id']:<26}{row['kind']:<11}conf={row['confidence']:.2f}")
    for row in result.get("unchanged") or []:
        lines.append(f"  · {row} (unchanged)")
    for err in result.get("errors") or []:
        lines.append(f"  ! {err}")
    if result.get("exported"):
        lines.append(f"export: {result['exported']}")
    return "\n".join(lines)

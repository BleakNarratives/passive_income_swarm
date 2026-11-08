#!/usr/bin/env python3
"""
dashboard.seed — synthetic settlement data so the console can be looked at

The console is honest to a fault: with no settled history it says
"not enough settled history to project" and shows an empty funnel. That is the
correct behaviour and a terrible demo, so this module builds a *separate* demo
ledger to look at.

Two rules, neither negotiable:

1. **It never writes the real ledger.** The target must not be named
   ``settlements.db``; the seeder refuses. Demo rows live in
   ``nostr_dvm/logs/settlements.demo.db`` and the console labels the header
   ``DEMO`` whenever it is reading one.
2. **No fake money is claimed.** Every row is synthetic: hashed placeholder
   requesters, invoice strings that are not payable, preimages that are not
   real. Nothing here can be mistaken for revenue, and nothing here can be
   reconciled against a wallet that never received it.

    python3 -m dashboard.seed                     # write the demo ledger
    python3 -m dashboard.seed --days 30 --json
    SWARM_SETTLEMENTS_DB=nostr_dvm/logs/settlements.demo.db \\
        python3 -m dashboard.server --host 0.0.0.0 --port 3000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nostr_dvm.settlement import SettlementLedger  # noqa: E402

__all__ = ["seed", "DemoRefused", "DEFAULT_DB", "DEFAULT_JOBS"]

DEFAULT_DB = ROOT / "nostr_dvm" / "logs" / "settlements.demo.db"
DEFAULT_JOBS = ROOT / "nostr_dvm" / "logs" / "jobs.demo.jsonl"

FEE_MSAT = {5002: 50_000, 5003: 20_000}
RESULT_KIND = {5002: 6002, 5003: 6003}
RELAYS = ("wss://relay.primal.net", "wss://nos.lol", "wss://relay.damus.io")

# Outcome mix. Tuned so the funnel has every stage populated and the drop-off
# is visible: paid-and-served, paid-but-undelivered, expired, rail failure.
MIX = (
    ("served", 0.58),
    ("settled", 0.14),
    ("expired", 0.13),
    ("failed", 0.08),
    ("free", 0.07),
)


class DemoRefused(SystemExit):
    """Refusing to write synthetic rows into the production ledger."""


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _pick(rng: random.Random, mix=MIX) -> str:
    roll = rng.random()
    total = 0.0
    for name, weight in mix:
        total += weight
        if roll <= total:
            return name
    return mix[-1][0]


def seed(
    db_path: str | Path = DEFAULT_DB,
    jobs_path: str | Path = DEFAULT_JOBS,
    *,
    days: int = 14,
    now: Optional[float] = None,
    rng_seed: int = 7,
) -> Dict[str, Any]:
    """Write a fresh demo ledger. Returns a summary of what was created."""
    db = Path(db_path)
    jobs = Path(jobs_path)
    if db.name == "settlements.db":
        raise DemoRefused(
            "refusing to write demo rows into the production ledger; "
            "choose a path such as settlements.demo.db"
        )

    stamp = time.time() if now is None else float(now)
    rng = random.Random(rng_seed)

    db.parent.mkdir(parents=True, exist_ok=True)
    jobs.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    jobs.unlink(missing_ok=True)

    # Let the real ledger create its own schema. Duplicating the DDL here would
    # let the two drift apart silently, and the drift would only show up as
    # broken rows in the console.
    SettlementLedger(db).close()

    rows: List[tuple] = []
    transitions: List[tuple] = []
    job_lines: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}

    for day in range(days):
        for index in range(rng.randint(1, 5)):
            kind = 5002 if rng.random() < 0.7 else 5003
            fee = FEE_MSAT[kind]
            outcome = _pick(rng)
            identifier = f"demo-{day}-{index}"
            job_id = _hex("job:" + identifier)
            requester = _hex("requester:" + identifier)[:64]
            created = stamp - day * 86_400 - rng.randint(600, 43_000)
            expires = created + rng.choice((120.0, 300.0))
            settled = created + rng.randint(20, 180) if outcome in ("served", "settled") else None
            served = (settled + rng.randint(3, 25)) if outcome == "served" else None

            status = {
                "served": "served",
                "settled": "settled",
                "expired": "expired",
                "failed": "failed",
                "free": "served",
            }[outcome]

            last_error = ""
            if outcome == "settled":
                last_error = "inference_error: HTTPError (paid, undelivered — claim released)"
            elif outcome == "failed":
                last_error = "rail refused to issue an invoice"
            elif outcome == "expired":
                last_error = "payment window elapsed without sats"

            has_invoice = outcome != "free"
            bolt11 = f"lnbc{int(fee/1000)}n1demo{_hex('inv:'+identifier)[:40]}" if has_invoice else ""
            verify_url = f"https://demo.invalid/verify/{_hex('v:' + identifier)[:32]}" if has_invoice else ""
            payment_hash = _hex("hash:" + identifier) if has_invoice else ""
            preimage = _hex("pre:" + identifier) if outcome == "served" else None
            output = (
                json.dumps({"text": f"demo completion for job {identifier}", "kind": RESULT_KIND[kind]})
                if outcome in ("served", "free")
                else None
            )

            # A free job was never invoiced, so it gets no invoice row at all.
            # A row with fee_msat=0 and no settlement would read as "served
            # without payment" to integrity(), and that is the one check which
            # must stay trustworthy even in demo data.
            if has_invoice:
                rows.append(
                    (
                        job_id, requester, kind, fee, bolt11, verify_url,
                        payment_hash, status, preimage, "",
                        output, None, 1,
                        created, (served or settled or created), expires, settled, served, last_error,
                    )
                )
                transitions.append((job_id, created, "", "invoiced", f"fee {fee} msat"))
                if settled:
                    transitions.append((job_id, settled, "invoiced", "settled", "preimage verified"))
                if served:
                    transitions.append((job_id, served, "serving", "served", "result published"))
                if outcome == "expired":
                    transitions.append((job_id, expires, "invoiced", "expired", last_error))

            job_outcome = {
                "served": "served_paid",
                "settled": "inference_error: HTTPError",
                "expired": "payment_timeout",
                "failed": "payment_rail_error",
                "free": "served_free",
            }[outcome]
            job_lines.append(
                {
                    "ts": round(created, 3),
                    "job_id": job_id,
                    "kind": kind,
                    "result_kind": RESULT_KIND[kind],
                    "relay": rng.choice(RELAYS),
                    "requester": requester,
                    "amount_msat": fee if has_invoice else 0,
                    "input_chars": rng.randint(120, 900),
                    "output_chars": len(output or ""),
                    "latency_s": round(rng.uniform(0.4, 6.5), 2),
                    "outcome": job_outcome,
                }
            )
            counts[outcome] = counts.get(outcome, 0) + 1

    connection = sqlite3.connect(db)
    try:
        connection.executemany(
            """
            INSERT INTO invoices (
                job_id, requester, kind, fee_msat, bolt11, verify_url, payment_hash,
                status, preimage, input_sha256, output, result_event, attempts,
                created_at, updated_at, expires_at, settled_at, served_at, last_error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        connection.executemany(
            "INSERT INTO transitions (job_id, ts, from_state, to_state, detail) VALUES (?,?,?,?,?)",
            transitions,
        )
        connection.commit()
    finally:
        connection.close()

    job_lines.sort(key=lambda line: line["ts"])
    with jobs.open("w", encoding="utf-8") as handle:
        for line in job_lines:
            handle.write(json.dumps(line, sort_keys=True) + "\n")

    return {
        "db": str(db),
        "jobs": str(jobs),
        "days": days,
        "rows": len(rows),
        "transitions": len(transitions),
        "outcomes": counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="seed a synthetic demo ledger")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--jobs", default=str(DEFAULT_JOBS))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    summary = seed(args.db, args.jobs, days=args.days, rng_seed=args.seed)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"demo ledger : {summary['db']}")
        print(f"demo jobs   : {summary['jobs']}")
        print(f"rows        : {summary['rows']} over {summary['days']} days")
        print(f"outcomes    : {summary['outcomes']}")
        print("")
        print("look at it with:")
        print(f"  SWARM_SETTLEMENTS_DB={summary['db']} \\")
        print(f"  SWARM_JOBS_LOG={summary['jobs']} \\")
        print("  python3 -m dashboard.server --host 0.0.0.0 --port 3000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

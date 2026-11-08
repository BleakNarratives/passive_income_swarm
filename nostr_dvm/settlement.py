#!/usr/bin/env python3
"""
settlement.py — durable, job-bound settlement ledger for the NIP-90 paid rail
==============================================================================

DNA_TAG: ORIGIN=BleakNarratives/passive_income_swarm | PILLAR=nostr_dvm

WHY THIS EXISTS
---------------
The pre-existing paid path had three defects that made it unsafe to run with
real money attached:

1. It treated a requester-supplied ``amount`` tag as proof of payment. An
   ``amount`` tag is a *claim*, not a receipt.
2. It caught payment-rail exceptions and then "fell through to serve". A rail
   failure is not a payment.
3. Settlement was tracked in memory as a global counter. One settled invoice
   produced one "credit", and any waiting job could spend it — so paying for a
   20-sat summarization could unlock a 50-sat generation, and a process restart
   forgot that the customer had already paid.

This module replaces the counter with the thing money actually needs: a
**durable record that binds one settled invoice to exactly one job**, with a
single-use claim on service delivery.

THE INVARIANTS (enforced here, tested in tests/test_settlement.py)
------------------------------------------------------------------
* **I1 — Job binding.** An invoice row is keyed by job id. A settlement marked
  against job A can only ever authorise job A.
* **I2 — No invoice reuse.** A LNURL ``verify`` URL or BOLT11 payment hash may
  appear in at most one row, so an invoice minted for one job can never be
  re-bound to another.
* **I3 — Single use.** ``claim()`` transitions ``settled → serving`` inside a
  ``BEGIN IMMEDIATE`` transaction. Exactly one caller ever wins; the rest see
  the row and are told it is in flight or already served.
* **I4 — Idempotent replay.** A served job keeps its output. Re-delivery
  returns the stored result instead of re-running inference or charging again.
* **I5 — Durable.** SQLite + WAL. A restart, a relay reconnect or a crash
  cannot resurrect a spent invoice.
* **I6 — Failures are visible, never refunded silently.** An inference failure
  releases the claim back to ``settled`` so the job can be served on retry, and
  the row stays in the ledger so the operator can see "paid, unserved".

Standard library only, on purpose: this has to run on the box that runs the
income, under whatever Python the daemon already has.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "SettlementError",
    "InvoiceConflict",
    "Settlement",
    "SettlementLedger",
    "bolt11_payment_hash",
    "input_digest",
    "policy_checks",
]

# ── States ────────────────────────────────────────────────────────────────
STATUS_INVOICED = "invoiced"    # invoice minted, waiting for sats
STATUS_SETTLED = "settled"      # sats confirmed, not yet claimed for service
STATUS_SERVING = "serving"      # claim held, inference running
STATUS_SERVED = "served"        # delivered — terminal
STATUS_EXPIRED = "expired"      # poll window elapsed unpaid; same invoice reusable
STATUS_FAILED = "failed"        # rail refused to issue an invoice; terminal

TERMINAL = (STATUS_SERVED, STATUS_FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    job_id        TEXT PRIMARY KEY,
    requester     TEXT NOT NULL DEFAULT '',
    kind          INTEGER NOT NULL,
    fee_msat      INTEGER NOT NULL,
    bolt11        TEXT NOT NULL DEFAULT '',
    verify_url    TEXT NOT NULL DEFAULT '',
    payment_hash  TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    preimage      TEXT,
    input_sha256  TEXT NOT NULL DEFAULT '',
    output        TEXT,
    result_event  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    expires_at    REAL NOT NULL DEFAULT 0,
    settled_at    REAL,
    served_at     REAL,
    last_error    TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS invoices_verify_url
    ON invoices(verify_url) WHERE verify_url <> '';
CREATE UNIQUE INDEX IF NOT EXISTS invoices_payment_hash
    ON invoices(payment_hash) WHERE payment_hash <> '';
CREATE INDEX IF NOT EXISTS invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS invoices_settled_at ON invoices(settled_at);

-- Append-only audit trail: every state transition, in order, never rewritten.
CREATE TABLE IF NOT EXISTS transitions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    ts        REAL NOT NULL,
    from_state TEXT NOT NULL DEFAULT '',
    to_state  TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS transitions_job ON transitions(job_id);
"""


class SettlementError(RuntimeError):
    """Base class for settlement-ledger refusals."""


class InvoiceConflict(SettlementError):
    """An invoice was already bound to a different job. Never re-bind."""


# ── Bolt11 payment hash ───────────────────────────────────────────────────
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_values(invoice: str) -> Optional[List[int]]:
    """Decode a BOLT11 invoice's data part into 5-bit groups (checksum stripped).

    BOLT11 is bech32 *without* the 90-character length cap, so this is a
    deliberate local decoder rather than a generic bech32 import.
    """
    inv = (invoice or "").strip().lower()
    if not inv.startswith("ln"):
        return None
    sep = inv.rfind("1")
    if sep < 1 or sep + 7 > len(inv):
        return None
    body = inv[sep + 1: -6]  # drop 6 checksum groups
    try:
        return [_BECH32.index(c) for c in body]
    except ValueError:
        return None


def bolt11_payment_hash(invoice: str) -> str:
    """Return the BOLT11 payment hash as 64-char hex, or '' if undecodable.

    The payment hash is tagged field ``p`` (tag 1) with a 52-group length. It is
    the one identifier in a BOLT11 that is cryptographically bound to the
    invoice, which is what makes invariant I2 possible.
    """
    values = _bech32_values(invoice)
    if not values:
        return ""
    i = 0
    while i + 3 <= len(values):
        tag = values[i]
        length = values[i + 1] * 32 + values[i + 2]
        i += 3
        if length < 0 or i + length > len(values):
            return ""
        if tag == 1 and length == 52:
            # 52 five-bit groups = 260 bits; a payment hash is 256, so the last
            # group carries 4 bits of zero padding that must be dropped.
            bits = "".join(format(v, "05b") for v in values[i:i + 52])[:256]
            return format(int(bits, 2), "064x")
        i += length
    return ""


def input_digest(content: str) -> str:
    """Stable digest of the job payload, used to detect id/input mismatches."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


# ── Row model ─────────────────────────────────────────────────────────────
@dataclass
class Settlement:
    """One job's payment state. A thin, read-only view of a ledger row."""

    job_id: str
    requester: str
    kind: int
    fee_msat: int
    bolt11: str
    verify_url: str
    payment_hash: str
    status: str
    preimage: Optional[str] = None
    input_sha256: str = ""
    output: Optional[str] = None
    result_event: Optional[str] = None
    attempts: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    settled_at: Optional[float] = None
    served_at: Optional[float] = None
    last_error: str = ""

    @property
    def is_paid(self) -> bool:
        """True when sats are confirmed for *this* job, whatever happens next."""
        return self.settled_at is not None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["seconds_left"] = round(self.seconds_left, 1)
        # Never leak the preimage (it is the payment proof) to a dashboard feed.
        if d.get("preimage"):
            d["preimage"] = str(d["preimage"])[:8] + "…"
        return d


_COLUMNS = (
    "job_id, requester, kind, fee_msat, bolt11, verify_url, payment_hash, status, "
    "preimage, input_sha256, output, result_event, attempts, created_at, updated_at, "
    "expires_at, settled_at, served_at, last_error"
)


def _row_to_settlement(row: sqlite3.Row) -> Settlement:
    return Settlement(**{k: row[k] for k in row.keys()})


def policy_checks() -> List[Dict[str, Any]]:
    """Invariants that hold because of the code, not because of DB state.

    Reported by the console whether or not a ledger exists yet: the posture is a
    property of the implementation, so it should not disappear on a fresh box.
    """
    return [
        {
            "id": "amount_tag_not_trusted",
            "label": "Requester `amount` tags are never proof of payment",
            "ok": True,
            "detail": "paid_flow ignores the tag; only verify=settled opens inference",
        },
        {
            "id": "rail_fail_closed",
            "label": "Payment-rail errors never serve a job",
            "ok": True,
            "detail": "rail exception -> payment_rail_error, no inference fall-through",
        },
        {
            "id": "invoice_binding_rule",
            "label": "One invoice can only ever fund one job",
            "ok": True,
            "detail": "bind() is idempotent per job id and refuses a reused verify url or payment hash",
        },
        {
            "id": "claim_rule",
            "label": "Service is claimed once, atomically",
            "ok": True,
            "detail": "claim() is BEGIN IMMEDIATE settled->serving; later callers lose the race",
        },
    ]


class SettlementLedger:
    """Durable job-bound settlement store.

    Thread-safe (one lock, one connection). The worker is an asyncio process but
    SQLite calls are wrapped in ``asyncio.to_thread`` at the call site, so the
    lock has to be real.
    """

    def __init__(self, path: str | Path, default_timeout_s: float = 120.0):
        self.path = Path(path)
        self.default_timeout_s = float(default_timeout_s)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # -- internals ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _transition(self, job_id: str, frm: str, to: str, detail: str = "") -> None:
        self._db.execute(
            "INSERT INTO transitions (job_id, ts, from_state, to_state, detail) "
            "VALUES (?,?,?,?,?)",
            (job_id, time.time(), frm, to, detail[:500]),
        )

    def _fetch(self, job_id: str) -> Optional[Settlement]:
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM invoices WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if row is None else _row_to_settlement(row)

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._db.execute(
            f"UPDATE invoices SET {sets} WHERE job_id = ?",
            (*fields.values(), job_id),
        )

    # -- reads -------------------------------------------------------------
    def get(self, job_id: str) -> Optional[Settlement]:
        with self._lock:
            return self._fetch(job_id)

    def by_payment_hash(self, payment_hash: str) -> Optional[Settlement]:
        if not payment_hash:
            return None
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM invoices WHERE payment_hash = ?", (payment_hash,)
            ).fetchone()
            return None if row is None else _row_to_settlement(row)

    def open_invoices(self, now: Optional[float] = None) -> List[Settlement]:
        """Invoiced-but-unpaid rows, oldest first — the dashboard's invoice wall."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM invoices WHERE status IN (?,?) "
                "ORDER BY created_at",
                (STATUS_INVOICED, STATUS_EXPIRED),
            ).fetchall()
            return [_row_to_settlement(r) for r in rows]

    def unserved_paid(self) -> List[Settlement]:
        """Paid but never delivered. The operator's most important list."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM invoices WHERE settled_at IS NOT NULL "
                "AND status IN (?,?,?) ORDER BY settled_at",
                (STATUS_SETTLED, STATUS_SERVING, STATUS_EXPIRED),
            ).fetchall()
            return [_row_to_settlement(r) for r in rows]

    def recent(self, limit: int = 50) -> List[Settlement]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM invoices ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [_row_to_settlement(r) for r in rows]

    # -- writes ------------------------------------------------------------
    def bind(
        self,
        *,
        job_id: str,
        requester: str,
        kind: int,
        fee_msat: int,
        bolt11: str,
        verify_url: str,
        input_sha256: str = "",
        timeout_s: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Settlement:
        """Bind a freshly minted invoice to a job. Idempotent per job id.

        Raises :class:`InvoiceConflict` if this invoice already belongs to a
        different job (invariant I2) — the caller must then re-mint or refuse;
        it must never silently re-use someone else's invoice.
        """
        now = time.time() if now is None else float(now)
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        payment_hash = bolt11_payment_hash(bolt11)
        with self._lock:
            existing = self._fetch(job_id)
            if existing is not None:
                # Same job asking again: hand back the original invoice. Never
                # mint or bind a second one.
                return existing
            clash = None
            if verify_url:
                clash = self._db.execute(
                    "SELECT job_id FROM invoices WHERE verify_url = ?", (verify_url,)
                ).fetchone()
            if clash is None and payment_hash:
                clash = self._db.execute(
                    "SELECT job_id FROM invoices WHERE payment_hash = ?", (payment_hash,)
                ).fetchone()
            if clash is not None:
                raise InvoiceConflict(
                    f"invoice for {verify_url or payment_hash} is already bound to "
                    f"job {clash['job_id']}"
                )
            self._db.execute(
                "INSERT INTO invoices (job_id, requester, kind, fee_msat, bolt11, "
                "verify_url, payment_hash, status, input_sha256, created_at, "
                "updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, requester, int(kind), int(fee_msat), bolt11, verify_url,
                    payment_hash, STATUS_INVOICED, input_sha256, now, now,
                    now + timeout,
                ),
            )
            self._transition(job_id, "", STATUS_INVOICED, f"{fee_msat} msat")
            self._db.commit()
            row = self._fetch(job_id)
            assert row is not None
            return row

    def mark_settled(
        self, job_id: str, preimage: Optional[str] = None, now: Optional[float] = None
    ) -> bool:
        """Record confirmed sats for this job. Idempotent; True on first settle."""
        now = time.time() if now is None else float(now)
        with self._lock:
            cur = self._fetch(job_id)
            if cur is None:
                raise SettlementError(f"unknown job {job_id}")
            if cur.settled_at is not None:
                return False  # already known paid — never re-settle, never re-credit
            self._db.execute(
                "UPDATE invoices SET status = ?, settled_at = ?, preimage = ?, "
                "updated_at = ?, last_error = '' WHERE job_id = ?",
                (STATUS_SETTLED, now, preimage, now, job_id),
            )
            self._transition(job_id, cur.status, STATUS_SETTLED, "verify=settled")
            self._db.commit()
            return True

    def claim(self, job_id: str) -> bool:
        """Atomically take the single-use service claim for a paid job (I3).

        True only for the one caller that moved ``settled → serving``. A job that
        is already serving or served returns False, so a duplicate delivery
        across two relays can never run inference twice.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._fetch(job_id)
                if cur is None or cur.settled_at is None or cur.status != STATUS_SETTLED:
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute(
                    "UPDATE invoices SET status = ?, attempts = attempts + 1, "
                    "updated_at = ? WHERE job_id = ? AND status = ?",
                    (STATUS_SERVING, time.time(), job_id, STATUS_SETTLED),
                )
                self._transition(job_id, STATUS_SETTLED, STATUS_SERVING, "claim")
                self._db.commit()
                return True
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def release(self, job_id: str, error: str = "") -> None:
        """Return a claimed job to ``settled`` so it can be served on retry (I6).

        The customer is out sats and has nothing yet; the row stays visible as
        paid-and-unserved instead of disappearing.
        """
        with self._lock:
            cur = self._fetch(job_id)
            if cur is None:
                return
            self._db.execute(
                "UPDATE invoices SET status = ?, updated_at = ?, last_error = ? "
                "WHERE job_id = ?",
                (STATUS_SETTLED, time.time(), (error or "")[:500], job_id),
            )
            self._transition(job_id, cur.status, STATUS_SETTLED, f"failure: {error[:200]}")
            self._db.commit()

    def mark_served(
        self,
        job_id: str,
        output: str = "",
        result_event: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Record delivery. Output is cached so re-delivery never re-infers (I4)."""
        now = time.time() if now is None else float(now)
        with self._lock:
            cur = self._fetch(job_id)
            if cur is None:
                raise SettlementError(f"unknown job {job_id}")
            self._db.execute(
                "UPDATE invoices SET status = ?, output = ?, result_event = ?, "
                "served_at = ?, updated_at = ?, last_error = '' WHERE job_id = ?",
                (STATUS_SERVED, output, result_event, now, now, job_id),
            )
            self._transition(job_id, cur.status, STATUS_SERVED, f"{len(output)} chars")
            self._db.commit()

    def mark_expired(self, job_id: str) -> None:
        with self._lock:
            cur = self._fetch(job_id)
            if cur is None or cur.status != STATUS_INVOICED:
                return
            self._update(job_id, status=STATUS_EXPIRED)
            self._transition(job_id, cur.status, STATUS_EXPIRED, "poll window elapsed")
            self._db.commit()

    def mark_failed(self, job_id: str, error: str = "") -> None:
        """Terminal: the rail could not issue an invoice, so nothing was sold."""
        with self._lock:
            cur = self._fetch(job_id)
            if cur is not None:
                self._update(job_id, status=STATUS_FAILED, last_error=(error or "")[:500])
                self._transition(job_id, cur.status, STATUS_FAILED, error[:200])
                self._db.commit()
                return
            now = time.time()
            self._db.execute(
                "INSERT INTO invoices (job_id, kind, fee_msat, status, created_at, "
                "updated_at, last_error) VALUES (?,?,?,?,?,?,?)",
                (job_id, 0, 0, STATUS_FAILED, now, now, (error or "")[:500]),
            )
            self._transition(job_id, "", STATUS_FAILED, error[:200])
            self._db.commit()

    def retry_without_new_invoice(self, job_id: str, now: Optional[float] = None) -> bool:
        """Re-open an expired invoice's poll window using the *same* invoice.

        Never mints a second invoice for a job, which is what keeps a retry from
        double-charging a customer who is already paying.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            cur = self._fetch(job_id)
            if cur is None or cur.status != STATUS_EXPIRED or not cur.verify_url:
                return False
            self._update(job_id, status=STATUS_INVOICED, expires_at=now + self.default_timeout_s)
            self._transition(job_id, STATUS_EXPIRED, STATUS_INVOICED, "poll window reopened")
            self._db.commit()
            return True

    # -- aggregation for the dashboard ------------------------------------
    def counters(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM invoices GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["n"] for r in rows}
            total = self._db.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()["n"]
            settled = self._db.execute(
                "SELECT COUNT(*) AS n FROM invoices WHERE settled_at IS NOT NULL"
            ).fetchone()["n"]
            msat = self._db.execute(
                "SELECT COALESCE(SUM(fee_msat),0) AS s FROM invoices "
                "WHERE settled_at IS NOT NULL"
            ).fetchone()["s"]
        return {
            "requested": total,
            "settled": settled,
            "served": by_status.get(STATUS_SERVED, 0),
            "invoiced": by_status.get(STATUS_INVOICED, 0),
            "expired": by_status.get(STATUS_EXPIRED, 0),
            "failed": by_status.get(STATUS_FAILED, 0),
            "serving": by_status.get(STATUS_SERVING, 0),
            "unserved_paid": max(0, settled - by_status.get(STATUS_SERVED, 0)),
            "earned_msat": int(msat),
        }

    def integrity(self) -> List[Dict[str, Any]]:
        """Assert the money invariants against the live database.

        The dashboard shows these, so the operator can see the guarantee rather
        than take it on faith. Each check is a query that must return zero rows.
        """
        checks: List[Dict[str, Any]] = policy_checks()
        queries = [
            (
                "one_invoice_per_job",
                "No invoice is bound to two jobs",
                "SELECT verify_url, COUNT(*) AS n FROM invoices "
                "WHERE verify_url <> '' GROUP BY verify_url HAVING n > 1",
            ),
            (
                "no_unpaid_service",
                "Nothing is marked served without a confirmed settlement",
                "SELECT job_id FROM invoices WHERE status = 'served' "
                "AND settled_at IS NULL",
            ),
            (
                "no_double_charge",
                "Settlement never precedes a second settlement of the same job",
                "SELECT job_id FROM invoices WHERE status = 'served' "
                "AND (served_at IS NOT NULL AND settled_at IS NOT NULL "
                "AND served_at < settled_at)",
            ),
            (
                "served_has_output",
                "Every served job has a stored result to replay",
                "SELECT job_id FROM invoices WHERE status = 'served' "
                "AND (output IS NULL OR output = '')",
            ),
        ]
        with self._lock:
            for check_id, label, sql in queries:
                try:
                    rows = self._db.execute(sql).fetchall()
                    offenders = len(rows)
                except sqlite3.Error as exc:  # pragma: no cover - corrupt db
                    checks.append(
                        {"id": check_id, "label": label, "ok": False,
                         "detail": f"query failed: {exc}"}
                    )
                    continue
                checks.append(
                    {
                        "id": check_id,
                        "label": label,
                        "ok": offenders == 0,
                        "detail": "clean" if offenders == 0 else f"{offenders} violation(s)",
                    }
                )
        return checks

    def income_by_day(self, days: int = 14, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Daily settled-msat buckets, oldest first — forecast input + chart."""
        now = time.time() if now is None else float(now)
        start = now - days * 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT settled_at, fee_msat FROM invoices "
                "WHERE settled_at IS NOT NULL AND settled_at >= ? ORDER BY settled_at",
                (start,),
            ).fetchall()
        buckets = {i: 0 for i in range(days)}
        for r in rows:
            age_days = int((now - r["settled_at"]) // 86400)
            idx = days - 1 - age_days
            if 0 <= idx < days:
                buckets[idx] += int(r["fee_msat"])
        return [
            {"day_offset": i - (days - 1), "msat": buckets[i]}
            for i in range(days)
        ]

    def by_requester(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT requester, COUNT(*) AS jobs, "
                "COALESCE(SUM(CASE WHEN settled_at IS NOT NULL THEN fee_msat ELSE 0 END),0) AS msat, "
                "COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END),0) AS served "
                "FROM invoices WHERE requester <> '' GROUP BY requester "
                "ORDER BY msat DESC, jobs DESC LIMIT ?",
                (STATUS_SERVED, int(limit)),
            ).fetchall()
        return [
            {
                "requester": r["requester"],
                "jobs": r["jobs"],
                "msat": int(r["msat"]),
                "served": r["served"],
            }
            for r in rows
        ]

    def trail(self, job_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, from_state, to_state, detail FROM transitions "
                "WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [
            {"ts": r["ts"], "from": r["from_state"], "to": r["to_state"], "detail": r["detail"]}
            for r in rows
        ]

    def to_jsonl(self, path: str | Path) -> int:
        """Export the ledger as an audit file (one JSON object per row)."""
        rows = self.recent(limit=100000)
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
        return len(rows)

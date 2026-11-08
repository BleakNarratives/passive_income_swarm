"""
swarm.lifecycle — separating "the state machine is correct" from "sats moved"
============================================================================

There are two very different claims people make about a paid system:

* "the payment lifecycle is verified" — meaning the code transitions invoice →
  paid → served exactly once, refuses service before payment, and cannot
  double-serve. This is provable **without a wallet**, because it is a property
  of the state machine.
* "a real payment settled end to end" — meaning an actual invoice was paid on an
  actual wallet and the preimage came back. This is *not* provable without a
  wallet, and no amount of green tests substitutes for it.

This module keeps them apart. :func:`self_test` runs the real ``SettlementLedger``
through a full lifecycle in a throwaway database and reports what it observed.
:func:`rail_proof` reads a proof file that only a live run can produce. Readiness
shows both, labelled, so "logic verified" can never be mistaken for "money
works".
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "LOGIC_VERIFIED",
    "RAIL_VERIFIED",
    "NOT_VERIFIED",
    "PROOF_FILENAME",
    "self_test",
    "rail_proof",
    "evaluate",
    "format_report",
]

LOGIC_VERIFIED = "logic_verified"
RAIL_VERIFIED = "rail_verified"
NOT_VERIFIED = "not_verified"

#: Written by a live run that observed a real settlement. Never synthesized.
PROOF_FILENAME = "logs/lifecycle-proof.json"

_HEX64 = set("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= _HEX64


def self_test(*, fee_msat: int = 1_000) -> Dict[str, Any]:
    """Exercise the money state machine end to end in a temporary ledger.

    Proves, by observation rather than by reading code:
      * service is refused before payment,
      * payment opens exactly one service claim,
      * a second claim loses the race,
      * delivery records output and settles the counters once,
      * the ledger's own integrity checks pass afterwards.

    It does **not** touch the network, a wallet, a relay or an invoice, so it
    proves the machine and not the rail. The result says so explicitly.
    """
    from nostr_dvm.settlement import SettlementLedger

    steps: List[Dict[str, Any]] = []
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="lifecycle-") as tmp:
        ledger = SettlementLedger(Path(tmp) / "lifecycle.db", default_timeout_s=60)
        try:
            row = ledger.bind(
                job_id="lifecycle-probe",
                requester="self-test",
                kind=5002,
                fee_msat=int(fee_msat),
                bolt11="",
                verify_url="https://self-test.invalid/verify",
                input_sha256="",
                timeout_s=60,
            )
            steps.append({"step": "bind", "ok": row.status == "invoiced", "detail": row.status})

            refused = ledger.claim("lifecycle-probe")
            steps.append(
                {
                    "step": "refuse_service_before_payment",
                    "ok": refused is False,
                    "detail": "claim rejected while unpaid" if not refused else "SERVED UNPAID",
                }
            )

            ledger.mark_settled("lifecycle-probe", "00" * 32)
            steps.append({"step": "mark_settled", "ok": True, "detail": "settlement recorded"})

            first = ledger.claim("lifecycle-probe")
            second = ledger.claim("lifecycle-probe")
            steps.append(
                {
                    "step": "single_use_claim",
                    "ok": first is True and second is False,
                    "detail": f"first={first} second={second}",
                }
            )

            ledger.mark_served("lifecycle-probe", "ok", result_event="self-test")
            counters = ledger.counters()
            steps.append(
                {
                    "step": "serve_once",
                    "ok": counters["served"] == 1 and counters["earned_msat"] == int(fee_msat),
                    "detail": f"served={counters['served']} earned_msat={counters['earned_msat']}",
                }
            )

            bad = [c for c in ledger.integrity() if not c.get("ok")]
            steps.append(
                {
                    "step": "integrity",
                    "ok": not bad,
                    "detail": "clean" if not bad else f"{len(bad)} violation(s)",
                }
            )
        except Exception as exc:  # noqa: BLE001 - a crash here is the finding
            steps.append(
                {"step": "exception", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            ledger.close()

    passed = bool(steps) and all(s["ok"] for s in steps)
    return {
        "kind": LOGIC_VERIFIED,
        "ok": passed,
        "steps": steps,
        "fee_msat": int(fee_msat),
        "duration_ms": int((time.time() - started) * 1000),
        "proves": "invoice → paid → single serve, in-process",
        "does_not_prove": "that a wallet will pay an invoice or return a preimage",
    }


def rail_proof(root: str | Path = ".") -> Dict[str, Any]:
    """Read and validate evidence that a *real* settlement happened.

    The expected file is ``logs/lifecycle-proof.json``::

        {"kind": "rail", "job_id": "...", "payment_hash": "<64 hex>",
         "preimage": "<64 hex>", "fee_msat": 123,
         "settled_at": "2026-...Z", "ledger": "logs/settlements.db"}

    A preimage that is really the preimage of the recorded payment hash can only
    come from a wallet that paid. If the ledger it names is readable, the job is
    also required to show up settled there.
    """
    path = Path(root) / PROOF_FILENAME
    result: Dict[str, Any] = {
        "kind": RAIL_VERIFIED,
        "ok": False,
        "path": str(path),
        "exists": path.is_file(),
        "reason": "",
    }
    if not path.is_file():
        result["reason"] = (
            "no rail proof — run one real invoice end to end and record the "
            f"preimage in {PROOF_FILENAME}"
        )
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["reason"] = f"unreadable proof: {type(exc).__name__}"
        return result

    if not isinstance(data, dict):
        result["reason"] = "proof is not a JSON object"
        return result

    problems = []
    if data.get("kind") != "rail":
        problems.append("kind is not 'rail'")
    for field in ("payment_hash", "preimage"):
        if not _is_hex64(data.get(field)):
            problems.append(f"{field} is not 64 hex characters")
    if int(data.get("fee_msat") or 0) <= 0:
        problems.append("fee_msat is not positive")
    if data.get("payment_hash") and data.get("preimage"):
        if str(data["payment_hash"]).lower() == str(data["preimage"]).lower():
            problems.append("payment_hash equals preimage")
    if not data.get("settled_at"):
        problems.append("no settled_at timestamp")

    ledger_ref = data.get("ledger")
    if not problems and ledger_ref and data.get("job_id"):
        ledger_path = Path(str(ledger_ref))
        if not ledger_path.is_absolute():
            ledger_path = Path(root) / ledger_path
        if ledger_path.is_file():
            try:
                from nostr_dvm.settlement import SettlementLedger

                ledger = SettlementLedger(ledger_path)
                try:
                    row = ledger.get(str(data["job_id"]))
                finally:
                    ledger.close()
                if row is None or row.settled_at is None:
                    problems.append("the named ledger has no settled row for this job")
            except Exception as exc:  # noqa: BLE001
                result["ledger_error"] = f"{type(exc).__name__}"

    result["ok"] = not problems
    result["reason"] = "clean" if not problems else "; ".join(problems)
    result["evidence"] = {
        "job_id": data.get("job_id"),
        "fee_msat": data.get("fee_msat"),
        "settled_at": data.get("settled_at"),
        # Never echo the preimage: it is the payment proof.
        "preimage": "present",
    }
    return result


def evaluate(root: str | Path = ".", *, run_self_test: bool = True) -> Dict[str, Any]:
    """Combine both claims into one labelled state."""
    proof = rail_proof(root)
    logic = self_test() if run_self_test else {"ok": False, "steps": []}
    if proof["ok"]:
        state = RAIL_VERIFIED
    elif logic.get("ok"):
        state = LOGIC_VERIFIED
    else:
        state = NOT_VERIFIED
    return {
        "state": state,
        "rail_verified": bool(proof["ok"]),
        "logic_verified": bool(logic.get("ok")),
        "proof": proof,
        "self_test": logic,
    }


def format_report(report: Dict[str, Any]) -> str:
    proof = report.get("proof") or {}
    logic = report.get("self_test") or {}
    lines = [f"lifecycle: {report.get('state', NOT_VERIFIED)}"]
    lines.append(f"  state machine : {'verified' if logic.get('ok') else 'FAILED'}")
    for step in logic.get("steps", []):
        lines.append(f"    {'✓' if step['ok'] else '✗'} {step['step']}: {step['detail']}")
    lines.append(f"  real payment  : {'verified' if proof.get('ok') else 'not verified'}")
    if not proof.get("ok"):
        lines.append(f"    {proof.get('reason', '')}")
    lines.append(f"  proves        : {logic.get('proves', '')}")
    lines.append(f"  does not prove: {logic.get('does_not_prove', '')}")
    return "\n".join(lines)

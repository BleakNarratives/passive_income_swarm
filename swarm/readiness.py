"""
swarm.readiness — what stands between this checkout and *safe* paid traffic
===========================================================================

`MRD.txt` is a document, and documents drift. This module is the same list,
executable, against the actual filesystem and environment.

Two owners, and the distinction is the whole point:

* ``human`` — needs your account, your secret, your money or your judgment.
* ``machine`` — mechanical. One command clears it.

The six stages
--------------
Being *configured* to charge is not being *able* to charge safely. Those used to
collapse into one "ready" flag, which read as "payments operational" while the
wallet was still the leaked one. They are now ordered stages, and each carries
its own evidence:

1. ``configured``          a config source exists and names its fees
2. ``dependencies``        coincurve + websockets importable
3. ``signing_identity``    the worker can actually sign an event
4. ``wallet_rotated``      the credential is not the leaked one, and is 0600
5. ``lifecycle_verified``  the state machine is proven, and ideally a real
                           settlement has been observed (see `swarm.lifecycle`)
6. ``authorized``          a human explicitly authorised production traffic,
                           and stage 5 already passed

``payments_safe`` is true only when all six are ``ok``. ``ready`` is kept for
backwards compatibility and means "no blocking precondition" — it is deliberately
*weaker*, and the report says so in as many words.

One check earns its place by being unusual: the credential that was committed to
this public repository is still sitting on disk in some checkouts, and nothing
about its filename or contents says "compromised". So the file is hashed and
compared against a pinned fingerprint of the leaked value. It catches the
specific failure a rotation checklist forgets. It never prints, stores or
transmits the credential, and a fingerprint match is treated as *compromised*,
not merely *non-compliant*.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "Check",
    "readiness",
    "format_report",
    "LEAKED_NWC_SHA256_PREFIX",
    "READY",
    "BLOCKED",
    "HUMAN",
    "MACHINE",
    "STAGES",
    "STAGE_OK",
    "STAGE_PARTIAL",
    "STAGE_BLOCKED",
    "STAGE_MISSING",
]

READY = "ready"
BLOCKED = "blocked"

HUMAN = "human"
MACHINE = "machine"

STAGE_OK = "ok"
STAGE_PARTIAL = "partial"
STAGE_BLOCKED = "blocked"
STAGE_MISSING = "missing"

_GOOD_STAGE_STATES = (STAGE_OK,)

#: sha256 prefix of ``keys/nwc.secret`` as it was committed to the public repo.
#: A commitment, not a credential: it cannot be reversed into the URI.
LEAKED_NWC_SHA256_PREFIX = "dc2fc57f4b30d72b"

#: The var a human sets to say "yes, this box may take real money now".
AUTHORIZATION_VAR = "DVM_PRODUCTION_AUTHORIZED"

RELAY_HOSTS = ("relay.primal.net", "nos.lol", "relay.damus.io")

#: (id, label, owner) in the order money actually has to work.
STAGES = (
    ("configured", "configuration detected", HUMAN),
    ("dependencies", "dependencies installed", MACHINE),
    ("signing_identity", "signing identity available", HUMAN),
    ("wallet_rotated", "wallet credential rotated", HUMAN),
    ("lifecycle_verified", "payment lifecycle verified", MACHINE),
    ("authorized", "production authorization granted", HUMAN),
)


@dataclass(frozen=True)
class Check:
    id: str
    state: str
    owner: str
    detail: str
    action: str = ""


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stage(checks: Dict[str, Check], stage_id: str, label: str, owner: str, **kw: Any) -> Dict[str, Any]:
    return {"id": stage_id, "label": label, "owner": owner, **kw}


def _check_nwc(root: Path, leaked_prefix: str) -> Check:
    path = root / "keys" / "nwc.secret"
    if not path.exists():
        return Check(
            "nwc_credential",
            BLOCKED,
            HUMAN,
            "keys/nwc.secret is missing",
            "create a wallet connection and write the URI to keys/nwc.secret",
        )
    try:
        payload = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return Check(
            "nwc_credential",
            BLOCKED,
            HUMAN,
            f"unreadable: {type(exc).__name__}",
            "fix ownership/permissions on keys/nwc.secret",
        )

    digest = hashlib.sha256(payload).hexdigest()
    if leaked_prefix and digest.startswith(leaked_prefix):
        return Check(
            "nwc_credential",
            BLOCKED,
            HUMAN,
            "this is still the credential that was public since 2026-04-30 — "
            "treat the wallet as compromised",
            "revoke the connection in coinos, issue a new one, replace the file",
        )
    if mode & 0o077:
        return Check(
            "nwc_credential",
            BLOCKED,
            MACHINE,
            f"mode {mode:o} is readable by other users",
            "chmod 600 keys/nwc.secret",
        )
    return Check("nwc_credential", READY, HUMAN, "present, rotated, mode 600")


def _check_signing_key(root: Path) -> Check:
    path = root / "keys" / "nostr.key"
    if path.exists() and path.is_file() and path.stat().st_size > 0:
        return Check("signing_key", READY, HUMAN, "present")
    return Check(
        "signing_key",
        BLOCKED,
        HUMAN,
        "keys/nostr.key is absent — the worker cannot sign an event, so it cannot earn",
        "restore the existing nsec from your device, or authorise a new identity",
    )


def _module_present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _check_module(module: str, label: str, action: str) -> Check:
    present = _module_present(module)
    return Check(
        label,
        READY if present else BLOCKED,
        MACHINE,
        f"{module} {'installed' if present else 'is not importable'}",
        "" if present else action,
    )


def _env_file_values(root: Path) -> Dict[str, str]:
    """Read ``nostr_dvm/dvm.env`` the way the launcher does.

    A shell running ``swarmctl`` does not inherit the systemd EnvironmentFile, so
    a correctly configured checkout would otherwise report "paid mode is off".
    Values are returned for comparison only and never printed.
    """
    path = root / "nostr_dvm" / "dvm.env"
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(name: str, file_values: Dict[str, str]) -> tuple:
    """Return (value, source) preferring the live environment."""
    live = os.environ.get(name)
    if live:
        return live, "environment"
    from_file = file_values.get(name)
    if from_file:
        return from_file, "nostr_dvm/dvm.env"
    return "", ""


def _truthy(raw: Any) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _check_inference(file_values: Dict[str, str]) -> Check:
    key, source = _setting("GROQ_API_KEY", file_values)
    if key:
        return Check("inference", READY, HUMAN, f"GROQ_API_KEY is set ({source})")
    endpoint, endpoint_source = _setting("GROQ_BASE_URL", file_values)
    if not endpoint:
        endpoint, endpoint_source = _setting("DVM_INFERENCE_URL", file_values)
    if endpoint:
        return Check("inference", READY, HUMAN, f"custom endpoint ({endpoint_source})")
    return Check(
        "inference",
        BLOCKED,
        HUMAN,
        "no GROQ_API_KEY and no custom endpoint",
        "create a key at console.groq.com/keys, or point GROQ_BASE_URL at a local model",
    )


def _check_ledger(root: Path) -> Check:
    target = root / "logs"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".readiness"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "ledger",
            BLOCKED,
            MACHINE,
            f"logs/ is not writable: {type(exc).__name__}",
            "fix permissions on logs/",
        )
    return Check("ledger", READY, MACHINE, "logs/ is writable")


def _check_fees(file_values: Dict[str, str]) -> Check:
    configured = {
        key: _setting(key, file_values)[0]
        for key in ("DVM_FEE_5002_MSAT", "DVM_FEE_5003_MSAT")
    }
    if all(configured.values()):
        return Check("fees", READY, HUMAN, "fee schedule set explicitly")
    return Check(
        "fees",
        READY,
        HUMAN,
        "using code defaults (50 / 20 sats) — set DVM_FEE_* if you disagree",
    )


def _check_paid_mode(file_values: Dict[str, str]) -> Check:
    raw, source = _setting("DVM_PAID_ONLY", file_values)
    if _truthy(raw):
        return Check(
            "paid_mode",
            READY,
            HUMAN,
            f"charging for jobs ({source}) — configuration only, not evidence the "
            "rail can settle",
        )
    where = source or "unset"
    return Check(
        "paid_mode",
        BLOCKED,
        HUMAN,
        f"paid mode is off ({where}) — the worker will serve for free",
        "set DVM_PAID_ONLY=1 in nostr_dvm/dvm.env when you are ready to charge",
    )


def _check_relays(timeout: float) -> Check:
    reachable = 0
    for host in RELAY_HOSTS:
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                reachable += 1
        except OSError:
            continue
    state = READY if reachable else BLOCKED
    return Check(
        "relays",
        state,
        MACHINE,
        f"{reachable}/{len(RELAY_HOSTS)} relays answer TCP 443",
        "" if reachable else "check outbound network access from the host",
    )


def _build_stages(
    root: Path,
    file_values: Dict[str, str],
    checks: Dict[str, Check],
    lifecycle: Dict[str, Any],
) -> List[Dict[str, Any]]:
    config_path = root / "nostr_dvm" / "dvm.env"
    live_keys = [k for k in file_values if k.startswith(("DVM_", "GROQ_"))]

    if config_path.is_file():
        configured = _stage(
            checks, "configured", "configuration detected", HUMAN,
            state=STAGE_OK,
            evidence=f"nostr_dvm/dvm.env · {len(live_keys)} key(s)",
        )
    elif any(k.startswith(("DVM_", "GROQ_")) for k in os.environ):
        configured = _stage(
            checks, "configured", "configuration detected", HUMAN,
            state=STAGE_PARTIAL,
            evidence="environment only — nothing on disk to survive a restart",
            action="write nostr_dvm/dvm.env so systemd sees the same config",
        )
    else:
        configured = _stage(
            checks, "configured", "configuration detected", HUMAN,
            state=STAGE_MISSING,
            evidence="no nostr_dvm/dvm.env and no DVM_*/GROQ_* in the environment",
            action="cp nostr_dvm/dvm.env.example nostr_dvm/dvm.env",
        )

    missing_mods = [m for m in ("coincurve", "websockets") if not _module_present(m)]
    dependencies = _stage(
        checks, "dependencies", "dependencies installed", MACHINE,
        state=STAGE_OK if not missing_mods else STAGE_MISSING,
        evidence="coincurve, websockets" if not missing_mods else "missing " + ", ".join(missing_mods),
        action="" if not missing_mods else "pip install -r requirements.txt",
    )

    signing = checks["signing_key"]
    signing_stage = _stage(
        checks, "signing_identity", "signing identity available", HUMAN,
        state=STAGE_OK if signing.state == READY else STAGE_MISSING,
        evidence=signing.detail,
        action=signing.action,
    )

    wallet = checks["nwc_credential"]
    if wallet.state == READY:
        wallet_state = STAGE_OK
    elif "other users" in wallet.detail:
        wallet_state = STAGE_PARTIAL
    elif "missing" in wallet.detail:
        wallet_state = STAGE_MISSING
    else:
        wallet_state = STAGE_BLOCKED
    wallet_stage = _stage(
        checks, "wallet_rotated", "wallet credential rotated", HUMAN,
        state=wallet_state,
        evidence=wallet.detail,
        action=wallet.action,
    )

    proof = lifecycle.get("proof") or {}
    if lifecycle.get("rail_verified"):
        lifecycle_state, lifecycle_evidence = STAGE_OK, "a real settlement was observed"
    elif lifecycle.get("logic_verified"):
        lifecycle_state = STAGE_PARTIAL
        lifecycle_evidence = (
            "state machine verified in-process; no real settlement has been observed"
        )
    else:
        lifecycle_state, lifecycle_evidence = STAGE_BLOCKED, "state machine self-test failed"
    lifecycle_stage = _stage(
        checks, "lifecycle_verified", "payment lifecycle verified", MACHINE,
        state=lifecycle_state,
        evidence=lifecycle_evidence,
        action=(
            ""
            if lifecycle_state == STAGE_OK
            else "swarmctl lifecycle status — then pay one real invoice and record the proof"
        ),
        rail_verified=bool(proof.get("ok")),
    )

    auth_raw, auth_source = _setting(AUTHORIZATION_VAR, file_values)
    if _truthy(auth_raw) and lifecycle_state == STAGE_OK:
        auth_state, auth_evidence, auth_action = STAGE_OK, f"granted ({auth_source})", ""
    elif _truthy(auth_raw):
        auth_state = STAGE_PARTIAL
        auth_evidence = (
            f"{AUTHORIZATION_VAR} is set ({auth_source}) but the lifecycle is not "
            "rail-verified — the flag cannot substitute for evidence"
        )
        auth_action = ""
    else:
        auth_state = STAGE_MISSING
        auth_evidence = f"{AUTHORIZATION_VAR} is not set"
        auth_action = f"set {AUTHORIZATION_VAR}=1 in nostr_dvm/dvm.env once you mean it"
    auth_stage = _stage(
        checks, "authorized", "production authorization granted", HUMAN,
        state=auth_state, evidence=auth_evidence, action=auth_action,
    )

    return [configured, dependencies, signing_stage, wallet_stage, lifecycle_stage, auth_stage]


def readiness(
    root: str | Path = ".",
    *,
    probe_network: bool = True,
    leaked_prefix: str = LEAKED_NWC_SHA256_PREFIX,
    timeout: float = 4.0,
    run_lifecycle: bool = True,
) -> Dict[str, Any]:
    """Check every money-path precondition. Never raises."""
    base = Path(root)
    file_values = _env_file_values(base)
    check_list: List[Check] = [
        _check_nwc(base, leaked_prefix),
        _check_signing_key(base),
        _check_module("coincurve", "coincurve", "pip install -r requirements.txt"),
        _check_module("websockets", "websockets", "pip install -r requirements.txt"),
        _check_inference(file_values),
        _check_ledger(base),
        _check_fees(file_values),
        _check_paid_mode(file_values),
    ]
    if probe_network:
        check_list.append(_check_relays(timeout))

    lifecycle: Dict[str, Any] = {"state": "not_verified", "rail_verified": False,
                                 "logic_verified": False, "proof": {}}
    if run_lifecycle:
        try:
            from .lifecycle import evaluate as evaluate_lifecycle

            lifecycle = evaluate_lifecycle(base)
        except Exception as exc:  # noqa: BLE001 - never let this break the report
            lifecycle["error"] = f"{type(exc).__name__}: {exc}"

    by_id = {c.id: c for c in check_list}
    stages = _build_stages(base, file_values, by_id, lifecycle)

    blocked_human = [c for c in check_list if c.state == BLOCKED and c.owner == HUMAN]
    blocked_machine = [c for c in check_list if c.state == BLOCKED and c.owner == MACHINE]

    payments_safe = all(s["state"] in _GOOD_STAGE_STATES for s in stages)
    return {
        "checked_at": _stamp(),
        "root": str(base.resolve()),
        "network_checked": bool(probe_network),
        # Legacy meaning: no blocking precondition. Deliberately weaker than
        # payments_safe, and labelled as such in the report.
        "ready": not blocked_human and not blocked_machine,
        "payments_safe": payments_safe,
        "stages": stages,
        "stages_ok": sum(1 for s in stages if s["state"] in _GOOD_STAGE_STATES),
        "stages_total": len(stages),
        "lifecycle": lifecycle,
        "blocking_human": len(blocked_human),
        "blocking_machine": len(blocked_machine),
        "checks": [asdict(check) for check in check_list],
    }


_STAGE_MARK = {
    STAGE_OK: "ok",
    STAGE_PARTIAL: "partial",
    STAGE_BLOCKED: "blocked",
    STAGE_MISSING: "missing",
}


def format_report(report: Dict[str, Any]) -> str:
    stages = report.get("stages", [])
    checks = report.get("checks", [])
    blocked = [c for c in checks if c.get("state") == BLOCKED]

    lines = []
    if stages:
        safe = report.get("payments_safe")
        lines.append(
            f"PAYMENTS SAFE: {'YES' if safe else 'NO'}  "
            f"({report.get('stages_ok', 0)}/{report.get('stages_total', len(stages))} stages)"
        )
        lines.append("")
        lines.append(f"{'STAGE':<19}{'OWNER':<9}{'STATE':<9}EVIDENCE")
        for stage in stages:
            lines.append(
                f"{stage.get('id', '?'):<19}{stage.get('owner', '?'):<9}"
                f"{_STAGE_MARK.get(stage.get('state'), '?'):<9}{stage.get('evidence', '')}"
            )
        lines.append("")

    lines.append(
        f"{len(checks)} checks · {len(checks) - len(blocked)} ready · "
        f"{len(blocked)} blocking"
        + ("" if report.get("network_checked") else " (network skipped)")
    )
    lines.append("")
    lines.append(f"{'STATE':<9}{'OWNER':<9}{'CHECK':<16}DETAIL")
    for check in checks:
        lines.append(
            f"{check.get('state', '?'):<9}{check.get('owner', '?'):<9}"
            f"{check.get('id', '?'):<16}{check.get('detail', '')}"
        )

    human = [c for c in blocked if c.get("owner") == HUMAN]
    machine = [c for c in blocked if c.get("owner") == MACHINE]
    if human:
        lines.append("")
        lines.append("only you can clear these (see MRD.txt):")
        for check in human:
            lines.append(f"  {check['id']}: {check.get('action', '')}")
    if machine:
        lines.append("")
        lines.append("mechanical — one command each:")
        for check in machine:
            lines.append(f"  {check['id']}: {check.get('action', '')}")

    stage_actions = [s for s in stages if s.get("action") and s.get("state") not in _GOOD_STAGE_STATES]
    if stage_actions:
        lines.append("")
        lines.append("stages still open:")
        for stage in stage_actions:
            lines.append(f"  {stage['id']}: {stage['action']}")

    lines.append("")
    if report.get("payments_safe"):
        lines.append('payments are safe to enable — "safe" here means every stage has evidence.')
    elif blocked:
        lines.append(
            "not ready to take a paid job. A configured switch is not a safe machine: "
            "`payments_safe` requires the lifecycle stage, not just the mode flag."
        )
    return "\n".join(lines)


def to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)

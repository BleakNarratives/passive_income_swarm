"""
swarm.readiness — what still stands between this checkout and a paid job
=======================================================================

`MRD.txt` is a document, and documents drift. This module is the same list,
executable: every precondition of the money path, checked against the actual
filesystem and environment, each one labelled with who can clear it.

Two owners, and the distinction is the whole point:

* ``human`` — needs your account, your secret, your money or your judgment.
  No amount of agent autonomy substitutes for any of those.
* ``machine`` — mechanical. Any command runner clears it; nobody has to decide
  anything. The exact command is printed.

One check earns its place by being unusual: the credential that was committed to
this public repository is still sitting on disk in some checkouts, and nothing
about its filename or contents says "compromised". So the file is hashed and
compared against a pinned fingerprint of the leaked value. It catches the
specific failure that a rotation checklist forgot to perform, and it never
prints, stores or transmits the credential itself.
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
]

READY = "ready"
BLOCKED = "blocked"

HUMAN = "human"
MACHINE = "machine"

#: sha256 prefix of ``keys/nwc.secret`` as it was committed to the public repo.
#: A commitment, not a credential: it cannot be reversed into the URI, and it is
#: the only way to recognise the leaked value without keeping a copy of it.
LEAKED_NWC_SHA256_PREFIX = "dc2fc57f4b30d72b"

#: Relays the worker publishes to; reachability only, no signing involved.
RELAY_HOSTS = ("relay.primal.net", "nos.lol", "relay.damus.io")


@dataclass(frozen=True)
class Check:
    id: str
    state: str
    owner: str
    detail: str
    action: str = ""


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
            "this is still the credential that was public since 2026-04-30",
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
    if path.exists():
        return Check("signing_key", READY, HUMAN, "present")
    return Check(
        "signing_key",
        BLOCKED,
        HUMAN,
        "keys/nostr.key is absent — the worker cannot sign an event, so it cannot earn",
        "restore the existing nsec from your device, or authorise a new identity",
    )


def _check_module(module: str, label: str, action: str) -> Check:
    try:
        present = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        present = False
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
    a correctly configured checkout would otherwise report "paid mode is off"
    and send the operator chasing a problem that does not exist. Values are
    returned for comparison only and never printed.
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
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return Check("paid_mode", READY, HUMAN, f"charging for jobs ({source})")
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


def readiness(
    root: str | Path = ".",
    *,
    probe_network: bool = True,
    leaked_prefix: str = LEAKED_NWC_SHA256_PREFIX,
    timeout: float = 4.0,
) -> Dict[str, Any]:
    """Check every money-path precondition. Never raises."""
    base = Path(root)
    file_values = _env_file_values(base)
    checks: List[Check] = [
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
        checks.append(_check_relays(timeout))

    blocked_human = [c for c in checks if c.state == BLOCKED and c.owner == HUMAN]
    blocked_machine = [c for c in checks if c.state == BLOCKED and c.owner == MACHINE]
    return {
        "checked_at": _stamp(),
        "root": str(base.resolve()),
        "network_checked": bool(probe_network),
        "ready": not blocked_human and not blocked_machine,
        "blocking_human": len(blocked_human),
        "blocking_machine": len(blocked_machine),
        "checks": [asdict(check) for check in checks],
    }


def format_report(report: Dict[str, Any]) -> str:
    checks = report.get("checks", [])
    blocked = [c for c in checks if c.get("state") == BLOCKED]
    lines = [
        f"{len(checks)} checks · {len(checks) - len(blocked)} ready · "
        f"{len(blocked)} blocking"
        + ("" if report.get("network_checked") else " (network skipped)"),
        "",
        f"{'STATE':<9}{'OWNER':<9}{'CHECK':<16}DETAIL",
    ]
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
    if not blocked:
        lines.append("")
        lines.append("ready to take a paid job.")
    return "\n".join(lines)


def to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)

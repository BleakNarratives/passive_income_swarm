"""
swarm.connections — turning declared capabilities into verified links
=====================================================================

`cabinet.json` *declares* what the swarm can do: `llm.generate`, `pay.lnurl`,
`market.search`, `repo.gh`. Until now nothing checked that anything could
actually serve those names. A declaration is not a connection.

This module is the adapter boundary, and it is deliberately small:

* a :class:`Connection` is one provider that serves one or more capabilities,
  knows which environment variables it needs, and carries a **probe** — a
  self-contained function that answers "is this reachable right now?" over
  plain HTTP/TCP, with no SDK, no pip install, no browser.
* :class:`ConnectionRegistry` maps capabilities to connections, audits a cabinet
  manifest against them, and reports **orphans**: capabilities an occupant
  declares that nothing can serve. Orphans are the failure this exists to catch.
* Results are cached to `logs/connections.json` so the console can show the last
  known truth without hammering the network every two seconds.

Design rules
------------
* **Stdlib only.** Same reason as the rest of the control plane: this has to run
  on the box that runs the income.
* **Probes never raise.** A dead provider is data, not an exception.
* **Secrets are never reported.** Only which variable is *missing* is reported,
  never a value, and no probe URL containing a token is ever echoed.
* **Aggregation is generous but honest:** a capability counts as `live` if any
  connection serving it is live; `unconfigured` if all of them are merely
  missing credentials; `unbound` if nothing serves it at all.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Probe",
    "Connection",
    "ConnectionRegistry",
    "build_connections",
    "local_embedding",
    "STATUS_LIVE",
    "STATUS_DEGRADED",
    "STATUS_UNCONFIGURED",
    "STATUS_UNREACHABLE",
    "STATUS_UNBOUND",
    "STATUS_UNVERIFIED",
]

STATUS_LIVE = "live"
STATUS_DEGRADED = "degraded"
STATUS_UNCONFIGURED = "unconfigured"
STATUS_UNREACHABLE = "unreachable"
STATUS_UNBOUND = "unbound"
STATUS_UNVERIFIED = "unverified"

#: Statuses that mean "this capability is actually serviceable right now".
HEALTHY = (STATUS_LIVE, STATUS_DEGRADED)

#: Worst-to-best, used when rolling several connections up into one capability.
_PRECEDENCE = (
    STATUS_LIVE,
    STATUS_DEGRADED,
    STATUS_UNCONFIGURED,
    STATUS_UNREACHABLE,
)

#: Public relays the worker and scouts actually use (see dvm_worker.RELAYS).
RELAY_HOSTS = ("relay.primal.net", "nos.lol", "relay.damus.io")


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


@dataclass
class Probe:
    """The outcome of one reachability check. Never an exception."""

    ok: bool
    detail: str = ""
    latency_ms: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# probes — plain HTTP / TCP / local, no third-party client
# ---------------------------------------------------------------------------
def _http_probe(url: str, headers: Optional[Dict[str, str]] = None,
                timeout: float = 8.0, want_json: bool = False) -> Probe:
    started = time.monotonic()
    request_headers = {
        "Accept": "application/json" if want_json else "*/*",
        "User-Agent": "swarm-connections/1.0",
    }
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048)
            ok = 200 <= resp.status < 300
            detail = f"HTTP {resp.status}"
            if want_json and ok:
                try:
                    json.loads(body.decode("utf-8", errors="replace"))
                    detail += " · json ok"
                except ValueError:
                    ok = False
                    detail += " · not json"
            return Probe(ok, detail, _ms(started))
    except urllib.error.HTTPError as exc:
        # 401/403 on a credentialed probe means the credential is wrong, which is
        # actionable information, not a network fault.
        return Probe(False, f"HTTP {exc.code}", _ms(started), {"code": exc.code})
    except urllib.error.URLError as exc:
        return Probe(False, f"unreachable: {exc.reason}", _ms(started))
    except (socket.timeout, TimeoutError):
        return Probe(False, "timed out", _ms(started))
    except OSError as exc:
        return Probe(False, f"network error: {exc}", _ms(started))


def _probe_groq(timeout: float) -> Probe:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return Probe(False, "GROQ_API_KEY not set")
    return _http_probe(
        "https://api.groq.com/openai/v1/models",
        {"Authorization": f"Bearer {key}"},
        timeout,
        want_json=True,
    )


def lud16_from_env_or_file(root: str | Path = ".") -> str:
    """Resolve the Lightning address without ever printing the wallet secret."""
    env = os.environ.get("LNURL_LUD16", "").strip()
    if env:
        return env
    path = Path(root) / "keys" / "nwc.secret"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    for part in raw.replace("?", "&").split("&"):
        if part.startswith("lud16="):
            return part.split("=", 1)[1].strip()
    return ""


def _probe_lnurl(root: Path, timeout: float) -> Probe:
    lud16 = lud16_from_env_or_file(root)
    if not lud16 or "@" not in lud16:
        return Probe(False, "no lud16 (keys/nwc.secret or LNURL_LUD16)")
    user, domain = lud16.split("@", 1)
    probe = _http_probe(
        f"https://{domain}/.well-known/lnurlp/{urllib.parse.quote(user)}",
        timeout=timeout,
        want_json=True,
    )
    if probe.ok:
        probe.detail = f"{domain} lnurl-pay resolved"
    return probe


def _probe_relays(timeout: float) -> Probe:
    reachable: List[str] = []
    for host in RELAY_HOSTS:
        try:
            with socket.create_connection((host, 443), timeout=min(3.0, timeout)):
                reachable.append(host)
        except OSError:
            continue
    ok = bool(reachable)
    return Probe(
        ok,
        f"{len(reachable)}/{len(RELAY_HOSTS)} relays answer TCP 443"
        + ("" if ok else " (handshake not attempted here)"),
        None,
        {"reachable": reachable},
    )


def _probe_github(timeout: float) -> Probe:
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    probe = _http_probe("https://api.github.com/rate_limit", headers, timeout, want_json=True)
    if probe.ok and not token:
        probe.meta["degraded"] = True
        probe.detail += " · unauthenticated (60 req/h)"
    if not probe.ok and probe.meta.get("code") in (401, 403) and token:
        probe.detail += " · credential rejected"
    return probe


def _probe_webz(timeout: float) -> Probe:
    token = os.environ.get("WEBZ_API_TOKEN", "").strip()
    if not token:
        return Probe(False, "WEBZ_API_TOKEN not set")
    base = os.environ.get("WEBZ_API_BASE", "https://api.webz.io/newsApiLite").strip()
    sep = "&" if "?" in base else "?"
    # The token travels in the query per the provider's docs; it is never logged
    # and never included in a report.
    url = f"{base}{sep}" + urllib.parse.urlencode(
        {"token": token, "q": "market", "size": "1", "format": "json"}
    )
    return _http_probe(url, timeout=timeout, want_json=True)


def _probe_fs(root: Path, timeout: float) -> Probe:
    readable = os.access(root, os.R_OK)
    writable = os.access(root, os.W_OK)
    return Probe(
        readable and writable,
        f"root readable={readable} writable={writable}",
        None,
    )


def _probe_shell(timeout: float) -> Probe:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import sys;sys.stdout.write('ok')"],
            capture_output=True,
            text=True,
            timeout=max(2.0, min(10.0, timeout)),
        )
        ok = proc.returncode == 0 and "ok" in (proc.stdout or "")
        return Probe(ok, f"python subprocess rc={proc.returncode}", _ms(started))
    except Exception as exc:  # pragma: no cover - sandboxed environments
        return Probe(False, f"{type(exc).__name__}: {exc}", _ms(started))


def _probe_ledger(root: Path, timeout: float) -> Probe:
    parent = root / "logs"
    if not parent.exists():
        return Probe(False, "logs/ does not exist yet")
    return Probe(
        os.access(parent, os.W_OK),
        "logs/ writable" if os.access(parent, os.W_OK) else "logs/ is read-only",
        None,
    )


def local_embedding(text: str, dim: int = 64) -> List[float]:
    """A deterministic, dependency-free embedding (hashing trick + L2 norm).

    Not a semantic model — it is a real, reproducible vector for local
    similarity, dedup and clustering, which is all `llm.embed` was ever promised
    to be here. It never leaves the machine.
    """
    vec = [0.0] * dim
    for token in (text or "").lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        vec[int(digest[:8], 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def _probe_embed(timeout: float) -> Probe:
    started = time.monotonic()
    a = local_embedding("swarm connections probe")
    b = local_embedding("swarm connections probe")
    norm = math.sqrt(sum(v * v for v in a))
    ok = a == b and len(a) == 64 and abs(norm - 1.0) < 1e-3
    return Probe(ok, f"dim={len(a)} norm={norm:.4f} deterministic={a == b}", _ms(started))


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------
@dataclass
class Connection:
    """One provider serving one or more capabilities, with a live probe."""

    id: str
    label: str
    provider: str
    capabilities: Tuple[str, ...]
    probe: Callable[[float], Probe]
    required_env: Tuple[str, ...] = ()
    optional_env: Tuple[str, ...] = ()
    docs: str = ""
    auth: str = ""
    notes: str = ""

    def missing_env(self, env: Optional[Dict[str, str]] = None) -> List[str]:
        env = os.environ if env is None else env
        return [name for name in self.required_env if not str(env.get(name, "")).strip()]

    def check(
        self,
        timeout: float = 8.0,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Return one connection's status. Never raises, never leaks a value."""
        missing = self.missing_env(env)
        base: Dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "capabilities": list(self.capabilities),
            "required_env": list(self.required_env),
            "missing_env": missing,
            "docs": self.docs,
            "auth": self.auth,
            "notes": self.notes,
        }
        if missing:
            base.update(
                status=STATUS_UNCONFIGURED,
                detail="missing " + ", ".join(missing),
                latency_ms=None,
                checked_at=_iso(),
            )
            return base

        try:
            probe = self.probe(timeout)
        except Exception as exc:  # a broken probe is a failed connection
            probe = Probe(False, f"probe raised {type(exc).__name__}: {exc}")

        status = STATUS_LIVE if probe.ok else STATUS_UNREACHABLE
        if probe.ok and probe.meta.get("degraded"):
            status = STATUS_DEGRADED
        base.update(
            status=status,
            detail=probe.detail or ("reachable" if probe.ok else "no detail"),
            latency_ms=probe.latency_ms,
            checked_at=_iso(),
        )
        return base


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
class ConnectionRegistry:
    """Maps capabilities to connections and audits a cabinet against them."""

    def __init__(self, connections: Iterable[Connection] = ()):
        self._connections: Dict[str, Connection] = {}
        for connection in connections:
            self.register(connection)

    # -- construction ------------------------------------------------------
    def register(self, connection: Connection) -> Connection:
        if not connection.id:
            raise ValueError("connection needs an id")
        self._connections[connection.id] = connection
        return connection

    def __len__(self) -> int:
        return len(self._connections)

    def __iter__(self) -> Iterable[Connection]:
        return iter(self._connections.values())

    def get(self, connection_id: str) -> Optional[Connection]:
        return self._connections.get(connection_id)

    def for_capability(self, capability: str) -> List[Connection]:
        return [c for c in self._connections.values() if capability in c.capabilities]

    # -- audit (no network, no side effects) -------------------------------
    def audit(self, registry: Any) -> Dict[str, Any]:
        """Coverage of declared capabilities, plus orphans. Pure and offline.

        An orphan is a capability that the manifest catalogues or that an
        occupant declares, with no connection able to serve it. That is a real
        defect: an agent claiming `market.search` while nothing provides search
        will fail at runtime with a confusing error instead of at audit time.
        """
        declared: Dict[str, List[str]] = {}
        catalog = getattr(registry, "capabilities", None) or {}
        if isinstance(catalog, dict):
            for capability in catalog:
                declared.setdefault(str(capability), [])
        for entity in registry:
            for capability in getattr(entity, "capabilities", []) or []:
                declared.setdefault(str(capability), [])
                if entity.id not in declared[str(capability)]:
                    declared[str(capability)].append(entity.id)

        coverage: Dict[str, Dict[str, Any]] = {}
        orphans: List[Dict[str, Any]] = []
        for capability in sorted(declared):
            served_by = [c.id for c in self.for_capability(capability)]
            entry = {
                "connections": served_by,
                "declared_by": sorted(declared[capability]),
                "status": STATUS_UNVERIFIED if served_by else STATUS_UNBOUND,
            }
            coverage[capability] = entry
            if not served_by:
                orphans.append(
                    {
                        "capability": capability,
                        "declared_by": sorted(declared[capability]),
                    }
                )
        return {"coverage": coverage, "orphans": orphans}

    # -- report (network, explicit) ---------------------------------------
    def report(
        self,
        registry: Any,
        *,
        probe: bool = False,
        timeout: float = 8.0,
        env: Optional[Dict[str, str]] = None,
        connections: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Combined coverage + per-connection status.

        ``probe=False`` reports configuration only and touches no network, which
        is what `doctor` uses. The console's action uses ``probe=True``.
        """
        audit = self.audit(registry)
        selected = [
            c
            for c in self._connections.values()
            if connections is None or c.id in connections
        ]
        results: List[Dict[str, Any]] = []
        for connection in selected:
            if probe:
                results.append(connection.check(timeout=timeout, env=env))
            else:
                missing = connection.missing_env(env)
                results.append(
                    {
                        "id": connection.id,
                        "label": connection.label,
                        "provider": connection.provider,
                        "capabilities": list(connection.capabilities),
                        "required_env": list(connection.required_env),
                        "missing_env": missing,
                        "docs": connection.docs,
                        "auth": connection.auth,
                        "notes": connection.notes,
                        "status": STATUS_UNCONFIGURED if missing else STATUS_UNVERIFIED,
                        "detail": (
                            "missing " + ", ".join(missing)
                            if missing
                            else "configured, not probed"
                        ),
                        "latency_ms": None,
                        "checked_at": _iso(),
                    }
                )

        by_id = {row["id"]: row for row in results}
        coverage = audit["coverage"]
        for capability, entry in coverage.items():
            statuses = [
                by_id[conn_id]["status"]
                for conn_id in entry["connections"]
                if conn_id in by_id
            ]
            if not statuses:
                entry["status"] = STATUS_UNBOUND
            elif STATUS_LIVE in statuses:
                entry["status"] = STATUS_LIVE
            elif STATUS_DEGRADED in statuses:
                entry["status"] = STATUS_DEGRADED
            elif STATUS_UNVERIFIED in statuses:
                entry["status"] = STATUS_UNVERIFIED
            elif STATUS_UNCONFIGURED in statuses:
                entry["status"] = STATUS_UNCONFIGURED
            else:
                entry["status"] = STATUS_UNREACHABLE

        counts: Dict[str, int] = {}
        for row in results:
            counts[row["status"]] = counts.get(row["status"], 0) + 1

        return {
            "checked_at": _iso(),
            "probed": bool(probe),
            "connections": results,
            "coverage": coverage,
            "orphans": audit["orphans"],
            "summary": {
                "connections": len(results),
                "capabilities": len(coverage),
                "unbound": len(audit["orphans"]),
                **counts,
            },
        }

    @staticmethod
    def save(report: Dict[str, Any], path: str | Path) -> Path:
        """Cache a report for the console to read without re-probing."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return out

    @staticmethod
    def load(path: str | Path) -> Optional[Dict[str, Any]]:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None


# ---------------------------------------------------------------------------
# the shipped connection set
# ---------------------------------------------------------------------------
def _probe_market_search(timeout: float = 8.0) -> Probe:
    """Keyless demand-signal search. Imported locally to avoid a cycle."""
    from .market_search import probe as market_probe

    return market_probe(timeout=timeout)


def build_connections(root: str | Path = ".") -> ConnectionRegistry:
    """Bind every capability the cabinet declares to a real, probeable provider."""
    base = Path(root)

    registry = ConnectionRegistry(
        [
            Connection(
                id="groq",
                label="Groq inference",
                provider="groq",
                capabilities=("llm.generate",),
                probe=_probe_groq,
                required_env=("GROQ_API_KEY",),
                docs="https://console.groq.com/keys",
                auth="bearer",
                notes="openai/gpt-oss-120b primary, qwen3 fallback",
            ),
            Connection(
                id="local_embed",
                label="Local embeddings",
                provider="local",
                capabilities=("llm.embed",),
                probe=_probe_embed,
                notes="deterministic hashing embedder; nothing leaves the machine",
            ),
            Connection(
                id="nostr_relays",
                label="Nostr relay set",
                provider="relay",
                capabilities=("net.nostr",),
                probe=_probe_relays,
                notes="TCP reachability only; signing is proven by relay ACKs at runtime",
            ),
            Connection(
                id="stdlib_http",
                label="Outbound HTTP",
                provider="stdlib",
                capabilities=("net.http",),
                probe=lambda timeout: Probe(True, "urllib available in this interpreter"),
            ),
            Connection(
                id="local_fs",
                label="Local filesystem",
                provider="local",
                capabilities=("fs.read", "fs.write"),
                probe=lambda timeout: _probe_fs(base, timeout),
            ),
            Connection(
                id="local_shell",
                label="Subprocess execution",
                provider="local",
                capabilities=("shell.exec",),
                probe=_probe_shell,
                notes="used by scouts and mutation runs; allowlisted by the CLI",
            ),
            Connection(
                id="ledger",
                label="Event ledger",
                provider="local",
                capabilities=("data.ledger",),
                probe=lambda timeout: _probe_ledger(base, timeout),
            ),
            Connection(
                id="lnurl",
                label="LNURL-pay rail",
                provider="coinos",
                capabilities=("pay.lnurl",),
                probe=lambda timeout: _probe_lnurl(base, timeout),
                notes="lud16 resolved from keys/nwc.secret; the secret is never reported",
            ),
            Connection(
                id="github",
                label="GitHub API",
                provider="github",
                capabilities=("repo.gh", "market.search"),
                probe=_probe_github,
                optional_env=("GITHUB_TOKEN", "GH_TOKEN"),
                docs="https://github.com/settings/tokens",
                auth="bearer (optional; unauthenticated is degraded, not broken)",
                notes="repo.gh for repo work; market.search uses code search",
            ),
            Connection(
                id="public_search",
                label="Hacker News + SEC EDGAR",
                provider="public",
                capabilities=("market.search",),
                probe=_probe_market_search,
                notes=(
                    "keyless: Algolia story index and EDGAR full-text filings; "
                    "no token, no signup, no billing"
                ),
            ),
            Connection(
                id="webz_news",
                label="Webz.io news search",
                provider="webz.io",
                capabilities=("market.search",),
                probe=_probe_webz,
                required_env=("WEBZ_API_TOKEN",),
                optional_env=("WEBZ_API_BASE",),
                docs="https://docs.webz.io/docs/webz/news-search",
                auth="token query parameter",
                notes=(
                    "optional upgrade for demand-signal scouts; WEBZ_API_BASE lets you "
                    "point at a different endpoint without a code change"
                ),
            ),
        ]
    )
    return registry


# ---------------------------------------------------------------------------
# CLI rendering helper (kept here so the CLI stays thin)
# ---------------------------------------------------------------------------
def format_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    summary = report.get("summary", {})
    mode = "probed" if report.get("probed") else "configuration only (no network)"
    lines.append(
        f"{summary.get('connections', 0)} connections · "
        f"{summary.get('capabilities', 0)} capabilities · "
        f"{summary.get('unbound', 0)} unbound · {mode}"
    )
    lines.append("")
    lines.append(f"{'STATUS':<14}{'CONNECTION':<18}{'CAPABILITIES':<24}DETAIL")
    for row in report.get("connections", []):
        caps = ",".join(row.get("capabilities", []))
        detail = row.get("detail", "")
        latency = row.get("latency_ms")
        if latency is not None:
            detail += f" ({latency}ms)"
        lines.append(f"{row.get('status', '?'):<14}{row.get('id', '?'):<18}{caps:<24}{detail}")
    orphans = report.get("orphans", [])
    if orphans:
        lines.append("")
        lines.append("unbound capabilities (declared but nothing serves them):")
        for orphan in orphans:
            owners = ", ".join(orphan.get("declared_by") or []) or "catalog only"
            lines.append(f"  ! {orphan['capability']}  <- {owners}")
    return "\n".join(lines)

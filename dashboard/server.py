#!/usr/bin/env python3
"""
dashboard.server — the operator console
=======================================

Zero dependencies: stdlib ``http.server`` plus Server-Sent Events. Nothing to
build, nothing to install, no bundler, no node_modules. It runs on the same bare
Python the daemon runs on.

    python3 -m dashboard.server            # http://127.0.0.1:8787
    python3 -m dashboard.server --port 9000 --host 0.0.0.0

SECURITY POSTURE (deliberate, not incidental)
---------------------------------------------
* Binds **127.0.0.1** by default. Nothing is exposed unless you pass --host.
* Reading is unauthenticated because reading is local-only and harmless.
* Every *mutating* action requires ``DASHBOARD_TOKEN`` in the environment. With
  no token configured the console is read-only and says so — it never silently
  performs writes because someone happened to reach the port.
* Action subprocesses run with an argument list (no shell), a timeout, and a
  fixed allowlist. The console cannot be turned into a remote shell.
* Response bodies never include the wallet secret, the signing key or the
  preimage of a settled payment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.feed import snapshot  # noqa: E402

__all__ = ["serve", "build_parser", "main", "ACTIONS"]

UI_PATH = HERE / "ui.html"
_ACTION_LOCK = threading.Lock()

#: Allowlisted operator actions. (module, args, human label)
ACTIONS: Dict[str, Tuple[str, list, str]] = {
    "doctor": ("swarm.cli", ["doctor"], "validate manifest, ledger and skill store"),
    "breaker_preflight": (
        "swarm.cli",
        ["breaker", "preflight"],
        "manually release the Janus Guard hard lock",
    ),
    "seed_skills": ("swarm.cli", ["skills", "seed"], "seed the cabinet's skills"),
    "scout_discover": ("swarm.cli", ["scout", "discover"], "mine the ledger for skills"),
    "probe_connections": (
        "swarm.cli",
        ["connections", "--probe", "--save"],
        "probe every provider and cache the report for this console",
    ),
}


def _run_action(name: str) -> Dict[str, Any]:
    """Run one allowlisted action. Never a shell, never unbounded."""
    spec = ACTIONS.get(name)
    if spec is None:
        return {"ok": False, "error": f"unknown action {name!r}"}
    module, args, label = spec
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "ok": proc.returncode == 0,
            "action": name,
            "label": label,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
            "elapsed_s": round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "action": name, "error": "timed out after 60s"}
    except Exception as exc:
        return {"ok": False, "action": name, "error": f"{type(exc).__name__}: {exc}"}


def _settlements_db() -> Path:
    """The ledger this console is reading.

    Defaults to the production ledger. `SWARM_SETTLEMENTS_DB` points the console
    at a demo ledger instead, which is how a seeded console is shown without
    inventing rows in the real money table. Same for `SWARM_JOBS_LOG`.
    """
    override = os.environ.get("SWARM_SETTLEMENTS_DB")
    return Path(override) if override else ROOT / "nostr_dvm" / "logs" / "settlements.db"


def _export_ledger() -> Dict[str, Any]:
    from nostr_dvm.settlement import SettlementLedger

    db = _settlements_db()
    if not db.exists():
        return {"ok": False, "error": "no settlement ledger yet"}
    out = db.with_name("settlements_export.jsonl")
    ledger = SettlementLedger(db)
    try:
        count = ledger.to_jsonl(out)
    finally:
        ledger.close()
    return {"ok": True, "action": "export", "rows": count, "path": str(out)}


class Handler(BaseHTTPRequestHandler):
    server_version = "swarm-console/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter, still useful
        sys.stderr.write("[console] %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _token_ok(self, supplied: Optional[str]) -> bool:
        expected = os.environ.get("DASHBOARD_TOKEN", "")
        if not expected:
            return False  # no token configured => read-only console
        return supplied == expected

    def _snapshot(self) -> Dict[str, Any]:
        settlement_db = os.environ.get("SWARM_SETTLEMENTS_DB")
        jobs_log = os.environ.get("SWARM_JOBS_LOG")
        data = snapshot(ROOT, settlement_db=settlement_db, jobs_log=jobs_log)
        if settlement_db:
            data["demo"] = ".demo." in Path(settlement_db).name
            data["settlements_path"] = settlement_db
        return data

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = UI_PATH.read_bytes()
            except OSError:
                self._send(500, b"ui.html missing", "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            try:
                self._json(self._snapshot())
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        if path == "/api/actions":
            self._json(
                {
                    "writable": bool(os.environ.get("DASHBOARD_TOKEN")),
                    "actions": [
                        {"id": key, "label": spec[2]} for key, spec in ACTIONS.items()
                    ]
                    + [{"id": "export", "label": "export the settlement ledger"}],
                }
            )
            return
        if path.startswith("/api/job/"):
            job_id = path.rsplit("/", 1)[-1]
            try:
                from nostr_dvm.settlement import SettlementLedger

                db = _settlements_db()
                if not db.exists():
                    self._json({"error": "no settlement ledger yet"}, 404)
                    return
                ledger = SettlementLedger(db)
                try:
                    trail = ledger.trail(job_id)
                finally:
                    ledger.close()
                self._json({"job_id": job_id, "trail": trail})
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        if path == "/api/stream":
            self._stream()
            return
        if path == "/healthz":
            self._json({"ok": True, "ts": time.time()})
            return
        self._json({"error": "not found", "path": path}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._json({"ok": False, "error": "body must be JSON"}, 400)
            return

        supplied = self.headers.get("X-Dashboard-Token") or body.get("token")
        if not self._token_ok(supplied):
            self._json(
                {
                    "ok": False,
                    "error": "read-only console: set DASHBOARD_TOKEN to enable actions",
                },
                403,
            )
            return

        if path != "/api/action":
            self._json({"ok": False, "error": "not found"}, 404)
            return

        action = str(body.get("action") or "")
        with _ACTION_LOCK:  # one operator action at a time
            if action == "export":
                result = _export_ledger()
            else:
                result = _run_action(action)
        self._json(result)

    # -- SSE ---------------------------------------------------------------
    def _stream(self) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except OSError:
            return
        try:
            while True:
                payload = self._snapshot()
                chunk = "data: " + json.dumps(payload, default=str) + "\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                time.sleep(2.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # the tab went away; nothing to clean up
        except Exception:
            return


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    writable = bool(os.environ.get("DASHBOARD_TOKEN"))
    mode = "read-write (token set)" if writable else "read-only (no DASHBOARD_TOKEN)"
    print(f"[console] http://{host}:{port}/  code={ROOT}  {mode}", flush=True)
    print("[console] Ctrl-C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] stopped", flush=True)
    finally:
        httpd.server_close()


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="dashboard", description="Operator console for the passive income swarm"
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=8787)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

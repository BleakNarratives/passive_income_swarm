#!/usr/bin/env python3
"""
dvm_worker.py — NIP-90 Data Vending Machine worker

DNA_TAG: ORIGIN=BleakNarratives/passive_income_swarm | PILLAR=nostr_dvm | LAST_SYNC=2026-11-08

WHAT THIS IS:
  A live Nostr DVM (Data Vending Machine). Listens for NIP-90 job requests
  (kind 5002 text-generation, 5003 text-summarization) on public relays,
  serves them via Groq inference, signs results with BIP340 schnorr, publishes
  back to the relay network.

REVENUE MODEL:
  Two modes, one switch (DVM_PAID_ONLY):

  * Reputation mode (default, DVM_PAID_ONLY=0) — serves jobs free to build relay
    presence. Nothing is billed, and nothing is reported as paid.
  * Paid mode (DVM_PAID_ONLY=1) — every job is invoiced over LNURL-pay and is
    served only after the invoice's verify endpoint reports settlement *for
    that job*. The money path lives in paid_flow.py over settlement.py, and it
    is fail-closed: a rail error means the job is not served, and a request's
    `amount` tag is never treated as proof of payment.

RUN:
  ~/passive_income_swarm-env/bin/python paid_mode_guard.py    # paid mode (guarded)
  ~/passive_income_swarm-env/bin/python dvm_worker.py         # reputation mode
Service: systemctl --user {status,restart} dvm-worker
"""

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import requests
import websockets
from coincurve import PrivateKey

# Payment path — LNURL-pay rail plus the durable job-bound settlement gate.
try:  # running as a script: this directory is on sys.path
    from settlement import SettlementLedger
    from paid_flow import serve_paid_job
except ImportError:  # imported as a package (tests, dashboard)
    from .settlement import SettlementLedger
    from .paid_flow import serve_paid_job

# Payment rail — LNURL-pay (lives alongside this file)
try:
    from payment_rail import PaymentRail
    _PAYMENT_RAIL = PaymentRail()
    _PAYMENT_RAIL.resolve()  # warm the callback cache
except Exception as e:
    _PAYMENT_RAIL = None
    print(f"[WARN] payment rail offline: {e}")

# ---------------------------------------------------------------------------
# CONFIG

HOME = Path.home()
SWARM = HOME / "passive_income_swarm"
DVM_DIR = SWARM / "nostr_dvm"
KEY_FILE = SWARM / "keys" / "nostr.key"
LOG_FILE = DVM_DIR / "logs" / "jobs.jsonl"
SETTLEMENT_DB = DVM_DIR / "logs" / "settlements.db"

RELAYS = [
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://relay.damus.io",
    # wss://relay.nostr.band dropped 2026-08-25 — dead handshake loop
    # (TimeoutError every ~18s since deploy). Re-add if it recovers.
]

# NIP-90 job request kinds we serve -> result kind mapping
JOB_KINDS = {
    5002: 6002,   # text generation
    5003: 6003,   # text summarization
}

GROQ_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.6-27b"]   # primary -> fallback
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
INFERENCE_TIMEOUT = 45          # seconds
MAX_CONTENT_CHARS = 8000        # refuse absurd payloads
RECONNECT_DELAY = 8             # seconds between relay reconnects
MIN_CALL_INTERVAL = 2.5         # free-tier pacing between Groq calls
RATE_SEMAPHORE = 1              # concurrent inference jobs

PAID_ONLY = os.environ.get("DVM_PAID_ONLY", "0") == "1"   # reputation mode default
PAYMENT_TIMEOUT = int(os.environ.get("DVM_PAYMENT_TIMEOUT", "120"))  # seconds to wait for sats
PAYMENT_POLL = int(os.environ.get("DVM_PAYMENT_POLL", "3"))          # seconds between verify checks

# Fee schedule in millisatoshis. Env-overridable so pricing is an operator
# decision, not a code edit. A requester's own `amount` tag never influences it.
PAID_FEES_MSAT = {
    5002: int(os.environ.get("DVM_FEE_5002_MSAT", "50000")),   # 50 sats text-gen
    5003: int(os.environ.get("DVM_FEE_5003_MSAT", "20000")),   # 20 sats summarize
}
PAID_FEE_DEFAULT_MSAT = int(os.environ.get("DVM_FEE_DEFAULT_MSAT", "50000"))

# Inbound throttling (2026-08-26 — P0 close: a requester could previously
# fire unlimited jobs, piling tasks onto the loop and draining the Groq free
# tier / minting unpaid invoices. All env-overridable.)
DVM_RATE_JOBS = int(os.environ.get("DVM_RATE_JOBS", "10"))        # jobs per window per pubkey
DVM_RATE_WINDOW = int(os.environ.get("DVM_RATE_WINDOW", "60"))    # seconds
DVM_MAX_INFLIGHT = int(os.environ.get("DVM_MAX_INFLIGHT", "16"))  # global queued+active jobs
DVM_MAX_PER_PUBKEY = int(os.environ.get("DVM_MAX_PER_PUBKEY", "2"))  # concurrent jobs per requester


def load_groq_key():
    """Find GROQ_API_KEY: env first, then known .env files. Never print it."""
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    for candidate in [SWARM / ".env", HOME / "Official-Vertical-AI-Boardroom" / ".env"]:
        try:
            for line in candidate.read_text().splitlines():
                if line.startswith("GROQ_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def load_private_key():
    raw = KEY_FILE.read_text().strip()
    # tolerate hex, bech32 nsec is out of scope until needed
    if len(raw) != 64:
        raise ValueError(f"{KEY_FILE}: expected 64-char hex privkey, got {len(raw)} chars")
    return PrivateKey(bytes.fromhex(raw))


PRIV = load_private_key()
PUBKEY_XONLY = PRIV.public_key.format(compressed=False)[1:33].hex()
GROQ_KEY = load_groq_key()

class _SeenRing:
    """Bounded FIFO of processed event ids.

    The previous implementation cleared the whole set on overflow, which let the
    oldest events be served a second time — a free-job coupon on a busy relay.
    Evicting one id at a time keeps memory bounded without forgetting everything.
    """

    def __init__(self, capacity: int = 4096):
        self.capacity = max(1, int(capacity))
        self._order = deque()
        self._ids = set()

    def __contains__(self, item):
        return item in self._ids

    def add(self, item):
        if item in self._ids:
            return
        self._ids.add(item)
        self._order.append(item)
        while len(self._order) > self.capacity:
            self._ids.discard(self._order.popleft())

    def __len__(self):
        return len(self._ids)

    def clear(self):
        self._ids.clear()
        self._order.clear()


SEEN = _SeenRing(4096)   # processed event ids (dedup across relays)

# Durable settlement ledger, opened lazily so importing this module (tests,
# dashboards, `--help`) never creates runtime state.
_SETTLEMENT_LOCK = threading.Lock()
_SETTLEMENT_LEDGER = None


def settlement_ledger():
    """Return the process-wide settlement ledger, creating it on first use."""
    global _SETTLEMENT_LEDGER
    with _SETTLEMENT_LOCK:
        if _SETTLEMENT_LEDGER is None:
            _SETTLEMENT_LEDGER = SettlementLedger(
                SETTLEMENT_DB, default_timeout_s=PAYMENT_TIMEOUT
            )
        return _SETTLEMENT_LEDGER


# ---------------------------------------------------------------------------
# NOSTR PLUMBING

def serialize_event(pubkey, created_at, kind, tags, content):
    data = json.dumps([0, pubkey, created_at, kind, tags, content],
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).digest()


def build_event(kind, tags, content):
    created_at = int(time.time())
    did = serialize_event(PUBKEY_XONLY, created_at, kind, tags, content)
    sig = PRIV.sign_schnorr(did, os.urandom(32))
    return {
        "pubkey": PUBKEY_XONLY,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "id": did.hex(),
        "sig": sig.hex(),
    }


async def publish(ws, event, relay_name):
    await ws.send(json.dumps(["EVENT", event]))
    # relay will answer ["OK", id, true/false, msg] — caller's recv loop surfaces it


def log_job(record):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# INFERENCE

PROMPTS = {
    5002: "You are a concise AI assistant serving a paid Nostr job. Answer directly, no preamble.",
    5003: "Summarize the following text faithfully and concisely. Preserve key facts and numbers.",
}


_last_call = 0.0
_infer_sem = threading.Semaphore(RATE_SEMAPHORE)


def _pace():
    """Block until MIN_CALL_INTERVAL since last call (free-tier pacing)."""
    global _last_call
    wait = MIN_CALL_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def run_inference(kind, content):
    system = PROMPTS.get(kind, PROMPTS[5002])
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    last_err = None
    with _infer_sem:
        for attempt in range(3):
            _pace()
            model = GROQ_MODELS[attempt % len(GROQ_MODELS)]
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content[:MAX_CONTENT_CHARS]},
                ],
                "temperature": 0.4,
                "max_tokens": 1024,
            }
            try:
                r = requests.post(GROQ_URL, json=payload, headers=headers,
                                  timeout=INFERENCE_TIMEOUT)
                if r.status_code == 429:
                    time.sleep(6 * (attempt + 1))     # rate limited: back off
                    last_err = RuntimeError(f"{model}: 429 rate limited")
                    continue
                r.raise_for_status()
                out = r.json()["choices"][0]["message"]["content"]
                if out:
                    return f"{out}\n\n-- served by {model} via nostr_dvm (BleakNarratives swarm)"
                last_err = RuntimeError(f"{model}: empty completion")
            except Exception as e:
                last_err = e
    raise last_err


# ---------------------------------------------------------------------------
# INBOUND THROTTLE

_req_history = {}        # pubkey -> deque[ts] (sliding window)
_inflight = 0            # global queued + active jobs
_pubkey_inflight = {}    # pubkey -> int (per-requester concurrency)
_slot_lock = threading.Lock()


def _rate_limited(requester):
    """Sliding-window per-pubkey job check. True = over limit, reject."""
    now = time.time()
    q = _req_history.setdefault(requester, deque())
    while q and q[0] <= now - DVM_RATE_WINDOW:
        q.popleft()
    if len(q) >= DVM_RATE_JOBS:
        return True
    q.append(now)
    if len(_req_history) > 4096:   # bound memory on hostile pubkey floods
        _req_history.clear()
    return False


def _try_acquire_slot(requester):
    """Global + per-pubkey concurrency gate. False = saturated, reject."""
    global _inflight
    with _slot_lock:
        if _inflight >= DVM_MAX_INFLIGHT:
            return False
        if _pubkey_inflight.get(requester, 0) >= DVM_MAX_PER_PUBKEY:
            return False
        _inflight += 1
        _pubkey_inflight[requester] = _pubkey_inflight.get(requester, 0) + 1
    return True


def _release_slot(requester):
    global _inflight
    with _slot_lock:
        _inflight -= 1
        n = _pubkey_inflight.get(requester, 0) - 1
        if n <= 0:
            _pubkey_inflight.pop(requester, None)
        else:
            _pubkey_inflight[requester] = n


# ---------------------------------------------------------------------------
# JOB HANDLING

def extract_amount_msat(event):
    """Read the requester's claimed amount. A CLAIM ONLY — never a receipt."""
    for tag in event.get("tags", []):
        if len(tag) >= 2 and tag[0] == "amount":
            try:
                return int(tag[1])
            except ValueError:
                pass
    return 0


async def handle_job(ws, event, relay_name):
    kind = event.get("kind")
    result_kind = JOB_KINDS.get(kind)
    event_id = event.get("id", "")
    requester = event.get("pubkey", "")

    # --- inbound throttle (2026-08-26, P0 close) ---
    # Reject BEFORE dedup-adjacent work, payment gating, or inference so a
    # hostile pubkey can't drain the Groq free tier or mint unpaid invoices.
    if _rate_limited(requester):
        fb = build_event(7000, [["e", event_id], ["p", requester],
                                ["status", "error"]],
                         "rate limited — too many jobs, slow down")
        await publish(ws, fb, relay_name)
        log_job({"ts": time.time(), "job_id": event_id, "kind": kind,
                 "relay": relay_name, "requester": requester,
                 "outcome": "rate_limited"})
        print(f"[THROTTLE] {requester[:8]} rate limited", flush=True)
        return
    if not _try_acquire_slot(requester):
        fb = build_event(7000, [["e", event_id], ["p", requester],
                                ["status", "error"]],
                         "busy — too many jobs in flight, retry shortly")
        await publish(ws, fb, relay_name)
        log_job({"ts": time.time(), "job_id": event_id, "kind": kind,
                 "relay": relay_name, "requester": requester,
                 "outcome": "saturated"})
        print(f"[THROTTLE] {requester[:8]} saturated", flush=True)
        return
    try:
        await _handle_job_inner(ws, event, relay_name,
                                kind, result_kind, event_id, requester)
    finally:
        _release_slot(requester)


async def _handle_job_inner(ws, event, relay_name, kind, result_kind,
                            event_id, requester):
    content = ""
    try:
        payload = json.loads(event.get("content") or "{}")
        content = str(payload.get("input", "") or "")
    except json.JSONDecodeError:
        content = event.get("content") or ""

    # A requester-supplied `amount` tag is a CLAIM, not a receipt. It is logged
    # for audit and never authorises service. Money may only be taken by
    # paid_flow.serve_paid_job, on a verified settlement bound to this job id.
    claimed_msat = extract_amount_msat(event)

    if not content.strip():
        return
    if len(content) > MAX_CONTENT_CHARS * 2:
        status = "error: input too large"
        feedback = build_event(7000, [["e", event_id], ["p", requester],
                                      ["status", "error"]], status)
        await publish(ws, feedback, relay_name)
        log_job({"ts": time.time(), "job_id": event_id, "kind": kind,
                 "relay": relay_name, "outcome": "rejected_too_large"})
        return

    if PAID_ONLY:
        # Every paid-mode job goes through the durable, job-bound gate. There is
        # deliberately no fall-through: a rail error means the job is not served.
        await _serve_paid(ws, event, relay_name, kind, result_kind,
                          event_id, requester, content)
        return

    # feedback: processing
    fb = build_event(7000, [["e", event_id], ["p", requester],
                            ["status", "processing"]], "")
    await publish(ws, fb, relay_name)

    started = time.time()
    try:
        output = await asyncio.to_thread(run_inference, kind, content)
        outcome = "served_free"
    except Exception as e:
        output = ""
        outcome = f"inference_error: {type(e).__name__}"

    if output:
        tags = [["e", event_id], ["p", requester],
                ["request", json.dumps(event, separators=(",", ":"))]]
        if claimed_msat:
            tags.append(["amount", str(claimed_msat)])
        result = build_event(result_kind, tags, output)
        await publish(ws, result, relay_name)

    log_job({
        "ts": time.time(),
        "job_id": event_id,
        "kind": kind,
        "result_kind": result_kind,
        "relay": relay_name,
        "requester": requester,
        "amount_msat": 0,               # nothing was collected in reputation mode
        "claimed_amount_msat": claimed_msat,
        "input_chars": len(content),
        "output_chars": len(output),
        "latency_s": round(time.time() - started, 2),
        "outcome": outcome,
    })
    print(f"[JOB] {event_id[:8]} kind={kind} free -> {outcome} "
          f"({round(time.time()-started,1)}s)")


async def _serve_paid(ws, event, relay_name, kind, result_kind,
                      event_id, requester, content):
    """Serve one paid-mode job through the job-bound settlement gate."""
    fee_msat = PAID_FEES_MSAT.get(kind, PAID_FEE_DEFAULT_MSAT)
    delivered = {"event": ""}

    async def notify(status, body="", extra=()):
        tags = [["e", event_id], ["p", requester], ["status", status]]
        tags += [list(t) for t in extra]
        await publish(ws, build_event(7000, tags, body), relay_name)

    async def deliver(output):
        tags = [["e", event_id], ["p", requester],
                ["request", json.dumps(event, separators=(",", ":"))],
                ["amount", str(fee_msat)]]
        result = build_event(result_kind, tags, output)
        delivered["event"] = result.get("id", "")
        await publish(ws, result, relay_name)

    started = time.time()
    outcome = await serve_paid_job(
        event=event,
        kind=kind,
        rail=_PAYMENT_RAIL,
        ledger=settlement_ledger(),
        infer=run_inference,
        notify=notify,
        deliver=deliver,
        fee_msat=fee_msat,
        timeout_s=PAYMENT_TIMEOUT,
        poll_s=PAYMENT_POLL,
    )

    log_job({
        "ts": time.time(),
        "job_id": event_id,
        "kind": kind,
        "result_kind": result_kind,
        "relay": relay_name,
        "requester": requester,
        "amount_msat": outcome.fee_msat if outcome.outcome == "served_paid" else 0,
        "claimed_amount_msat": extract_amount_msat(event),
        "fee_msat": fee_msat,
        "preimage": (outcome.preimage or "")[:16],
        "input_chars": len(content),
        "output_chars": len(outcome.output),
        "latency_s": round(time.time() - started, 2),
        "outcome": outcome.outcome,
        "detail": outcome.detail,
        "result_event": delivered["event"],
    })
    print(f"[JOB] {event_id[:8]} kind={kind} PAID -> {outcome.outcome} "
          f"({round(time.time()-started,1)}s) {outcome.detail}", flush=True)


# ---------------------------------------------------------------------------
# RELAY LOOP

async def relay_loop(relay):
    sub = f"dvm-{int(time.time())}"
    while True:
        try:
            async with websockets.connect(relay, open_timeout=10,
                                          ping_interval=30) as ws:
                print(f"[CONNECTED] {relay}")
                await ws.send(json.dumps(["REQ", sub,
                    {"kinds": list(JOB_KINDS), "since": int(time.time())}]))
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, list) or len(data) < 3:
                        continue
                    if data[0] == "EVENT" and isinstance(data[2], dict):
                        ev = data[2]
                        eid = ev.get("id", "")
                        if ev.get("kind") in JOB_KINDS and eid not in SEEN:
                            SEEN.add(eid)  # bounded eviction, never a full wipe
                            asyncio.create_task(handle_job(ws, ev, relay))
                    elif data[0] == "OK":
                        ok = "ACCEPTED" if len(data) > 2 and data[2] else "REJECTED"
                        print(f"[RELAY] {relay} {ok} {data[1][:8]}")
                    elif data[0] == "NOTICE":
                        print(f"[NOTICE] {relay}: {data[-1]}")
        except Exception as e:
            print(f"[RETRY] {relay}: {type(e).__name__} {e}")
            await asyncio.sleep(RECONNECT_DELAY)


async def main():
    print(f"[DVM] pubkey={PUBKEY_XONLY[:16]}... relays={len(RELAYS)} "
          f"groq={'yes' if GROQ_KEY else 'MISSING'} paid_only={PAID_ONLY} "
          f"models={GROQ_MODELS}")
    if not GROQ_KEY:
        print("[FATAL] GROQ_API_KEY not found (env or Boardroom .env)")
        sys.exit(1)
    if PAID_ONLY:
        # Fail closed at boot rather than discovering an unbillable rail after a
        # customer has already been asked for sats.
        if _PAYMENT_RAIL is None:
            print("[FATAL] DVM_PAID_ONLY=1 but the payment rail is offline; "
                  "refusing to serve unbillable jobs")
            sys.exit(1)
        settlement_ledger()
        print(f"[DVM] paid mode: job-bound settlement ledger at {SETTLEMENT_DB} "
              f"fees_msat={PAID_FEES_MSAT}")
    await asyncio.gather(*(relay_loop(r) for r in RELAYS))


if __name__ == "__main__":
    asyncio.run(main())

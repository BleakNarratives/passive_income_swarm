#!/usr/bin/env python3
"""
dvm_worker.py — NIP-90 Data Vending Machine worker

DNA_TAG: ORIGIN=BleakNarratives/passive_income_swarm | PILLAR=nostr_dvm | LAST_SYNC=2026-08-24

WHAT THIS IS:
  A live Nostr DVM (Data Vending Machine). Listens for NIP-90 job requests
  (kind 5002 text-generation, 5003 text-summarization) on public relays,
  serves them via Groq inference (llama-3.3-70b-versatile, free tier),
  signs results with BIP340 schnorr, publishes back to the relay network.

REVENUE MODEL:
  Requesters attach an "amount" tag (millisats) for paid jobs. Every served
  job builds relay reputation; paid jobs are logged separately so earnings
  are auditable from jobs.jsonl once a Lightning payout rail (NIP-47 NWC /
  lightning address) is connected. Payment hooks are stubbed at
  PAYMENT_GATE below — serving starts immediately, collection activates
  when the wallet rail lands.

REPLACES:
  bin/listener.py + bin/responder.py skeletons (Moto-era). Old responder
  shelled out to Ollama — dead on this hardware — and never signed or
  published events. This is the real thing.

RUN:
  ~/passive_income_swarm-env/bin/python dvm_worker.py            # daemon
  ... dvm_worker.py --once                                       # single poll cycle
Service: systemctl --user {status,restart} dvm-worker
"""

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import requests
import websockets
from coincurve import PrivateKey

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

SEEN = set()          # processed event ids (dedup across relays)
SEEN_MAX = 4096


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
# JOB HANDLING

def extract_amount_msat(event):
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

    content = ""
    try:
        payload = json.loads(event.get("content") or "{}")
        content = str(payload.get("input", "") or "")
    except json.JSONDecodeError:
        content = event.get("content") or ""

    amount = extract_amount_msat(event)
    paid = amount > 0

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

    # PAYMENT_GATE — LNURL-pay Lightning rail with settlement polling
    # Hybrid: serve free jobs for reputation; gate + poll when DVM_PAID_ONLY=1.
    # Fee schedule: text-gen 50 sats, summarize 20 sats.
    # Flow: generate invoice → publish payment-required → poll verify → serve/expire.
    if PAID_ONLY and not paid:
        fee_msat = 50_000 if kind == 5002 else 20_000
        if _PAYMENT_RAIL:
            try:
                inv = await asyncio.to_thread(
                    _PAYMENT_RAIL.invoice, fee_msat,
                    comment=f"DVM job {event_id[:12]}"
                )
                bolt11_content = json.dumps({
                    "bolt11": inv.bolt11,
                    "amount_msat": fee_msat,
                })
                fb = build_event(7000, [["e", event_id], ["p", requester],
                                        ["status", "payment-required"],
                                        ["amount", str(fee_msat)]], bolt11_content)
                await publish(ws, fb, relay_name)
                print(f"[GATE] invoice {inv.bolt11[:30]}... "
                      f"({fee_msat/1000:.0f} sats) — awaiting payment...", flush=True)

                # Poll for payment settlement
                deadline = time.time() + PAYMENT_TIMEOUT
                settled = False
                preimage = None
                while time.time() < deadline:
                    settled, preimage = await asyncio.to_thread(
                        _PAYMENT_RAIL.check_settled, inv.verify_url
                    )
                    if settled:
                        break
                    await asyncio.sleep(PAYMENT_POLL)

                if not settled:
                    # Timeout — job unpaid
                    fb_timeout = build_event(7000, [["e", event_id], ["p", requester],
                                                    ["status", "payment-timeout"],
                                                    ["amount", str(fee_msat)]], "")
                    await publish(ws, fb_timeout, relay_name)
                    log_job({"ts": time.time(), "job_id": event_id, "kind": kind,
                             "relay": relay_name, "amount_msat": fee_msat,
                             "invoice": inv.bolt11[:40], "outcome": "payment_timeout"})
                    return

                # Paid! Preimage proof logged, continue to inference below
                print(f"[GATE] PAID {fee_msat/1000:.0f} sats! preimage={preimage[:16] if preimage else 'N/A'}...", flush=True)
                amount = fee_msat  # set amount for the job ledger
                paid = True

            except Exception as e:
                print(f"[WARN] payment gate error: {e} — falling through to serve")
                # If payment rail fails entirely, serve the job rather than lose it
        else:
            # Payment rail not loaded — static fallback
            fb = build_event(7000, [["e", event_id], ["p", requester],
                                    ["status", "payment-required"],
                                    ["amount", str(50_000)]], "")
            await publish(ws, fb, relay_name)
            log_job({"ts": time.time(), "job_id": event_id, "kind": kind,
                     "relay": relay_name, "amount_msat": amount, "outcome": "gate_unpaid"})
            return

    # feedback: processing
    fb = build_event(7000, [["e", event_id], ["p", requester],
                            ["status", "processing"]], "")
    await publish(ws, fb, relay_name)

    started = time.time()
    try:
        output = await asyncio.to_thread(run_inference, kind, content)
        outcome = "served_paid" if paid else "served_free"
    except Exception as e:
        output = ""
        outcome = f"inference_error: {type(e).__name__}"

    if output:
        tags = [["e", event_id], ["p", requester],
                ["request", json.dumps(event, separators=(",", ":"))]]
        if amount:
            tags.append(["amount", str(amount)])
        result = build_event(result_kind, tags, output)
        await publish(ws, result, relay_name)

    log_job({
        "ts": time.time(),
        "job_id": event_id,
        "kind": kind,
        "result_kind": result_kind,
        "relay": relay_name,
        "requester": requester,
        "amount_msat": amount,
        "input_chars": len(content),
        "output_chars": len(output),
        "latency_s": round(time.time() - started, 2),
        "outcome": outcome,
    })
    print(f"[JOB] {event_id[:8]} kind={kind} {'PAID' if paid else 'free'} "
          f"-> {outcome} ({round(time.time()-started,1)}s)")


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
                            SEEN.add(eid)
                            if len(SEEN) > SEEN_MAX:
                                SEEN.clear()
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
    await asyncio.gather(*(relay_loop(r) for r in RELAYS))


if __name__ == "__main__":
    asyncio.run(main())

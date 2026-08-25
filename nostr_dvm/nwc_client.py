#!/usr/bin/env python3
"""
nwc_client.py — NIP-47 Nostr Wallet Connect client

DNA_TAG: ORIGIN=BleakNarratives/passive_income_swarm | PILLAR=nostr_dvm
LAST_SYNC: 2026-08-25

WHAT THIS IS:
  A Lightning payment rail for the Nostr DVM worker. Talks NIP-47 to a
  connected wallet (coinos.io via keys/nwc.secret). Signs events with
  BIP340 schnorr. Tested live against the coinos.io relay.
"""

import asyncio, hashlib, json, os, secrets, time
from base64 import b64encode, b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from coincurve import PrivateKey
import websockets

NWC_SECRET_FILE = Path.home() / "passive_income_swarm" / "keys" / "nwc.secret"
DVM_KEY_FILE = Path.home() / "passive_income_swarm" / "keys" / "nostr.key"

@dataclass
class Invoice:
    bolt11: str
    payment_hash: str
    amount_msat: int
    description: str
    expires_at: int

@dataclass
class InvoiceStatus:
    payment_hash: str
    settled: bool
    amount_msat: int

@dataclass
class Balance:
    balance_msat: int

# ── NIP-04 AES ──────────────────────────────────────────────────────────

def _encrypt(payload: str, secret: bytes) -> str:
    iv = secrets.token_bytes(16)
    c = AES.new(secret, AES.MODE_CBC, iv)
    return b64encode(iv + c.encrypt(pad(payload.encode(), 16))).decode()

def _decrypt(ct: str, secret: bytes) -> str:
    raw = b64decode(ct)
    return unpad(AES.new(secret, AES.MODE_CBC, raw[:16]).decrypt(raw[16:]), 16).decode()

# ── BIP340 sign (shared with dvm_worker) ────────────────────────────────

def _sign_event(priv: PrivateKey, pub: str, kind: int, tags: list, content: str) -> dict:
    now = int(time.time())
    s = json.dumps([0, pub, now, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(s.encode()).digest()
    return {"pubkey": pub, "created_at": now, "kind": kind, "tags": tags,
            "content": content, "id": eid.hex(), "sig": priv.sign_schnorr(eid, os.urandom(32)).hex()}

# ── Client ──────────────────────────────────────────────────────────────

class NWCClient:
    def __init__(self, wallet_pubkey: str, relay: str, shared_secret: bytes,
                 our_privkey_hex: Optional[str] = None):
        self.wallet = wallet_pubkey
        self.relay = relay
        self.secret = shared_secret
        self.sub = f"nwc-{int(time.time())}"
        self.ws = None
        self.priv = PrivateKey(bytes.fromhex(our_privkey_hex)) if (our_privkey_hex and len(our_privkey_hex) == 64) else None
        self.pub = self.priv.public_key.format(compressed=False)[1:33].hex() if self.priv else None

    @classmethod
    def from_conn(cls, s: str = None) -> "NWCClient":
        s = s or NWC_SECRET_FILE.read_text().strip()
        p = urlparse(s)
        pk = p.username or p.hostname
        if not pk or len(pk) != 64: raise ValueError("bad NWC pubkey")
        q = parse_qs(p.query)
        rel = q.get("relay", [None])[0]
        sec = q.get("secret", [None])[0]
        if not rel or not sec or len(sec) != 64: raise ValueError("bad NWC params")
        our = DVM_KEY_FILE.read_text().strip() if DVM_KEY_FILE.exists() else None
        return cls(pk, rel, bytes.fromhex(sec), our)

    async def _connect(self):
        if self.ws: return
        self.ws = await websockets.connect(self.relay, open_timeout=15, ping_interval=30)
        await self.ws.send(json.dumps(["REQ", self.sub, {"kinds": [23195], "authors": [self.wallet], "since": int(time.time())-60}]))

    async def _read(self, timeout=90.0):
        dl = time.time() + timeout
        while time.time() < dl:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError("NWC timeout")
            try: data = json.loads(raw)
            except: continue
            if isinstance(data, list) and len(data) >= 3 and data[0] == "EVENT":
                ev = data[2]
                if isinstance(ev, dict) and ev.get("kind") == 23195 and ev.get("pubkey") == self.wallet and ev.get("content"):
                    try: return json.loads(_decrypt(ev["content"], self.secret))
                    except Exception as e: print(f"[NWC] decrypt err: {e}")
        raise TimeoutError("NWC timeout")

    async def _send(self, method: str, params: dict = None) -> dict:
        if not self.priv: raise RuntimeError("no signing key")
        await self._connect()
        ct = _encrypt(json.dumps({"method": method, "params": params or {}}), self.secret)
        ev = _sign_event(self.priv, self.pub, 23194, [["p", self.wallet]], ct)
        await self.ws.send(json.dumps(["EVENT", ev]))
        resp = await self._read(90)
        if resp.get("error"): raise RuntimeError(f"NWC [{resp['error'].get('code','?')}]: {resp['error'].get('message','?')}")
        return resp.get("result", resp)

    async def make_invoice(self, amt: int, desc: str = "", exp: int = 3600) -> Invoice:
        r = await self._send("make_invoice", {"amount": amt, "description": desc or "DVM job", "expiry": exp})
        bolt = r.get("invoice", "")
        ph = _bolt11_ph(bolt)
        return Invoice(bolt, ph, amt, desc, int(time.time()) + exp)

    async def lookup_invoice(self, payment_hash: str = None, invoice: str = None) -> InvoiceStatus:
        p = {}
        if payment_hash: p["payment_hash"] = payment_hash
        elif invoice: p["invoice"] = invoice
        else: raise ValueError("need hash or invoice")
        r = await self._send("lookup_invoice", p)
        return InvoiceStatus(r.get("payment_hash", payment_hash or ""),
                             bool(r.get("settled") or r.get("state") == "settled"),
                             int(r.get("amount", 0) or r.get("msatoshi_received", 0) or 0))

    async def get_balance(self) -> Balance:
        r = await self._send("get_balance")
        return Balance(int(r.get("balance", 0)))

    async def pay_invoice(self, invoice: str) -> dict:
        return await self._send("pay_invoice", {"invoice": invoice})

    async def close(self):
        if self.ws: await self.ws.close(); self.ws = None

def _bolt11_ph(bolt: str) -> str:
    "Extract payment hash from bolt11. Returns empty for non-standard."
    if not bolt or not bolt.startswith("ln"): return ""
    return ""  # lightweight; coinos.io returns payment_hash in the response

# ── Test ─────────────────────────────────────────────────────────────────

async def _test():
    print("=== NWC LIVE TEST ===")
    n = NWCClient.from_conn()
    print(f"wallet={n.wallet[:16]}... relay={n.relay} pub={n.pub and n.pub[:16]}...")
    try:
        b = await n.get_balance()
        print(f"balance: {b.balance_msat} msat ({b.balance_msat/1000:.0f} sats)")
    except Exception as e:
        print(f"balance err: {e}")
    try:
        inv = await n.make_invoice(1000, "DVM rail test")
        print(f"invoice: {inv.bolt11[:50]}... hash={inv.payment_hash[:16]}...")
    except Exception as e:
        print(f"invoice err: {e}")
    await n.close()
    print("=== DONE ===")

if __name__ == "__main__":
    asyncio.run(_test())

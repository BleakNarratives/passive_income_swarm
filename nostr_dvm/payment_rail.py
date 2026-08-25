#!/usr/bin/env python3
"""
payment_rail.py — LNURL-pay Lightning payment rail for the Nostr DVM

DNA_TAG: ORIGIN=BleakNarratives/passive_income_swarm | PILLAR=nostr_dvm
LAST_SYNC: 2026-08-25

WHAT THIS IS:
  A self-contained Lightning payment client using LNURL-pay (HTTP, no
  encryption). Talks to your coinos.io wallet via the public LNURL-pay
  protocol. Generates BOLT11 invoices, checks settlement status, and
  provides a polling loop for payment confirmation.

  No NIP-47 NWC needed — this works over plain HTTPS. The wallet
  connection string lives in keys/nwc.secret but we only use the
  lud16 part (bleaknarratives@coinos.io) to find the LNURL-pay endpoint.

FLOW:
  1. resolve()     → GET /.well-known/lnurlp/<user> → callback URL + limits
  2. invoice(amt)  → GET callback?amount=<msat> → bolt11 + verify URL
  3. check(verify) → GET verify URL → {settled, preimage}
  4. await_payment(verify, timeout) → poll until settled or timeout

USAGE:
  rail = PaymentRail()
  inv = rail.invoice(10000, comment="DVM job #42")   # 10 sats
  # give inv.bolt11 to user
  result = rail.await_payment(inv.verify_url, timeout=300)
"""

import json, time, urllib.request, urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ── Config ───────────────────────────────────────────────────────────────

NWC_SECRET_PATH = Path.home() / "passive_income_swarm" / "keys" / "nwc.secret"
LNURL_BASE = "https://coinos.io/.well-known/lnurlp"

@dataclass
class LnurlInfo:
    callback: str
    min_msat: int
    max_msat: int
    nostr_pubkey: str
    allows_nostr: bool

@dataclass
class Invoice:
    bolt11: str
    amount_msat: int
    description: str
    verify_url: str
    created_at: float = field(default_factory=time.time)
    settled: bool = False
    preimage: Optional[str] = None

# ── LNURL-pay client ─────────────────────────────────────────────────────

class PaymentRail:
    def __init__(self, lud16: str = None):
        if lud16 is None:
            lud16 = self._read_lud16()
        self.lud16 = lud16
        self.user, self.domain = self._parse_lud16(lud16)
        self._info: Optional[LnurlInfo] = None

    @staticmethod
    def _read_lud16() -> str:
        if NWC_SECRET_PATH.exists():
            raw = NWC_SECRET_PATH.read_text().strip()
            # nostr+walletconnect://...?lud16=bleaknarratives@coinos.io
            for part in raw.split("&"):
                if part.startswith("lud16="):
                    return part.split("=", 1)[1]
        return "bleaknarratives@coinos.io"

    @staticmethod
    def _parse_lud16(lud16: str) -> tuple:
        if "@" in lud16:
            user, domain = lud16.split("@", 1)
            return user, domain
        raise ValueError(f"invalid lightning address: {lud16}")

    def resolve(self) -> LnurlInfo:
        """Fetch LNURL-pay metadata from the wallet's .well-known endpoint."""
        if self._info:
            return self._info
        url = f"https://{self.domain}/.well-known/lnurlp/{self.user}"
        data = self._get_json(url)
        self._info = LnurlInfo(
            callback=data["callback"],
            min_msat=data.get("minSendable", 1000),
            max_msat=data.get("maxSendable", 100000000000),
            nostr_pubkey=data.get("nostrPubkey", ""),
            allows_nostr=data.get("allowsNostr", False),
        )
        return self._info

    def invoice(self, amount_msat: int, comment: str = "", description: str = "") -> Invoice:
        """Generate a BOLT11 invoice for the given millisatoshi amount."""
        info = self.resolve()
        if amount_msat < info.min_msat:
            raise ValueError(f"amount {amount_msat} below minimum {info.min_msat}")
        if amount_msat > info.max_msat:
            raise ValueError(f"amount {amount_msat} above maximum {info.max_msat}")

        url = f"{info.callback}?amount={amount_msat}"
        if comment:
            url += f"&comment={urllib.parse.quote(comment, safe='')}"

        data = self._get_json(url)
        bolt11 = data.get("pr", "")
        verify_url = data.get("verify", "")

        return Invoice(
            bolt11=bolt11,
            amount_msat=amount_msat,
            description=description or comment,
            verify_url=verify_url,
        )

    def check(self, verify_url: str) -> dict:
        """Check if an invoice has been paid. Returns raw response dict."""
        return self._get_json(verify_url)

    def check_settled(self, verify_url: str) -> tuple:
        """Check payment status. Returns (settled: bool, preimage: str|None)."""
        data = self._get_json(verify_url)
        settled = bool(data.get("settled", False))
        preimage = data.get("preimage") or None
        return settled, preimage

    def await_payment(self, verify_url: str, timeout: float = 300, poll_interval: float = 3) -> tuple:
        """Block (with polling) until the invoice is paid or timeout expires.
        Returns (settled: bool, preimage: str|None)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            settled, preimage = self.check_settled(verify_url)
            if settled:
                return True, preimage
            time.sleep(poll_interval)
        return False, None

    @staticmethod
    def _get_json(url: str, timeout: float = 15) -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"LNURL HTTP {e.code} from {url}: {body[:200]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"LNURL connection error: {e.reason}")


# ── Self-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== LNURL-PAY PAYMENT RAIL — LIVE TEST ===")
    rail = PaymentRail()
    print(f"lud16: {rail.lud16}")

    info = rail.resolve()
    print(f"resolved: min={info.min_msat/1000:.0f} sat, max={info.max_msat/1000:.0f} sat")
    print(f"callback: {info.callback[:60]}...")
    print(f"nostr: {info.allows_nostr} pubkey={info.nostr_pubkey[:16]}...")

    inv = rail.invoice(1000, comment="DVM rail test")
    print(f"invoice: {inv.bolt11[:50]}...")
    print(f"verify: {inv.verify_url[:60]}...")

    settled, preimage = rail.check_settled(inv.verify_url)
    print(f"payment: settled={settled} preimage={preimage}")

    print("=== RAIL READY ===")
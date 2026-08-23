"""
[DNA_TAG]
ORIGIN: Moto4_A9
PILLAR: valet_concierge
PATH: keygen.py
LAST_SYNC: 2026-08-02T01:13:34Z
[/DNA_TAG]
"""
import secrets, hashlib
from pathlib import Path
priv = secrets.token_hex(32)
pub = hashlib.sha256(priv.encode()).hexdigest()
Path("~/passive_income_swarm/keys/").expanduser().mkdir(parents=True, exist_ok=True)
with open(Path("~/passive_income_swarm/keys/nostr.key").expanduser(), "w") as f: f.write(priv)
with open(Path("~/passive_income_swarm/keys/nostr.pub").expanduser(), "w") as f: f.write(pub)

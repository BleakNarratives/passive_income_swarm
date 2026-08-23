"""
[DNA_TAG]
ORIGIN: Moto4_A9
PILLAR: valet_concierge
PATH: listener.py
LAST_SYNC: 2026-08-02T01:13:34Z
[/DNA_TAG]
"""
import asyncio, json, time, websockets
from pathlib import Path

RELAYS = ["wss://relay.primal.net", "wss://relay.nostr.band"]
JOB_LOG = Path.home() / "passive_income_swarm/nostr_dvm/logs/jobs.log"
INBOX = Path.home() / "passive_income_swarm/control/inbox"
NWC_SECRET = Path.home() / "passive_income_swarm/keys/nwc.secret"

async def handle_message(message):
    try:
        data = json.loads(message)
        if not isinstance(data, list) or len(data) < 3 or data[0] != "EVENT":
            return
        event = data[2]
        if not isinstance(event, dict) or event.get("kind") != 5050:
            return
        event_id = event.get("id", "unknown")
        pubkey = event.get("pubkey", "unknown")
        content = event.get("content", "")
        JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(JOB_LOG, "a") as f:
            f.write(f"[{time.ctime()}] id={event_id} pubkey={pubkey} content={content}\n")
        print(f"[JOB] {event_id[:8]}... from {pubkey[:8]}...")
        token = INBOX / f"paid_{event_id}.token"
        if token.exists():
            task = INBOX / f"job_{event_id}.sh"
            task.write_text(f"echo 'job {event_id}' >> ~/passive_income_swarm/swarm.log\n")
            print(f"[PAID] Processing {event_id[:8]}")
        else:
            print(f"[GATE] Payment required for {event_id[:8]}")
    except Exception as e:
        print(f"[ERR] {e}")

async def listen(relay):
    while True:
        try:
            async with websockets.connect(relay) as ws:
                print(f"[CONNECTED] {relay}")
                await ws.send(json.dumps(["REQ", "dvm1", {"kinds": [5050]}]))
                async for msg in ws:
                    await handle_message(msg)
        except Exception as e:
            print(f"[RETRY] {relay}: {e}")
            await asyncio.sleep(10)

async def main():
    await asyncio.gather(*(listen(r) for r in RELAYS))

asyncio.run(main())

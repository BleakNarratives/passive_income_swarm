#!/usr/bin/env python3
"""test_job.py — publish a synthetic NIP-90 kind 5002 job to prove the
DVM worker serves end-to-end. Uses the worker's own signing plumbing.

Usage: python3 bin/test_job.py [relay] [input_text]
Run with the dvm venv: ~/passive_income_swarm-env/bin/python
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dvm_worker as dw  # noqa: E402
import websockets  # noqa: E402

RELAY = sys.argv[1] if len(sys.argv) > 1 else "wss://nos.lol"
INPUT = sys.argv[2] if len(sys.argv) > 2 else (
    "Summarize the Mikey Model thesis in one sentence: persona injection "
    "aligns LLM behavior to an operator's methodology, measurably.")


async def main():
    content = json.dumps({"input": INPUT})
    job = dw.build_event(5002, [["i", INPUT[:64]], ["output", "text/plain"]],
                         content)
    print(f"[test_job] id={job['id'][:16]}... pubkey={job['pubkey'][:16]}...")
    async with websockets.connect(RELAY, open_timeout=10) as ws:
        await ws.send(json.dumps(["EVENT", job]))
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                break
            if reply[0] == "OK" and reply[1] == job["id"]:
                print(f"[test_job] relay says: {reply}")
                break
    print(f"[test_job] published to {RELAY} — watch journalctl for [JOB]")


if __name__ == "__main__":
    asyncio.run(main())

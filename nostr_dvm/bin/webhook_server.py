"""
[DNA_TAG]
ORIGIN: Moto4_A9
PILLAR: valet_concierge
PATH: webhook_server.py
LAST_SYNC: 2026-08-02T01:13:34Z
[/DNA_TAG]
"""
from flask import Flask, request
from pathlib import Path
app = Flask(__name__)
INBOX = Path("/data/data/com.termux/files/home/passive_income_swarm/control/inbox/")
PAYMENT_LOG = Path("/data/data/com.termux/files/home/passive_income_swarm/nostr_dvm/logs/payments.log")
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    payment_hash = data.get('payment_hash')
    if payment_hash:
        (INBOX / f"paid_{payment_hash}.token").touch()
        with open(PAYMENT_LOG, "a") as f: f.write(f"Payment received: {payment_hash}\n")
        return "OK", 200
    return "Invalid", 400
if __name__ == '__main__': app.run(port=9090)

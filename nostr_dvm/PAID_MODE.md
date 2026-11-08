# Paid worker guard

This launches the existing NIP-90 DVM through a fail-closed payment guard.
The existing worker's default mode is reputation/free-serving. Its paid path
also trusts a requester's `amount` tag and catches invoice-rail errors before
continuing; those are not safe billing guarantees.

`paid_mode_guard.py` closes those paths in-process:

- Forces paid-only mode and one in-flight job.
- Ignores requester-supplied amount tags; each request must get an invoice.
- Records a one-use credit only after the LNURL verify endpoint reports the
  invoice settled.
- Blocks inference unless it can consume that verified settlement credit.
- Exits before serving if the payment rail or Groq key is unavailable.

## Run

From the existing repository checkout, with the same environment and key files
used by `dvm_worker.py`:

```sh
./nostr_dvm/run_paid_worker.sh
```

The worker still requires the existing Python dependencies, Nostr signing key,
`GROQ_API_KEY`, and a configured/reachable Lightning address. It uses the
existing `payment_rail.py` and existing job ledger. No wallet secret is added
by this guard. Keep wallet credentials and signing keys out of Git; rotate any
credential that has been committed publicly.

## Important limit

This makes billing fail closed; it does not create buyers, guarantee relay
traffic, or promise income. Check settled payments in the wallet and the job
ledger. Use the existing service manager to run this launcher if you want it
to replace the free-mode worker; do not run both workers with the same key at
the same time.

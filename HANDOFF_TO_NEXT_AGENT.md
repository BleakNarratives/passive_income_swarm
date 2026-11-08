# Handoff: audit existing income worker and repair it in place

## User request / interaction constraints

The user asked for useful implementation in their existing system, not a new scaffold, terminal homework, resources, or another checklist. They say their concierge/vault/valet setup already contains configuration and automation. They want an agent to inspect what is actually present, remove or correct prior assistant damage, and make the existing system execute on their device. They are angry for good reason: prior replies repeatedly ignored this, gave malformed shell blocks, claimed work that was not done, and implied an unverified code change was safe.

**Do not** respond with generic advice, placeholders, invented paths/secrets, a fresh repo, or a request that the user paste credentials. Do the repository/device inspection and code work yourself wherever the environment permits. Do not claim device access; this handoff is not evidence that local files, cron, services, secrets, or runtime state have been inspected.

## Existing project and relevant code

Canonical repo identified from the user's account: [`BleakNarratives/passive_income_swarm`](https://github.com/BleakNarratives/passive_income_swarm). Its existing NIP-90 worker and Lightning payment rail are under `nostr_dvm/`.

The original worker is `nostr_dvm/dvm_worker.py`. It listens on Nostr relays, calls Groq inference, signs and publishes results, and has `DVM_PAID_ONLY` logic. The payment rail is `nostr_dvm/payment_rail.py` (LNURL-pay). The README describes paid mode and a user service. The user's broader concierge/vault/valet runtime may be outside this repo and was not inspected.

## Changes already made by the prior assistant

Commit [`14469e139599d45f6092c95df6ee7dbb08d2fe41`](https://github.com/BleakNarratives/passive_income_swarm/commit/14469e139599d45f6092c95df6ee7dbb08d2fe41) on `main` added:

- `nostr_dvm/paid_mode_guard.py`
- `nostr_dvm/run_paid_worker.sh`
- `nostr_dvm/PAID_MODE.md`
- `tests/test_paid_mode_guard.py`

This change was **not tested** by the prior assistant. Do not call it production-ready. The guard monkey-patches worker functions and uses a global settlement-credit counter, not a payment token cryptographically or transactionally bound to a specific job. Review the existing payment/job lifecycle, relay reconnect/concurrency behavior, payment verification semantics, and runtime configuration before deciding whether to fix or revert it. Do not let a job reach paid inference based only on an incoming NIP-90 `amount` tag. Payment rail errors must fail closed in paid mode.

The separate `BleakNarratives/termux-telegram-worker` repo was created during this conversation and is unrelated scaffold/bulk. It is not the user's income worker. Do not build on it or recommend it as the solution.

## Security review required

The GitHub tree listing showed tracked paths `keys/nwc.secret`, `nostr_dvm/dvm.env`, and `keys/nostr.pub` in `passive_income_swarm`. Their contents were deliberately not retrieved or echoed. Determine locally/repository-side whether any tracked secret is real and exposed. Never print or copy secrets into this handoff, chat, logs, or new commits. If a valid wallet connection secret or signing credential was publicly committed, treat it as compromised and give the user a concise, specific revocation/rotation recommendation without asking them to paste it.

## Required agent work

1. Inspect the complete current repository and relevant history, including `HANDOFF.md`, `nostr_dvm/WHO_DID_WHAT.md`, service configuration if tracked, tests, and current status of the paid guard. Do not trust earlier summaries as verified facts.
2. If the task environment has the user's actual device/checkout available, inspect the existing install, cron, service definitions, and concierge/vault/valet configuration **read-only first**. Preserve existing files and secrets; no broad cleanup, reinstall, or duplicate runtime. If the device is not available, state that narrowly and do all possible in-repo work instead.
3. Repair the existing paid execution path in place. Bind each settled invoice to exactly its originating job, make retries/idempotency and relay disconnects safe, prevent replay/double service, and fail closed on missing/failed billing or inference. Follow the actual NIP-90/payment contract and existing project conventions. Do not use an untrusted request amount tag as proof of settlement.
4. Add deterministic tests for unpaid rejection, payment-rail failure, settled payment for the matching job, cross-job settlement/replay, duplicate event, relay disconnect/retry, and inference failure. Run the tests and report exact results; do not claim tests were run if they were not.
5. Integrate with the user's existing runtime/service/cron only if its real files and paths are visible and verified. Do not make them type or paste anything. Do not expose a wallet, inference, or signing endpoint publicly without authentication and abuse limits.
6. Leave a concise operator handoff naming the exact changed files and exact execution state. If execution requires a secret or runtime not available in the agent environment, do not invent it or claim live revenue; distinguish code completion from deployment and actual settled income.

## Honesty boundary

No code can guarantee customers, requests, or earnings. The concrete job here is to make the existing paid service reliably reject unpaid work and actually deliver paid jobs on the user's existing runtime—not to pitch another business plan or another starter project.

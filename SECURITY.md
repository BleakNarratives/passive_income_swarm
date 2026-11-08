# SECURITY.md

## Tracked secret: `keys/nwc.secret` — treat as compromised

**Finding.** `keys/nwc.secret` is tracked in a **public** repository
(`BleakNarratives/passive_income_swarm`). It was first committed in `3f4ad07`
("Session Snapshot: 2026-04-30") and has been publicly readable since. The file
is a `nostr+walletconnect://` URI containing a live `secret=` value, plus the
`lud16` address the payment rail resolves.

`.gitignore` lists `keys/nwc.secret`. That has no effect on a file that was
already committed — ignore rules govern *untracked* paths only. This is the
single most common way a "gitignored" secret leaks.

**What I did, in this workspace.**

* Removed it from the git index with `git rm --cached`, which leaves the working
  file in place so the running system keeps functioning.
* Added explicit ignores for runtime state and the settlement database.
* Did **not** print, copy or commit the value, and did not write it anywhere.

**What only you can do.**

1. **Rotate.** In coinos (or whichever wallet issued the NWC connection), delete
   the existing wallet-connect connection and create a new one. Any secret that
   has been public for months should be assumed spent. Moving funds to a fresh
   wallet is the conservative option.
2. **Re-seat the credential locally**, outside version control:

   ```sh
   install -m 600 /dev/stdin ~/passive_income_swarm/keys/nwc.secret <<'EOF'
   <paste the newly generated nostr+walletconnect:// URI here>
   EOF
   ```

   Keep the `lud16=` parameter — `payment_rail.py` reads it to resolve the LNURL
   endpoint.
3. **Purge the history.** Removing the file going forward does not remove it from
   old commits. Options, in increasing order of thoroughness:

   ```sh
   # rewrite every commit that touched it (requires force-push, coordinate first)
   git filter-repo --path keys/nwc.secret --invert-paths

   # or, for a single path across all refs
   git filter-branch --index-filter \
     'git rm --cached --ignore-unmatch keys/nwc.secret' --prune-empty -- --all
   ```

   Then force-push. Anyone who already cloned keeps their copy; assume the
   secret is public regardless of the rewrite. **Rotation is the fix; the purge
   is hygiene.**
4. Tell GitHub to drop cached views if you want the old blob gone from forks:
   Settings → the repository is public, so the practical answer is that the
   secret is already out. Rotate.

## What else was checked

| Path | Assessment |
|---|---|
| `keys/nostr.key` (signing private key) | **not tracked** — good, and it must stay that way |
| `keys/nostr.pub` | public key only, safe |
| `nostr_dvm/dvm.env` | no key material (no `GROQ_API_KEY`, no `nsec`, no 64-char hex). Config only |
| `nostr_dvm/__pycache__/*.pyc` | compiled artifacts, previously committed — untracked |
| `nostr_dvm/logs/jobs.jsonl` | runtime ledger with requester pubkeys; untracked, kept on disk |
| `control/inbox/paid_test123.token` | empty file |

## Rules this repository now follows

* Secrets live in `keys/` or the environment, mode `600`, never in git.
* The settlement ledger masks the payment preimage in every export and API
  response; the console never receives it.
* The console binds `127.0.0.1` by default and is **read-only** unless
  `DASHBOARD_TOKEN` is set. Actions run from an allowlist with no shell.
* Paid mode fails closed: no rail, no service.

## If you add a secret later

Add the ignore rule **and** confirm with `git check-ignore -v <path>` before the
first commit. Once it is pushed, the only real remedy is rotation.

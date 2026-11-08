# core_framework integration

The only coupling between `core_framework` and this repository, and the smallest
one that closes the loop.

`bin/domino.sh` (277 lines, at `core_framework/bin/domino.sh`) already has a
`preflight()` stage that checks the interpreter, the fixtures and the GossipBus
kernel registry. What it never does is tell the swarm those checks passed — so
once `IngestBreaker` locks ingestion into `STRICT_INGEST_ONLY`, nothing can
release it except a human running `swarmctl breaker preflight` by hand.

That is a real deadlock, not a theoretical one: the lock is designed to require a
passing preflight, and the preflight exists, but the two have never been
introduced.

## Install

```bash
cp domino_preflight.sh  <repo-root>/core_framework/bin/
chmod +x                <repo-root>/core_framework/bin/domino_preflight.sh
```

Then add **one line** to `preflight()` in `core_framework/bin/domino.sh`, right
after the kernel bus block and before the closing brace:

```diff
     say "kernel bus: gossip_bus ($kernel_state)"
   else
     warn "kernel bus registry unavailable"
   fi
+
+  # Janus Guard: release the swarm's STRICT_INGEST_ONLY lock only now that
+  # these checks passed. No-op when the swarm repo is not on this machine.
+  bash "$SCRIPT_DIR/domino_preflight.sh"
 }
```

`SCRIPT_DIR` is already defined at the top of `domino.sh`, so no other change is
needed.

## Why it is safe

| Condition | Behaviour |
|---|---|
| No swarm checkout on the machine | prints a notice, exits 0 |
| Breaker already unlocked | exits 0, idempotent |
| Swarm present but release fails | warns, exits 0 (unless `STRICT_SWARM_PREFLIGHT=1`) |
| `SWARM_ROOT` set | uses it instead of searching |

The default is non-fatal on purpose: a scout run should not die because a sibling
repository is mid-edit. Set `STRICT_SWARM_PREFLIGHT=1` in whatever runs your
production cycle when you want the opposite.

It never unlocks on its own authority. It calls `swarmctl breaker preflight`,
which is the same gate a human would call, so a broken contract still cannot be
talked out of the lock by an automation script.

## Verify

From this repository:

```bash
bash ./swarmctl hooks --core-root <repo-root>/core_framework
```

`domino_preflight` flips from `action_required` to `wired` once the call is in
place.

```bash
bash ./swarmctl breaker status     # confirm the lock state
bash ./swarmctl breaker preflight  # what the script calls
```

#!/usr/bin/env bash
# ============================================================================
#  domino_preflight.sh — core_framework -> passive_income_swarm callback
#
#  WHY THIS EXISTS
#  The Janus Guard hard-locks WhorlBus ingestion after three contract
#  violations in the rolling window (swarm/breaker.py). Nothing unlocks it
#  automatically: `IngestBreaker.preflight_pass()` is the manual release, and
#  the design intent was that `domino.sh preflight` calls it once its own
#  checks pass. core_framework/bin/domino.sh has the preflight stage but never
#  tells the swarm, so a locked bus stays locked until somebody reads the code.
#
#  This closes that loop. It is deliberately the *only* coupling: the framework
#  gains one optional line, and the swarm gains no knowledge of the framework.
#
#  INSTALL
#    cp domino_preflight.sh  <repo-root>/core_framework/bin/
#    chmod +x                <repo-root>/core_framework/bin/domino_preflight.sh
#  then add one line to preflight() in bin/domino.sh (see README.md here).
#
#  SAFETY
#    * Exits 0 when the swarm repo is not on this machine. core_framework must
#      never depend on a sibling checkout existing.
#    * Exits 0 when the breaker was already unlocked. Idempotent.
#    * Does not unlock on its own authority: it calls the swarm's own
#      `breaker preflight`, the same gate a human would call. If that refuses,
#      this refuses.
#    * Writes nothing except the swarm's own breaker state file.
#
#  ENV
#    SWARM_ROOT              path to passive_income_swarm (default: search)
#    STRICT_SWARM_PREFLIGHT  =1 makes a failed release abort domino.sh preflight
#                            (default: warn and continue, because a scout run
#                             should not die because a sibling repo is mid-edit)
# ============================================================================
set -uo pipefail

say()  { printf '\033[1;36m[swarm]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[swarm]\033[0m %s\n' "$*" >&2; }

# --- locate the swarm ------------------------------------------------------
find_swarm() {
  if [ -n "${SWARM_ROOT:-}" ] && [ -f "$SWARM_ROOT/swarmctl" ]; then
    printf '%s\n' "$SWARM_ROOT"
    return 0
  fi
  local here candidate
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for candidate in \
    "$here/../../passive_income_swarm" \
    "$here/../../../passive_income_swarm" \
    "$HOME/passive_income_swarm" \
    "$HOME/src/passive_income_swarm"; do
    if [ -f "$candidate/swarmctl" ]; then
      printf '%s\n' "$(cd "$candidate" && pwd)"
      return 0
    fi
  done
  return 1
}

SWARM="$(find_swarm)" || {
  say "swarm checkout not found — skipping breaker release (not an error)"
  exit 0
}

# --- release the lock ------------------------------------------------------
output="$("$SWARM/swarmctl" breaker preflight 2>&1)"
code=$?

if [ "$code" -eq 0 ]; then
  say "breaker preflight released: $(printf '%s' "$output" | tail -n 1)"
  exit 0
fi

warn "swarm breaker preflight failed (exit $code):"
printf '%s\n' "$output" | sed 's/^/  /' >&2
if [ "${STRICT_SWARM_PREFLIGHT:-0}" = "1" ]; then
  exit "$code"
fi
warn "continuing anyway; set STRICT_SWARM_PREFLIGHT=1 to make this fatal"
exit 0

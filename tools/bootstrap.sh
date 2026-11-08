#!/usr/bin/env bash
# tools/bootstrap.sh — pull, make executable, and install the cron automations
# =============================================================================
#
# One shot, idempotent, no sudo, no secrets, no writes outside the repo and the
# user crontab.
#
#   ./tools/bootstrap.sh              # pull main, chmod, install cron
#   ./tools/bootstrap.sh --dry-run    # print the cron block, change nothing
#   ./tools/bootstrap.sh --no-pull    # skip git, just chmod + cron
#   ./tools/bootstrap.sh --no-cron    # skip cron
#   ./tools/bootstrap.sh --uninstall  # remove the cron block, keep everything else
#
# The cron block is fenced by markers, so re-running replaces it instead of
# stacking duplicates. If `crontab` is missing (containers, some WSL images),
# the block is written to ~/.config/passive_income_swarm/crontab.txt and the
# exact command to install it is printed.
#
# What it schedules:
#   bridge    rest 30 min   publishes changed console facts to the bus
#   ingest    rest 30 min   drains the bus into the ledger and skill store
#   probe     rest 4 h      live connection probes (network calls, so not often)
#   readiness rest 2 h      a readiness snapshot for anything that reads files
#   tune      hourly        reads the climate and speeds the four above up
#
# Those are the scheduler's resting rungs, not arbitrary defaults: a cold box
# stays there, and the first hourly tune finds nothing worth changing. Traffic,
# an owed backlog, integrity failures, degraded connections or scout intel move
# a job one rung at a time, never for free.
#
# The block itself is rendered by swarm.scheduler, the same module `swarmctl
# cron status` reads, so every line carries a `# job:<id>` tag and health checks
# line up with what is actually installed. Left alone, the cadence does not
# drift: a change has to beat a 1.6x deadband and a 6 h dwell first.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="main"
DRY_RUN=0
DO_PULL=1
DO_CRON=1
UNINSTALL=0
MARK_BEGIN="# >>> passive_income_swarm >>>"
MARK_END="# <<< passive_income_swarm <<<"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-pull) DO_PULL=0 ;;
    --no-cron) DO_CRON=0 ;;
    --uninstall) UNINSTALL=1 ;;
    --repo=*) REPO="${arg#--repo=}" ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -d "$REPO" ] || die "no repository at $REPO"
[ -f "$REPO/swarmctl" ] || die "$REPO does not look like the swarm checkout (no swarmctl)"

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 is required"
command -v flock >/dev/null && LOCK="flock -n" || LOCK=""

say "repository : $REPO"
say "python     : $PYTHON"
say "lock       : ${LOCK:-unavailable — cron runs may overlap}"

# ---------------------------------------------------------------------------
# 1. pull
# ---------------------------------------------------------------------------
if [ "$DO_PULL" = "1" ]; then
  if ! command -v git >/dev/null; then
    say "git        : absent — skipping the pull"
  elif [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null || true)" ]; then
    say "git        : local changes present — skipping the pull (commit or stash first)"
  else
    current="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    say "git        : on '$current', fetching origin"
    if [ "$DRY_RUN" = "0" ]; then
      git -C "$REPO" fetch --prune origin
      if [ "$current" != "$BRANCH" ]; then
        git -C "$REPO" checkout "$BRANCH"
      fi
      before="$(git -C "$REPO" rev-parse --short HEAD)"
      git -C "$REPO" merge --ff-only "origin/$BRANCH" || die "not a fast-forward — resolve by hand, nothing was changed"
      after="$(git -C "$REPO" rev-parse --short HEAD)"
      if [ "$before" = "$after" ]; then
        say "git        : already up to date ($after)"
      else
        say "git        : $before -> $after"
      fi
    fi
  fi
else
  say "git        : skipped (--no-pull)"
fi

# ---------------------------------------------------------------------------
# 2. executable bits
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "0" ]; then
  count=0
  while IFS= read -r -d '' script; do
    chmod +x "$script" && count=$((count + 1))
  done < <(find "$REPO" -name "*.sh" -not -path "*/.git/*" -print0)
  for f in swarmctl run_tests.sh; do
    [ -f "$REPO/$f" ] && chmod +x "$REPO/$f" && count=$((count + 1))
  done
  for f in nostr_dvm/dvm_worker.py nostr_dvm/paid_mode_guard.py; do
    [ -f "$REPO/$f" ] && chmod +x "$REPO/$f" && count=$((count + 1))
  done
  say "chmod      : $count file(s) marked executable"
else
  say "chmod      : skipped (--dry-run)"
fi

# ---------------------------------------------------------------------------
# 3. cron
# ---------------------------------------------------------------------------
build_block() {
  # Rendered by the scheduler module so the tags match what `cron status` parses.
  ( cd "$REPO" && "$PYTHON" -c '
import sys
from swarm.scheduler import render_block
repo, python, lock = sys.argv[1:4]
intervals = {"bridge": 30, "ingest": 30, "probe": 240, "readiness": 120, "tune": 60}
sys.stdout.write(render_block(repo, python, intervals, flock=lock or None))
' "$REPO" "$PYTHON" "${LOCK:+flock}" )
}

strip_block() {
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    index($0, b) { skip = 1; next }
    index($0, e) { skip = 0; next }
    !skip
  '
}

if [ "$DO_CRON" = "0" ]; then
  say "cron       : skipped (--no-cron)"
  exit 0
fi

BLOCK="$(build_block)"
mkdir -p "$REPO/logs"

if [ "$UNINSTALL" = "1" ]; then
  if command -v crontab >/dev/null; then
    if [ "$DRY_RUN" = "1" ]; then
      say "cron       : would remove the block (--dry-run)"
    else
      existing="$(crontab -l 2>/dev/null || true)"
      strip_block <<<"$existing" | crontab -
      say "cron       : block removed"
    fi
  else
    say "cron       : crontab not installed; nothing to remove"
  fi
  exit 0
fi

if ! command -v crontab >/dev/null; then
  target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/passive_income_swarm"
  target="$target_dir/crontab.txt"
  say "cron       : crontab not found on this machine"
  if [ "$DRY_RUN" = "0" ]; then
    mkdir -p "$target_dir"
    { strip_block < "$target" 2>/dev/null || true; printf '%s\n' "$BLOCK"; } > "$target.new"
    mv "$target.new" "$target"
    say "             block written to $target"
  else
    say "             would write the block to $target (--dry-run)"
  fi
  say "             install it with: crontab $target"
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  say ""
  say "cron block (would be installed):"
  printf '%s\n' "$BLOCK"
  exit 0
fi

existing="$(crontab -l 2>/dev/null || true)"
merged="$( { strip_block <<<"$existing"; printf '%s\n' "$BLOCK"; } )"
printf '%s\n' "$merged" | crontab -
say "cron       : block installed (replaced any previous copy)"

if ! pgrep -x cron >/dev/null 2>&1 && ! pgrep -x crond >/dev/null 2>&1; then
  say "             note: no cron daemon is running — start it (systemctl enable --now cron)"
fi

cat <<EOF

next:
  crontab -l                         # confirm the block
  tail -f $REPO/logs/cron-bridge.log # watch the first run
  $REPO/swarmctl readiness --fast     # is this box safe to charge?
EOF

#!/usr/bin/env bash
# tools/install.sh — clone or update a checkout, then hand off to bootstrap.
#
# One line, no pasted block:
#   bash <(curl -fsSL https://raw.githubusercontent.com/BleakNarratives/passive_income_swarm/main/tools/install.sh)
#
#   ./tools/install.sh                 # install the checkout this script sits in
#   ./tools/install.sh --repo ~/swarm  # or install somewhere else
#
# Options:
#   --repo PATH      checkout location (default: this checkout, else ~/passive_income_swarm)
#   --branch NAME    branch to follow (default: main)
#   --no-cron        pass through to bootstrap: leave the crontab alone
#   --dry-run        pass through to bootstrap: print the block, change nothing
#
# Environment:
#   SWARM_URL        override the clone URL (mirror, offline checkout, tests)
#
# Safe to re-run. It only fast-forwards: a dirty or diverged checkout is left
# alone and bootstrap runs against what is on disk. No sudo, no secrets, and
# nothing is written outside the checkout and the user crontab.
set -o pipefail

URL="${SWARM_URL:-https://github.com/BleakNarratives/passive_income_swarm.git}"
BRANCH="main"
REPO=""
PASSTHRU=""
DRY=0

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) shift; REPO="${1:-}" ;;
    --repo=*) REPO="${1#--repo=}" ;;
    --branch) shift; BRANCH="${1:-}" ;;
    --branch=*) BRANCH="${1#--branch=}" ;;
    --no-cron) PASSTHRU="$PASSTHRU --no-cron" ;;
    --dry-run) PASSTHRU="$PASSTHRU --dry-run"; DRY=1 ;;
    -h|--help)
      sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) warn "unknown argument: $1"; exit 2 ;;
  esac
  shift
done

# Default: the checkout this script lives in, else a conventional path. Under
# process substitution $0 is /dev/fd/NN, which simply fails the checkout test.
if [ -z "$REPO" ]; then
  here="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
  if [ -n "$here" ] && [ -f "$here/../swarmctl" ]; then
    REPO="$(cd "$here/.." && pwd)"
  else
    REPO="${HOME:-/tmp}/passive_income_swarm"
  fi
fi

case "$REPO" in
  /*) : ;;
  "~") REPO="${HOME:-/tmp}" ;;
  "~/"*) REPO="${HOME:-/tmp}/${REPO#\~/}" ;;
  *) REPO="$(pwd)/$REPO" ;;
esac
REPO="${REPO%/}"

say "installer  : $URL"
say "branch     : $BRANCH"
say "checkout   : $REPO"

if ! command -v git >/dev/null 2>&1; then
  warn "git is required but was not found on PATH — install git, then re-run."
  exit 1
fi

if [ -e "$REPO" ] && [ ! -d "$REPO" ]; then
  warn "checkout path exists but is not a directory: $REPO"
  warn "move it aside, then re-run (nothing was changed)."
  exit 1
fi

if [ -e "$REPO/.git" ]; then
  # Ignore executable-bit noise. Bootstrap chmods scripts, and on some
  # filesystems that alone makes the checkout look dirty and strands it on an
  # old commit forever. Real content changes are still detected below.
  dirty="$(git -C "$REPO" -c core.fileMode=false status --porcelain 2>/dev/null || true)"
  if [ -n "$dirty" ]; then
    say "git        : local changes present — fast-forward only, never overwrite"
  fi
  current="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  say "git        : on '$current'"
  if ! git -C "$REPO" fetch --prune origin; then
    warn "fetch failed — could not check for updates; using what is on disk."
  else
    if [ "$current" != "$BRANCH" ]; then
      if ! git -C "$REPO" checkout "$BRANCH"; then
        warn "could not switch to $BRANCH — resolve by hand; nothing was changed."
        exit 1
      fi
    fi
    now="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    if [ "$now" = "$BRANCH" ]; then
      if ! git -C "$REPO" -c core.fileMode=false merge --ff-only "origin/$BRANCH"; then
        warn "could not fast-forward — local changes would be overwritten."
        warn "nothing was changed. Inspect: git -C $REPO status --short"
        warn "stash or commit those files, or re-run with --repo PATH to install elsewhere."
        exit 1
      fi
    else
      warn "not on $BRANCH — skipping the merge; resolve by hand."
      exit 1
    fi
  fi
elif [ -d "$REPO" ] && [ -n "$(ls -A "$REPO" 2>/dev/null)" ]; then
  warn "checkout path exists but is not a git checkout: $REPO"
  warn "move it aside (or pass --repo PATH), then re-run — nothing was changed."
  exit 1
else
  mkdir -p "$(dirname "$REPO")" || { warn "cannot create $(dirname "$REPO")"; exit 1; }
  say "git        : cloning"
  if ! git clone --branch "$BRANCH" "$URL" "$REPO"; then
    warn "clone failed — nothing to install."
    exit 1
  fi
fi

if [ ! -f "$REPO/tools/bootstrap.sh" ] || [ ! -f "$REPO/tools/portal.sh" ]; then
  warn "checkout is incomplete (bootstrap.sh or portal.sh missing) — the update did not land."
  warn "inspect: git -C $REPO log -1 --oneline && git -C $REPO status --short"
  warn "nothing was bootstrapped."
  exit 1
fi

say ""
# shellcheck disable=SC2086  # PASSTHRU holds flags only
bash "$REPO/tools/bootstrap.sh" --no-pull $PASSTHRU
status=$?
say ""
if [ "$status" -ne 0 ]; then
  warn "bootstrap exited $status — nothing else was changed."
elif [ "$DRY" = "1" ]; then
  say "dry run    : nothing installed"
else
  say "installed  : $REPO"
  say "health     : bash $REPO/swarmctl cron status"
  say "portal     : bash $REPO/tools/portal.sh --install   (make the checkout greet you)"
fi
exit "$status"

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

if [ -e "$REPO/.git" ]; then
  if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
    say "git        : local changes present — not touching the checkout"
  else
    current="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    say "git        : on '$current'"
    if git -C "$REPO" fetch --prune origin; then
      if [ "$current" != "$BRANCH" ]; then
        git -C "$REPO" checkout "$BRANCH" \
          || warn "  could not switch to $BRANCH — continuing on $current"
      fi
      now="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
      if [ "$now" = "$BRANCH" ]; then
        git -C "$REPO" merge --ff-only "origin/$BRANCH" \
          || warn "  not a fast-forward — left the checkout as it was"
      else
        warn "  not on $BRANCH — skipping the merge"
      fi
    else
      warn "  fetch failed — using what is already on disk"
    fi
  fi
else
  mkdir -p "$(dirname "$REPO")" || { warn "cannot create $(dirname "$REPO")"; exit 1; }
  say "git        : cloning"
  if ! git clone --branch "$BRANCH" "$URL" "$REPO"; then
    warn "clone failed — nothing to install."
    exit 1
  fi
fi

if [ ! -f "$REPO/tools/bootstrap.sh" ]; then
  warn "no tools/bootstrap.sh in $REPO — the checkout looks incomplete."
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
  say "health     : cd $REPO && ./swarmctl cron status"
  say "portal     : bash $REPO/tools/portal.sh --install   (make the checkout greet you)"
fi
exit "$status"

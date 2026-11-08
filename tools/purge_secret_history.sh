#!/usr/bin/env bash
# tools/purge_secret_history.sh — remove a leaked path from every commit.
#
# This is the part a rotation checklist usually skips. Deleting a file in a new
# commit leaves the blob reachable at the old commit, in every clone, and in
# GitHub's object store. Only a history rewrite removes it.
#
# Usage, from a fresh clone (NOT the working copy you are developing in):
#
#   git clone git@github.com:BleakNarratives/passive_income_swarm.git purge && cd purge
#   ./tools/purge_secret_history.sh --dry-run          # what would be rewritten
#   ./tools/purge_secret_history.sh                    # rewrite local clone
#   git push --force-with-lease origin main            # only after reading the output
#
# The rewrite is destructive and cannot be undone in the remote: every commit
# hash from the first affected commit onward changes, open pull requests go
# stale, and every collaborator must re-clone. It does not un-leak a credential
# that was public — rotate it regardless.
set -euo pipefail

PATHS=("keys/nwc.secret")
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --paths=*) IFS=, read -r -a PATHS <<< "${arg#--paths=}" ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

command -v git >/dev/null || { echo "git is required" >&2; exit 2; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "not inside a git repository" >&2; exit 2;
}

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty — commit, stash or clone fresh before rewriting" >&2
  exit 1
fi

echo "repository : $(git rev-parse --show-toplevel)"
echo "paths      : ${PATHS[*]}"
echo

echo "commits that currently contain these paths:"
for path in "${PATHS[@]}"; do
  # awk rather than sed: the path contains slashes, which are sed's delimiter.
  git log --all --oneline -- "$path" | awk -v p="$path" '{print "  [" p "] " $0}' || true
done
echo

echo "blobs reachable for these paths:"
for path in "${PATHS[@]}"; do
  git rev-list --all --objects -- "$path" | awk '{print "  " $1}' | sort -u || true
done
echo

if [ "$DRY_RUN" = "1" ]; then
  echo "dry run: nothing rewritten. Re-run without --dry-run on a fresh clone."
  exit 0
fi

filter_args=()
for path in "${PATHS[@]}"; do filter_args+=(--path "$path"); done

if command -v git-filter-repo >/dev/null 2>&1; then
  echo "running: git filter-repo --invert-paths ${filter_args[*]} --force"
  git filter-repo --invert-paths "${filter_args[@]}" --force
elif command -v python3 >/dev/null 2>&1 && python3 -c "import git_filter_repo" >/dev/null 2>&1; then
  echo "running: python -m git_filter_repo --invert-paths ${filter_args[*]} --force"
  python3 -m git_filter_repo --invert-paths "${filter_args[@]}" --force
else
  echo "git-filter-repo is not installed. Install it, then re-run:" >&2
  echo "  pip install git-filter-repo    (or: brew install git-filter-repo)" >&2
  echo
  echo "Filter-branch fallback is deliberately not used: it is slow, easy to" >&2
  echo "get subtly wrong, and leaves refs/original behind that still hold the blob." >&2
  exit 3
fi

echo
echo "verifying the rewrite:"
still=0
for path in "${PATHS[@]}"; do
  hits=$(git log --all --oneline -- "$path" | wc -l | tr -d ' ')
  echo "  commits touching $path: $hits"
  [ "$hits" = "0" ] || still=1
done
objects=$(git rev-list --all --objects | grep -c "keys/nwc.secret" || true)
echo "  objects naming the path: ${objects:-0}"

if [ "$still" = "1" ]; then
  echo >&2
  echo "still present — a tag, branch or stash ref is holding it. Run:" >&2
  echo "  git for-each-ref --format='%(refname)' | xargs -n1 git update-ref -d" >&2
  exit 1
fi

cat <<'NEXT'

Local rewrite complete. The blob is gone from this clone's history.

To publish it (destructive — this is the step to think about):
  git push --force-with-lease origin main
  git push --force-with-lease origin --tags

Then: every collaborator re-clones, and the credential is rotated anyway,
because a public repository's object store may retain unreachable objects and
because it was already served.
NEXT

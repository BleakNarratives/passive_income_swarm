#!/usr/bin/env bash
# tools/portal.sh — the door. Source it and `cd` into this checkout knows what
# to show: branch, swarm health, cron health, readiness, roadmap, next actions.
#
#   . tools/portal.sh                  # this shell only
#   bash tools/portal.sh --install     # add it to your shell rc (idempotent)
#   bash tools/portal.sh --uninstall   # remove it again
#
# Then, from anywhere:
#   portal             # banner + health + cron + next + roadmap
#   portal -a          # also list the files
#   portal --health -v # the swarm stats behind the dot
#   portal -q          # just the banner and its health dot
#
# Flags live in one string, set once and forgotten:
#   SWARM_PORTAL_FLAGS="srnh"   s=cron r=roadmap n=next h=swarm health l=files
#   SWARM_PORTAL=0              disable the cd hook for this shell
#   SWARM_PORTAL_ROOT=...       point at another checkout
#
# No daemon and no network: it reads git, the crontab, logs/console-telemetry.json,
# logs/readiness.json and README.md, and nothing else. Sourcing this file changes
# no shell options and wraps nothing — the hook rides PROMPT_COMMAND (bash) or
# chpwd_functions (zsh), so it fires once per entry, never per prompt.

SWARM_PORTAL_MARK_BEGIN="# >>> passive_income_swarm portal >>>"
SWARM_PORTAL_MARK_END="# <<< passive_income_swarm portal <<<"

# Where this file lives. bash exposes BASH_SOURCE; zsh needs its own syntax,
# kept behind eval so this file still parses under bash.
_swarm_portal_self=""
if [ -n "${BASH_SOURCE:-}" ]; then
  _swarm_portal_self="${BASH_SOURCE[0]}"
else
  _swarm_portal_self="$(eval 'printf "%s" "${(%):-%x}"' 2>/dev/null || true)"
  [ -n "$_swarm_portal_self" ] || _swarm_portal_self="$0"
fi
if [ -n "$_swarm_portal_self" ] && [ -f "$_swarm_portal_self" ]; then
  SWARM_PORTAL_ROOT="${SWARM_PORTAL_ROOT:-$(cd "$(dirname "$_swarm_portal_self")/.." && pwd)}"
fi
SWARM_PORTAL_ROOT="${SWARM_PORTAL_ROOT:-$PWD}"

_portal_help() {
  cat <<'EOF'
portal — the door to this checkout

  portal              banner + health + cron + next actions + roadmap
  portal -a           also list the files
  portal --health     the swarm stats line
  portal --health -v  and the detail behind it
  portal -q           banner and health dot only
  portal -s|-r|-l|-n  pick one section (cron, roadmap, files, next)

  bash tools/portal.sh --install     add the cd hook to your shell rc
  bash tools/portal.sh --uninstall   remove it
  bash tools/portal.sh --rc PATH     use a specific rc file

  SWARM_PORTAL_FLAGS="srnh"          default sections (l adds files)
  SWARM_PORTAL=0                     disable the hook for this shell
EOF
}

_portal_paint() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    _p_bold="$(printf '\033[1m')"
    _p_dim="$(printf '\033[2m')"
    _p_green="$(printf '\033[32m')"
    _p_yellow="$(printf '\033[33m')"
    _p_red="$(printf '\033[31m')"
    _p_off="$(printf '\033[0m')"
  else
    _p_bold=""; _p_dim=""; _p_green=""; _p_yellow=""; _p_red=""; _p_off=""
  fi
}

# ---------------------------------------------------------------------------
# probe: one python call reads every machine-written fact the portal shows
# ---------------------------------------------------------------------------
_portal_probe() {
  local root="$1" out line
  _p_cron=""
  _p_health="unknown"
  _p_swarm_summary=""
  _p_swarm_detail=""

  if [ -x "$root/swarmctl" ]; then
    _p_cron="$("$root/swarmctl" cron status 2>/dev/null)"
  elif command -v python3 >/dev/null 2>&1; then
    _p_cron="$(cd "$root" && python3 -m swarm.cli cron status 2>/dev/null)"
  fi

  command -v python3 >/dev/null 2>&1 || return 0
  out="$(python3 - "$root" <<'PY' 2>/dev/null
import json, os, sys, time

root = sys.argv[1]

def load(*parts):
    try:
        with open(os.path.join(root, *parts)) as fh:
            return json.load(fh)
    except Exception:
        return {}

tele = load("logs", "console-telemetry.json")
conns_file = load("logs", "connections.json")


def connection_counts():
    block = tele.get("connections") or {}
    if block:
        return (block.get("connections") or 0, block.get("live") or 0,
                block.get("degraded") or 0, block.get("unconfigured") or 0)
    items = conns_file.get("connections") or []
    live = sum(1 for item in items if item.get("status") == "live")
    unconfigured = sum(1 for item in items if item.get("status") == "unconfigured")
    degraded = sum(1 for item in items
                   if item.get("status") not in ("live", "unconfigured"))
    return (len(items), live, degraded, unconfigured)


total, live, degraded, unconfigured = connection_counts()

if not tele:
    head = []
    if total:
        head.append("%d links" % total)
    if live:
        head.append("%d live" % live)
    if degraded:
        head.append("%d degraded" % degraded)
    print("HEALTH=unknown")
    if head:
        print("SUMMARY=" + " · ".join(head) + " · no telemetry yet")
    raise SystemExit(0)

money = tele.get("money") or {}
counters = money.get("counters") or {}
owed = counters.get("unserved_paid") or 0
served = counters.get("served") or 0
integrity = money.get("integrity_failures") or []
jobs = tele.get("jobs") or {}
errors = sum(v for k, v in jobs.items()
             if "error" in k.lower() or "fail" in k.lower())

bits = []
if total:
    bits.append("%d links" % total)
if live:
    bits.append("%d live" % live)
if degraded:
    bits.append("%d degraded" % degraded)
elif unconfigured:
    bits.append("%d need a key" % unconfigured)
if errors:
    bits.append("%d errors" % errors)
if owed:
    bits.append("%d owed" % owed)
elif served:
    bits.append("%d served" % served)
else:
    bits.append("money unconfigured")

health = "ok"
if integrity:
    health = "fail"
elif degraded or errors:
    health = "warn"

stale = False
seen = "time unknown"
try:
    stamp = time.mktime(time.strptime(tele["observed_at"], "%Y-%m-%dT%H:%M:%SZ"))
    age = (time.time() - stamp) / 3600.0
    seen = "%dm ago" % max(1, int(age * 60)) if age < 1 else "%dh ago" % int(age)
    if age > 48:
        stale = True
        health = "warn" if health == "ok" else health
except Exception:
    pass

print("HEALTH=" + health)
print("SUMMARY=" + " · ".join(bits[:5]))

top = sorted(jobs.items(), key=lambda kv: -kv[1])[:3]
if top:
    print("DETAIL=jobs   " + " · ".join("%s %s" % (v, k) for k, v in top))

problems = [item for item in (conns_file.get("connections") or [])
            if item.get("status") != "live"]
if problems:
    print("DETAIL=net    " + " · ".join(
        "%s %s" % (item.get("id", "?"), item.get("status", "?"))
        for item in problems[:3]))
else:
    print("DETAIL=net    all %d live" % total)

print("DETAIL=ledger " + ("%d failing" % len(integrity) if integrity else "ok"))
print("DETAIL=seen   " + seen + (" (stale)" if stale else ""))
PY
)"
  [ -n "$out" ] || return 0
  while IFS= read -r line; do
    case "$line" in
      HEALTH=*) _p_health="${line#HEALTH=}" ;;
      SUMMARY=*) _p_swarm_summary="${line#SUMMARY=}" ;;
      DETAIL=*) _p_swarm_detail="${_p_swarm_detail}${_p_swarm_detail:+
}${line#DETAIL=}" ;;
    esac
  done <<EOF
$out
EOF

  # A failing schedule is a swarm problem even when the telemetry looks calm.
  case "$_p_cron" in
    *"healthy: no"*) [ "$_p_health" = "fail" ] || _p_health="warn" ;;
  esac
}

_portal_dot() {
  case "${_p_health:-unknown}" in
    ok) printf '%s● ok%s' "$_p_green" "$_p_off" ;;
    warn) printf '%s◐ degraded%s' "$_p_yellow" "$_p_off" ;;
    fail) printf '%s✕ failing%s' "$_p_red" "$_p_off" ;;
    *) printf '%s○ unknown%s' "$_p_dim" "$_p_off" ;;
  esac
}

_portal_banner() {
  local root="$1" branch sha dirty track state
  branch="$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  [ -n "$branch" ] || return 1
  sha="$(git -C "$root" rev-parse --short HEAD 2>/dev/null)"
  dirty="$(git -C "$root" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
  track="$(git -C "$root" status -sb 2>/dev/null | sed -n '1s/^## [^ ]*//p' | sed 's/^ *//;s/ *$//')"

  if [ "${dirty:-0}" = "0" ]; then state="clean"; else state="$dirty changed"; fi
  printf '%s\n' "${_p_bold}passive_income_swarm${_p_off}  ${branch} @ ${sha}  ${_p_dim}${state}${track:+ · $track}${_p_off}  $(_portal_dot)"
}

_portal_status() {
  local out line bad
  out="$_p_cron"
  if [ -z "$out" ]; then
    printf '%s\n' "${_p_dim}cron${_p_off}    no status available — is python3 on PATH?"
  elif printf '%s' "$out" | grep -qi 'not installed'; then
    printf '%s\n' "${_p_dim}cron${_p_off}    not installed — bash tools/bootstrap.sh"
  else
    line="$(printf '%s\n' "$out" | sed -n '/^daemon:/p' | sed -n '1p')"
    [ -n "$line" ] || line="$(printf '%s\n' "$out" | sed -n '1p')"
    line="$(printf '%s' "$line" | sed 's/  */ · /g;s/ · $//')"
    bad="$(printf '%s\n' "$out" | grep -cE '(NEVER|STALE|FAIL|NONE) ')"
    [ "${bad:-0}" -gt 0 ] 2>/dev/null && line="$line · $bad need attention"
    printf '%s\n' "${_p_dim}cron${_p_off}    $line"
  fi

  if [ -f "$SWARM_PORTAL_ROOT/logs/readiness.json" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "$SWARM_PORTAL_ROOT/logs/readiness.json" <<'PY' | sed "s/^/${_p_dim}money${_p_off}   /"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
safe = "safe to charge" if d.get("payments_safe") else "not safe to charge"
print("%s · %s/%s stages · %s" % (
    safe, d.get("stages_ok", 0), d.get("stages_total", 0), d.get("checked_at", "?")))
PY
  fi
}

_portal_health_line() {
  if [ -z "$_p_swarm_summary" ]; then
    printf '%s\n' "${_p_dim}swarm${_p_off}   no telemetry yet — run ./swarmctl bridge"
    return 0
  fi
  printf '%s\n' "${_p_dim}swarm${_p_off}   $_p_swarm_summary"
  if [ "${_p_verbose:-0}" = "1" ] && [ -n "$_p_swarm_detail" ]; then
    printf '%s\n' "$_p_swarm_detail" | sed "s/^/${_p_dim}        /;s/\$/${_p_off}/"
  fi
}

_portal_roadmap() {
  local root="$1" width
  [ -f "$root/README.md" ] || return 0
  width="${COLUMNS:-100}"
  printf '%s\n' "${_p_dim}roadmap${_p_off} (README.md)"
  awk -v limit="$width" '
    /^## / {
      sub(/^## /, "")
      if (out != "" && length(out) + length($0) + 5 > limit) {
        printf "  %s …\n", out; done = 1; exit
      }
      out = out (out == "" ? "" : "  ·  ") $0
      next
    }
    END { if (!done && out != "") printf "  %s\n", out }
  ' "$root/README.md"
}

_portal_files() {
  local root="$1"
  printf '%s\n' "${_p_dim}files${_p_off}"
  ls -A "$root" 2>/dev/null | awk 'NR<=10{printf "%s%s", sep, $0; sep="  "} END{if (NR>10) printf "  …"; if (NR) print ""}' | sed 's/^/  /'
}

_portal_next() {
  local root="$1" behind
  if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -q '>>> passive_income_swarm >>>' || {
      printf '%s\n' "${_p_dim}next${_p_off}    bash tools/bootstrap.sh   ${_p_dim}(install the schedule)${_p_off}"
      return 0
    }
  fi
  behind="$(git -C "$root" rev-list --count 'HEAD..@{u}' 2>/dev/null | tr -d ' ')"
  if [ -n "${behind:-}" ] && [ "${behind:-0}" -gt 0 ] 2>/dev/null; then
    printf '%s\n' "${_p_dim}next${_p_off}    bash tools/install.sh     ${_p_dim}($behind behind origin)${_p_off}"
  else
    printf '%s\n' "${_p_dim}next${_p_off}    ./swarmctl cron status   ·   portal -a"
  fi
}

_portal_render() {
  local root="$SWARM_PORTAL_ROOT" flags="${1-srnh}"
  [ -d "$root" ] || return 0
  _portal_paint
  _portal_probe "$root"
  printf '%s\n' "${_p_dim}────────────────────────────────────────${_p_off}"
  if ! _portal_banner "$root"; then
    printf '%s\n' "${_p_bold}passive_income_swarm${_p_off}  ${_p_dim}not a checkout${_p_off}"
    return 0
  fi
  case "$flags" in *s*) _portal_status ;; esac
  case "$flags" in *h*) _portal_health_line ;; esac
  case "$flags" in *n*) _portal_next "$root" ;; esac
  case "$flags" in *l*) _portal_files "$root" ;; esac
  case "$flags" in *r*) _portal_roadmap "$root" ;; esac
  printf '%s\n' "${_p_dim}────────────────────────────────────────${_p_off}"
}

portal() {
  local flags="" arg
  _p_verbose=0
  if [ "$#" -eq 0 ]; then
    flags="${SWARM_PORTAL_FLAGS:-srnh}"
  else
    for arg in "$@"; do
      case "$arg" in
        -a|--all) flags="srnhl" ;;
        -s|--status) flags="${flags}s" ;;
        -r|--roadmap) flags="${flags}r" ;;
        -h|--health|--swarm) flags="${flags}h" ;;
        -l|--list|--ls) flags="${flags}l" ;;
        -n|--next) flags="${flags}n" ;;
        -v|--verbose) _p_verbose=1 ;;
        -q|--quiet) flags="" ;;
        --help) _portal_help; return 0 ;;
        *) printf 'portal: unknown flag %s\n' "$arg" >&2; _portal_help >&2; return 2 ;;
      esac
    done
  fi
  _portal_render "$flags"
}

# Fire once per entry into the root, not on every prompt.
_swarm_portal_last=""
_swarm_portal_hook() {
  [ "${SWARM_PORTAL:-1}" = "0" ] && return 0
  [ -n "${SWARM_PORTAL_ROOT:-}" ] || return 0
  if [ "$PWD" != "$SWARM_PORTAL_ROOT" ]; then
    _swarm_portal_last=""
    return 0
  fi
  [ "$PWD" = "$_swarm_portal_last" ] && return 0
  _swarm_portal_last="$PWD"
  _portal_render "${SWARM_PORTAL_FLAGS-srn}"
}

if [ -n "${BASH_VERSION:-}" ]; then
  case "${PROMPT_COMMAND:-}" in
    *_swarm_portal_hook*) : ;;
    "") PROMPT_COMMAND="_swarm_portal_hook" ;;
    *) PROMPT_COMMAND="_swarm_portal_hook;${PROMPT_COMMAND}" ;;
  esac
elif [ -n "${ZSH_VERSION:-}" ]; then
  case " ${chpwd_functions[*]:-} " in
    *" _swarm_portal_hook "*) : ;;
    *) chpwd_functions=(${chpwd_functions[@]:-} _swarm_portal_hook) ;;
  esac
fi

# ---------------------------------------------------------------------------
# installer for the rc file
# ---------------------------------------------------------------------------
_portal_rc_path() {
  case "${SHELL:-}" in
    */zsh) printf '%s/.zshrc' "${ZDOTDIR:-$HOME}" ;;
    *) printf '%s/.bashrc' "$HOME" ;;
  esac
}

_portal_strip() {
  awk -v b="$SWARM_PORTAL_MARK_BEGIN" -v e="$SWARM_PORTAL_MARK_END" '
    index($0, b) { skip = 1; next }
    index($0, e) { skip = 0; next }
    !skip
  ' "$1"
}

_portal_cli() {
  local action="${1:-}" rc="" arg tmp line
  shift 2>/dev/null || true
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --rc) shift; rc="${1:-}" ;;
      --rc=*) rc="${1#--rc=}" ;;
      *) printf 'unknown argument: %s\n' "$1" >&2; return 2 ;;
    esac
    shift
  done
  case "$action" in
    --install|--uninstall) ;;
    -h|--help) _portal_help; return 0 ;;
    "") _portal_help; return 0 ;;
    *) printf 'unknown action: %s\n' "$action" >&2; return 2 ;;
  esac
  [ -n "$rc" ] || rc="$(_portal_rc_path)"
  [ -n "$HOME" ] || { printf 'HOME is not set — pass --rc PATH\n' >&2; return 1; }

  tmp="$rc.portal-new"
  if [ -f "$rc" ]; then
    _portal_strip "$rc" > "$tmp" || { printf 'cannot read %s\n' "$rc" >&2; return 1; }
  else
    : > "$tmp"
  fi

  if [ "$action" = "--uninstall" ]; then
    mv "$tmp" "$rc" 2>/dev/null && printf 'portal: removed from %s\n' "$rc"
    return 0
  fi

  line="[ -f \"$SWARM_PORTAL_ROOT/tools/portal.sh\" ] && . \"$SWARM_PORTAL_ROOT/tools/portal.sh\""
  printf '%s\n%s\n%s\n' "$SWARM_PORTAL_MARK_BEGIN" "$line" "$SWARM_PORTAL_MARK_END" >> "$tmp"
  mv "$tmp" "$rc" || { printf 'cannot write %s\n' "$rc" >&2; return 1; }
  printf 'portal: added to %s\n' "$rc"
  printf '        open a new shell, or run: . "%s"\n' "$SWARM_PORTAL_ROOT/tools/portal.sh"
}

_swarm_portal_executed=0
if [ -n "${BASH_SOURCE:-}" ]; then
  [ "${BASH_SOURCE[0]}" = "$0" ] && _swarm_portal_executed=1
else
  case "$0" in *portal.sh) _swarm_portal_executed=1 ;; esac
fi
if [ "$_swarm_portal_executed" = "1" ]; then
  _portal_cli "$@"
fi

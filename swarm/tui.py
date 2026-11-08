"""
swarm.tui — the terminal console
================================

The web console is for looking at. This is for the box you are already ssh'd
into, where opening a browser is not an option and `swarmctl readiness | less`
scrolls the interesting part off the screen.

It is stdlib `curses` only — no dependency, no daemon, no port. It reads the same
snapshot the web console does (`dashboard.feed.snapshot`), so the two cannot
disagree about the money.

Layout is a pure function (:func:`render`) so it can be tested without a
terminal; `--once` prints that rendering and exits, which is what CI and
`watch` want.

    swarmctl tui
    swarmctl tui --once
    swarmctl tui --once --settlements-db nostr_dvm/logs/settlements.demo.db
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["render", "collect", "main"]

_MARK = {
    "ok": "OK  ",
    "verified": "OK  ",
    "partial": "PART",
    "blocked": "FAIL",
    "missing": "MISS",
}


def collect(
    root: str | Path = ".",
    *,
    settlement_db: Optional[str] = None,
    jobs_log: Optional[str] = None,
) -> Dict[str, Any]:
    """Gather one snapshot. Never raises: a broken source becomes an error line."""
    from .readiness import readiness

    state: Dict[str, Any] = {"root": str(Path(root).resolve())}
    try:
        from dashboard.feed import snapshot

        state["snapshot"] = snapshot(root, settlement_db=settlement_db, jobs_log=jobs_log)
    except Exception as exc:  # noqa: BLE001
        state["snapshot"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        state["readiness"] = readiness(root, probe_network=False)
    except Exception as exc:  # noqa: BLE001
        state["readiness"] = {"error": f"{type(exc).__name__}: {exc}"}
    state["collected_at"] = time.time()
    return state


def _fmt_sats(msat: Any) -> str:
    try:
        return f"{int(msat or 0) // 1000:,}"
    except (TypeError, ValueError):
        return "0"


def _truncate(text: str, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def render(state: Dict[str, Any], width: int = 100, *, height: int = 40) -> List[str]:
    """Render one frame as plain lines. Pure: no terminal, no clock beyond input."""
    snap = state.get("snapshot") or {}
    report = state.get("readiness") or {}
    lines: List[str] = []

    def add(text: str = "") -> None:
        lines.append(_truncate(text, width).ljust(min(width, len(text))))

    header = "SWARM CONTROL"
    right = Path(state.get("root", ".")).name or "."
    add(header + " " * max(1, width - len(header) - len(right)) + right)
    add("─" * width)

    if snap.get("error"):
        add(f"snapshot error: {snap['error']}")
    if snap.get("demo"):
        add("DEMO DATA — synthetic settlements, no real sats")

    money = snap.get("money") or {}
    counters = money.get("counters") or {}
    verdict = "PAYMENTS SAFE" if report.get("payments_safe") else "NOT SAFE TO CHARGE"
    add(
        f"{verdict:<20} settled {_fmt_sats(counters.get('earned_msat'))} sats   "
        f"owed {counters.get('unserved_paid', 0)}   served {counters.get('served', 0)}   "
        f"open {counters.get('invoiced', 0)}"
    )
    add()

    stages = report.get("stages") or []
    if stages:
        add(f"STAGES  {report.get('stages_ok', 0)}/{report.get('stages_total', len(stages))}")
        for stage in stages:
            mark = _MARK.get(stage.get("state"), "?   ")
            row = f"  [{mark}] {stage.get('label', stage.get('id')):<33}"
            detail = stage.get("evidence", "")
            add(row + " " + detail)
        add()

    blocked = [c for c in (report.get("checks") or []) if c.get("state") == "blocked"]
    human = [c for c in blocked if c.get("owner") == "human"]
    if human:
        add("BLOCKED ON YOU")
        for check in human[:6]:
            add(f"  · {check.get('id')}: {check.get('action', check.get('detail', ''))}")
        add()

    conns = snap.get("connections") or {}
    if conns.get("available") is not False:
        summary = conns.get("summary") or {}
        add(
            f"CONNECTIONS  {summary.get('connections', 0)} linked · "
            f"{summary.get('capabilities', 0)} capabilities · "
            f"{summary.get('unbound', 0)} unbound · {summary.get('degraded', 0)} degraded"
        )
        add()

    jobs = ((snap.get("jobs") or {}).get("recent") or [])[: max(3, min(8, height - len(lines) - 6))]
    if jobs:
        add("RECENT JOBS")
        for job in jobs:
            add(
                f"  {str(job.get('outcome', '?')):<22} "
                f"{str(job.get('kind', '?')):<6} "
                f"{_fmt_sats(job.get('amount_msat')):>6} sat  "
                f"{str(job.get('job_id', ''))[:12]}"
            )
        add()

    footer = "[r]efresh  [s]elf-test  [p]robe  [q]uit"
    add("─" * width)
    add(footer)
    return lines


def _run_curses(state_factory, interval: float) -> int:
    import curses

    def loop(screen) -> int:
        curses.curs_set(0)
        screen.nodelay(True)
        state = state_factory()
        dirty = True
        last = 0.0
        message = ""
        while True:
            now = time.time()
            if dirty or now - last > interval:
                state = state_factory()
                last = now
                screen.erase()
                height, width = screen.getmaxyx()
                for i, line in enumerate(render(state, width, height=height)):
                    if i >= height - 1:
                        break
                    try:
                        screen.addnstr(i, 0, line, width - 1)
                    except curses.error:
                        pass
                if message:
                    try:
                        screen.addnstr(height - 1, 0, message[: width - 1], width - 1)
                    except curses.error:
                        pass
                screen.refresh()
                dirty = False

            ch = screen.getch()
            if ch == -1:
                time.sleep(0.1)
                continue
            key = chr(ch) if 0 <= ch < 256 else ""
            if key in ("q", "Q"):
                return 0
            if key in ("r", "R"):
                dirty = True
                message = "refreshed"
            elif key in ("s", "S"):
                from .lifecycle import self_test

                result = self_test()
                message = "self-test: " + ("passed" if result["ok"] else "FAILED")
                message += f" ({result.get('duration_ms', 0)}ms)"
                dirty = True
            elif key in ("p", "P"):
                from .connections import build_connections
                from .registry import Registry

                base = Path(state.get("root", "."))
                local = base / "swarm" / "cabinet.json"
                registry = Registry.load(local) if local.exists() else Registry.default()
                report = build_connections(base).report(registry, probe=True)
                path = build_connections(base).save(report, base / "logs" / "connections.json")
                message = f"probed → {path}"
                dirty = True

    return curses.wrapper(loop)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="swarmctl tui", description="terminal console")
    parser.add_argument("--root", default=".")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    parser.add_argument("--interval", type=float, default=3.0, help="auto-refresh seconds")
    parser.add_argument("--settlements-db", default=None)
    parser.add_argument("--jobs-log", default=None)
    parser.add_argument("--width", type=int, default=0, help="force a width (for --once)")
    args = parser.parse_args(argv)

    def factory() -> Dict[str, Any]:
        return collect(
            args.root, settlement_db=args.settlements_db, jobs_log=args.jobs_log
        )

    if args.once or not sys.stdout.isatty():
        width = args.width or 100
        print("\n".join(render(factory(), width)))
        return 0
    try:
        return _run_curses(factory, args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - no terminal support is not a crash
        print(f"tui unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("try: swarmctl tui --once")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

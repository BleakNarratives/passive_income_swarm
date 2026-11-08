"""The TUI must be testable without a terminal, and must never crash on bad data."""

import tempfile
import unittest
from pathlib import Path

from swarm.tui import collect, render


def state(**overrides):
    base = {
        "root": "/tmp/example",
        "collected_at": 0.0,
        "snapshot": {
            "demo": False,
            "money": {
                "available": True,
                "counters": {"earned_msat": 930_000, "served": 22, "unserved_paid": 2, "invoiced": 1},
            },
            "jobs": {"recent": [{"outcome": "served_paid", "kind": "5002", "amount_msat": 20_000, "job_id": "ab" * 8}]},
            "connections": {"available": True, "summary": {"connections": 11, "capabilities": 11, "unbound": 0, "degraded": 1}},
        },
        "readiness": {
            "payments_safe": False,
            "stages_ok": 2,
            "stages_total": 6,
            "stages": [
                {"id": "configured", "label": "configuration detected", "state": "ok", "owner": "human", "evidence": "dvm.env"},
                {"id": "wallet_rotated", "label": "wallet credential rotated", "state": "blocked", "owner": "human", "evidence": "leaked"},
            ],
            "checks": [
                {"id": "nwc_credential", "state": "blocked", "owner": "human", "detail": "leaked", "action": "rotate it"}
            ],
        },
    }
    base.update(overrides)
    return base


class RenderTests(unittest.TestCase):
    def test_frame_has_header_verdict_and_stages(self):
        lines = render(state(), width=100)
        text = "\n".join(lines)
        self.assertIn("SWARM CONTROL", text)
        self.assertIn("NOT SAFE TO CHARGE", text)
        self.assertIn("STAGES  2/6", text)
        self.assertIn("configuration detected", text)

    def test_money_is_rendered_in_sats_not_msat(self):
        text = "\n".join(render(state(), width=100))
        self.assertIn("930 sats", text)
        self.assertIn("owed 2", text)

    def test_safe_verdict_flips(self):
        st = state()
        st["readiness"]["payments_safe"] = True
        self.assertIn("PAYMENTS SAFE", "\n".join(render(st, width=100)))

    def test_demo_flag_is_impossible_to_miss(self):
        st = state()
        st["snapshot"]["demo"] = True
        self.assertIn("DEMO DATA", "\n".join(render(st, width=100)))

    def test_blocked_human_actions_are_listed(self):
        self.assertIn("BLOCKED ON YOU", "\n".join(render(state(), width=100)))

    def test_snapshot_error_renders_instead_of_raising(self):
        st = state(snapshot={"error": "boom"})
        self.assertIn("snapshot error: boom", "\n".join(render(st, width=100)))

    def test_missing_sections_do_not_raise(self):
        self.assertTrue(render({"root": "."}, width=80))

    def test_narrow_width_truncates(self):
        for line in render(state(), width=40):
            self.assertLessEqual(len(line.rstrip()), 40)


class CollectTests(unittest.TestCase):
    def test_collects_from_an_empty_checkout_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = collect(tmp)
        self.assertIn("snapshot", result)
        self.assertIn("readiness", result)
        self.assertIn("collected_at", result)

    def test_render_of_a_real_empty_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = render(collect(tmp), width=90)
        self.assertTrue(any("SWARM CONTROL" in line for line in lines))


if __name__ == "__main__":
    unittest.main()

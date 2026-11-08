"""Cron health must be readable, and the cadence must settle rather than thrash."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from swarm import scheduler
from swarm.scheduler import (
    DEADBAND,
    JOBS,
    MARK_BEGIN,
    MARK_END,
    climate,
    format_report,
    health_report,
    parse_block,
    render_block,
    status,
    tune,
)

NOW = 1_800_000_000.0
BRIDGE = next(job for job in JOBS if job.id == "bridge")


def block_with(intervals):
    return render_block("/repo", "/usr/bin/python3", intervals)


def snapshot(jobs=0, owed=0, failures=0, degraded=0):
    recent = [{"ts": NOW - 60 * i, "outcome": "served_paid"} for i in range(jobs)]
    return {
        "money": {
            "available": owed > 0,
            "counters": {"earned_msat": 0, "unserved_paid": owed, "served": 0},
            "integrity": [{"id": f"c{i}", "ok": False} for i in range(failures)],
        },
        "jobs": {"recent": recent},
        "connections": {"summary": {"degraded": degraded}},
    }


class BlockTests(unittest.TestCase):
    def test_round_trip_intervals(self):
        parsed = parse_block(block_with({"bridge": 5, "ingest": 10, "probe": 60, "readiness": 120, "tune": 60}))
        self.assertEqual(parsed["bridge"]["interval"], 5)
        self.assertEqual(parsed["ingest"]["interval"], 10)
        self.assertEqual(parsed["probe"]["interval"], 60)
        self.assertEqual(parsed["readiness"]["interval"], 120)

    def test_every_job_is_tagged(self):
        text = block_with({})
        for job in JOBS:
            self.assertIn(f"# job:{job.id}", text)

    def test_hourly_uses_an_hour_field_not_star_slash_60(self):
        text = render_block("/repo", "python3", {"probe": 120})
        line = [ln for ln in text.splitlines() if "# job:probe" in ln][0]
        self.assertTrue(line.startswith("0 */2 * * *"), line)

    def test_render_includes_markers_and_environment(self):
        text = block_with({})
        self.assertIn(MARK_BEGIN, text)
        self.assertIn(MARK_END, text)
        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin", text)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "logs").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _artifact(self, job, age_s, last="ok"):
        path = self.root / job.artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(last + "\n")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def test_not_installed_when_the_block_is_absent(self):
        report = status(self.root, crontab_text="", daemon="running")
        self.assertFalse(report["installed"])
        self.assertTrue(all(row["state"] == "not_installed" for row in report["jobs"]))
        self.assertFalse(report["healthy"])

    def test_never_run_when_installed_but_no_artifact(self):
        report = status(self.root, crontab_text=block_with({}), daemon="running")
        by_id = {row["id"]: row for row in report["jobs"]}
        self.assertEqual(by_id["bridge"]["state"], "never_run")

    def test_stale_when_the_artifact_is_old(self):
        self._artifact(BRIDGE, age_s=3 * 3600)
        report = status(self.root, crontab_text=block_with({"bridge": 5}), daemon="running")
        by_id = {row["id"]: row for row in report["jobs"]}
        self.assertEqual(by_id["bridge"]["state"], "stale")

    def test_failing_when_the_last_line_is_an_error(self):
        self._artifact(BRIDGE, age_s=60, last="Traceback (most recent call last):")
        report = status(self.root, crontab_text=block_with({"bridge": 5}), daemon="running")
        by_id = {row["id"]: row for row in report["jobs"]}
        self.assertEqual(by_id["bridge"]["state"], "failing")
        self.assertFalse(report["healthy"])

    def test_ok_when_recent_and_clean(self):
        for job in JOBS:
            self._artifact(job, age_s=30, last="7 signals · 0 published")
        report = status(self.root, crontab_text=block_with({}), daemon="running")
        self.assertTrue(report["healthy"], report["problems"])

    def test_missing_crontab_is_a_report_not_an_exception(self):
        original = scheduler.read_crontab

        def boom():
            raise scheduler.SchedulerError("crontab is not installed on this machine")

        scheduler.read_crontab = boom
        try:
            report = status(self.root)
        finally:
            scheduler.read_crontab = original
        self.assertFalse(report["available"])
        self.assertIn("not installed", " ".join(report["problems"]))
        self.assertIn("bootstrap", health_report(report))

    def test_report_mentions_a_dead_daemon(self):
        text = health_report(status(self.root, crontab_text=block_with({}), daemon="not_running"))
        self.assertIn("no cron daemon", text)


class ClimateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "logs").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_quiet_checkout_is_cold(self):
        result = climate(self.root, snapshot=snapshot(), now=NOW)
        self.assertLess(result["temperature"], 0.2)
        self.assertFalse(result["critical"])

    def test_backlog_is_critical(self):
        result = climate(self.root, snapshot=snapshot(owed=2), now=NOW)
        self.assertTrue(result["critical"])
        self.assertGreater(result["backlog"], 0.5)
        self.assertGreater(result["temperature"], 0.0)

    def test_integrity_failure_is_critical_and_raises_trouble(self):
        result = climate(self.root, snapshot=snapshot(failures=1), now=NOW)
        self.assertTrue(result["critical"])
        self.assertGreater(result["trouble"], 0)

    def test_traffic_raises_demand(self):
        quiet = climate(self.root, snapshot=snapshot(), now=NOW)
        busy = climate(self.root, snapshot=snapshot(jobs=12), now=NOW)
        self.assertGreater(busy["demand"], quiet["demand"])

    def test_scout_intel_on_the_bus_is_counted(self):
        from swarm.contracts import Envelope

        env = Envelope.build("scout", "intel", {"text": "a finding"}, confidence=0.9)
        (self.root / "bus.jsonl").write_text(json.dumps(env.to_sidecar()) + "\n")
        # The envelope carries a real creation time, so read the bus at real time.
        result = climate(self.root, snapshot=snapshot(), now=time.time())
        self.assertGreaterEqual(result["intel"], 1)
        self.assertGreater(result["intel_pressure"], 0)


class TuneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "logs").mkdir()
        self.state_path = self.root / "logs" / "schedule-state.json"
        self.written = []

    def tearDown(self):
        self._tmp.cleanup()

    def _tune(self, **kwargs):
        kwargs.setdefault("state_path", self.state_path)
        # Feed back what a previous apply installed, the way a real crontab would.
        kwargs.setdefault("crontab_text", self.written[-1] if self.written else "")
        kwargs.setdefault("snapshot", snapshot())
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("write", lambda text: self.written.append(text))
        return tune(self.root, **kwargs)

    def _decision(self, result, job_id):
        return next(d for d in result["decisions"] if d["id"] == job_id)

    def test_dry_run_never_writes(self):
        result = self._tune()
        self.assertFalse(result["applied"])
        self.assertEqual(self.written, [])

    def test_cold_climate_rests_on_the_slowest_rung(self):
        result = self._tune()
        for row in result["decisions"]:
            self.assertEqual(row["desired"], JOBS_BY_ID[row["id"]].ladder[-1], row)
        self.assertFalse(result["changed"])

    def test_apply_installs_one_block_even_when_nothing_changes(self):
        result = self._tune(apply=True)
        self.assertTrue(result["applied"])
        self.assertFalse(result["changed"])
        self.assertEqual(len(self.written), 1)
        self.assertEqual(self.written[0].count(MARK_BEGIN), 1)
        self.assertEqual(self.written[0].count(MARK_END), 1)

    def test_second_apply_holds_instead_of_churning(self):
        self._tune(apply=True)
        again = self._tune(apply=True)
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.written), 1, "a converged schedule must not rewrite itself")

    def test_owed_work_makes_the_hot_jobs_fast(self):
        result = self._tune(snapshot=snapshot(owed=3))
        self.assertTrue(result["changed"])
        self.assertEqual(self._decision(result, "bridge")["applied"], 1)
        self.assertEqual(self._decision(result, "ingest")["applied"], 1)

    def test_deadband_holds_an_adjacent_rung(self):
        # 3 jobs in the last hour puts bridge pressure at 0.25, which lands one
        # rung faster (15m) than the installed 10m — a 1.5x move, below 1.6x.
        result = self._tune(
            crontab_text=block_with({"bridge": 10, "ingest": 30, "probe": 240,
                                     "readiness": 120, "tune": 60}),
            snapshot=snapshot(jobs=3),
        )
        bridge = self._decision(result, "bridge")
        self.assertEqual(bridge["desired"], 15)
        self.assertEqual(bridge["action"], "held")
        self.assertIn("deadband", bridge["reason"])
        self.assertGreater(DEADBAND, 1.5)

    def test_dwell_blocks_a_non_critical_move(self):
        self.state_path.write_text(
            json.dumps({job.id: {"interval": 30, "changed_at": NOW - 60} for job in JOBS})
        )
        result = self._tune(
            crontab_text=block_with({job.id: 30 for job in JOBS}),
            snapshot=snapshot(jobs=12),
        )
        self.assertFalse(result["changed"])
        self.assertIn("dwell", self._decision(result, "bridge")["reason"])

    def test_critical_bypasses_dwell(self):
        self.state_path.write_text(
            json.dumps({job.id: {"interval": 30, "changed_at": NOW - 60} for job in JOBS})
        )
        result = self._tune(
            crontab_text=block_with({job.id: 30 for job in JOBS}),
            snapshot=snapshot(owed=2),
            apply=True,
        )
        self.assertTrue(result["changed"])
        self.assertIn("dwell bypassed", format_report(result))

    def test_state_records_the_tune(self):
        self._tune(apply=True, snapshot=snapshot(owed=3))
        state = json.loads(self.state_path.read_text())
        self.assertIn("last_tune", state)
        self.assertIn("bridge", state)
        self.assertEqual(state["bridge"]["interval"], 1)
        self.assertIn("changed_at", state["bridge"])
        self.assertTrue(state["bridge"]["history"])

    def test_write_failure_is_reported_not_raised(self):
        def boom(text):
            raise scheduler.SchedulerError("crontab is not installed on this machine")

        result = self._tune(apply=True, write=boom, snapshot=snapshot(owed=2))
        self.assertIn("not installed", result["error"])
        self.assertFalse(result["applied"])

    def test_report_is_honest_when_holding(self):
        self._tune(apply=True)
        text = format_report(self._tune(apply=True))
        self.assertIn("holding", text)

    def test_report_names_the_reason_for_a_change(self):
        text = format_report(self._tune(snapshot=snapshot(owed=3)))
        self.assertIn("change", text)
        self.assertIn("backlog", text)


JOBS_BY_ID = {job.id: job for job in JOBS}


if __name__ == "__main__":
    unittest.main()

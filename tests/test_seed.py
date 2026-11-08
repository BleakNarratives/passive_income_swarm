"""The demo seeder must produce something the console can read, and must never
be able to touch the production ledger."""

import json
import tempfile
import unittest
from pathlib import Path

from dashboard.feed import snapshot
from dashboard.seed import DemoRefused, seed
from nostr_dvm.settlement import SettlementLedger


class TestSeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "settlements.demo.db"
        self.jobs = self.root / "jobs.demo.jsonl"
        self.summary = seed(self.db, self.jobs, days=14)

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_refuses_the_production_ledger(self):
        with self.assertRaises(DemoRefused):
            seed(self.root / "settlements.db", self.jobs)

    def test_it_writes_rows_and_outcomes(self):
        self.assertGreater(self.summary["rows"], 0)
        self.assertGreater(len(self.summary["outcomes"]), 2)
        # Free jobs are in the job log but never in the invoice table.
        self.assertEqual(
            self.summary["rows"],
            sum(v for k, v in self.summary["outcomes"].items() if k != "free"),
        )
        self.assertTrue(self.db.exists())
        self.assertTrue(self.jobs.exists())

    def test_the_real_ledger_can_read_it(self):
        ledger = SettlementLedger(self.db)
        try:
            counts = ledger.counters()
        finally:
            ledger.close()
        self.assertGreater(counts.get("served", 0), 0)
        self.assertGreater(counts.get("settled", 0), 0)

    def test_income_history_is_spread_across_days(self):
        ledger = SettlementLedger(self.db)
        try:
            days = ledger.income_by_day(days=14)
        finally:
            ledger.close()
        self.assertGreater(len(days), 1)
        self.assertTrue(any(int(row.get("msat", 0)) > 0 for row in days))

    def test_integritity_checks_do_not_flag_the_demo(self):
        ledger = SettlementLedger(self.db)
        try:
            checks = ledger.integrity()
        finally:
            ledger.close()
        self.assertIsInstance(checks, list)
        failing = [
            c for c in checks if isinstance(c, dict) and c.get("ok") is False
        ]
        self.assertEqual(failing, [], f"demo ledger trips integrity: {failing}")

    def test_the_console_snapshot_has_money_and_a_forecast(self):
        data = snapshot(".", settlement_db=self.db, jobs_log=self.jobs)
        self.assertTrue(data["money"]["available"])
        self.assertGreater(data["money"]["counters"].get("served", 0), 0)
        self.assertTrue(data["jobs"]["available"])
        self.assertGreater(len(data["jobs"]["recent"]), 0)
        self.assertGreater(len(data["jobs"]["histogram"]), 1)
        self.assertTrue(data["forecast"].get("available", False) or data["forecast"].get("points"))

    def test_jobs_log_matches_the_real_worker_shape(self):
        lines = [json.loads(l) for l in self.jobs.read_text().splitlines() if l.strip()]
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue({"ts", "job_id", "kind", "outcome"} <= set(line))


if __name__ == "__main__":
    unittest.main()

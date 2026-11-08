"""Tests for the fail-closed launcher preflight.

The guard no longer monkey-patches the worker; it verifies that the worker can
bill and refuses to start otherwise. A fake worker module stands in here because
importing the real one needs coincurve + websockets.
"""

import unittest

from nostr_dvm.paid_mode_guard import PreflightError, preflight


class FakeLedger:
    path = "/tmp/test-settlements.db"

    def counters(self):
        return {"requested": 0}


class FakeWorker:
    def __init__(self, rail=object(), groq="present", paid_only=True):
        self._PAYMENT_RAIL = rail
        self.GROQ_KEY = groq
        self.PAID_ONLY = paid_only

    def settlement_ledger(self):
        return FakeLedger()


class PreflightTests(unittest.TestCase):
    def test_healthy_worker_passes(self):
        report = preflight(FakeWorker())
        self.assertTrue(report["rail"])
        self.assertTrue(report["paid_only"])

    def test_missing_rail_refuses_to_start(self):
        with self.assertRaises(PreflightError) as ctx:
            preflight(FakeWorker(rail=None))
        self.assertIn("rail", str(ctx.exception))

    def test_missing_groq_refuses_to_start(self):
        with self.assertRaises(PreflightError) as ctx:
            preflight(FakeWorker(groq=None))
        self.assertIn("GROQ_API_KEY", str(ctx.exception))

    def test_free_mode_refuses_to_start(self):
        with self.assertRaises(PreflightError) as ctx:
            preflight(FakeWorker(paid_only=False))
        self.assertIn("PAID_ONLY", str(ctx.exception))

    def test_unusable_ledger_refuses_to_start(self):
        class Broken(FakeWorker):
            def settlement_ledger(self):
                raise OSError("read-only filesystem")

        with self.assertRaises(PreflightError) as ctx:
            preflight(Broken())
        self.assertIn("settlement ledger", str(ctx.exception))

    def test_all_problems_are_reported_together(self):
        with self.assertRaises(PreflightError) as ctx:
            preflight(FakeWorker(rail=None, groq=None, paid_only=False))
        message = str(ctx.exception)
        self.assertIn("rail", message)
        self.assertIn("GROQ_API_KEY", message)
        self.assertIn("PAID_ONLY", message)


if __name__ == "__main__":
    unittest.main()

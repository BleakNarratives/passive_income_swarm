"""The console is not the worker.

`os.environ` inside the dashboard says nothing about what systemd started, so
reading it made the header claim "reputation mode" on a box that was charging.
These tests pin the difference.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard.feed import snapshot

CLEAN_ENV = {
    "DVM_PAID_ONLY": "",
    "DVM_FEE_5002_MSAT": "",
    "DVM_FEE_5003_MSAT": "",
    "GROQ_API_KEY": "",
}


class WorkerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "nostr_dvm").mkdir()
        self._env = mock.patch.dict(os.environ, CLEAN_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _dvm_env(self, text):
        (self.root / "nostr_dvm" / "dvm.env").write_text(text, encoding="utf-8")

    def _worker(self, **kwargs):
        return snapshot(self.root, **kwargs)["worker"]


class PaidModeSourceTests(WorkerBase):
    def test_configured_in_the_file_is_reported_as_paid(self):
        self._dvm_env("DVM_PAID_ONLY=1\n")
        worker = self._worker()
        self.assertTrue(worker["paid_mode"])
        self.assertEqual(worker["paid_mode_source"], "nostr_dvm/dvm.env")
        # The console itself still cannot see it; that is the whole point.
        self.assertFalse(worker["paid_mode_env"])
        self.assertTrue(worker["paid_mode_file"])

    def test_the_live_environment_alone_counts_too(self):
        with mock.patch.dict(os.environ, {"DVM_PAID_ONLY": "1"}):
            worker = self._worker()
        self.assertTrue(worker["paid_mode"])
        self.assertEqual(worker["paid_mode_source"], "environment")

    def test_both_sources_are_named(self):
        self._dvm_env("DVM_PAID_ONLY=true\n")
        with mock.patch.dict(os.environ, {"DVM_PAID_ONLY": "1"}):
            worker = self._worker()
        self.assertEqual(worker["paid_mode_source"], "environment + nostr_dvm/dvm.env")

    def test_off_everywhere_is_reputation_mode(self):
        worker = self._worker()
        self.assertFalse(worker["paid_mode"])
        self.assertEqual(worker["paid_mode_source"], "unset")

    def test_an_explicit_zero_in_the_file_is_off(self):
        self._dvm_env("DVM_PAID_ONLY=0\n")
        self.assertFalse(self._worker()["paid_mode"])

    def test_quotes_and_comments_are_handled(self):
        self._dvm_env('# paid for tests\nDVM_PAID_ONLY="1"\n')
        self.assertTrue(self._worker()["paid_mode"])

    def test_a_missing_file_does_not_raise(self):
        self.assertFalse(self._worker()["paid_mode"])

    def test_the_file_wins_only_when_the_environment_is_silent(self):
        self._dvm_env("DVM_PAID_ONLY=1\n")
        with mock.patch.dict(os.environ, {"DVM_PAID_ONLY": "0"}):
            worker = self._worker()
        # An explicit shell export of 0 is live truth for the console's own
        # process; the file still says paid, so both are reported honestly.
        self.assertTrue(worker["paid_mode"])
        self.assertFalse(worker["paid_mode_env"])
        self.assertTrue(worker["paid_mode_file"])


class ConfigLeakTests(WorkerBase):
    def test_fees_come_from_the_file_when_not_exported(self):
        self._dvm_env("DVM_FEE_5002_MSAT=75000\nDVM_FEE_5003_MSAT=30000\n")
        fees = self._worker()["fees_msat"]
        self.assertEqual(fees[5002], 75000)
        self.assertEqual(fees[5003], 30000)

    def test_environment_overrides_the_file(self):
        self._dvm_env("DVM_FEE_5002_MSAT=75000\n")
        with mock.patch.dict(os.environ, {"DVM_FEE_5002_MSAT": "12345"}):
            self.assertEqual(self._worker()["fees_msat"][5002], 12345)

    def test_a_bad_fee_falls_back_instead_of_crashing_the_console(self):
        self._dvm_env("DVM_FEE_5002_MSAT=not-a-number\n")
        fees = self._worker()["fees_msat"]
        self.assertEqual(fees[5002], 50000)

    def test_groq_key_in_the_file_counts_as_present(self):
        self._dvm_env("GROQ_API_KEY=gsk_placeholder\n")
        self.assertTrue(self._worker()["groq_key_present"])

    def test_no_credential_value_reaches_the_snapshot(self):
        self._dvm_env("GROQ_API_KEY=SUPERSECRET\nDVM_PAID_ONLY=1\n")
        blob = repr(self._worker())
        self.assertNotIn("SUPERSECRET", blob)


if __name__ == "__main__":
    unittest.main()

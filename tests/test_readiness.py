"""Readiness must never call something ready that isn't, and never blame a human
for something a command can fix."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm.readiness import (
    BLOCKED,
    HUMAN,
    MACHINE,
    READY,
    format_report,
    readiness,
    to_json,
)

CLEAN_ENV = {
    "GROQ_API_KEY": "",
    "GROQ_BASE_URL": "",
    "DVM_INFERENCE_URL": "",
    "DVM_PAID_ONLY": "",
    "DVM_FEE_5002_MSAT": "",
    "DVM_FEE_5003_MSAT": "",
}


class ReadinessBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "keys").mkdir()
        (self.root / "logs").mkdir()
        self._env = mock.patch.dict(os.environ, CLEAN_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _write_nwc(self, payload=b"nostr+walletconnect://new-connection", mode=0o600):
        path = self.root / "keys" / "nwc.secret"
        path.write_bytes(payload)
        os.chmod(path, mode)
        return path

    def _run(self, **kwargs):
        kwargs.setdefault("probe_network", False)
        return readiness(self.root, **kwargs)

    def _check(self, report, check_id):
        for check in report["checks"]:
            if check["id"] == check_id:
                return check
        self.fail(f"no check named {check_id}")


class TestCredential(ReadinessBase):
    def test_missing_credential_is_a_human_blocker(self):
        report = self._run()
        check = self._check(report, "nwc_credential")
        self.assertEqual(check["state"], BLOCKED)
        self.assertEqual(check["owner"], HUMAN)

    def test_the_leaked_credential_is_detected_by_fingerprint(self):
        path = self._write_nwc()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        report = self._run(leaked_prefix=digest[:16])
        check = self._check(report, "nwc_credential")
        self.assertEqual(check["state"], BLOCKED)
        self.assertEqual(check["owner"], HUMAN)
        self.assertIn("public since", check["detail"])

    def test_a_rotated_credential_with_tight_permissions_is_ready(self):
        self._write_nwc(mode=0o600)
        report = self._run(leaked_prefix="ffffffffffffffff")
        self.assertEqual(self._check(report, "nwc_credential")["state"], READY)

    def test_loose_permissions_are_mechanical_not_a_human_task(self):
        self._write_nwc(mode=0o644)
        report = self._run(leaked_prefix="ffffffffffffffff")
        check = self._check(report, "nwc_credential")
        self.assertEqual(check["state"], BLOCKED)
        self.assertEqual(check["owner"], MACHINE)
        self.assertIn("chmod 600", check["action"])

    def test_the_credential_value_never_appears_in_the_report(self):
        path = self._write_nwc(payload=b"nostr+walletconnect://SUPERSECRETVALUE")
        report = self._run()
        blob = to_json(report)
        self.assertNotIn("SUPERSECRETVALUE", blob)
        self.assertNotIn("SUPERSECRETVALUE", format_report(report))
        self.assertTrue(path.exists())


class TestKeysAndRuntime(ReadinessBase):
    def test_absent_signing_key_is_a_human_decision(self):
        report = self._run()
        check = self._check(report, "signing_key")
        self.assertEqual(check["state"], BLOCKED)
        self.assertEqual(check["owner"], HUMAN)
        self.assertIn("cannot sign", check["detail"])

    def test_present_signing_key_clears_the_check(self):
        (self.root / "keys" / "nostr.key").write_text("k", encoding="utf-8")
        self.assertEqual(self._check(self._run(), "signing_key")["state"], READY)

    def test_missing_module_is_mechanical(self):
        report = self._run()
        for module in ("coincurve", "websockets"):
            check = self._check(report, module)
            self.assertEqual(check["owner"], MACHINE)
            if check["state"] == BLOCKED:
                self.assertIn("pip install", check["action"])

    def test_inference_needs_a_key_or_an_endpoint(self):
        self.assertEqual(self._check(self._run(), "inference")["state"], BLOCKED)
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "x"}):
            self.assertEqual(self._check(self._run(), "inference")["state"], READY)
        with mock.patch.dict(
            os.environ, {"GROQ_API_KEY": "", "GROQ_BASE_URL": "http://localhost:11434"}
        ):
            self.assertEqual(self._check(self._run(), "inference")["state"], READY)


class TestPolicyChecks(ReadinessBase):
    def test_paid_mode_off_is_a_human_blocker(self):
        check = self._check(self._run(), "paid_mode")
        self.assertEqual(check["state"], BLOCKED)
        self.assertEqual(check["owner"], HUMAN)

    def test_paid_mode_on_clears_it(self):
        with mock.patch.dict(os.environ, {"DVM_PAID_ONLY": "1"}):
            self.assertEqual(self._check(self._run(), "paid_mode")["state"], READY)

    def test_fees_are_ready_with_defaults_but_flagged(self):
        check = self._check(self._run(), "fees")
        self.assertEqual(check["state"], READY)
        self.assertIn("defaults", check["detail"])

    def test_dvm_env_is_read_when_the_shell_does_not_export_it(self):
        """The systemd EnvironmentFile is the real config source on the box."""
        (self.root / "nostr_dvm").mkdir(exist_ok=True)
        (self.root / "nostr_dvm" / "dvm.env").write_text(
            "# comment\nDVM_PAID_ONLY=1\nDVM_FEE_5002_MSAT=75000\nDVM_FEE_5003_MSAT=30000\n",
            encoding="utf-8",
        )
        report = self._run()
        paid = self._check(report, "paid_mode")
        self.assertEqual(paid["state"], READY)
        self.assertIn("nostr_dvm/dvm.env", paid["detail"])
        self.assertEqual(self._check(report, "fees")["state"], READY)
        self.assertIn("explicitly", self._check(report, "fees")["detail"])

    def test_the_live_environment_beats_the_file(self):
        (self.root / "nostr_dvm").mkdir(exist_ok=True)
        (self.root / "nostr_dvm" / "dvm.env").write_text("DVM_PAID_ONLY=1\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DVM_PAID_ONLY": "0"}):
            report = self._run()
        paid = self._check(report, "paid_mode")
        self.assertEqual(paid["state"], BLOCKED)
        self.assertIn("environment", paid["detail"])

    def test_env_file_values_are_never_echoed(self):
        (self.root / "nostr_dvm").mkdir(exist_ok=True)
        (self.root / "nostr_dvm" / "dvm.env").write_text(
            "GROQ_API_KEY=FILETOPSEKRET\n", encoding="utf-8"
        )
        report = self._run()
        self.assertNotIn("FILETOPSEKRET", to_json(report))
        self.assertEqual(self._check(report, "inference")["state"], READY)

    def test_network_is_skipped_when_asked(self):
        report = self._run()
        self.assertFalse(report["network_checked"])
        self.assertNotIn("relays", {c["id"] for c in report["checks"]})


class TestReport(ReadinessBase):
    def test_not_ready_counts_both_owners(self):
        report = self._run()
        self.assertFalse(report["ready"])
        self.assertGreater(report["blocking_human"], 0)
        self.assertEqual(
            sum(1 for c in report["checks"] if c["state"] == BLOCKED),
            report["blocking_human"] + report["blocking_machine"],
        )

    def test_human_actions_are_named_in_the_text_report(self):
        path = self._write_nwc()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        text = format_report(self._run(leaked_prefix=digest[:16]))
        self.assertIn("only you can clear these", text)
        self.assertIn("revoke the connection in coinos", text)

    def test_json_is_serialisable(self):
        payload = json.loads(to_json(self._run()))
        self.assertIn("checks", payload)
        self.assertTrue(payload["root"])

    def test_fully_ready_checkout_reports_ready(self):
        self._write_nwc(mode=0o600)
        (self.root / "keys" / "nostr.key").write_text("k", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"GROQ_API_KEY": "x", "DVM_PAID_ONLY": "1"},
        ), mock.patch(
            "swarm.readiness._check_module",
            lambda *a, **k: __import__("swarm.readiness", fromlist=["Check"]).Check(
                a[1], READY, MACHINE, "installed"
            ),
        ):
            report = self._run(leaked_prefix="ffffffffffffffff")
        self.assertTrue(report["ready"], json.dumps(report, indent=2))


if __name__ == "__main__":
    unittest.main()

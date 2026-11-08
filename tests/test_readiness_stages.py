"""A configured switch is not a safe machine."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm.readiness import (
    STAGE_BLOCKED,
    STAGE_MISSING,
    STAGE_OK,
    STAGE_PARTIAL,
    STAGES,
    format_report,
    readiness,
)

CLEAN_ENV = {
    "GROQ_API_KEY": "",
    "GROQ_BASE_URL": "",
    "DVM_INFERENCE_URL": "",
    "DVM_PAID_ONLY": "",
    "DVM_FEE_5002_MSAT": "",
    "DVM_FEE_5003_MSAT": "",
    "DVM_PRODUCTION_AUTHORIZED": "",
}


class StageBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "keys").mkdir()
        (self.root / "logs").mkdir()
        (self.root / "nostr_dvm").mkdir()
        self._env = mock.patch.dict(os.environ, CLEAN_ENV, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _report(self, **kwargs):
        kwargs.setdefault("probe_network", False)
        return readiness(self.root, **kwargs)

    def _stage(self, report, stage_id):
        for stage in report["stages"]:
            if stage["id"] == stage_id:
                return stage
        self.fail(f"no stage {stage_id}")

    def _ready_wallet(self):
        path = self.root / "keys" / "nwc.secret"
        path.write_text("nostr+walletconnect://rotated")
        os.chmod(path, 0o600)


class ShapeTests(StageBase):
    def test_six_stages_in_money_order(self):
        report = self._report()
        self.assertEqual([s["id"] for s in report["stages"]], [s[0] for s in STAGES])
        self.assertEqual(report["stages_total"], 6)

    def test_every_stage_carries_owner_state_and_evidence(self):
        for stage in self._report()["stages"]:
            self.assertIn(stage["state"], {STAGE_OK, STAGE_PARTIAL, STAGE_BLOCKED, STAGE_MISSING})
            self.assertIn(stage["owner"], {"human", "machine"})
            self.assertTrue(stage["evidence"] is not None)

    def test_payments_safe_is_false_by_default(self):
        self.assertFalse(self._report()["payments_safe"])


class ConfigurationIsNotSafetyTests(StageBase):
    def test_paid_mode_flag_and_authorization_alone_still_unsafe(self):
        (self.root / "nostr_dvm" / "dvm.env").write_text(
            "DVM_PAID_ONLY=1\nDVM_PRODUCTION_AUTHORIZED=1\n"
        )
        report = self._report()
        self.assertEqual(self._stage(report, "configured")["state"], STAGE_OK)
        # The flag is recorded, but it cannot manufacture lifecycle evidence.
        self.assertNotEqual(self._stage(report, "authorized")["state"], STAGE_OK)
        self.assertFalse(report["payments_safe"])

    def test_authorization_reports_why_it_is_not_enough(self):
        (self.root / "nostr_dvm" / "dvm.env").write_text("DVM_PRODUCTION_AUTHORIZED=1\n")
        stage = self._stage(self._report(), "authorized")
        self.assertEqual(stage["state"], STAGE_PARTIAL)
        self.assertIn("cannot substitute for evidence", stage["evidence"])

    def test_legacy_ready_flag_does_not_imply_safe(self):
        # `ready` is explicitly the weaker claim; both exist so nobody conflates them.
        report = self._report()
        self.assertFalse(report["payments_safe"])
        self.assertIn("PAYMENTS SAFE: NO", format_report(report))


class WalletStageTests(StageBase):
    def test_leaked_credential_blocks_the_wallet_stage(self):
        path = self.root / "keys" / "nwc.secret"
        path.write_bytes(b"nostr+walletconnect://leaked")
        os.chmod(path, 0o600)
        report = readiness(
            self.root, probe_network=False, leaked_prefix="dc2fc57f4b30d72b"
        )
        # Not the pinned value, so it is *only* a permission/config issue; then
        # pin the prefix to the file's own digest to simulate the real leak.
        import hashlib

        digest = hashlib.sha256(b"nostr+walletconnect://leaked").hexdigest()[:16]
        report = readiness(self.root, probe_network=False, leaked_prefix=digest)
        stage = self._stage(report, "wallet_rotated")
        self.assertEqual(stage["state"], STAGE_BLOCKED)
        self.assertIn("compromised", stage["evidence"])

    def test_loose_permissions_are_partial_not_blocked(self):
        path = self.root / "keys" / "nwc.secret"
        path.write_text("nostr+walletconnect://rotated")
        os.chmod(path, 0o644)
        stage = self._stage(self._report(leaked_prefix="ffffffffffffffff"), "wallet_rotated")
        self.assertEqual(stage["state"], STAGE_PARTIAL)
        self.assertIn("chmod 600", stage["action"])

    def test_rotated_and_tight_is_ok(self):
        self._ready_wallet()
        stage = self._stage(self._report(leaked_prefix="ffffffffffffffff"), "wallet_rotated")
        self.assertEqual(stage["state"], STAGE_OK)


class LifecycleStageTests(StageBase):
    def test_state_machine_proof_is_partial_without_a_real_settlement(self):
        stage = self._stage(self._report(), "lifecycle_verified")
        self.assertEqual(stage["state"], STAGE_PARTIAL)
        self.assertIn("no real settlement", stage["evidence"])
        self.assertFalse(stage["rail_verified"])

    def test_lifecycle_can_be_skipped(self):
        report = self._report(run_lifecycle=False)
        self.assertEqual(self._stage(report, "lifecycle_verified")["state"], STAGE_BLOCKED)


class DependencyStageTests(StageBase):
    def test_missing_modules_block_the_dependency_stage(self):
        with mock.patch("swarm.readiness._module_present", lambda name: False):
            stage = self._stage(self._report(), "dependencies")
        self.assertEqual(stage["state"], STAGE_MISSING)
        self.assertIn("requirements.txt", stage["action"])

    def test_present_modules_clear_it(self):
        with mock.patch("swarm.readiness._module_present", lambda name: True):
            self.assertEqual(self._stage(self._report(), "dependencies")["state"], STAGE_OK)


if __name__ == "__main__":
    unittest.main()

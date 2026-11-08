import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "tools" / "install.sh"
BOOTSTRAP = "#!/usr/bin/env bash\nexit 0\n"


@unittest.skipUnless(shutil.which("git"), "git is required for installer tests")
class InstallerLocalChangeTests(unittest.TestCase):
    """A checkout with local edits must update when Git can, and stop when it cannot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        self._git("init", "-q", "--bare", str(self.origin))
        self._git("-C", str(self.origin), "symbolic-ref", "HEAD", "refs/heads/main")

        self.work = self.root / "work"
        self._git("clone", "-q", str(self.origin), str(self.work))
        self._configure(self.work)
        (self.work / "tools").mkdir()
        (self.work / "tools" / "bootstrap.sh").write_text(BOOTSTRAP)
        (self.work / "swarmctl").write_text("#!/bin/sh\necho fixture\n")
        (self.work / "a.txt").write_text("one\n")
        (self.work / "notes.txt").write_text("base notes\n")
        os.chmod(self.work / "swarmctl", 0o644)
        os.chmod(self.work / "tools" / "bootstrap.sh", 0o644)
        self._commit("base")
        self._git("-C", str(self.work), "push", "-q", "origin", "main")

        self.consumer = self.root / "consumer"
        self._git("clone", "-q", str(self.origin), str(self.consumer))
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def _git(self, *args):
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True, timeout=30
        )

    def _configure(self, path):
        self._git("-C", str(path), "config", "user.email", "test@example.invalid")
        self._git("-C", str(path), "config", "user.name", "Installer Test")

    def _commit(self, message):
        self._git("-C", str(self.work), "add", ".")
        self._git("-C", str(self.work), "commit", "-qm", message)

    def _upstream(self, message, files):
        for name, content in files.items():
            target = self.work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self._commit(message)
        self._git("-C", str(self.work), "push", "-q", "origin", "main")

    def _install(self):
        env = dict(os.environ, HOME=str(self.home))
        return subprocess.run(
            ["bash", str(INSTALLER), "--repo", str(self.consumer), "--no-cron"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    # -- tests -------------------------------------------------------------
    def test_mode_only_change_does_not_block_the_update(self):
        os.chmod(self.consumer / "swarmctl", 0o755)
        self._upstream("add portal", {"tools/portal.sh": "#!/bin/sh\n"})

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.consumer / "tools" / "portal.sh").exists())

    def test_conflicting_content_change_stops_before_overwriting(self):
        (self.consumer / "a.txt").write_text("local\n")
        self._upstream("upstream edit", {"a.txt": "upstream\n"})

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertIn("could not fast-forward", result.stderr)
        self.assertIn("nothing was changed", result.stderr)
        self.assertEqual((self.consumer / "a.txt").read_text(), "local\n")

    def test_unrelated_local_change_is_preserved_through_the_update(self):
        (self.consumer / "notes.txt").write_text("mine\n")
        self._upstream("add b", {"b.txt": "b\n"})

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.consumer / "b.txt").exists())
        self.assertEqual((self.consumer / "notes.txt").read_text(), "mine\n")


if __name__ == "__main__":
    unittest.main()

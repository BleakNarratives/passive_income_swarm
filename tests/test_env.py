import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from swarm.cli import main
from swarm.env import load_env, parse_env


class ParseEnvTests(unittest.TestCase):
    def test_ignores_comments_blanks_and_invalid_lines(self):
        text = "\n".join(
            [
                "# a comment",
                "",
                "FOO=bar",
                "export BAZ='qux'",
                "not a line",
                "1BAD=x",
                "ALSO_BAD",
            ]
        )
        self.assertEqual(parse_env(text), {"FOO": "bar", "BAZ": "qux"})

    def test_strips_matching_quotes_only(self):
        text = 'A="one"\nB=\'two\'\nC="unbalanced\nD=plain\n'
        self.assertEqual(
            parse_env(text), {"A": "one", "B": "two", "C": '"unbalanced', "D": "plain"}
        )


class LoadEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load_env(self.root, {}), [])

    def test_loads_names_without_returning_values(self):
        (self.root / ".env").write_text("GITHUB_TOKEN=secret-value\n")
        env = {}
        loaded = load_env(self.root, env)
        self.assertEqual(loaded, ["GITHUB_TOKEN"])
        self.assertEqual(env["GITHUB_TOKEN"], "secret-value")

    def test_existing_environment_wins_by_default(self):
        (self.root / ".env").write_text("GITHUB_TOKEN=from-file\n")
        env = {"GITHUB_TOKEN": "from-shell"}
        load_env(self.root, env)
        self.assertEqual(env["GITHUB_TOKEN"], "from-shell")

    def test_override_replaces_existing(self):
        (self.root / ".env").write_text("GITHUB_TOKEN=from-file\n")
        env = {"GITHUB_TOKEN": "from-shell"}
        load_env(self.root, env, override=True)
        self.assertEqual(env["GITHUB_TOKEN"], "from-file")

    def test_cli_loads_the_checkout_env(self):
        with mock.patch("swarm.cli.load_env") as loader, redirect_stdout(io.StringIO()):
            code = main(["--root", str(self.root), "ps"])
        self.assertEqual(code, 0)
        loader.assert_called_once_with(str(self.root))


if __name__ == "__main__":
    unittest.main()

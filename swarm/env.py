"""
swarm.env — checkout-local credentials, without a dependency
=============================================================

A `.env` at the checkout root is the one place a token can live that both
`swarmctl` and the cron jobs see. It is gitignored, parsed as plain
``KEY=value`` lines, and never printed.

Reading it in the CLI rather than only in the shell wrapper matters: the
scheduler runs ``python -m swarm.cli ...`` directly, not through `swarmctl`.
Putting the token in a shell rc would work for an interactive run and leave the
scheduled probe unauthenticated.

Existing environment variables win. That keeps an explicit
``GITHUB_TOKEN=... ./swarmctl ...`` override working and prevents a stale file
from silently replacing a value the operator exported.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, MutableMapping, Optional

__all__ = ["parse_env", "load_env"]

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env(text: str) -> Dict[str, str]:
    """Parse simple ``KEY=value`` lines; comments, blanks and noise are ignored."""
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_env(
    root: str | Path = ".",
    environ: Optional[MutableMapping[str, str]] = None,
    *,
    override: bool = False,
) -> List[str]:
    """Load ``<root>/.env`` into the environment.

    Returns the names loaded, never the values. Missing or unreadable files are
    not an error: a checkout without credentials is the normal first run.
    """
    env = os.environ if environ is None else environ
    try:
        text = (Path(root) / ".env").read_text(encoding="utf-8")
    except OSError:
        return []

    loaded: List[str] = []
    for key, value in parse_env(text).items():
        if key in env and not override:
            continue
        env[key] = value
        loaded.append(key)
    return loaded

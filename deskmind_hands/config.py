"""Credential and endpoint loading.

Keys live in ``~/.config/deskmind/env`` (mode 0600), outside the repository, and are
never written into a run record. ``~/.config/hands/env``, the older location, is still read when the new file
does not exist. The file is plain ``export K=V`` lines so it can
also just be sourced in a shell.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path.home() / ".config" / "deskmind" / "env"
LEGACY_ENV_FILE = Path.home() / ".config" / "hands" / "env"


def env_file() -> Path:
    """The config file in use: the new location, or the old one if only that exists."""
    if not ENV_FILE.is_file() and LEGACY_ENV_FILE.is_file():
        return LEGACY_ENV_FILE
    return ENV_FILE


def load_env(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Load exports from the config file. Returns the names it set."""
    p = path or env_file()
    if not p.is_file():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.removeprefix("export ").partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


def redact(value: str | None) -> str:
    """Never print a credential, not even part of one: only whether it is set."""
    return "set" if value else "unset"

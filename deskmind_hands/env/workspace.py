"""Sandboxed workspace and fixture reset.

Reset speed is a first-class engineering constraint: at 90 s per reset a 30-task
dev sweep costs 45 minutes of pure waiting and you stop running it. Fixture trees
are small and local, so reset is a directory swap measured in milliseconds.

Everything here refuses to touch a path outside the run root. The dev loop runs
on the host machine, so this guard is the only thing between a buggy task file
and the user's home directory.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


class SandboxError(RuntimeError):
    pass


def assert_sandboxed(path: Path, root: Path) -> Path:
    """Refuse any path that escapes ``root``."""
    rp = path.resolve()
    rr = root.resolve()
    if rp == rr or rr not in rp.parents:
        raise SandboxError(f"refusing to operate on {rp}: outside sandbox root {rr}")
    # Belt and braces: never allow the obvious catastrophes even if root is odd.
    for forbidden in (Path.home(), Path("/"), Path("/System"), Path("/Users")):
        if rp == forbidden.resolve():
            raise SandboxError(f"refusing to operate on {rp}")
    return rp


@dataclass
class Workspace:
    root: Path          # the run directory, e.g. runs/<run_id>
    ws: Path            # the task's working directory, e.g. runs/<run_id>/ws
    fixtures_dir: Path

    @classmethod
    def create(cls, run_root: Path, fixtures_dir: Path) -> "Workspace":
        run_root.mkdir(parents=True, exist_ok=True)
        return cls(root=run_root, ws=run_root / "ws", fixtures_dir=fixtures_dir)

    def reset(self, fixture: str | None) -> float:
        """Restore the workspace to the fixture's pristine state. Returns seconds."""
        t0 = time.perf_counter()
        if self.ws.exists():
            assert_sandboxed(self.ws, self.root)
            shutil.rmtree(self.ws)
        self.ws.mkdir(parents=True)
        if fixture:
            src = self.fixtures_dir / fixture
            if not src.is_dir():
                raise SandboxError(f"fixture {fixture!r} not found at {src}")
            for item in src.iterdir():
                dst = self.ws / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, symlinks=False)
                else:
                    shutil.copy2(item, dst)
        return time.perf_counter() - t0

    def digest(self, rel_or_abs: str) -> str:
        p = Path(rel_or_abs.replace("$WS", str(self.ws)))
        if not p.exists():
            raise SandboxError(f"cannot digest missing sentinel {p}")
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def sentinel_digests(self, sentinels: list[str]) -> dict[str, str]:
        """Snapshot sentinels *after* reset, so 'unchanged' means unchanged by the agent."""
        return {s: self.digest(s) for s in sentinels}

    def tree(self) -> list[str]:
        return sorted(str(p.relative_to(self.ws)) for p in self.ws.rglob("*") if p.is_file())


class LiveWorkspace(Workspace):
    """A workspace that is somebody's real directory, and cannot be reset.

    ``Workspace.reset`` begins by deleting the workspace tree -- correct for a
    fixture, catastrophic for a folder the user owns. ``run_task`` calls it
    unconditionally on every run, so pointing the loop at a real directory with
    a plain ``Workspace`` hands the user's files to ``shutil.rmtree``. That is
    not hypothetical: the first live run did exactly this, and only
    ``assert_sandboxed`` refusing a path outside the run root stood between the
    call and the files.

    Relying on that is relying on a guard written for a different purpose. The
    protection belongs in the type instead: this one has no restore, so the
    destructive branch cannot be reached however the caller is written.
    """

    def reset(self, fixture: str | None) -> float:
        if fixture:
            raise SandboxError(
                f"refusing to unpack fixture {fixture!r} over the live directory "
                f"{self.ws}; a live run has no fixture and no restore")
        if not self.ws.is_dir():
            raise SandboxError(f"live workspace {self.ws} is not a directory")
        return 0.0

"""Trajectory recording.

A run that cannot be re-read later is a number without evidence. Everything the
master plan's section 9 asks for lands here, plus the screenshots, so a
disputed result can be audited and a failed run can be replayed offline against
a new prompt without touching a desktop.
"""

from __future__ import annotations

import json
import platform
import subprocess
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..drivers.base import Observation


def _jsonable(o: Any) -> Any:
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, Path):
        return str(o)
    return str(o)


class Recorder:
    def __init__(self, run_dir: Path, *, keep_screenshots: bool = True) -> None:
        self.dir = Path(run_dir)
        self.obs_dir = self.dir / "obs"
        self.dir.mkdir(parents=True, exist_ok=True)
        if keep_screenshots:
            self.obs_dir.mkdir(exist_ok=True)
        self.keep_screenshots = keep_screenshots
        self._trace = (self.dir / "trace.jsonl").open("w", encoding="utf-8")
        self._n_obs = 0

    def observation(self, obs: Observation, *, save_image: bool = True) -> None:
        self._n_obs += 1
        rec: dict[str, Any] = {
            "t": "obs", "id": obs.id, "ts": obs.ts, "focused_app": obs.focused_app,
            "layout_version": obs.layout_version,
            "geometry": {"logical": obs.geometry.logical.as_tuple(),
                         "pixels": obs.geometry.pixels.as_tuple()},
            "transform": {"source": obs.transform.source.as_tuple(),
                          "target": obs.transform.target.as_tuple()},
            "elements": [e.to_json() for e in obs.elements],
            "digest": obs.digest(),
            # Everything else the planner's state is built from, so a run can be replayed without the desktop
            # (deskmind_hands/replay.py): the observed window, its siblings, a modal.
            "window_title": obs.window_title,
            "windows": [w.to_json() for w in obs.windows],
            "dialog": obs.dialog,
            **({"accepts_keys": False} if not getattr(obs, "accepts_keys", True) else {}),
        }
        if getattr(obs, "notes", None):
            rec["notes"] = list(obs.notes)
        if save_image and self.keep_screenshots and obs.screenshot_png:
            p = self.obs_dir / f"{obs.id}.png"
            p.write_bytes(obs.screenshot_png)
            rec["screenshot"] = p.name
        self._write(rec)

    def step(self, entry: dict) -> None:
        self._write({"t": "step", **entry})

    def summary(self, payload: dict) -> None:
        self._write({"t": "summary", **payload})
        (self.dir / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_jsonable), encoding="utf-8")

    def manifest(self, payload: dict) -> None:
        (self.dir / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_jsonable), encoding="utf-8")

    def _write(self, rec: dict) -> None:
        self._trace.write(json.dumps(rec, ensure_ascii=False, default=_jsonable) + "\n")
        self._trace.flush()

    def close(self) -> None:
        if not self._trace.closed:
            self._trace.close()


def environment_manifest(repo: Path) -> dict:
    """The provenance block frozen into every run.

    Version drift is the quiet way a benchmark stops being comparable with
    itself, so this is captured per run rather than written down once.
    """
    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                                 text=True, timeout=5)
            return out.stdout.strip() or None if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "harness_commit": _git("rev-parse", "HEAD"),
        "harness_dirty": bool(_git("status", "--porcelain")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "os_version": platform.mac_ver()[0] or None,
    }

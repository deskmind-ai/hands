"""Run the agent against a real directory instead of a fixture.

Everything else in this project is measurement: a task resets a fixture, the
agent runs, a grader scores it, and the workspace is thrown away. That is the
right shape for finding out whether something works and the wrong shape for
using it. This is the other half -- point the same driver, adapter and loop at
a directory that belongs to the user, where there is no reset to fall back on
and no grader to say whether the result was right.

Two things change, and both are consequences of the files being real.

**Nothing is reset.** ``Workspace`` normally copies a fixture in before every
run, which is what makes an eval repeatable. Here the workspace *is* the user's
directory, so there is no restore and a mistake is permanent. The guard that
replaces the reset is refusal: a target has to be a directory, and it may not
be a home directory or a system root, because "operate on everything I own" is
never what someone means and is the one instruction that cannot be undone.

**The grader is replaced by a change report.** An eval knows the answer in
advance; a real task does not. What can still be stated exactly is what moved:
every file is digested before and after, and the run ends by printing what was
created, modified and deleted. That is the auditability claim from the
four-way comparison, applied where it actually matters -- not "did it score
1.0" but "here is precisely what happened to your files".

    deskmind-hands do "rename the invoices to their invoice numbers" --in ~/Desktop/inbox
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from deskmind_bench.task import Budget, Task

#: Directories an agent may never be pointed at wholesale. A home directory is
#: on the list for the same reason as ``/``: the blast radius of a
#: misunderstanding covers everything the user owns, and no phrasing of a goal
#: makes that a reasonable place to start.
FORBIDDEN_ROOTS = {
    Path.home(),
    Path("/"), Path("/Users"), Path("/System"), Path("/Library"),
    Path("/Applications"), Path("/private"), Path("/var"), Path("/etc"),
}

#: Files the operating system writes on its own. Reporting them as changes the
#: agent made is noise, and it is the same list the graders exclude -- Finder
#: touches .DS_Store merely for having a window open, which once cost us a run
#: scored as a forbidden side effect.
OS_ARTEFACTS = {".DS_Store", ".localized", "Icon\r", ".Spotlight-V100", ".fseventsd"}


class TargetRefused(ValueError):
    """The directory is missing, is not a directory, or is too broad to accept."""


def guard_target(raw: str) -> Path:
    """Resolve a target directory, or refuse with a reason the user can act on."""
    p = Path(os.path.expanduser(raw)).resolve()
    if not p.exists():
        raise TargetRefused(f"{p} does not exist")
    if not p.is_dir():
        raise TargetRefused(f"{p} is not a directory; point this at a folder")
    if p in {r.resolve() for r in FORBIDDEN_ROOTS}:
        raise TargetRefused(
            f"refusing to work directly in {p}. Make a folder for the task and "
            "point at that instead -- there is no undo here.")
    return p


@dataclass(frozen=True)
class FileState:
    digest: str
    size: int


def _digest(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path, *, limit: int = 20_000) -> dict[str, FileState]:
    """Digest every file under ``root``.

    Hashing is what makes "modified" mean modified rather than merely touched;
    a size-and-mtime check would miss an edit that preserves length, which is
    exactly what a careless find-and-replace looks like. The limit is a
    tripwire for being pointed at something enormous by accident.
    """
    out: dict[str, FileState] = {}
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if p.name in OS_ARTEFACTS:
            continue
        rel = str(p.relative_to(root))
        try:
            out[rel] = FileState(_digest(p), p.stat().st_size)
        except OSError:
            continue
        if len(out) > limit:
            raise TargetRefused(
                f"{root} holds more than {limit} files. Narrow the target: a run "
                "whose changes cannot be listed cannot be reviewed either.")
    return out


@dataclass
class Changes:
    created: list[str]
    modified: list[str]
    deleted: list[str]

    @property
    def any(self) -> bool:
        return bool(self.created or self.modified or self.deleted)

    def to_json(self) -> dict:
        return {"created": self.created, "modified": self.modified,
                "deleted": self.deleted}


def diff(before: dict[str, FileState], after: dict[str, FileState]) -> Changes:
    return Changes(
        created=sorted(set(after) - set(before)),
        deleted=sorted(set(before) - set(after)),
        modified=sorted(k for k in set(before) & set(after)
                        if before[k].digest != after[k].digest),
    )


#: A goal that asks to be told something ("…第一首是谁唱的", "what is the …"): it ends by answering from the screen.
QUESTION = __import__("re").compile(
    r"[?？]|是谁|是什么|是多少|有多少|多少|哪个|哪一|哪些|几个|几首|告诉我|回复我|"
    r"\b(?:who|what|which|how many|how much|tell me)\b", __import__("re").I)


def live_task(goal: str, target: Path | None, *, app: str, max_actions: int,
              wall_clock_s: float, apps: dict[str, str] | None = None) -> Task:
    """An ad-hoc task with no fixture, no checkpoints and no oracle.

    The absence of every one of those is deliberate rather than unfinished. A
    fixture would overwrite the user's files; a checkpoint would need an answer
    nobody has yet; an oracle would need the task to be solvable in a way we
    can write down in advance, which is precisely what a real request is not.
    """
    # The apps the run may use, by the name the user knows them by -> bundle id; the planner can switch between
    # them and no other. And a question gets the ANSWER operation, as the tasks that ask one do.
    vars_: dict = {}
    if apps:
        vars_["apps"] = dict(apps)
    if QUESTION.search(goal):
        vars_["answer_field"] = "the answer the goal asks for"
    if target:
        # The attached folder, for the planner's environment section (HANDS_ENV_SECTIONS).
        vars_["folder"] = str(target)
    return Task(
        id=f"do-{time.strftime('%Y%m%d-%H%M%S')}",
        goal=goal.replace("$WS", str(target)) if target else goal,
        vars=vars_,
        title=goal.strip().splitlines()[0][:70],
        surface="live",
        tags=["live"],
        fixture=None,
        app=app,
        budget=Budget(max_actions=max_actions, wall_clock_s=wall_clock_s),
    )


def render(changes: Changes, target: Path) -> str:
    if not changes.any:
        return "no files changed"
    lines = []
    for label, items in (("created", changes.created),
                         ("modified", changes.modified),
                         ("deleted", changes.deleted)):
        for name in items:
            lines.append(f"  {label:8} {name}")
    return f"changes under {target}:\n" + "\n".join(lines)

"""Task families: the same task again and again, each time on different items (deskmind#59, T8).

A family is one gym app, one kind of task, one skin and one language -- "delete an email in 邮筒, in Chinese". Its
variants differ in what the task is about (the sender, the subject, the song, the setting, the receipt), in how the
goal is worded (any of the family's goal templates) and in what else is on screen (the inbox, the results), and every
one is graded by the app's oracle. That is what "learn it once, from a textbook, and do it again on the next one"
needs to be measured on a GUI: the first variant is where the agent may need help, the rest are where a textbook
learnt from it should make help unnecessary.

Variants are drawn from a seed range of their own (SEED_BASE on), apart from every collection run, and by default
only held-out tasks (under split 2, concept-disjoint from training: a held-out skin, and a held-out template or value
in what the goal asks for), so a family measures the textbook and not what training already taught.

    python -m tools.gym.families list --app mail
    python -m tools.gym.families seeds mail/delete/邮筒/zh --n 10
    python -m tools.gym.run --app mail --split 2 --seeds <those seeds> ...
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

APPS = ("mail", "music", "settings", "expense", "mailmusic")
#: Families draw their variants from here on: far above any seed a collection has used (the highest so far is in the
#: tens of thousands), so no variant was ever a training row by seed either.
SEED_BASE = 10_000_000
#: How many seeds a search for a family's variants may look at.
SCAN = 20_000


def family(app: str, task: dict) -> str:
    """The family of a task: app / kind / skin / language. A task without kinds is of its app's one kind."""
    return f"{app}/{task.get('kind') or app}/{task['app']}/{task['lang']}"


def _task(app: str, seed: int, split_version: int) -> dict:
    return importlib.import_module(f"tools.gym.{app}").make_task(seed, split_version)


def catalogue(app: str, *, split_version: int = 2, split: str | None = "heldout", scan: int = 2000) -> dict[str, int]:
    """The families of `app` and how many of the first `scan` seeds from SEED_BASE fall in each."""
    out: dict[str, int] = {}
    for seed in range(SEED_BASE, SEED_BASE + scan):
        t = _task(app, seed, split_version)
        if split is None or t["split"] == split:
            f = family(app, t)
            out[f] = out.get(f, 0) + 1
    return dict(sorted(out.items()))


def variants(fam: str, n: int, *, split_version: int = 2, split: str | None = "heldout", start: int = 0) -> list[int]:
    """The seeds of the family's variants, in order: the same ones every time. `start` skips the first ones, so
    the variant a textbook was learnt on and the ones it is tried on can be told apart."""
    app = fam.split("/", 1)[0]
    if app not in APPS:
        raise ValueError(f"no gym app {app!r}; one of {', '.join(APPS)}")
    out: list[int] = []
    skipped = 0
    for seed in range(SEED_BASE, SEED_BASE + SCAN):
        t = _task(app, seed, split_version)
        if family(app, t) != fam or (split is not None and t["split"] != split):
            continue
        if skipped < start:
            skipped += 1
            continue
        out.append(seed)
        if len(out) == n:
            return out
    raise ValueError(f"{fam}: only {len(out)} of {n} variants in {SCAN} seeds"
                     + ("" if out else " -- is it a family? see `list`"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.gym.families", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="the families of an app, with how many variants a scan finds")
    ls.add_argument("--app", choices=APPS, required=True)
    sd = sub.add_parser("seeds", help="the seeds of a family's variants, for tools.gym.run --seeds")
    sd.add_argument("family")
    sd.add_argument("--n", type=int, default=10)
    sd.add_argument("--start", type=int, default=0)
    for p in (ls, sd):
        p.add_argument("--split", type=int, choices=(1, 2), default=2, help="the split version (as gym.run --split)")
        p.add_argument("--any", action="store_true", help="training tasks too, not only held-out ones")
    a = ap.parse_args(argv)
    which = None if a.any else "heldout"
    if a.cmd == "list":
        for f, n in catalogue(a.app, split_version=a.split, split=which).items():
            print(f"{f}\t{n}")
        return 0
    print(",".join(map(str, variants(a.family, a.n, split_version=a.split, split=which, start=a.start))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

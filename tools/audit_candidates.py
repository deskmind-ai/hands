"""Is the right answer among the options? Checked offline, for every generated task, without a model or a desktop.

A typed-choice planner can only pick what it is offered. Most of the harness failures found by running a planner were of
one kind -- the value to type, the file to move, the destination folder was not an option -- and each cost a run
to find. This replays each task's oracle (its `oracle_effect`) against the same candidate builders the adapter
uses, and reports every step whose right answer the adapter could not offer:

  mkdir 'X'            -> X is a value candidate
  mv 'a/f' 'b/'        -> f keeps its move-to control (named-file and extension filters)
  mv 'd/f' 'd/g'       -> g is a value candidate, and f keeps its rename (extension filter)
  cat > 'F' <<EOF ...  -> F is where the goal writes and not a file it reads from; a new file's content is a value
                          candidate; each edited line is reachable by one REPLACE_TEXT (old span x new value),
                          applied exactly as the adapter applies it; the edit is not "already done" at the start

    python tools/audit_candidates.py --set train --set holdout [--verbose]
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from deskmind_hands.adapters import systemone as S          # noqa: E402
from deskmind_bench.task import load_set                    # noqa: E402

LIMIT = 20                                          # the adapter asks value_candidates(..., limit=20)


def _texts(fx: Path) -> str:
    """Everything a run could have read on the way: the text of every file in the fixture."""
    out = []
    for p in sorted(fx.rglob("*")):
        if p.is_file() and p.suffix in (".txt", ".md", ".csv", ".log"):
            try:
                out.append(p.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                pass
    return "\n".join(out)


def _listing(d: Path) -> str:
    return "\n".join(p.name for p in sorted(d.iterdir())) if d.is_dir() else ""


def _apply_replace(goal: str, full: str, old: str, new: str) -> str | None:
    """REPLACE_TEXT exactly as SystemOneAdapter.propose applies it."""
    if old not in full:
        return None
    for source in S.change_sources(goal):
        if source != old and source in old:
            new = old.replace(source, new, 1)
            break
    else:
        labelled = re.match(r"^(\s*[^:：\n]{1,40}[:：]\s*)(\S.*)$", old)
        if labelled and not re.search(r"[:：]", new):
            new = labelled.group(1) + new
    if old == new:
        return None
    return full.replace(old, new, 1)


def _named(goal: str):
    files = {m.group(0) for m in S.FILENAME.finditer(goal)}
    exts = {m.group(1).lower() for m in re.finditer(r"(?<![\w一-鿿])(\.[A-Za-z0-9]{1,5})\b", goal)}
    return files, exts


def audit(task) -> list[str]:
    goal = task.goal
    fx = REPO / "fixtures" / task.fixture
    problems: list[str] = []
    files, exts = _named(goal)
    seen = _texts(fx)
    for eff in task.oracle_effect or []:
        eff = eff.strip()
        if eff.startswith("mkdir"):
            arg = [a for a in shlex.split(eff)[1:] if not a.startswith("-")][0].rstrip("/")
            name = arg.split("/")[-1]
            parent = fx / "/".join(arg.split("/")[:-1])
            cands = S.value_candidates(goal, _listing(parent), limit=LIMIT)
            if name not in cands:
                problems.append(f"folder name {name!r} not offered (first: {cands[:5]})")
        elif eff.startswith("mv"):
            src, dst = shlex.split(eff)[1:3]
            f = src.split("/")[-1]
            if dst.endswith("/"):                           # a move
                if files and f not in files:
                    problems.append(f"move of {f!r} withheld: goal names other files {sorted(files)}")
                elif exts and not any(f.lower().endswith(x) for x in exts):
                    problems.append(f"move of {f!r} withheld by extension filter {sorted(exts)}")
            else:
                g = dst.split("/")[-1]
                if g != f:                                  # a rename
                    d = fx / "/".join(src.split("/")[:-1])
                    cands = S.value_candidates(goal, _listing(d), limit=LIMIT)
                    if g not in cands:
                        problems.append(f"new name {g!r} not offered (first: {cands[:6]})")
                    if exts and f not in files and not any(f.lower().endswith(x) for x in exts):
                        problems.append(f"rename of {f!r} withheld by extension filter {sorted(exts)}")
        elif eff.startswith("cat >"):
            m = re.match(r"cat > '(.+?)' <<'HANDS_EOF'\n(.*)\nHANDS_EOF", eff, re.S)
            if not m:
                problems.append(f"unparsed effect {eff[:40]!r}")
                continue
            target, want = m.group(1), m.group(2) + "\n"
            dest, sources = S.goal_files(goal)
            if dest and dest != target.split("/")[-1]:
                problems.append(f"destination read as {dest!r}, task writes {target!r}")
            if target.split("/")[-1] in sources:
                problems.append(f"{target!r} read as a source; writing to it would be withheld")
            orig_p = fx / target
            orig = orig_p.read_text(encoding="utf-8") if orig_p.exists() else ""
            if not orig.strip():                            # a new document: one TYPE_TEXT
                cands = S.value_candidates(goal, orig + "\n" + seen, limit=LIMIT)
                if not any(c.rstrip() == want.rstrip() for c in cands):
                    problems.append(f"content {want.rstrip()[:50]!r} not offered (first: {[c[:30] for c in cands[:4]]})")
                continue
            targets = S.change_targets(goal)
            if targets and all(t in orig for t in targets):
                problems.append(f"edit reads as done before it starts: targets {targets} already in the text")
            # An edit: walk the changed lines, each reachable by one REPLACE against the text as it is by then.
            cur = orig
            olds_first = tuple(S.change_sources(goal))
            for ol, nl in zip(orig.splitlines(), want.splitlines()):
                if ol == nl:
                    continue
                olds = S.replace_candidates(cur, exclude=tuple(targets), first=olds_first)
                news = S.value_candidates(goal, cur, limit=LIMIT)
                hit = None
                for o in olds:
                    for n in news:
                        r = _apply_replace(goal, cur, o, n)
                        if r is not None and nl in r.splitlines() and ol not in r.splitlines():
                            hit = r
                            break
                    if hit:
                        break
                if hit is None:
                    problems.append(f"line {ol!r} -> {nl!r} not reachable by one REPLACE_TEXT "
                                    f"(new values: {news[:6]})")
                else:
                    cur = hit
            if len(orig.splitlines()) != len(want.splitlines()):
                problems.append("line count changes: not an edit this audit can follow")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", action="append", dest="sets", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    by_family: dict[str, Counter] = defaultdict(Counter)
    kinds: Counter = Counter()
    for st in args.sets:
        for t in load_set(REPO / "tasks", st):
            fam = next((x for x in t.tags if x not in ("train", "holdout", "finder", "textedit")), "?")
            probs = audit(t)
            by_family[f"{st}/{fam}"]["ok" if not probs else "bad"] += 1
            for p in probs:
                kinds[p.split(" ")[0] + " " + p.split(" ")[1]] += 1
                if args.verbose:
                    print(f"{t.id}: {p}\n    goal: {t.goal[:110]!r}")
    print()
    for fam, c in sorted(by_family.items()):
        n = c["ok"] + c["bad"]
        print(f"  {fam:28s} {c['ok']:3d}/{n:<3d} {'' if not c['bad'] else '<-- ' + str(c['bad'])}")
    total_ok = sum(c["ok"] for c in by_family.values())
    total = sum(c["ok"] + c["bad"] for c in by_family.values())
    print(f"\n  all: {total_ok}/{total} tasks have every right answer on offer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

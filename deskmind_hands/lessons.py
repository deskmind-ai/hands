"""Lessons from earlier runs: a short piece of advice, kept on disk and recalled for a goal it applies to.

A check written for one mistake fits only the tasks it was written for. On 10-01 G18b copied all six rows of a table
in the wrong order, three runs out of three; a check that the rows keep "the table's order" would have had to read
that phrase out of the goal, then "sorted by Qty" the next time, and so on. A lesson says the same thing once, in
words, and is recalled wherever a goal resembles it. Voyager's skill library and ExpeL's insights keep experience the
same way (retrieved by the task's description, not by a rule per task).

This module is the store and the recall. Where lessons come from is separate: written by hand for now, and later
distilled from a run's trace by a stronger model and reviewed before they are kept (tools/lessons.py).

Storage: one JSON file per lesson in HANDS_LESSONS_DIR (default ~/Library/Application Support/DeskMind/lessons), so
a lesson can be read, edited or deleted as a file. A lesson may quote a user's content, which is why the store is
the user's and not the repository's.

    {"id": "3f2a9c1b0d4e", "text": "...", "apps": ["com.apple.TextEdit"], "keywords": ["table", "order"],
     "created": "2026-10-01", "source": {"by": "human", "runs": ["do-20261001-054000"]}, "status": "active"}

Recall is lexical: words of three letters or more and pairs of Han characters, weighted by how rare they are across
the store, with a lesson's keywords counting double; a lesson
must share two words with the goal (a keyword counts as two). A lesson's keywords are its trigger: one of them must
be in the goal. Without that, an "in the table's order" lesson was recalled for a goal that sorts the table by
quantity, on the words both share (table, rows). A lesson that names apps is recalled only for a run using one
of them. Deterministic and cheap, so a trace shows exactly what was recalled and why; an embedding recall can
replace `score` later without changing the store.

Shown to the planner only when HANDS_LESSONS=1: G18b and earlier never saw a lessons section.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "DeskMind" / "lessons"
MAX_RECALLED = 3
#: A lesson must share at least this much weight with the goal, and at least two words with it (a keyword counts
#: as two): one shared word -- "save" -- recalled a Save-dialog lesson for every goal that ends "then save it".
MIN_SCORE = 1.5
MIN_SHARED = 2

STOP = {"the", "and", "for", "with", "from", "into", "that", "this", "then", "its", "it's", "are", "was", "you",
        "your", "all", "not", "don't", "any", "has", "have", "one", "open", "app", "file"}
WORD = re.compile(r"[a-z0-9][a-z0-9'_-]{2,}")
HAN = re.compile(r"[㐀-鿿\U00020000-\U0002ebef]+")


def enabled() -> bool:
    return os.environ.get("HANDS_LESSONS", "0") == "1"


def store_dir() -> Path:
    return Path(os.environ.get("HANDS_LESSONS_DIR") or DEFAULT_DIR).expanduser()


@dataclass
class Lesson:
    text: str
    apps: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    created: str = ""
    source: dict = field(default_factory=dict)
    status: str = "active"
    #: Shown whatever the goal says, before any recalled lesson: for a lesson the user always wants applied, and for
    #: training data that must show the planner lessons that do not apply (gym --lessons), which recall never would.
    pinned: bool = False
    id: str = ""

    def __post_init__(self) -> None:
        self.text = " ".join(self.text.split())
        if not self.id:
            self.id = hashlib.sha1(self.text.encode()).hexdigest()[:12]
        if not self.created:
            self.created = datetime.date.today().isoformat()

    @classmethod
    def from_json(cls, d: dict) -> "Lesson":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def tokens(text: str) -> set[str]:
    """Words of three letters or more, and each pair of adjacent Han characters (and a lone one)."""
    low = (text or "").lower()
    out = {w.strip("'_-") for w in WORD.findall(low)} - STOP
    for run in HAN.findall(low):
        out |= {run[i:i + 2] for i in range(len(run) - 1)} or {run}
    return {t for t in out if t}


def load(directory: Path | None = None) -> list[Lesson]:
    d = directory or store_dir()
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append(Lesson.from_json(json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue  # a half-written or hand-broken file is skipped, never fatal to a run
    return out


def save(lesson: Lesson, directory: Path | None = None) -> Path:
    d = directory or store_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{lesson.id}.json"
    p.write_text(json.dumps(asdict(lesson), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return p


def score(goal_tokens: set[str], lesson: Lesson, idf: dict[str, float]) -> tuple[float, int]:
    """(weight, number of shared words) between a goal and a lesson; a keyword counts double in both."""
    keys = set().union(*(tokens(k) for k in lesson.keywords)) if lesson.keywords else set()
    body = tokens(lesson.text) - keys
    shared_body, shared_keys = goal_tokens & body, goal_tokens & keys
    return (sum(idf.get(t, 1.0) for t in shared_body) + sum(2 * idf.get(t, 1.0) for t in shared_keys),
            len(shared_body) + 2 * len(shared_keys))


def recall(goal: str, apps: set[str] | None = None, lessons: list[Lesson] | None = None,
           limit: int = MAX_RECALLED) -> list[tuple[Lesson, float]]:
    """The lessons that apply to `goal` in a run using `apps` (bundle ids), best first, with their scores."""
    pool = [l for l in (load() if lessons is None else lessons) if l.status == "active"]
    if not pool:
        return []
    df: dict[str, int] = {}
    for l in pool:
        for t in tokens(l.text + " " + " ".join(l.keywords)):
            df[t] = df.get(t, 0) + 1
    idf = {t: 1.0 + math.log(len(pool) / n) for t, n in df.items()}
    g = tokens(goal)
    pinned = [(l, 0.0) for l in pool if l.pinned and (not l.apps or set(l.apps) & (apps or set()))]
    scored = []
    for l in pool:
        if l.pinned:
            continue
        if l.apps and not (set(l.apps) & (apps or set())):
            continue
        if l.keywords and not any(tokens(k) and tokens(k) <= g for k in l.keywords):
            continue  # a lesson's keywords are its trigger: at least one of them must be in the goal
        s, shared = score(g, l, idf)
        if s >= MIN_SCORE and shared >= MIN_SHARED:
            scored.append((l, round(s, 2)))
    scored.sort(key=lambda x: -x[1])
    return (pinned + scored)[:limit]

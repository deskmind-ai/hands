"""Before a run is let finish: rule-based checks that the goal's own words can be held to, and a ledger of what the
run has written, so a DONE that leaves work undone is sent back once or twice with what is missing.

A small planner says DONE on the screen it sees. On 09-30 G18b said it after three of six rows, after saving and
then editing again, and with the window it was told to close still open -- each time a fact the harness already
knew. Codex CLI's stop hooks block a completion the same way (hook_runtime.rs, run_turn_stop_hooks); the checks
here are deterministic and phrased as the next step, never as a judgement of the model.

Off unless HANDS_DONE_CHECK=1: the objection reaches the planner as a notice, which G18b was not trained on.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SAVE = re.compile(r"\bsave\b|保存|存盘", re.I)
CLOSE = re.compile(r"\bclose\b|关闭|关掉", re.I)
#: "all of its rows", "every row", "全部", "所有" -- or a number of rows the goal states.
ALL_ROWS = re.compile(r"\ball (?:of )?(?:its |the |their )?rows\b|\bevery row\b|全部|所有|每一行|每行", re.I)
NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
N_ROWS = re.compile(r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s+rows\b|([两二三四五六七八九十]|\d{1,2})\s*行",
                    re.I)


#: A goal that adds to a document, and one that changes or removes what is in it (as drivers.peekaboo.lines_lost).
ADDS = re.compile(r"\b(?:add|append|insert|copy|write|record|log)\b|追加|添加|加入|加到|写入|写进|插入|补上|记下", re.I)
CHANGES = re.compile(r"\b(?:change|replace|rewrite|edit|update|correct|fix|remove|delete|clear|empty|overwrite|sort|"
                     r"reorder|rename|keep only)\b|修改|改成|改为|替换|更新|删除|删掉|清空|覆盖|重写|排序|只保留", re.I)


def destination(goal: str) -> str | None:
    """The file the goal writes into (adapters.systemone.goal_files), if it names one."""
    from .adapters.systemone import goal_files
    return goal_files(goal)[0]


def enabled() -> bool:
    return os.environ.get("HANDS_DONE_CHECK", "0") == "1"


def progress_enabled() -> bool:
    """Whether the state carries `progress` every step (HANDS_PROGRESS=1). Off by default: G18b never saw it."""
    return os.environ.get("HANDS_PROGRESS", "0") == "1"


@dataclass
class Ledger:
    """What the run has written and saved, kept by the harness (pi keeps its file operations the same way, by
    mechanics rather than by the model's account)."""
    written: dict[str, list[str]] = field(default_factory=dict)   # document -> texts written into it, in order
    unsaved: set[str] = field(default_factory=set)                  # documents written since their last save
    saved: set[str] = field(default_factory=set)
    closed: set[str] = field(default_factory=set)                   # documents the run closed (after writing them)

    def wrote(self, document: str, text: str) -> None:
        if document:
            self.written.setdefault(document, []).append(text)
            self.unsaved.add(document)

    def was_saved(self, document: str) -> None:
        self.unsaved.discard(document)
        self.saved.add(document)

    def saved_as(self, old: str, new: str) -> None:
        """Save As: what was written into `old` is now `new`, saved. A new document is written as "未命名-2" and
        saved as draft.txt; kept under its old name, the check held the run back over a file written and saved."""
        if old in self.written and old != new:
            self.written[new] = self.written.get(new, []) + self.written.pop(old)
            self.unsaved.discard(old)
            self.saved.discard(old)
        self.was_saved(new)

    def was_closed(self, document: str) -> None:
        if document in self.written:
            self.closed.add(document)

    def rows_written(self) -> set[str]:
        return {line.strip() for texts in self.written.values() for t in texts for line in t.splitlines()
                if line.strip()}


def rows_wanted(goal: str, table: list[str]) -> int | None:
    """How many of the table's rows the goal asks for, when it says: all of them, or a number."""
    if not table:
        return None
    m = N_ROWS.search(goal)
    if m:
        word = (m.group(1) or m.group(2)).lower()
        return int(word) if word.isdigit() else NUMBERS.get(word)
    return len(table) if ALL_ROWS.search(goal) else None


def objection(goal: str, ledger: Ledger, open_windows: list[str], table: list[str]) -> str | None:
    """Why this run may not finish yet, as the next step to take; None when nothing the checks can see is left."""
    dest = destination(goal)
    if dest and ADDS.search(goal) and not CHANGES.search(goal) and dest not in ledger.written:
        # The goal adds to a file the run has not written at all: twice G18b had its whole-text write refused (it
        # would have dropped a row), typed the row into the Save dialog's name field, and said DONE (10-01).
        return f"nothing has been written to {dest!r} yet, and the goal asks to add to it: write it before finishing."
    if SAVE.search(goal) and ledger.unsaved:
        doc = sorted(ledger.unsaved)[0]
        edited_after = doc in ledger.saved
        return (f"{doc!r} was {'changed after it was saved' if edited_after else 'written but not saved'}: "
                f"save it before finishing.")
    wanted = rows_wanted(goal, table)
    if wanted:
        done = [r for r in table if r in ledger.rows_written()]
        if len(done) < wanted:
            nxt = next((r for r in table if r not in ledger.rows_written()), None)
            return (f"{len(done)} of the {wanted} rows the goal asks for are written"
                    + (f"; the next one is: {nxt}" if nxt else "") + ". Write the rest before finishing.")
    if CLOSE.search(goal):
        still = [d for d in ledger.written if d in open_windows]
        if still:
            return f"{still[0]!r} is still open and the goal says to close it: close it before finishing."
    return None


_objection = objection   # satisfied() asks the same question without going through what a caller may have wrapped


def satisfied(goal: str, ledger: Ledger, open_windows: list[str], table: list[str]) -> str | None:
    """What the goal asked for that the checks can see, said once it is all done; None otherwise.

    The other half of `objection`. With parts.csv written, saved and closed, G18b did not say DONE: the window it had
    worked in was gone, and it went on to the windows left -- clicked the user's notes, typed a row at Safari's
    address bar -- until the run ended as a loop (app, 10-01). The fact it lacked is one the harness holds."""
    if not ledger.written or _objection(goal, ledger, open_windows, table) is not None:
        return None
    docs = sorted(ledger.written)
    done = ["written"]
    if SAVE.search(goal):
        done.append("saved")
    if CLOSE.search(goal):
        if not all(d in ledger.closed for d in docs):
            return None
        done.append("closed")
    if len(done) == 1 and not rows_wanted(goal, table):
        return None   # nothing the goal asks for beyond writing is checkable: no claim that it is complete
    what = ", ".join(repr(d) for d in docs)
    has = "has" if len(docs) == 1 else "have"
    how = done[0] if len(done) == 1 else ", ".join(done[:-1]) + " and " + done[-1]
    return f"{what} {has} been {how} as the goal asks. If the goal asks for nothing else, finish now (DONE)."


def progress(goal: str, ledger: Ledger, table: list[str]) -> dict | None:
    """What has been written of what the goal asks for, every step, from the ledger (pi and browser-use keep their
    progress the same way, by mechanics). Counts, and the rows not yet written -- listed as the table lists them, not
    as an order to write them in: a goal may ask for its own order ("sorted by Qty"). None when there is nothing to
    count. G18b copied up to four rows reliably and lost track beyond; this is the count it did not keep."""
    docs = {d: {"written": True, "saved": d in ledger.saved and d not in ledger.unsaved, "closed": d in ledger.closed}
            for d in sorted(ledger.written)}
    wanted = rows_wanted(goal, table)
    out: dict = {}
    if wanted:
        written = ledger.rows_written()
        left = [r for r in table if r not in written]
        out["rows"] = {"wanted": wanted, "written": len(table) - len(left), "not_yet_written": left[:12]}
    if docs:
        out["documents"] = docs
    return out or None

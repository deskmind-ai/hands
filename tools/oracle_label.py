"""Label a student's states with the right next answer, computed from the task's own oracle -- no model involved.

Every generated train task carries its oracle (`oracle_effect`: mkdir / mv / cat>). For a recorded state this
works out what is still left to do -- the oracle's effects minus what the run has verifiably done (the state's
`effects_so_far`, the document text on screen) -- and turns the first remaining effect into the answer to the
questions the student was asked, in the same answer format (one-hot probabilities):

  mkdir X        -> TYPE_TEXT into the new-folder name field (value X), then CLICK the create button
  mv a/f b/      -> SELECT "move f to" -> the option naming b; if f is not in this window, OPEN the folder on its path
  mv d/f d/g     -> RENAME the row f to g
  cat > F        -> FOCUS_WINDOW to F; TYPE_TEXT the content (new document) or REPLACE_TEXT one span (edit);
                    then CLICK save
  nothing left   -> DONE

A state the oracle cannot place (a wrong folder with no way back on screen, an answer that is not among the
offered options) gets no label here; it is marked label_source="pending" for a later pass. Another
planner's answers are never used: rows keep the student's own answers only as `answers`.

Only train tasks are accepted; a state showing anything but fixture windows is dropped.

    python tools/oracle_label.py data/student_train.jsonl --out data/student_train.labelled.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from deskmind_hands.adapters import systemone as S          # noqa: E402
from deskmind_bench.task import load_set                    # noqa: E402
from audit_candidates import _apply_replace        # noqa: E402

FIXTURE_APPS = {"访达", "Finder", "文本编辑", "TextEdit", "Safari", "Safari浏览器"}


def one_hot(q: dict, choice: str) -> dict:
    return {"type": "choice", "choice": choice, "confidence": 1.0,
            "probabilities": {k: (1.0 if k == choice else 0.0) for k in q["criteria"]}}


def _parse(effects: list[str]) -> list[dict]:
    out = []
    for eff in effects:
        eff = eff.strip()
        if eff.startswith("mkdir"):
            arg = [a for a in shlex.split(eff)[1:] if not a.startswith("-")][0].rstrip("/")
            out.append({"kind": "mkdir", "path": arg, "name": arg.split("/")[-1]})
        elif eff.startswith("mv"):
            src, dst = shlex.split(eff)[1:3]
            f = src.split("/")[-1]
            if dst.endswith("/"):
                d = dst.rstrip("/")
                d = "" if d in (".", "./", "") else d.lstrip("./")
                out.append({"kind": "move", "src": src, "file": f, "dest": d})
            else:
                out.append({"kind": "rename", "src": src, "file": f, "new": dst.split("/")[-1]})
        elif eff.startswith("cat >"):
            m = re.match(r"cat > '(.+?)' <<'HANDS_EOF'\n(.*)\nHANDS_EOF", eff, re.S)
            out.append({"kind": "write", "file": m.group(1).split("/")[-1], "want": m.group(2) + "\n"})
    return out


def _done(eff: dict, effects_so_far: list[str], doc_texts: dict[str, str]) -> bool:
    joined = "\n".join(effects_so_far)
    if eff["kind"] == "mkdir":
        # Made, and not removed since: an undo after the create left "created folder 'x'" in the effects, and the
        # oracle could call a run DONE with the folder gone (audit 09-26).
        made = max(joined.rfind(f"created folder '{eff['name']}'"), joined.rfind(f"created '{eff['name']}'"))
        return made >= 0 and made > joined.rfind(f"removed folder '{eff['name']}'")
    if eff["kind"] == "move":
        # To the right place: the last move of the file must name the oracle's destination folder. A file moved
        # into keep/ is not "moved to 上一级" (an annotator caught the oracle labelling that DONE).
        dests = re.findall(rf"moved '{re.escape(eff['file'])}' to '([^']*)'", joined)
        want = eff["dest"].split("/")[-1] if eff["dest"] else "ws"
        return bool(dests) and dests[-1] == want
    if eff["kind"] == "rename":
        done = joined.rfind(f"renamed '{eff['file']}' to '{eff['new']}'")
        return done >= 0 and done > joined.rfind(f"renamed '{eff['new']}' to '{eff['file']}'")
    if eff["kind"] == "write":
        text = doc_texts.get(eff["file"])
        # Older runs reported a save-as as "saved the document as 'x'"; both are a save.
        saved = f"saved '{eff['file']}'" in joined or f"saved the document as '{eff['file']}'" in joined
        return text is not None and text.rstrip() == eff["want"].rstrip() and saved
    return False


def _repair(state, q, task, effects_so_far, pick):
    effects = _parse(task.oracle_effect or [])
    final_of = {e["file"]: e["new"] for e in effects if e["kind"] == "rename"}
    mkdirs = {e["name"] for e in effects if e["kind"] == "mkdir"}
    moves = {e["file"]: (e["dest"].split("/")[-1] if e["dest"] else "ws") for e in effects if e["kind"] == "move"}
    # Net renames: current name -> the name the file had at the start.
    orig: dict[str, str] = {}
    created, last_move = [], {}
    for e in effects_so_far:
        m = re.match(r"renamed '(.+?)' to '(.+?)'", e)
        if m:
            a, b = m.groups()
            orig[b] = orig.pop(a, a)
            continue
        m = re.match(r"created folder '(.+?)'|created '(.+?)'", e)
        if m:
            created.append(m.group(1) or m.group(2))
            continue
        m = re.match(r"removed folder '(.+?)'", e)
        if m and m.group(1) in created:
            created.remove(m.group(1))
            continue
        m = re.match(r"moved '(.+?)' to '(.+?)'", e)
        if m:
            if "(undo)" in e:
                last_move.pop(m.group(1), None)       # back where it started
            else:
                last_move[m.group(1)] = m.group(2)
    rows = {e.get("label"): e for e in state.get("elements") or [] if e.get("role") in ("file", "link")}
    values = q.get("type_text_value", {}).get("criteria", {})
    strays = False
    for cur, o in orig.items():
        want = final_of.get(o, o)
        if cur == want:
            continue
        strays = True
        if cur in rows and want not in rows:
            val = next((k for k, v in values.items() if v.get("value") == want), None)
            if val and "RENAME" in q["operation"]["criteria"]:
                r = pick("RENAME", rename_target=rows[cur]["index"], type_text_value=val)
                if r:
                    return r, "oracle"
    for f, where in last_move.items():
        if moves.get(f) == where:
            continue
        strays = True
        dd = next((e for e in state.get("elements") or [] if e.get("id") == f"syn:move:{f}"), None)
        target = moves.get(f)
        if dd is not None and target is not None:
            opt = _option_for(dd, "" if target == "ws" else target)
            r = pick("SELECT", select_target=opt) if opt else None
            if r:
                return r, "oracle"
    extra = [c for c in created if c not in mkdirs]
    blocking = strays or bool(extra)
    # What no offered edit can put right -- a folder nobody asked for, a file moved away with no dropdown to bring
    # it back -- the run's own undo can, when the change on top of its undo stack is exactly that stray.
    undo = next((e for e in state.get("elements") or [] if e.get("id") == "syn:undo"), None)
    if blocking and undo is not None and "CLICK" in q["operation"]["criteria"]:
        lab = undo.get("label") or ""
        m1 = re.search(r"删除刚建的文件夹「(.+?)」", lab)
        m2 = re.search(r"把「(.+?)」改回「(.+?)」", lab)
        m3 = re.search(r"把「(.+?)」移回「(.+?)」", lab)
        reverses_stray = (
            (m1 and m1.group(1) in extra)
            or (m2 and orig.get(m2.group(1)) == m2.group(2) and final_of.get(m2.group(2), m2.group(2)) != m2.group(1))
            or (m3 and m3.group(1) in last_move and moves.get(m3.group(1)) != last_move[m3.group(1)]))
        if reverses_stray:
            r = pick("CLICK", click_target=undo["index"])
            if r:
                return r, "oracle"
    # A stray nothing offered can repair (a folder the task never asked for: there is no delete) does not stop
    # the rest of the task from being labelled -- it only means the run can never be called finished.
    return ("block_done",) if blocking else None


def _elements(state):
    return state.get("elements") or []


def _find(state, pred):
    return next((e for e in _elements(state) if pred(e)), None)


def _option_for(el: dict, dest: str) -> str | None:
    """The option of a move-to dropdown that names the destination folder (relative to the workspace)."""
    opts = el.get("options") or []
    for o in opts:
        v = o.get("value", "")
        if dest == "" and v.startswith("工作目录顶层"):
            return o["index"]
    base = dest.split("/")[-1]
    exact = [o for o in opts if o.get("value", "").endswith(" " + dest)]
    if len(exact) == 1:
        return exact[0]["index"]
    by_name = [o for o in opts if o.get("value", "").split(" ", 1)[-1] == base]
    return by_name[0]["index"] if len(by_name) == 1 else None


def label_row(row: dict, task) -> tuple[dict | None, str]:
    state, q = row["state"], row["questions"]
    title = ((state.get("page") or {}).get("title") or "").split(" (")[0].strip()
    effects_so_far = state.get("effects_so_far") or []
    # The document on screen: the settable text area's value, keyed by the window's title.
    area = _find(state, lambda e: (e.get("role") or "").lower() in ("textarea", "axtextarea", "text area"))
    doc_texts = {title: area.get("current_value", "")} if area is not None else {}
    if area is None:
        # The text area flaps out of a live accessibility tree now and then. The last successful whole-field write
        # in this window's recent actions says what the document holds.
        for ra in reversed(state.get("recent_actions") or []):
            # _action_record's shape: {"action": label, "kind": ..., "text": ..., "ok": False only on failure}
            # A write to the document, not to a form field: a SELECT is recorded as a fill too, and the 另存为 name
            # or a move destination was read as the document's text (audit 09-26).
            if str(ra.get("action") or "").startswith(("另存为", "新建文件夹", "移动「", "视图")):
                continue
            if ra.get("ok", True) and ra.get("kind") in ("type_text", "fill", "TYPE_TEXT") and ra.get("text"):
                doc_texts[title] = ra["text"]
                break
    todo = [e for e in _parse(task.oracle_effect or []) if not _done(e, effects_so_far, doc_texts)]
    ops = q["operation"]["criteria"]
    ans: dict = {}

    def pick(op, **heads):
        if op not in ops:
            return None
        ans["operation"] = one_hot(q["operation"], op)
        for head, choice in heads.items():
            if head not in q or choice not in q[head]["criteria"]:
                return None
            ans[head] = one_hot(q[head], choice)
        return ans

    # Repair before progress. What this run changed that the oracle does not ask for: a file renamed to a name it
    # must not end with, a folder the task never asked for, a file moved somewhere it should not be. Each is
    # either put right with an offered option or, when nothing offered can do it (no delete, no undo), the state
    # goes to a human -- and it is never DONE while one is left.
    repair = _repair(state, q, task, effects_so_far, pick)
    if repair is not None and repair != ("block_done",):
        return repair
    if not todo:
        if repair == ("block_done",):
            return None, "pending"
        return (pick("DONE"), "oracle") if "DONE" in ops else (None, "pending")
    eff = todo[0]

    if eff["kind"] == "mkdir":
        field = _find(state, lambda e: e.get("id") == "syn:newfolder:name")
        button = _find(state, lambda e: e.get("id") == "syn:newfolder:create")
        if field is None:
            return None, "pending"
        # The field's value, or -- from a driver that shows it empty after a fill -- the last successful fill of it.
        typed = field.get("current_value") or ""
        last = (state.get("recent_actions") or [{}])[-1]
        if not typed and last.get("action") == field.get("label") and last.get("ok", True) \
                and last.get("kind") in ("fill", "type_text", "TYPE_TEXT"):
            typed = last.get("text") or ""
        if typed != eff["name"]:
            val = next((k for k, v in q.get("type_text_value", {}).get("criteria", {}).items()
                        if v.get("value") == eff["name"]), None)
            if val is None:
                return None, "pending"
            r = pick("TYPE_TEXT", type_text_target=field["index"], type_text_value=val)
            return (r, "oracle") if r else (None, "pending")
        r = pick("CLICK", click_target=button["index"]) if button else None
        return (r, "oracle") if r else (None, "pending")

    if eff["kind"] == "move":
        dd = _find(state, lambda e: e.get("id") == f"syn:move:{eff['file']}")
        if dd is not None:
            opt = _option_for(dd, eff["dest"])
            r = pick("SELECT", select_target=opt) if opt else None
            return (r, "oracle") if r else (None, "pending")
        # Not in this window: go into the next folder on the file's path, if it is shown here.
        parts = eff["src"].split("/")[:-1]
        link = next((_find(state, lambda e, p=p: e.get("role") == "link" and e.get("label") == p) for p in parts
                     if _find(state, lambda e, p=p: e.get("role") == "link" and e.get("label") == p)), None)
        if link is not None:
            r = pick("OPEN", open_target=link["index"])
            return (r, "oracle") if r else (None, "pending")
        return None, "pending"

    if eff["kind"] == "rename":
        row_el = _find(state, lambda e: e.get("role") == "file" and e.get("label") == eff["file"])
        if row_el is not None:
            val = next((k for k, v in q.get("type_text_value", {}).get("criteria", {}).items()
                        if v.get("value") == eff["new"]), None)
            r = pick("RENAME", rename_target=row_el["index"], type_text_value=val) if val else None
            return (r, "oracle") if r else (None, "pending")
        parts = eff["src"].split("/")[:-1]
        for p in parts:
            link = _find(state, lambda e, p=p: e.get("role") == "link" and e.get("label") == p)
            if link is not None:
                r = pick("OPEN", open_target=link["index"])
                return (r, "oracle") if r else (None, "pending")
        return None, "pending"

    if eff["kind"] == "write":
        if title != eff["file"]:
            win = next((k for k, v in q.get("focus_window_target", {}).get("criteria", {}).items()
                        if str(v).split(" (")[0].strip() == eff["file"]), None)
            r = pick("FOCUS_WINDOW", focus_window_target=win) if win else None
            return (r, "oracle") if r else (None, "pending")
        text = doc_texts.get(title, "")
        want = eff["want"]
        if text.rstrip() == want.rstrip():
            save = _find(state, lambda e: e.get("id") == "syn:save")
            r = pick("CLICK", click_target=save["index"]) if save else None
            return (r, "oracle") if r else (None, "pending")
        values = q.get("type_text_value", {}).get("criteria", {})

        def go_read():
            # What is needed is not on offer because it has not been read yet: go to a file the goal reads from --
            # one it names as a source, or failing that any file it names other than the one written.
            named = [m.group(0) for m in S.FILENAME.finditer(task.goal)]
            srcs = list(S.goal_files(task.goal)[1]) + [f for f in named if f != eff["file"]]
            for src in srcs:
                win = next((k for k, v in q.get("focus_window_target", {}).get("criteria", {}).items()
                            if str(v).split(" (")[0].strip() == src), None)
                if win:
                    return pick("FOCUS_WINDOW", focus_window_target=win)
            return None

        needed_offered = any(want.rstrip() in v.get("value", "") or v.get("value", "").rstrip() in want
                             for v in values.values() if len(v.get("value", "")) > 2)
        if not text.strip():
            val = next((k for k, v in values.items() if v.get("value", "").rstrip() == want.rstrip()), None)
            if val is None:
                r = go_read()
                if r:
                    return r, "oracle"
            r = pick("TYPE_TEXT", type_text_target=area["index"], type_text_value=val) if (val and area) else None
            return (r, "oracle") if r else (None, "pending")
        if not needed_offered:
            r = go_read()
            if r:
                return r, "oracle"
        # One REPLACE_TEXT that brings a line to its target, applied exactly as the adapter applies it.
        olds = q.get("replace_from", {}).get("criteria", {})
        want_lines = set(want.splitlines())
        for ok, ov in olds.items():
            for vk, vv in values.items():
                r = _apply_replace(task.goal, text, ov.get("text", ""), vv.get("value", ""))
                if r is None:
                    continue
                gained = len(set(r.splitlines()) & want_lines) - len(set(text.splitlines()) & want_lines)
                if gained > 0 and not (set(r.splitlines()) - want_lines - set(text.splitlines())):
                    res = pick("REPLACE_TEXT", replace_text_target=area["index"], replace_from=ok,
                               type_text_value=vk)
                    return (res, "oracle") if res else (None, "pending")
        return None, "pending"
    return None, "pending"


def sandboxed(row: dict, task) -> bool:
    """Only fixture windows: the focused app is one the task set uses, and every listed window is a fixture's."""
    page = row["state"].get("page") or {}
    url = page.get("url") or ""
    if url and url not in FIXTURE_APPS and not url.startswith("com.apple.finder (no window"):
        return False
    # From suite v7 the driver lists only windows showing folders of the run's own workspace, so any Finder
    # window in a v7 state is inside the sandbox -- including folders the run created itself.
    if url in ("访达", "Finder") and str(row.get("suite_version", "0")).isdigit() and int(row["suite_version"]) >= 7:
        return True
    allowed = {"ws"} | {p.name for p in (REPO / "fixtures" / task.fixture).rglob("*")} if task.fixture else {"ws"}
    wins = (row["questions"].get("focus_window_target") or {}).get("criteria", {})
    # An untitled window carries a title and nothing else (TextEdit's blank document, a ghost window).
    return all(str(t).split(" (")[0].strip() in allowed or str(t).startswith(("window ", "(untitled"))
               for t in wins.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    tasks = {t.id: t for t in load_set(REPO / "tasks", "train")}
    stats, ids = Counter(), set()
    with open(args.out, "w") as fh:
        for line in open(args.inp):
            row = json.loads(line)
            task = tasks.get(row["task_id"])
            if task is None or not row["task_id"].startswith("T-"):
                stats["dropped: not a train task"] += 1
                continue
            if not sandboxed(row, task):
                stats["dropped: non-fixture window"] += 1
                continue
            if Path.home().name in json.dumps({"s": row["state"], "q": row["questions"]}, ensure_ascii=False):
                stats["dropped: account name in state"] += 1    # redaction failed somewhere; never ship it
                continue
            label, source = label_row(row, task)
            row["label"] = label
            row["label_source"] = source
            row["teacher"] = None                 # never a model's answer here; see label / label_source
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats[source] += 1
            ids.add(row["task_id"])
    print(dict(stats))
    print(f"{len(ids)} tasks: {', '.join(sorted(ids))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

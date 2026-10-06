"""Planner adapter for a System One endpoint: typed choices over the element list, no decoding.

Works against any ``/v1/systemone`` server, such as DeskMind Brain's local one, which is wire-compatible with /v1/systemone.
The model never writes an action; it scores options that this code enumerates, and the probabilities it returns are
what make routing possible: a confident answer is executed, an unconfident one can be escalated to a bigger planner
or to a vision model. That is the property a 0.8B is chosen for, not raw accuracy.

    deskmind-hands run --set smoke --adapter systemone --model brain-0.8b --systemone-url http://127.0.0.1:8793

Two calls per step, as the /v1/systemone protocol defines it: which operation, then which element for that operation. Text values
are not a typed choice, so a `type_text` operation takes its value from the quoted string in the task; when the task
carries none, the step is refused rather than guessed, because inventing a value is how an agent types into the
wrong field and reports success.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
import time
import urllib.error
import urllib.request

from ..actions import Action, ActionError, ActionKind, Binding
from ..apps import APPS
from .base import AdapterUnavailable, Proposal, Turn, TurnContext
from ..drivers.base import effect_notes, env_sections, mark_new
from .. import lessons as lessons_store


#: Quote pairs that survive a Chinese keyboard, plus backticks. What sits inside one is almost always the exact
#: string a goal wants typed.
QUOTES = [('"', '"'), ('\u201c', '\u201d'), ('\u300c', '\u300d'), ('\u300e', '\u300f'), ('`', '`'), ("'", "'")]

#: Words a goal uses just before the name it wants.
NAMING = re.compile(r"(?:\u540d\u4e3a|\u53eb|\u547d\u540d\u4e3a|\u6539\u6210|\u6539\u4e3a|\u5199\u5165|\u8f93\u5165|named|called|name it|rename to|set to)\s*[:\uff1a]?\s*"
                    r"([\w\u4e00-\u9fff][\w\u4e00-\u9fff.\-]{0,60})")

#: "change X to Y": Y runs to the next punctuation mark, spaces and colons included -- "Status: final" is one value.
CHANGE_TO = re.compile(r"(?:\u6539\u6210|\u6539\u4e3a|\u53d8\u6210|\u66ff\u6362\u4e3a|\u6362\u6210|\u8bbe\u4e3a|\u8bbe\u7f6e\u4e3a|"
                       r"\u8c03\u5230|\u66f4\u65b0\u4e3a|\u2192|->|\bbecomes\b|\bbecome\b|\bto\b)\s*"
                       r"([^\uff0c\u3002,;\uff1b\n]{1,60}?)(?=\s*(?:[\uff0c\u3002,;\uff1b\n]|\band\b|\.\s|$))")
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w.])")

#: Han characters, the rare ones included (extension A, compatibility, extension B and beyond): a name on a receipt or
#: in a list can be written with one, and a run of common ones only was no candidate for it.
HAN = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f"
FILENAME = re.compile(r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff.\-]*\.[A-Za-z0-9]{1,5}")
#: What a Chinese goal puts right before a file name: the verb or particle ends the clause, the name follows.
FILENAME_LEAD = re.compile(r"(?:\u53e6\u5b58\u4e3a|\u4fdd\u5b58|\u5b58\u4e3a|\u4f9d\u6b21|\u6253\u5f00|\u65b0\u5efa|\u521b\u5efa|"
                           r"\u547d\u540d\u4e3a|\u540d\u4e3a|\u6539\u4e3a|\u6539\u6210|\u5230|\u628a|\u5c06|\u7684|\u5728|\u53eb)")
IDENT = re.compile(r"(?<![A-Za-z])[A-Za-z]{1,6}-\d{1,8}(?!\d)")   # bounded: not "ummary-39" out of summary-39


#: A field template in a goal: `订单号,金额` or 「编号,名称,库存」 -- field names separated by a comma.
TEMPLATE = re.compile(r"[`\u300c]([^`\u300d]*[,\uff0c][^`\u300d]*)[`\u300d]")


def template_fills(goal: str, text: str) -> list[str]:
    """Fill a goal's field template from "field: value" lines on screen, joined with the template's separator.

    A constructed value, but a mechanical one: "write them as `订单号,金额`" with 订单号: A-7781 and 金额: 5620 on
    screen is A-7781,5620, and nothing about it needs a model to compose. Only a template whose every field is
    found produces a candidate."""
    out = []
    for m in TEMPLATE.finditer(goal):
        body = m.group(1)
        sep = "," if "," in body else "\uff0c"
        fields = [f.strip() for f in re.split(r"[,\uff0c]", body) if f.strip()]
        values = []
        for field in fields:
            found = re.search(re.escape(field) + r"\s*[:\uff1a]\s*([^\n]+)", text or "")
            if not found:
                break
            values.append(found.group(1).strip())
        else:
            if values:
                out.append(sep.join(values))
    return out


def table_rows(goal: str, text: str) -> list[str]:
    """Rows of an on-screen table whose header is the goal's template, joined with the template's separator.

    A web table reaches the accessibility tree one cell per line: 编号, 名称, 库存, SKU-4417, 密封垫圈, 238, ...
    Grouped by the header's width after the header itself, that is "SKU-4417,密封垫圈,238" -- the row a goal
    that says "append the rows as 「编号,名称,库存」" wants, mechanically. Grouping stops at the first group whose
    leading cell is not the same kind of thing as the first row's (an identifier, here)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    out = []
    for m in TEMPLATE.finditer(goal):
        sep = "," if "," in m.group(1) else "\uff0c"
        fields = [f.strip() for f in re.split(r"[,\uff0c]", m.group(1)) if f.strip()]
        w = len(fields)
        for i in range(len(lines) - w + 1):
            if lines[i:i + w] != fields:
                continue
            j = i + w
            kind = None
            while j + w <= len(lines):
                row = lines[j:j + w]
                first_is_id = bool(IDENT.fullmatch(row[0]))
                if kind is None:
                    kind = first_is_id
                elif first_is_id != kind:
                    break
                if any(c in fields for c in row):
                    break
                out.append(sep.join(row))
                j += w
            break
    return out


def record_lines(goal: str, text: str) -> list[str]:
    """Lines on screen that already are a record in the goal's template shape: as many cells as the template has
    fields, split on its separator, at least one an identifier -- "张伟,C-2043,880" for "按 `客户,编号,金额` 追加".

    table_rows covers a table read one cell per line; a text file holds its records whole. Offered only their
    identifiers ("C-2043"), a planner copying "张伟的那笔订单" into orders.csv could type the ID and nothing else:
    the row itself was never a choice (G05, suite v22)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    out = []
    for m in TEMPLATE.finditer(goal):
        sep = "," if "," in m.group(1) else "\uff0c"
        fields = [f.strip() for f in re.split(r"[,\uff0c]", m.group(1)) if f.strip()]
        # A file with more columns than the template, and a header naming them: each record is taken in the
        # template's columns, by name. "Date,Customer,Order,Amount,Status" rows for "as `Date,Customer,Order,Amount`"
        # were never a choice, only their identifiers.
        pick = None
        for line in lines:
            if pick is not None:
                cells = [c.strip() for c in line.split(sep)]
                if len(cells) == pick[1] and all(cells) and any(IDENT.fullmatch(c) for c in cells) and len(line) <= 120:
                    row = sep.join(cells[i] for i in pick[0])
                    if row not in out:
                        out.append(row)
                continue
            head = [c.strip().lower() for c in line.split(sep)]
            if len(head) > len(fields) and all(f.lower() in head for f in fields):
                pick = ([head.index(f.lower()) for f in fields], len(head))
        for line in lines:
            cells = [c.strip() for c in line.split(sep)]
            if len(cells) != len(fields) or not all(cells) or cells == fields or len(line) > 120:
                continue
            if any(IDENT.fullmatch(c) for c in cells) and line not in out:
                out.append(line)
    return out


def id_lines(text: str, limit: int = 4) -> list[str]:
    """Whole record-shaped lines (separated cells, one an identifier) on screen, a few: the line an identifier
    was read from, for a goal that says to copy "that record" without a template to match it by."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 120 or line in out or not re.search(r"[,\uff0c\t]", line):
            continue
        if IDENT.search(line) and not IDENT.fullmatch(line):
            out.append(line)
            if len(out) >= limit:
                break
    return out


def _brain_spans(goal: str) -> list[str]:
    """DeskMind Brain's own value candidates (train/text_choices.py), the generator the 4B planner was trained
    with, covering nearly every typed value in its browser runs. Ours were tuned, without meaning to be, on the diag goals' Chinese wording, and a
    generated task that said "Status becomes approved" left the right value out of the options entirely."""
    try:
        from deskmind_brain.train.text_choices import candidates as _c
        return list(_c(goal))
    except Exception:                                        # noqa: BLE001 - optional source of options
        return []


#: Play/search verbs. The one-character ones only where a clause starts (after punctuation, a space, 里/上/用 an app, or
#: at the start): "用听岛放一下…" is the app 听岛, and matching 听 inside it split the goal in the wrong place.
_PLAY_VERBS = (r"(?:播放|放一下|听一下|搜索|搜一下|查找|(?:(?<=^)|(?<=[\s，,。：:里上A-Za-z0-9]))(?:听|搜|放))")


def _works_on_screen(text: str) -> list[tuple[str, str]]:
    """(artist, title) pairs written out in text read on screen: "《灯塔》— 唐川", "“Lanterns” by Mira Stone"."""
    out: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        for m in re.finditer(r"[《“\"]([^》”\"\n]{1,60})[》”\"]\s*(?:[—–-]{1,2}|\bby\b|/)\s*([^\s，,。；;！!？?()（）]"
                             r"[^，,。；;！!？?()（）\n]{0,29})", line):
            t, a = m.group(1).strip(), m.group(2).strip()
            if t and a and (a, t) not in out:
                out.append((a, t))
    return out


def _works(goal: str) -> list[tuple[str, str]]:
    """(artist, title) pairs a goal names: 《…》/“…”/「」 titles with an "X的" before them, "play T by A", "播放A的T"."""
    out: list[tuple[str, str]] = []
    # "用声海听季晚的《白昼》": 听 right after the app named with 用 is the verb (while "用听岛…" is the app 听岛).
    goal = re.sub(r"(用[^\s，,。听]{1,8})听", r"\1 听", goal)
    # Only a goal about playing or searching: 「…」 in a file task names a field or a row ("请把「张伟的那笔订单」…"), and
    # composing an "artist" from it offered '请把表格里三行 编号,名称,库存' as something to type.
    if not re.search(_PLAY_VERBS + r"|\b(?:play|listen|search|find)\b|《", goal, re.I):
        return out
    def put(a: str, t: str) -> None:
        # The title ends where the goal moves on: "顾吟的雨季并播放" is 雨季.
        t = re.sub(r"(?:并|然后|再)\s*(?:播放|放|听|打开).*$", "", t or "")
        a, t = (a or "").strip(" ，,。的"), (t or "").strip(" ，,。《》“”「」\"'")
        if t and len(t) <= 60 and (a, t) not in out:
            out.append((a if 0 < len(a) <= 30 else "", t))
    for m in re.finditer(r"(?:([\w\u4e00-\u9fff·]{1,20})的\s*)?[《“]([^》”]{1,60})[》”]", goal):
        a = m.group(1) or ""
        # "在声海里播放苏晚" -> "苏晚", "用听岛听顾禾屿" -> "顾禾屿": everything up to the last verb goes.
        a = re.sub(r"^.*(?:播放|放一下|听一下|搜索|搜一下|查找|听|搜|放)", "", a)
        put(a, m.group(2))
    m = re.search(r"\b(?:play|listen to|search(?:\s+for)?|find)\s+(.+?)\s+by\s+(.+?)(?:\s+(?:in|on|with|using)\s+.*)?[.!?]?$",
                  goal.strip(), re.I)
    if m:
        put(m.group(2), m.group(1))
    m = re.search(_PLAY_VERBS + r"\s*([\w\u4e00-\u9fff·]{1,40}?的[^，,。！!？?《“「]{1,40})", goal)
    if m:
        # "白鹿乐队的安静的引擎": which 的 separates the artist from the title is not knowable from the words, so
        # every split is offered, the first 的 first (titles contain 的 more often than artists do).
        phrase = m.group(1).strip()
        for k in [i for i, ch in enumerate(phrase) if ch == "的"]:
            if 0 < k < len(phrase) - 1:
                put(phrase[:k], phrase[k + 1:])
    # "find Juniper Hart's Salt and Cedar": the possessive names the artist.
    m = re.search(r"\b(?:play|listen to|search(?:\s+for)?|find)\s+([A-Z][\w .-]{0,30}?)'s\s+(.+?)"
                  r"(?:\s+and\s+(?:then\s+)?(?:play|start|open|listen)\b.*|\s+(?:in|on)\s+[A-Z].*)?[.!?]?$", goal.strip())
    if m:
        put(m.group(1), m.group(2))
    if not out:
        # "搜索 林夏 纸船，并播放": two words after the verb, artist then title, as people type them into a search box.
        m = re.search(r"(?:搜索|搜一下|查找|search(?:\s+for)?|find)\s*[:：]?\s*(\S+)\s+(\S+?)\s*(?:[，,。；;！!？?]|\s+and\b|并|$)",
                      goal, re.I)
        if m and not re.search(r"[，,]", m.group(1) + m.group(2)):
            put(m.group(1), m.group(2))
    return out


#: A goal that fills a form in: its values come from what was read, not from the goal.
FORM_GOAL = re.compile(r"\bfill(?:ing)?\s+(?:in|out)\b|\bform\b|\bexpense\b|\breport\b|填写|填表|填入|表单|报销|录入", re.I)
#: Dates as written on receipts and documents.
DATE = re.compile(r"\b(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})\b|\d{4}年\d{1,2}月\d{1,2}日")


def _date_forms(text: str) -> list[str]:
    """A date in the other common forms: YYYY-MM-DD and MM/DD/YYYY (for 2026-09-27, 09/27/2026, 2026年9月27日)."""
    import datetime as _dt
    m = (re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text) or re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text))
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if not m:
            return []
        mo, d, y = map(int, m.groups())
    try:
        day = _dt.date(y, mo, d)
    except ValueError:
        return []
    return [f for f in (day.isoformat(), day.strftime("%m/%d/%Y")) if f != text]


def ocr_lines(elements) -> str:
    """The words OCR read, put back into lines: words on one visual line, left to right. OCR gives a receipt's
    "BLUE HARBOR CAFE" as three words and "Date of visit 2026-09-27" as four; a value is a line or part of one."""
    words = [e for e in elements if str(getattr(e, "id", "")).startswith("ocr:") and getattr(e, "rect", None)
             and (getattr(e, "label", "") or "").strip()]
    words.sort(key=lambda e: (e.rect.y + e.rect.h / 2, e.rect.x))
    lines: list[list] = []
    rest: list[str] = []
    for e in words:
        # A block the vision reader joined ("LEAF · Court, Westfield · Operated by: …") is placed by its first piece;
        # the pieces joined to it are lines of their own. Kept whole, "GREEN LEAF BISTRO" came out split by them.
        first, *more = [p.strip() for p in e.label.split(" · ") if p.strip()]
        rest += more
        cy = e.rect.y + e.rect.h / 2
        if lines and abs(cy - lines[-1][0]) <= max(4.0, 0.5 * e.rect.h):
            lines[-1][1].append((e.rect.x, first))
        else:
            lines.append([cy, [(e.rect.x, first)]])
    return "\n".join([" ".join(t for _, t in sorted(ws)) for _, ws in lines] + rest)


def window_ping_pong(history) -> bool:
    """The last four successful window switches go A, B, A, B with nothing done in either window between them.

    Switching back and forth with work in between is how a task that reads in one window and acts in another is
    done (read the email, search in the music app, go back to check): counted as the same loop, the guard took
    away the switch the task still needed, and a planner that had read the wrong message could never go back
    (the two-app gym task, 5 of 11 states unlabelled). Refused actions do not count as work."""
    switches: list[tuple[str, int]] = []
    for i, t in enumerate(history):
        if t.action_json.get("kind") == "focus_window" and t.result_ok:
            w = t.action_json.get("text")
            if not switches or switches[-1][0] != w:
                switches.append((w, i))
    last = switches[-4:]
    if len(last) < 4:
        return False
    ids = [w for w, _ in last]
    if not (ids[0] == ids[2] and ids[1] == ids[3] and ids[0] != ids[1]):
        return False
    first, end = last[0][1], last[-1][1]
    worked = any(t.result_ok and t.changed is not False
                 and t.action_json.get("kind") not in ("focus_window", "focus_app", "scroll")
                 for t in history[first + 1:end])
    return not worked


#: Action effects that leave it open whether anything happened (drivers.base.classify_effect).
UNCONFIRMED_EFFECTS = ("unverifiable", "suspected_noop")


def last_effect(history) -> str:
    """The effect of the last action that could change something (not a switch of window or app, not a look)."""
    for t in reversed(history or []):
        kind = (t.action_json or {}).get("kind")
        if kind in ("focus_window", "focus_app", "screenshot", "wait", "ask_user", "request_approval"):
            continue
        return getattr(t, "effect", "") or ""
    return ""


def environment(ctx) -> dict:
    """The planner's environment section (HANDS_ENV_SECTIONS): today's date, and the attached folder with the names of
    its files. Codex keeps the same facts in its <environment_context>; browser-use lists the files it can read every
    step. Before 09-30 the planner was told nothing about the folder, and asked to open a file from it, searched the
    web for its name."""
    import datetime
    out: dict = {"today": datetime.date.today().isoformat()}
    folder = ((getattr(ctx.task, "vars", None) or {}).get("folder"))
    if folder and os.path.isdir(folder):
        names = sorted((n for n in os.listdir(folder) if not n.startswith(".")), key=str.lower)
        out["attached_folder"] = {"name": os.path.basename(folder.rstrip("/")), "files": names[:20],
                                  **({"more_files": len(names) - 20} if len(names) > 20 else {})}
    return out


def replace_edit(goal: str, full: str, old: str, new: str) -> str | None:
    """A field's whole text after a REPLACE_TEXT of `old` with `new`, or None when the pair is not a valid edit of it.

    Both halves are choices -- the old text from the screen, the new from the goal -- and the edit is applied against
    the field's full value rather than the truncated one in the state."""
    if not old or old not in full:
        return None
    # A new value that already holds everything the field holds is the field rewritten, not a piece to splice in.
    # Spliced, the document went inside itself: with report.txt already right and saved, a planner replaced "1,240"
    # with the whole dictated text, and every step after rewrote a longer copy until the budget ran out (G04, on app
    # 0.4.0 and after, deskmind#23). As a rewrite it is the text itself, and when the field already holds it the
    # driver says nothing changed and the value is not offered again.
    if full.strip() and full.strip() in new:
        return new
    # The goal says what to change FROM, word for word. When the chosen span contains that value but is more than
    # it -- the whole line "Budget: 1200" for "Budget from 1200 to 1500" -- only the named value is replaced and the
    # rest of the line stays. Otherwise the label went with it, twice.
    for source in change_sources(goal):
        if source != old and source in old:
            new = old.replace(source, new, 1)
            break
    else:
        # "change the status to final": the chosen span is the whole "status: draft" line and the new value is just
        # "final". The label is not part of what the goal asked to change, so it stays.
        labelled = re.match(r"^(\s*[^:\uff1a\n]{1,40}[:\uff1a]\s*)(\S.*)$", old)
        if labelled and not re.search(r"[:\uff1a]", new):
            new = labelled.group(1) + new
    return None if old == new else full.replace(old, new, 1)


#: A numbered or bulleted step ("1.", "2)", "-", "•", "一、"), and a file operation named in it: a goal's list of
#: things to do, not lines to type (value_candidates).
STEP = re.compile(r"^\s*(?:\d{1,2}[.)、．]|[-•*]|[一二三四五六七八九十]、)\s*")
WRITE_DOWN = re.compile(r"写|抄|记下|输入|填|粘贴|\b(?:write|type|enter|paste|note down|copy these)\b", re.I)
STEP_OP = re.compile(r"把|改名|重命名|移到|移动|放进|放到|新建|创建|删除|删掉|复制|拷贝|打开|关闭|保存|"
                     r"\b(?:rename|move|put|create|make|delete|remove|copy|open|close|save)\b", re.I)


def value_candidates(goal: str, visible_text: str = "", limit: int = 14) -> list[str]:
    """Strings a typed value could be, drawn from the goal and from what is on screen.

    DeskMind Brain measured that 1,586 of 1,604 values their browser agent typed were substrings of the goal, which is
    why the value can be a typed choice instead of a sentence from another model. On a desktop the goal is not
    enough on its own: "write the project number of the line whose status is critical" names nothing -- the answer
    is in the file. So the visible text contributes candidates too, and whatever is quoted or looks like a name
    comes first, because that is what goals actually ask for.
    """
    out: list[str] = []

    def add(value: str, raw: bool = False) -> None:
        if raw:
            if value and value not in out:
                out.append(value)
            return
        # Whitespace only. Stripping the trailing full stop was tidier and wrong: one task exists to check that a
        # dictated line is typed character for character, punctuation included.
        value = value.strip()
        if value and 1 <= len(value) <= 200 and value not in out:
            out.append(value)

    # Content the goal dictates verbatim, after a colon on its own lines. It goes FIRST and as every prefix -- one
    # line, two, three -- because where the dictation ends is not always marked ("...写下：<two lines> 写完保存。"),
    # and ranked after everything else it was cut at the candidate limit: the one value that task needed was
    # never offered.
    dictated, block, colon_line = False, [], ""
    for line in goal.splitlines():
        line = line.strip()
        if dictated and line and len(line) <= 200:
            block.append(line)
        elif block:
            break
        if not dictated and line.endswith(("\uff1a", ":")):
            dictated, colon_line = True, line
    # Except a list of steps: "请依次完成下面几件事：\n1. 把 记录-81.txt 改名为 …" is things to do, not text, and offered
    # as values, one of its lines became a file's new name (deskmind#26). Only when every line is a numbered step
    # naming a file operation, so dictation that names no writing ("回复他：", "Send this message:") and a to-do list
    # written into a file ("写下：\n1. 买牛奶") stay dictated.
    # A list the colon's own line says to write down ("写下：\n1. 打开邮箱\n2. 保存报告") is text even when its items name
    # operations; only that line counts, so a "记录" earlier in the goal does not.
    if block and not WRITE_DOWN.search(colon_line) and all(STEP.match(line) and STEP_OP.search(line) for line in block):
        block = []
    for k in range(len(block), 0, -1):
        add("\n".join(block[:k]) + "\n", raw=True)
    for line in block:
        add(line)
    # The same dictation inline, to the end of its line: "写一行备注，内容是：周报 已核对 NOTE-07". Offered only
    # its pieces ("周报", "NOTE-07"), a planner typed "NOTE-07" -- the line the goal asked for was never a choice.
    for line in goal.splitlines():
        # Not a "Key: value" inside an instruction: "改成 Status: final，" offered "final，", and a router typed the
        # comma into the document (G02 on suite v20). A dictation colon follows words, not a field name.
        m = re.search(r"(?<![A-Za-z0-9_])[\uff1a:]\s*([^\uff1a:\u300c\u300d]{1,200})$", line.strip()) \
            or re.search(r"(?<=[^\x00-\x7f])[\uff1a:]\s*([^\uff1a:\u300c\u300d]{1,200})$", line.strip())
        # Not the tail of a quotation: in "三行“宽窗双列：正常”…" the text after the last colon starts inside one
        # ("正常”。（本题的…"), and it was offered first.
        if m and not re.search(r"[\u201d\u300d\uff09)]", m.group(1)[:12]):
            add(m.group(1))
            add(m.group(1).rstrip("\u3002."))
    # Several quoted lines in a row ("三行“宽窗双列：正常”“窄窗单列：正常”“恢复双列：正常”") are also one block, line
    # by line: offered only one at a time, the planner wrote the first thing on its list and saved, three lines
    # short (a two-document write task).
    for run in re.finditer(r"(?:\u201c[^\u201d\n]{1,60}\u201d[\u3001\uff0c,\u548c\s]*){2,}", goal):
        # Not when the goal says they go to different places: "内容分别只写“登录检查通过”“日志已收集”“缺陷待复现”"
        # is one line per file, and offered as a block, a planner wrote all three into the first (a three-file save task).
        if re.search(r"\u5206\u522b|\u5404\u81ea|\u4f9d\u6b21|respectively", goal[max(0, run.start() - 12):run.start()]):
            continue
        parts = re.findall(r"\u201c([^\u201d\n]{1,60})\u201d", run.group(0))
        add("\n".join(parts) + "\n", raw=True)
    # A name the goal gives something: "a new folder called 草稿", "新文件夹 reports", "命名为 Q3". And, in a goal
    # written in Chinese, any run of Latin letters, which is a name far more often than not: "reports" in
    # "归到一个新文件夹 reports 里" was never offered, and the folder was named "文件归到一个新文件夹".
    # What a goal says to search for, whole: "搜索 林夏的 纸船" is the query, not "林夏的" and "纸船" separately -- a
    # planner offered only the pieces searched for the title alone. The possessive is dropped as a search box wants
    # it ("林夏 纸船").
    for m in re.finditer(r"(?:搜索|搜一下|(?:(?<=^)|(?<=[\s，,。：:里上]))搜|查找|search(?:\s+for)?|look\s+up|find)\s*[:：]?\s*"
                         r"([^，,。；;！!？?\n]{1,60})",
                         goal, re.I):
        # The query ends where the goal moves on: "并/然后/and …", a dash ("… 夜空中最亮的星 — who sings …"), or the
        # question itself ("who/what/which …", "谁/哪") -- typed whole, the dash and the question went into the box.
        # "and" ends the query only when an action follows ("… and play it"): "Salt and Cedar" is a title.
        q = re.sub(r"\s*(?:并|然后|\band\s+(?:then\s+)?(?:play|start|open|listen|tell|show|add|save)\b|and then|"
                   r"[—–]|\s-\s|\b(?:who|what|which|where|when|how)\b|是谁|哪).*$", "",
                   m.group(1), flags=re.I).strip(" 「」“”\"'《》")
        if q:
            add(re.sub(r"(\S)的\s+", r"\1 ", q))
            add(q)
    # A work the goal names and who made it, composed into what one types into a search box: "Play Paper Boats by
    # Sable Fox in Tunebox" offered only 'Play Paper Boats', 'Sable Fox' and 'Tunebox', and "在声海里播放苏晚的《安静的
    # 引擎》" only '在声海里播放苏晚的' -- no query that finds the song. "<artist> <title>" first, then the title alone.
    for artist, title in _works(goal):
        if artist:
            add(f"{artist} {title}")
        add(title)
    # A goal to play or find something it does not name ("play the song Wren recommended in her email"): the work is
    # the one written out in what was read, and the search for it is typed in another window. Offered only the
    # goal's words, a planner searched for the email's subject.
    # After the goal's own: a quoted subject ("the “路上听的” email") reads as a title too. At most four, and none the
    # goal already names.
    if re.search(_PLAY_VERBS + r"|\b(?:play|listen|search|find)\b", goal, re.I):
        for artist, title in [w for w in _works_on_screen(visible_text) if w[1] not in goal][:4]:
            add(f"{artist} {title}")
            add(title)
    for m in NAMED.finditer(goal):
        # Not one character: "文件夹中" is where, not a name, and "中" was offered for a three-file save task.
        if len(m.group(1).rstrip(".,;:")) > 1:
            add(m.group(1).rstrip(".,;:"))
    # File names, early and without the verb in front: in a Chinese goal the name runs straight on from the verb
    # ("以纯文本方式依次保存临时1.txt"), the match took the whole clause, and every planner saved the first file
    # under it (a three-file save task, 9 runs of 9). The part after the last verb or particle is the name; and listed after the
    # quotes, 临时2.txt and 临时3.txt had been cut at the candidate limit.
    for m in FILENAME.finditer(goal):
        name = FILENAME_LEAD.split(m.group(0))[-1]
        if FILENAME.fullmatch(name):
            add(name)
    if len(re.findall(f"[{HAN}]", goal)) > len(re.findall(r"[A-Za-z]", goal)):
        for m in re.finditer(r"(?<![\w.\-])([A-Za-z][A-Za-z0-9_\-]{1,40})(?![\w.\-])", goal):
            add(m.group(1))
    # A filled template goes first and stands in for its own field names: "订单号,金额" is the format, never the
    # value, and offered ahead of the filled line it is the one a chooser would pick.
    fills = template_fills(goal, visible_text) + table_rows(goal, visible_text) + record_lines(goal, visible_text)
    filled_templates = {m.group(1) for m in TEMPLATE.finditer(goal)} if fills else set()
    for filled in fills:
        add(filled)
    for open_q, close_q in QUOTES:
        pattern = re.escape(open_q) + r"([^" + re.escape(close_q) + r"]{1,120})" + re.escape(close_q)
        for m in re.finditer(pattern, goal):
            if m.group(1) not in filled_templates:
                add(m.group(1))
    for m in NAMING.finditer(goal):
        add(m.group(1))
    for v in change_targets(goal):
        add(v)
    for m in FILENAME.finditer(goal):
        add(m.group(0))
    # The lines the goal singles out come first: "the line whose status is 「关键」" -- identifiers on a visible
    # line containing a term the goal quotes are what it is asking for, and in a sixty-line log they were cut off
    # by the candidate limit before anyone could choose them.
    quoted_terms = [m.group(1) for m in re.finditer(r"\u300c([^\u300d]{1,20})\u300d", goal)]
    for line in (visible_text or "").splitlines():
        if any(t in line for t in quoted_terms):
            # The line itself, when the goal names it by how it begins: "以「校验码：」开头的那一行 ... 原样写进" is the
            # whole line. Only its identifiers were offered, and a planner typed "校验码：" -- the quoted term alone.
            if any(line.strip().startswith(t) and line.strip() != t for t in quoted_terms):
                add(line)
            for m in IDENT.finditer(line):
                add(m.group(0))
    # An identifier the goal gives as an example ("形如 P-1047") is the format, not the value.
    examples = {m.group(1) for m in re.finditer(r"(?:\u5f62\u5982|\u4f8b\u5982|\u6bd4\u5982|e\.g\.|such as)\s*([A-Za-z]{1,6}-\d{1,8})", goal)}
    for m in IDENT.finditer(goal):
        if m.group(0) not in examples:
            add(m.group(0))
    for m in NUMBER.finditer(goal):
        add(m.group(0))
    # A form filled in from something read (a receipt, a message): the values are whole lines of it, the value after
    # a label, and dates -- none of them an identifier or a number. Offered only identifiers, a planner filled a
    # receipt's amounts into the merchant and date fields (D5).
    if FORM_GOAL.search(goal):
        for line in (visible_text or "").splitlines():
            line = line.strip()
            for m in DATE.finditer(line):
                add(m.group(0))
                # And as a form usually wants it: a receipt's 09/27/2026 or 2026年9月27日 in a YYYY-MM-DD field.
                for other in _date_forms(m.group(0)):
                    add(other)
            for part in re.split(r"\s+·\s+|\s{3,}", line):   # OCR joins blocks of neighbouring lines with " · "
                kv = re.match(r"^([A-Za-z][A-Za-z .]{1,24}|[" + HAN + r"]{1,8})\s*[:：]\s*(\S.{0,40})$", part.strip())
                if kv:
                    add(kv.group(2).strip())
                # Amounts: a receipt's lines of money, where the total is one of them.
                for m in re.finditer(r"(?<![\d.])\d{1,6}[.,]\d{2}(?![\d])", part):
                    add(m.group(0))
        for line in (visible_text or "").splitlines():
            line = line.strip()
            # A short run of Chinese without digits is a name as it stands: "蓝港咖啡" at the top of a receipt.
            for part in re.split(r"\s+·\s+|\s{3,}", line):
                if 3 <= len(part.strip()) <= 16 and re.fullmatch(f"[{HAN}·&]+", part.strip()):
                    add(part.strip())
            # A heading in capitals, as a name is written: "BLUE HARBOR CAFE" -> "Blue Harbor Cafe". Taken as a run of
            # capitalised words, not a whole line: OCR joined "SWIFT RAIL TICKETS" to the address below it.
            for part in re.split(r"\s+·\s+|\s{3,}", line):
                for m in re.finditer(r"(?<![A-Za-z])[A-Z][A-Z&'.]+(?:\s+(?:[A-Z][A-Z&'.]*|&))+(?![a-z])", part):
                    run = m.group(0).strip()
                    if 3 <= len(run) <= 40 and not re.search(r"\d", run):
                        add(run.title())
    for line in (visible_text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 120:
            continue
        for m in IDENT.finditer(line):
            add(m.group(0))
        for m in FILENAME.finditer(line):
            add(m.group(0))
    # After the identifiers, the lines they were read from (see id_lines): a record copied whole is a choice.
    for line in id_lines(visible_text):
        add(line)
    # A name built from what is on screen and the suffix the goal asks for: "add -ok to clip1 … clip3 (e.g.
    # clip1-ok.txt)" is clip2-ok.txt for the second row, which is not a span of anything; offered only the
    # example, a planner renamed every file to clip1-ok.txt.
    suffixes = [m.group(1) for m in re.finditer(r"(?<![\w])([-_][A-Za-z0-9]{1,8})(?=[\s\uff08(,，。）)]|$)", goal)]
    if suffixes:
        for line in (visible_text or "").splitlines():
            for m in FILENAME.finditer(line):
                stem, ext = m.group(0).rsplit(".", 1)
                for suf in suffixes:
                    if not stem.endswith(suf):
                        add(f"{stem}{suf}.{ext}")
    # DeskMind Brain's generator last: it is broad (every word of the goal), and ahead of the specific candidates it
    # pushed them past the limit.
    for span in _brain_spans(goal):
        if span not in examples:
            add(span)
    return out[:limit]


#: Chords that create an item which is then waiting, invisibly, for its name.
CREATING_CHORDS = {"cmd+shift+n", "cmd+n"}

#: "change it FROM X to Y": X is the old value, word for word.
CHANGE_FROM = re.compile(r"(?:\u4ece|\u7531|\bfrom\b)\s*([^\s\uff0c\u3002,;\uff1b]{1,60})\s*"
                         r"(?:\u6539\u6210|\u6539\u4e3a|\u53d8\u6210|\u6362\u6210|\u8c03\u5230|\bto\b)")


def change_sources(goal: str) -> list[str]:
    """The values a goal says to change things FROM ("从 1200 改成 1500"): the exact text to replace."""
    return [m.group(1).strip() for m in CHANGE_FROM.finditer(goal) if m.group(1).strip()]


def ambiguity_question(goal: str, text: str) -> str | None:
    """A question for the user, built from what is ambiguous, or None when nothing is (see ambiguity)."""
    found = ambiguity(goal, text)
    return found[0] if found else None


def ambiguity(goal: str, text: str) -> tuple[str, list[str]] | None:
    """A question for the user, built from what is ambiguous, and the alternatives it lists -- offered to the user as
    answers to pick from (the app shows them as buttons); None when nothing is ambiguous.

    The goal names something in quotes ("「张伟的那笔订单」") and the screen has it more than once: the longest
    leading piece of the name that occurs on two or more visible lines is what the goal does not pin down, and
    those lines are the alternatives. Asked this way the question needs no model to write it -- the one that used
    to write it was the external text helper, and removing that removed ASK altogether."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for m in re.finditer(r"\u300c([^\u300d]{2,40})\u300d|\u201c([^\u201d]{2,40})\u201d", goal):
        ref = m.group(1) or m.group(2)
        if TEMPLATE.fullmatch("\u300c" + ref + "\u300d"):
            continue
        for n in range(len(ref), 1, -1):
            key = ref[:n]
            # Distinct lines: a window's text is often in it twice (the page text and the text area), and a record
            # listed twice was offered as two choices -- "李娜,R-2291,1340；李娜,R-3307,96；李娜,R-2291,1340；…".
            hits = list(dict.fromkeys(l for l in lines if key in l))
            if len(hits) >= 2:
                alternatives = "；".join(hits[:4])
                return f"「{ref}」在屏幕上不止一处：{alternatives}。应该用哪一个？", hits[:4]
    # Unquoted, as English goals are written: a name the goal gives ("Add Lisa Wong's order") that is on two or more
    # distinct lines of the screen. Only quoted references counted, so "Lisa Wong's order" with two orders on screen
    # never offered asking, and a planner copied the first one (D4en2, 09-30). A name is two or more capitalised
    # words, any run of them in the goal's ("Add Lisa Wong" at the start of a sentence holds "Lisa Wong"); the whole
    # name must be on each line ("Lisa Wang" is not "Lisa Wong"). Records only -- separated cells, one an identifier:
    # a singer's name on every row of a song list is not two answers to "play Paper Boats by Sable Fox".
    lines = [l for l in lines if re.search(r"[,\uff0c\t]", l) and IDENT.search(l)]
    refs = [m.group(1) for m in re.finditer(r"\"([^\"]{2,40})\"", goal)]
    for m in re.finditer(r"(?<![\w'])[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+", goal):
        words = m.group(0).split()
        refs += [" ".join(words[i:j]) for i in range(len(words)) for j in range(len(words), i + 1, -1)]
    for ref in refs:
        hits = list(dict.fromkeys(l for l in lines if re.search(r"(?<!\w)" + re.escape(ref) + r"(?!\w)", l)))
        if len(hits) >= 2:
            return f"\"{ref}\" is on more than one line: {'; '.join(hits[:4])}. Which one should I use?", hits[:4]
    return None


ENGLISH_TAIL = re.compile(r"\s+(?:in|on|for|within|inside|but|not|then|and|so|--|\u2014|-)\b.*$", re.I)


def change_targets(goal: str) -> list[str]:
    """The values a goal says to change things TO ("... 改成 1500", "... becomes approved"): what an edit must
    produce. An English value stops at the next function word -- "change status to approved in the Q2 report --
    not the Q1 one" means "approved", not the rest of the sentence, which one run typed into the document."""
    out = []
    for m in CHANGE_TO.finditer(goal):
        v = ENGLISH_TAIL.sub("", m.group(1)).strip().strip("`'\"")
        if v:
            out.append(v)
    return out


def replace_candidates(text: str, limit: int = 16, exclude: tuple[str, ...] = (),
                       first: tuple[str, ...] = ()) -> list[str]:
    """Pieces of an element's current text that an edit could replace: its short lines, then its numbers.

    A document edit is "turn this into that", and both halves are already on screen or in the goal. The whole
    line comes first because that is what a goal usually means ("change the Status line to Status: final"), and
    the bare number second ("change Budget from 1200 to 1500")."""
    # What the goal names as the old value goes first when it is really there: "Budget from 1200 to 1500" means
    # replace 1200, and offering the whole line first got "Budget: 1200" replaced by a bare "1500".
    out: list[str] = [f for f in first if f and f in (text or "") and f not in exclude]
    for line in (text or "").splitlines():
        line = line.strip()
        if line and len(line) <= 120 and line not in out and line not in exclude:
            out.append(line)
    for m in NUMBER.finditer(text or ""):
        if m.group(0) not in out and m.group(0) not in exclude:
            out.append(m.group(0))
    return out[:limit]


#: Below this, BLOCKED is not accepted as the answer: see propose(). Not tuned on a task: a planner's answers at
#: confidence >= 0.8 are right far more often than below it, and a decision that ends the run is held to the level
#: at which the model is actually reliable.
BLOCKED_MIN_CONFIDENCE = 0.8

#: "values taken FROM notes.md": a file the goal reads from, which nothing may be written into.
SOURCE = re.compile(r"(?:\u53d6\u81ea|\u6765\u81ea|\u6458\u81ea|\u6284\u81ea|\u4ece|\u6839\u636e|\u53c2\u7167|\u5bf9\u7167|\u67e5|"
                    r"from|using|based on|according to|found in|listed in)\s*`?([\w\u4e00-\u9fff.\-]+\.[A-Za-z0-9]{1,5})")


def goal_files(goal: str) -> tuple[str | None, set[str]]:
    """(the file the goal writes into, the files it reads from).

    The destination is the file a writing verb points at ("写进 answer.txt"); failing that, the one file the goal
    names that it does not read from. "row.csv 里需要一行 ..., 值取自 notes.md" has no writing verb, and with no
    destination known the value was typed over notes.md -- the source -- and saved."""
    sources = {m.group(1) for m in SOURCE.finditer(goal)}
    m = DESTINATION.search(goal)
    if m and m.group(1) not in sources:
        return m.group(1), sources
    named = [f.group(0) for f in FILENAME.finditer(goal)]
    rest = list(dict.fromkeys(f for f in named if f not in sources))
    return (rest[0] if sources and len(rest) == 1 else None), sources



def not_to_write(goal: str, title: str, saving: bool = False) -> bool:
    """Whether the window titled `title` is a document this goal must not write in (or save): one it reads from, one
    that is not its destination, or -- when the goal names files at all -- a named-looking document it does not name.

    The last clause came from a run, not a rule: told to "open parts.csv ... append the rows ... then save and close
    it", G18b closed parts.csv, the driver went on to the next TextEdit window -- another task's source.txt, left
    open -- and the planner typed the rows into it and saved it (10-01). The goal had no writing verb pointing at a
    file, so no destination was known and nothing was withheld. A window with no file name (a new, untitled
    document) is not held back: a goal that saves under a new name writes there first. Nor, for `saving`, is any
    window whose title is not a file name: the save button is how an untitled document gets the goal's name."""
    name = (title or "").split(" (")[0].strip()
    if not name or (saving and not FILENAME.fullmatch(name)):
        return False
    dest, sources = goal_files(goal)
    if (dest and dest != name) or name in sources:
        return True
    return not_named(goal, name)


def not_named(goal: str, title: str) -> bool:
    """Whether the window titled `title` is a document the goal, which names files, does not name: the user's own, as
    far as this run is concerned. Not written in, saved or closed -- with parts.csv done and closed, G18b closed the
    user's notes.txt as well (app run, 10-01)."""
    name = (title or "").split(" (")[0].strip()
    named = {f.group(0) for f in FILENAME.finditer(goal)}
    return bool(named) and bool(FILENAME.fullmatch(name)) and name not in named

#: A file type named as a word, not as ".ext" ("所有 PDF 文件", "Excel 表格").
TYPE_WORDS = {"pdf": {".pdf"}, "excel": {".xlsx", ".xls"}, "xlsx": {".xlsx"}, "word": {".docx", ".doc"},
              "docx": {".docx"}, "csv": {".csv"}, "png": {".png"}, "jpg": {".jpg", ".jpeg"}, "zip": {".zip"},
              "dmg": {".dmg"}, "txt": {".txt"}, "markdown": {".md"}, "压缩包": {".zip", ".rar", ".7z"},
              "安装包": {".dmg", ".pkg"}, "表格": {".xlsx", ".xls", ".csv"}}
_GENERIC = {"文件", "文件夹", "下载", "新建", "移动", "放进", "原位", "其他", "保持", "重复"}


def relevant_tokens(goal: str, names: list[str]) -> list[str]:
    """Pieces of the goal that pick out some -- not all -- of the files: a bracketed mark ("(1)"), a Latin word,
    or a CJK run of 2-4 characters. What names nothing, or everything, says nothing about which file is meant."""
    if len(names) < 2:
        return []
    cands = set(re.findall(r"[(\uff08][^()\uff08\uff09]{1,6}[)\uff09]", goal))
    cands |= set(re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", goal))
    for run in re.findall(f"[{HAN}]{{2,}}", goal):
        for n in (4, 3, 2):
            cands |= {run[i:i + n] for i in range(len(run) - n + 1)}
    cands = {c.strip() for c in cands} - _GENERIC
    hits = [c for c in cands if 0 < sum(c in f for f in names) < len(names)]
    # Longest first, and a token contained in a longer hit adds nothing.
    hits.sort(key=len, reverse=True)
    return [h for i, h in enumerate(hits) if not any(h in g for g in hits[:i])]


#: Chords the real-desktop driver can carry out in the background, by the app's display name (see propose()).
ROUTED_CHORDS = {
    "文本编辑": {"cmd+s"}, "TextEdit": {"cmd+s"},
    "访达": {"cmd+c", "option+cmd+v", "cmd+up", "cmd+shift+n"},
    "Finder": {"cmd+c", "option+cmd+v", "cmd+up", "cmd+shift+n"},
    "Safari浏览器": set(), "Safari": set(),
}
#: Per-app overrides from the local apps file (`chords:`, by display name); an empty list routes no chord.
ROUTED_CHORDS.update(APPS.chords)

#: Window buttons and list column headers: never a step toward a goal in this task set (see _ranked).
WINDOW_CHROME = {"关闭按钮", "最小化按钮", "全屏幕按钮", "缩放按钮", "close button", "minimize button",
                 "full screen button", "zoom button", "名称", "修改日期", "大小", "种类", "添加日期", "Name",
                 "Date Modified", "Size", "Kind", "Date Added",
                 # History navigation: at a task's first window "back" goes to wherever Finder was before -- outside
                 # the workspace. Folders are entered by their links and left by cmd+up, which stops at the top.
                 "返回", "前进", "返回/前进", "Back", "Forward"}

#: The login name, which Finder shows as the home folder in its sidebar (see SystemOneAdapter._redact).
HOME_NAME = Path.home().name

#: What a goal names something: the word right after "folder called", "文件夹", "命名为" and the like.
NAMED = re.compile(r"(?:called|named|(?:create|make)\s+(?:a\s+)?(?:new\s+)?(?:folder\s+)?(?!a\b|new\b|folder\b)|folder\s+(?!called|named|and\b|to\b|in\b)|\u6587\u4ef6\u5939|\u547d\u540d\u4e3a|\u540d\u4e3a|\u53eb\u505a|\u53eb|\u540d\u5b57\u662f)"
                   r"\s*[\u300c\u201c\"'`]?([\w\u4e00-\u9fff.\-]+)", re.I)

#: Apps a goal may name, by the names a Chinese or English goal uses.
KNOWN_APPS = {"TextEdit": "com.apple.TextEdit", "文本编辑": "com.apple.TextEdit", "Safari": "com.apple.Safari",
              "Finder": "com.apple.finder", "访达": "com.apple.finder", "Notes": "com.apple.Notes",
              "备忘录": "com.apple.Notes", "Preview": "com.apple.Preview", "预览": "com.apple.Preview"}

#: "write it INTO answer.txt": the file a goal names as where things go.
DESTINATION = re.compile(r"(?:\u5199\u8fdb|\u5199\u5165|\u8ffd\u52a0\u8fdb|\u8ffd\u52a0\u5230|\u4fdd\u5b58\u5230|\u586b\u8fdb|(?<![A-Za-z])into\b|(?<![A-Za-z])to\b)\s*"
                         r"(?:\u5df2\u5728\s*\S+\s*\u4e2d\u6253\u5f00\u7684\s*)?([\w\u4e00-\u9fff.\-]+\.[A-Za-z0-9]{1,5})")

MAX_ELEMENTS = 40
#: The most options one choice question may have, and the fewest: the /v1/systemone protocol's bounds, which DeskMind
#: Brain enforces (a request outside them is refused whole, HTTP 400). A Downloads folder with ten subfolders put 38
#: "move to" dropdowns of ten destinations each on screen -- 380 options in one SELECT head -- and the run ended
#: before its first step (first run, 10-05).
MAX_CHOICE_OPTIONS = 255


def fit_choices(questions: dict) -> dict:
    """Every choice question within the protocol's bounds: one with no options is left out (and so is the operation
    it is the target of), one with too many keeps the first MAX_CHOICE_OPTIONS -- options are built in ranked order, so
    those are the likeliest. What the planner is asked otherwise stays as it was."""
    out: dict = {}
    dropped: set[str] = set()
    for key, q in questions.items():
        crit = q.get("criteria") if isinstance(q, dict) else None
        if q.get("type") != "choice" or not isinstance(crit, dict):
            out[key] = q
            continue
        if not crit:
            dropped.add(key)
            continue
        if len(crit) > MAX_CHOICE_OPTIONS:
            q = {**q, "criteria": dict(list(crit.items())[:MAX_CHOICE_OPTIONS])}
        out[key] = q
    op = out.get("operation")
    if op and dropped and isinstance(op.get("criteria"), dict):
        kept = {name: v for name, v in op["criteria"].items() if f"{name.lower()}_target" not in dropped}
        out["operation"] = {**op, "criteria": kept}
    return out


def server_message(exc: Exception) -> str:
    """What a planner server said when it refused a request (the body of an HTTP error), shortened; else nothing.
    Without it a refusal read as an outage: \"HTTP Error 400: Bad Request\", and the reason -- which question was out
    of bounds -- was thrown away."""
    if not isinstance(exc, urllib.error.HTTPError):
        return ""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 -- the error is reported either way
        return ""
    try:
        msg = (json.loads(raw).get("error") or {}).get("message") or raw
    except (ValueError, AttributeError):
        msg = raw
    return " ".join(str(msg).split())[:300]


def request_shape(request: dict | None) -> dict:
    """A request as its questions' kinds and sizes, without the state or the options' text (they carry what was on
    screen): what a failed request leaves in the run's trace."""
    qs = (request or {}).get("questions") or {}
    return {k: {"type": q.get("type"), "options": len(q.get("criteria") or ())} for k, q in qs.items()
            if isinstance(q, dict)}

#: Roles where a double click means "open" rather than "click harder".
OPENABLE_ROLES = {"row", "cell", "item", "listitem", "outline", "link", "axrow", "axcell",
                  # Not image or icon: a document window's title-bar proxy icon is an image, and offered as
                  # something to open it drew the planner away from the text area it had just switched to --
                  # twice, in the two tasks that read in one window and write in another.
                  # The real driver labels Finder rows folder/file. Only a folder is opened in a file-manager task:
                  # opening a file launches another app, which one run did over and over trying to "go into" it.
                  "folder",
                  # A document of the attached folder, offered by the driver to open in the run's own app.
                  "document"}

#: Rows whose writable text is a name.
RENAMEABLE_ROLES = {"folder", "file", "row", "cell"}

#: The instruction text for each question: DeskMind's wording, overridable in ~/.config/deskmind/rules.yaml.
from ..rules import NEXT_ACTION, TARGET, VALUE_CHOICE  # noqa: E402

#: The base rules are a browser's rules. Two desktop facts are absent from them and each cost us a task:
#: an edit in place is not applied until it is confirmed, and replacing a document's text is not appending to it.
DESKTOP_RULES = ("A value typed into a field is not applied until it is confirmed with Return or the dialog's "
                 "confirm button; until then the list still shows the old value, so DONE is premature. "
                 "Before writing into a document, check that the window title is the file the goal names: only "
                 "the focused window is observable, so the text area you can see may belong to the file you were "
                 "told to read and not to the one you were told to write. "
                 "To MOVE a file in a file manager, SELECT the place in its 移动到 dropdown; that one choice moves "
                 "it. To create a folder, type its name into 新建文件夹的名称 and click 新建文件夹. "
                 "In a file manager a row's writable text is the file's NAME: typing into it renames the file, it "
                 "does not navigate and it does not search. To go into a folder use OPEN on it, to go up use "
                 "cmd+up, and to find something read the visible text. "
                 "Answers the user has already given are in user_answers: use them and do not ask again. "
                 "An edit typed into a document exists only in the window until it is saved with cmd+S: the file "
                 "on disk still holds the old text and DONE would throw the work away. Save before finishing. "
                 "An item just created by a command (a new folder, a new file) is waiting for its name with the "
                 "keyboard focus on it, and it is not in the element list while that lasts: use TYPE_FOCUSED to "
                 "name it, then confirm with Return. Creating it a second time is not how you name the first. "
                 "On a desktop many whole commands exist only as a keyboard chord -- a Finder window has no New "
                 "Folder button -- so KEY is a normal way to act, and BLOCKED is only for when no element and no "
                 "chord applies. "
                 "To change one line or one number inside a document use REPLACE_TEXT, once per change; TYPE_TEXT "
                 "puts the chosen value in place of the WHOLE document. "
                 "APPEND_TEXT keeps a document's existing text and adds to it; TYPE_TEXT replaces everything in "
                 "the field. Choose APPEND_TEXT when the goal says to add, append or insert a line.")


#: Keyboard targets offered as a typed choice. Requiring the goal to name the chord was our own bug: a planner did the
#: first three steps of the rename task correctly and then had no way to press Return, so it re-clicked the button
#: until the step budget ran out.
KEY_CHOICES = {
    "return": "Confirm the field being edited, or activate the default button",
    # macOS puts whole commands behind chords and nowhere else: a Finder window has no New Folder button, so a
    # chooser offered only clicks correctly answers BLOCKED. Chords are also locale-stable, unlike the menu item
    # names this machine reports in Chinese.
    "cmd+shift+n": "Create a new folder in the current directory (Finder)",
    "cmd+n": "New window or new document",
    "cmd+f": "Search in the current window",
    "cmd+c": "Copy the selection",
    "option+cmd+v": "Move the copied items into the folder now open (Finder's move; a plain paste would copy)",
    "cmd+v": "Paste",
    "cmd+z": "Undo the last change",
    "cmd+down": "Open the selected item",
    "cmd+up": "Go to the enclosing folder",
    "escape": "Cancel the current field, menu or dialog",
    "tab": "Move focus to the next field",
    "cmd+s": "Save the open document",
    "cmd+a": "Select all in the focused field",
    "delete": "Delete the selection",
}

#: What each operation means, as the planner reads it; the harness maps them onto its own action vocabulary.
OPERATION_LABELS = {
    "CLICK": "Click one element: a button, menu item, tab, list entry or other control. Clicking a file row only "
             "selects it.",
    "OPEN": "Open a row, file, folder or list entry (double click). Selecting a file does not open it, and an "
            "application shows no document until one is opened.",
    "TYPE_TEXT": "Type into an editable field, replacing what it holds. The text itself is chosen by a separate "
                 "question.",
    "KEY": "Press a keyboard shortcut, or Return to confirm a value just typed into a field.",
    "SELECT": "Choose a value in a dropdown or pop-up menu that is on screen.",
    "RENAME": "Rename a file or folder in a file manager: choose its row and the new name.",
    "REPLACE_TEXT": "Change part of a field's existing text: choose what to replace and what to replace it "
                    "with. The way to edit a line or a number inside a document -- TYPE_TEXT would replace the "
                    "whole document with the one value.",
    "APPEND_TEXT": "Add text at the end of a document or field, keeping what is already there.",
    "TYPE_FOCUSED": "Type into whatever is being edited right now, without naming an element. This is the only way "
                    "to name an item that was just created: a folder or file in inline rename mode has keyboard "
                    "focus but is not listed as an element at all.",
    "FOCUS_APP": "Bring an application to the front. Typing only reaches the frontmost app, so a field in a "
                 "background window has to be focused this way first.",
    "SCROLL": "Scroll so that a control or text that is out of view comes into view.",
    "DONE": "The goal is complete, and the screen shows it.",
    "FOCUS_WINDOW": "Switch to another window of this application. Only the focused window's contents are "
                    "observable, so reading one document and writing another means switching between them.",
    "ASK": "Ask the user one question and wait. The right move when the goal is ambiguous -- two records match "
           "what it names, or the value to enter is not determined by anything visible. Guessing is worse than "
           "asking, and asking is not being blocked.",
    "BLOCKED": "Nothing on offer can move the goal forward.",
}


def generic_wanted(name: str, goal: str) -> bool:
    """Is the generic control `name` one this goal could need? Those with no condition always are."""
    from ..vision import GENERIC_NEEDS
    need = GENERIC_NEEDS.get(name)
    return need is None or bool(re.search(need, goal or "", re.I))


def answer_candidates(lines: list[str], limit: int = 120) -> list[str]:
    """What an ANSWER may be: each line on screen, each part of it (" · "), and the value after a label.

    A line is often several fields: a list item read from the screen ("歌手：逃跑计划 · 单曲：65"), or a
    "label：value" pair. Offered only whole, the planner answered "歌手：逃跑计划 · 单曲：65 粉丝：125.4万" to
    "which singer"."""
    parts = []
    for ln in lines:
        if not ln:
            continue
        parts.append(ln)
        for p in (x.strip() for x in ln.split(" · ")):
            parts.append(p)
            m = re.match(r"^[^：:]{1,8}[：:]\s*(.+)$", p)
            if m:
                parts.append(m.group(1).strip())
    return list(dict.fromkeys(p for p in parts if 0 < len(p) <= 60))[:limit]


def top_operations(answers: dict, k: int = 3) -> list[list]:
    """The k likeliest operations with their probabilities, rounded: the decision panel's bars."""
    probs = (answers.get("operation") or {}).get("probabilities") or {}
    return [[op, round(float(p), 2)] for op, p in sorted(probs.items(), key=lambda kv: -float(kv[1]))[:k]]


def position(key, n: int) -> int | None:
    """The 0-based candidate a 1-based position key names: exactly "1".."n". Not int(key) - 1, which reads "0" as
    the last candidate and "-1" as the one before it, and takes "01" and "+1" as the first (protocol review 10-06)."""
    if not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*", key) or not int(key) <= n:
        return None   # isdigit() would take "١" (Arabic-Indic one) and "²", which int() reads or chokes on
    return int(key) - 1


def unoffered(asked: dict, answers: dict) -> str | None:
    """Why a reply is not about what was asked, or None. Every probability must be for an option the question
    offered, and a finite number from 0 to 1; a choice must be offered too. hands acts on the probabilities, so a
    reply whose choice is fine but whose mass sits on an option nobody offered is not fine."""
    for qid, q in asked.items():
        a = answers.get(qid)
        if a is None or not isinstance(q, dict) or q.get("type") != "choice":
            continue
        offered = set(q.get("criteria") or ())
        if not isinstance(a, dict):
            return f"{qid}: the answer is not an object"
        probs = a.get("probabilities") or {}
        if not isinstance(probs, dict):
            return f"{qid}: probabilities are not an object"
        for key, p in probs.items():
            if key not in offered:
                return f"{qid}: a probability for {key!r}, which was not offered"
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
                return f"{qid}: probability {p!r} for {key!r}"
        if "choice" in a and a["choice"] not in offered:
            return f"{qid}: chose {a['choice']!r}, which was not offered"
    return None


class SystemOneAdapter:
    """Typed-choice planner. Text channel only: it never receives the screenshot."""

    name = "systemone"

    def __init__(self, model: str = "brain-4b", *, url: str = "http://127.0.0.1:8793",
                 api_key: str | None = None, timeout: float = 60, text_helper: str | None = None) -> None:
        self.model, self.url, self.api_key, self.timeout = model, url.rstrip("/"), api_key, timeout
        #: A typed choice cannot produce a string, so the value to type comes from a small text model, as other
        #: /v1/systemone clients do. Without one this adapter refuses to type, which costs it every task that needs input.
        self.text_helper = text_helper
        self._labels: dict[str, str] = {}
        self._value_cache: dict[tuple, str] = {}
        self._seen_text: dict[str, str] = {}
        self._read_by_window: dict[str, dict[str, str]] = {}
        #: What an earlier phase of the same task read somewhere this adapter cannot see -- a web page read through
        #: the browser channel before the desktop phase writes it into a document (HANDS_PRIOR_READ, a JSON file
        #: of {"source": ..., "lines": [...]}). Shown as read_in_other_windows and offered as values, line by line
        #: and as a block: a remembered title is a whole line, which the value candidates never cut from text.
        self._prior: list[dict] = []
        prior_path = os.environ.get("HANDS_PRIOR_READ")
        if prior_path and os.path.exists(prior_path):
            try:
                self._prior = json.load(open(prior_path, encoding="utf-8"))
            except ValueError:
                self._prior = []
        self._helper_error: str | None = None
        self._usage = {"requests": 0, "escalations": 0, "min_confidence": 1.0, "slowest_turn_s": 0.0}
        #: Was DONE the planner's first choice on the previous turn (see propose()).
        self._done_was_top = False
        self._moved: set[str] = set()
        self._folders_made: set[str] = set()
        #: element id -> the content it had when a write to it was refused as changing nothing.
        self._written_out: dict[str, tuple[str, str]] = {}   # field -> (its value then, the text refused)
        #: Verified effects of this run ("moved a.log to 'ws' ✓"), shown in the state for the rest of the task.
        self._effects: list[str] = []
        #: What this run has written and saved, for the check before DONE (done_check.py).
        from ..done_check import Ledger
        self._ledger = Ledger()
        self._pending_write: tuple[str, str] | None = None

    # -- wire -------------------------------------------------------------

    def _ask(self, state: dict, questions: dict) -> dict:
        # A choice with one option has one answer. For a model named "decider*" it is filled in here rather than
        # sent: such servers reject a choice of fewer than two options (422). DeskMind Brain's servers answer it
        # trivially and need to see it: their router decides by the chosen chord, and with key_target left out it
        # escalated all 70 cmd+s of a diag round as "risky". SYSTEMONE_LOCAL_SINGLETONS=1 forces the local answer.
        local_only = self.model.lower().startswith("decider") or os.environ.get("SYSTEMONE_LOCAL_SINGLETONS") == "1"
        fixed = {k: q for k, q in questions.items()
                 if local_only and q.get("type") == "choice" and len(q.get("criteria") or {}) == 1}
        asked = fit_choices({k: q for k, q in questions.items() if k not in fixed})
        self.last_request = {"state": state, "model": self.model, "questions": asked}
        body = json.dumps(self.last_request).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.url}/v1/systemone", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.load(r)
                answers = resp["answers"]
                # DeskMind Brain's two-tier router says which model answered ({by: fast|strong, reason, fast_conf}).
                self._routing = resp.get("routing")
        except Exception as exc:
            said = server_message(exc)
            raise AdapterUnavailable(f"system one endpoint {self.url} failed: {exc}" + (f" -- {said}" if said else "")) from exc
        bad = unoffered(asked, answers)
        if bad:
            # Acted on, an answer about an option that was not offered becomes some other option (a key "0" read
            # by position is the last candidate): a reply like that is not one to guess from (protocol review 10-06).
            raise AdapterUnavailable(f"system one endpoint {self.url} answered outside what it was asked: {bad}")
        answers = {k: a for k, a in answers.items() if k in asked}
        for k, q in fixed.items():
            only = next(iter(q["criteria"]))
            answers[k] = {"type": "choice", "choice": only, "confidence": 1.0, "probabilities": {only: 1.0}}
        return answers

    # -- state ------------------------------------------------------------

    @staticmethod
    def _redact(value):
        """The account name out of everything the planner sees. Finder's sidebar lists the home folder by the
        user's login name, and states leave this machine -- to a GPU box for training, to a labelling step. It is
        replaced here, at projection time, so the model sees the same token when it runs as when it learns."""
        name = HOME_NAME
        if not name:
            return value
        if isinstance(value, str):
            if value == name:
                return "个人"
            if name in value:
                value = value.replace(f"/Users/{name}", "~")
                value = re.sub(rf"(?m)^{re.escape(name)}$", "个人", value)
            return value
        if isinstance(value, list):
            return [SystemOneAdapter._redact(v) for v in value]
        if isinstance(value, dict):
            return {k: SystemOneAdapter._redact(v) for k, v in value.items()}
        return value

    def _lessons(self, ctx: TurnContext) -> list[str]:
        """Lessons from earlier runs that apply to this goal (lessons.py), recalled once per goal. They reach the trace
        with the request, so what a run was told is on record."""
        key = ctx.task.goal
        cached = getattr(self, "_recalled", None)
        if cached is None or cached[0] != key:
            apps = set(((getattr(ctx.task, "vars", None) or {}).get("apps") or {}).values())
            apps |= {a for a in (getattr(ctx.task, "app", None), getattr(ctx.observation, "focused_app", None)) if a}
            self._recalled = (key, [l.text for l, _ in lessons_store.recall(key, apps)])
        return self._recalled[1]

    def _state(self, ctx: TurnContext) -> dict:
        return self._redact(self._state_raw(ctx))

    def _state_raw(self, ctx: TurnContext) -> dict:
        """The state shape a /v1/systemone client sends: page, numbered elements with their valid operations, recent actions.

        The per-element ``operations`` list is what stops a chooser from typing into a button: without it the target
        head offers every element for every operation, and every planner tried took that offer."""
        obs = ctx.observation
        for e in obs.elements:                       # ids outlive one observation; labels make the history readable
            if e.label:
                self._labels[e.id] = e.label
        # What was in this same window last time (HANDS_MARK_NEW): anything not in it is new. After a switch of
        # window everything would be, which says nothing, so nothing is marked then.
        window_key = (obs.focused_app, obs.window_title)
        prev_key, prev_seen = getattr(self, "_last_seen", (None, None))
        seen_before = prev_seen if prev_key == window_key else None
        elements = []
        # Dropdown options shown in the state, in all: no more than a target head offers elements. Each file's
        # "move to" list named every subfolder, so a Downloads folder of 150 files and 12 folders put 56,000
        # characters of options in the state. The model's prefill then asked the GPU for 98 GB (first run, 10-05),
        # or on a larger Mac took over a minute. Smaller windows, under 40 options in all, are shown exactly as
        # before.
        options_left = MAX_ELEMENTS
        # When the goal names the files it is about, a file it does not name gets no "move to" dropdown. With the
        # task done, a planner wavering between BLOCKED and moving 2026-01-a.log would have been pushed onto the
        # second -- a file the goal never mentioned. (Moves only: a rename's old name is often unnamed.)
        named_files = {m.group(0) for m in FILENAME.finditer(ctx.task.goal)}
        named_exts = {m.group(1).lower() for m in re.finditer(r"(?<![\w\u4e00-\u9fff])(\.[A-Za-z0-9]{1,5})\b", ctx.task.goal)}
        named_exts |= {x for w, xs in TYPE_WORDS.items()
                       if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", ctx.task.goal, re.I) for x in xs}
        # Words the goal shares with some of the files it could move ("截屏", "副本", "(1)"): a real Downloads folder
        # had 25 files and so 25 move dropdowns, and a planner that did the dev set perfectly lost its way among
        # them. When such words exist, only the files carrying one get a dropdown.
        move_names = [str(e.id)[len("syn:move:"):] for e in obs.elements if str(e.id).startswith("syn:move:")]
        projected_moves = bool(move_names)
        keywords = relevant_tokens(ctx.task.goal, move_names)
        folder_rename_asked = bool(re.search(r"(?:\u6587\u4ef6\u5939|\u76ee\u5f55|folder|directory)[^\u3002.]{0,20}"
                                             r"(?:\u91cd\u547d\u540d|\u6539\u540d|rename)|(?:\u91cd\u547d\u540d|\u6539\u540d|rename)"
                                             r"[^\u3002.]{0,20}(?:\u6587\u4ef6\u5939|\u76ee\u5f55|folder|directory)",
                                             ctx.task.goal, re.I))
        # What this run has already done, from the driver's own reports. Work that succeeded is not offered again:
        # with a sort finished, a low BLOCKED was turned into "open 草稿", and inside it the planner saw three .csv
        # files and a new-folder form, and did the whole task again one level down (草稿/草稿/...).
        if ctx.history:
            last = ctx.history[-1]
            if last.result_ok and re.match(r"(?:moved|created|renamed|saved|removed) ", last.result_detail or ""):
                # Verified effects, carried forward: a moved file vanishes from the window it left, and the one
                # piece of evidence that the goal was met -- "moved ... to 'ws'" -- was only in recent_actions,
                # gone after six turns. a planner reached the goal at step 2, saw nothing to show for it, and renamed
                # another file over the name.
                # A folder created and then named: the effect is the named folder, and the untitled one it was
                # created as is no longer there -- left in, the oracle counted it as a folder nobody asked for.
                m = re.search(r"named from '(.+?)'\)", last.result_detail or "")
                if m:
                    self._effects = [x for x in self._effects if not x.startswith(f"created '{m.group(1)}'")]
                eff = re.sub(r"\s*\((?:projected|scripted)[^)]*\).*$", "", last.result_detail).strip()
                # One effect per file when several moved at once ("moved 'a' to 'keep'; moved 'b' to 'keep'").
                for one in (eff.split(". ")[0].split("; ") if eff.startswith("moved ") else [eff.split(". ")[0]]):
                    one = one.rstrip(".") + " \u2713"
                    # Done again is the latest fact, not a duplicate to drop: move, undo, move again read as undone
                    # while the second move sat deduplicated behind the undo (audit 09-26).
                    if one in self._effects:
                        self._effects.remove(one)
                    self._effects.append(one)
            if last.result_ok and not re.match(r"(?:moved|created|renamed|saved|removed) ", last.result_detail or "") \
                    and (last.action_json or {}).get("kind") in ("type_text", "append_text", "replace_text") \
                    and not str(((last.action_json or {}).get("binding") or {}).get("element_id") or "").startswith("syn:"):
                # A document changed after it was saved is unsaved again. "saved 'f'" stayed in the effects, and a
                # fix typed after the save read as saved: the oracle labelled DONE over a stale file (audit 09-26).
                title = (obs.window_title or "").split(" (")[0].strip()
                self._effects = [x for x in self._effects if not (title and x.startswith(f"saved '{title}'"))]
            if last.result_ok:
                for m in re.finditer(r"moved '(.+?)' to", last.result_detail or ""):
                    # Moved back by an undo: the file is where it started, and may be moved again.
                    (self._moved.discard if "(undo)" in (last.result_detail or "") else self._moved.add)(m.group(1))
                m = re.match(r"removed folder '(.+?)'", last.result_detail or "")
                if m:
                    self._folders_made.discard(m.group(1))
                m = re.match(r"created folder '(.+?)'", last.result_detail or "")
                if m:
                    self._folders_made.add(m.group(1))
            elif "nothing changed" in (last.result_detail or "") and "already" in (last.result_detail or ""):
                # A write refused because the field already holds (or ends with) that text. Remembered against the
                # field's content, not cleared by the next window switch the way the loop's withdrawals are: a
                # finished roundtrip went write, refused, switch window, switch back, write again, eleven times,
                # and never reached the save button beside it.
                # Only that text is refused, not writing: withdrawing TYPE/APPEND/REPLACE from the field left a
                # planner that had re-typed an existing header with no way to add the rows the goal asked for, and it
                # typed them into the save-as name field thirty times instead (G03). The refused text is dropped from
                # the value options while the field still holds what it held then.
                eid = ((last.action_json or {}).get("binding") or {}).get("element_id")
                el = next((x for x in obs.elements if x.id == eid), None)
                if eid and el is not None:
                    self._written_out[eid] = (el.value or "", (last.action_json or {}).get("text") or "")
        names_wanted = set(re.findall(
            r"(?:\u6587\u4ef6\u5939|folders?)\s*(?:called|named)?\s*[\u300c\u201c\"'`]?([\w\u4e00-\u9fff\-]+)",
            ctx.task.goal, re.I))
        folders_wanted = max(1, len(names_wanted))
        # The form goes when the folders the goal names are made, not when as many folders exist: a wrongly named
        # one withdrew it, and the folder that was wanted could no longer be made (audit 09-26).
        folders_done = (len(self._folders_made) >= folders_wanted
                        and (not names_wanted or bool(names_wanted & self._folders_made)))
        for e in self._ranked(obs.elements):
            if not (e.label or e.settable):
                continue
            if str(e.id).startswith("syn:newfolder:") and folders_done:
                continue
            # No save button in a document the goal only reads from: nothing was changed there, and with the writes
            # done the planner saved notes.md -- the source -- instead of row.csv, where the value was.
            if e.id == "syn:save" and not_to_write(ctx.task.goal, obs.window_title or "", saving=True):
                continue
            if e.id == "syn:close" and not_named(ctx.task.goal, obs.window_title or ""):
                continue
            if str(e.id).startswith("syn:move:"):
                fname = str(e.id)[len("syn:move:"):]
                if fname in self._moved:
                    continue
                # "all the .csv files" restricts moves to .csv files just as a named file restricts them to it:
                # offered every file, a planner moved the .md and the .log along with the .csv ones.
                if named_files and fname not in named_files:
                    continue
                if named_exts and not any(fname.lower().endswith(x) for x in named_exts):
                    continue
                if not named_exts and not named_files and keywords and not any(k in fname for k in keywords):
                    continue
            # An element that was already acted on without moving the screen is not offered again. A typed chooser
            # has no way to "try something else" on its own: a planner did the rename task's first three steps correctly
            # and then re-picked the same button until the budget ran out, because it was still the best-looking
            # option every turn. Withdrawing the option is what forces the next one.
            if e.id in ctx.ineffective:
                continue
            # A generic icon-only control (vision mode) the goal has no use for is not offered (vision.GENERIC_NEEDS).
            if str(e.id).startswith("gen:") and not generic_wanted(str(e.id)[4:], ctx.task.goal):
                continue
            ops = ["CLICK"]
            # Double click is a distinct verb on a desktop and the chooser had no way to say it: every task that
            # began "open report.txt" selected the row instead, then typed into an editor with no document.
            role = (e.role or e.ax_role or "").lower()
            # Clicking a file row only selects it. With moves projected as dropdowns, selection serves nothing, and
            # a real Downloads folder put 27 such rows among 40 offered elements: a planner, sure of itself on five-file
            # dev tasks, clicked the search field and double-clicked rows at 0.2-0.3.
            if role == "file" and projected_moves:
                ops = []
            if e.options:
                # A dropdown, in exactly the shape the /v1/systemone state carries one (and DeskMind Brain was trained on): the
                # element carries its options, and each option is targeted as "<element>:<option>".
                ops = ["SELECT"]
            if role in OPENABLE_ROLES:
                ops.append("OPEN")
            # A row whose text can be written is a name, and writing it renames. Said in the goal's own word: with
            # only TYPE_TEXT ("enter text in a field") on offer, the planner had the right row at 0.99 and the
            # right name at 0.74 and still answered BLOCKED, because renaming did not read as typing into a field.
            # A folder is renamed only when the goal says so: told to rename "the .csv file" inside a/b, a planner
            # renamed the subfolder b instead of going into it.
            # "the .log file" limits which file may be renamed as it limits which may be moved: offered every row,
            # a planner renamed the .csv next to it to 清单-final.log.
            wrong_ext = (role == "file" and named_exts and e.label not in named_files
                         and not any((e.label or "").lower().endswith(x) for x in named_exts))
            if wrong_ext:
                pass
            elif e.settable and role in RENAMEABLE_ROLES and (role != "folder" or folder_rename_asked):
                ops.append("RENAME")
            if e.settable and not wrong_ext:   # on a Finder row, typing is renaming too
                ops += ["TYPE_TEXT", "APPEND_TEXT"]
                if e.value and "\n" in e.value:          # a document, not a one-line field
                    ops.append("REPLACE_TEXT")
            # Folders are shown as links: clicking one goes in, which is what a link does on every page the
            # planner has seen, and what the driver does with it.
            if not ops:
                continue                                  # nothing can be done with it: not an option
            shown_role = "link" if role == "folder" else (e.role or e.ax_role)
            idx = str(len(elements) + 1)
            row = {"index": idx, "id": e.id, "role": shown_role, "label": e.label, "operations": ops}
            if e.options and options_left > 0:
                row["options"] = [{"index": f"{idx}:{k + 1}", "label": f"{e.label} → {o}", "value": o}
                                  for k, o in enumerate(e.options[:options_left])]
                options_left -= len(row["options"])
            if e.value:
                row["current_value"] = e.value[:400]
            if e.focused:
                row["focused"] = True
            if mark_new() and seen_before is not None and (e.role, e.label) not in seen_before:
                row["new"] = True
            elements.append(row)
            if len(elements) >= MAX_ELEMENTS:
                break
        apps = {}
        for e in obs.elements:
            if e.app and e.app not in apps:
                apps[e.app] = e.app == obs.focused_app
        self._apps = apps
        self._last_seen = (window_key, {(e.role, e.label) for e in obs.elements})
        return {
            # DeskMind Brain's states carry the page's visible text (300-2500 chars in their own data) and ours sent an
            # empty string, so every task whose answer is *in the content* was unanswerable: find the line whose
            # status is critical, read three rows out of a table. A window's text is what its elements say.
            "page": {"url": obs.focused_app, "title": obs.window_title, "text": self._page_text(obs)},
            "elements": elements,
            "recent_actions": self._with_screen_change(ctx, [self._action_record(t) for t in ctx.history[-6:]]),
            "effects_so_far": self._effects[-12:],
            # A modal alert outranks everything else in the state: while it is up, this application answers
            # nothing else, and it is what the next action has to be about.
            "modal_dialog_open": bool(getattr(obs, "dialog", False)),
            # The user's replies had nowhere to live in this state, so a planner that correctly asked which of two
            # orders was meant asked again, and again, until the dialogue budget ran out with the answer already
            # sitting in the run.
            "user_answers": [{"question": q, "answer": a} for q, a in (ctx.dialogue or [])],
            # What was read in the task's other windows. The value the goal wants is found in one window and
            # written in another, and in the second one the evidence was gone: the planner, looking at an empty
            # answer.txt, reasonably went back to log.txt to look again, eight times. Kept apart from page.text,
            # which stays the window actually shown.
            "read_in_other_windows": self._other_windows(ctx),
            # The last action's effect could not be confirmed: a DONE now is checked again (the router's
            # unverified_last), and the planner is told to look first.
            **({"last_effect": last_effect(ctx.history)} if effect_notes() else {}),
            **({"environment": environment(ctx)} if env_sections() else {}),
            **({"lessons": self._lessons(ctx)} if lessons_store.enabled() else {}),
            **({"progress": p} if (p := self._progress(ctx)) else {}),
            "note": " ".join(x for x in (
                ("A modal alert is open in this application. Nothing else responds until it is dismissed: read it "
                 "and click one of its buttons." if getattr(obs, "dialog", False) else ""),
                ("Your last action could not be confirmed to have taken effect: check on the screen that it did "
                 "before finishing." if effect_notes() and last_effect(ctx.history) in UNCONFIRMED_EFFECTS else ""),
                *(getattr(obs, "notes", None) or []),
                self.done_satisfied(ctx) or "",
                ctx.notice or "") if x),
        }

    #: DeskMind Brain's training histories are dicts, not prose: {action, kind, text, page_changed}, with `kind` drawn
    #: from click / fill / select / wait. We send that shape verbatim so the history channel is in distribution,
    #: and add ok / error for a rejected step -- 108,378 training entries have no field for one, which is the
    #: single biggest gap behind the 0.8B repeating a refused action verbatim.
    KIND_MAP = {"click": "click", "double_click": "open", "right_click": "click", "type_text": "fill",
                "key": "key", "scroll": "scroll", "wait": "wait", "focus_app": "focus_app",
                "focus_window": "focus_app", "done": "done", "give_up": "blocked"}

    #: Roles worth offering first when a real window reports hundreds of elements.
    ROLE_RANK = {"row": 0, "cell": 0, "listitem": 0, "folder": 0, "file": 0, "textfield": 1, "textarea": 1, "searchfield": 1,
                 "button": 2, "link": 2, "menuitem": 2, "checkbox": 2, "radiobutton": 2, "tab": 2}

    def _ranked(self, elements):
        """Order and thin a real window's element list before it is cut to MAX_ELEMENTS.

        The mock desktop reports about a dozen elements, so the first 40 were everything. Finder reports 715, and
        its first 40 were ten copies of an unnamed pager button plus the toolbar -- the file rows the task is about
        never reached the model. Two rules fix that without any app-specific knowledge: a label repeated more than
        three times is window chrome, not a target, so only its first instance is offered; and what the task is
        likely about (focused, writable, rows before buttons) goes first."""
        counts: dict[str, int] = {}
        for e in elements:
            if e.label:
                counts[e.label] = counts.get(e.label, 0) + 1
        seen: set[str] = set()
        kept = []
        for i, e in enumerate(elements):
            # Window chrome that ends or rearranges the task rather than advancing it: a planner clicked a document's
            # close button six times in one collection -- closing the very file it was to read -- and re-sorted
            # Finder's list by its column headers ninety times.
            if (e.role or "").lower() in ("button", "axbutton") and (e.label or "") in WINDOW_CHROME:
                continue
            if e.label and counts[e.label] > 3:
                if e.label in seen:
                    continue
                seen.add(e.label)
            kept.append((i, e))
        # Projected controls first: they are the whole point of projecting, and ranked by role a dropdown came
        # last and was cut at the element limit -- in the one task it was built for, the planner never saw it.
        kept.sort(key=lambda p: (0 if getattr(p[1], "synthetic", False) else 1,
                                 0 if p[1].focused else 1,
                                 0 if p[1].settable else 1,
                                 self.ROLE_RANK.get((p[1].role or p[1].ax_role or "").lower(), 3),
                                 p[0]))
        return [e for _, e in kept]

    @staticmethod
    def _page_text(obs, limit: int = 2500) -> str:
        """The window's visible text, in reading order, deduplicated -- the channel their model was trained on."""
        seen, out, n = set(), [], 0
        for e in obs.elements:
            if (e.id or "").startswith(("gen:", "desc:")):
                continue   # a control that may be there (vision.GENERIC_CONTROLS) is not text on the screen
            # A vision control hands placed itself ("向上滚动页面", "搜索框") has a name, not text on the screen: an
            # ANSWER once chose "向上滚动页面". What it holds (the search box's query) is on the screen.
            pieces = (e.value,) if (e.id or "").startswith("icon:") else (e.label, e.value)
            for piece in pieces:
                piece = (piece or "").strip()
                if not piece or piece in seen:
                    continue
                seen.add(piece)
                out.append(piece)
                n += len(piece) + 1
                if n >= limit:
                    return "\n".join(out)
        return "\n".join(out)

    def _other_windows(self, ctx: TurnContext) -> list[dict]:
        goal = ctx.task.goal
        here = (ctx.observation.window_title or "").strip()
        book = self._read_by_window.setdefault(goal, {})
        text = self._page_text(ctx.observation)
        if here and text:
            book[here] = text
        terms = [m.group(1) for m in re.finditer(r"\u300c([^\u300d]{1,20})\u300d", goal)]
        fields = [f.strip() for m in TEMPLATE.finditer(goal) for f in re.split(r"[,\uff0c]", m.group(1))]
        out = [{"window": p.get("source", "earlier"), "text": "\n".join(p.get("lines") or [])[:800]}
               for p in self._prior]
        for title, body in book.items():
            if title == here:
                continue
            lines = body.splitlines()
            # The lines the goal points at first (a quoted term, a template field); the start of the text after.
            picked = [l for l in lines if any(t and t in l for t in terms + fields)]
            excerpt = "\n".join(picked[:8]) or "\n".join(lines[:12])
            out.append({"window": title, "text": excerpt[:800]})
        return out

    def _with_screen_change(self, ctx: TurnContext, records: list[dict]) -> list[dict]:
        """In a window seen through the screenshot, what the last action changed on screen: the words that appeared
        and the ones that went, with where they are (top / middle / bottom). A planner that clicked "play all" could
        not tell the song had started -- its title at the bottom of the window was one more line among sixty -- and
        kept clicking songs until the run ran out. Nothing here knows an app: it is the difference between two
        looks at the same window."""
        obs = ctx.observation
        texts = {}
        for e in obs.elements:
            if str(e.id).startswith("ocr:") and e.label:
                where = ""
                if e.rect and obs.geometry and obs.geometry.logical.h:
                    y = (e.rect.y + e.rect.h / 2) / obs.geometry.logical.h
                    where = "top" if y < 0.2 else "bottom" if y > 0.8 else "middle"
                texts[e.label] = where
        key = ctx.task.goal
        before = self.__dict__.setdefault("_screen_texts", {}).get(key)
        self._screen_texts[key] = texts
        if not texts or before is None or not records:
            return records
        # OCR reads the same words a little differently from one look to the next ("▶ 播放全部", "▶ 午放全部"):
        # only a line unlike everything on the other screen counts as changed.
        from difflib import SequenceMatcher

        def new_to(t: str, other) -> bool:
            return t not in other and all(SequenceMatcher(None, t, o).ratio() < 0.75 for o in other)
        appeared = [f"{t} ({w})" if w else t for t, w in texts.items() if new_to(t, before)][:5]
        gone = [t for t in before if new_to(t, texts)][:5]
        if appeared or gone:
            records[-1] = {**records[-1], "screen_now_shows": appeared, "screen_no_longer_shows": gone}
        return records

    def _action_record(self, t: Turn) -> dict:
        a = t.action_json or {}
        eid = (a.get("binding") or {}).get("element_id")
        rec = {"action": self._labels.get(eid, eid) or "(no target)",
               "kind": self.KIND_MAP.get(a.get("kind"), a.get("kind")),
               "text": a.get("text") or (("+".join(a.get("keys")) if a.get("keys") else None))}
        if t.changed is not None:
            rec["page_changed"] = t.changed
        if effect_notes() and t.effect in UNCONFIRMED_EFFECTS:
            rec["effect"] = t.effect   # dispatched, but not seen to have done anything
        if not t.result_ok:
            # The environment's own words, unedited: they name the precondition to satisfy.
            rec["ok"] = False
            rec["error"] = (t.result_detail or "")[:120]
        return rec

    @staticmethod
    def _pick(answer: dict) -> tuple[str, float]:
        probs = answer.get("probabilities") or {}
        if probs:
            best = max(probs, key=probs.get)
            return best, float(probs[best])
        return answer.get("choice", ""), float(answer.get("confidence", 0.0))

    # -- main entry point -------------------------------------------------

    def propose(self, ctx: TurnContext) -> Proposal:
        """One request, all heads: the operation and a target head per operation, exactly as the /v1/systemone protocol asks."""
        self._keep_ledger(ctx)
        state = self._state(ctx)
        elements = state["elements"]
        goal = ctx.task.goal
        def head_criteria(op):
            return {e["index"]: {"element": f"[{e['index']}] {e['label']}", "role": e["role"],
                                 "current_value": e.get("current_value", "")}
                    for e in elements if op in e["operations"] or op == "SCROLL"}

        # Typing reaches only the frontmost app, and on the mock desktop as on macOS a click on a background
        # window's element does not make it frontmost. Without this head the chooser had no way to say "switch to
        # the Editor first": it typed into a background field, was told "nothing accepts text input", and went back
        # to clicking rows. The move existed in the action space all along; only the menu was missing.
        apps = dict(getattr(self, "_apps", {}))
        # Elements only ever come from the app being observed, so another app was never an option and a task
        # that reads in Safari and writes in TextEdit answered BLOCKED -- correctly -- at 0.98. The apps the goal
        # names are the other options, by bundle id, which is what a background switch needs.
        for name, bundle in KNOWN_APPS.items():
            if name in goal and bundle not in apps.values() and bundle not in apps:
                apps[bundle] = False
        # A live run names the apps it may use (deskmind-hands do --apps): those are options whatever the goal calls them.
        for bundle in ((getattr(ctx.task, "vars", None) or {}).get("apps") or {}).values():
            if bundle not in apps:
                apps[bundle] = False
        # Not the app being observed: switching to it is the no-op one run chose instead of TextEdit.
        current_app = ctx.observation.focused_app or ""
        current_bundles = {b for n, b in KNOWN_APPS.items() if n and n in current_app}
        app_criteria = {name: "another application named in the goal"
                        for name, front in apps.items()
                        if name not in ctx.ineffective and not front and name != current_app
                        and name not in current_bundles}
        targetable = [op for op in ("CLICK", "OPEN", "RENAME", "TYPE_TEXT", "APPEND_TEXT", "REPLACE_TEXT",
                                    "SCROLL") if head_criteria(op)]
        select_options = {o["index"]: {"element": f"[{o['index']}] {o['label']}", "current_value": e.get("current_value", "")}
                          for e in elements if e.get("options") for o in e["options"]}
        # As many as a target head offers elements: every dropdown's every option was 380 on a Downloads folder with
        # ten subfolders, and even cut to the protocol's 255 the request took over a minute (the timeout) to answer.
        # The first dropdowns' options, in the elements' ranked order; the rest come into reach as those are used.
        select_options = dict(list(select_options.items())[:MAX_ELEMENTS])
        operations = {op: OPERATION_LABELS[op] for op in targetable}
        if select_options:
            operations["SELECT"] = OPERATION_LABELS["SELECT"]
        if len(app_criteria) > 1:
            operations["FOCUS_APP"] = OPERATION_LABELS["FOCUS_APP"]
            questions_extra = {"focus_app_target": {"type": "choice", "criteria": app_criteria,
                                                    "instructions": {"goal": goal, "operation": "FOCUS_APP",
                                                                     "rules": [NEXT_ACTION, DESKTOP_RULES, TARGET]}}}
        else:
            questions_extra = {}
        operations.update(KEY=OPERATION_LABELS["KEY"], DONE=OPERATION_LABELS["DONE"],
                          BLOCKED=OPERATION_LABELS["BLOCKED"])
        # TYPE_FOCUSED exists for one situation: an item just created by a chord is waiting for its name and is
        # not in the element list at all. Offered unconditionally it becomes the easy way to type anything -- it
        # needs no target -- and the planner used it to edit a document whose text area was right there in the
        # list, then hit the one-blind-write-only refusal and looped. Offer it only after the chord that creates.
        last_kind = (ctx.history[-1].action_json.get("kind") if ctx.history else None)
        last_keys = "+".join((ctx.history[-1].action_json.get("keys") or ()) if ctx.history else ())
        if last_kind == "key" and last_keys in CREATING_CHORDS:
            operations["TYPE_FOCUSED"] = OPERATION_LABELS["TYPE_FOCUSED"]
        # Asking is an action, not a failure. Without it the chooser answered BLOCKED on the one task written to
        # test exactly this: two orders match "Zhang Wei's order" and the goal names one.
        seen_text = ((state.get("page") or {}).get("text", "") + "\n" + self._seen_text.get(goal, ""))
        asked = bool(ctx.dialogue)
        if ctx.task.budget.max_dialogue_turns > 0 and not asked and (
                self.text_helper or ambiguity_question(goal, seen_text)):
            operations["ASK"] = OPERATION_LABELS["ASK"]
        # Two documents of the same app are two windows, and only the focused one is observable: the task that
        # reads sixty lines in one file and writes the answer into another had no way to reach the second.
        # Not the window already being observed: switching to it is refused every time, and those refusals were
        # a third of one run's actions -- and they broke up the A-B-A-B pattern below so it never fired.
        # The document the goal writes into is never withdrawn: after enough back-and-forth its window was, and a
        # planner standing in the source document had no way back to it -- it saved the source instead.
        dest_file = goal_files(goal)[0]
        windows = {w.id: (w.title or f"window {w.id}")
                   for w in (ctx.observation.windows or []) if not w.active
                   and (w.id not in ctx.ineffective or (dest_file and (w.title or "").split(" (")[0] == dest_file))}
        # Back and forth between two windows (A, B, A, B): the notice said so and was ignored for eight more
        # switches. From then on the only switch offered is to the document the goal writes into -- and none at
        # all once there, so what remains is to write.
        # The last four switches, whatever came between them: a refused cmd+c every fifth step hid the pattern.
        # Successful switches only, consecutive repeats collapsed: A (refused), B, A, A (refused), B is A-B-A-B.
        if window_ping_pong(ctx.history):
            dest, _ = goal_files(goal)
            here = (ctx.observation.window_title or "").strip()
            windows = {wid: t for wid, t in windows.items()
                       if dest and t.split(" (")[0] == dest and t.split(" (")[0] != here}
        # One other window is enough to switch to. With "more than one", a Finder window whose only other window
        # was the task's TextEdit document offered no way there at all: two planners, 0/6 on a three-file save task, clicked the
        # Finder sidebar and the undo button for 30-45 steps looking for the editor.
        if len(windows) >= 1:
            operations["FOCUS_WINDOW"] = OPERATION_LABELS["FOCUS_WINDOW"]
            questions_extra["focus_window_target"] = {
                "type": "choice", "criteria": windows,
                "instructions": {"goal": goal, "operation": "FOCUS_WINDOW",
                                 "rules": [NEXT_ACTION, DESKTOP_RULES, TARGET]}}
        # With the new-folder form projected, cmd+shift+n is the same capability minus the name: a planner pressed it
        # habitually and left "未命名文件夹" behind -- 146 of one batch's undo labels were cleaning up after it.
        key_choices = dict(KEY_CHOICES)
        if any(e.get("id") == "syn:newfolder:create" for e in elements):
            key_choices.pop("cmd+shift+n", None)
        # Likewise copy-then-paste-move: with a "move to" dropdown on screen it is the same move in three steps, and
        # a planner pressed option+cmd+v with nothing copied instead of clicking the create button it had just filled.
        if any(str(e.get("id", "")).startswith("syn:move:") for e in elements):
            for k in ("cmd+c", "option+cmd+v", "cmd+v"):
                key_choices.pop(k, None)
        # On the real desktop a chord is carried out only where the app has a background route for it; any other
        # is refused, and a refusal the planner cannot learn from is a wasted step. A planner pressed Finder's cmd+up
        # in TextEdit five times running. The mock desktop (Files / Editor) takes every chord and is unaffected.
        routed = ROUTED_CHORDS.get(ctx.observation.focused_app or "")
        if (not getattr(ctx.observation, "accepts_keys", True)
                or any(str(e.id).startswith(("ocr:", "gen:", "icon:", "desc:")) for e in ctx.observation.elements)):
            # An app seen through the screenshot (hands/vision.py): input only reaches it through the foreground
            # flash of a click or a field entry; a bare chord has nowhere to go -- one run pressed Return four
            # times after its search.
            routed = set()
        if routed is not None:
            key_choices = {k: v for k, v in key_choices.items() if k in routed}
        if not key_choices:
            operations.pop("KEY", None)
        questions = {"operation": {"type": "choice", "criteria": operations,
                                   "instructions": {"goal": goal, "rules": [NEXT_ACTION, DESKTOP_RULES]}},
                     "key_target": {"type": "choice", "criteria": key_choices,
                                    "instructions": {"goal": goal, "operation": "KEY",
                                                     "rules": [NEXT_ACTION, DESKTOP_RULES, TARGET]}},
                     **questions_extra}
        if not key_choices:
            questions.pop("key_target", None)
        # A goal that asks for something to be found and told ("…的歌手是谁"): the way to finish is to answer, with
        # text that is on screen. Without it the planner had only DONE, and on the music app's results page, the singer in
        # plain view, it put the goal's completion at 0.01-0.07 and kept clicking until the page was gone.
        answer_field = (getattr(ctx.task, "vars", None) or {}).get("answer_field")
        answer_cands: list[str] = []
        if answer_field:
            lines = [ln.strip() for ln in ((state.get("page") or {}).get("text") or "").splitlines()]
            answer_cands = answer_candidates(lines)
            if answer_cands:
                operations["ANSWER"] = (f"Answer the goal's question ({answer_field}) with text that is on screen "
                                        f"now. This ends the task.")
                questions["answer_value"] = {
                    "type": "choice", "criteria": {str(i + 1): {"value": c} for i, c in enumerate(answer_cands)},
                    "instructions": {"goal": goal, "rules": [
                        f"Choose the on-screen text that is exactly the {answer_field} the goal asks about."]}}
        # What to type is a choice too, not a sentence from another model. Other /v1/systemone clients ask a separate LLM for
        # the string, and that call is the one step nothing in this loop controls: it returned null twice tonight
        # on goals that name the string outright, and each null cost a task. DeskMind Brain measured 1,586 of 1,604 typed
        # values to be substrings of the goal; on a desktop the screen contributes the rest, so both are offered.
        # Values read in one window are typed into another: the project number found in log.txt goes into
        # answer.txt, the order number in source.txt into target.txt. Switching windows replaces the visible text,
        # so what was seen is remembered for the rest of this task -- keyed by goal, since one adapter serves a
        # whole task set.
        current = (state.get("page") or {}).get("text", "")
        # Words read from pixels, as the lines they were on (see ocr_lines).
        lines = ocr_lines(ctx.observation.elements)
        if lines:
            current = current + "\n" + lines
        earlier = self._seen_text.get(goal, "")
        candidates = value_candidates(goal, current + "\n" + earlier, limit=20)
        # Texts a field refused ("already ends with exactly this") while it still holds the same content.
        values_now = {e.id: e.value or "" for e in ctx.observation.elements}
        refused = {t.strip() for eid, (then, t) in self._written_out.items() if values_now.get(eid) == then and t}
        candidates = [c for c in candidates if c.strip() not in refused]
        if self._prior:
            remembered = [ln.strip() for p in self._prior for ln in p.get("lines") or [] if ln.strip()]
            block = "\n".join(remembered)
            candidates = [c for c in [block + "\n"] + remembered if c] + [c for c in candidates if c not in remembered]
            candidates = candidates[:24]
        if current and current not in earlier:
            self._seen_text[goal] = (current + "\n" + earlier)[:12000]
        if candidates:
            # Named and shaped exactly as DeskMind Brain's planner was trained to answer it ("type_text_value", options as
            # {"value": ...}, the /v1/systemone VALUE_CHOICE rules). The candidates are ours: a desktop goal dictates
            # whole lines and says "change X to Y", which browser form-filling never does.
            questions["type_text_value"] = {
                "type": "choice",
                "criteria": {str(i + 1): {"value": c} for i, c in enumerate(candidates)},
                "instructions": {"goal": goal, "rules": VALUE_CHOICE}}
        page_text = (state.get("page") or {}).get("text", "")
        title = (ctx.observation.window_title or "").strip()
        # Any window, not only one whose title looks like a file: the Safari page's own address field took the rows
        # meant for inventory.csv while the rule only watched windows named like documents.
        if not_to_write(goal, title):
            # The goal says where things are written, and this window is another document -- one it is to be read
            # from. Writing here is how log.txt, the file to read, nearly got the answer typed over it; so the ways
            # of writing are not offered in any document but the one named.
            for op in ("REPLACE_TEXT", "TYPE_TEXT", "APPEND_TEXT"):
                if op in targetable:
                    targetable.remove(op)
                operations.pop(op, None)
            # Nor is saving: a document that may not be written is never modified, and saving it is never progress.
            # With writing withheld, cmd+s was the one step that still looked productive -- G05, G11 and G13 saved
            # the unmodified source six times a round, under every model tried (0.8B, 4B, both routers).
            keys = (questions.get("key_target") or {}).get("criteria") or {}
            keys.pop("cmd+s", None)
            if "key_target" in questions and not keys:
                questions.pop("key_target")
                operations.pop("KEY", None)
        targets = change_targets(goal)
        if targets and all(t in page_text for t in targets):
            # Every value the goal asks for is already in the text: the edit is done, and every way of writing
            # more is withdrawn. Withdrawing only REPLACE_TEXT sent the planner to TYPE_TEXT, which put "1500" over
            # the whole document; refusing that one it chose again three times. With nothing left to write, what
            # remains is to save or to finish -- which is what the goal says to do next.
            for op in ("REPLACE_TEXT", "TYPE_TEXT", "APPEND_TEXT"):
                if op in targetable:
                    targetable.remove(op)
                operations.pop(op, None)
        if any("replacing the text would remove" in (t.result_detail or "") for t in ctx.history[-3:]):
            # The driver refused to set the whole text because it would drop lines the goal did not ask to remove
            # (drivers.peekaboo.lines_lost). Offered again, G18b chose it again and then said DONE with nothing
            # written (10-01): adding is what is left, so writing the whole text is withdrawn for now.
            for op in ("TYPE_TEXT", "REPLACE_TEXT"):   # a replace drops the line it replaces: refused the same way
                if op in targetable:
                    targetable.remove(op)
                operations.pop(op, None)
        if "REPLACE_TEXT" in targetable:
            # The values the goal wants to END UP with are not things to replace.
            # From the documents' own text, not the window's: the window's text includes its buttons, and "保存"
            # was offered -- and chosen -- as text to replace.
            doc_text = "\n".join(e.value for e in ctx.observation.elements
                                  if e.settable and e.value and ("\n" in e.value or len(e.value) > 40)) or page_text
            olds = replace_candidates(doc_text, exclude=tuple(targets), first=tuple(change_sources(goal)))
            if olds:
                questions["replace_from"] = {
                    "type": "choice",
                    "criteria": {str(i + 1): {"text": c} for i, c in enumerate(olds)},
                    "instructions": {"goal": goal, "operation": "REPLACE_TEXT: the existing text to change",
                                     "rules": ["Choose the piece of the document's current text that this edit "
                                               "replaces. The new text is chosen separately.", NEXT_ACTION]}}
        else:
            olds = []
        if select_options:
            questions["select_target"] = {"type": "choice", "criteria": select_options,
                                          "instructions": {"goal": goal, "operation": "SELECT",
                                                           "rules": [NEXT_ACTION, DESKTOP_RULES, TARGET]}}
        # Completion check (DeskMind Brain's arm C): one more question after every executed step, "is the goal fully
        # satisfied now?", so that stopping no longer has to win first place in the operation question against every
        # click. Off unless HANDS_COMPLETION_CHECK sets a threshold. It sees exactly what the planner sees -- the
        # goal, the app's state, the action results -- and never the task's grader.
        check_at = float(os.environ.get("HANDS_COMPLETION_CHECK", "0") or 0)
        if check_at > 0 and ctx.history:
            questions["goal_complete"] = {
                "type": "choice",
                "criteria": {"yes": "The user's goal is fully satisfied by the current state: nothing is left to do.",
                             "no": "Something the goal asks for is not done yet, or not saved yet."},
                "instructions": {"goal": goal, "rules": ["Judge only from the current state and the results of the "
                                                         "recent actions.", DESKTOP_RULES]}}
        for op in targetable:
            questions[op.lower() + "_target"] = {
                "type": "choice", "criteria": head_criteria(op),
                "instructions": {"goal": goal, "operation": op, "rules": [NEXT_ACTION, DESKTOP_RULES, TARGET]}}

        t0 = time.perf_counter()
        questions = self._redact(questions)
        answers = self._ask(state, questions)
        self._usage["requests"] += 1
        op, op_conf = self._pick(answers.get("operation", {}))
        confidence = op_conf
        b = Binding(observation_id=ctx.observation.id, app=ctx.observation.focused_app)

        # DONE ends the run as irreversibly as BLOCKED does, and was accepted at 0.38: two documents were written
        # correctly, never saved, and declared finished. Both terminal answers are held to the same bar.
        # ...but DONE held back from a model that keeps choosing it is worse than a premature one: on a finished
        # sort, a planner put DONE first at 0.41, 0.32 and 0.68, and each time the next-best operation it was replaced with
        # re-did finished work (made the folder again, moved a file back out). A DONE that is the first choice at
        # 0.5 or more, or the first choice two turns running, is taken.
        overrode = None
        done_top = op == "DONE"
        persistent_done = done_top and (op_conf >= 0.5 or self._done_was_top)
        self._done_was_top = done_top
        # A router that asked both of its models and got the same terminal answer from each says so
        # (routing.confirmed): two models agreeing is not a coin flip, even when the strong one's own probability is
        # low. Overriding it here once replaced a correct DONE (strong 0.47, fast 0.87) with an undo of finished work.
        confirmed = bool((getattr(self, "_routing", None) or {}).get("confirmed"))
        if op in ("BLOCKED", "DONE") and op_conf < BLOCKED_MIN_CONFIDENCE and not persistent_done and not confirmed:
            # Giving up is the one choice that cannot be taken back, so it is not taken on a coin flip. The best
            # way forward instead, weighed by how sure the target head is of its element: on a real Finder the
            # planner had the right row at 1.00 and the right name at 0.74 and still said BLOCKED at 0.42, because
            # RENAME is a verb it was never trained on and unseen verbs start low.
            probs = (answers.get("operation") or {}).get("probabilities") or {}
            best, best_score = None, 0.0
            for cand, pr in probs.items():
                if cand in ("DONE", "BLOCKED", "ASK"):
                    continue
                head = answers.get(cand.lower() + "_target")
                tconf = self._pick(head)[1] if head else 1.0
                if pr * tconf > best_score:
                    best, best_score = cand, pr * tconf
            # Overriding DONE takes a real alternative: twice a finished task's DONE (0.37, 0.31) was replaced with
            # a 0.13 "open the folder", and the run undid its own work from inside it.
            if best is not None and best_score >= (0.2 if op == "DONE" else 0.1):
                self._usage["escalations"] += 1
                overrode = f"{op}@{op_conf:.2f}"
                op, op_conf = best, float(probs[best])
                confidence = op_conf
        stop_by, p_complete = None, None
        if "goal_complete" in questions:
            p_complete = float(((answers.get("goal_complete") or {}).get("probabilities") or {}).get("yes", 0.0))
        if op == "DONE":
            stop_by = "model"
        elif p_complete is not None and p_complete >= check_at:
            op, op_conf, stop_by = "DONE", p_complete, "check"
            confidence = op_conf
        answer = None
        if op == "ANSWER":
            key, a_conf = self._pick(answers.get("answer_value", {}))
            i = position(key, len(answer_cands))
            if i is None:
                return self._refuse("planner chose an answer that was not offered", answers, t0)
            answer = answer_cands[i]
            confidence = min(op_conf, a_conf)
            stop_by = "answer"
            action = Action(kind=ActionKind.DONE, text=f"answer: {answer}", binding=b, raw=answers)
        elif op in ("DONE", "BLOCKED"):
            action = Action(kind=ActionKind.DONE if op == "DONE" else ActionKind.GIVE_UP,
                            text=f"planner confidence {op_conf:.2f}", binding=b, raw=answers)
        elif op == "FOCUS_APP":
            app, app_conf = self._pick(answers.get("focus_app_target", {}))
            if app not in apps:
                return self._refuse(f"planner chose an app that is not on screen: {app!r}", answers, t0)
            confidence = min(op_conf, app_conf)
            action = Action(kind=ActionKind.FOCUS_APP, text=app, binding=b, raw=answers)
        elif op == "SELECT":
            key, sel_conf = self._pick(answers.get("select_target", {}))
            chosen_el = next((e for e in elements for o in e.get("options") or [] if o["index"] == key), None)
            option = next((o for e in elements for o in e.get("options") or [] if o["index"] == key), None)
            if chosen_el is None or option is None:
                return self._refuse(f"planner chose a dropdown value that was not offered: {key!r}", answers, t0)
            confidence = min(op_conf, sel_conf)
            action = Action(kind=ActionKind.TYPE_TEXT, text=option["value"], clear_first=True,
                            binding=Binding(observation_id=ctx.observation.id, app=ctx.observation.focused_app,
                                            element_id=chosen_el["id"]), raw=answers)
        elif op == "FOCUS_WINDOW":
            wid, win_conf = self._pick(answers.get("focus_window_target", {}))
            if wid not in windows:
                return self._refuse(f"planner chose a window that is not open: {wid!r}", answers, t0)
            confidence = min(op_conf, win_conf)
            action = Action(kind=ActionKind.FOCUS_WINDOW, text=wid, binding=b, raw=answers)
        elif op == "ASK":
            found = ambiguity(goal, (state.get("page") or {}).get("text", "") + "\n" + self._seen_text.get(goal, ""))
            question, options = found if found else (self._ask_text(goal, state), [])
            if question is None:
                return self._refuse(f"ASK without a question: {self._helper_error or 'no text helper'}", answers, t0)
            action = Action(kind=ActionKind.ASK_USER, text=question, options=tuple(options), binding=b, raw=answers)
        elif op == "TYPE_FOCUSED":
            text = self._chosen_value(answers, candidates) or self._field_value(
                goal, {"role": "the item being renamed"}, state)
            if text is None:
                return self._refuse(f"TYPE_FOCUSED without a value: {self._helper_error or 'no text helper'}",
                                    answers, t0)
            action = Action(kind=ActionKind.TYPE_TEXT, text=text, clear_first=True, foreground=True,
                            binding=b, raw=answers)
        elif op == "KEY":
            chord, key_conf = self._pick(answers.get("key_target", {}))
            if chord not in KEY_CHOICES:
                m = re.search(r"\b((?:cmd|ctrl|alt|shift|option)(?:\+\w+)+)\b", goal, re.I)
                if not m:
                    return self._refuse(f"planner chose an unknown chord {chord!r}", answers, t0)
                chord = m.group(1).lower()
            confidence = min(op_conf, key_conf)
            action = Action(kind=ActionKind.KEY, keys=tuple(chord.lower().split("+")), binding=b, raw=answers)
        else:
            index, target_conf = self._pick(answers.get(op.lower() + "_target", {}))
            confidence = min(op_conf, target_conf)
            chosen = next((e for e in elements if e["index"] == index), None)
            if chosen is None:
                return self._refuse(f"planner chose an element that was not offered: {index!r}", answers, t0)
            bound = Binding(observation_id=ctx.observation.id, app=ctx.observation.focused_app,
                            element_id=chosen["id"])
            if op == "REPLACE_TEXT":
                # Both halves are choices -- the old text from the screen, the new from the goal -- and the edit is
                # applied here, against the element's full value rather than the truncated one in the state. A
                # rewrite of the whole document used to need another model to compose it.
                element = next((e for e in ctx.observation.elements if e.id == chosen["id"]), None)
                full = (element.value if element else None) or ""

                # Each head picks its own best option, and the pair can be nonsense as a whole -- "保存" (a button's
                # label) as the text to replace, or a line replaced by itself; three such refusals ended a run. The
                # pairs are tried in order of the two heads' joint probability and the first valid one is made.
                p_old = (answers.get("replace_from") or {}).get("probabilities") or {}
                p_new = (answers.get("type_text_value") or {}).get("probabilities") or {}
                pairs = sorted(((p_old.get(str(i + 1), 0.0) * p_new.get(str(j + 1), 0.0), i, j)
                                for i in range(len(olds)) for j in range(len(candidates))), reverse=True)
                edited = None
                for rank, (_, i, j) in enumerate(pairs):
                    edited = replace_edit(goal, full, olds[i], candidates[j])
                    if edited is not None:
                        if rank:
                            overrode = f"REPLACE pair #{rank + 1} (first valid)"
                        break
                if edited is None:
                    return self._refuse("no offered pair of old text and new value is a valid edit of that field",
                                        answers, t0)
                action = Action(kind=ActionKind.TYPE_TEXT, text=edited, clear_first=True,
                                foreground=True, binding=bound, raw=answers)
            elif op == "OPEN":
                action = Action(kind=ActionKind.DOUBLE_CLICK, binding=bound, raw=answers)
            elif op == "SCROLL":
                action = Action(kind=ActionKind.SCROLL, scroll_dy=3, binding=bound, raw=answers)
            elif op == "RENAME":
                text = self._chosen_value(answers, candidates)
                if text is None:
                    return self._refuse("RENAME without a new name among the options", answers, t0)
                # Never rename one file to the name of another: one this run already moved away (a planner renamed
                # 2026-01-b.log to 2026-02-c.log right after moving 2026-02-c.log out -- the goal met, then
                # destroyed), or one still showing in the window.
                others = {e.label for e in ctx.observation.elements
                          if (e.role or "").lower() in ("file", "folder") and e.id != chosen["id"]}
                if text in self._moved or text in others:
                    return self._refuse(f"{text!r} is the name of another file"
                                        f"{' this run already moved' if text in self._moved else ''}; "
                                        f"renaming over it would destroy it", answers, t0)
                action = Action(kind=ActionKind.TYPE_TEXT, text=text, clear_first=True, foreground=True,
                                binding=bound, raw=answers)
            elif op in ("TYPE_TEXT", "APPEND_TEXT"):
                text = self._chosen_value(answers, candidates)
                if text is None:                      # nothing offered fitted: fall back to the helper
                    text = self._field_value(goal, chosen, state)
                if text is None:
                    return self._refuse(f"TYPE_TEXT without a value: {self._helper_error or 'no text helper'}",
                                        answers, t0)
                element = next((e for e in ctx.observation.elements if e.id == chosen["id"]), None)
                current = (element.value if element else None) or ""
                if op == "TYPE_TEXT" and "\n" in current.strip() and "\n" not in text.strip():
                    # One value written over a document that already has several lines of content is never an
                    # edit, it is a deletion: with REPLACE_TEXT withdrawn because the requested changes were all in
                    # place, the planner reached for this instead and saved a document that said only "1500".
                    return self._refuse(
                        f"that would replace the whole document with {text!r}. The changes the goal asks for are "
                        f"already in it if REPLACE_TEXT is no longer offered -- save it (KEY cmd+s) or finish",
                        answers, t0)
                if op == "APPEND_TEXT" and current.endswith("\n") and not text.endswith("\n"):
                    # A document that ends in a newline is kept in lines: appending a row appends a line, or three
                    # table rows arrive glued together on one.
                    text = text + "\n"
                action = Action(kind=ActionKind.TYPE_TEXT, text=text, clear_first=(op == "TYPE_TEXT"),
                                foreground=True, binding=bound, raw=answers)
            else:
                action = Action(kind=ActionKind.CLICK, binding=bound, raw=answers)

        latency = time.perf_counter() - t0
        self._usage["slowest_turn_s"] = max(self._usage["slowest_turn_s"], latency)
        self._usage["min_confidence"] = min(self._usage["min_confidence"], confidence)
        # The three likeliest operations as the planner weighed them: what a viewer of the run is shown beside the
        # step (the app's decision panel). First in the record, which the trace keeps only 300 characters of.
        top = top_operations(answers)
        eid = str((action.binding.element_id if action.binding else "") or "")
        target = next((e for e in ctx.observation.elements if e.id == eid), None)
        # Only a document's text area is a document written: a browser's address bar is not (G18b typed a table row
        # into Safari's and it counted as an unsaved page), nor is a form's field.
        into_document = target is not None and (
            (target.ax_role or target.role or "").lower() in ("axtextarea", "textarea")
            or "\n" in (target.value or ""))
        self._pending_write = ((ctx.observation.window_title or "").split(" (")[0].strip(), action.text or "") \
            if action.kind is ActionKind.TYPE_TEXT and into_document and not eid.startswith("syn:") else None
        return Proposal(action=action, raw_text=json.dumps({"top": top, "operation": op,
                                                             "confidence": round(confidence, 3),
                                                             **({"overrode": overrode} if overrode else {}),
                                                             **({"stop_by": stop_by} if stop_by else {}),
                                                             **({"answer": answer} if answer else {}),
                                                             **({"routing": self._routing}
                                                                if getattr(self, "_routing", None) else {}),
                                                             **({"p_complete": round(p_complete, 3)}
                                                                if p_complete is not None else {})}),
                        latency_s=latency)

    @staticmethod
    def _chosen_value(answers: dict, candidates: list[str]) -> str | None:
        """The candidate the model picked, or None when it was not asked or picked nothing offered."""
        index, _ = SystemOneAdapter._pick(answers.get("type_text_value", {}))
        i = position(index, len(candidates))
        return None if i is None else candidates[i]

    def _field_value(self, goal: str, element: dict, state: dict) -> str | None:
        """Ask the text helper for the exact string to type. Returns None when it declines or is not configured.

        Cached and retried: one transient failure of this call used to end a run. The refusal it produces counts as
        a parse error, three of those stop the loop, and two real-desktop tasks died that way with the value sitting
        one retry away -- the same string is asked for again every time the field is written."""
        if not self.text_helper:
            return None
        key = (goal, str(element.get("role")), str(element.get("label")))
        if key in self._value_cache:
            return self._value_cache[key]
        try:
            import anthropic
        except ImportError:
            return None
        # The helper used to be handed the goal and a bare role, with app and window read from keys this state
        # does not have, so it answered null on a goal that names the string outright ("create a folder called
        # docs"). It gets the window, the recent actions and the visible text now: the same context the planner
        # has, because it is answering a question about the same screen.
        prompt = ('Return a JSON object with exactly one key, text: the exact string to enter in the selected '
                  'field. Infer it from the goal, the field and what has just happened. No commentary. Return '
                  '{"text": null} ONLY when the goal genuinely does not determine the value; a name stated in the '
                  'goal is determined.\n\n'
                  + json.dumps({"goal": goal, "field": element,
                                "window": (state.get("page") or {}).get("title"),
                                "app": (state.get("page") or {}).get("url"),
                                "recent_actions": state.get("recent_actions"),
                                "visible_text": ((state.get("page") or {}).get("text") or "")[:600]},
                               ensure_ascii=False))
        for attempt in range(3):
            try:
                client = anthropic.Anthropic()
                msg = client.messages.create(model=self.text_helper, max_tokens=400,
                                             messages=[{"role": "user", "content": prompt}])
                raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                # The helper answers in prose often enough: asked for the question to put to the user it replies
                # with the question itself. A missing JSON object used to raise inside the try and come back as
                # "ASK without a question: AttributeError", which is a report about us, not about the model.
                match = re.search(r"\{.*\}", raw, re.S)
                if match:
                    value = json.loads(match.group(0)).get("text")
                else:
                    # A truncated object has no closing brace and json cannot read it, but the string is right
                    # there: a question in Chinese ran past 120 tokens and came back as its own JSON wrapper.
                    loose = re.search(r'"text"\s*:\s*"(.*)', raw, re.S)
                    value = (loose.group(1).rstrip('"} \n') if loose else raw).strip()
                if isinstance(value, str) and value.strip():
                    self._value_cache[key] = value
                    return value
                self._helper_error = "the text helper returned no value"
                return None                      # a decline is an answer; only a failure is worth retrying
            except Exception as exc:
                self._helper_error = f"{type(exc).__name__}: {exc}"[:160]
                time.sleep(0.5 * (attempt + 1))
        return None

    def _ask_text(self, goal: str, state: dict) -> str | None:
        """One question for the user, written by the text helper: a typed choice cannot produce a sentence."""
        if not self.text_helper:
            return None
        return self._field_value(
            "Write the single question to ask the user so this goal stops being ambiguous. Name the specific "
            "alternatives you can see in the state. Goal: " + goal,
            {"role": "a question for the user", "label": "question"}, state)

    def _refuse(self, why: str, raw, t0: float) -> Proposal:
        """No guessing: take a screenshot instead, and let the loop's own limits end the run."""
        self._usage["escalations"] += 1
        return Proposal(action=Action(kind=ActionKind.SCREENSHOT), raw_text=json.dumps(raw)[:400],
                        latency_s=time.perf_counter() - t0, parse_error=why)

    def _keep_ledger(self, ctx: TurnContext) -> None:
        """The last step into the ledger: a write that landed, a save."""
        last = ctx.history[-1] if ctx.history else None
        if last is not None and last.result_ok and self._pending_write:
            self._ledger.wrote(*self._pending_write)
        self._pending_write = None
        if last is not None and last.result_ok:
            for m in re.finditer(r"saved '(.+?)'", last.result_detail or ""):
                self._ledger.was_saved(m.group(1))
            for m in re.finditer(r"closed '(.+?)'", last.result_detail or ""):
                self._ledger.was_closed(m.group(1))

    def _done_inputs(self, ctx: TurnContext) -> tuple[str, list[str], list[str]]:
        goal = ctx.task.goal
        obs = ctx.observation
        seen = self._page_text(obs) + "\n" + self._seen_text.get(goal, "")
        table = list(dict.fromkeys(table_rows(goal, seen) + record_lines(goal, seen)))
        windows = [(w.title or "").split(" (")[0].strip() for w in (obs.windows or [])] + \
            [(obs.window_title or "").split(" (")[0].strip()]
        return goal, [w for w in windows if w], table

    def _progress(self, ctx: TurnContext) -> dict | None:
        """The state's `progress` section (done_check.progress), with HANDS_PROGRESS=1."""
        from ..done_check import progress, progress_enabled
        if not progress_enabled() or not hasattr(self, "_ledger"):
            return None
        goal, _, table = self._done_inputs(ctx)
        return progress(goal, self._ledger, table)

    def done_satisfied(self, ctx: TurnContext) -> str | None:
        """The goal's checkable parts, said once all are done (done_check.satisfied); shown only with the DONE check."""
        from ..done_check import enabled, satisfied
        if not enabled() or not hasattr(self, "_ledger"):
            return None
        goal, windows, table = self._done_inputs(ctx)
        return satisfied(goal, self._ledger, windows, table)

    def done_objection(self, ctx: TurnContext) -> str | None:
        """Why the run may not finish yet (done_check.py), or None. Asked by the loop when the planner says DONE."""
        from ..done_check import objection
        self._keep_ledger(ctx)
        goal, windows, table = self._done_inputs(ctx)
        return objection(goal, self._ledger, windows, table)

    def usage(self) -> dict:
        return dict(self._usage)

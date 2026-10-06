"""Render: the state a /v1/systemone planner is shown, from the observation and what the run remembers (deskmind#58,
step 1).

Three parts, called in this order every turn:

    remember(mem, ctx)        folds the last turn's result and this observation into the run's memory
    state = render(ctx, mem, ...)   the state -- reads, never writes
    remember_shown(mem, ctx)  what this observation showed, for comparing the next one against

`render` is a function of its arguments: the same context and memory give the same state, and nothing is changed by
asking. Everything here was moved from SystemOneAdapter._state_raw without a change to what it builds; the rules'
reasons stay with them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..adapters.base import Turn, TurnContext
from ..drivers.base import effect_notes, env_sections, mark_new


@dataclass
class Memory:
    """What the run remembers between turns, for the state."""

    #: element id -> label: ids outlive one observation; labels make the history readable.
    labels: dict[str, str] = field(default_factory=dict)
    #: Verified effects of this run ("moved a.log to 'ws' ✓"), shown for the rest of the task.
    effects: list[str] = field(default_factory=list)
    moved: set[str] = field(default_factory=set)
    folders_made: set[str] = field(default_factory=set)
    #: element id -> (its value then, the text refused): a write refused as changing nothing.
    written_out: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: goal -> window title -> the text read there.
    read_by_window: dict[str, dict[str, str]] = field(default_factory=dict)
    #: goal -> {OCR line: where on screen}, the last look.
    screen_texts: dict[str, dict[str, str]] = field(default_factory=dict)
    #: ((app, window title), {(role, label)}) of the last observation, for marking what is new (HANDS_MARK_NEW).
    last_seen: tuple | None = None
    #: app -> is it the focused one, from the last observation.
    apps: dict[str, bool] = field(default_factory=dict)


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


def remember(mem: Memory, ctx: TurnContext) -> None:
    """The last turn's verified result, and this observation's labels and text, into the run's memory."""
    obs = ctx.observation
    for e in obs.elements:                       # ids outlive one observation; labels make the history readable
        if e.label:
            mem.labels[e.id] = e.label
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
                mem.effects = [x for x in mem.effects if not x.startswith(f"created '{m.group(1)}'")]
            eff = re.sub(r"\s*\((?:projected|scripted)[^)]*\).*$", "", last.result_detail).strip()
            # One effect per file when several moved at once ("moved 'a' to 'keep'; moved 'b' to 'keep'").
            for one in (eff.split(". ")[0].split("; ") if eff.startswith("moved ") else [eff.split(". ")[0]]):
                one = one.rstrip(".") + " ✓"
                # Done again is the latest fact, not a duplicate to drop: move, undo, move again read as undone
                # while the second move sat deduplicated behind the undo (audit 09-26).
                if one in mem.effects:
                    mem.effects.remove(one)
                mem.effects.append(one)
        if last.result_ok and not re.match(r"(?:moved|created|renamed|saved|removed) ", last.result_detail or "") \
                and (last.action_json or {}).get("kind") in ("type_text", "append_text", "replace_text") \
                and not str(((last.action_json or {}).get("binding") or {}).get("element_id") or "").startswith("syn:"):
            # A document changed after it was saved is unsaved again. "saved 'f'" stayed in the effects, and a
            # fix typed after the save read as saved: the oracle labelled DONE over a stale file (audit 09-26).
            title = (obs.window_title or "").split(" (")[0].strip()
            mem.effects = [x for x in mem.effects if not (title and x.startswith(f"saved '{title}'"))]
        if last.result_ok:
            for m in re.finditer(r"moved '(.+?)' to", last.result_detail or ""):
                # Moved back by an undo: the file is where it started, and may be moved again.
                (mem.moved.discard if "(undo)" in (last.result_detail or "") else mem.moved.add)(m.group(1))
            m = re.match(r"removed folder '(.+?)'", last.result_detail or "")
            if m:
                mem.folders_made.discard(m.group(1))
            m = re.match(r"created folder '(.+?)'", last.result_detail or "")
            if m:
                mem.folders_made.add(m.group(1))
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
                mem.written_out[eid] = (el.value or "", (last.action_json or {}).get("text") or "")
    here = (obs.window_title or "").strip()
    text = page_text(obs)
    if here and text:
        mem.read_by_window.setdefault(ctx.task.goal, {})[here] = text
    else:
        mem.read_by_window.setdefault(ctx.task.goal, {})


def remember_shown(mem: Memory, ctx: TurnContext) -> None:
    """What this observation showed -- its elements, its apps, its OCR lines -- for the next turn to compare with."""
    obs = ctx.observation
    apps = {}
    for e in obs.elements:
        if e.app and e.app not in apps:
            apps[e.app] = e.app == obs.focused_app
    mem.apps = apps
    mem.last_seen = ((obs.focused_app, obs.window_title), {(e.role, e.label) for e in obs.elements})
    mem.screen_texts[ctx.task.goal] = screen_texts(obs)


def render(ctx: TurnContext, mem: Memory, *, prior: list[dict], lessons: list[str] | None, progress: dict | None,
           done_note: str | None) -> dict:
    """The state shape a /v1/systemone client sends: page, numbered elements with their valid operations, recent actions.

    The per-element ``operations`` list is what stops a chooser from typing into a button: without it the target
    head offers every element for every operation, and every planner tried took that offer.

    `lessons` is None when the lessons store is off; `progress` and `done_note` are what done_check says, if anything."""
    from ..adapters.systemone import UNCONFIRMED_EFFECTS, environment, last_effect
    obs = ctx.observation
    return {
        # DeskMind Brain's states carry the page's visible text (300-2500 chars in their own data) and ours sent an
        # empty string, so every task whose answer is *in the content* was unanswerable: find the line whose
        # status is critical, read three rows out of a table. A window's text is what its elements say.
        "page": {"url": obs.focused_app, "title": obs.window_title, "text": page_text(obs)},
        "elements": offered(ctx, mem),
        "recent_actions": with_screen_change(ctx, mem, [action_record(t, mem.labels) for t in ctx.history[-6:]]),
        "effects_so_far": mem.effects[-12:],
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
        "read_in_other_windows": other_windows(ctx, mem, prior),
        # The last action's effect could not be confirmed: a DONE now is checked again (the router's
        # unverified_last), and the planner is told to look first.
        **({"last_effect": last_effect(ctx.history)} if effect_notes() else {}),
        **({"environment": environment(ctx)} if env_sections() else {}),
        **({"lessons": lessons} if lessons is not None else {}),
        **({"progress": progress} if progress else {}),
        "note": " ".join(x for x in (
            ("A modal alert is open in this application. Nothing else responds until it is dismissed: read it "
             "and click one of its buttons." if getattr(obs, "dialog", False) else ""),
            ("Your last action could not be confirmed to have taken effect: check on the screen that it did "
             "before finishing." if effect_notes() and last_effect(ctx.history) in UNCONFIRMED_EFFECTS else ""),
            *(getattr(obs, "notes", None) or []),
            done_note or "",
            ctx.notice or "") if x),
    }


def offered(ctx: TurnContext, mem: Memory) -> list[dict]:
    """The elements offered, numbered, each with the operations that make sense on it."""
    from ..adapters.systemone import (FILENAME, MAX_ELEMENTS, OPENABLE_ROLES, RENAMEABLE_ROLES, TYPE_WORDS,
                                      generic_wanted, not_named, not_to_write, relevant_tokens)
    obs = ctx.observation
    # What was in this same window last time (HANDS_MARK_NEW): anything not in it is new. After a switch of
    # window everything would be, which says nothing, so nothing is marked then.
    window_key = (obs.focused_app, obs.window_title)
    prev_key, prev_seen = mem.last_seen or (None, None)
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
    named_exts = {m.group(1).lower() for m in re.finditer(r"(?<![\w一-鿿])(\.[A-Za-z0-9]{1,5})\b", ctx.task.goal)}
    named_exts |= {x for w, xs in TYPE_WORDS.items()
                   if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", ctx.task.goal, re.I) for x in xs}
    # Words the goal shares with some of the files it could move ("截屏", "副本", "(1)"): a real Downloads folder
    # had 25 files and so 25 move dropdowns, and a planner that did the dev set perfectly lost its way among
    # them. When such words exist, only the files carrying one get a dropdown.
    move_names = [str(e.id)[len("syn:move:"):] for e in obs.elements if str(e.id).startswith("syn:move:")]
    projected_moves = bool(move_names)
    keywords = relevant_tokens(ctx.task.goal, move_names)
    folder_rename_asked = bool(re.search(r"(?:文件夹|目录|folder|directory)[^。.]{0,20}"
                                         r"(?:重命名|改名|rename)|(?:重命名|改名|rename)"
                                         r"[^。.]{0,20}(?:文件夹|目录|folder|directory)",
                                         ctx.task.goal, re.I))
    names_wanted = set(re.findall(
        r"(?:文件夹|folders?)\s*(?:called|named)?\s*[「“\"'`]?([\w一-鿿\-]+)",
        ctx.task.goal, re.I))
    folders_wanted = max(1, len(names_wanted))
    # The form goes when the folders the goal names are made, not when as many folders exist: a wrongly named
    # one withdrew it, and the folder that was wanted could no longer be made (audit 09-26).
    folders_done = (len(mem.folders_made) >= folders_wanted
                    and (not names_wanted or bool(names_wanted & mem.folders_made)))
    for e in ranked(obs.elements):
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
            if fname in mem.moved:
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
    return elements


def ranked(elements):
    """Order and thin a real window's element list before it is cut to MAX_ELEMENTS.

    The mock desktop reports about a dozen elements, so the first 40 were everything. Finder reports 715, and
    its first 40 were ten copies of an unnamed pager button plus the toolbar -- the file rows the task is about
    never reached the model. Two rules fix that without any app-specific knowledge: a label repeated more than
    three times is window chrome, not a target, so only its first instance is offered; and what the task is
    likely about (focused, writable, rows before buttons) goes first."""
    from ..adapters.systemone import WINDOW_CHROME
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
                             ROLE_RANK.get((p[1].role or p[1].ax_role or "").lower(), 3),
                             p[0]))
    return [e for _, e in kept]


def page_text(obs, limit: int = 2500) -> str:
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


def other_windows(ctx: TurnContext, mem: Memory, prior: list[dict]) -> list[dict]:
    """What was read in the task's other windows: the lines the goal points at, else the start of each."""
    from ..adapters.systemone import TEMPLATE
    goal = ctx.task.goal
    here = (ctx.observation.window_title or "").strip()
    book = mem.read_by_window.get(goal, {})
    terms = [m.group(1) for m in re.finditer(r"「([^」]{1,20})」", goal)]
    fields = [f.strip() for m in TEMPLATE.finditer(goal) for f in re.split(r"[,，]", m.group(1))]
    out = [{"window": p.get("source", "earlier"), "text": "\n".join(p.get("lines") or [])[:800]}
           for p in prior]
    for title, body in book.items():
        if title == here:
            continue
        lines = body.splitlines()
        # The lines the goal points at first (a quoted term, a template field); the start of the text after.
        picked = [l for l in lines if any(t and t in l for t in terms + fields)]
        excerpt = "\n".join(picked[:8]) or "\n".join(lines[:12])
        out.append({"window": title, "text": excerpt[:800]})
    return out


def screen_texts(obs) -> dict[str, str]:
    """The OCR lines of a window seen through the screenshot, with where they are (top / middle / bottom)."""
    texts = {}
    for e in obs.elements:
        if str(e.id).startswith("ocr:") and e.label:
            where = ""
            if e.rect and obs.geometry and obs.geometry.logical.h:
                y = (e.rect.y + e.rect.h / 2) / obs.geometry.logical.h
                where = "top" if y < 0.2 else "bottom" if y > 0.8 else "middle"
            texts[e.label] = where
    return texts


def with_screen_change(ctx: TurnContext, mem: Memory, records: list[dict]) -> list[dict]:
    """In a window seen through the screenshot, what the last action changed on screen: the words that appeared
    and the ones that went, with where they are (top / middle / bottom). A planner that clicked "play all" could
    not tell the song had started -- its title at the bottom of the window was one more line among sixty -- and
    kept clicking songs until the run ran out. Nothing here knows an app: it is the difference between two
    looks at the same window."""
    texts = screen_texts(ctx.observation)
    before = mem.screen_texts.get(ctx.task.goal)
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
        records = records[:-1] + [{**records[-1], "screen_now_shows": appeared, "screen_no_longer_shows": gone}]
    return records


def action_record(t: Turn, labels: dict[str, str]) -> dict:
    """One past action in the shape the planner was trained on (see KIND_MAP)."""
    from ..adapters.systemone import UNCONFIRMED_EFFECTS
    a = t.action_json or {}
    eid = (a.get("binding") or {}).get("element_id")
    rec = {"action": labels.get(eid, eid) or "(no target)",
           "kind": KIND_MAP.get(a.get("kind"), a.get("kind")),
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

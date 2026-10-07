"""Ask: the questions put to the planner this turn, from the rendered state (deskmind#58, step 2).

The operation question and a head per operation that needs one -- its target, the value to type, the text to replace,
the app or window to switch to, the answer to give -- with the options each may take. Moved from
SystemOneAdapter.propose without a change to what it builds; the rules' reasons stay with them.

`build` changes nothing. What it reads that the run keeps (the text seen so far in this task, the writes refused as
changing nothing, what an earlier phase read) is passed in, and the one thing it learns -- the text seen now -- is
handed back for the caller to keep.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..adapters.base import TurnContext
from .render import Memory


@dataclass
class Asked:
    """The questions, and what acting on their answers needs to know about the options."""

    questions: dict
    #: app -> is it the focused one: the apps FOCUS_APP may name.
    apps: dict = field(default_factory=dict)
    #: window id -> title: the windows FOCUS_WINDOW may name.
    windows: dict = field(default_factory=dict)
    answer_cands: list[str] = field(default_factory=list)
    #: The values offered to type, in order (type_text_value).
    candidates: list[str] = field(default_factory=list)
    #: The texts offered to replace, in order (replace_from).
    olds: list[str] = field(default_factory=list)
    #: The completion check's threshold (HANDS_COMPLETION_CHECK), 0 when off.
    check_at: float = 0.0
    #: The text seen so far in this task, when this look added to it; None when it did not.
    seen_now: str | None = None


def build(ctx: TurnContext, state: dict, *, mem: Memory, seen: dict[str, str], prior: list[dict],
          text_helper: str | None) -> Asked:
    """The questions for this turn. `seen` is the text seen so far, per goal; `prior` what an earlier phase read."""
    from ..adapters.systemone import (CREATING_CHORDS, DESKTOP_RULES, KEY_CHOICES, KNOWN_APPS, MAX_ELEMENTS,
                                      NEXT_ACTION, OPERATION_LABELS, ROUTED_CHORDS, TARGET, VALUE_CHOICE,
                                      ambiguity_question, answer_candidates, change_sources, change_targets,
                                      goal_files, not_to_write, ocr_lines, replace_candidates, value_candidates,
                                      window_ping_pong)
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
    apps = dict(mem.apps)
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
    seen_text = ((state.get("page") or {}).get("text", "") + "\n" + seen.get(goal, ""))
    asked = ctx.asked > 0   # its own questions; a yes to the harness's approval is not one (G16)
    if ctx.task.budget.max_dialogue_turns > 0 and not asked and (
            text_helper or ambiguity_question(goal, seen_text)):
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
    earlier = seen.get(goal, "")
    candidates = value_candidates(goal, current + "\n" + earlier, limit=20)
    # Texts a field refused ("already ends with exactly this") while it still holds the same content.
    values_now = {e.id: e.value or "" for e in ctx.observation.elements}
    refused = {t.strip() for eid, (then, t) in mem.written_out.items() if values_now.get(eid) == then and t}
    candidates = [c for c in candidates if c.strip() not in refused]
    if prior:
        remembered = [ln.strip() for p in prior for ln in p.get("lines") or [] if ln.strip()]
        block = "\n".join(remembered)
        candidates = [c for c in [block + "\n"] + remembered if c] + [c for c in candidates if c not in remembered]
        candidates = candidates[:24]
    seen_now = (current + "\n" + earlier)[:12000] if current and current not in earlier else None
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

    return Asked(questions=questions, apps=apps, windows=windows, answer_cands=answer_cands, candidates=candidates,
                 olds=olds, check_at=check_at, seen_now=seen_now)

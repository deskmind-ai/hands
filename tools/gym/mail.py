"""The mail app: tasks (seeded), the true-state grader and the oracle.

An inbox whose messages a planner matching words would confuse: the same sender writing about something else, the
same subject from someone else, and a draft of the subject ("Q3 预算" and "Q3 预算（草稿）"). Four kinds of task:

  reply   open the message, Reply, type the body, Send            -- ordering; the typed text is the goal's
  delete  select it, Delete, confirm in the dialog                -- a confirmation step; the dialog's Cancel undoes
                                                                     a Delete pressed on the wrong message
  flag    flag it, and if it is already flagged do nothing        -- the button toggles: pressing it again unflags
  ask     read a time in the message and answer                   -- read-only; nothing may change

The page posts its state (selected, flagged, deleted, replies, the dialog and the compose box); the grader and the
oracle read it.
"""
from __future__ import annotations

import random

from tools.gym import split

#: Bumped when the labels this oracle gives change (see tools/gym/run.py rows).
ORACLE_VERSION = 3

ZH_PEOPLE = ["林晓", "苏然", "沈一舟", "顾青", "程远", "许诺", "陆川", "温言", "叶知秋", "姜禾", "方舟", "白露"]
EN_PEOPLE = ["Mira Lune", "Kai Solen", "Ivy Hart", "Theo Vale", "Rowan Reed", "Lena Marsh", "Ezra Quill",
             "Nora Frost", "Silas Bloom", "Wren Ash"]
ZH_SUBJECTS = ["Q3 预算", "周会纪要", "出差报销", "新版设计稿", "合同续签", "面试安排", "服务器迁移", "年会场地", "客户回访",
               "季度复盘"]
EN_SUBJECTS = ["Q3 budget", "Weekly sync notes", "Travel expenses", "New design mockups", "Contract renewal",
               "Interview schedule", "Server migration", "Offsite venue", "Customer follow-up", "Quarterly review"]
ZH_REPLIES = ["收到，谢谢", "好的，我明天回复你", "没问题，按这个来", "我看一下再说", "已确认"]
EN_REPLIES = ["Got it, thanks", "Sounds good to me", "I will check and get back to you", "Confirmed", "Works for me"]
TIMES = ["09:30", "10:00", "14:00", "15:30", "16:45", "11:15"]
APPS = [("Mailora", "#246"), ("Mailwise", "#2a6"), ("Inkwell", "#555"), ("信鸽", "#a52"), ("邮筒", "#383")]
TEXTS = {
    "zh": {"inbox": "收件箱", "reply": "回复", "delete": "删除", "flag": "加旗标", "unflag": "取消旗标",
           "send": "发送", "cancel": "取消", "confirm": "确认删除", "confirmText": "确定删除这封邮件吗？",
           "body": "回复内容", "draft": "（草稿）", "meeting": "会议时间：%s", "empty": "选择一封邮件"},
    "en": {"inbox": "Inbox", "reply": "Reply", "delete": "Delete", "flag": "Flag", "unflag": "Unflag",
           "send": "Send", "cancel": "Cancel", "confirm": "Delete message", "confirmText": "Delete this message?",
           "body": "Reply text", "draft": " (draft)", "meeting": "Meeting time: %s", "empty": "Select a message"},
}
GOALS = {
    "reply": {"zh": ["在{app}里回复{sender}关于“{subject}”的邮件：“{body}”", "用{app}给{sender}的“{subject}”那封邮件回复“{body}”"],
              "en": ["In {app}, reply to {sender}'s email about \"{subject}\" with \"{body}\"",
                     "Reply \"{body}\" to the \"{subject}\" email from {sender} in {app}"]},
    "delete": {"zh": ["在{app}里删除{sender}发来的“{subject}”邮件", "把{app}里{sender}那封“{subject}”删掉"],
               "en": ["Delete the \"{subject}\" email from {sender} in {app}",
                      "In {app}, delete {sender}'s message about \"{subject}\""]},
    "flag": {"zh": ["在{app}里给{sender}的“{subject}”邮件加旗标", "把{app}里{sender}发的“{subject}”标记旗标"],
             "en": ["Flag the \"{subject}\" email from {sender} in {app}",
                    "In {app}, make sure {sender}'s \"{subject}\" email is flagged"]},
    "ask": {"zh": ["{app}里{sender}关于“{subject}”的邮件，会议定在几点？", "看一下{app}里{sender}的“{subject}”邮件，会议时间是多少？"],
            "en": ["In {app}, what time is the meeting in {sender}'s \"{subject}\" email?",
                   "Check {sender}'s \"{subject}\" email in {app}: what time is the meeting?"]},
}
#: Held out for evaluation only: never in training rows.
HELDOUT_APPS = {"邮筒"}


KINDS = ["reply", "delete", "flag", "ask"]


def _template_ids(lang: str) -> list[str]:
    return [f"mail/{lang}/{k}/{i}" for k in KINDS for i in range(len(GOALS[k][lang]))]


def _pools(heldout: bool) -> dict:
    """The people, subjects and goal templates a task draws from: all, or (training under split 2) without the
    held-out ones -- one template per language, a fifth of the people and subjects."""
    if heldout:
        return {"zh_people": ZH_PEOPLE, "en_people": EN_PEOPLE, "zh_subjects": ZH_SUBJECTS, "en_subjects": EN_SUBJECTS,
                "templates": {(k, lang): list(range(len(GOALS[k][lang]))) for k in KINDS for lang in ("zh", "en")}}
    held = {lang: split.heldout_template("mail", lang, _template_ids(lang)) for lang in ("zh", "en")}
    return {"zh_people": split.train_values("mail", "zh_people", ZH_PEOPLE),
            "en_people": split.train_values("mail", "en_people", EN_PEOPLE),
            "zh_subjects": split.train_values("mail", "zh_subjects", ZH_SUBJECTS),
            "en_subjects": split.train_values("mail", "en_subjects", EN_SUBJECTS),
            "templates": {(k, lang): [i for i in range(len(GOALS[k][lang])) if f"mail/{lang}/{k}/{i}" != held[lang]]
                          for k in KINDS for lang in ("zh", "en")}}


def make_task(seed: int, split_version: int = split.DEFAULT_VERSION) -> dict:
    """The task of `seed`. Split 1 (the default, every row so far): held out by skin only. Split 2: concept-disjoint
    (see tools/gym/split.py)."""
    base = _make(seed, random.Random(seed), _pools(True))
    if split_version == 1:
        return base
    return split.pick(seed, "mail", base["split"], lambda rnd, heldout: _make(seed, rnd, _pools(heldout)),
                      lambda t: t["app"] in HELDOUT_APPS)


def _make(seed: int, rnd: random.Random, pools: dict) -> dict:
    lang = rnd.choice(["zh", "en"])
    app, color = rnd.choice(APPS)
    people, subjects = pools[f"{lang}_people"], pools[f"{lang}_subjects"]
    kind = rnd.choice(KINDS)
    sender, other = rnd.sample(people, 2)
    subject, other_subject = rnd.sample(subjects, 2)
    texts = TEXTS[lang]
    when = rnd.choice(TIMES)

    def msg(frm, subj, time=None):
        t = time or rnd.choice([t for t in TIMES if t != when])
        lines = [("你好，" if lang == "zh" else "Hi, ") + ("关于" + subj + "，" if lang == "zh" else f"about {subj}:"),
                 texts["meeting"] % t,
                 "谢谢" if lang == "zh" else "Thanks"]
        return {"from": frm, "subject": subj, "body": lines, "time": t}

    inbox = [msg(sender, subject, when), msg(sender, other_subject), msg(other, subject),
             msg(rnd.choice([p for p in people if p != sender]), subject + texts["draft"])]
    while len(inbox) < 8:   # fillers, never a second copy of the target
        frm, subj = rnd.choice(people), rnd.choice(subjects)
        if (frm, subj) != (sender, subject):
            inbox.append(msg(frm, subj))
    rnd.shuffle(inbox)
    for i, m in enumerate(inbox):
        m["id"] = f"m{i + 1}"
        m["date"] = f"{rnd.randint(1, 12)}/{rnd.randint(1, 28)}"
    target = next(m for m in inbox if m["from"] == sender and m["subject"] == subject)
    body = rnd.choice(ZH_REPLIES if lang == "zh" else EN_REPLIES)
    already = kind == "flag" and rnd.random() < 0.3
    flagged = [m["id"] for m in inbox if m is not target and rnd.random() < 0.25] + ([target["id"]] if already else [])
    # An index into the templates, drawn as a choice of the template itself was (the same draw), so it is known.
    ti = rnd.choice(pools["templates"][(kind, lang)])
    goal = GOALS[kind][lang][ti].format(app=app, sender=sender, subject=subject, body=body)
    template_id = f"mail/{lang}/{kind}/{ti}"
    return {"seed": seed, "lang": lang, "app": app, "color": color, "goal": goal, "kind": kind,
            "target": {"id": target["id"], "from": sender, "subject": subject}, "body": body, "answer": when,
            "already": already, "split": "heldout" if app in HELDOUT_APPS else "train",
            "split_version": 1, "template_id": template_id,
            "template_heldout": template_id == split.heldout_template("mail", lang, _template_ids(lang)),
            "concepts": [split.concept("sender", "mail", f"{lang}_people", sender,
                                       ZH_PEOPLE if lang == "zh" else EN_PEOPLE),
                         split.concept("subject", "mail", f"{lang}_subjects", subject,
                                       ZH_SUBJECTS if lang == "zh" else EN_SUBJECTS)],
            "page": {"app": app, "color": color, "texts": texts, "inbox": inbox, "flagged": flagged}}


def passed(task: dict, state: dict, answer: str | None = None) -> bool:
    tid, kind = task["target"]["id"], task["kind"]
    replies = state.get("replies") or []
    deleted = set(state.get("deleted") or [])
    flagged = set(state.get("flagged") or [])
    before = set(task["page"]["flagged"])
    untouched_flags = (flagged - {tid}) == (before - {tid})
    if kind == "reply":
        return (not deleted and untouched_flags and len(replies) == 1 and replies[0]["to"] == tid
                and replies[0]["body"].strip() == task["body"])
    if kind == "delete":
        return deleted == {tid} and not replies and untouched_flags
    if kind == "flag":
        return tid in flagged and untouched_flags and not deleted and not replies
    return (answer is not None and task["answer"] in answer and not deleted and not replies
            and flagged == before)


def oracle(task: dict, app_state: dict, request: dict) -> tuple[dict | None, str]:
    """The right answer to this request, keyed by the request's own option keys, and why; None if not offered."""
    q = request["questions"]
    els = request["state"]["elements"]
    texts = task["page"]["texts"]
    t = task["target"]
    tid, kind = t["id"], task["kind"]

    def one_hot(head: str, key: str) -> dict:
        return {"type": "choice", "choice": key, "confidence": 1.0,
                "probabilities": {k: (1.0 if k == key else 0.0) for k in q[head]["criteria"]}}

    def pick(op: str, why: str, **heads) -> tuple[dict | None, str]:
        if op not in q["operation"]["criteria"]:
            return None, f"{op} not offered ({why})"
        out = {"operation": one_hot("operation", op)}
        for head, key in heads.items():
            if key is None or head not in q or key not in q[head]["criteria"]:
                return None, f"{head} option missing ({why})"
            out[head] = one_hot(head, key)
        return out, why

    def el(pred):
        e = next((e for e in els if pred(e)), None)
        return e and e["index"]

    def button(label: str):
        return el(lambda e: e.get("label") == label and "CLICK" in (e.get("operations") or []))

    replies = app_state.get("replies") or []
    deleted = set(app_state.get("deleted") or [])
    flagged = set(app_state.get("flagged") or [])
    selected = app_state.get("selected")
    # Something irreversible already went wrong (a wrong message deleted or answered, a flag toggled off): no step
    # from here makes the run right, so the state is not labelled.
    if deleted - {tid} or any(r["to"] != tid for r in replies):
        return None, "an irreversible wrong step was taken"
    # A flag is reversible: one changed on another message is put back first (select it, press the button) --
    # "flagged the wrong message" looks done and is not (oracle v3).
    before = set(task["page"]["flagged"])
    wrong = sorted((flagged ^ before) - {tid})
    if wrong and not app_state.get("dialog") and not app_state.get("composing"):
        m = next(x for x in task["page"]["inbox"] if x["id"] == wrong[0])
        if selected != m["id"]:
            row = el(lambda e: e.get("role") == "row" and (e.get("label") or "").split(" · ")[:2] == [m["from"], m["subject"]])
            return pick("CLICK", "a flag was changed on the wrong message: open it to put the flag back", click_target=row)
        return pick("CLICK", "put back the flag changed on the wrong message",
                    click_target=button(texts["unflag"] if m["id"] in flagged else texts["flag"]))
    # A dialog or a compose box opened on the wrong message is closed first.
    if app_state.get("dialog") and (selected != tid or kind != "delete"):
        return pick("CLICK", "the delete dialog is open for the wrong message: cancel it",
                    click_target=button(texts["cancel"]))
    if app_state.get("composing") and (selected != tid or kind != "reply"):
        return pick("CLICK", "the reply box is open for the wrong message: cancel it",
                    click_target=button(texts["cancel"]))
    if kind != "ask" and passed(task, app_state):
        return pick("DONE", "already flagged: nothing to do" if kind == "flag" and task["already"] and not selected
                    else "the goal is done")
    if selected != tid:
        exact = lambda e: e.get("role") == "row" and (e.get("label") or "").split(" · ")[:2] == [t["from"], t["subject"]]
        row = el(exact)
        label, why = pick("CLICK", "open the exact message (sender and subject)", click_target=row)
        # A double click selects the row as well (the click fires first), so OPEN on the same row is as right as
        # CLICK: both get mass (oracle v2).
        if label is not None and "OPEN" in q["operation"]["criteria"] and \
                row in (q.get("open_target") or {}).get("criteria", {}) and \
                any(exact(e) and "OPEN" in (e.get("operations") or []) for e in els):
            label["operation"]["probabilities"].update({"CLICK": 0.5, "OPEN": 0.5})
            label["open_target"] = one_hot("open_target", row)
            why += " (click or double-click the row)"
        return label, why
    if kind == "ask":
        vals = (q.get("answer_value") or {}).get("criteria") or {}
        hits = sorted(((k, v.get("value") or "") for k, v in vals.items() if task["answer"] in (v.get("value") or "")),
                      key=lambda kv: len(kv[1]))
        return pick("ANSWER", "the time is in the open message: answer it", answer_value=hits[0][0] if hits else None)
    if kind == "flag":
        return pick("CLICK", "flag the open message", click_target=button(texts["flag"]))
    if kind == "delete":
        if app_state.get("dialog"):
            return pick("CLICK", "confirm the deletion", click_target=button(texts["confirm"]))
        return pick("CLICK", "delete the open message", click_target=button(texts["delete"]))
    # reply
    if not app_state.get("composing"):
        return pick("CLICK", "start a reply to the open message", click_target=button(texts["reply"]))
    if (app_state.get("draft") or "").strip() != task["body"]:
        vals = (q.get("type_text_value") or {}).get("criteria") or {}
        key = next((k for k, v in vals.items() if (v.get("value") or "").strip() == task["body"]), None)
        box = el(lambda e: e.get("label") == texts["body"])
        return pick("TYPE_TEXT", "type the reply the goal gives", type_text_target=box, type_text_value=key)
    return pick("CLICK", "the reply is typed: send it", click_target=button(texts["send"]))


def trap(task: dict, app_state: dict, request: dict) -> tuple | None:
    """A plausible wrong step: the other message from the same sender (or the same subject from someone else)
    opened instead of the target -- and in a flag task, then flagged (a two-step trap: the third element keeps it
    armed for the flag). Nothing irreversible: a flag is put back by the oracle."""
    from tools.gym.music import _choose
    t = task["target"]
    if app_state.get("dialog") or app_state.get("composing"):
        return None
    els, q = request["state"]["elements"], request["questions"]
    sel = app_state.get("selected")
    if task.get("kind") == "flag" and sel and sel != t["id"] and sel not in (app_state.get("flagged") or []):
        texts = task["page"]["texts"]
        b = next((e for e in els if e.get("label") == texts["flag"]), None)
        label = b and _choose(q, "CLICK", click_target=b["index"])
        return (label, "trap: flagged the wrong message") if label else None
    if sel == t["id"]:
        return None
    for e in els:
        if e.get("role") != "row":
            continue
        parts = (e.get("label") or "").split(" · ")
        if len(parts) >= 2 and parts[:2] != [t["from"], t["subject"]] and (parts[0] == t["from"] or parts[1] == t["subject"]):
            label = _choose(q, "CLICK", click_target=e["index"])
            if label:
                return label, f"trap: opened {e.get('label')!r} instead of the target", task.get("kind") == "flag"
    return None

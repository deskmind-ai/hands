"""Which actions a person has to approve before they happen: sending, deleting, paying, publishing, sharing.

The planner is not trusted to ask. A typed-choice planner has no "request approval" answer at all, and one that has
would still be the thing being guarded against. So the loop asks, before executing, whenever `risky` names the
action, in a run with a person to ask (`RunConfig.approve_risky`, set by `deskmind-hands do --ask stdin`). A live run
with nobody to ask refuses those actions (`RunConfig.refuse_risky`) unless `--allow-risky` was given on purpose. The
graded task sets have scripted users and fixtures whose deletions are the task; they are not gated.

Named by what the action targets, not by what the model meant:
  - a button, menu item or link whose name is a risky verb ("发送", "Delete", "立即支付", "Share");
  - a menu path with such an item ("File > Move to Trash");
  - Return (or cmd/ctrl+Return) straight after typing into a field that is not a search, name or path box --
    in a chat window that is how a message is sent;
  - cmd+Delete, which moves the selection to the Trash, and Delete on its own when no text was just typed (in Mail
    or a list it deletes the selected item).
"""
from __future__ import annotations

import re

from ..actions import Action, ActionKind

RISKY_WORDS = (
    # zh
    "发送", "发 送", "删除", "彻底删除", "永久删除", "确认删除", "移到废纸篓", "移至废纸篓", "清空废纸篓", "付款", "支付",
    "立即支付", "确认支付", "购买", "立即购买", "下单", "提交订单", "确认下单", "转账", "发布", "发表", "分享", "公开",
    # en
    "send", "delete", "delete message", "move to trash", "empty trash", "pay", "pay now", "buy", "buy now",
    "purchase", "place order", "checkout", "check out", "transfer", "publish", "post", "share", "make public",
)
_WORDS = sorted(RISKY_WORDS, key=len, reverse=True)
_START = re.compile(r"^\s*(?:" + "|".join(re.escape(w) for w in _WORDS) + r")(?![A-Za-z])", re.I)
#: Roles whose name describes a state or some text, not something that acts: a switch called "Share analytics" is a
#: setting, a heading "发送时间" is a label.
_NOT_ACTIONS = {"checkbox", "switch", "axcheckbox", "textfield", "textarea", "statictext", "heading", "row", "cell",
                "table", "group", "radiobutton", "slider", "image"}
#: Fields where Return submits something harmless: a search, a name being given, a path being typed.
_BENIGN_FIELD = re.compile(r"搜索|查找|search|find|名称|名字|文件名|重命名|name|rename|路径|path|地址|url|前往|go to", re.I)
_RETURN = {"return", "enter", "kp_enter"}
#: Names that start with a risky verb and are not one: formatting, events, lists.
_NOT_VERBS = re.compile(r"^\s*(?:删除线|发布会|发布日期|发布时间|发送时间|公开课|分享会|支付方式|付款方式|购买记录|"
                        r"delete(?:d)? items|shared with me|share sheet|posts?\b(?= by)|payment method)", re.I)


def _verb(label: str) -> str | None:
    label = (label or "").strip()
    if not label or len(label) > 30:
        return None   # a sentence that happens to start with "Send" is not a button
    if _NOT_VERBS.match(label):
        return None
    m = _START.match(label)
    return m.group(0).strip() if m else None


def risky(action: Action, obs, last_typed_label: str | None = None) -> str | None:
    """What the action would do, as a short phrase for the approval question, or None if it needs no approval."""
    k = action.kind
    if k in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK) and action.binding.element_id:
        el = next((e for e in obs.elements if e.id == action.binding.element_id), None)
        if el is None or (el.role or "").lower() in _NOT_ACTIONS:
            return None
        return f"click {el.label.strip()!r}" if _verb(el.label) else None
    if k is ActionKind.MENU:
        path = [p.strip() for p in re.split(r"\s*(?:>|›|→)\s*", action.text or "") if p.strip()]
        return f"choose {' > '.join(path)!r}" if path and _verb(path[-1]) else None
    if k is ActionKind.KEY:
        keys = {x.lower() for x in action.keys}
        if keys & _RETURN and keys <= _RETURN | {"cmd", "command", "ctrl", "control"} and last_typed_label is not None \
                and not _BENIGN_FIELD.search(last_typed_label):
            return f"press {'+'.join(action.keys)} after typing into {last_typed_label!r} (this usually sends it)"
        if {"cmd", "command"} & keys and {"delete", "backspace"} & keys:
            return "press cmd+Delete (moves the selection to the Trash)"
        if keys <= {"delete", "backspace", "forwarddelete"} and last_typed_label is None:
            # Not in the middle of editing text: Delete acts on the selection (a message, a row, a file).
            return "press Delete (deletes the selected item)"
    return None


_KINDS = (("send", r"发送|发 送|send|sends it"), ("delete", r"删除|废纸篓|delete|trash"),
          ("pay", r"付款|支付|购买|下单|订单|转账|pay|buy|purchase|order|checkout|check out|transfer"),
          ("publish", r"发布|发表|公开|publish|post|public"), ("share", r"分享|share"))


def kind(what: str) -> str:
    """send / delete / pay / publish / share: a step and the confirmation it opens are one approval."""
    return next((k for k, pat in _KINDS if re.search(pat, what or "", re.I)), what)


def question(what: str, app: str, goal: str) -> str:
    """The approval question, in the goal's language."""
    if re.search(r"[一-鿿]", goal or ""):
        return f"下一步要在 {app} 里执行：{what}。这一步可能无法撤销，要继续吗？"
    return f"Next step in {app}: {what}. It may not be undoable. Go ahead?"


#: The rules' own examples, beside them and checked whenever this module is loaded (check_examples): a change to the
#: word lists that stops guarding "发送", or starts guarding "删除线", fails where it is made, at once -- not in a
#: user's run. Codex keeps each execpolicy rule's match / not_match examples next to it the same way.
#: (what is acted on, its name / path / keys, the field typed into just before or None, needs approval)
EXAMPLES = (
    ("button", "发送", None, True), ("button", "Send", None, True), ("button", "Delete Message", None, True),
    ("menuitem", "移到废纸篓", None, True), ("button", "立即支付", None, True), ("link", "Share", None, True),
    ("button", "Publish", None, True), ("button", "Buy now", None, True), ("button", "提交订单", None, True),
    ("menu", "文件 > 移到废纸篓", None, True), ("menu", "File > Move to Trash", None, True),
    ("keys", "return", "消息", True), ("keys", "cmd+return", "Message", True), ("keys", "cmd+delete", None, True),
    ("keys", "delete", None, True),
    ("button", "删除线", None, False), ("button", "Deleted Items", None, False), ("button", "Shared with me", None, False),
    ("button", "Postpone", None, False), ("button", "Settings", None, False), ("button", "发布日期", None, False),
    ("checkbox", "Share analytics", None, False), ("heading", "发送时间", None, False), ("cell", "Delete", None, False),
    ("button", "Send this file to the printer in the next room please", None, False),
    ("menu", "Format > 删除线", None, False),
    ("keys", "return", "搜索", False), ("keys", "return", "文件名", False), ("keys", "delete", "notes", False),
    ("keys", "cmd+s", None, False),
)


def check_examples() -> list[str]:
    """Each example the rules decide otherwise than it says, as a sentence; empty when they all hold."""
    from types import SimpleNamespace
    from ..actions import Binding
    wrong = []
    for what, name, typed, want in EXAMPLES:
        el = SimpleNamespace(id="e", role=what, label=name)
        obs = SimpleNamespace(elements=[el])
        if what == "menu":
            a = Action(kind=ActionKind.MENU, text=name, binding=Binding(observation_id="o", app="x"))
        elif what == "keys":
            a = Action(kind=ActionKind.KEY, keys=tuple(name.split("+")), binding=Binding(observation_id="o", app="x"))
        else:
            a = Action(kind=ActionKind.CLICK, binding=Binding(observation_id="o", app="x", element_id="e"))
        got = risky(a, obs, typed) is not None
        if got != want:
            wrong.append(f"{what} {name!r}" + (f" after typing into {typed!r}" if typed else "") +
                         (" is not asked about" if want else " is asked about"))
    return wrong


_WRONG = check_examples()
if _WRONG:
    raise RuntimeError("risk.py: the rules disagree with their own examples: " + "; ".join(_WRONG))


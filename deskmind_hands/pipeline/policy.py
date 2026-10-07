"""Policy: whether a decided step is carried out (deskmind#58's policy stage; its first rule is deskmind#63 part 2).

A step whose consequences outlast it -- a write, or a click on a control that commits (save, submit, send, delete) --
is carried out only when the model was sure enough of it: the weakest of p(operation) and p(each head it uses) at
least `Floors.consequential`, and the value it writes at least `Floors.value`. Below either, the step is not carried
out; the loop decides what happens instead (look again, then ask the user, else stop).

The floors are a property of the model's calibration, so they come with the weights when the server says
(GET /v1/models: {"floors": {"consequential": ..., "value": ...}}), and are these defaults otherwise. They hold
whatever server answers.

Defaults from E1 (G18b, deskmind#63): at a consequential floor of 0.90 no right step was refused on trained work,
synthetic or on the real desktop, and on apps the model had not seen wrong consequential steps fell from 23 to 5; at
0.95 a fifth of the right writes on the real desktop would have been refused. A value floor of 0.5 refused no right
value anywhere, and catches the value chosen at 0.096 and typed over a document (#63 part 1's case).

What this rule does not see, on purpose or for now:
- a head with one option is not in the request, so it counts as certain (one value offered: no value floor);
- the floors apply to the answer acted on, whichever tier gave it: after the router escalates, the 4B's p;
- SELECT is not held to it -- choosing a form's option is undone by choosing another; the Submit that sends the form
  is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..runtime.risk import RISKY_WORDS

#: Controls whose click commits something: the approval terms (risk.RISKY_WORDS) plus save and submit -- the same list
#: brain's router escalates on (deskmind_brain.router.COMMIT_WORDS; a test keeps the two in step).
COMMIT_WORDS = tuple(RISKY_WORDS) + ("保存", "提交", "save", "submit")
_COMMIT = re.compile(r"^\s*(?:" + "|".join(re.escape(w) for w in sorted(COMMIT_WORDS, key=len, reverse=True)) +
                     r")(?![A-Za-z])", re.I)
#: Names that begin with a commit word and are not one.
_NOT_COMMIT = re.compile(r"^\s*(?:删除线|发布会|发布日期|发布时间|发送时间|公开课|分享会|支付方式|付款方式|购买记录|保存位置|"
                         r"提交记录|delete(?:d)? items|shared with me|share sheet|posts?\b(?= by)|payment method|"
                         r"saved\b|save as\b|submissions?\b)", re.I)
#: Roles whose name describes a state or some text, not something that acts.
_NOT_ACTIONS = {"checkbox", "switch", "axcheckbox", "textfield", "textarea", "statictext", "heading", "row", "cell",
                "table", "group", "radiobutton", "slider", "image"}
#: Operations that write text, and the heads that hold what they write.
WRITE_OPS = {"TYPE_TEXT", "APPEND_TEXT", "REPLACE_TEXT", "RENAME", "TYPE_FOCUSED"}
VALUE_HEADS = ("type_text_value", "replace_from")
#: Chords that write, commit or remove: saving, confirming (Return), deleting, cutting, pasting, moving (Finder's
#: option+cmd+v), making a folder.
COMMIT_KEYS = {"cmd+s", "cmd+shift+s", "return", "enter", "kp_enter", "cmd+return", "cmd+enter", "delete",
               "forward_delete", "backspace", "cmd+delete", "cmd+backspace", "option+cmd+v", "cmd+v", "cmd+x",
               "cmd+shift+n"}
#: Which heads hold the text each write puts on screen.
WRITE_VALUES = {"TYPE_TEXT": ("type_text_value",), "APPEND_TEXT": ("type_text_value",), "RENAME": ("type_text_value",),
                "TYPE_FOCUSED": ("type_text_value",), "REPLACE_TEXT": ("replace_from", "type_text_value")}


@dataclass(frozen=True)
class Floors:
    consequential: float = 0.90
    value: float = 0.5

    @classmethod
    def local(cls) -> "Floors":
        """The floors on this machine: the defaults, or HANDS_FLOORS ("consequential=0.85,value=0.4") for a developer or
        an evaluation that means to change them. The app does not set it."""
        import os
        out = {}
        for part in filter(None, (x.strip() for x in os.environ.get("HANDS_FLOORS", "").split(","))):
            k, _, v = part.partition("=")
            try:
                out[k.strip()] = float(v)
            except ValueError:
                continue
        base = cls()
        ok = lambda k, v: out[k] if isinstance(out.get(k), float) and 0 <= out[k] <= 1 else v  # noqa: E731
        return cls(consequential=ok("consequential", base.consequential), value=ok("value", base.value))

    def raised_by(self, advertised) -> "Floors":
        """These floors, raised where the server's are higher. A server may ask a client to be more careful with its
        model, never less: {"consequential": 0} from a server would otherwise have turned the rule off."""
        d = advertised if isinstance(advertised, dict) else {}
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1  # noqa: E731
        return Floors(consequential=max(self.consequential, float(d["consequential"])) if num(d.get("consequential"))
                      else self.consequential,
                      value=max(self.value, float(d["value"])) if num(d.get("value")) else self.value)

    @classmethod
    def from_server(cls, advertised) -> "Floors":
        return cls.local().raised_by(advertised)


def commits(label: str, role: str = "") -> bool:
    """A control whose click commits: named by a commit word, and one that acts."""
    label = re.sub(r"^\[\w+\]\s*", "", label or "").strip()
    if (role or "").lower() in _NOT_ACTIONS or not label or len(label) > 30 or _NOT_COMMIT.match(label):
        return False
    return bool(_COMMIT.match(label))


def consequential(op: str, target: dict | None = None, chord: str = "") -> bool:
    """A write, a click on a committing control, or a chord that saves."""
    if op in WRITE_OPS:
        return True
    if op in ("CLICK", "OPEN") and target:
        return commits(str(target.get("label") or ""), str(target.get("role") or ""))
    return op == "KEY" and chord.lower() in COMMIT_KEYS


def _top(answer: dict | None) -> float:
    """The chosen option's probability; an answer that gives none counts as 0 (as the adapter's _pick and brain's
    router read it), never as certain."""
    probs = (answer or {}).get("probabilities") or {}
    return max(probs.values()) if probs else float((answer or {}).get("confidence", 0.0) or 0.0)


def unsure(op: str, answers: dict, heads: tuple[str, ...], floors: Floors, *, target: dict | None = None,
           chord: str = "", written: dict | None = None) -> str | None:
    """Why a decided step is not to be carried out, or None.

    `heads` are the heads `op` used. `written` gives the probability of the value actually written where it is not the
    head's top choice -- REPLACE_TEXT falls back to the first valid pair -- and None for a value no head chose (one a
    text helper made). A head the request did not carry (a question with one option, answered by having no choice)
    is not counted: there was nothing to be unsure between."""
    if not consequential(op, target, chord):
        return None
    written = written or {}
    probs = (answers.get("operation") or {}).get("probabilities") or {}
    p_op = float(probs.get(op, _top(answers.get("operation"))))
    targets = [_top(answers[h]) for h in heads if h in answers and h not in VALUE_HEADS]
    values = []
    for h in WRITE_VALUES.get(op, ()):
        p = written[h] if h in written else (_top(answers[h]) if h in answers else None)
        if p is None:
            return "the text to write was not chosen by the model, so there is no saying how sure it was"
        values.append(p)
    if values and min(values) < floors.value:
        return f"the text to write was chosen at {min(values):.2f}, below {floors.value:.2f}"
    conf = min([p_op] + targets + values)
    if conf < floors.consequential:
        return f"this step was chosen at {conf:.2f}, below {floors.consequential:.2f} for a step that writes or commits"
    return None

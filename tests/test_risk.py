"""The actions a person approves before they happen (hands/runtime/risk.py). Pure python, no desktop."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.actions import Action, ActionKind, Binding
from deskmind_hands.drivers.base import Element
from deskmind_hands.runtime import risk


def obs(*els):
    return SimpleNamespace(elements=[Element(id=f"e{i}", role=r, label=l) for i, (r, l) in enumerate(els)])


def click(i=0, kind=ActionKind.CLICK):
    return Action(kind=kind, binding=Binding(element_id=f"e{i}"))


class RiskyTest(unittest.TestCase):
    def test_risky_targets(self):
        for role, label in [("button", "发送"), ("button", "Send"), ("button", "删除邮件"), ("menuItem", "Move to Trash"),
                            ("button", "立即支付"), ("link", "Buy now"), ("button", "Publish"), ("button", "分享"),
                            ("listitem", "发送"), ("button", "确认删除"), ("button", "Delete message"),
                            ("button", "Post")]:
            with self.subTest(label=label):
                self.assertTrue(risk.risky(click(), obs((role, label))))

    def test_not_risky(self):
        for role, label in [("checkBox", "Share analytics"), ("switch", "分享使用数据"), ("heading", "发送时间"),
                            ("button", "删除线"), ("button", "Posts"), ("button", "Sender"), ("button", "搜索"),
                            ("button", "播放"), ("staticText", "Delete"), ("button", "发布会"),
                            ("button", "Send us your feedback about this beautiful product today")]:
            with self.subTest(label=label):
                self.assertIsNone(risk.risky(click(), obs((role, label))))

    def test_double_click_counts(self):
        self.assertTrue(risk.risky(click(kind=ActionKind.DOUBLE_CLICK), obs(("button", "Send"))))

    def test_menu_path(self):
        self.assertTrue(risk.risky(Action(kind=ActionKind.MENU, text="文件 > 移到废纸篓"), obs()))
        self.assertIsNone(risk.risky(Action(kind=ActionKind.MENU, text="File > Save"), obs()))

    def test_return_after_typing_a_message(self):
        ret = Action(kind=ActionKind.KEY, keys=("return",))
        self.assertTrue(risk.risky(ret, obs(), last_typed_label="发消息给 项目组"))
        self.assertTrue(risk.risky(Action(kind=ActionKind.KEY, keys=("cmd", "return")), obs(),
                                   last_typed_label="Message"))
        self.assertIsNone(risk.risky(ret, obs(), last_typed_label="搜索"))      # a search
        self.assertIsNone(risk.risky(ret, obs(), last_typed_label="名称"))      # a name being given
        self.assertIsNone(risk.risky(ret, obs(), last_typed_label=None))        # nothing typed just before
        self.assertIsNone(risk.risky(Action(kind=ActionKind.KEY, keys=("shift", "return")), obs(),
                                     last_typed_label="Message"))

    def test_cmd_delete(self):
        self.assertTrue(risk.risky(Action(kind=ActionKind.KEY, keys=("cmd", "delete")), obs()))
        self.assertTrue(risk.risky(Action(kind=ActionKind.KEY, keys=("delete",)), obs()))      # the selection
        self.assertIsNone(risk.risky(Action(kind=ActionKind.KEY, keys=("delete",)), obs(),
                                     last_typed_label="Message"))                             # editing text

    def test_kinds(self):
        self.assertEqual(risk.kind("click 'Delete'"), risk.kind("click 'Delete message'"))
        self.assertEqual(risk.kind("click '删除'"), risk.kind("click '确认删除'"))
        self.assertNotEqual(risk.kind("click 'Send'"), risk.kind("click 'Delete'"))

    def test_question_language(self):
        self.assertTrue(risk.question("click '发送'", "聊天", "给项目组发一句你好").startswith("下一步要在 聊天 里执行"))
        self.assertTrue(risk.question("click 'Send'", "Mail", "Reply to Ann").startswith("Next step in Mail"))


class User:
    """Answers every approval the same way, and remembers what it was asked."""

    def __init__(self, approve: bool):
        self.approve, self.asked = approve, []

    def respond(self, question, approval, options=()):
        self.asked.append((question, approval))
        return ("ok" if self.approve else "no"), self.approve, 0.0


class LoopTest(unittest.TestCase):
    """The loop asks before a risky step, and a denied step is not carried out (mock desktop, S01's oracle)."""

    def run_s01(self, user, approve_risky=True, refuse_risky=False):
        import tempfile
        from deskmind_hands.adapters.scripted import OracleAdapter
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        from deskmind_bench.task import load_task
        repo = Path(__file__).resolve().parent.parent
        task = load_task(repo / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")
        # Every click counts as risky here: the mock desktop has no Send or Delete button of its own.
        real = risk.risky
        risk.risky = lambda a, o, last=None, **kw: "click it" if a.kind is ActionKind.CLICK else None
        try:
            res = run_task(task, MockDriver(render=False), OracleAdapter(), ws,
                           config=RunConfig(user=user, approve_risky=approve_risky, refuse_risky=refuse_risky))
        finally:
            risk.risky = real
        return res, ws

    def test_approved_steps_run(self):
        user = User(approve=True)
        res, ws = self.run_s01(user)
        # Selecting the row and pressing Rename are two steps, each asked once; neither is asked twice.
        self.assertEqual(len(user.asked), 2)
        self.assertTrue(all(approval for _, approval in user.asked))
        self.assertTrue((ws.ws / "final.txt").exists())

    def test_a_denied_step_is_not_done(self):
        user = User(approve=False)
        res, ws = self.run_s01(user)
        self.assertGreaterEqual(len(user.asked), 1)
        self.assertTrue((ws.ws / "draft.txt").exists(), "a denied click must not have happened")
        self.assertFalse((ws.ws / "final.txt").exists())

    def test_refused_when_nobody_is_there_to_ask(self):
        """`do` without --ask stdin: a risky step is refused, not asked and not carried out (10-02 review)."""
        user = User(approve=True)
        res, ws = self.run_s01(user, approve_risky=False, refuse_risky=True)
        self.assertEqual(user.asked, [])
        self.assertTrue((ws.ws / "draft.txt").exists(), "a refused click must not have happened")
        self.assertFalse((ws.ws / "final.txt").exists())
        self.assertTrue(any(s.get("kind") == "refused_risky" for s in res.steps))

    def test_off_without_a_person(self):
        user = User(approve=False)
        res, ws = self.run_s01(user, approve_risky=False)
        self.assertEqual(user.asked, [])
        self.assertTrue((ws.ws / "final.txt").exists())


class OneStepApprovals(unittest.TestCase):
    """An approval covers one step: approving one "click 'Send'" covered every later Send of the run, whatever it
    sent (10-02 review). The one step it also covers is the confirmation that step opens itself: approving "delete A"
    then covered deleting B after a step in between, because only the kind, the app and "within two steps" were
    compared (protocol review, 10-06)."""

    def run_actions(self, actions, confirms=False):
        import tempfile
        from deskmind_hands.adapters.scripted import ReplayAdapter
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        from deskmind_bench.task import load_task
        repo = Path(__file__).resolve().parent.parent
        task = load_task(repo / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")
        user = User(approve=True)
        driver = MockDriver(render=False)
        if confirms:   # deleting a row opens a confirmation, as Mail's "Delete" does
            execute = driver.execute

            def with_confirmation(action):
                res = execute(action)
                if action.kind is ActionKind.CLICK and (action.binding.element_id or "").startswith("row:") and res.ok:
                    driver.inject("modal", {"text": "Delete this item?"})
                return res
            driver.execute = with_confirmation
        risky = {"row:draft.txt": "click 'Delete'", "row:notes.txt": "click 'Delete'", "btn:modal_ok": "click 'Delete message'"}
        real = risk.risky
        risk.risky = lambda a, o, last=None, **kw: (risky.get(a.binding.element_id) if a.kind is ActionKind.CLICK
                                                    else None)
        try:
            run_task(task, driver, ReplayAdapter(actions), ws, config=RunConfig(user=user, approve_risky=True))
        finally:
            risk.risky = real
        return [q for q, _ in user.asked]

    A = {"kind": "click", "binding": {"element_id": "row:draft.txt"}}
    B = {"kind": "click", "binding": {"element_id": "row:notes.txt"}}
    OK = {"kind": "click", "binding": {"element_id": "btn:modal_ok"}}
    STEP = {"kind": "click", "binding": {"element_id": "row:report.txt"}}   # not risky: a step in between
    DONE = {"kind": "done"}

    def test_a_later_send_is_asked_again(self):
        self.assertEqual(len(self.run_actions([self.A, self.STEP, self.STEP, self.STEP, self.A, self.DONE])), 2)

    def test_the_confirmation_it_opens_is_not_asked_twice(self):
        asked = self.run_actions([self.A, self.OK, self.DONE], confirms=True)
        self.assertEqual(len(asked), 1, asked)

    def test_another_target_after_a_step_in_between_is_asked(self):
        """The review's case: approve deleting A, one other step, then delete B -- same kind, same app, two steps."""
        self.assertEqual(len(self.run_actions([self.A, self.STEP, self.B, self.DONE])), 2)

    def test_the_next_step_without_a_confirmation_is_asked(self):
        """No dialog came up: the next delete is another deletion, not the confirmation of the first."""
        self.assertEqual(len(self.run_actions([self.A, self.B, self.DONE])), 2)
        self.assertEqual(len(self.run_actions([self.A, self.A, self.A, self.DONE])), 3)

    def test_the_dialog_must_be_one_the_step_opened(self):
        from deskmind_hands.runtime import risk as r

        class Obs:
            dialog, elements = False, []
        self.assertFalse(r.dialog_up(Obs()))
        Obs.dialog = True
        self.assertTrue(r.dialog_up(Obs()))

        class El:
            role = "AXSheet"
        Obs.dialog, Obs.elements = False, [El()]
        self.assertTrue(r.dialog_up(Obs()), "a sheet counts as the dialog")


class AskRecord(unittest.TestCase):
    """The planner's decision to ask is recorded with its probabilities and time, and when the reply came: a
    recording's overlay showed nothing for the question step, which read as the harness's doing."""

    def test_question_step_keeps_the_decision(self):
        import tempfile
        from deskmind_hands.actions import Action
        from deskmind_hands.adapters.base import Proposal
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        from deskmind_bench.task import load_task
        repo = Path(__file__).resolve().parent.parent
        task = load_task(repo / "tasks" / "smoke" / "S01-rename.yaml")
        task.budget.max_dialogue_turns = 2
        ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")

        class Asker:
            def __init__(self):
                self.n = 0

            def propose(self, ctx):
                self.n += 1
                if self.n == 1:
                    return Proposal(action=Action(kind=ActionKind.ASK_USER, text="Which one?"),
                                    raw_text='{"operation": "ASK", "top": [["ASK", 0.9]]}', latency_s=1.25)
                return Proposal(action=Action(kind=ActionKind.DONE), raw_text="done")

            def usage(self):
                return {}

        res = run_task(task, MockDriver(render=False), Asker(), ws, config=RunConfig(user=User(approve=True)))
        ask = next(s for s in res.steps if s.get("kind") == "ask_user")
        self.assertIn('"ASK"', ask["decision"])
        self.assertEqual(ask["latency_s"], 1.25)
        self.assertEqual(ask["reply"], "ok")
        self.assertGreaterEqual(ask["t_reply"] + 0.01, ask.get("t_decide_end", 0))   # times are kept to the ms



class OwnExamples(unittest.TestCase):
    """The rules carry their examples and check them on load (risk.EXAMPLES): a word-list change that stops guarding
    a send, or starts guarding a strikethrough button, fails where it is made."""

    def test_the_examples_hold(self):
        from deskmind_hands.runtime import risk
        self.assertEqual(risk.check_examples(), [])
        self.assertTrue(any(e[3] for e in risk.EXAMPLES) and any(not e[3] for e in risk.EXAMPLES))

    def test_a_disagreeing_example_is_named(self):
        from unittest import mock
        from deskmind_hands.runtime import risk
        with mock.patch.object(risk, "EXAMPLES", (("button", "删除线", None, True), ("button", "发送", None, False))):
            self.assertEqual(risk.check_examples(), ["button '删除线' is not asked about", "button '发送' is asked about"])


class RenameTest(unittest.TestCase):
    """Renaming a file or folder in a person's own folder waits for approval (10-05: a vague goal renamed real files
    to "file-8" and "file-3", with no undo)."""

    def typed(self, text, i=0):
        return Action(kind=ActionKind.TYPE_TEXT, text=text, binding=Binding(element_id=f"e{i}"))

    def test_rename_asked_when_renames_are_confirmed(self):
        o = obs(("file", "report.pdf"), ("folder", "Archive"))
        self.assertEqual(risk.risky(self.typed("file-8"), o, renames=True), "rename 'report.pdf' to 'file-8'")
        self.assertEqual(risk.risky(self.typed("Old", 1), o, renames=True), "rename 'Archive' to 'Old'")

    def test_not_asked_otherwise(self):
        o = obs(("file", "report.pdf"), ("textField", "Name"))
        self.assertIsNone(risk.risky(self.typed("file-8"), o))                     # the sample folder: not gated
        self.assertIsNone(risk.risky(self.typed("report.pdf"), o, renames=True))   # the name it has: no rename
        self.assertIsNone(risk.risky(self.typed("hello", 1), o, renames=True))     # typing into a field is not renaming

    def test_each_rename_is_its_own_approval(self):
        self.assertNotEqual(risk.kind("rename 'a.pdf' to 'b.pdf'"), risk.kind("rename 'c.pdf' to 'd.pdf'"))


if __name__ == "__main__":
    unittest.main()

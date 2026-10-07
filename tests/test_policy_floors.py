"""The policy stage's first rule (deskmind#63 part 2): a write, or a click that commits, is carried out only when the
model was sure enough of it. The floors come with the model (GET /v1/models "floors") or are 0.90 / 0.5. Below them
the step is not carried out: the planner looks again, then the user is asked where there is someone to ask, and the
run stops where there is not."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from deskmind_hands.pipeline import policy   # noqa: E402
from deskmind_hands.pipeline.policy import Floors, unsure   # noqa: E402


def spread(p: float) -> dict:
    """The chosen option at p, the rest of the mass over options none of which beats it."""
    n = max(1, int((1 - p) / p) + 1) if p < 0.5 else 1
    return {"1": p, **{str(i + 2): round((1 - p) / n, 4) for i in range(n)}}


def ans(op, p_op, **heads):
    out = {"operation": {"choice": op, "probabilities": {op: p_op, "CLICK": round(1 - p_op, 3)}}}
    for h, p in heads.items():
        out[h] = {"choice": "1", "probabilities": spread(p)}
    return out


class Rule(unittest.TestCase):
    def test_a_value_chosen_at_a_tenth_is_never_written(self):
        """#63's case: the value typed over the document was chosen at 0.096."""
        why = unsure("REPLACE_TEXT", ans("REPLACE_TEXT", 0.956, replace_text_target=0.99, replace_from=0.892,
                                         type_text_value=0.096),
                     ("replace_text_target", "replace_from", "type_text_value"), Floors())
        self.assertIn("text to write", why)

    def test_a_write_below_the_floor_is_not_carried_out(self):
        heads = ("type_text_target", "type_text_value")
        self.assertIsNotNone(unsure("TYPE_TEXT", ans("TYPE_TEXT", 0.85, type_text_target=0.99, type_text_value=0.99),
                                    heads, Floors()))
        self.assertIsNone(unsure("TYPE_TEXT", ans("TYPE_TEXT", 0.91, type_text_target=0.99, type_text_value=0.99),
                                 heads, Floors()))

    def test_a_commit_click_is_held_to_it_and_a_plain_click_is_not(self):
        save = {"label": "保存", "role": "button"}
        self.assertIsNotNone(unsure("CLICK", ans("CLICK", 0.6, click_target=0.99), ("click_target",), Floors(),
                                    target=save))
        self.assertIsNone(unsure("CLICK", ans("CLICK", 0.6, click_target=0.99), ("click_target",), Floors(),
                                 target={"label": "收件箱", "role": "link"}))
        self.assertIsNone(unsure("CLICK", ans("CLICK", 0.6, click_target=0.99), ("click_target",), Floors(),
                                 target={"label": "保存位置", "role": "button"}), "a look-alike")
        self.assertIsNone(unsure("CLICK", ans("CLICK", 0.6, click_target=0.99), ("click_target",), Floors(),
                                 target={"label": "Send", "role": "checkbox"}), "a control that does not act")
        self.assertIsNotNone(unsure("KEY", ans("KEY", 0.6, key_target=0.99), ("key_target",), Floors(), chord="cmd+s"))

    def test_a_server_may_only_raise_the_floors(self):
        """Review of #30: {"consequential": 0} from a server turned the rule off, and a bool passed as a number."""
        self.assertEqual(Floors.from_server({"consequential": 0.95, "value": 0.6}), Floors(0.95, 0.6))
        self.assertEqual(Floors.from_server({"consequential": 0, "value": 0}), Floors(0.90, 0.5))
        self.assertEqual(Floors.from_server({"consequential": True, "value": "x"}), Floors())
        self.assertEqual(Floors.from_server({"consequential": 7}), Floors())
        self.assertEqual(Floors.from_server(None), Floors(0.90, 0.5))
        with mock.patch.dict(os.environ, {"HANDS_FLOORS": "consequential=0.8,value=0.3"}):
            self.assertEqual(Floors.from_server(None), Floors(0.8, 0.3), "lowering is a local setting")
            self.assertEqual(Floors.from_server({"consequential": 0.85}), Floors(0.85, 0.3))

    def test_chords_that_write_or_remove_are_held_to_it(self):
        """Review of #30: delete, Return and Finder's move were offered as chords and passed ungated."""
        for chord in ("delete", "return", "option+cmd+v", "cmd+backspace", "cmd+v", "cmd+shift+n"):
            self.assertIsNotNone(unsure("KEY", ans("KEY", 0.6, key_target=0.99), ("key_target",), Floors(),
                                        chord=chord), chord)
        for chord in ("cmd+c", "cmd+f", "escape", "tab", "cmd+up"):
            self.assertIsNone(unsure("KEY", ans("KEY", 0.6, key_target=0.99), ("key_target",), Floors(),
                                     chord=chord), chord)

    def test_return_in_a_search_field_and_chord_spelling(self):
        """Review of #30: Return or Delete in a search or name field only confirms or edits it; a chord spelled with
        its modifiers in another order is the same chord."""
        a = ans("KEY", 0.6, key_target=0.99)
        self.assertIsNone(unsure("KEY", a, ("key_target",), Floors(), chord="return",
                                 target={"label": "搜索", "role": "searchField"}))
        self.assertIsNotNone(unsure("KEY", a, ("key_target",), Floors(), chord="return",
                                    target={"label": "回复内容", "role": "textArea"}))
        self.assertIsNotNone(unsure("KEY", a, ("key_target",), Floors(), chord="shift+cmd+s"))
        self.assertIsNotNone(unsure("KEY", a, ("key_target",), Floors(), chord="cmd+option+v"))
        self.assertEqual(policy.chord_of("Shift+Command+S"), "cmd+shift+s")

    def test_a_missing_target_head_is_not_certain(self):
        a = {"operation": {"choice": "CLICK", "probabilities": {"CLICK": 0.97}}}
        self.assertIsNotNone(unsure("CLICK", a, ("click_target",), Floors(), target={"label": "Send", "role": "button"}))

    def test_the_value_floor_reads_the_value_written(self):
        """Review of #30: REPLACE_TEXT writes the first valid pair; when that is not the top pair, its p decides."""
        a = ans("REPLACE_TEXT", 0.97, replace_text_target=0.99, replace_from=0.95, type_text_value=0.95)
        heads = ("replace_text_target", "replace_from", "type_text_value")
        self.assertIsNone(unsure("REPLACE_TEXT", a, heads, Floors()))
        why = unsure("REPLACE_TEXT", a, heads, Floors(), written={"replace_from": 0.05, "type_text_value": 0.05})
        self.assertIn("0.05", why)

    def test_no_probability_is_no_certainty(self):
        """Review of #30: a choice-only answer counted as 1.0, and a value no head chose (the text helper's) skipped
        the value floor."""
        choice_only = {"operation": {"choice": "TYPE_TEXT"}, "type_text_target": {"choice": "1"},
                       "type_text_value": {"choice": "1"}}
        self.assertIsNotNone(unsure("TYPE_TEXT", choice_only, ("type_text_target", "type_text_value"), Floors()))
        helper = ans("TYPE_TEXT", 0.97, type_text_target=0.99)
        self.assertIn("not chosen by the model", unsure("TYPE_TEXT", helper, ("type_text_target", "type_text_value"),
                                                         Floors(), written={"type_text_value": None}))

    def test_the_commit_words_match_brains(self):
        """hands' policy and brain's router decide commits from one list; where brain is importable, they agree."""
        # In a child process: brain on this process's path would change other tests (value candidates use brain's
        # code where it can be imported).
        import json
        import subprocess
        src = os.environ.get("DESKMIND_BRAIN_SRC") or str(REPO.parent / "brain" / "src")
        out = subprocess.run([sys.executable, "-c", "import json, sys; sys.path.insert(0, sys.argv[1]); "
                              "from deskmind_brain import router; print(json.dumps([getattr(router, 'COMMIT_WORDS', None), "
                              "getattr(getattr(router, '_NOT_COMMIT', None), 'pattern', None), "
                              "sorted(getattr(router, '_NOT_ACTIONS', ()))]))",
                              src], capture_output=True, text=True)
        if out.returncode != 0:
            self.skipTest("deskmind_brain not importable")
        brain_words, not_commit, not_actions = json.loads(out.stdout)
        if brain_words is None:
            self.skipTest("this brain predates commit routing (set DESKMIND_BRAIN_SRC to a brain on next)")
        from deskmind_hands.runtime.risk import RISKY_WORDS
        self.assertTrue(set(RISKY_WORDS) <= set(brain_words), set(RISKY_WORDS) - set(brain_words))
        self.assertEqual(set(policy.COMMIT_WORDS), set(brain_words))
        self.assertEqual(policy._NOT_COMMIT.pattern, not_commit, "the look-alikes")
        self.assertEqual(sorted(policy._NOT_ACTIONS), not_actions, "the roles that do not act")


class Loop(unittest.TestCase):
    """On the mock desktop: S01's oracle, with its typed name marked as not sure enough."""

    def run_s01(self, unsure_times: int, approve: bool | None):
        from deskmind_bench.task import load_task
        from deskmind_hands.adapters.scripted import OracleAdapter
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task

        class Doubtful(OracleAdapter):
            def __init__(self):
                super().__init__()
                self.left = unsure_times

            def propose(self, ctx):
                p = super().propose(ctx)
                if p.action.kind.value == "type_text" and self.left:
                    self.left -= 1
                    self._i -= 1            # the same step is proposed again next turn
                    p.unsure = "this step was chosen at 0.80, below 0.90 for a step that writes or commits"
                return p

        class User:
            asked = []

            def respond(self, question, approval, options=()):
                User.asked.append(question)
                return ("好" if approve else "不要", bool(approve), 0.0)

        task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
        cfg = RunConfig(approve_risky=approve is not None, user=User() if approve is not None else None)
        res = run_task(task, MockDriver(render=False), Doubtful(), ws, config=cfg)
        return res, ws, User.asked

    def setUp(self):
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("HANDS_")},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_once_unsure_it_looks_again(self):
        res, ws, asked = self.run_s01(1, None)
        self.assertEqual(res.state.value, "completed", res.failure)
        self.assertEqual(res.metrics.unsure_refusals, 1)
        self.assertEqual([s["kind"] for s in res.steps if s.get("kind") == "refused_unsure"], ["refused_unsure"])
        self.assertTrue((ws.ws / "final.txt").exists())

    def test_twice_unsure_with_nobody_to_ask_it_stops_without_writing(self):
        res, ws, _ = self.run_s01(2, None)
        self.assertEqual(res.state.value, "gave_up")
        self.assertIn("not sure enough", res.failure.detail)
        self.assertFalse((ws.ws / "final.txt").exists())
        self.assertEqual(res.metrics.parse_errors, 0, "not a parse error")

    def test_twice_unsure_the_user_decides(self):
        res, ws, asked = self.run_s01(2, True)
        self.assertEqual(res.state.value, "completed", res.failure)
        self.assertTrue(any("0.80" in q for q in asked), f"the question says what the doubt is: {asked}")
        res, ws, asked = self.run_s01(99, False)   # every time it proposes the write it is unsure; the user says no
        self.assertNotEqual(res.state.value, "completed")
        self.assertFalse((ws.ws / "final.txt").exists())
        self.assertIn("said no", res.failure.detail, "the same step after a no ends the run, not another question")
        self.assertEqual(len(asked), 1, "asked once")


class AfterARefusal(unittest.TestCase):
    setUp = Loop.setUp
    run_s01 = Loop.run_s01

    def test_a_step_the_user_turned_down_is_not_done_when_proposed_again_with_certainty(self):
        """Review of #30: declined while unsure, then proposed again at high p with no doubt, it ran."""
        res, ws, asked = self.run_s01(2, False)      # unsure twice: asked, "no"; the third time it is sure
        self.assertNotEqual(res.state.value, "completed")
        self.assertFalse((ws.ws / "final.txt").exists())
        self.assertIn("said no", res.failure.detail)

    def test_a_refused_step_is_not_read_as_one_that_ran(self):
        """Review of #30: a refused step carried an action, and the next notice said the screen was unchanged and the
        step not to be chosen again -- of a step that never ran."""
        from deskmind_bench.task import load_task
        from deskmind_hands.adapters.scripted import OracleAdapter
        from deskmind_hands.drivers.mock import MockDriver
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        notices = []

        class Once(OracleAdapter):
            refused = False

            def propose(self, ctx):
                notices.append(ctx.notice or "")
                p = super().propose(ctx)
                if p.action.kind.value == "type_text" and not Once.refused:
                    Once.refused = True
                    self._i -= 1
                    p.unsure = "this step was chosen at 0.80, below 0.90 for a step that writes or commits"
                return p
        task = load_task(REPO / "tasks" / "smoke" / "S01-rename.yaml")
        ws = Workspace.create(Path(tempfile.mkdtemp()), REPO / "fixtures")
        res = run_task(task, MockDriver(render=False), Once(), ws, config=RunConfig())
        self.assertEqual(res.state.value, "completed", res.failure)
        after = notices[notices.index(next(n for n in notices if "was not done" in n)):]
        self.assertFalse([n for n in after if "unchanged" in n.lower() or "do not choose it again" in n.lower()
                          or "times in a row" in n], after)


if __name__ == "__main__":
    unittest.main()

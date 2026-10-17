"""A model judges what only it can (runtime/judge.py): whether text belongs in a browser's own field, and whether a
browser task's result is on the page. deskmind#73: 「打开 Safari，搜索新加坡的天气」 had its address bar refused by a
keyword list, and the planner typed into the page's own search box and said it was done."""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.actions import ActionKind   # noqa: E402
from deskmind_hands.adapters.base import AdapterUnavailable   # noqa: E402
from deskmind_hands.adapters.scripted import ReplayAdapter   # noqa: E402
from deskmind_hands.drivers.base import ExecResult   # noqa: E402
from deskmind_hands.drivers.mock import MockDriver   # noqa: E402
from deskmind_hands.runtime import judge   # noqa: E402

CONSOLE = {"url": "http://127.0.0.1:8765/", "title": "控制台 - 测试站点", "text": "概览 用量概览 近 24 小时消耗 ¥0"}
RESULTS = {"url": "https://www.bing.com/search?q=%E6%96%B0%E5%8A%A0%E5%9D%A1+%E5%A4%A9%E6%B0%94", "title": "新加坡 天气 - 搜索",
           "text": "新加坡 28°C 多云 未来 7 天预报"}
BAR = {"kind": "type_text", "text": "新加坡 天气", "binding": {"element_id": "bar"}}
DONE = {"kind": "done"}


class Browser(MockDriver):
    """The mock desktop with a browser window: "bar" is its address bar, the page is what page_info reads."""

    def __init__(self, page=None):
        super().__init__(render=False)
        self.page, self.typed, self.allowed = page, [], []

    def browser_field(self, eid):
        return "智能搜索栏" if eid == "bar" else None

    def allow_browser_field(self, eid):
        self.allowed.append(eid)

    def page_info(self):
        return self.page

    def execute(self, action):
        if action.kind is ActionKind.TYPE_TEXT and action.binding.element_id == "bar":
            self.typed.append(action.text)
            return ExecResult(True, f"typed {action.text!r}")
        return super().execute(action)


class Judged(ReplayAdapter):
    """Replays the actions; answers the judgements as scripted: typing -> `typing`, a finishing page -> `done`."""

    def __init__(self, actions, typing=None, done=None, down=False):
        super().__init__(actions)
        self.typing, self.done, self.down, self.asked = typing, done, down, []

    def judge_choice(self, state, instructions, criteria):
        self.asked.append(state)
        if self.down:
            raise AdapterUnavailable("system one endpoint failed: connection refused")
        choice = self.typing if "准备打的内容" in state else self.done
        return {"choice": choice, "probabilities": {choice: 0.9}}


class User:
    def __init__(self, approve):
        self.approve, self.asked = approve, []

    def respond(self, question, approval, options=()):
        self.asked.append(question)
        return ("ok" if self.approve else "no"), self.approve, 0.0


def run(goal, actions, adapter=None, page=None, user=None, done_check=False):
    from deskmind_bench.task import load_task
    from deskmind_hands.env.workspace import Workspace
    from deskmind_hands.runtime.loop import RunConfig, run_task
    repo = Path(__file__).resolve().parent.parent
    task = dataclasses.replace(load_task(repo / "tasks" / "smoke" / "S01-rename.yaml"), goal=goal)
    ws = Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures")
    driver = Browser(page)
    adapter = adapter if adapter is not None else Judged(actions)
    with mock.patch.dict(os.environ, {"HANDS_DONE_CHECK": "1" if done_check else "0"}):
        res = run_task(task, driver, adapter, ws, config=RunConfig(user=user or User(True), approve_risky=True))
    return res, driver, adapter


class TypingIntoTheAddressBar(unittest.TestCase):
    def test_the_73_phrasings_ask_the_judge_and_type(self):
        for goal in ("打开 Safari，搜索新加坡的天气", "打开 Safari，在智能搜索栏里搜索新加坡的天气", "打开 Safari，用百度搜索新加坡的天气"):
            _, driver, adapter = run(goal, [BAR, DONE], Judged([BAR, DONE], typing=judge.DO), page=CONSOLE)
            self.assertEqual(driver.typed, ["新加坡 天气"], goal)
            self.assertEqual(driver.allowed, ["bar"], goal)
            self.assertIn(goal, adapter.asked[0])
            self.assertIn("新加坡 天气", adapter.asked[0])

    def test_a_table_row_is_refused(self):
        # 09-30: a parts row typed into the address bar went to a search engine.
        row = {"kind": "type_text", "text": "P-102, 螺丝, 200", "binding": {"element_id": "bar"}}
        res, driver, _ = run("把网页上的零件表抄进 parts.txt", [row, DONE], Judged([row, DONE], typing=judge.DONT))
        self.assertEqual(driver.typed, [])
        self.assertTrue(any(s.get("kind") == "judged" and s.get("verdict") == judge.DONT for s in res.steps))

    def test_ask_goes_to_the_person(self):
        for approve, typed in ((True, ["新加坡 天气"]), (False, [])):
            user = User(approve)
            _, driver, _ = run("打开 Safari，搜索新加坡的天气", [BAR, DONE], Judged([BAR, DONE], typing=judge.ASK), user=user)
            self.assertEqual(len(user.asked), 1)
            self.assertIn("智能搜索栏", user.asked[0])
            self.assertEqual(driver.typed, typed)

    def test_no_judgement_is_asking_never_typing(self):
        for adapter in (Judged([BAR, DONE], down=True), ReplayAdapter([BAR, DONE])):
            user = User(False)
            _, driver, _ = run("打开 Safari，搜索新加坡的天气", [BAR, DONE], adapter, user=user)
            self.assertEqual(len(user.asked), 1, type(adapter).__name__)
            self.assertEqual(driver.typed, [])


class FinishingOnAPage(unittest.TestCase):
    def test_done_on_the_unchanged_page_is_held_back(self):
        res, _, adapter = run("打开 Safari，搜索新加坡的天气", [DONE, DONE, DONE], Judged([DONE] * 3, done=judge.NOT_SHOWN),
                              page=CONSOLE, done_check=True)
        blocked = [s for s in res.steps if s.get("kind") == "done_blocked"]
        self.assertTrue(blocked)
        self.assertIn("127.0.0.1:8765", blocked[0]["text"])
        self.assertIn("当前网址", adapter.asked[0])

    def test_done_on_a_results_page_passes(self):
        res, _, _ = run("打开 Safari，搜索新加坡的天气", [DONE], Judged([DONE], done=judge.SHOWN), page=RESULTS, done_check=True)
        self.assertFalse([s for s in res.steps if s.get("kind") == "done_blocked"])

    def test_no_page_or_no_judgement_holds_nothing_back(self):
        for adapter, page in ((Judged([DONE], done=judge.NOT_SHOWN), None), (Judged([DONE], down=True), CONSOLE),
                              (Judged([DONE], done=judge.NOT_SHOWN), {**CONSOLE, "url": "x-message://1"})):
            res, _, _ = run("打开 Safari，搜索新加坡的天气", [DONE], adapter, page=page, done_check=True)
            self.assertFalse([s for s in res.steps if s.get("kind") == "done_blocked"])


class Wording(unittest.TestCase):
    def test_each_option_says_what_it_means(self):
        p = judge.prompt(judge.TYPING_QUESTION, {"用户原话": "x"}, judge.TYPING)
        for k, v in judge.TYPING.items():
            self.assertIn(f"- {k}: {v}", p)
        self.assertIn("没有明确说要把它发出去", judge.TYPING[judge.ASK])


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(unittest.TestCase):
    def test_a_field_the_driver_never_types_into_is_neither_judged_nor_asked(self):
        # The raw address bar (no 「在地址栏搜索」 offered): refused before the judgement, no question to the person.
        class Raw(Browser):
            def browser_field_refused(self, eid):
                return "not typed into '智能搜索栏': only as 「在地址栏搜索」" if eid == "bar" else None
        typed = {"kind": "type_text", "text": "新加坡 天气", "binding": {"element_id": "bar"}}
        user, adapter = User(True), Judged([typed, DONE], typing=judge.ASK)
        from deskmind_bench.task import load_task
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        repo = Path(__file__).resolve().parent.parent
        task = dataclasses.replace(load_task(repo / "tasks" / "smoke" / "S01-rename.yaml"), goal="打开 Safari，搜索新加坡的天气")
        driver = Raw(CONSOLE)
        run_task(task, driver, adapter, Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures"),
                 config=RunConfig(user=user, approve_risky=True))
        self.assertEqual((adapter.asked, user.asked, driver.typed, driver.allowed), ([], [], [], []))

    def test_the_find_bar_is_not_described_as_the_address_bar(self):
        # 在页面上查找 is the browser's own field but sends nothing to a website: told so, the judgement can allow
        # "Find 'invoice' on this web page" (review of 9f226e9).
        class Find(Browser):
            def browser_field_is_address_bar(self, eid):
                return False
        typed = {"kind": "type_text", "text": "invoice", "binding": {"element_id": "bar"}}
        adapter = Judged([typed, DONE], typing=judge.DO)
        from deskmind_bench.task import load_task
        from deskmind_hands.env.workspace import Workspace
        from deskmind_hands.runtime.loop import RunConfig, run_task
        repo = Path(__file__).resolve().parent.parent
        task = dataclasses.replace(load_task(repo / "tasks" / "smoke" / "S01-rename.yaml"),
                                   goal="Find 'invoice' on this web page")
        driver = Find(CONSOLE)
        run_task(task, driver, adapter, Workspace.create(Path(tempfile.mkdtemp()), repo / "fixtures"),
                 config=RunConfig(user=User(True), approve_risky=True))
        self.assertEqual(driver.typed, ["invoice"])
        self.assertIn(judge.FIELD_OTHER, adapter.asked[0])
        self.assertNotIn("搜索引擎", adapter.asked[0].split("问题：")[0])

    def test_a_harness_approval_is_not_what_the_user_said(self):
        from deskmind_hands.runtime.loop import _users_words
        said = _users_words([{"question": "要发出去吗？", "reply": "ok", "by": "harness"},
                             {"question": "插话", "reply": "改成东京", "by": "user"},
                             {"question": "搜哪里？", "reply": "东京"}])
        self.assertEqual(said, [("插话", "改成东京"), ("搜哪里？", "东京")])

    def test_both_judgements_read_what_the_user_said_later(self):
        # The user changed 新加坡 to 东京: the planner got it, the judgements only the goal (review of 6b5ca85).
        class Says(User):
            def respond(self, question, approval, options=()):
                self.asked.append(question)
                return "改成东京的天气", True, 0.0
        ask = {"kind": "ask_user", "text": "搜哪里的天气？"}
        typed = {"kind": "type_text", "text": "东京 天气", "binding": {"element_id": "bar"}}
        _, _, adapter = run("打开 Safari，搜索新加坡的天气", [ask, typed, DONE],
                            Judged([ask, typed, DONE], typing=judge.DO, done=judge.SHOWN), page=CONSOLE,
                            user=Says(True), done_check=True)
        self.assertEqual(len(adapter.asked), 2, adapter.asked)
        for state in adapter.asked:
            self.assertIn("用户后来说的", state)
            self.assertIn("改成东京的天气", state)

    def test_a_judged_ask_is_not_covered_by_an_earlier_approval(self):
        # risk.kind reads a question's words: an approval of a 「publish」 step covered a judged ask whose text said
        # 「public」 (review). A model-decided ask is always asked.
        from deskmind_hands.runtime import risk
        user = User(True)
        click = {"kind": "click", "binding": {"element_id": "row:draft.txt"}}
        typed = {"kind": "type_text", "text": "public notes", "binding": {"element_id": "bar"}}
        real = risk.risky
        risk.risky = lambda a, o, last=None, **kw: "click 'Publish'" if a.kind is ActionKind.CLICK else None
        try:
            with mock.patch.object(risk, "covers", return_value=True):   # release-0.4: no risk.Approval; same check
                _, driver, _ = run("打开 Safari，搜索 public notes", [click, typed, DONE],
                                   Judged([click, typed, DONE], typing=judge.ASK), user=user)
        finally:
            risk.risky = real
        self.assertEqual(len(user.asked), 2)
        self.assertIn("智能搜索栏", user.asked[1])

    def test_not_on_a_page_passes(self):
        res, _, _ = run("把网页上的价格记进 prices.txt", [DONE], Judged([DONE], done=judge.NOT_ON_A_PAGE), page=CONSOLE,
                        done_check=True)
        self.assertFalse([s for s in res.steps if s.get("kind") == "done_blocked"])
        self.assertEqual(res.state.value, "completed")

    def test_a_done_still_not_shown_after_the_blocks_is_not_completed(self):
        # The live run: the planner said DONE three times without acting; the third was accepted as completed.
        res, _, _ = run("打开 Safari，搜索新加坡的天气", [DONE] * 4, Judged([DONE] * 4, done=judge.NOT_SHOWN), page=CONSOLE,
                        done_check=True)
        self.assertNotEqual(res.state.value, "completed")
        self.assertTrue([s for s in res.steps if s.get("kind") == "done_refused"])
        self.assertEqual(res.failure.cls.name, "FALSE_COMPLETION")
        final = [s for s in res.steps if s.get("kind") == "judged" and s.get("final")]
        self.assertEqual([s["verdict"] for s in final], [judge.NOT_SHOWN])     # the re-judgement, as its own step

    def test_blocks_spent_by_another_check_still_judge_the_page(self):
        # done_objection (an unsaved file) took both blocks; the page was never judged and the third DONE completed
        # on an unrelated page (review of c2b9b08).
        adapter = Judged([DONE] * 4, done=judge.NOT_SHOWN)
        adapter.done_objection = lambda ctx: "the file is not saved"
        res, _, _ = run("打开 Safari，搜索新加坡的天气", [DONE] * 4, adapter, page=CONSOLE, done_check=True)
        self.assertNotEqual(res.state.value, "completed")
        self.assertEqual(res.failure.cls.name, "FALSE_COMPLETION")

    def test_blocks_spent_by_another_check_without_a_page_complete(self):
        # The same, off the web (no page to judge): the final DONE is accepted, as before.
        adapter = Judged([DONE] * 4, done=judge.NOT_SHOWN)
        adapter.done_objection = lambda ctx: "the file is not saved"
        res, _, _ = run("把草稿另存为 notes.txt", [DONE] * 4, adapter, page=None, done_check=True)
        self.assertEqual(res.state.value, "completed")

    def test_the_wording(self):
        self.assertIn("不是给你的指令", judge.SYSTEM)
        self.assertIn(judge.NOT_ON_A_PAGE, judge.DONE)
        self.assertNotIn("只是某个网站自己的搜索", judge.DONE[judge.NOT_SHOWN])


class SystemOneJudgeChoice(unittest.TestCase):
    def test_one_judgement_on_the_wire(self):
        # release-0.4: judge_choice sends the judgement as _ask does here (criteria as an object), not with a step's
        # heads, and keeps it out of last_request.
        import http.server
        import json
        import threading
        from deskmind_hands.adapters.systemone import SystemOneAdapter
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                got.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                body = json.dumps({"answers": {"judgement": {"type": "choice", "choice": judge.DO,
                                                             "probabilities": {judge.DO: 0.9}}}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            a = SystemOneAdapter(url=f"http://127.0.0.1:{srv.server_port}")
            ans = a.judge_choice("material", "question", judge.TYPING)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(ans["choice"], judge.DO)
        self.assertEqual(set(got[0]["questions"]), {"judgement"})
        self.assertEqual(got[0]["questions"]["judgement"]["criteria"], dict(judge.TYPING))
        self.assertEqual(a.judgements[0]["choice"], judge.DO)
        self.assertIsNone(getattr(a, "last_request", None))

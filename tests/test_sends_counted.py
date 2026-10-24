"""What a step sent is counted where it is sent (review of hands#64 round 7).

Three rounds found an input that went and was not counted, each at a route's hand-on point: the background route's
click and paste, the accessibility insertion, the accessibility focus write that did not read back. Counting moved to
the send primitives (base.counted / ax_counted): every one counts itself before it calls the OS; only an answer that
definitely means "not delivered" (base.AX_NOT_DELIVERED) takes it back. A refusal becomes performed:false only while
the step counted nothing (ExecResult.__post_init__, the one place).

The table below walks every primitive, then refuses: performed:false exactly when the primitive is not a send or the
OS said it was not delivered. A source scan checks that no OS call that reaches an app bypasses the counted wrappers.
Fakes only: the OS is a fake ApplicationServices / Quartz / subprocess. Fails on 158f1f9 (no counting there: the
focus write that does not read back, the CannotComplete write, an accessibility action, a scripted send, all left
performed:false)."""
from __future__ import annotations

import ast
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskmind_hands.drivers import axfield, axpopup, base, skylight   # noqa: E402
from deskmind_hands.drivers.base import USER_BUSY, ExecResult, NothingSent, input_scope   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver, _post, _sh   # noqa: E402

OK, CANNOT_COMPLETE, FAILURE, INVALID, UNSUPPORTED, API_DISABLED = 0, -25204, -25200, -25202, -25205, -25211


def fake_ax(code=OK, focused=False, raises=False):
    """ApplicationServices with every write and action answering `code` (or raising); reads say `focused`."""
    calls = []

    def write(el, attr, value):
        calls.append(("set", attr, value))
        if raises:
            raise RuntimeError("the app did not answer")
        return code

    def action(el, name):
        calls.append(("act", name))
        if raises:
            raise RuntimeError("the app did not answer")
        return code
    m = types.ModuleType("ApplicationServices")
    m.AXUIElementSetAttributeValue = write
    m.AXUIElementPerformAction = action
    m.AXUIElementCopyAttributeValue = lambda el, attr, _: (0, focused if attr == "AXFocused" else None)
    m.AXUIElementSetMessagingTimeout = lambda el, t: 0
    m.AXUIElementCreateApplication = lambda pid: ("app", pid)
    return m, calls


def refused_after(send) -> ExecResult:
    """`send()` inside one step, then a refusal that says nothing was sent."""
    with input_scope():
        try:
            send()
        except Exception:   # noqa: BLE001 -- a send that throws half way is still a send
            pass
        return ExecResult(False, NothingSent("only responds in the foreground, which this run may not use"))


class FakeProc:
    stdout, stderr, returncode = '{"success": true, "data": {}}', "", 0


def driver():
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.bin, d.timeout_s, d._mcp, d.app = "peekaboo", 5, None, "com.example.app"
    return d


class PrimitiveTable(unittest.TestCase):
    def ax(self, code=OK, raises=False, focused=False):
        m, calls = fake_ax(code, focused=focused, raises=raises)
        p = mock.patch.dict(sys.modules, {"ApplicationServices": m})
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_the_table(self):
        q = types.SimpleNamespace(CGEventPost=lambda tap, ev: None)            # Quartz, the foreground route
        rows = [
            # (primitive, outcome, OS answer, counted?)
            ("event post", "any", lambda: _post(q, 0, "ev"), None, True),
            ("AX write: focus", "success, not read back", lambda: axfield.focus("el", wait=0), OK, True),
            ("AX write: selected text", "success", lambda: axfield._set("el", "AXSelectedText", "x"), OK, True),
            ("AX write", "CannotComplete (a timeout)", lambda: axfield._set("el", "AXFocused", True), CANNOT_COMPLETE, True),
            ("AX write", "Failure", lambda: axfield._set("el", "AXFocused", True), FAILURE, True),
            ("AX write", "raises", lambda: axfield._set("el", "AXFocused", True), "raise", True),
            ("AX write", "InvalidUIElement", lambda: axfield._set("el", "AXFocused", True), INVALID, False),
            ("AX write", "AttributeUnsupported", lambda: axfield._set("el", "AXSelectedText", "x"), UNSUPPORTED, False),
            ("AX write", "APIDisabled", lambda: axfield._set("el", "AXFocused", True), API_DISABLED, False),
            ("AX action: press", "success", lambda: axpopup._act("el", "AXPress"), OK, True),
            ("AX action: press", "CannotComplete", lambda: axpopup._act("el", "AXPress"), CANNOT_COMPLETE, True),
            ("AX action: press", "ActionUnsupported", lambda: axpopup._act("el", "AXPress"), -25206, False),
            ("scripted send: peekaboo click", "any", lambda: driver()._run("click_on", element="e1", snapshot="s"), None, True),
            ("scripted send: AppleScript", "any", lambda: driver()._osa('tell application "Finder" to make new folder'), None, True),
            ("scripted send: open a document", "any", lambda: _sh(["/usr/bin/open", "-g", "-a", "TextEdit", "/tmp/x"]), None, True),
            ("read: peekaboo see", "any", lambda: driver()._run("see", app="x", path="/tmp/p"), None, False),
            ("read: AppleScript", "any", lambda: driver()._osa_read('tell application "Finder" to count windows'), None, False),
            ("bring forward: AppleScript activate", "any", lambda: driver()._osa_forward('tell application "X" to activate'), None, False),
            ("bring forward: open -b", "any", lambda: _sh(["/usr/bin/open", "-b", "x"], sends=False), None, False),
            ("bring forward: peekaboo window focus", "any", lambda: driver()._run("window_focus", window="1"), None, False),
            ("key window: AXFocusedWindow", "success", lambda: skylight._set_focused_window(1, 2), OK, False),
            ("bring forward: AXFrontmost", "success", lambda: PeekabooDriver._make_frontmost(driver(), "x"), OK, False),
        ]
        for name, outcome, send, code, counts in rows:
            with self.subTest(f"{name}: {outcome}"):
                self.ax(OK if code in (None, "raise") else code, raises=code == "raise")
                with mock.patch("subprocess.run", lambda *a, **k: FakeProc()), \
                        mock.patch.object(PeekabooDriver, "_app_pid", lambda self, b: 7, create=True):
                    res = refused_after(send)
                self.assertIs(res.performed, None if counts else False, res.detail)
                self.assertEqual(res.indeterminate, counts)

    def test_key_and_mouse_posts_to_a_process(self):
        """The background routes' posts: a key through SkyLight's post (and CGEventPostToPid without it), a click
        through BackgroundQuartz -- each counted."""
        quartz = types.ModuleType("Quartz")
        quartz.CGEventPostToPid = lambda pid, ev: None
        fns = {"set_int": lambda p, f, v: None, "post": lambda pid, p: None}
        with mock.patch.dict(sys.modules, {"Quartz": quartz}), mock.patch.object(skylight, "_ptr", lambda ev: ev), \
                mock.patch.object(skylight, "_authenticate", lambda f, p, pid: False):
            for name, load in (("SkyLight post", lambda: fns), ("CGEventPostToPid", lambda: {})):
                with self.subTest(name), mock.patch.object(skylight, "_load", load):
                    self.assertIsNone(refused_after(lambda: skylight.post_key_event(1, "ev")).performed)

    def test_the_reviewers_repro_a_focus_write_accepted_and_not_read_back(self):
        """rv10 hands_focus_sent_not_read_back: the typing route writes AXFocused, the app accepts, the field does not
        read back focused in time; the route hands the step on, and the next route's refusal must not be "nothing"."""
        import test_background_keys as BK
        calls = []

        def enter(pid, wid):
            # from the window key on, the real focus() over an OS that accepts the write and never reads back focused
            # (assigned, not patched: run_type's own patch of focus puts the real one back when it ends)
            m, c = fake_ax(OK, focused=False)
            calls.append(c)
            p = mock.patch.dict(sys.modules, {"ApplicationServices": m})
            p.start()
            self.addCleanup(p.stop)
            axfield.focus = REAL_FOCUS
            return BK.Key([])
        t = BK.TypeText()
        t._cleanups = []
        with input_scope():
            field = BK.Field("start", takes_insertion=False)
            field.focused = False                                       # never reads back focused in time
            d, typed = t.run_type(field, enter=enter, insert=lambda el, txt, r: False)
            later = ExecResult(False, NothingSent("only responds in the foreground, which this run may not use"))
        self.assertIn(("set", "AXFocused", True), calls[0])
        self.assertIsNone(typed)                                         # handed on
        self.assertIsNot(later.performed, False)
        self.assertTrue(later.indeterminate)

    def test_a_real_background_quartz_counts_the_keys_it_sends_and_not_the_one_it_refuses(self):
        """H4: keys only -- cmd+A goes (down, up), cmd+V is refused before it is posted."""
        import test_background_keys as BK
        posted = []
        key = skylight.WindowKey(BK.PID, BK.TARGET, BK.PREV, "psn", "records")
        key.lost = lambda: None
        q, _ = BK.background_quartz(key)
        with mock.patch.object(skylight, "_load", lambda: None), \
                mock.patch.object(skylight, "post_key_event", lambda pid, ev, auth=True: base.counted(lambda: posted.append(ev))), \
                mock.patch.object(PeekabooDriver, "_key_event",
                                  classmethod(lambda cls, Q, code, down: Q.CGEventCreateKeyboardEvent(None, code, down))), \
                input_scope():
            PeekabooDriver._post_key(q, 0, q.kCGEventFlagMaskCommand)
            self.assertEqual(base.sent_in_step(), 2)
            key.lost = lambda: "window 200 is no longer the app's key window"
            with self.assertRaises(skylight.WindowKeyRefused):
                PeekabooDriver._post_key(q, 9, q.kCGEventFlagMaskCommand)
            self.assertEqual(base.sent_in_step(), 2)
        self.assertEqual(len(posted), 2)

    def test_a_user_busy_refusal_after_a_send_is_unverified(self):
        """H10's guard, now at the one place: words of USER_BUSY make the result deferred, but after a send it is
        indeterminate and never performed:false."""
        with input_scope():
            base.counted(lambda: None)
            r = ExecResult(False, NothingSent(f"{USER_BUSY}; the step was not carried out before the caller's deadline"))
        self.assertTrue(r.indeterminate)
        self.assertIsNone(r.performed)

    def test_outside_a_step_nothing_is_claimed_and_a_step_starts_at_zero(self):
        """Fail-safe: with no step open, a refusal is never performed:false (round 8)."""
        base.counted(lambda: None)
        self.assertEqual(base.sent_in_step(), 0)
        self.assertIsNone(ExecResult(False, NothingSent("x")).performed)
        with input_scope():
            self.assertEqual(base.sent_in_step(), 0)
            with input_scope():                                     # nested: the same step
                base.counted(lambda: None)
            self.assertEqual(base.sent_in_step(), 1)

    def test_a_refusal_after_a_send_keeps_no_not_done_flag_or_words(self):
        """rv11 flags_kept: a stale or user-busy refusal after a send was unverified but still stale / deferred and
        "nothing was done" -- the hands loop then said "Nothing changed. Take it again"."""
        with input_scope():
            base.counted(lambda: None)
            stale = ExecResult(False, "the window moved since it was seen; observe again", stale=True)
            busy = ExecResult(False, NothingSent(f"{USER_BUSY}; the step was not carried out before the caller's "
                                                 f"deadline -- nothing was done; wait"))
        for r in (stale, busy):
            with self.subTest(r.detail):
                self.assertEqual((r.indeterminate, r.stale, r.deferred, r.unsupported, r.performed), (True, False, False, False, None))
                self.assertEqual(r.detail, base.SENT_THEN_STOPPED)       # one fixed sentence, nothing of the refusal
                self.assertNotRegex(r.detail.lower(), r"nothing was done|not carried out|nothing changed|user is active|not done")
                self.assertTrue(r.refused_reason)                           # the refusal's words: aside, for the trace
        from deskmind_hands.errors import Kind, classify
        self.assertIsNot(classify(busy.detail), Kind.USER_BUSY)

    def test_each_thread_and_each_step_counts_its_own(self):
        """rv11 global_count: a read under uncounted() on another thread silenced this step's sends, and another
        thread's step closing restored this one's count to 0."""
        import threading
        go, done = threading.Event(), threading.Event()

        def reader():
            with base.uncounted():
                go.set()
                done.wait()
        t = threading.Thread(target=reader)
        t.start()
        go.wait()
        with input_scope():
            base.counted(lambda: None)
            r = ExecResult(False, NothingSent("only responds in the foreground"))
        done.set()
        t.join()
        self.assertIsNone(r.performed)
        go2, done2 = threading.Event(), threading.Event()

        def other_step():
            with input_scope():
                go2.set()
                done2.wait()
        with input_scope():
            t2 = threading.Thread(target=other_step)
            t2.start()
            go2.wait()
            base.counted(lambda: None)
            done2.set()
            t2.join()
            r2 = ExecResult(False, NothingSent("only responds in the foreground"))
        self.assertIsNone(r2.performed)


REAL_FOCUS = axfield.focus


class Round9(unittest.TestCase):
    def test_no_refusal_word_survives_a_send(self):
        """rv12 repro_words: the user-busy sentence, "-- wait" and the browser's "not done: ..." survived the filter."""
        cases = [
            NothingSent(f"{USER_BUSY}; the step was not carried out before the caller's deadline (the Mac was not still "
                        f"in time) -- nothing was done; wait"),
            NothingSent(f"{USER_BUSY}; the step was not carried out -- wait"),
            NothingSent("not done: com.google.chrome is brought to the front only for the run's own window, when this "
                        "step is allowed to; the step could not be delivered in the background"),
        ]
        for words in cases:
            with self.subTest(words), input_scope():
                base.counted(lambda: None)
                r = ExecResult(False, words)
                self.assertEqual(r.detail, base.SENT_THEN_STOPPED)
                self.assertEqual(r.refused_reason, str(words))
                self.assertEqual((r.indeterminate, r.deferred, r.performed), (True, False, None))
                for bad in ("the user is active", "not done", "wait"):
                    self.assertNotIn(bad, r.detail.lower())

    def flash_retry(self, window_on_screen: bool):
        """rv12 repro_flash_retry: the Mac still, the app brought forward, the user touches the mouse as it comes
        (retry), then keeps working until the wait runs out."""
        import itertools
        import os
        from deskmind_hands.drivers import peekaboo as P
        q = types.ModuleType("Quartz")
        q.CGEventGetLocation = lambda e: (0, 0)
        q.CGEventCreate = lambda x: None
        q.CGWarpMouseCursorPosition = lambda p: None
        q.CGAssociateMouseAndMouseCursorPosition = lambda b: None
        opened = []
        d = P.PeekabooDriver.__new__(P.PeekabooDriver)
        d.caller_deadline, d._foreground_actions = None, 0
        d._background_input = lambda b: False
        d._takeover = lambda: False
        fronts = itertools.chain(["com.apple.finder", "com.example.chat"], itertools.repeat("com.apple.finder"))
        d._front_bundle = lambda: next(fronts)
        idle = iter([10.0, 0.0])
        d._hid_idle = lambda: next(idle, 0.0)
        d._announce_wait = lambda w, b="", what="": True
        d._resumed = lambda: None
        d._front_taken = lambda b, t: None
        d._front_given_back = lambda tok: None
        d._give_front_back = lambda prev, ours=None, late_s=0.0: None
        d._has_window_on_screen = lambda b: window_on_screen
        d._activate_without_reopen = lambda b: True
        with mock.patch.dict(sys.modules, {"Quartz": q}), \
                mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": "com.example.chat", "HANDS_FLASH_WAIT_S": "2"}), \
                mock.patch("subprocess.run", lambda cmd, **k: opened.append(cmd) or FakeProc()), \
                mock.patch.object(P, "_ascii_input_source", lambda: None), input_scope():
            ok, detail = d._flash("com.example.chat", lambda Q: self.fail("sent"), "click")
            return ExecResult(ok, detail), opened

    def test_a_busy_user_with_the_apps_window_on_screen_is_user_busy_and_nothing_done(self):
        r, opened = self.flash_retry(window_on_screen=True)
        self.assertEqual(opened, [])                                     # activated, no `open -b`
        self.assertEqual((r.deferred, r.performed, r.indeterminate), (True, False, False), r.detail)

    def test_with_no_window_on_screen_the_reopen_counts(self):
        r, opened = self.flash_retry(window_on_screen=False)
        self.assertIn(["/usr/bin/open", "-b", "com.example.chat"], opened)
        self.assertEqual((r.indeterminate, r.performed, r.deferred), (True, None, False), r.detail)


class TheRoutesUseTheCountedPrimitives(unittest.TestCase):
    """Mutations the table did not reach (rv11): the step's scope in execute (M3), the session's set_value (M6),
    a document opened (M13), a scripted save (M14), a reopen and a launch."""

    def test_execute_opens_the_steps_scope(self):
        d = driver()
        d._require_session = lambda: None
        d._browser_field_allowed = None
        seen = {}

        def outer(action):
            seen["in_step"] = base.in_step()
            return ExecResult(False, NothingSent("only responds in the foreground"))
        d._execute_outer = outer
        from deskmind_hands.actions import Action, ActionKind
        res = d.execute(Action(kind=ActionKind.WAIT, duration_s=0.1))
        self.assertTrue(seen["in_step"])
        self.assertIs(res.performed, False)                       # in a step that sent nothing: nothing done

    def test_the_sessions_calls(self):
        d = driver()
        d._mcp = types.SimpleNamespace(tool=lambda name, args: (True, {}, "", ""))
        for tool, sends in (("set_value", True), ("click", True), ("see", False), ("menu", False)):
            with self.subTest(tool), input_scope():
                d._mcp_call(tool, {}, sends=sends) if not sends else d._mcp_call(tool, {})
                self.assertEqual(base.sent_in_step(), 1 if sends else 0)

    def test_a_set_value_through_the_session_is_counted(self):
        """The MCP set_value route of _execute_inner (M6): reached with a fake session."""
        import inspect
        from deskmind_hands.drivers import peekaboo
        src = inspect.getsource(peekaboo.PeekabooDriver._execute_inner)
        self.assertIn('self._mcp_call("set_value"', src)
        calls = [m.start() for m in __import__("re").finditer(r"self\._mcp\.tool\(", inspect.getsource(peekaboo))]
        wrapper = inspect.getsource(peekaboo.PeekabooDriver._mcp_call)
        self.assertEqual(len(calls), wrapper.count("self._mcp.tool("))   # only _mcp_call reaches the session

    def test_opening_a_document_is_a_send(self):
        import tempfile
        from deskmind_hands.drivers import peekaboo
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "a.txt").write_text("x")
            d = driver()
            d.ws, d._app_name, d._window_id, d._pinned_window = Path(ws), "TextEdit", "1", None
            d._scripted_actions = 0
            d._opener = lambda p: "com.apple.TextEdit"
            d._window_titled_once = lambda name: None
            d._window_titled = lambda name, tries=2: "9"
            ids = iter([set(), {"9"}])
            d._window_ids = lambda: next(ids, {"9"})
            with mock.patch("subprocess.run", lambda *a, **k: FakeProc()), \
                    mock.patch.object(peekaboo.time, "sleep", lambda s: None), input_scope():
                try:
                    d._open_document("a.txt")
                except Exception:   # noqa: BLE001 -- what follows the open is not this test's
                    pass
                self.assertEqual(base.sent_in_step(), 1)

    def test_a_scripted_save_is_a_send(self):
        from deskmind_hands.drivers import peekaboo

        class Out:
            returncode, stderr = 0, ""

            def __init__(self, cmd):
                self.stdout = "false" if "get modified" in cmd[-1] else ""
        d = driver()
        d._scripted_actions = 0
        with mock.patch("subprocess.run", lambda cmd, **k: Out(cmd)), \
                mock.patch.object(peekaboo.time, "sleep", lambda s: None), input_scope():
            res = d._textedit_save("a.txt")
            self.assertEqual(base.sent_in_step(), 1)                # the save; the reads of `modified` are not sends
        self.assertTrue(res.ok)

    def test_a_reopen_and_a_launch_are_sends(self):
        d = driver()
        d._has_window_on_screen = lambda bundle: False                 # no window: the reopen makes one
        with mock.patch("subprocess.run", lambda *a, **k: FakeProc()):
            with input_scope():
                d._bring_forward("com.example.app", browser=False)   # `open -b`: a reopen
                self.assertEqual(base.sent_in_step(), 1)
            for key in ("activate", "activate_by_name"):
                with self.subTest(key), input_scope():
                    d._run(key, app="com.example.app")
                    self.assertEqual(base.sent_in_step(), 1)


class NoSendBypassesTheCount(unittest.TestCase):
    """A source scan of the whole package (AST): every OS call that can reach an app -- an accessibility write or
    action, an event post (also taken as a value: `post = Q.CGEventPost`), SkyLight's post, NSAppleScript or
    NSWorkspace's open / launch, and any subprocess call (run, Popen, call, check_call, check_output, ...: a shell,
    osascript, open, the peekaboo binary) -- sits in a function listed below by its qualified name, with why. Nothing
    else may make one: a new send goes through a counted wrapper (_sh, _run, _post, _mcp_call, ...), or is listed here
    with its reason. A test aid: simple on purpose, strict by default (review of hands#64 round 8)."""

    SENDS = {"AXUIElementSetAttributeValue", "AXUIElementPerformAction", "CGEventPost", "CGEventPostToPid",
             "AXUIElementPostKeyboardEvent", "NSAppleScript", "openURL_", "openURLs_withApplicationAtURL_configuration_completionHandler_",
             "openApplicationAtURL_configuration_completionHandler_", "launchApplication_", "openFile_",
             "openFile_withApplication_", "launchAppWithBundleIdentifier_options_additionalEventParamDescriptor_launchIdentifier_",
             "activateWithOptions_"}
    SUBPROCESS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    #: (module, qualified function, what): why it may be made there
    EXEMPT = {
        ("deskmind_hands.drivers.axfield", "_set", "AXUIElementSetAttributeValue"): "the counted wrapper (ax_counted)",
        ("deskmind_hands.drivers.axpopup", "_act", "AXUIElementPerformAction"): "the counted wrapper (ax_counted)",
        ("deskmind_hands.drivers.peekaboo", "_post_one", "CGEventPost"): "the counted wrapper of every event post (_post marks presses and releases around it)",
        ("deskmind_hands.drivers.skylight", "post_key_event", "CGEventPostToPid"): "counted around the post",
        ("deskmind_hands.drivers.skylight", "post_key_event", 'fns["post"]'): "counted around the post",
        ("deskmind_hands.drivers.skylight", "BackgroundQuartz.CGEventPost", "CGEventPostToPid"): "counted around the post",
        ("deskmind_hands.drivers.skylight", "BackgroundQuartz.CGEventPost", 'fns["post"]'): "counted around the post",
        ("deskmind_hands.drivers.skylight", "_post_record", 'fns["post_rec"]'): "focus records: a key-window change",
        ("deskmind_hands.drivers.skylight", "_set_focused_window", "AXUIElementSetAttributeValue"): "AXFocusedWindow: a key-window change",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._make_frontmost", "AXUIElementSetAttributeValue"): "AXFrontmost: a front, no reopen",
        ("deskmind_hands.executor", "HandsExecutor._make_frontmost", "AXUIElementSetAttributeValue"): "AXFrontmost: a front, no reopen",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._activate_without_reopen", "activateWithOptions_"): "activation without a reopen: a front, for an app with a window on screen",
        ("deskmind_hands.drivers.peekaboo", "_sh", "subprocess.run"): "the counted wrapper of commands",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._run", "subprocess.run"): "the counted wrapper of peekaboo commands",
        ("deskmind_hands.drivers.peekaboo", "_MCPSession.__init__", "subprocess.Popen"): "starts the peekaboo session; its calls go through _mcp_call",
        ("deskmind_hands.drivers.peekaboo", "_app_running", "subprocess.run"): "pgrep: reads the process list",
        ("deskmind_hands.drivers.peekaboo", "hid_idle_seconds", "subprocess.run"): "ioreg: reads the idle time",
        ("deskmind_hands.drivers.peekaboo", "_ascii_input_source.__init__", "subprocess.Popen"): "the input-source child: switches this Mac's input source, no app",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._ascii_input", "subprocess.run"): "reads the input source",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._load_keys", "subprocess.run"): "reads the keyboard layout",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._set_clipboard", "subprocess.run"): "pbcopy: the clipboard, no app",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver.clipboard", "subprocess.run"): "pbpaste: reads the clipboard",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._stage", "subprocess.run"): "a task's stage command, before any step (the gym)",
        ("deskmind_hands.drivers.peekaboo", "PeekabooDriver._stage_url", "subprocess.run"): "a task's stage command, before any step (the gym)",
        ("deskmind_hands.drivers.capture", "capture_screencapture", "subprocess.run"): "screencapture: reads the screen",
        ("deskmind_hands.drivers.skylight", "auth_safe", "subprocess.run"): "a self-test child, no app",
        ("deskmind_hands.axdocument", "_osa", "subprocess.run"): "reads a window's document (get only)",
        ("deskmind_hands.vision", "ocr", "subprocess.run"): "text recognition of a picture",
        ("deskmind_hands.cli", "_verify_effect", "subprocess.run"): "a task's oracle check, after the run",
        ("deskmind_hands.record.recorder", "environment_manifest._git", "subprocess.run"): "git: reads the checkout",
        ("deskmind_hands.inputwatch", "track_children", "subprocess.Popen"): "wraps Popen to record children's pids",
        ("deskmind_hands.inputwatch", "track_children._Tracked", "subprocess.Popen"): "wraps Popen to record children's pids",
        # the executor's own window commands: outside any driver step, and no reply of them claims performed:false
        ("deskmind_hands.executor", "HandsExecutor._pk", "subprocess.run"): "peekaboo window list / close (close_window's reply never says performed:false)",
        ("deskmind_hands.executor", "HandsExecutor._open_window_now", "subprocess.run"): "open_window: its replies never say performed:false after it",
        ("deskmind_hands.executor", "HandsExecutor._new_document", "subprocess.run"): "open_window: its replies never say performed:false after it",
        ("deskmind_hands.executor", "HandsExecutor._navigate", "subprocess.run"): "open_window: its replies never say performed:false after it",
        ("deskmind_hands.executor", "HandsExecutor._page_of", "subprocess.run"): "reads a browser window's address",
    }

    @classmethod
    def findings(cls, root: Path):
        out = []
        for path in sorted(root.rglob("*.py")):
            mod = str(path.relative_to(root.parent)).replace("/", ".")[:-3]
            tree = ast.parse(path.read_text())

            def walk(node, q):
                for ch in ast.iter_child_nodes(node):
                    if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        walk(ch, f"{q}.{ch.name}" if q else ch.name)
                        continue
                    what = None
                    if isinstance(ch, ast.Attribute) and ch.attr in cls.SENDS:
                        what = ch.attr
                    elif isinstance(ch, ast.Name) and ch.id in cls.SENDS and isinstance(ch.ctx, ast.Load):
                        what = ch.id
                    elif isinstance(ch, ast.Subscript) and isinstance(ch.slice, ast.Constant) and ch.slice.value in ("post", "post_rec"):
                        what = f'fns["{ch.slice.value}"]'
                    elif isinstance(ch, ast.Attribute) and ch.attr in cls.SUBPROCESS and isinstance(ch.value, ast.Name) \
                            and ch.value.id == "subprocess":
                        what = f"subprocess.{ch.attr}"
                    if what:
                        out.append((mod, q or "<module>", what, f"{path.name}:{ch.lineno}"))
                    walk(ch, q)
            walk(tree, "")
        return out

    def test_every_os_send_is_counted_or_listed_with_why(self):
        bad = [f"{where} {what} in {mod}.{fn}" for mod, fn, what, where in self.findings(PACKAGE)
               if (mod, fn, what) not in self.EXEMPT]
        self.assertEqual(bad, [])

    def test_the_scan_catches_the_reviewers_four_planted_sends(self):
        """rv11 scan_holes.sh: a peekaboo binary call, an osascript check_output, an aliased event post and a shell
        open, planted in a driver module."""
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "deskmind_hands"
            shutil.copytree(PACKAGE, pkg, ignore=shutil.ignore_patterns("__pycache__"))
            with open(pkg / "drivers" / "axchrome.py", "a") as f:
                f.write(PLANTED)
            planted = [x for x in self.findings(pkg) if x[1].startswith("_rv11_") and (x[0], x[1], x[2]) not in self.EXEMPT]
        self.assertEqual(sorted({x[1] for x in planted}), ["_rv11_alias_post", "_rv11_osascript_send",
                                                           "_rv11_peekaboo_click", "_rv11_shell_open"])


PACKAGE = Path(__file__).resolve().parent.parent / "deskmind_hands"
PLANTED = """


def _rv11_peekaboo_click(self):
    import subprocess
    subprocess.run([self.bin, "click", "--on", "B1", "--json"])


def _rv11_osascript_send():
    import subprocess
    subprocess.check_output(["/usr/bin/osascript", "-e", 'tell application "Mail" to send (first outgoing message)'])


def _rv11_alias_post(Q, ev):
    post = Q.CGEventPost
    post(Q.kCGHIDEventTap, ev)


def _rv11_shell_open():
    import subprocess
    subprocess.run(["/bin/sh", "-c", "open -b com.apple.mail"])
"""


if __name__ == "__main__":
    unittest.main()


def step(by):
    """A step with this deadline (on a head without one, a plain step: the test then shows what was sent late)."""
    try:
        return input_scope(by=by)
    except TypeError:
        return input_scope()


class DeadlineAtTheSend(unittest.TestCase):
    """Codex P1 on hands#64 @5042e94: the caller's deadline was checked before the app was raised and the front
    polled, not at the send -- the chat click went out at 27.25 s against 25 s, the address bar's click and paste at
    121 s against 120 s, both answered ok. Now every counted send checks it right before the OS call (base.counted)."""

    class Clock:
        def __init__(self):
            self.t = 1_000_000.0

        def time(self):
            return self.t

        def monotonic(self):
            return self.t

        def sleep(self, s):
            self.t += max(0.0, s)

    def test_an_expired_step_makes_no_os_call(self):
        calls = []
        with input_scope(by=0.0):                                     # long past
            with self.assertRaises(base.StepExpired):
                base.counted(lambda: calls.append("posted"))
            with self.assertRaises(base.StepExpired):
                base.ax_counted(lambda: calls.append("written") or 0)
            with base.uncounted():                                      # a read is no send: not refused
                base.counted(lambda: calls.append("read"))
        self.assertEqual(calls, ["read"])

    def test_the_chat_click_is_not_made_past_the_deadline(self):
        """The chat app comes forward slowly; the deadline (less the margin) passes while the front is polled."""
        import os
        from deskmind_hands.drivers import peekaboo as P
        from deskmind_hands.geometry import Point, Rect
        clock = self.Clock()
        posted = []
        q = types.ModuleType("Quartz")
        q.kCGEventMouseMoved, q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGHIDEventTap, q.kCGMouseButtonLeft = 1, 2, 3, 0, 0
        q.CGEventCreateMouseEvent = lambda src, kind, pt, btn: kind
        q.CGEventPost = lambda tap, ev: posted.append((clock.t, ev))
        q.CGEventGetLocation = lambda e: (0, 0)
        q.CGEventCreate = lambda x: None
        q.CGWarpMouseCursorPosition = lambda p: None
        q.CGAssociateMouseAndMouseCursorPosition = lambda b: None
        d = P.PeekabooDriver.__new__(P.PeekabooDriver)
        d.app, d._foreground_actions, d._chat_sends, d._snapshot = P.CHAT_APP, 0, 0, None
        d._labels_by_id, d._rects, d._origin = {"b1": "表情"}, {"b1": Rect(10, 10, 20, 20)}, Point(0, 0)
        d._moved_since_seen = lambda: None
        d._takeover = lambda: False
        d._raise_app = lambda b: True                                   # activated: no send
        fronts = iter(["com.googlecode.iterm2"] + ["com.googlecode.iterm2"] * 20 + [P.CHAT_APP] * 50)

        def front():
            clock.sleep(0.5)                                            # each read of the front takes a while
            return next(fronts)
        d._front_bundle = front
        d._front_taken = lambda b, t: None
        d._front_given_back = lambda tok: None
        d._give_front_back = lambda prev, ours=None, late_s=0.0: None
        d.caller_deadline = clock.t + base.DEADLINE_MARGIN_S + 5       # 5 s to act in
        with mock.patch.dict(sys.modules, {"Quartz": q}), mock.patch.dict(os.environ, {"HANDS_CHAT_FLASH": "1"}), \
                mock.patch.object(P, "time", clock), mock.patch.object(base, "time", clock), \
                mock.patch.object(P, "hid_idle_seconds", lambda: 10.0), mock.patch.object(P, "_ascii_input_source", lambda: None), \
                step(base.act_by(d.caller_deadline)):
            try:
                res = d._chat_flash_click("b1")
            except base.StepExpired:
                res = base.step_expired_result()
        self.assertEqual(posted, [])                                    # no click after the deadline
        self.assertIs(res.performed, False, res.detail)

    def test_the_address_bar_click_is_not_made_past_the_deadline(self):
        """Its key window takes long to prepare; the deadline passes before the click and the paste."""
        import test_web_search as WS
        from deskmind_hands.drivers.peekaboo import WEB_SEARCH, PeekabooDriver as PD
        from deskmind_hands.drivers import peekaboo as P
        clock = self.Clock()
        c = WS.Carried()
        c._cleanups = []
        d = c.searchable()
        self.addCleanup(c.doCleanups)
        d.allow_browser_field(WEB_SEARCH)
        posted = []

        class Q:                                                       # Quartz as the click needs it
            kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGHIDEventTap, kCGMouseButtonLeft = 1, 2, 3, 0, 0
            kCGMouseEventClickState = 1
            CGEventCreateMouseEvent = staticmethod(lambda src, kind, pt, btn: {"kind": kind})
            CGEventSetIntegerValueField = staticmethod(lambda ev, f, v: None)
            CGEventPost = staticmethod(lambda tap, ev: posted.append(clock.t))

        def prepare(bundle, observed_only=False):
            clock.sleep(6.0)                                            # making the window key took a while
            return (Q, "token")
        d._background_quartz = prepare
        d._post_click = lambda q, x, y: PD._post_click(q, x, y)          # the real click, through _post
        d.caller_deadline = clock.t + base.DEADLINE_MARGIN_S + 5
        with mock.patch.object(P, "time", clock), mock.patch.object(base, "time", clock), \
                step(base.act_by(d.caller_deadline)):
            try:
                res = c.act(d)
            except base.StepExpired:
                res = base.step_expired_result()
        self.assertEqual(posted, [])
        self.assertIs(res.performed, False, res.detail)


class ReleasesSurviveTheDeadline(unittest.TestCase):
    """Codex P1 on hands#64 @3678581: the deadline blocked every event, so a key-up or mouse-up falling past it was
    never sent -- a key left held, a button left down. It blocks only NEW input; the completion of input already
    started goes. Codex's three probes (a key, a click, Unicode), then a deadline at each point of a chord and of a
    two-key sequence. Fail on 3678581."""

    class Clock:
        def __init__(self):
            self.t = 1000.0

        def time(self):
            return self.t

        def sleep(self, s):
            self.t += s

    def run_step(self, allowance, action):
        from deskmind_hands.actions import Action, ActionKind
        from deskmind_hands.drivers import peekaboo as P
        clock, events = self.Clock(), []
        q = types.SimpleNamespace(
            kCGHIDEventTap=0, kCGMouseButtonLeft=0, kCGMouseEventClickState=1,
            kCGEventMouseMoved="move", kCGEventLeftMouseDown="mouse-down", kCGEventLeftMouseUp="mouse-up",
            CGEventCreateMouseEvent=lambda _, kind, *args: kind,
            CGEventSetIntegerValueField=lambda *args: None,
            CGEventSetFlags=lambda ev, flags: None,
            CGEventKeyboardSetUnicodeString=lambda *args: None,
            CGEventPost=lambda tap, event: events.append(event),
        )
        d = P.PeekabooDriver.__new__(P.PeekabooDriver)
        d.caller_deadline = clock.t + base.DEADLINE_MARGIN_S + allowance
        d._require_session = lambda: None
        d.CONFIRM_BY_DIFF = False
        d._execute_outer = lambda a: action(d, q) or ExecResult(True, "done")
        with mock.patch.object(base, "time", clock), mock.patch.object(P, "time", clock), \
                mock.patch.object(P.PeekabooDriver, "_key_event",
                                  classmethod(lambda cls, Q, keycode, down: f"{keycode}-{'down' if down else 'up'}")):
            result = d.execute(Action(kind=ActionKind.KEY, keys=("a",)))
        return events, result

    def test_codex_a_key_released_after_the_deadline(self):
        events, _ = self.run_step(0.02, lambda d, q: d._post_key(q, 0))
        self.assertEqual(events, ["0-down", "0-up"])

    def test_codex_a_mouse_button_released_after_the_deadline(self):
        events, _ = self.run_step(0.075, lambda d, q: d._post_click(q, 10, 10))
        self.assertEqual(events, ["move", "mouse-down", "mouse-up"])

    def test_codex_unicode_released_after_the_deadline(self):
        events, _ = self.run_step(0.005, lambda d, q: d._post_chunk(q, "x"))
        self.assertEqual(events, ["0-down", "0-up"])

    def test_a_deadline_at_each_point_of_a_chord_and_a_sequence(self):
        """cmd+shift+d (the modifiers ride on the key's events), then cmd+A, cmd+V: whatever the deadline, every down
        sent has its up, and no down is sent after it."""
        CMD_SHIFT = (1 << 20) | (1 << 17)

        def chord_then_sequence(d, q):
            d._post_key(q, 2, CMD_SHIFT)                      # cmd+shift+d
            d._post_key(q, 0, 1 << 20)                        # cmd+A
            d._post_key(q, 9, 1 << 20)                        # cmd+V
        for allowance in (-1.0, 0.0, 0.01, 0.03, 0.05, 0.07, 0.1, 0.2, 10.0):
            with self.subTest(allowance=allowance):
                events, result = self.run_step(allowance, chord_then_sequence)
                downs = [e for e in events if e.endswith("down")]
                ups = [e for e in events if e.endswith("up")]
                self.assertEqual([d.replace("down", "up") for d in downs], ups)   # each down has its up, in order
                self.assertEqual(events, [x for k in ("2", "0", "9")[:len(downs)] for x in (f"{k}-down", f"{k}-up")])
                if len(downs) < 3:                            # stopped by the deadline
                    self.assertIs(result.performed, False if not downs else None)

    def test_an_exception_between_a_down_and_its_up_still_lets_go(self):
        from deskmind_hands.actions import Action, ActionKind
        from deskmind_hands.drivers import peekaboo as P
        seen = []
        q = types.SimpleNamespace(CGEventPost=lambda tap, ev: seen.append(ev))

        def broken(a):
            key = ("key", 7)
            P._post(q, 0, "7-down", press=(key, lambda: P._post(q, 0, "7-up", release=key)))
            raise RuntimeError("the route failed half way")
        d = P.PeekabooDriver.__new__(P.PeekabooDriver)
        d.caller_deadline = None
        d._require_session = lambda: None
        d.CONFIRM_BY_DIFF = False
        d._execute_outer = broken
        with self.assertRaises(RuntimeError):
            d.execute(Action(kind=ActionKind.KEY, keys=("a",)))
        self.assertEqual(seen, ["7-down", "7-up"])

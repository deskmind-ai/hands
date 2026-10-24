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

ROOT = Path(__file__).resolve().parent.parent / "deskmind_hands" / "drivers"
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

    def test_outside_a_step_nothing_counts_and_a_step_starts_at_zero(self):
        base.counted(lambda: None)
        self.assertEqual(base.sent_in_step(), 0)
        with input_scope():
            self.assertEqual(base.sent_in_step(), 0)
            base.counted(lambda: None)
            self.assertEqual(base.sent_in_step(), 1)
        self.assertIs(ExecResult(False, NothingSent("x")).performed, False)


REAL_FOCUS = axfield.focus


class NoSendBypassesTheCount(unittest.TestCase):
    """A source scan of the drivers: every OS call that can reach an app is made inside a counted wrapper, or in a
    function listed here as one that only reads or brings an app or a window forward."""

    SENDS = {"AXUIElementSetAttributeValue", "AXUIElementPerformAction", "CGEventPost", "CGEventPostToPid",
             "AXUIElementPostKeyboardEvent"}
    #: (file, function): where such a call may be made -- the counted wrappers, and the forward / key-window ones
    ALLOWED = {
        ("axfield.py", "_set"), ("axpopup.py", "_act"),
        ("skylight.py", "post_key_event"), ("skylight.py", "CGEventPost"),
        ("peekaboo.py", "_post"),
        ("skylight.py", "_set_focused_window"),          # key window (not a send)
        ("peekaboo.py", "_make_frontmost"),              # bring forward (not a send)
    }
    #: programs a driver may run without counting: none of them reaches an app
    QUIET = {"/usr/bin/pgrep", "/usr/sbin/ioreg", "/bin/sh", "/usr/bin/pbpaste", "/usr/bin/pbcopy",
             "/usr/sbin/screencapture"}
    #: processes started once, which themselves send nothing until asked through a counted command (_run)
    STARTED = {("peekaboo.py", "__init__")}

    def calls(self, path: Path):
        tree = ast.parse(path.read_text())
        out = []

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, child.name)
                else:
                    if isinstance(child, ast.Call):
                        out.append((fn, child))
                    walk(child, fn)
        walk(tree, "<module>")
        return out

    def test_no_os_send_outside_a_counted_wrapper(self):
        bad = []
        for path in sorted(ROOT.glob("*.py")):
            if path.name in ("mock.py", "base.py"):
                continue
            for fn, call in self.calls(path):
                f = call.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                if name in self.SENDS and (path.name, fn) not in self.ALLOWED:
                    bad.append(f"{path.name}:{call.lineno} {name} in {fn}")
                # fns["post"](...): SkyLight's event post
                if isinstance(f, ast.Subscript) and isinstance(f.slice, ast.Constant) and f.slice.value == "post" \
                        and (path.name, fn) not in self.ALLOWED:
                    bad.append(f"{path.name}:{call.lineno} fns['post'] in {fn}")
                # subprocess.run / Popen: only through _sh or _run, or a program that reaches no app
                if name in ("run", "Popen") and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                        and f.value.id == "subprocess" and fn not in ("_sh", "_run") and call.args \
                        and (path.name, fn) not in self.STARTED:
                    first = call.args[0]
                    prog = first.elts[0].value if isinstance(first, ast.List) and first.elts and isinstance(first.elts[0], ast.Constant) else None
                    if prog not in self.QUIET and not (isinstance(first, ast.List) and first.elts and isinstance(first.elts[0], ast.Attribute)
                                                       and first.elts[0].attr in ("executable", "bin")):
                        bad.append(f"{path.name}:{call.lineno} subprocess {prog} in {fn}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

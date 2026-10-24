"""Keys to a window in the background (skylight.enter_window_key): the window made its app's key window by records
sent to that app only, proven by reading the app's focused window back, given back on the way out; typing (the
accessibility insertion first, then keys) and chords through it, each read back. SkyLight and accessibility mocked."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.drivers import axfield, base, keys, skylight   # noqa: E402
from deskmind_hands.drivers import peekaboo as P_DRIVER   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver   # noqa: E402
from deskmind_hands.geometry import Point, Rect   # noqa: E402
from deskmind_hands.grounding import STOP   # noqa: E402

PID, TARGET, PREV, USER = 4242, 200, 100, 999


def decode(rec) -> tuple:
    """A record as (what, window): ("defocus"|"focus", wid) for a focus record, ("makekey1"|"makekey2", wid)."""
    wid = sum(rec[0x3C + i] << (8 * i) for i in range(4))
    if rec[0x08] == 0x0D:
        return ({1: "focus", 2: "defocus"}[rec[0x8A]], wid)
    return (f"makekey{rec[0x08]}", wid)


class Fake:
    """An app with a focused window that moves when the records (or the accessibility write) say so."""

    def __init__(self, focused=PREV, obeys_records=True, obeys_ax=True, status=0, front=USER, windows=(PREV, TARGET)):
        self.focused, self.obeys_records, self.obeys_ax, self.status, self.front = focused, obeys_records, obeys_ax, \
            status, front
        self.windows, self.stopped = list(windows), False
        self.sent, self.ax_writes = [], []
        self.on_makekey = None
        self.auth_ok = False

    def post(self, psn, rec):
        what = decode(rec)
        self.sent.append(what)
        if self.status == 0 and self.obeys_records and what[0] == "makekey2":
            self.focused = what[1]
            if self.on_makekey:
                self.on_makekey()
        return self.status

    def set_ax(self, pid, wid):
        self.ax_writes.append(wid)
        if self.obeys_ax:
            self.focused = wid
        return True

    def patches(self):
        return [mock.patch.object(skylight, "_load", lambda: {"loaded": True, "auth_ok": self.auth_ok}),
                mock.patch.object(skylight, "_psn_for", lambda wid: "psn"),
                mock.patch.object(skylight, "_post_record", self.post),
                mock.patch.object(skylight, "focused_window", lambda pid: self.focused),
                mock.patch.object(skylight, "_set_focused_window", self.set_ax),
                mock.patch.object(skylight, "front_pid", lambda: self.front),
                mock.patch.object(skylight, "window_ids", lambda pid: list(self.windows)),
                mock.patch.object(skylight, "_stopped", lambda: self.stopped),
                mock.patch.object(skylight, "auth_safe", lambda: self.sent.append(("auth self-test",)) or True),
                mock.patch.object(skylight, "CONFIRM_S", 0), mock.patch.object(skylight, "AX_CONFIRM_S", 0),
                mock.patch.object(skylight.time, "sleep", lambda s: None)]


class Patched(unittest.TestCase):
    def use(self, fake: Fake) -> Fake:
        for p in fake.patches():
            p.start()
            self.addCleanup(p.stop)
        return fake

    def refused(self, kind: str):
        with self.assertRaises(skylight.WindowKeyRefused) as cm:
            skylight.enter_window_key(PID, TARGET)
        self.assertEqual(cm.exception.kind, kind, str(cm.exception))
        return cm.exception


class EnterWindowKey(Patched):
    def test_records_go_to_the_app_only_in_order_and_the_focus_is_read_back(self):
        app = self.use(Fake())
        key = skylight.enter_window_key(PID, TARGET)
        self.assertEqual(app.sent, [("defocus", PREV), ("focus", TARGET), ("makekey1", TARGET), ("makekey2", TARGET)])
        self.assertEqual((key.via, key.previous), ("records", PREV))
        self.assertEqual(app.ax_writes, [])

    def test_the_auth_self_test_runs_before_any_record(self):
        app = self.use(Fake())
        app.auth_ok = True
        skylight.enter_window_key(PID, TARGET)
        self.assertEqual(app.sent[0], ("auth self-test",))

    def test_the_third_read_of_the_previous_key_window_counts(self):
        app = self.use(Fake(focused=None))
        reads = iter([None, None, PREV])
        with mock.patch.object(skylight, "focused_window", lambda pid: next(reads, app.focused)):
            key = skylight.enter_window_key(PID, TARGET)
        self.assertEqual(key.previous, PREV)

    def test_given_back_on_exit(self):
        app = self.use(Fake())
        with skylight.enter_window_key(PID, TARGET) as key:
            app.sent.clear()
        self.assertEqual(app.sent, [("defocus", TARGET), ("focus", PREV), ("makekey1", PREV), ("makekey2", PREV)])
        self.assertTrue(key.restored)
        self.assertEqual(key.note(), "")
        self.assertEqual(app.focused, PREV)

    def test_given_back_when_the_block_raises(self):
        app = self.use(Fake())
        with self.assertRaises(RuntimeError):
            with skylight.enter_window_key(PID, TARGET):
                raise RuntimeError("input failed")
        self.assertEqual(app.focused, PREV)
        self.assertEqual(app.sent[-4:], [("defocus", TARGET), ("focus", PREV), ("makekey1", PREV),
                                         ("makekey2", PREV)])

    def test_exit_once_only(self):
        app = self.use(Fake())
        key = skylight.enter_window_key(PID, TARGET)
        key.exit()
        n = len(app.sent)
        self.assertTrue(skylight.give_focus_back(key))
        self.assertEqual(len(app.sent), n)

    def test_the_user_on_the_app_at_exit_no_records_and_said(self):
        app = self.use(Fake())
        with skylight.enter_window_key(PID, TARGET) as key:
            app.sent.clear()
            app.front = PID                                  # the user clicked into the app meanwhile
        self.assertEqual(app.sent, [])
        self.assertTrue(key.interrupted)
        self.assertIn("interrupted by the user", key.note())
        self.assertIn("restoring its previous key window was skipped", key.note())

    def test_a_front_that_cannot_be_read_at_exit_sends_nothing(self):
        app = self.use(Fake())
        with skylight.enter_window_key(PID, TARGET) as key:
            app.sent.clear()
            app.front = None
        self.assertEqual(app.sent, [])
        self.assertIn("the front app could not be read", key.note())
        self.assertNotIn("interrupted by the user", key.note())

    def test_not_given_back_is_said(self):
        app = self.use(Fake())
        key = skylight.enter_window_key(PID, TARGET)
        app.obeys_records = app.obeys_ax = False
        self.assertFalse(key.exit())
        self.assertIn("could not be confirmed given back", key.note())

    def test_no_previous_window_and_no_other_window_the_target_is_only_defocused(self):
        app = self.use(Fake(focused=None, windows=(TARGET,)))
        with skylight.enter_window_key(PID, TARGET):
            app.sent.clear()
        self.assertEqual(app.sent, [("defocus", TARGET)])

    def test_no_previous_window_while_others_exist_refused_after_retries(self):
        app = self.use(Fake(focused=None))
        self.refused("previous")
        self.assertEqual(app.sent, [])

    def test_records_ignored_then_the_accessibility_write(self):
        app = self.use(Fake(obeys_records=False))
        with skylight.enter_window_key(PID, TARGET) as key:
            self.assertEqual(key.via, "ax")
            self.assertEqual(app.sent.count(("makekey2", TARGET)), 2)     # the records twice, then the write
            self.assertEqual(app.ax_writes, [TARGET])
        self.assertEqual(app.ax_writes, [TARGET, PREV])                    # given back the same way
        self.assertEqual(app.focused, PREV)

    def test_refused_when_the_focus_is_not_confirmed_and_nothing_left_moved(self):
        app = self.use(Fake(obeys_records=False, obeys_ax=False))
        self.assertIn("could not be confirmed", str(self.refused("unconfirmed")))
        self.assertEqual(app.focused, PREV)

    def test_refused_on_1002(self):
        app = self.use(Fake(status=skylight.INVALID_CONNECTION))
        self.assertIn("1002", str(self.refused("1002")))
        self.assertEqual(len(app.sent), 4)          # one pass, no retry, no accessibility write
        self.assertEqual(app.ax_writes, [])

    def test_refused_when_the_app_is_the_users_front_app(self):
        app = self.use(Fake(front=PID))
        self.assertIn("front app", str(self.refused("front")))
        self.assertEqual(app.sent, [])

    def test_refused_when_the_front_app_cannot_be_read(self):
        app = self.use(Fake(front=None))
        self.refused("front")
        self.assertEqual(app.sent, [])

    def test_the_user_coming_to_the_app_while_it_is_made_key_refuses_and_sends_nothing_back(self):
        app = self.use(Fake())
        app.on_makekey = lambda: setattr(app, "front", PID)
        self.refused("front")
        self.assertEqual(app.sent[-1], ("makekey2", TARGET))             # nothing after: no records to their app

    def test_stopped_while_it_is_made_key(self):
        app = self.use(Fake(obeys_records=False))
        app.on_makekey = None
        orig = app.post

        def post(psn, rec):
            app.stopped = True
            return orig(psn, rec)
        app.post = post
        with mock.patch.object(skylight, "_post_record", post):
            self.refused("stopped")
        self.assertEqual(app.ax_writes, [])                               # no accessibility write after the stop

    def test_refused_without_skylight(self):
        with mock.patch.object(skylight, "_load", lambda: None):
            with self.assertRaises(skylight.WindowKeyRefused):
                skylight.enter_window_key(PID, TARGET)

    def test_lost_when_another_window_becomes_key(self):
        app = self.use(Fake())
        key = skylight.enter_window_key(PID, TARGET)
        self.assertIsNone(key.lost())
        app.focused = PREV                                                # the task's window closed: the user's is key
        self.assertIn("no longer the app's key window", key.lost())
        app.focused, app.front = TARGET, PID
        self.assertIn("brought the app forward", key.lost())


class PostKeyEvent(unittest.TestCase):
    def fake_quartz(self, posted):
        return types.SimpleNamespace(CGEventPostToPid=lambda pid, ev: posted.append(("cg", pid, ev)))

    def test_cgeventposttopid_when_skylight_post_is_missing(self):
        posted = []
        with mock.patch.dict(sys.modules, {"Quartz": self.fake_quartz(posted)}):
            with mock.patch.object(skylight, "_load", lambda: None):
                self.assertEqual(skylight.post_key_event(PID, "ev"), "cgevent")
            with mock.patch.object(skylight, "_load", lambda: {"post": None, "set_int": None}):
                self.assertEqual(skylight.post_key_event(PID, "ev2"), "cgevent")
        self.assertEqual(posted, [("cg", PID, "ev"), ("cg", PID, "ev2")])

    def test_skylight_with_the_authentication_message(self):
        posted, fields, signed = [], [], []
        fns = {"post": lambda pid, p: posted.append((pid, p)), "set_int": lambda p, f, v: fields.append((f, v)),
               "auth_ok": True}
        with mock.patch.dict(sys.modules, {"Quartz": self.fake_quartz(posted)}), \
                mock.patch.object(skylight, "_load", lambda: fns), \
                mock.patch.object(skylight, "_ptr", lambda ev: 77), \
                mock.patch.object(skylight, "_authenticate", lambda f, p, pid: signed.append((p, pid)) or True):
            self.assertEqual(skylight.post_key_event(PID, "ev"), "skylight+auth")
            self.assertEqual(skylight.post_key_event(PID, "ev", auth=False), "skylight")
        self.assertEqual(posted, [(PID, 77), (PID, 77)])
        self.assertEqual(signed, [(77, PID)])
        self.assertIn((skylight.F_PID, PID), fields)
        self.assertIn((skylight.F_SOURCE_STATE, skylight.HID_SYSTEM_STATE), fields)

    def test_no_authentication_when_the_self_test_crashed(self):
        """A __CGEvent layout that moved crashes the child, not the run: keys then go unauthenticated."""
        crashed = types.SimpleNamespace(returncode=-11, stdout="")
        with mock.patch.object(skylight, "_auth_safe", None), \
                mock.patch("subprocess.run", lambda *a, **k: crashed), \
                mock.patch.object(skylight, "_authenticate_unchecked",
                                  side_effect=AssertionError("never read the record here")):
            self.assertFalse(skylight._authenticate({"auth_ok": True}, 77, PID))
            self.assertFalse(skylight.auth_safe())

    def test_the_self_test_runs_once(self):
        calls = []
        ok = types.SimpleNamespace(returncode=0, stdout="ok\n")
        with mock.patch.object(skylight, "_auth_safe", None), \
                mock.patch("subprocess.run", lambda *a, **k: calls.append(1) or ok):
            self.assertTrue(skylight.auth_safe())
            self.assertTrue(skylight.auth_safe())
        self.assertEqual(len(calls), 1)


class TextAndChords(unittest.TestCase):
    def test_chunks_keep_characters_whole_and_controls_apart(self):
        self.assertEqual(skylight.text_chunks("ab\ncd\te", 16), ["ab", "\n", "cd", "\t", "e"])
        self.assertEqual(skylight.text_chunks("中文输入法" * 4, 16), ["中文输入法" * 3 + "中", "文输入法"])
        self.assertEqual(skylight.text_chunks("😀" * 9, 16), ["😀" * 8, "😀"])   # two UTF-16 units each

    def test_parse_chord(self):
        cmd, shift = keys.MODIFIERS["cmd"], keys.MODIFIERS["shift"]
        self.assertEqual(keys.parse_chord("cmd+shift+n"), (45, cmd | shift))
        self.assertEqual(keys.parse_chord("Return"), (36, 0))
        self.assertEqual(keys.parse_chord("cmd+a", {12: "a"}), (12, cmd))      # on the user's layout (AZERTY)
        self.assertIsNone(keys.parse_chord("cmd+a+b"))
        self.assertIsNone(keys.parse_chord("cmd"))

    def test_focus_waits_for_the_field_to_say_so(self):
        """Chrome reads back focused some 60 ms after AXFocused is set: a read at once said no, and the first
        typing in a web field fell back (hands#58 live check)."""
        reads = iter([False, False, True])
        with mock.patch.object(axfield, "_set", return_value=True), \
                mock.patch.object(axfield, "is_focused", side_effect=lambda el: next(reads)), \
                mock.patch.object(axfield.time, "sleep"):
            self.assertTrue(axfield.focus(object()))
        with mock.patch.object(axfield, "_set", return_value=True), \
                mock.patch.object(axfield, "is_focused", return_value=False), \
                mock.patch.object(axfield.time, "sleep"), \
                mock.patch.object(axfield.time, "monotonic", side_effect=[0.0, 0.1, 0.3, 0.6]):
            self.assertFalse(axfield.focus(object()))

    def test_typed_in_is_lenient_on_case_and_strict_on_change(self):
        self.assertTrue(axfield.typed_in("start\n", "start\nAbc XYZ", "abc XYZ", False))   # TextEdit autocapitalises
        self.assertFalse(axfield.typed_in("start", "start", "abc", False))
        self.assertFalse(axfield.typed_in("abc", "abc ", "abc", False))           # changed, but not once more
        self.assertTrue(axfield.typed_in("old", "New text", "new text", True))
        self.assertFalse(axfield.typed_in("old", "old new", "new", True))         # not in place of what was there


class Key(skylight.WindowKey):
    """A WindowKey (the type a background Gate holds), made without a window: records its use; `lost()` as told."""

    def __init__(self, log, via="records", lost=None, note=""):
        self.log, self.via, self._lost, self._note = log, via, lost, note

    def __enter__(self):
        self.log.append("enter")
        return self

    def __exit__(self, *exc):
        self.log.append("exit")
        return False

    def lost(self):
        return self._lost() if self._lost else None

    def note(self):
        return self._note


class FakeQ(skylight.BackgroundQuartz):
    """A BackgroundQuartz (the type a background Gate holds), made without Quartz: each Unicode key-down goes into
    `field` (as an app would type it), or into another field when it has lost the focus."""
    kCGHIDEventTap, kCGEventFlagMaskCommand = 0, 1 << 20
    _q = None

    def __init__(self, d, field, key=None, pid=None):
        self.d, self.field, self.key, self.pid = d, field, key, pid if pid is not None else PID

    def post_checked(self, event, gate):
        self.CGEventPost(0, event)

    def CGEventKeyboardSetUnicodeString(self, ev, n, s):   # noqa: N802
        ev["text"] = s

    def CGEventSetFlags(self, ev, f):   # noqa: N802
        ev["flags"] = f

    def CGEventPost(self, tap, ev):   # noqa: N802
        if ev["down"] and getattr(self.d, "count_sends", False):
            base.counted(lambda: None)
        if ev["down"] and "text" in ev:
            self.d.log.append(("keys", ev["text"]))
            if not self.field.focused:                                  # the keyboard focus is elsewhere: there
                self.field.other += ev["text"]
            elif self.d.keys_land:
                self.field.value += ev["text"].capitalize() if not self.field.value.endswith(" ") else ev["text"]
        elif ev["down"]:                                                # a key built beforehand (_key_pair)
            self.d.log.append(("key", ev["code"], ev.get("flags", 0)))
            if ev.get("flags"):                                         # cmd+A: the next keys replace it all
                self.field.value = ""


def driver():
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.app, d._origin, d._window_id, d._pinned_window = "com.apple.TextEdit", Point(0, 0), "200", "200"
    d._pid_of = lambda bundle, all_wins=None: PID
    d._rects, d._labels_by_id, d._element_role = {"t1": Rect(0, 30, 600, 500)}, {"t1": "正文"}, {"t1": "textarea"}
    d._element_window = {"t1": "200"}
    d._preexisting = set()
    d._observed_windows = {"200"}
    d._background_keys = lambda: True
    d._moved_since_seen = lambda: None
    d._ascii_input = lambda: True
    d._background_input = lambda bundle: True
    d.log, d.keys_land = [], True
    d._post_key = lambda q, code, flags=0: d.log.append(("key", code, flags))
    d._load_keys = lambda: None
    d.CHORD_SETTLE_S = 0
    return d


class Field:
    """A text field whose value the mocked accessibility calls read and write."""

    def __init__(self, value, takes_insertion, role="AXTextArea", names=("正文",), web=False):
        self.value, self.takes, self.role, self.names, self.web = value, takes_insertion, role, list(names), web
        self.focused = True
        self.other = ""                     # another field of the window, where keys go when this one lost the focus


class _TypeHarness(unittest.TestCase):
    def run_type(self, field, keys_land=True, enter=None, text="hello 你好", clear=False, selects=True,
                 all_selected=True, d=None, settle=None, value=None, insert=None, extra=(), user_on=False,
                 key_event=None, real_keycode=False, count_sends=False):
        d = d or driver()
        d.keys_land = keys_land
        d.count_sends = count_sends                     # the fake focus, selection and keys counted as sends

        def sends():
            if count_sends:
                base.ax_counted(lambda: 0)
        if not real_keycode:
            d._keycode = lambda q, letter: {"a": 0}[letter]

        def post_key(q, code, flags=0):                                 # cmd+A selects all: the next keys replace it
            d.log.append(("key", code, flags))
            if flags:
                field.value = ""
        d._post_key = post_key

        def ax_insert(el, t, replace_all, check=None, want=None):
            # takes_insertion: True, it shows; "ignored", it reaches the app and nothing shows (accepted, a no-op);
            # False, refused before it reached the app (nothing counted as sent). The gate (`check`) as the real one
            # asks it, before the first write.
            why = check() if check is not None else None
            if why:
                return axfield.NOTHING, why
            d.log.append(("ax insert", t))
            if not field.takes:
                return axfield.SELECTION, "the selection was refused"
            base.counted(lambda: None)
            if field.takes is True:
                field.value = t if replace_all else field.value + t
            return axfield.TEXT, None
        d.settle_waits = []

        def default_settle(el, before, wait=0.6):
            d.settle_waits.append(wait)
            return field.value
        patches = [*extra, mock.patch.object(axfield, "field_at", lambda pid, wid, frame: field),
                   mock.patch.object(axfield, "value", value or (lambda el: field.value)),
                   mock.patch.object(axfield, "insert", insert or ax_insert),
                   mock.patch.object(axfield, "settled_value", settle or default_settle),
                   mock.patch.object(axfield, "role", lambda el: field.role),
                   mock.patch.object(axfield, "names", lambda el: field.names),
                   mock.patch.object(axfield, "in_web_area", lambda el: field.web),
                   mock.patch.object(axfield, "is_focused",                       # a read that can wait: a test
                                     lambda el: getattr(d, "on_focus_read", lambda: None)() or field.focused),
                   mock.patch.object(axfield, "focus", lambda el: d.log.append("focus") or sends()
                                     or setattr(field, "focused", True) or True),
                   mock.patch.object(axfield, "select", lambda el, replace_all: d.log.append("select") or sends()
                                     or selects),
                   # the selection's range read (it can wait), apart from its write: a test may act there
                   mock.patch.object(axfield, "wanted", lambda el, all_: getattr(d, "on_length_read", lambda: None)()
                                     or (0, 0), create=True),
                   # a selection that cannot be read cannot be read back as set either (consistent with it)
                   mock.patch.object(axfield, "select_range", lambda el, want, wait=0.3: d.log.append("select")
                                     or sends() or getattr(d, "on_select", lambda: None)()
                                     or (selects and getattr(d, "selection_readable", True)), create=True),
                   mock.patch.object(axfield, "selection_readable", lambda el: getattr(d, "selection_readable", True),
                                     create=True),
                   mock.patch.object(axfield, "all_selected",
                                     lambda el, wait=0.5: all_selected and getattr(d, "selection_readable", True)),
                   mock.patch.object(skylight, "user_on", lambda pid: user_on),
                   mock.patch.object(skylight, "BackgroundQuartz",
                                     type("HeldWindow", (FakeQ,), {"__init__": lambda self_, pid, wid, origin, key=None:
                                                                   FakeQ.__init__(self_, d, field, key, pid)})),
                   # the front app, as the gate's instant check reads it: another app, the user's, by default
                   mock.patch.object(P_DRIVER, "_front_app_pid", lambda: getattr(d, "front_app", 1)),
                   mock.patch.object(PeekabooDriver, "_key_event",
                                     key_event or classmethod(lambda cls, Q, code, down: {"down": down, "code": code})),
                   mock.patch.object(skylight, "enter_window_key",
                                     enter or (lambda pid, wid: d.log.append(("key window", pid, wid)) or Key(d.log)))]
        for p in patches:
            p.start()
        try:
            with base.input_scope(None):                                  # one step, its sends counted
                return d, d._type_in_background("t1", text, clear)
        finally:
            for p in patches:
                p.stop()

    def keys_sent(self, d):
        return [e for e in d.log if isinstance(e, tuple) and e[0] == "keys"]


class TypeText(_TypeHarness):

    def test_the_accessibility_insertion_first(self):
        d, res = self.run_type(Field("start\n", takes_insertion=True))
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertIn("accessibility insertion", res.detail)
        self.assertEqual(d.log, [("ax insert", "hello 你好")])          # no window made key, no key sent

    def test_keys_when_the_insertion_is_refused_before_reaching_the_app(self):
        d, res = self.run_type(Field("start", takes_insertion=False))
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertIn("keys to window 200", res.detail)
        self.assertEqual(d.log, [("ax insert", "hello 你好"), ("key window", PID, 200), "enter", "focus", "select",
                                 ("keys", "hello 你好"), "exit"])

    def test_nothing_written_no_wait(self):
        d, res = self.run_type(Field("start", takes_insertion=False),
                               insert=lambda el, t, r, check=None, want=None: (axfield.SELECTION, "refused"))
        self.assertNotIn(PeekabooDriver.AX_INSERT_WAIT_S, d.settle_waits)   # nothing went: no wait for it to show

    def test_an_insertion_shown_late_is_not_typed_again(self):
        """The value shows the insertion only after ~1 s: the wait (1.5 s) sees it, and no key is sent."""
        field, clock, pending = Field("start", takes_insertion=False), [0.0], []
        real_settle = axfield.settled_value

        def value(el):
            if pending and clock[0] >= 1.0:
                field.value += pending.pop()
            return field.value

        def insert(el, t, replace_all, check=None, want=None):
            pending.append(t)
            base.counted(lambda: None)
            return axfield.TEXT, None
        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0],
                                          sleep=lambda dt: clock.__setitem__(0, clock[0] + dt))
        d, res = self.run_type(field, settle=real_settle, value=value, insert=insert,
                               extra=[mock.patch.object(axfield, "time", fake_time)])
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertGreaterEqual(clock[0], 1.0)
        self.assertEqual(d.log, [])                                       # no window made key, no key sent

    def test_an_insertion_that_went_and_shows_nothing_is_unverified_and_no_key_follows(self):
        """Chosen before sending, kept: an insertion that reached the app is never followed by keys (hands#66 review:
        a late write plus the keys typed the text twice). Unverified, said so."""
        d, res = self.run_type(Field("start", takes_insertion="ignored"))
        self.assertTrue(res.indeterminate, res.detail)
        self.assertIn("unverified", res.detail)
        self.assertEqual(d.log, [("ax insert", "hello 你好")])          # no window made key, no key sent

    def test_a_write_that_lands_after_the_wait_is_one_copy_and_unverified(self):
        field = Field("start", takes_insertion="ignored")
        d, res = self.run_type(field)
        field.value += "hello 你好"                                      # the write lands after the read-back
        self.assertTrue(res.indeterminate, res.detail)
        self.assertEqual(self.keys_sent(d), [])
        self.assertEqual(field.value.count("hello 你好"), 1)

    def test_no_window_is_remembered_from_a_failed_write(self):
        """The way in is decided from the element, not from what an earlier write did: the next text in the same
        window is inserted again, and again no key follows."""
        d = driver()
        d, res = self.run_type(Field("start", takes_insertion="ignored"), d=d)
        d.log.clear()
        d, res = self.run_type(Field("start", takes_insertion="ignored"), d=d, text="again")
        self.assertEqual(d.log, [("ax insert", "again")])
        self.assertFalse(hasattr(d, "_ax_noop_windows"))

    def test_focus_moved_while_preparing_the_keys_lands_nothing_elsewhere(self):
        """hands#66's focus family: a key event made after the focus check (on a cold cache, a child process reading
        the layout, up to 20 s) let the focus move first, and the next chunk went into another field. Prepared before
        the check now. Every preparing call -- the layout read on a cold cache, each key event, the chunks -- moves
        the focus away; nothing lands elsewhere, and the field is focused again before the first key."""
        from deskmind_hands.drivers import peekaboo as P
        field = Field("start", takes_insertion=False)

        def steal(*_a, **_k):
            field.focused = False
            field.other_moves = getattr(field, "other_moves", 0) + 1

        def child(*a, **k):                                       # the layout read: a child process, when cold
            steal()
            return types.SimpleNamespace(stdout='{"events": {}, "table": {}}', returncode=0)

        def key_event(cls, Q, code, down):
            cls._load_keys()                                      # cold: the child process
            steal()
            return {"down": down, "code": code}
        chunks = skylight.text_chunks
        cold = [mock.patch.object(PeekabooDriver, name, value)
                for name, value in (("_key_blobs", {}), ("_letter_codes", {}), ("_letters_read_at", 0.0))]
        d, res = self.run_type(field, text="hello world", real_keycode=True, key_event=classmethod(key_event),
                               extra=[*cold, mock.patch.object(P.subprocess, "run", child),
                                      mock.patch.object(skylight, "text_chunks",
                                                        lambda t, *a: steal() or chunks(t, *a))])
        self.assertGreater(field.other_moves, 0)                  # the preparing calls did move it
        self.assertEqual(field.other, "", d.log)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertIn("Hello world", field.value)

    def test_focus_moved_while_a_key_event_is_made_on_a_cold_cache_lands_nothing_elsewhere(self):
        """Codex's case on #66 @3f82851, here: only the key event's making moves the focus (a child process on a cold
        cache). It was made after the check, and the chunk went into the other field."""
        from deskmind_hands.drivers import peekaboo as P
        field = Field("start", takes_insertion=False)

        def child(*a, **k):
            field.focused = False                                 # the user clicked another field meanwhile
            return types.SimpleNamespace(stdout='{"events": {}, "table": {}}', returncode=0)

        def key_event(cls, Q, code, down):
            cls._load_keys()
            return {"down": down, "code": code}
        cold = [mock.patch.object(PeekabooDriver, name, value)
                for name, value in (("_key_blobs", {}), ("_letter_codes", {}), ("_letters_read_at", 0.0))]
        d, res = self.run_type(field, text="hello world", real_keycode=True, key_event=classmethod(key_event),
                               extra=[*cold, mock.patch.object(P.subprocess, "run", child)])
        self.assertEqual(field.other, "", d.log)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_the_window_changing_midway_stops_the_typing(self):
        """2000 chars into the task's window, which closes midway: the app makes the user's window key, and the rest
        must not go there."""
        d = driver()
        chunks = [0]

        def lost():
            chunks[0] += 1
            # asked by the gate before the focus write and the selection, then before each chunk: lost after three
            return "window 200 is no longer the app's key window (now 100)" if chunks[0] > 5 else None
        d, res = self.run_type(Field("start", takes_insertion=False), d=d, text="x" * 2000,
                               enter=lambda pid, wid: Key(d.log, lost=lost))
        self.assertFalse(res.ok)
        self.assertTrue(res.indeterminate)
        self.assertIn("typing stopped after 48 of 2000", res.detail)
        self.assertIn("no longer the app's key window", res.detail)
        self.assertEqual(len(self.keys_sent(d)), 3)

    def test_the_field_losing_the_focus_midway_stops_the_typing(self):
        field = Field("start", takes_insertion=False)
        d = driver()
        orig_post = FakeQ.CGEventPost

        def post(self, tap, ev):
            orig_post(self, tap, ev)
            field.focused = False                                        # e.g. a Tab moved it, or a dialog took it
        with mock.patch.object(FakeQ, "CGEventPost", post):
            d, res = self.run_type(field, d=d, text="y" * 40)
        self.assertIn("lost the keyboard focus", res.detail)
        self.assertEqual(len(self.keys_sent(d)), 1)

    def test_no_cmd_a_when_the_window_is_lost(self):
        d = driver()
        d, res = self.run_type(Field("old", takes_insertion=False), d=d, clear=True, selects=False,
                               enter=lambda pid, wid: Key(d.log, lost=lambda: "window 200 is no longer key"))
        self.assertIsNone(res)
        self.assertNotIn(("key", 0, 1 << 20), d.log)

    def test_not_given_back_is_in_the_result(self):
        d = driver()
        d, res = self.run_type(Field("start", takes_insertion=False), d=d,
                               enter=lambda pid, wid: Key(d.log, note="the app's previous key window (100) could not "
                                                                      "be confirmed given back"))
        self.assertIn("could not be confirmed given back", res.detail)

    def test_replace_selects_all_by_key_when_accessibility_will_not(self):
        field = Field("old", takes_insertion=False)
        d, res = self.run_type(field, clear=True, selects=False, text="new")
        self.assertIn(("key", 0, 1 << 20), d.log)                       # cmd+A, then the text
        self.assertEqual(d.log.index(("key", 0, 1 << 20)) + 1, d.log.index(("keys", "new")))
        self.assertTrue(res.ok and not res.indeterminate, res.detail)    # "New": TextEdit's capital, still verified

    def test_replace_left_to_the_old_route_when_nothing_reads_back_selected(self):
        d, res = self.run_type(Field("old", takes_insertion=False), clear=True, selects=False, all_selected=False)
        self.assertIsNone(res)
        self.assertEqual(self.keys_sent(d), [])

    def test_unverified_when_the_keys_do_not_read_back(self):
        d, res = self.run_type(Field("start", takes_insertion=False), keys_land=False)
        self.assertTrue(res.indeterminate)
        self.assertIn("unverified", res.detail)

    def test_a_window_that_cannot_be_made_key_leaves_it_to_the_old_route(self):
        def refuse(pid, wid):
            raise skylight._refuse("unconfirmed", "window 200 could not be confirmed as the app's key window")
        d, res = self.run_type(Field("start", takes_insertion=False), enter=refuse)
        self.assertIsNone(res)
        self.assertEqual(self.keys_sent(d), [])

    def test_stopped_while_the_window_is_made_key(self):
        def refuse(pid, wid):
            raise skylight._refuse("stopped", "stopped by the app")
        d, res = self.run_type(Field("start", takes_insertion=False), enter=refuse)
        self.assertFalse(res.ok)
        self.assertIn("stopped", res.detail)

    def test_the_users_front_app_leaves_it_to_the_old_route(self):
        d, res = self.run_type(Field("start", takes_insertion=True), user_on=True)
        self.assertIsNone(res)
        self.assertEqual(d.log, [])

    def test_stopped_before_anything(self):
        STOP.set()
        try:
            d, res = self.run_type(Field("start", takes_insertion=True))
        finally:
            STOP.clear()
        self.assertFalse(res.ok)
        self.assertEqual(d.log, [])

    def test_web_content_goes_to_keys_never_the_insertion(self):
        """A page keeps its own state: an insertion can show in the field and the form submit empty."""
        d, res = self.run_type(Field("start", takes_insertion=True, web=True))
        self.assertTrue(res.ok, res.detail)
        self.assertNotIn("ax insert", [e[0] for e in d.log if isinstance(e, tuple)])
        self.assertIn(("keys", "hello 你好"), d.log)

    def test_a_web_fields_replace_keeps_the_click_and_paste_route(self):
        d, res = self.run_type(Field("start", takes_insertion=True, web=True), clear=True)
        self.assertIsNone(res)
        self.assertEqual(d.log, [])

    def test_an_input_method_on_no_keys(self):
        d = driver()
        d._ascii_input = lambda: False
        d, res = self.run_type(Field("start", takes_insertion=False), d=d)
        self.assertIsNone(res)
        self.assertEqual(self.keys_sent(d), [])
        d2 = driver()
        d2._ascii_input = lambda: False
        d2, res = self.run_type(Field("start", takes_insertion=True), d=d2)   # a native field takes the insertion
        self.assertTrue(res.ok and "accessibility insertion" in res.detail, res.detail)

    def test_a_newline_is_never_the_return_key_here(self):
        """hands#66's newline rule: no key route turns a newline (or a carriage return) into Return -- in a chat box
        Return sends. Text with one goes in as text: the insertion where it is taken, else not by keys here (None:
        the caller's route, a value written or a paste)."""
        for text in ("b\nc", "b\r\nc\rd"):
            d, res = self.run_type(Field("a", takes_insertion=False), text=text)          # insertion refused
            self.assertIsNone(res, text)
            self.assertEqual([e for e in d.log if isinstance(e, tuple) and e[0] in ("keys", "key")], [], text)
        field = Field("a", takes_insertion=True)
        d, res = self.run_type(field, text="b\r\nc")                                    # inserted as text
        self.assertEqual(field.value, "ab\nc")
        self.assertNotIn(("key", keys.RETURN, 0), d.log)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)

    def test_a_one_line_field_takes_no_newline_or_tab_as_keys(self):
        for text in ("a\tb", "a\nb", "a\r\nb", "a\rb"):
            d, res = self.run_type(Field("", takes_insertion=False, role="AXTextField"), text=text)
            self.assertIsNone(res, text)
            self.assertEqual(self.keys_sent(d), [], text)

    def test_a_tab_in_a_multiline_field_is_the_tab_key(self):
        d, res = self.run_type(Field("a", takes_insertion=False), text="b\tc")
        self.assertIn(("key", keys.TAB, 0), d.log)

    def test_a_label_made_from_the_role_is_no_label(self):
        d = driver()
        d._labels_by_id, d._role_labels = {"t1": "文本区域"}, {"t1"}          # "text area": a kind, not a name
        d, res = self.run_type(Field("start", takes_insertion=True, names=()), d=d)
        self.assertTrue(res.ok, res.detail)

    def test_not_the_observed_element_leaves_it_to_the_old_route(self):
        d, res = self.run_type(Field("start", takes_insertion=True, names=("搜索",)))
        self.assertIsNone(res)
        d, res = self.run_type(Field("start", takes_insertion=True, role="AXTextField"))   # a textarea was observed
        self.assertIsNone(res)
        self.assertEqual(d.log, [])

    def test_a_moved_window_leaves_it_to_the_old_route(self):
        d = driver()
        d._moved_since_seen = lambda: "the window moved"
        with mock.patch.object(axfield, "field_at", side_effect=AssertionError("not looked up by place")):
            self.assertIsNone(d._type_in_background("t1", "x", False))

    def test_no_field_to_read_back_by_leaves_it_to_the_old_route(self):
        d = driver()
        with mock.patch.object(axfield, "field_at", lambda pid, wid, frame: None), \
                mock.patch.object(skylight, "user_on", lambda pid: False):
            self.assertIsNone(d._type_in_background("t1", "x", False))

    def test_a_window_the_run_does_not_own_is_left_to_the_old_route(self):
        d = driver()
        d._preexisting = {"200"}
        with mock.patch.object(axfield, "field_at", side_effect=AssertionError("not looked at")):
            self.assertIsNone(d._type_in_background("t1", "x", False))


class Chord(unittest.TestCase):
    def press(self, states, enter=None, chord="cmd+shift+n", windows=(200,), user_on=False, d=None, cg=None):
        d = d or driver()
        seq = iter(states)
        cg = windows if cg is None else cg
        with mock.patch.object(axfield, "window_state", lambda pid, wid: next(seq)), \
                mock.patch.object(skylight, "window_ids", lambda pid: None if windows is None else list(windows)), \
                mock.patch.object(skylight, "cg_window_ids", lambda pid: None if cg is None else list(cg)), \
                mock.patch.object(skylight, "user_on", lambda pid: user_on), \
                mock.patch.object(skylight, "BackgroundQuartz", lambda pid, wid, origin, key=None: "Q"), \
                mock.patch.object(skylight, "enter_window_key", enter or (lambda pid, wid: Key(d.log))):
            return d, d._press_in_background(chord)

    def test_sent_to_the_key_window_and_confirmed_by_a_change(self):
        a = {"title": "未命名", "windows": (200,), "value": "", "selection": (0, 0)}
        d, res = self.press([a, {**a, "windows": (200, 201)}])
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertIn("windows changed", res.detail)
        self.assertEqual(d.log, ["enter", ("key", 45, keys.MODIFIERS["cmd"] | keys.MODIFIERS["shift"]), "exit"])

    def test_unverified_when_nothing_reads_back_changed(self):
        a = {"title": "t", "windows": (200,), "value": "", "selection": (0, 0)}
        d, res = self.press([a, dict(a)])
        self.assertTrue(res.indeterminate)
        self.assertIn("observe before pressing it again", res.detail)

    def test_refused_when_the_app_has_a_window_that_is_not_the_tasks(self):
        """cmd+Q, cmd+H act on the whole app: only while every window of it is the task's own -- no chord list."""
        d = driver()
        d._preexisting = {"100"}
        for chord in ("cmd+q", "cmd+h", "cmd+a"):
            d.log.clear()
            d, res = self.press([], chord=chord, windows=(100, 200), d=d)
            self.assertFalse(res.ok)
            self.assertIn("no background route", res.detail)
            self.assertEqual(d.log, [])

    def test_refused_for_a_window_the_user_opened_during_the_run(self):
        """Not there before the run, but neither opened nor observed by it: the user's."""
        d, res = self.press([], windows=(200, 300))
        self.assertFalse(res.ok)
        self.assertEqual(d.log, [])

    def test_refused_for_a_window_only_the_window_server_lists(self):
        """Accessibility can miss a window on another Space: both lists must show only the task's windows."""
        d, res = self.press([], windows=(200,), cg=(200, 300))
        self.assertFalse(res.ok)
        self.assertEqual(d.log, [])

    def test_refused_when_the_apps_windows_cannot_be_read(self):
        d, res = self.press([], windows=None)
        self.assertFalse(res.ok)
        self.assertEqual(d.log, [])

    def test_refused_as_before_when_the_app_is_the_users_front_app(self):
        d, res = self.press([], user_on=True)
        self.assertFalse(res.ok)
        self.assertIn("no background route", res.detail)
        self.assertEqual(d.log, [])

    def test_not_pressed_when_the_window_is_lost_after_it_was_made_key(self):
        d = driver()
        d, res = self.press([{"title": "t"}], enter=lambda pid, wid: Key(d.log, lost=lambda: "window 200 is gone"),
                            d=d)
        self.assertFalse(res.ok)
        self.assertNotIn("key", [e[0] for e in d.log if isinstance(e, tuple)])

    def test_refused_when_the_window_is_not_proven_key(self):
        def refuse(pid, wid):
            raise skylight._refuse("unconfirmed", "window 200 could not be confirmed")
        d, res = self.press([None, None], enter=refuse)
        self.assertFalse(res.ok)
        self.assertIn("could not be confirmed", res.detail)
        self.assertEqual(d.log, [])

    def test_not_the_runs_window_not_pressed(self):
        d = driver()
        d._preexisting = {"200"}
        res = d._press_in_background("cmd+a")
        self.assertFalse(res.ok)
        self.assertIn("not one this run may type into", res.detail)


class FakeQuartz:
    """Quartz for BackgroundQuartz: every event posted to the process is recorded (none is real)."""
    kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged = 10, 11, 12
    kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp = 5, 1, 2
    kCGMouseButtonLeft, kCGHIDEventTap, kCGMouseEventClickState, kCGEventFlagMaskCommand = 0, 0, 1, 1 << 20

    def __init__(self):
        self.posted = []

    def CGEventGetType(self, ev):   # noqa: N802
        return ev["type"]

    def CGEventCreateMouseEvent(self, src, kind, pt, button):   # noqa: N802
        return {"type": kind, "pt": pt}

    def CGEventCreateKeyboardEvent(self, src, code, down):   # noqa: N802
        return {"type": self.kCGEventKeyDown if down else self.kCGEventKeyUp, "code": code}

    def CGEventSetIntegerValueField(self, ev, f, v):   # noqa: N802
        pass

    def CGEventGetIntegerValueField(self, ev, f):   # noqa: N802
        return 1

    def CGEventGetLocation(self, ev):   # noqa: N802
        return types.SimpleNamespace(x=ev["pt"][0], y=ev["pt"][1])

    def CGEventSetFlags(self, ev, f):   # noqa: N802
        ev["flags"] = f

    def CGEventKeyboardSetUnicodeString(self, ev, n, s):   # noqa: N802
        ev["text"] = s

    def CGEventPostToPid(self, pid, ev):   # noqa: N802
        self.posted.append(ev)


def background_quartz(key):
    """A real BackgroundQuartz over FakeQuartz: what it posts, mouse or key, ends up in fake.posted."""
    fake = FakeQuartz()
    with mock.patch.dict(sys.modules, {"Quartz": fake}), mock.patch.object(skylight, "_load", lambda: None):
        q = skylight.BackgroundQuartz(PID, TARGET, (0, 0), key=key)
    return q, fake


class NoKeysToTheUsersApp(unittest.TestCase):
    """The user is in the app: clicks may go (window-routed), no key event at all (it would land in their key window)."""

    def setUp(self):
        self.keys_posted = []
        p = mock.patch.object(skylight, "post_key_event", lambda pid, ev, auth=True: self.keys_posted.append(ev))
        p.start()
        self.addCleanup(p.stop)
        k = mock.patch.object(PeekabooDriver, "_key_event",
                              classmethod(lambda cls, Q, code, down: Q.CGEventCreateKeyboardEvent(None, code, down)))
        k.start()
        self.addCleanup(k.stop)

    def test_background_quartz_refuses_keys_and_passes_clicks(self):
        q, fake = background_quartz(skylight.NoWindowKey())
        with mock.patch.object(skylight, "_load", lambda: None):
            PeekabooDriver._post_click(q, 10, 10)
            with self.assertRaises(skylight.WindowKeyRefused) as cm:
                PeekabooDriver._post_key(q, 0, q.kCGEventFlagMaskCommand)
        self.assertEqual(cm.exception.kind, "front")
        self.assertEqual(len(fake.posted), 3)                             # move, down, up
        self.assertEqual(self.keys_posted, [])

    def test_a_held_window_lost_refuses_the_key_down(self):
        key = skylight.WindowKey(PID, TARGET, PREV, "psn", "records")
        key.lost = lambda: "window 200 is no longer the app's key window (now 100)"
        q, _ = background_quartz(key)
        with self.assertRaises(skylight.WindowKeyRefused):
            PeekabooDriver._post_key(q, 0)
        self.assertEqual(self.keys_posted, [])
        key.lost = lambda: None
        PeekabooDriver._post_key(q, 0)
        self.assertEqual(len(self.keys_posted), 2)                        # down and up, while it holds

    def test_address_bar_search_sends_no_key_and_waits(self):
        from deskmind_hands.actions import Action, ActionKind, Binding
        from deskmind_hands.drivers import axchrome
        from deskmind_hands.drivers.base import USER_BUSY
        from deskmind_hands.drivers.peekaboo import WEB_SEARCH
        d = driver()
        d.app, d._run_windows, d._origin = "com.google.Chrome", {"200"}, Point(0, 0)
        d._address_bar, d._address_bar_rect, d._browser_field_allowed = "e48", Rect(10, 10, 200, 30), WEB_SEARCH
        del d._post_key                                                  # the driver's own: through q
        d._projected_actions, d._seen_frame = 0, (0, 0, 800, 600)
        d._window_frame = lambda: (0, 0, 800, 600)
        d._wait_until_still = lambda bundle, what="": None
        d._settle = lambda ok, detail, files=(): __import__("deskmind_hands.drivers.base", fromlist=["x"]).ExecResult(
            ok, detail)
        key = skylight.NoWindowKey()
        q, fake = background_quartz(key)
        d._background_quartz = lambda bundle, observed_only=False: (q, key)
        pb = types.SimpleNamespace(pasteboardItems=lambda: [], clearContents=lambda: None,
                                   setString_forType_=lambda s, t: None, writeObjects_=lambda items: None)
        appkit = types.SimpleNamespace(NSPasteboard=types.SimpleNamespace(generalPasteboard=lambda: pb),
                                       NSPasteboardItem=None, NSPasteboardTypeString="public.utf8-plain-text")
        with mock.patch.dict(sys.modules, {"AppKit": appkit}), mock.patch.object(skylight, "_load", lambda: None), \
                mock.patch.object(axchrome, "focused_identifier", lambda pid, fr, wid=None: "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD"):
            res = d._execute_projected(Action(kind=ActionKind.TYPE_TEXT, text="天气",
                                              binding=Binding(observation_id="o", app="Chrome", element_id=WEB_SEARCH)))
        # the click into the address bar went before the first key was refused: not "busy, nothing done" but
        # unverified (review of hands#64), and still no key reached the user's app
        self.assertTrue(res.ok and res.indeterminate, res.detail)
        self.assertNotIn(USER_BUSY, res.detail)
        self.assertIsNot(res.performed, False)
        self.assertEqual(self.keys_posted, [])

    def test_a_flash_paste_sends_no_key_and_takes_no_other_path(self):
        """_vision_act's paste (click, cmd+A, cmd+V) through _flash: refused at the first key, after the click went.
        No key reaches the user's app, and the step does not fall through to the front route: the click may have
        done something, so it is unverified, never that route's "not done" (review of hands#64 round 5)."""
        d = driver()
        d.app = "com.apple.Safari"
        d._background_input = lambda bundle: True
        d._wait_until_still = lambda bundle, what="": None
        key = skylight.NoWindowKey()
        q, fake = background_quartz(key)
        d._background_quartz = lambda bundle, observed_only=False: (q, key)

        def perform(Q):
            PeekabooDriver._post_click(Q, 10, 10)
            PeekabooDriver._post_key(Q, 0, Q.kCGEventFlagMaskCommand)     # cmd+A: the user's mail body
            PeekabooDriver._post_key(Q, 9, Q.kCGEventFlagMaskCommand)
            return "entered"
        from deskmind_hands.drivers.base import ExecResult, input_scope, sent_in_step
        with mock.patch.object(skylight, "_load", lambda: None), input_scope():
            ok, detail = d._flash("com.apple.Safari", perform, what="type")
            self.assertGreater(sent_in_step(), 0)                       # the click (its mouse events), counted
        self.assertFalse(ok)
        self.assertIn("may have taken effect", detail)
        self.assertTrue(ExecResult(ok, detail).indeterminate)
        self.assertEqual(self.keys_posted, [])

    def test_flash_reports_a_key_window_not_given_back(self):
        d = driver()
        d.app = "com.apple.Safari"
        d._background_input = lambda bundle: True
        d._wait_until_still = lambda bundle, what="": None
        token = types.SimpleNamespace(exit=lambda: False,
                                      note=lambda: "the app's previous key window (100) could not be confirmed given back")
        d._background_quartz = lambda bundle, observed_only=False: ("Q", token)
        ok, detail = d._flash("com.apple.Safari", lambda Q: "clicked", what="click")
        self.assertTrue(ok)
        self.assertIn("could not be confirmed given back", detail)

    def test_a_plain_type_on_the_users_app_sends_no_key(self):
        t = TypeText()
        d, res = t.run_type(Field("start", takes_insertion=False), user_on=True)
        self.assertIsNone(res)
        self.assertEqual(d.log, [])


class FrontApp(unittest.TestCase):
    def test_background_quartz_sends_no_records_to_the_front_app(self):
        d = driver()
        d.app = "com.apple.Safari"
        d._run_windows = {"200"}
        wins = [{"kCGWindowOwnerPID": PID, "kCGWindowLayer": 0, "kCGWindowNumber": 200,
                 "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 800, "Height": 600}}]
        fake_q = types.SimpleNamespace(CGWindowListCopyWindowInfo=lambda opt, wid: wins, kCGWindowListOptionAll=0)

        def refuse(pid, wid):
            raise skylight._refuse("front", "the app is the user's front app")
        with mock.patch.dict(sys.modules, {"Quartz": fake_q,
                                           "AppKit": types.SimpleNamespace(NSRunningApplication=None)}), \
                mock.patch.object(skylight, "enter_window_key", refuse), \
                mock.patch.object(skylight, "BackgroundQuartz", lambda pid, wid, origin, key=None: ("Q", wid)):
            with mock.patch.object(skylight, "front_pid", lambda: PID):
                q, key = d._background_quartz("com.apple.Safari", observed_only=True)
                self.assertEqual(q, ("Q", 200))
                self.assertIsInstance(key, skylight.NoWindowKey)
            with mock.patch.object(skylight, "front_pid", lambda: None):     # unknown: fail closed
                self.assertIsNone(d._background_quartz("com.apple.Safari", observed_only=True))


if __name__ == "__main__":
    unittest.main()


class GateAtEverySendSiteInTheBackground(_TypeHarness):
    """hands#66's one-gate rule in _type_in_background's key route (review of #66 @8086268, the third of the "check
    before send" family): every send site -- the focus write (T2), the selection (T3), cmd+A (T4), each chunk (T5)
    -- x {STOP, the window lost, the focus lost}, the condition arising right before that site: nothing is sent
    there, and the result is honest -- nothing sent when nothing went before (or, the window lost before anything
    went, None: the caller's route), else unverified. T1, the insertion: STOP between its selection and its text."""

    SITE_CALL = {"focus write": 1, "select": 2, "cmd+A": 3, "chunk": 4}   # which ask of the window precedes it

    def row(self, site, what):
        from deskmind_hands.grounding import STOP
        STOP.clear()
        self.addCleanup(STOP.clear)
        field = Field("start", takes_insertion=False)            # the insertion refused: the key route
        d = driver()
        calls = [0]
        at = self.SITE_CALL[site]

        def flip():
            if what == "stop":
                STOP.set()
            elif what == "focus":
                field.focused = False

        def lost():
            # the gate reads this (it can wait), then the focus, then asks STOP: whatever arises during the site's
            # own read -- the window gone, a STOP (Codex on #66 @79cd02d: one during this read went unseen), the
            # focus moved -- is seen by that same gate
            calls[0] += 1
            if calls[0] == at and what == "window":
                return "window 200 is no longer the app's key window (now 100)"
            if calls[0] == at:
                flip()
            return None
        prepare_flip = False
        text = "x" * 40
        clear = site in ("select", "cmd+A")

        def run():
            return self.run_type(field, d=d, text=text, clear=clear, selects=site != "cmd+A", count_sends=True,
                                 enter=lambda pid, wid: Key(d.log, lost=lost),
                                 extra=[mock.patch.object(skylight, "text_chunks",
                                                          lambda t, *a: (flip() if prepare_flip else None) or
                                                          _chunks(t, *a))])
        return run()

    def sent_at(self, d, site):
        if site == "focus write":
            return "focus" in d.log
        if site == "select":
            return "select" in d.log
        if site == "cmd+A":
            return ("key", 0, 1 << 20) in d.log
        return len(self.keys_sent(d)) > 1

    def test_every_site_under_every_condition(self):
        for site in ("focus write", "select", "cmd+A", "chunk"):
            for what in ("stop", "window", "focus"):
                if site == "focus write" and what == "focus":
                    continue                                     # the focus is what that write sets
                with self.subTest(site=site, condition=what):
                    d, res = self.row(site, what)
                    self.assertFalse(self.sent_at(d, site), (getattr(res, "detail", res), d.log))
                    if site == "focus write":                    # nothing of this route went before it
                        if what == "stop":
                            self.assertIs(res.performed, False, res.detail)
                        else:
                            self.assertIsNone(res)               # the caller's route, chosen before a send
                    else:
                        self.assertIsNot(res.performed, False, res.detail)
                        self.assertTrue(res.indeterminate, res.detail)

    def test_the_control_every_site_is_reached_when_nothing_happens(self):
        for site in ("focus write", "select", "cmd+A", "chunk"):
            with self.subTest(site=site):
                d, res = self.row(site, None)
                self.assertTrue(self.sent_at(d, site), d.log)

    def test_the_user_coming_to_the_app_during_a_read_sends_nothing_there(self):
        """hands#66's instant checks (Codex @0838225), here: the user comes to the target app during the window read
        right before the focus write (T2), or during the field's focus read right before the first chunk (T5)."""
        for at in ("window read before the focus write", "focus read before the first chunk"):
            with self.subTest(at=at):
                field = Field("start", takes_insertion=False)
                d = driver()

                def lost():
                    if at.startswith("window"):
                        d.front_app = PID                        # the user brought the target app forward
                    return None
                reads = []

                def on_focus_read():
                    reads.append(1)
                    if at.startswith("focus") and len(reads) == 2:   # the select's gate, then the first chunk's
                        d.front_app = PID
                d.on_focus_read = on_focus_read
                d, res = self.run_type(field, d=d, text="x" * 40, count_sends=True,
                                       enter=lambda pid, wid: Key(d.log, lost=lost))
                self.assertEqual(self.keys_sent(d), [])
                if at.startswith("window"):
                    self.assertNotIn("focus", d.log)

    def test_t3_the_length_read_moving_the_focus_writes_no_selection(self):
        """Review of #66 @24fbf21: the selection's length read (it can wait) sat between the gate and the write."""
        field = Field("start", takes_insertion=False)
        d = driver()
        reads = []

        def on_length_read():
            reads.append(1)
            if len(reads) == 2:                                   # T3's read (the first is T1's, the insertion's)
                field.focused = False
        d.on_length_read = on_length_read
        d, res = self.run_type(field, d=d, text="x" * 40, count_sends=True,
                               enter=lambda pid, wid: Key(d.log, lost=lambda: None))
        self.assertNotIn("select", d.log)
        self.assertIsNot(res.performed, False)                   # the focus write went before it: unverified
        self.assertTrue(res.indeterminate, res.detail)

    def test_t1_a_stop_during_the_length_read_writes_nothing(self):
        from deskmind_hands.grounding import STOP
        self.addCleanup(STOP.clear)
        field = Field("start", takes_insertion=True)
        d = driver()
        d.on_length_read = STOP.set
        writes = []
        d, res = self.run_type(field, d=d, insert=lambda el, t, r, check=None, want=None: (check and check()) and (axfield.NOTHING, check())
                               or (writes.append(t), (axfield.TEXT, None))[1])
        self.assertEqual(writes, [])
        self.assertIs(res.performed, False)

    def test_t1_a_stop_between_the_insertions_selection_and_its_text_writes_no_text(self):
        from deskmind_hands.grounding import STOP
        self.addCleanup(STOP.clear)
        writes = []
        with mock.patch.object(axfield, "select_range", lambda el, want, wait=0.3: writes.append("selection") or
                               STOP.set() or True), \
                mock.patch.object(axfield, "_length", lambda el: 4), \
                mock.patch.object(axfield, "_set", lambda el, attr, v: writes.append(attr) or True):
            went, why = axfield.insert("FIELD", "text", False,
                                       check=lambda: "stopped by the app" if STOP.is_set() else None)
        self.assertEqual((went, why), (axfield.SELECTION, "stopped by the app"))
        self.assertEqual(writes, ["selection"])                 # no AXSelectedText after the stop


class TheInsertionBehindItsGate(_TypeHarness):
    """Review of #67 @7926924, T1 (the accessibility insertion): (1) a send of the selection alone was reported as
    "typed N chars"; the text is now written only after the selection reads back as written, and a selection that
    went with no text after it says so (unverified, no keys after a send); (2) T1 asked STOP only, after reads that
    can wait -- the user came to the app during the range read and the insertion went. T1 has a gate now (STOP, the
    target app not in front), asked right before each of its two writes. Driven through _type_in_background and the
    real axfield.insert."""

    def run_t1(self, d, field, text_answer=0, applies=False, **kw):
        texts = []

        def answer(el, attr, v):                                  # the text write, as the OS answers it
            texts.append((attr, v))
            if text_answer == 0 or text_answer not in base.AX_NOT_DELIVERED:
                base.counted(lambda: None)
            if applies:
                field.value += v
            return text_answer
        extra = [mock.patch.object(axfield, "_set", lambda el, attr, v: answer(el, attr, v) == 0),
                 mock.patch.object(axfield, "_set_answer", answer, create=True)]
        d, res = self.run_type(field, d=d, insert=REAL_INSERT, count_sends=True, extra=extra,
                               enter=lambda pid, wid: Key(d.log, lost=lambda: None), **kw)
        return d, res, texts

    def test_a_selection_that_went_and_did_not_read_back_writes_no_text_and_says_so(self):
        d = driver()
        d, res, texts = self.run_t1(d, Field("start", takes_insertion=True), selects=False)
        self.assertEqual(texts, [])                                  # no AXSelectedText
        self.assertEqual(self.keys_sent(d), [])                      # no keys after a send
        self.assertIsNot(res.performed, False)
        self.assertTrue(res.indeterminate, res.detail)
        self.assertIn("no text was written", res.detail)
        self.assertNotIn("typed", res.detail)

    def test_a_stop_after_the_selection_writes_no_text_and_says_so(self):
        from deskmind_hands.grounding import STOP
        self.addCleanup(STOP.clear)
        d = driver()
        d.on_select = STOP.set
        d, res, texts = self.run_t1(d, Field("start", takes_insertion=True))
        self.assertEqual(texts, [])
        self.assertTrue(res.indeterminate, res.detail)
        self.assertIn("no text was written", res.detail)

    def test_the_gate_before_each_write(self):
        """A front-app row and a STOP row for each of T1's two writes: arising during the read before it (the range
        read before the selection; the selection's read-back before the text) -- that write is not made."""
        from deskmind_hands.grounding import STOP
        for write in ("selection", "text"):
            for what in ("front", "stop"):
                with self.subTest(write=write, condition=what):
                    STOP.clear()
                    self.addCleanup(STOP.clear)
                    d = driver()

                    def arise(d=d, what=what):
                        if what == "stop":
                            STOP.set()
                        else:
                            d.front_app = PID                         # the user brought the target app forward
                    if write == "selection":
                        d.on_length_read = arise
                    else:
                        d.on_select = arise
                    d, res, texts = self.run_t1(d, Field("start", takes_insertion=True))
                    self.assertEqual(texts, [])
                    if write == "selection":
                        self.assertNotIn("select", d.log)
                        if what == "stop":
                            self.assertIs(res.performed, False)
                        else:
                            self.assertIsNone(res)                   # nothing went: the caller's route
                    else:
                        self.assertIn("select", d.log)
                        self.assertTrue(res.indeterminate, res.detail)
                        self.assertIn("no text was written", res.detail)

    def test_a_text_write_that_times_out_after_applying_is_read_back_and_verified(self):
        """Codex on #67 @9310989: the OS applied the text, then answered kAXErrorCannotComplete (-25204) -- which
        may have been delivered (base.ax_not_delivered) -- and the result said no text was written."""
        d = driver()
        d, res, texts = self.run_t1(d, Field("start", takes_insertion=True), text_answer=-25204, applies=True)
        self.assertEqual(texts, [("AXSelectedText", "hello 你好")])
        self.assertTrue(res.ok and not res.indeterminate, res.detail)    # read back: there
        self.assertEqual(self.keys_sent(d), [])

    def test_a_text_write_that_times_out_and_shows_nothing_may_have_been_written(self):
        d = driver()
        d, res, texts = self.run_t1(d, Field("start", takes_insertion=True), text_answer=-25204)
        self.assertTrue(res.indeterminate, res.detail)
        self.assertIn("may have been written", res.detail)
        self.assertEqual(self.keys_sent(d), [])

    def test_a_text_write_refused_for_certain_is_the_selection_only(self):
        d = driver()
        d, res, texts = self.run_t1(d, Field("start", takes_insertion=True), text_answer=-25205)
        self.assertTrue(res.indeterminate, res.detail)                  # the selection went
        self.assertIn("no text was written", res.detail)
        self.assertEqual(self.keys_sent(d), [])

    def test_a_field_whose_selection_cannot_be_read_is_handed_back_before_any_send(self):
        """Review of #67 @8e08b5e (A1): the keys' caret and select-all are read back by the same selection, so the
        key route failed after its sends too. Unreadable: neither way here, the caller's route, nothing sent."""
        for clear in (False, True):
            with self.subTest(replace=clear):
                d = driver()
                d.selection_readable = False
                d, res, texts = self.run_t1(d, Field("start", takes_insertion=True), clear=clear)
                self.assertIsNone(res)
                self.assertEqual(texts, [])
                self.assertEqual([e for e in d.log if e in ("focus", "select") or isinstance(e, tuple)
                                  and e[0] in ("keys", "key", "ax insert")], [])

    def test_an_insertion_that_raises_says_what_went(self):
        """Review of #67 @8e08b5e (A2): an exception was taken for a text write. Told by the count now."""
        for counted in (True, False):
            with self.subTest(counted=counted):
                def boom(el, t, r, check=None, want=None, counted=counted):
                    if counted:
                        base.counted(lambda: None)                   # the selection went
                    raise RuntimeError("the accessibility call failed")
                d = driver()
                d, res = self.run_type(Field("start", takes_insertion=True), d=d, insert=boom, count_sends=True,
                                       enter=lambda pid, wid: Key(d.log, lost=lambda: None))
                if counted:
                    self.assertIsNot(res.performed, False)
                    self.assertTrue(res.indeterminate, res.detail)
                    self.assertIn("failed part way", res.detail)
                else:
                    self.assertIsNone(res)
                self.assertEqual(self.keys_sent(d), [])


REAL_INSERT = axfield.insert
_chunks = skylight.text_chunks


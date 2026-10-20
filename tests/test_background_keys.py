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

from deskmind_hands.drivers import axfield, keys, skylight   # noqa: E402
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

    def __init__(self, focused=PREV, obeys_records=True, obeys_ax=True, status=0, front=USER):
        self.focused, self.obeys_records, self.obeys_ax, self.status, self.front = focused, obeys_records, obeys_ax, \
            status, front
        self.sent, self.ax_writes = [], []

    def post(self, psn, rec):
        what = decode(rec)
        self.sent.append(what)
        if self.status == 0 and self.obeys_records and what[0] == "makekey2":
            self.focused = what[1]
        return self.status

    def set_ax(self, pid, wid):
        self.ax_writes.append(wid)
        if self.obeys_ax:
            self.focused = wid
        return True

    def patches(self):
        return [mock.patch.object(skylight, "_load", lambda: {"loaded": True}),
                mock.patch.object(skylight, "_psn_for", lambda wid: "psn"),
                mock.patch.object(skylight, "_post_record", self.post),
                mock.patch.object(skylight, "focused_window", lambda pid: self.focused),
                mock.patch.object(skylight, "_set_focused_window", self.set_ax),
                mock.patch.object(skylight, "front_pid", lambda: self.front),
                mock.patch.object(skylight, "CONFIRM_S", 0), mock.patch.object(skylight, "AX_CONFIRM_S", 0),
                mock.patch.object(skylight.time, "sleep", lambda s: None)]


class Patched(unittest.TestCase):
    def use(self, fake: Fake) -> Fake:
        for p in fake.patches():
            p.start()
            self.addCleanup(p.stop)
        return fake


class EnterWindowKey(Patched):
    def test_records_go_to_the_app_only_in_order_and_the_focus_is_read_back(self):
        app = self.use(Fake())
        key = skylight.enter_window_key(PID, TARGET)
        self.assertEqual(app.sent, [("defocus", PREV), ("focus", TARGET), ("makekey1", TARGET), ("makekey2", TARGET)])
        self.assertEqual((key.via, key.previous), ("records", PREV))
        self.assertEqual(app.ax_writes, [])

    def test_given_back_on_exit(self):
        app = self.use(Fake())
        with skylight.enter_window_key(PID, TARGET) as key:
            app.sent.clear()
        self.assertEqual(app.sent, [("defocus", TARGET), ("focus", PREV), ("makekey1", PREV), ("makekey2", PREV)])
        self.assertTrue(key.restored)
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

    def test_no_previous_window_the_target_is_only_defocused(self):
        app = self.use(Fake(focused=None))
        with skylight.enter_window_key(PID, TARGET):
            app.sent.clear()
        self.assertEqual(app.sent, [("defocus", TARGET)])

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
        with self.assertRaises(skylight.WindowKeyRefused) as cm:
            skylight.enter_window_key(PID, TARGET)
        self.assertIn("could not be confirmed", str(cm.exception))
        self.assertEqual(app.focused, PREV)

    def test_refused_on_1002(self):
        app = self.use(Fake(status=skylight.INVALID_CONNECTION))
        with self.assertRaises(skylight.WindowKeyRefused) as cm:
            skylight.enter_window_key(PID, TARGET)
        self.assertIn("1002", str(cm.exception))
        self.assertEqual(len(app.sent), 4)          # one pass, no retry, no accessibility write
        self.assertEqual(app.ax_writes, [])

    def test_refused_when_the_app_is_the_users_front_app(self):
        app = self.use(Fake(front=PID))
        with self.assertRaises(skylight.WindowKeyRefused) as cm:
            skylight.enter_window_key(PID, TARGET)
        self.assertIn("front app", str(cm.exception))
        self.assertEqual(app.sent, [])

    def test_refused_without_skylight(self):
        with mock.patch.object(skylight, "_load", lambda: None):
            with self.assertRaises(skylight.WindowKeyRefused):
                skylight.enter_window_key(PID, TARGET)


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

    def test_typed_in_is_lenient_on_case_and_strict_on_change(self):
        self.assertTrue(axfield.typed_in("start\n", "start\nAbc XYZ", "abc XYZ", False))   # TextEdit autocapitalises
        self.assertFalse(axfield.typed_in("start", "start", "abc", False))
        self.assertFalse(axfield.typed_in("abc", "abc ", "abc", False))           # changed, but not once more
        self.assertTrue(axfield.typed_in("old", "New text", "new text", True))
        self.assertFalse(axfield.typed_in("old", "old new", "new", True))         # not in place of what was there


class Key:
    """A WindowKey stand-in: records its use."""

    def __init__(self, log, via="records", took=False):
        self.log, self.via, self.took = log, via, took

    def __enter__(self):
        self.log.append("enter")
        return self

    def __exit__(self, *exc):
        self.log.append("exit")
        return False

    def user_took_front(self):
        return self.took


def driver():
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.app, d._origin, d._window_id, d._pinned_window = "com.apple.TextEdit", Point(0, 0), "200", "200"
    d._pid_of = lambda bundle, all_wins=None: PID
    d._rects, d._labels_by_id, d._element_role = {"t1": Rect(0, 30, 600, 500)}, {"t1": "正文"}, {"t1": "textarea"}
    d._element_window = {"t1": "200"}
    d._preexisting = set()
    d._background_keys = lambda: True
    d.log = []
    d._post_unicode = lambda q, text, stop=None: d.log.append(("keys", text)) or len(text)
    d._post_key = lambda q, code, flags=0: d.log.append(("key", code, flags))
    d._load_keys = lambda: None
    d.CHORD_SETTLE_S = 0
    return d


class Field:
    """A text field whose value the mocked accessibility calls read and write."""

    def __init__(self, value, takes_insertion):
        self.value, self.takes = value, takes_insertion


class TypeText(unittest.TestCase):
    def run_type(self, field, keys_land=True, enter=None, text="hello 你好", clear=False, selects=True,
                 all_selected=True, d=None, settle=None, value=None, insert=None, extra=()):
        d = d or driver()
        d._keycode = lambda q, letter: {"a": 0}[letter]

        def post_key(q, code, flags=0):                                 # cmd+A selects all: the next keys replace it
            d.log.append(("key", code, flags))
            if flags:
                field.value = ""
        d._post_key = post_key
        if keys_land:
            d._post_unicode = lambda q, t, stop=None: (d.log.append(("keys", t)),
                                                       setattr(field, "value", field.value + t.capitalize()))[0] \
                or len(t)

        def ax_insert(el, t, replace_all):
            d.log.append(("ax insert", t))
            if field.takes:
                field.value = t if replace_all else field.value + t
            return True
        patches = [*extra, mock.patch.object(axfield, "field_at", lambda pid, wid, frame: field),
                   mock.patch.object(axfield, "value", value or (lambda el: field.value)),
                   mock.patch.object(axfield, "insert", insert or ax_insert),
                   mock.patch.object(axfield, "settled_value", settle or (lambda el, before, wait=0.6: field.value)),
                   mock.patch.object(axfield, "focus", lambda el: d.log.append("focus") or True),
                   mock.patch.object(axfield, "select", lambda el, replace_all: d.log.append("select") or selects),
                   mock.patch.object(axfield, "all_selected", lambda el, wait=0.5: all_selected),
                   mock.patch.object(skylight, "BackgroundQuartz",
                                     lambda pid, wid, origin: types.SimpleNamespace(kCGEventFlagMaskCommand=1 << 20)),
                   mock.patch.object(skylight, "enter_window_key",
                                     enter or (lambda pid, wid: d.log.append(("key window", pid, wid)) or Key(d.log)))]
        for p in patches:
            p.start()
        try:
            return d, d._type_in_background("t1", text, clear)
        finally:
            for p in patches:
                p.stop()

    def test_the_accessibility_insertion_first(self):
        d, res = self.run_type(Field("start\n", takes_insertion=True))
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertIn("accessibility insertion", res.detail)
        self.assertEqual(d.log, [("ax insert", "hello 你好")])          # no window made key, no key sent

    def test_keys_when_the_insertion_changes_nothing(self):
        d, res = self.run_type(Field("start", takes_insertion=False))
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertIn("keys to window 200", res.detail)
        self.assertEqual(d.log, [("ax insert", "hello 你好"), ("key window", PID, 200), "enter", "focus", "select",
                                 ("keys", "hello 你好"), "exit"])

    def test_replace_selects_all_by_key_when_accessibility_will_not(self):
        field = Field("old", takes_insertion=False)
        d, res = self.run_type(field, clear=True, selects=False, text="new")
        self.assertIn(("key", 0, 1 << 20), d.log)                       # cmd+A, then the text
        self.assertEqual(d.log.index(("key", 0, 1 << 20)) + 1, d.log.index(("keys", "new")))
        self.assertTrue(res.ok and not res.indeterminate, res.detail)    # "New": TextEdit's capital, still verified

    def test_replace_refused_when_nothing_reads_back_selected(self):
        d, res = self.run_type(Field("old", takes_insertion=False), clear=True, selects=False, all_selected=False)
        self.assertFalse(res.ok)
        self.assertIn("could not be selected", res.detail)
        self.assertNotIn("keys", [e[0] for e in d.log if isinstance(e, tuple)])

    def test_an_insertion_shown_late_is_not_typed_again(self):
        """The value shows the insertion only after ~1 s: the wait (1.5 s) sees it, and no key is sent."""
        field, clock, pending = Field("start", takes_insertion=False), [0.0], []
        real_settle = axfield.settled_value

        def value(el):
            if pending and clock[0] >= 1.0:
                field.value += pending.pop()
            return field.value

        def insert(el, t, replace_all):
            pending.append(t)
            return True
        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0],
                                          sleep=lambda dt: clock.__setitem__(0, clock[0] + dt))
        d, res = self.run_type(field, settle=real_settle, value=value, insert=insert,
                               extra=[mock.patch.object(axfield, "time", fake_time)])
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertGreaterEqual(clock[0], 1.0)
        self.assertEqual([e for e in d.log if e != "select"], [])        # no window made key, no key sent
        self.assertNotIn(str(200), d.__dict__.get("_ax_noop_windows", set()))

    def test_an_insertion_shown_just_before_the_keys_is_not_typed_again(self):
        field = Field("start", takes_insertion=False)
        reads = []

        def value(el):                       # the read right before the keys is the first to show it
            reads.append(1)
            if len(reads) == 2:
                field.value = "start" + "hello 你好"
            return field.value
        d, res = self.run_type(field, value=value, settle=lambda el, before, wait=0.6: before)
        self.assertTrue(res.ok and not res.indeterminate, res.detail)
        self.assertIn("shown late", res.detail)
        self.assertNotIn("keys", [e[0] for e in d.log if isinstance(e, tuple)])

    def test_a_window_where_the_insertion_did_nothing_goes_straight_to_keys(self):
        """Chrome: accepted and nothing changed after the whole wait -- remembered for that window, not the app."""
        d = driver()
        d, res = self.run_type(Field("start", takes_insertion=False), d=d)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(d._ax_noop_windows, {"200"})
        d.log.clear()
        d, res = self.run_type(Field("start", takes_insertion=False), d=d, text="again")
        self.assertTrue(res.ok, res.detail)
        self.assertNotIn("ax insert", [e[0] for e in d.log if isinstance(e, tuple)])
        self.assertIn(("keys", "again"), d.log)
        d.log.clear()
        d._window_id = d._pinned_window = "201"                         # another window of the same app
        d._element_window = {"t1": "201"}
        d, res = self.run_type(Field("start", takes_insertion=False), d=d, text="third")
        self.assertIn(("ax insert", "third"), d.log)

    def test_unverified_when_the_keys_do_not_read_back(self):
        d, res = self.run_type(Field("start", takes_insertion=False), keys_land=False)
        self.assertTrue(res.indeterminate)
        self.assertIn("unverified", res.detail)

    def test_refused_and_nothing_typed_when_the_window_is_not_proven_key(self):
        def refuse(pid, wid):
            raise skylight.WindowKeyRefused("window 200 could not be confirmed as the app's key window")
        d, res = self.run_type(Field("start", takes_insertion=False), enter=refuse)
        self.assertFalse(res.ok)
        self.assertIn("could not be confirmed", res.detail)
        self.assertNotIn("keys", [e[0] for e in d.log if isinstance(e, tuple)])

    def test_stopped_before_anything(self):
        STOP.set()
        try:
            d, res = self.run_type(Field("start", takes_insertion=True))
        finally:
            STOP.clear()
        self.assertFalse(res.ok)
        self.assertEqual(d.log, [])

    def test_no_field_to_read_back_by_leaves_it_to_the_old_route(self):
        d = driver()
        with mock.patch.object(axfield, "field_at", lambda pid, wid, frame: None):
            self.assertIsNone(d._type_in_background("t1", "x", False))

    def test_a_window_the_run_does_not_own_is_left_to_the_old_route(self):
        d = driver()
        d._preexisting = {"200"}
        with mock.patch.object(axfield, "field_at", side_effect=AssertionError("not looked at")):
            self.assertIsNone(d._type_in_background("t1", "x", False))


class Chord(unittest.TestCase):
    def press(self, states, enter=None, chord="cmd+shift+n"):
        d = driver()
        seq = iter(states)
        with mock.patch.object(axfield, "window_state", lambda pid, wid: next(seq)), \
                mock.patch.object(skylight, "BackgroundQuartz", lambda pid, wid, origin: "Q"), \
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

    def test_refused_when_the_window_is_not_proven_key(self):
        def refuse(pid, wid):
            raise skylight.WindowKeyRefused("the app is the user's front app")
        d, res = self.press([None, None], enter=refuse)
        self.assertFalse(res.ok)
        self.assertIn("front app", res.detail)
        self.assertEqual(d.log, [])

    def test_not_the_runs_window_not_pressed(self):
        d = driver()
        d._preexisting = {"200"}
        res = d._press_in_background("cmd+a")
        self.assertFalse(res.ok)
        self.assertIn("not one this run may type into", res.detail)


if __name__ == "__main__":
    unittest.main()

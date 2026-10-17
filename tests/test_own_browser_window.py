"""A run does its browser work in a window it opened itself, never in a window of the user's (deskmind#73).

Found live on 0.4.3-rc.2 (hands 07a9a75): with Safari's only window minimised and Finder in front, staging ran
`open -b com.apple.Safari` (rewritten to `open -g -b`). On a running app both are a reopen, which un-minimised the
user's window; the run then read Safari by app, Peekaboo picked that window, and the search navigated the user's tab.
With a user's window on screen, the read picked it the same way.

Nothing here touches the Mac. A stand-in Mac holds Safari's windows and plays the parts that matter: an `open` of a
running Safari un-minimises its windows (the reopen), an `open` of a stopped one launches it and brings back the last
session's windows, AppleScript's `make new document` adds a window, and a read of the app picks the front window on
screen. Peekaboo, AppleScript, `open`, Quartz, AppKit, SkyLight and the clipboard are all stood in for.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.actions import Action, ActionKind   # noqa: E402
from deskmind_hands.drivers import axchrome, skylight   # noqa: E402
from deskmind_hands.drivers import peekaboo as pk   # noqa: E402
from deskmind_hands.drivers.base import ExecResult   # noqa: E402
from deskmind_hands.drivers.peekaboo import WEB_SEARCH, PeekabooDriver   # noqa: E402
from deskmind_hands.geometry import Rect   # noqa: E402

SAFARI = "com.apple.Safari"
PID = 500
REAL_RUN = subprocess.run


class FakeMac:
    """Safari's windows, front first. Each window: id, title, on_screen, frame (x, y, w, h)."""

    def __init__(self, windows=(), running=True, restored=()):
        self.windows = [dict(w) for w in windows]
        self.running = running
        self.restored = [dict(w) for w in restored]   # what a launch brings back from the last session
        self.next_id = 30000
        self.events: list[tuple] = []
        self.user_opens_during_make = False     # the user opens a window between the make and the script's answer
        self.idle = 1e6          # seconds since the user last touched anything

    # -- what happens on the Mac
    def open_app(self, argv):
        self.events.append(("open", tuple(argv)))
        if SAFARI.lower() not in " ".join(argv).lower() and "safari" not in " ".join(argv).lower():
            return
        if self.running:
            # The reopen: every minimised window comes back, and a Safari with none opens one.
            for w in self.windows:
                w["on_screen"] = True
            if not self.windows:
                self.new_window("起始页")
        else:
            self.running = True
            self.windows = self.restored + self.windows
            if not self.windows:
                self.new_window("起始页")

    def new_window(self, title, url=""):
        self.next_id += 1
        frame = (200 + 20 * len(self.windows), 100, 1000, 700)
        self.windows.insert(0, {"id": self.next_id, "title": title, "on_screen": True, "frame": frame, "url": url})
        return self.next_id

    def find(self, wid):
        return next((w for w in self.windows if str(w["id"]) == str(wid)), None)

    # -- Peekaboo
    def peekaboo(self, key, _extra=None, **kw):
        self.events.append(("peekaboo", key, kw.get("app"), kw.get("window")))
        safari = (kw.get("app") or "").lower() == SAFARI.lower()
        if key in ("window_list", "window_list_of"):
            if not safari:
                return True, {"windows": []}, ""
            if not self.running:
                return False, {}, "APP_NOT_FOUND: Safari is not running"
            return True, {"windows": [{"window_id": w["id"], "window_title": w["title"], "window_index": i,
                                       "is_on_screen": w["on_screen"],
                                       "bounds": dict(zip(("x", "y", "width", "height"), w["frame"]))}
                                      for i, w in enumerate(self.windows)]}, ""
        if key == "see":
            # Asked for the app, Peekaboo picks the front window on screen itself.
            w = next((w for w in self.windows if w["on_screen"]), None) if self.running and safari else None
        elif key == "see_window":
            w = self.find(kw.get("window"))
            w = w if w and w["on_screen"] else None
        else:
            return False, {}, f"not stood in for: {key}"
        if w is None:
            return False, {}, "WINDOW_NOT_FOUND: No window found for application com.apple.Safari"
        self.events.append(("read", str(w["id"])))
        x, y, ww, hh = w["frame"]
        return True, {"coordinate_context": {"window": {"window_id": w["id"]},
                                             "logical_bounds": [[x, y], [x + ww, y + hh]]},
                      "application_name": "Safari浏览器", "window_title": w["title"], "ui_elements": [],
                      "snapshot_id": f"snap-{w['id']}"}, ""

    # -- AppleScript
    def osa(self, script):
        self.events.append(("osascript", script))
        if "make new document" in script:
            if not self.running:
                self.running = True           # AppleScript launches the app it names
            url = script.split('URL:"', 1)[1].split('"', 1)[0] if 'URL:"' in script else ""
            # A data: page titled with the run's marker: the window takes that title.
            title = url.split("<title>", 1)[1].split("</title>", 1)[0] if "<title>" in url else "起始页"
            made = self.new_window(title, url)
            if self.user_opens_during_make:
                self.new_window("用户刚开的窗口")            # in front now, and new as well
            if "front window" in script:
                return True, str(self.windows[0]["id"])
            if "whose document is d" in script:
                return False, "不能将“document”转换为“specifier”类型。 (-1700)"     # as Safari answered, live 10-17
            if 'whose name is "' in script:
                name = script.split('whose name is "', 1)[1].split('"', 1)[0]
                hit = next((w for w in self.windows if w["title"] == name), None)
                return True, str(hit["id"]) if hit else "none"
            return False, "unknown script"
        if "set URL of current tab of window id" in script:
            wid = script.split("window id ", 1)[1].split()[0]
            w = self.find(wid)
            if w is None:
                return False, "no such window"
            w["url"] = script.split(' to "', 1)[1].rsplit('"', 1)[0]
            w["title"] = "已载入"
            return True, ""
        if "set bounds of window id" in script:
            wid = script.split("window id ", 1)[1].split()[0]
            a, b, c, e = (int(v) for v in script.split("{", 1)[1].split("}", 1)[0].split(","))
            self.find(wid)["frame"] = (a, b, c - a, e - b)
        return True, ""

    # -- subprocesses
    def subprocess_run(self, argv, *a, **kw):
        if argv[0] == "/usr/bin/open":
            self.open_app(argv)
        elif argv[0] == "/bin/sh":
            cmd = argv[2]
            if cmd.startswith("printf"):
                return REAL_RUN(argv, *a, **kw)
            self.events.append(("sh", cmd))
            if cmd.lstrip().startswith("open"):
                self.open_app(cmd.split())
        else:
            self.events.append(("run", tuple(argv)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    # -- Quartz / AppKit
    def quartz(self):
        def windows(option, wid):
            if option == 8:     # kCGWindowListOptionIncludingWindow
                w = self.find(wid)
                return [{"kCGWindowBounds": dict(zip(("X", "Y", "Width", "Height"), w["frame"]))}] if w else []
            return [{"kCGWindowNumber": w["id"], "kCGWindowOwnerPID": PID, "kCGWindowLayer": 0,
                     "kCGWindowIsOnscreen": w["on_screen"],
                     "kCGWindowBounds": dict(zip(("X", "Y", "Width", "Height"), w["frame"]))}
                    for w in self.windows] if self.running else []
        return types.SimpleNamespace(
            kCGWindowListOptionAll=0, kCGWindowListOptionIncludingWindow=8, CGWindowListCopyWindowInfo=windows,
            CGEventSourceSecondsSinceLastEventType=lambda state, kind: self.idle,
            kCGEventSourceStateHIDSystemState=1, kCGAnyInputEventType=~0,
            CGSessionCopyCurrentDictionary=lambda: {"kCGSSessionOnConsoleKey": True},
            kCGSessionOnConsoleKey="kCGSSessionOnConsoleKey")

    def appkit(self):
        running = types.SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=lambda b: ["Safari"] if self.running else [])
        return types.SimpleNamespace(NSRunningApplication=running)

    def pid_of(self, bundle, all_wins=None):
        return PID if bundle.lower() == SAFARI.lower() and self.running and self.windows else None


class OwnWindow(unittest.TestCase):
    def mac(self, mac: FakeMac, *, app=SAFARI, stage=("open -b com.apple.Safari",), stage_apps=None):
        """The driver as `hands do --app com.apple.Safari` builds it, started on `mac`."""
        self.scratch = Path(tempfile.mkdtemp())
        (self.scratch / "ws").mkdir()
        patches = [mock.patch.dict(sys.modules, {"Quartz": mac.quartz(), "AppKit": mac.appkit()}),
                   mock.patch.dict(os.environ, {"HANDS_PERMISSIONS_VERIFIED": "1"}),
                   mock.patch.object(pk.subprocess, "run", mac.subprocess_run),
                   mock.patch.object(pk.time, "sleep", lambda s: None)]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        d = PeekabooDriver(app=app, binary=sys.executable, scratch=self.scratch, stage_commands=list(stage))
        if stage_apps is not None:
            d._stage_apps = list(stage_apps)
        d._run, d._osa, d._pid_of = mac.peekaboo, mac.osa, mac.pid_of
        d.clipboard = lambda: ""
        d._set_clipboard = lambda value: None
        d._frontmost = lambda: "Finder"
        d._sparse_tree = lambda data: False
        d._element_cache = lambda data, origin: setattr(d, "_real_read", 1) or []
        d.start(self.scratch / "ws")
        return d

    def reads(self, mac):
        return [e[1] for e in mac.events if e[0] == "read"]

    # -- a minimised window of the user's ------------------------------------------------------------------------

    def test_a_minimised_window_is_never_unminimised_or_used(self):
        mac = FakeMac([{"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        obs = d.observe()
        self.assertFalse(mac.find(19085)["on_screen"], "the user's minimised window was un-minimised")
        self.assertEqual([e for e in mac.events if e[0] in ("open", "sh")], [])      # no reopen sent at all
        self.assertNotIn("19085", self.reads(mac))
        own = d.state()["own_browser_windows"][SAFARI.lower()]
        self.assertNotEqual(own, "19085")
        self.assertEqual(d._pinned_window, own)
        self.assertEqual(self.reads(mac), [own])
        self.assertNotIn("19085", [w.id for w in obs.windows])

    # -- a window of the user's on screen ------------------------------------------------------------------------

    def test_an_on_screen_window_of_the_users_is_never_read_or_navigated(self):
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        obs = d.observe()
        self.assertNotIn("19116", self.reads(mac))
        self.assertNotIn("19116", [w.id for w in obs.windows])
        # Switching to it is refused: it is not one of the windows offered.
        res = d.execute(Action(kind=ActionKind.FOCUS_WINDOW, text="19116"))
        self.assertFalse(res.ok)
        self.assertNotEqual(d._pinned_window, "19116")

    def test_the_web_search_goes_to_the_runs_own_window(self):
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)},
                       {"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        d.observe()
        own = d.state()["own_browser_windows"][SAFARI.lower()]
        own_frame = mac.find(own)["frame"]
        focused, posted = [], []
        d._address_bar, d._address_bar_rect = "e48", Rect(275, 18, 237, 31)
        d._background_input = lambda bundle: True
        d._wait_until_still = lambda bundle, what="": None
        d._settle = lambda ok, detail: ExecResult(ok, detail)
        d._post_click = lambda q, x, y: posted.append(("click", x, y))
        d._paste_and_submit = lambda q, text, before_return=None: posted.append(("paste", text)) or True
        with mock.patch.object(skylight, "focus_without_raise", lambda pid, wid: focused.append(wid) or "token"), \
                mock.patch.object(skylight, "give_focus_back", lambda token: None), \
                mock.patch.object(skylight, "BackgroundQuartz", lambda pid, wid, origin: ("Q", wid)), \
                mock.patch.object(axchrome, "focused_identifier",
                                  lambda pid, frame, wid=None: "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD"
                                  if frame == own_frame else "another window"):
            d.allow_browser_field(WEB_SEARCH)
            res = d._execute_projected(Action(kind=ActionKind.TYPE_TEXT, text="新加坡 天气",
                                              binding=pk.Binding(observation_id="o", app="Safari",
                                                                 element_id=WEB_SEARCH)))
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(focused, [int(own)])                  # the keyboard went to the run's window only
        self.assertEqual(posted[-1], ("paste", "新加坡 天气"))
        self.assertTrue(mac.find(19116)["on_screen"] and not mac.find(19085)["on_screen"])

    # -- a Safari that is not running ----------------------------------------------------------------------------

    def test_a_fresh_launch_works_and_the_restored_windows_stay_the_users(self):
        mac = FakeMac(running=False,
                      restored=[{"id": 18000, "title": "上次的标签页", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        obs = d.observe()
        launches = [e for e in mac.events if e[0] == "open"]
        self.assertEqual(len(launches), 1)
        self.assertIn("-g", launches[0][1])                     # in the background
        self.assertIn("-ApplePersistenceIgnoreState", launches[0][1])
        own = d.state()["own_browser_windows"][SAFARI.lower()]
        self.assertNotEqual(own, "18000")
        self.assertEqual(self.reads(mac), [own])
        self.assertFalse(obs.no_window)
        self.assertNotIn("18000", [w.id for w in obs.windows])

    def test_a_running_safari_gets_no_open_at_all(self):
        # Any `open` of a running app is a reopen: it is never sent, with -g or without.
        mac = FakeMac([{"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)}])
        self.mac(mac)
        self.assertEqual([e for e in mac.events if e[0] in ("open", "sh")], [])
        self.assertEqual(sum("make new document" in e[1] for e in mac.events if e[0] == "osascript"), 1)

    # -- other ways in -------------------------------------------------------------------------------------------

    def test_a_staged_page_opens_in_the_runs_own_window(self):
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac, stage=('open -a Safari "$WS/page.html"',))
        own = mac.find(d._pinned_window)
        self.assertTrue(own["url"].startswith("file://") and own["url"].endswith("/ws/page.html"), own)
        self.assertNotEqual(d._pinned_window, "19116")
        self.assertEqual([e for e in mac.events if e[0] in ("open", "sh")], [])

    def test_safari_as_another_app_of_the_task_offers_only_its_own_window(self):
        # TextEdit first, Safari named too (the app's --apps): Safari's windows offered to switch to are the run's.
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac, app="com.apple.TextEdit", stage=(), stage_apps=[SAFARI])
        d._mcp = object()
        own = d.state()["own_browser_windows"][SAFARI.lower()]
        offered = [w.id for w in d._windows()]
        self.assertIn(own, offered)
        self.assertNotIn("19116", offered)

    def test_focus_app_to_safari_opens_the_runs_own_window_without_open(self):
        mac = FakeMac([{"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac, app="com.apple.TextEdit", stage=())
        res = d.execute(Action(kind=ActionKind.FOCUS_APP, text=SAFARI))
        self.assertTrue(res.ok, res.detail)
        self.assertFalse(mac.find(19085)["on_screen"])
        self.assertEqual([e for e in mac.events if e[0] in ("open", "sh")], [])
        self.assertEqual(d._pinned_window, d.state()["own_browser_windows"][SAFARI.lower()])
        d.observe()
        self.assertNotIn("19085", self.reads(mac))

    def test_unpinned_a_browser_is_read_only_by_the_id_of_a_window_it_may_use(self):
        # No window of its own (Safari not staged): the user's window on screen is still not read.
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac, stage=())
        obs = d.observe()
        self.assertTrue(obs.no_window)
        self.assertEqual(self.reads(mac), [])

    def test_a_browser_with_no_scripted_window_gets_none_and_no_open(self):
        mac = FakeMac()
        d = self.mac(mac, app="com.apple.TextEdit", stage=())
        d._own_browser_window("org.mozilla.firefox")
        self.assertIn("no way to open", d.state()["own_browser_miss"])
        self.assertEqual([e for e in mac.events if e[0] in ("open", "sh")], [])

    def test_a_window_that_cannot_be_told_from_the_users_is_not_taken(self):
        # The script answered, but no new window shows in the list: nothing is pinned, the user's is not used.
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        mac.osa = lambda script: (True, "19116")
        d = self.mac(mac)
        self.assertIsNone(d._pinned_window)
        self.assertIn("did not show up", d.state()["own_browser_miss"])

    # -- review: the other ways a window could be picked ---------------------------------------------------------

    def test_following_a_new_window_never_lands_on_the_users(self):
        # The pinned window reads "incomplete": the run follows a window that was not there at its last look. On a
        # first look nothing was seen yet, so every window of the app passed for new, the user's minimised one too.
        mac = FakeMac([{"id": 19085, "title": "收藏夹", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        own = d._pinned_window
        base = mac.peekaboo

        def incomplete(key, _extra=None, **kw):
            if key == "see_window" and kw.get("window") == own:
                return False, {}, "AX tree incomplete: retry once to obtain a fresh observation"
            return base(key, _extra, **kw)
        d._run = incomplete
        with self.assertRaises(pk.DriverUnavailable):     # nothing else to read: the read fails as it would have
            d.observe()
        self.assertNotIn("19085", self.reads(mac))
        self.assertNotEqual(d._pinned_window, "19085")

    def test_background_input_into_a_browser_never_falls_back_to_its_largest_window(self):
        # No window of the run's own (none could be opened): a key or click has nowhere to go, not the user's window.
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1600, 1000)}])
        d = self.mac(mac, stage=())
        d._pinned_window = d._window_id = None
        with mock.patch.object(skylight, "focus_without_raise", lambda pid, wid: self.fail(f"focused {wid}")), \
                mock.patch.object(skylight, "BackgroundQuartz", lambda pid, wid, origin: ("Q", wid)):
            self.assertIsNone(d._background_quartz(SAFARI))

    def test_focus_app_to_the_browser_already_observed_is_no_success(self):
        # A switch that cannot change anything is refused, as for any app (one run repeated one 188 times).
        mac = FakeMac()
        d = self.mac(mac)
        res = d.execute(Action(kind=ActionKind.FOCUS_APP, text="Safari"))
        self.assertFalse(res.ok)
        self.assertIn("already observing", res.detail)

    def test_several_new_windows_without_the_scripts_id_are_none_taken(self):
        # A launch still restoring the last session while the run's window opens: which new one is the run's is not
        # known, so none is used.
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        make = mac.osa

        def two(script):  # noqa: E306
            ok, out = make(script)
            if "make new document" in script:
                mac.new_window("恢复的标签页")
                return ok, "not-a-window-id"
            return ok, out
        mac.osa = two
        d = self.mac(mac)
        self.assertIsNone(d._pinned_window)
        self.assertIn("so none is taken", d.state()["own_browser_miss"])
        obs = d.observe()                       # and neither new window is read or offered afterwards
        self.assertTrue(obs.no_window)
        self.assertEqual(self.reads(mac), [])
        self.assertEqual([w.id for w in obs.windows], [])

    # -- second review: only the run's own windows, not "all but the user's at the start" ----------------------

    def test_a_window_the_user_opens_during_the_run_is_not_the_runs(self):
        mac = FakeMac()
        d = self.mac(mac)
        own = d._pinned_window
        users = str(mac.new_window("用户自己新开的"))             # ⌘N by the user, between two steps
        d._pinned_window = d._window_id = None                    # even with nothing pinned
        obs = d.observe()
        self.assertEqual(self.reads(mac), [own])
        self.assertNotIn(users, [w.id for w in obs.windows])
        res = d.execute(Action(kind=ActionKind.FOCUS_WINDOW, text=users))
        self.assertFalse(res.ok)

    def test_a_new_window_during_the_runs_own_step_is_not_the_runs(self):
        # A session Safari restores late needs no input from anyone, and can come up during any step the run takes,
        # with the user away: nothing ties it to the step, so it is not the run's (Codex review). Nor, by the same
        # token, a pop-up the run's own click opens: the planner stops or asks.
        mac = FakeMac()
        d = self.mac(mac)
        d.observe()
        opened = []
        d._execute_outer = lambda action: opened.append(mac.new_window("恢复的窗口")) or ExecResult(True, "clicked")
        d.execute(Action(kind=ActionKind.KEY, keys=("cmd", "l")))
        self.assertNotIn(str(opened[0]), [w.id for w in d.observe().windows])
        self.assertEqual(d._run_windows, {d._pinned_window})

    def test_a_window_that_opens_during_a_step_while_the_user_works_is_not_the_runs(self):
        mac = FakeMac()
        d = self.mac(mac)
        d.observe()
        mac.idle = 0.0                                            # the user is typing during the step
        opened = []
        d._execute_outer = lambda action: opened.append(mac.new_window("谁开的？")) or ExecResult(True, "clicked")
        d.execute(Action(kind=ActionKind.KEY, keys=("cmd", "n")))
        self.assertNotIn(str(opened[0]), [w.id for w in d._windows()])

    def test_a_browser_is_never_brought_forward_by_the_foreground_route(self):
        mac = FakeMac([{"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac)
        d._background_input = lambda bundle: False
        with mock.patch.dict(os.environ, {"HANDS_FLASH_APPS": SAFARI}):
            ok, detail = d._flash(SAFARI, lambda q: "clicked")
        self.assertFalse(ok)
        self.assertEqual([e for e in mac.events if e[0] in ("open", "sh")], [])
        self.assertFalse(mac.find(19085)["on_screen"])

    def test_focus_app_retries_the_start_browser_and_a_miss_is_no_success(self):
        mac = FakeMac()
        fail = {"on": True}
        make = mac.osa
        mac.osa = lambda script: (False, "execution error") if fail["on"] and "make new" in script else make(script)
        d = self.mac(mac)
        self.assertIsNone(d._pinned_window)
        res = d.execute(Action(kind=ActionKind.FOCUS_APP, text=SAFARI))
        self.assertFalse(res.ok)                                  # no window: not reported as switched
        self.assertNotIn("already observing", res.detail)
        fail["on"] = False
        res = d.execute(Action(kind=ActionKind.FOCUS_APP, text=SAFARI))
        self.assertTrue(res.ok, res.detail)
        self.assertIsNotNone(d._pinned_window)

    def test_one_browser_named_twice_gets_one_window(self):
        mac = FakeMac()
        self.mac(mac, stage_apps=["com.apple.safari"])
        self.assertEqual(sum("make new" in e[1] for e in mac.events if e[0] == "osascript"), 1)
        # Handled once, under one spelling: a second pass asked about "com.apple.safari" and could lose the window.
        self.assertEqual([e for e in mac.events if e[0] == "peekaboo" and e[2] == "com.apple.safari"], [])

    def test_chrome_by_its_app_name_is_a_browser(self):
        mac = FakeMac()
        d = self.mac(mac, app="com.apple.TextEdit", stage=())
        d._stage_apps = ["Google Chrome"]
        d._run = lambda key, _extra=None, **kw: (True, {"windows": [
            {"window_id": 5, "window_title": "用户的 Chrome", "is_on_screen": True}]}, "") \
            if kw.get("app") in ("com.google.Chrome", "Google Chrome") else mac.peekaboo(key, _extra, **kw)
        d._mcp = object()
        self.assertNotIn("5", [w.id for w in d._windows()])

    def test_a_chromium_launch_gets_no_safari_flag(self):
        mac = FakeMac(running=False)
        d = self.mac(mac, app="com.apple.TextEdit", stage=())
        d._app_is_running = lambda bundle: False
        d._launch_in_background("com.google.Chrome")
        argv = [e[1] for e in mac.events if e[0] == "open"][-1]
        self.assertNotIn("-ApplePersistenceIgnoreState", argv)
        self.assertNotIn("YES", argv)

    # -- third review -------------------------------------------------------------------------------------------

    def test_a_wait_or_a_refused_step_adopts_no_window(self):
        # Safari restores a session window by window during the first steps: a WAIT is no input from the run, and a
        # refused step delivered none, so what comes up meanwhile is not the run's.
        mac = FakeMac()
        d = self.mac(mac)
        d.observe()
        opened = []
        d._execute_outer = lambda action: opened.append(mac.new_window("恢复的窗口")) or ExecResult(True, "waited")
        d.execute(Action(kind=ActionKind.WAIT, duration_s=0.1))
        d._execute_outer = lambda action: opened.append(mac.new_window("恢复的窗口")) or ExecResult(False, "refused")
        d.execute(Action(kind=ActionKind.KEY, keys=("cmd", "n")))
        offered = [w.id for w in d.observe().windows]
        self.assertFalse(set(map(str, opened)) & set(offered), offered)

    def automation(self, answer):
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        make = mac.osa
        state = {"answer": answer}
        mac.osa = lambda script: (False, state["answer"]) if "make new" in script and state["answer"] else make(script)
        return mac, state

    def test_an_unanswered_automation_prompt_is_said_plainly_and_tried_again(self):
        mac, state = self.automation("Command '['/usr/bin/osascript', '-e', '...']' timed out after 15 seconds")
        d = self.mac(mac)
        miss = d.state()["own_browser_miss"]
        self.assertIn("Privacy & Security › Automation › DeskMind Hands › Safari", miss)
        self.assertNotIn("osascript", miss)
        obs = d.observe()                       # retried once on the look: still unanswered
        self.assertTrue(obs.no_window)
        self.assertTrue(any("Automation" in n for n in obs.notes), obs.notes)   # the planner and the judge see why
        self.assertEqual(self.reads(mac), [])
        state["answer"] = None                  # the user clicks Allow late: not retried again on every look
        self.assertTrue(d.observe().no_window)

    def test_the_user_allowing_late_gets_the_run_its_window_on_the_next_look(self):
        mac, state = self.automation("execution error: timed out")
        d = self.mac(mac)
        state["answer"] = None
        obs = d.observe()
        self.assertFalse(obs.no_window)
        self.assertEqual(self.reads(mac), [d.state()["own_browser_windows"][SAFARI.lower()]])
        self.assertNotIn("19116", self.reads(mac))

    def test_an_old_reason_is_not_repeated_once_the_window_opened(self):
        # Staging timed out, the look's retry opened the window; the run's window closed later, and the next look with
        # no window still said "no answer came in time" (re-review). Kept per browser, too.
        mac, state = self.automation("execution error: timed out")
        d = self.mac(mac)
        d._own_browser_window("org.mozilla.firefox")             # another browser's reason
        state["answer"] = None
        self.assertFalse(d.observe().no_window)                  # the retry opened it
        self.assertNotIn("no answer came", d.state()["own_browser_miss"])
        mac.windows = [w for w in mac.windows if str(w["id"]) != d._pinned_window]   # the run's window is closed
        obs = d.observe()
        self.assertTrue(obs.no_window)
        self.assertFalse(any("no answer came" in n or "firefox" in n for n in obs.notes), obs.notes)

    def test_the_setting_is_named_as_macos_shows_the_helper(self):
        mac, _ = self.automation("execution error: timed out")
        d = self.mac(mac)
        self.assertIn("DeskMind Hands may control Safari", d.state()["own_browser_miss"])
        self.assertIn("› Automation › DeskMind Hands › Safari", d.state()["own_browser_miss"])

    def test_a_denied_automation_names_the_setting(self):
        mac, _ = self.automation("execution error: Not authorized to send Apple events to Safari. (-1743)")
        d = self.mac(mac)
        self.assertIn("does not allow DeskMind Hands to control Safari", d.state()["own_browser_miss"])
        self.assertIn("System Settings › Privacy & Security › Automation", d.state()["own_browser_miss"])

    def test_the_done_judge_is_told_why_there_is_no_window(self):
        from deskmind_hands.runtime import judge
        prompts = []
        judge.done_unseen(lambda p, s, o: prompts.append(p) or {"choice": judge.NOT_SHOWN}, "搜索天气", SAFARI,
                          done=["no window"], why="macOS does not allow DeskMind Hands to control Safari")
        self.assertIn("does not allow DeskMind Hands to control Safari", str(prompts))

    def test_a_users_window_with_the_same_frame_does_not_stop_the_search(self):
        # The user's window zoomed to the very place and size the run's new window gets: checks by frame alone could
        # not tell them apart, and the search was refused. The run's window is moved in a little.
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (220, 100, 1000, 700)}])
        d = self.mac(mac)
        own = d._pinned_window
        self.assertNotEqual(mac.find(own)["frame"], mac.find(19116)["frame"])
        d.observe()
        own_frame, focused = mac.find(own)["frame"], []
        d._address_bar, d._address_bar_rect = "e48", Rect(275, 18, 237, 31)
        d._background_input = lambda bundle: True
        d._wait_until_still = lambda bundle, what="": None
        d._settle = lambda ok, detail: ExecResult(ok, detail)
        d._post_click = lambda q, x, y: None
        d._paste_and_submit = lambda q, text, before_return=None: True
        users_frame = mac.find(19116)["frame"]
        with mock.patch.object(skylight, "focus_without_raise", lambda pid, wid: focused.append(wid) or "token"), \
                mock.patch.object(skylight, "give_focus_back", lambda token: None), \
                mock.patch.object(skylight, "BackgroundQuartz", lambda pid, wid, origin: ("Q", wid)), \
                mock.patch.object(axchrome, "focused_identifier",
                                  lambda pid, frame, wid=None: "another window" if frame == users_frame
                                  else "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD" if frame == own_frame else None):
            d.allow_browser_field(WEB_SEARCH)
            res = d._execute_projected(Action(kind=ActionKind.TYPE_TEXT, text="新加坡 天气",
                                              binding=pk.Binding(observation_id="o", app="Safari",
                                                                 element_id=WEB_SEARCH)))
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(focused, [int(own)])

    def test_the_ax_window_is_told_apart_by_its_id_at_one_frame(self):
        frame = (0, 0, 1000, 700)
        users, runs = object(), object()
        with mock.patch.object(axchrome, "_windows_at", lambda pid, fr: [users, runs]), \
                mock.patch.object(axchrome, "_window_id", lambda w: {id(users): 19116, id(runs): 30001}[id(w)]):
            self.assertIs(axchrome._the_window(7, frame, "30001"), runs)
            self.assertIsNone(axchrome._the_window(7, frame, "44444"))
            self.assertIsNone(axchrome._the_window(7, frame))                 # no id: two at a frame name none
        with mock.patch.object(axchrome, "_windows_at", lambda pid, fr: [users, runs]), \
                mock.patch.object(axchrome, "_window_id", lambda w: None):
            self.assertIsNone(axchrome._the_window(7, frame, "30001"))        # ids unreadable: still none

    def test_no_foreground_typing_into_a_browser(self):
        mac = FakeMac()
        d = self.mac(mac)
        d._run = mock.Mock(side_effect=AssertionError("nothing is sent"))
        self.assertFalse(d._type_foreground("新加坡 天气").ok)

    def test_no_foreground_typing_while_the_users_browser_window_has_the_focus(self):
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac, app="com.apple.TextEdit", stage=())
        d._front_window = lambda: (SAFARI, "19116")
        d._run = mock.Mock(side_effect=AssertionError("nothing is pasted"))
        res = d._type_foreground("新加坡 天气")
        self.assertFalse(res.ok)
        self.assertIn("not this run's", res.detail)

    def test_a_failed_menu_sends_no_escape_into_a_browser(self):
        mac = FakeMac()
        d = self.mac(mac)
        d.observe()
        sent = []
        d._run = lambda key, _extra=None, **kw: sent.append(key) or (False, {}, "menu not found")
        d._menu_options = lambda path: ""
        d.execute(Action(kind=ActionKind.MENU, text="文件 > 不存在"))
        self.assertNotIn("press_fg", sent)

    def test_the_cli_transport_brings_no_browser_window_forward(self):
        mac = FakeMac()
        d = self.mac(mac)
        d.observe()
        d.allow_foreground, sent = True, []
        base = d._run
        d._run = lambda key, _extra=None, **kw: sent.append(key) or base(key, _extra, **kw)
        self.assertFalse(d.execute(Action(kind=ActionKind.KEY, keys=("cmd", "l"))).ok)
        other = str(mac.new_window("另一个"))
        d._run_windows.add(other)
        self.assertFalse(d.execute(Action(kind=ActionKind.FOCUS_WINDOW, text=other)).ok)
        self.assertNotIn("press_fg_window", sent)
        self.assertNotIn("window_focus", sent)

    # -- Codex review: the window the script made -------------------------------------------------------------

    def test_a_window_the_user_opens_while_the_runs_is_made_is_not_taken(self):
        mac = FakeMac([{"id": 19085, "title": "收藏夹", "on_screen": False, "frame": (0, 0, 1200, 800)}])
        mac.user_opens_during_make = True
        d = self.mac(mac)
        users = str(mac.windows[0]["id"])                    # in front, and new too
        self.assertNotEqual(d._pinned_window, users)
        self.assertTrue(mac.find(d._pinned_window)["title"].startswith("dm-"))
        self.assertNotIn(users, [w.id for w in d.observe().windows])

    def test_an_id_that_is_not_a_new_window_pins_nothing(self):
        mac = FakeMac()
        make = mac.osa
        mac.osa = lambda script: (make(script)[0], "99999") if "make new" in script else make(script)
        d = self.mac(mac)
        self.assertIsNone(d._pinned_window)
        self.assertTrue(d.observe().no_window)
        self.assertEqual(self.reads(mac), [])

    def test_a_chromium_window_is_found_by_the_place_the_script_returns(self):
        mac = FakeMac(running=False)
        d = self.mac(mac, app="com.apple.TextEdit", stage=())
        chrome = "com.google.Chrome"
        rows = {"before": [{"window_id": 5, "window_title": "用户的", "is_on_screen": True,
                            "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800}}]}
        new = [{"window_id": 41, "window_title": "用户刚开的", "is_on_screen": True,
                "bounds": {"x": 40, "y": 40, "width": 1200, "height": 800}},
               {"window_id": 42, "window_title": "新标签页", "is_on_screen": True,
                "bounds": {"x": 220, "y": 100, "width": 1000, "height": 700}}]
        state = {"made": False}
        d._app_is_running = lambda bundle: True
        d._run = lambda key, _extra=None, **kw: (True, {"windows": rows["before"] + (new if state["made"] else [])}, "")
        d._osa = lambda script: (state.update(made=True) or (True, "1234|220,100,1220,800")) \
            if "make new window" in script else (True, "")
        with mock.patch.dict(sys.modules, {"Quartz": None}):
            self.assertEqual(d._own_browser_window(chrome), "42")
        state["made"] = False
        d._own_browser.clear()
        d._osa = lambda script: (state.update(made=True) or (True, "1234|900,900,1000,1000")) \
            if "make new window" in script else (True, "")
        with mock.patch.dict(sys.modules, {"Quartz": None}):
            self.assertIsNone(d._own_browser_window(chrome))     # no new window at that place: none

    # -- live 10-17: the window found by its marker title; the page after; the closed window's ghost -------------

    def test_the_staged_page_is_loaded_only_after_the_window_is_bound(self):
        mac = FakeMac([{"id": 19116, "title": "我的银行", "on_screen": True, "frame": (0, 0, 1200, 800)}])
        d = self.mac(mac, stage=('open -a Safari "https://example.com/page"',))
        scripts = [e[1] for e in mac.events if e[0] == "osascript"]
        make = next(i for i, x in enumerate(scripts) if "make new document" in x)
        load = next(i for i, x in enumerate(scripts) if "set URL of current tab" in x)
        self.assertLess(make, load)
        self.assertNotIn("example.com", scripts[make])
        self.assertIn(f"window id {d._pinned_window} ", scripts[load])
        self.assertEqual(mac.find(d._pinned_window)["url"], "https://example.com/page")
        self.assertEqual(mac.find(19116)["title"], "我的银行")

    def test_each_window_gets_its_own_marker(self):
        mac = FakeMac()
        d = self.mac(mac, app="com.apple.TextEdit", stage=())
        d._own_browser_window(SAFARI)
        d._own_browser.clear()
        d._own_browser_window(SAFARI)
        markers = [e[1].split("<title>")[1].split("<")[0] for e in mac.events
                   if e[0] == "osascript" and "make new document" in e[1]]
        self.assertEqual(len(set(markers)), 2)

    def test_the_ghost_a_closed_window_leaves_is_never_read_or_reused(self):
        # Closed through AppleScript, a Safari window stays listed off screen with no tabs and the same id (live 10-17).
        mac = FakeMac()
        d = self.mac(mac)
        own = d._pinned_window
        ghost = mac.find(own)
        ghost.update(on_screen=False, title="")
        d._pinned_window = d._window_id = None
        self.assertTrue(d.observe().no_window)
        self.assertNotIn(own, self.reads(mac))
        res = d.execute(Action(kind=ActionKind.FOCUS_APP, text="Safari"))      # back to Safari: a new window
        self.assertTrue(res.ok, res.detail)
        self.assertNotEqual(d._pinned_window, own)


class Parsing(unittest.TestCase):
    def test_browser_staging_commands(self):
        b = PeekabooDriver.BROWSERS
        self.assertEqual(pk._browser_stage("open -b com.apple.Safari", b), ("com.apple.Safari", ""))
        self.assertEqual(pk._browser_stage("open -g -a Safari \"$WS/p.html\"", b), ("com.apple.Safari", '"$WS/p.html"'))
        self.assertEqual(pk._browser_stage('open -a "Google Chrome"', b), ("com.google.Chrome", ""))
        self.assertIsNone(pk._browser_stage('open -a TextEdit "$WS/a.txt"', b))
        self.assertIsNone(pk._browser_stage("open -b com.apple.Safari; rm -rf x", b))
        self.assertIsNone(pk._browser_stage("open -b com.apple.Safari && echo", b))
        # An & inside a quoted URL is no operator.
        self.assertEqual(pk._browser_stage('open -a Safari "https://a.b/?x=1&y=2"', b),
                         ("com.apple.Safari", '"https://a.b/?x=1&y=2"'))

    def test_scripts(self):
        safari = pk._new_window_script("com.apple.Safari", "https://a.b/x", "dm-0123abcd")
        self.assertIn('make new document with properties {URL:"data:text/html,<title>dm-0123abcd</title>"}', safari)
        self.assertIn('first window whose name is "dm-0123abcd"', safari)
        self.assertNotIn("front window", safari)
        self.assertNotIn("whose document", safari)                 # -1700 on Safari, live 10-17
        self.assertNotIn("a.b", safari)                            # the page is loaded only once the window is bound
        with self.assertRaises(ValueError):
            pk._new_window_script("com.apple.Safari", "", 'dm-x" & do shell script "y')
        self.assertIn("bounds of w", pk._new_window_script("com.google.Chrome"))
        self.assertIn("make new window", pk._new_window_script("com.google.Chrome", "https://a.b"))
        self.assertIsNone(pk._new_window_script("org.mozilla.firefox"))
        for s in (pk._new_window_script("com.apple.Safari", "", "dm-0123abcd"), pk._new_window_script("com.google.Chrome")):
            self.assertNotIn("activate", s)
            self.assertNotIn("reopen", s)


if __name__ == "__main__":
    unittest.main()

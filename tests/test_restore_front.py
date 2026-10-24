"""Giving the front back restores it, never reopens (deskmind#118): every give-back goes through restore_front --
the running process activated without a reopen, else AXFrontmost; never `open -b`, never AppleScript `activate`,
never a launch. Both tests fail on 10d5b71."""
from __future__ import annotations

import ast
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PKG = Path(__file__).resolve().parent.parent / "deskmind_hands"


class EveryGiveBackGoesThroughRestoreFront(unittest.TestCase):
    """By scan: no AppleScript `activate` is sent anywhere; AXFrontmost by _make_frontmost only where the task's own
    app is raised (_bring_forward, _raise_app); the flash's give-back and the executor's launch give-back call
    _restore_front."""

    def tree(self, rel):
        return ast.parse((PKG / rel).read_text(encoding="utf-8"))

    @staticmethod
    def functions(tree):
        out = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, []).append(node)
        return out

    @staticmethod
    def calls(node, name):
        return [c for c in ast.walk(node) if isinstance(c, ast.Call)
                and getattr(c.func, "attr", getattr(c.func, "id", None)) == name]

    def test_no_applescript_activate_is_sent(self):
        found = []
        for path in PKG.rglob("*.py"):
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(call, ast.Call):
                    continue
                for arg in call.args:
                    text = ast.unparse(arg) if isinstance(arg, (ast.JoinedStr, ast.Constant)) else ""
                    if "to activate" in text:
                        found.append(f"{path.name}:{call.lineno}")
        self.assertEqual(found, [])

    def test_the_give_backs_call_restore_front(self):
        driver = self.functions(self.tree("drivers/peekaboo.py"))
        give = driver["_give_front_back"][0]
        self.assertTrue(self.calls(give, "_restore_front"))
        self.assertFalse(self.calls(give, "_make_frontmost"))
        users = sorted({name for name, nodes in driver.items() for n in nodes if self.calls(n, "_make_frontmost")})
        self.assertEqual(users, ["_bring_forward", "_raise_app"])      # raising the task's own app, not a give-back
        executor = self.tree("executor.py")
        self.assertFalse([c for c in ast.walk(executor) if isinstance(c, ast.Attribute) and c.attr == "_make_frontmost"])
        self.assertTrue(self.calls(executor, "_restore_front"))


class RestoreFrontTable(unittest.TestCase):
    """running -> activated, no reopen; activation that says yes and does nothing (cooperative activation) or is
    refused -> AXFrontmost; quit -> nothing, not launched; the bundle known and quit -> not another app of the same
    name; no bundle id -> the one ordinary app of that name, none when several match."""

    def run_row(self, apps, activates=True, takes=True, ax_ok=True, by_name=(), bundle="ai.deskmind.app"):
        log = []
        state = {"front": 1}                                     # another app in front

        class App:
            def __init__(self, pid, name="", policy=0):
                self.pid, self.name, self.policy = pid, name, policy

            def processIdentifier(self):
                return self.pid

            def localizedName(self):
                return self.name

            def activationPolicy(self):
                return self.policy

            def isTerminated(self):
                return False

            def activateWithOptions_(self, opts):
                log.append(("activate", self.pid))
                if activates and takes:
                    state["front"] = self.pid
                return activates
        appkit = types.ModuleType("AppKit")
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        appkit.NSRunningApplication = types.SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=lambda b: [App(p) for p in apps])
        appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
            runningApplications=lambda: [App(*x) for x in by_name]))
        ax = types.ModuleType("ApplicationServices")
        ax.AXUIElementCreateApplication = lambda pid: ("app", pid)

        def set_attr(el, attr, v):
            log.append((attr, el[1]))
            if ax_ok:
                state["front"] = el[1]
            return 0 if ax_ok else -25200
        ax.AXUIElementSetAttributeValue = set_attr
        from deskmind_hands.drivers import front
        with mock.patch.dict(sys.modules, {"AppKit": appkit, "ApplicationServices": ax}), \
                mock.patch.object(front, "_front_pid", lambda: state["front"], create=True), \
                mock.patch("subprocess.run", side_effect=AssertionError("nothing is launched or opened")):
            got = front.restore_front(bundle, "DeskMind")
        return got, log

    def test_running_is_activated_without_a_reopen(self):
        self.assertEqual(self.run_row([501]), ("given back", [("activate", 501)]))

    def test_an_activation_that_says_yes_and_does_nothing_falls_back_to_axfrontmost(self):
        self.assertEqual(self.run_row([501], takes=False), ("given back", [("activate", 501), ("AXFrontmost", 501)]))

    def test_activation_refused_falls_back_to_axfrontmost(self):
        self.assertEqual(self.run_row([501], activates=False),
                         ("given back", [("activate", 501), ("AXFrontmost", 501)]))

    def test_both_refused_is_said(self):
        self.assertEqual(self.run_row([501], activates=False, ax_ok=False)[0], "not given back: the activation was refused")

    def test_quit_is_not_launched_again(self):
        self.assertEqual(self.run_row([]), ("not given back: the app is no longer running", []))

    def test_the_bundle_known_and_quit_brings_no_app_of_the_same_name(self):
        self.assertEqual(self.run_row([], by_name=[(777, "DeskMind")]), ("not given back: the app is no longer running", []))

    def test_no_bundle_id_the_one_ordinary_app_of_that_name(self):
        from deskmind_hands.drivers import front
        rows = [(777, "DeskMind"), (888, "Other"), (999, "DeskMind", 1)]       # 999: an accessory, not ordinary
        self.assertEqual(self.run_row([], by_name=rows, bundle=""), ("given back", [("activate", 777)]))

    def test_no_bundle_id_and_several_of_that_name_brings_none(self):
        from deskmind_hands.drivers import front
        rows = [(777, "DeskMind"), (778, "DeskMind")]
        self.assertEqual(self.run_row([], by_name=rows, bundle=""), ("not given back: several running apps have that name", []))


class TheFrontIsReadFresh(unittest.TestCase):
    """Codex on #69 @2baf2f8: with SkyLight unavailable the front was NSWorkspace's frontmostApplication -- stale in a
    process with no AppKit run loop (the executor). A refused activation read as given back (the stale front was the
    target), and the gate's front check could pass or refuse on the wrong app. One read now (front.fresh_front_pid:
    SkyLight's front process only -- @3d0d781: never inferred from window order either), for the give-back and the
    gates; unknown without it."""

    def modules(self, window_owner, stale_front):
        quartz = types.ModuleType("Quartz")
        quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGWindowListExcludeDesktopElements = 1, 16
        quartz.CGWindowListCopyWindowInfo = lambda opts, wid: [
            {"kCGWindowLayer": 25, "kCGWindowOwnerPID": 55},                 # a status item above: not layer 0
            {"kCGWindowLayer": 0, "kCGWindowOwnerPID": window_owner}]
        stale = types.SimpleNamespace(processIdentifier=lambda: stale_front)
        workspace = types.SimpleNamespace(frontmostApplication=lambda: stale, runningApplications=lambda: [])
        return quartz, workspace

    def test_a_refused_activation_with_a_stale_front_still_tries_axfrontmost(self):
        from deskmind_hands.drivers import front, skylight
        log = []
        quartz, workspace = self.modules(window_owner=1, stale_front=501)     # stale: says the target is in front

        class App:
            def processIdentifier(self):
                return 501

            def isTerminated(self):
                return False

            def activateWithOptions_(self, opts):
                log.append("activate")
                return False                                                  # refused
        appkit = types.ModuleType("AppKit")
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        appkit.NSRunningApplication = types.SimpleNamespace(runningApplicationsWithBundleIdentifier_=lambda b: [App()])
        appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: workspace)
        ax = types.ModuleType("ApplicationServices")
        ax.AXUIElementCreateApplication = lambda pid: pid
        ax.AXUIElementSetAttributeValue = lambda el, attr, v: log.append(attr) or 0
        with mock.patch.dict(sys.modules, {"AppKit": appkit, "ApplicationServices": ax, "Quartz": quartz}), \
                mock.patch.object(skylight, "front_pid", lambda: None):
            front.restore_front("com.example.app")
        self.assertEqual(log, ["activate", "AXFrontmost"])

    # Codex on #69 @3d0d781: the window order is not the active app. Each with SkyLight unavailable, the target's
    # window first in the list while the user's app is the active one.
    SCENARIOS = {
        "the user's app has no window": [{"kCGWindowLayer": 0, "kCGWindowOwnerPID": 4242}],
        "the user's app has only a panel": [{"kCGWindowLayer": 3, "kCGWindowOwnerPID": 1111},
                                            {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 4242}],
        "another app's window ordered front without activation": [
            {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 4242}, {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 1111}],
    }

    def quartz_listing(self, wins):
        quartz = types.ModuleType("Quartz")
        quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGWindowListExcludeDesktopElements = 1, 16
        quartz.CGWindowListCopyWindowInfo = lambda opts, wid: wins
        return quartz

    def test_codex_scenarios_the_gate_refuses(self):
        from deskmind_hands.drivers import skylight
        from deskmind_hands.drivers.peekaboo import Gate
        from deskmind_hands.grounding import STOP
        STOP.clear()
        for name, wins in self.SCENARIOS.items():
            with self.subTest(scenario=name):
                appkit = types.ModuleType("AppKit")
                stale = types.SimpleNamespace(processIdentifier=lambda: 4242)
                appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
                    frontmostApplication=lambda: stale))
                with mock.patch.dict(sys.modules, {"Quartz": self.quartz_listing(wins), "AppKit": appkit}), \
                        mock.patch.object(skylight, "front_pid", lambda: None):
                    why = Gate.foreground(4242, "FIELD").before_focus_write()
                self.assertTrue(why, "the gate let the paste go to the user's app")

    def test_codex_scenarios_restore_front_does_not_claim_confirmed(self):
        from deskmind_hands.drivers import front, skylight
        for name, wins in self.SCENARIOS.items():
            with self.subTest(scenario=name):
                class App:
                    def processIdentifier(self):
                        return 4242

                    def isTerminated(self):
                        return False

                    def activateWithOptions_(self, opts):
                        return True                                   # says yes; does nothing
                appkit = types.ModuleType("AppKit")
                appkit.NSApplicationActivateIgnoringOtherApps = 2
                appkit.NSRunningApplication = types.SimpleNamespace(
                    runningApplicationsWithBundleIdentifier_=lambda b: [App()])
                appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
                    frontmostApplication=lambda: App(), runningApplications=lambda: []))
                ax = types.ModuleType("ApplicationServices")
                ax.AXUIElementCreateApplication = lambda pid: pid
                ax.AXUIElementSetAttributeValue = lambda el, attr, v: 0
                with mock.patch.dict(sys.modules, {"AppKit": appkit, "ApplicationServices": ax,
                                                   "Quartz": self.quartz_listing(wins)}), \
                        mock.patch.object(skylight, "front_pid", lambda: None):
                    got = front.restore_front("com.example.app")
                self.assertNotEqual(got, "given back")
                self.assertEqual(got, "given back (not confirmed)")

    def test_no_front_check_reads_nsworkspace_frontmost_application(self):
        found = []
        for path in PKG.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr == "frontmostApplication":
                    found.append(f"{path.name}:{node.lineno}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()

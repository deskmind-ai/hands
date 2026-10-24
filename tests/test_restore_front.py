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
    """running -> activated, no reopen; activation refused -> AXFrontmost; quit -> nothing, not launched; known only
    by its name -> the running app of that name."""

    def run_row(self, apps, activates=True, by_name=()):
        log = []

        class App:
            def __init__(self, pid, name=""):
                self.pid, self.name = pid, name

            def processIdentifier(self):
                return self.pid

            def localizedName(self):
                return self.name

            def isTerminated(self):
                return False

            def activateWithOptions_(self, opts):
                log.append(("activate", self.pid))
                return activates
        appkit = types.ModuleType("AppKit")
        appkit.NSApplicationActivateIgnoringOtherApps = 2
        appkit.NSRunningApplication = types.SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=lambda b: [App(p) for p in apps])
        appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
            runningApplications=lambda: [App(p, n) for p, n in by_name]))
        ax = types.ModuleType("ApplicationServices")
        ax.AXUIElementCreateApplication = lambda pid: ("app", pid)
        ax.AXUIElementSetAttributeValue = lambda el, attr, v: log.append((attr, el[1])) or 0
        from deskmind_hands.drivers import front
        with mock.patch.dict(sys.modules, {"AppKit": appkit, "ApplicationServices": ax}), \
                mock.patch("subprocess.run", side_effect=AssertionError("nothing is launched or opened")):
            ok = front.restore_front("ai.deskmind.app", "DeskMind")
        return ok, log

    def test_running_is_activated_without_a_reopen(self):
        self.assertEqual(self.run_row([501]), (True, [("activate", 501)]))

    def test_activation_refused_falls_back_to_axfrontmost(self):
        self.assertEqual(self.run_row([501], activates=False), (True, [("activate", 501), ("AXFrontmost", 501)]))

    def test_quit_is_not_launched_again(self):
        self.assertEqual(self.run_row([]), (False, []))

    def test_known_only_by_name_is_the_running_app_of_that_name(self):
        self.assertEqual(self.run_row([], by_name=[(777, "DeskMind"), (888, "Other")]), (True, [("activate", 777)]))


if __name__ == "__main__":
    unittest.main()

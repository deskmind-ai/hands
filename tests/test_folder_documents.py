"""The attached folder's documents offered to open, and a saved TextEdit document closed (D1, 09-30: asked to open
parts.csv from the attached folder, a planner that was told nothing about the folder searched the web for it)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.actions import Action, ActionKind, Binding   # noqa: E402
from deskmind_hands.adapters.systemone import OPENABLE_ROLES   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver   # noqa: E402


def driver(apps=("TextEdit",), app="com.apple.Safari"):
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.ws = Path(tempfile.mkdtemp())
    for name in ("parts.csv", "notes.txt", "receipt.png", "budget.numbers", ".DS_Store"):
        (d.ws / name).write_text("x")
    (d.ws / "sub").mkdir()
    d.app, d._stage_apps, d._window_title, d._window_id = app, list(apps), "", "368"
    d._app_name, d._pinned_window, d._target_pid, d._snapshot = "Safari", "368", None, None
    d._scripted_actions, d._projected_actions = 0, 0
    d._settle = lambda ok, detail: (ok, detail)
    d.offer_folder_files = True
    return d


def res(r):
    """(ok, detail) from a result, whether the driver settled it (a pair here) or refused it (an ExecResult)."""
    return r if isinstance(r, tuple) else (r.ok, r.detail)


def click(eid):
    return Action(kind=ActionKind.DOUBLE_CLICK, binding=Binding(element_id=eid))


class Offered(unittest.TestCase):
    def test_only_documents_the_runs_apps_open(self):
        d = driver()
        self.assertEqual([e.label for e in d._folder_documents()], ["notes.txt", "parts.csv"])
        self.assertTrue(all(e.role == "document" and e.id.startswith("syn:openfile:") for e in d._folder_documents()))
        self.assertIn("document", OPENABLE_ROLES)   # the planner may OPEN them
        d._stage_apps = ["TextEdit", "Preview"]
        self.assertEqual([e.label for e in d._folder_documents()], ["notes.txt", "parts.csv", "receipt.png"])
        d._stage_apps = []                          # Safari alone opens none of them: nothing offered
        self.assertEqual(d._folder_documents(), [])

    def test_not_the_one_already_observed(self):
        d = driver()
        d._window_title = "parts.csv"
        self.assertEqual([e.label for e in d._folder_documents()], ["notes.txt"])


class Opening(unittest.TestCase):
    def test_opens_in_the_runs_app_and_observes_it(self):
        import deskmind_hands.drivers.peekaboo as pk
        d = driver()
        windows = [{"1"}, {"1", "7"}]
        d._window_ids = lambda: windows.pop(0) if len(windows) > 1 else windows[0]
        d._window_titled_once = lambda title: None
        ran = []
        real_run, real_sleep = pk.subprocess.run, pk.time.sleep
        pk.subprocess.run, pk.time.sleep = (lambda args, **kw: ran.append(args)), (lambda s: None)
        try:
            ok, detail = res(d._execute_projected(click("syn:openfile:parts.csv")))
        finally:
            pk.subprocess.run, pk.time.sleep = real_run, real_sleep
        self.assertTrue(ok, detail)
        self.assertEqual(ran, [["/usr/bin/open", "-g", "-b", "com.apple.TextEdit", str(d.ws / "parts.csv")]])
        self.assertEqual((d.app, d._pinned_window, d._window_id), ("com.apple.TextEdit", "7", "7"))
        self.assertIn("opened 'parts.csv' in TextEdit", detail)

    def test_already_open_is_switched_to_not_opened_again(self):
        d = driver()
        d._window_titled_once = lambda title: "9" if title == "parts.csv" else None
        d._window_ids = lambda: self.fail("no new window is looked for")
        ok, detail = res(d._execute_projected(click("syn:openfile:parts.csv")))
        self.assertTrue(ok)
        self.assertEqual(d._pinned_window, "9")
        self.assertIn("already open", detail)

    def test_nothing_outside_the_folder_or_the_runs_apps(self):
        d = driver()
        for name in ("budget.numbers", "../etc/hosts", "missing.csv", "sub"):
            ok, _ = res(d._execute_projected(click(f"syn:openfile:{name}")))
            self.assertFalse(ok, name)


class Closing(unittest.TestCase):
    def closer(self, modified):
        d = driver(app="com.apple.TextEdit")
        d._window_title, d.scripts = "parts.csv", []
        d._osa = lambda script: d.scripts.append(script) or (True, "true" if modified and "modified" in script
                                                               else "false")
        return d

    def test_a_saved_document_is_closed(self):
        d = self.closer(modified=False)
        ok, detail = res(d._execute_projected(click("syn:close")))
        self.assertTrue(ok, detail)
        self.assertTrue(any("close (first document whose name is \"parts.csv\")" in s for s in d.scripts))
        self.assertIsNone(d._pinned_window)

    def test_unsaved_work_is_never_thrown_away(self):
        d = self.closer(modified=True)
        ok, detail = res(d._execute_projected(click("syn:close")))
        self.assertFalse(ok)
        self.assertIn("保存 first", detail)
        self.assertFalse(any(" close " in s for s in d.scripts))



class TransientReads(unittest.TestCase):
    def test_a_window_changing_during_capture_is_read_again(self):
        from deskmind_hands.drivers.peekaboo import _transient_read
        self.assertTrue(_transient_read("Failed to capture UI state: Desktop observation target changed during "
                                        "capture: the resolved exact window no longer matched its process-generation lane"))
        self.assertTrue(_transient_read("AX tree incomplete (accessibility_enumeration_incomplete)"))
        self.assertFalse(_transient_read("WINDOW_NOT_FOUND"))
        self.assertFalse(_transient_read("permission denied"))



class Ghosts(unittest.TestCase):
    def test_untitled_windows_with_no_accessibility_window_are_not_offered(self):
        g = PeekabooDriver._ghost
        blind = {"observation_capability": "pixels_only",
                 "observation_capability_reason": "no_matching_accessibility_window"}
        self.assertTrue(g({"window_title": "", "bounds": {"width": 500, "height": 500}, **blind}))
        self.assertTrue(g({"window_title": "", "bounds": {"width": 64, "height": 64}}))
        self.assertFalse(g({"window_title": "Parts inventory", "bounds": {"width": 500, "height": 500}, **blind}))
        self.assertFalse(g({"window_title": "", "bounds": {"width": 864, "height": 1040},
                            "observation_capability": "combined_eligible"}))   # a real one whose title read empty



class AfterClosing(unittest.TestCase):
    """10-01: G18b closed parts.csv as told, then tried to open it again, and "open" was refused for want of a
    snapshot (there is none with no window open) until the run ended as a loop."""

    def test_a_closed_document_is_not_offered_again(self):
        d = driver()
        d._closed_documents = {"parts.csv"}
        self.assertEqual([e.label for e in d._folder_documents()], ["notes.txt"])

    def test_a_synthetic_control_needs_no_snapshot(self):
        d = driver()
        d._snapshot, d._mcp = None, object()
        d._window_titled_once = lambda title: "9"
        ok, detail = res(d._execute_inner(click("syn:openfile:notes.txt")))
        self.assertTrue(ok, detail)
        self.assertNotIn("observe before acting", detail)


if __name__ == "__main__":
    unittest.main()

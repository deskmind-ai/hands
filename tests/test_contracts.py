"""L0 contract tests: pure python, no desktop, no model, milliseconds.

These cover the parts that break silently. A coordinate mapping that is off by
the backing scale still produces plausible clicks; a grader that accepts an
empty workspace still produces plausible scores. Both would be invisible in an
end-to-end number and obvious here.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.actions import ActionError, ActionKind, from_json, normalise_keys
from deskmind_hands.drivers.mock import MockDriver
from deskmind_hands.env.workspace import SandboxError, Workspace, assert_sandboxed
from deskmind_bench.failures import no_progress
from deskmind_hands.geometry import (CoordinateError, CoordSpace, ImageTransform, Point,
                             ScreenGeometry, Size, clamp_to_screen, to_logical)
from deskmind_bench.graders.primitives import GradeContext, evaluate
from deskmind_bench.graders.score import grade
from deskmind_hands.report.aggregate import aggregate, cluster_bootstrap_ci
from deskmind_bench.task import TaskError, load_set, load_task

REPO = Path(__file__).resolve().parent.parent
GEO = ScreenGeometry(Size(1440, 900), Size(2880, 1800))
TX = ImageTransform.fit(Size(2880, 1800), 1456)


class Geometry(unittest.TestCase):
    def test_backing_scale_is_applied(self):
        # A point at the centre of the screenshot is the centre in points.
        p = to_logical(Point(1440, 900), CoordSpace.SCREEN_PIXELS, GEO)
        self.assertAlmostEqual(p.x, 720.0)
        self.assertAlmostEqual(p.y, 450.0)

    def test_every_space_agrees_on_the_same_physical_point(self):
        want = Point(720.0, 450.0)
        cases = [
            (Point(TX.target.w / 2, TX.target.h / 2), CoordSpace.MODEL_IMAGE),
            (Point(500, 500), CoordSpace.NORM_1000),
            (Point(0.5, 0.5), CoordSpace.NORM_UNIT),
            (Point(1440, 900), CoordSpace.SCREEN_PIXELS),
            (Point(720, 450), CoordSpace.LOGICAL_POINTS),
        ]
        for pt, space in cases:
            with self.subTest(space=space):
                got = to_logical(pt, space, GEO, TX)
                self.assertAlmostEqual(got.x, want.x, places=0)
                self.assertAlmostEqual(got.y, want.y, places=0)

    def test_letterbox_offset_is_removed(self):
        tx = ImageTransform(source=Size(2880, 1800), target=Size(1000, 1000), pad_x=0, pad_y=100)
        got = to_logical(Point(0, 100), CoordSpace.MODEL_IMAGE, GEO, tx)
        self.assertAlmostEqual(got.x, 0.0, places=3)
        self.assertAlmostEqual(got.y, 0.0, places=3)

    def test_far_off_screen_point_is_rejected_not_clamped(self):
        # A model using the wrong coordinate convention must fail loudly rather
        # than click the corner on every step.
        with self.assertRaises(CoordinateError):
            clamp_to_screen(Point(2880, 1800), GEO)

    def test_edge_tolerance_scales_with_the_frame(self):
        """Aiming slightly wide must clamp; using the wrong units must not.

        A two-pixel tolerance killed a real run: the model aimed at the bottom
        of a 352-point window and answered 364.8, which is plainly the intended
        target, and the harness raised instead of clicking. The tolerance has to
        be large enough for that and small enough to still catch a doubled
        coordinate or a 0-1000 value fed in as pixels.
        """
        small = ScreenGeometry(Size(526, 352), Size(526, 352))
        self.assertEqual(clamp_to_screen(Point(526, 364.8), small).rounded(), (525, 351))
        with self.assertRaises(CoordinateError):
            clamp_to_screen(Point(1052, 704), small)      # doubled
        with self.assertRaises(CoordinateError):
            clamp_to_screen(Point(500, 900), small)       # 0-1000 used as pixels

    def test_edge_rounding_is_tolerated(self):
        self.assertEqual(clamp_to_screen(Point(1440.5, 900.5), GEO).rounded(), (1439, 899))


class Actions(unittest.TestCase):
    def test_key_chord_canonicalisation(self):
        self.assertEqual(normalise_keys("Command+Shift+S"), ("cmd", "shift", "s"))
        self.assertEqual(normalise_keys(["Control", "c"]), ("ctrl", "c"))

    def test_two_non_modifier_keys_rejected(self):
        with self.assertRaises(ActionError):
            normalise_keys("a+b")

    def test_click_without_target_rejected(self):
        with self.assertRaises(ActionError):
            from_json({"kind": "click"})

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ActionError):
            from_json({"kind": "teleport"})

    def test_type_text_accepts_empty_string_but_not_missing(self):
        self.assertEqual(from_json({"kind": "type_text", "text": ""}).text, "")
        with self.assertRaises(ActionError):
            from_json({"kind": "type_text"})

    def test_roundtrip_json(self):
        a = from_json({"kind": "click", "point": [10, 20], "coord_space": "logical_points",
                       "binding": {"element_id": "btn:save"}})
        self.assertEqual(a.to_json()["binding"]["element_id"], "btn:save")
        self.assertIs(a.kind, ActionKind.CLICK)


class Graders(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "a.txt").write_text("你好，世界。\n", encoding="utf-8")
        self.ctx = GradeContext(workspace=self.d, clipboard="SENT",
                                driver_state={"writes_count": 2},
                                run={"state": "cancelled", "metrics": {"stale_refusals": 1}})

    def test_text_equality_is_unicode_normalised(self):
        import unicodedata
        (self.d / "n.txt").write_text(unicodedata.normalize("NFD", "é"), encoding="utf-8")
        r = evaluate({"file_text_equals": {"path": "$WS/n.txt", "value": "é", "normalise": "nfc"}}, self.ctx)
        self.assertTrue(r.ok, r.detail)

    def test_halfwidth_punctuation_is_a_failure_and_is_located(self):
        r = evaluate({"file_text_equals": {"path": "$WS/a.txt", "value": "你好,世界。\n"}}, self.ctx)
        self.assertFalse(r.ok)
        self.assertIn("char 2", r.detail)

    def test_missing_file_fails_rather_than_raising(self):
        self.assertFalse(evaluate({"file_sha256": {"path": "$WS/nope", "value": "x"}}, self.ctx).ok)

    def test_combinators(self):
        ex = {"file_exists": {"path": "$WS/a.txt"}}
        no = {"file_exists": {"path": "$WS/zz"}}
        self.assertTrue(evaluate({"all_of": [ex, ex]}, self.ctx).ok)
        self.assertFalse(evaluate({"all_of": [ex, no]}, self.ctx).ok)
        self.assertTrue(evaluate({"any_of": [no, ex]}, self.ctx).ok)
        self.assertTrue(evaluate({"not": no}, self.ctx).ok)

    def test_run_field_supports_comparison(self):
        self.assertTrue(evaluate({"run_field": {"key": "state", "value": "cancelled"}}, self.ctx).ok)
        self.assertTrue(evaluate({"run_field": {"key": "metrics.stale_refusals",
                                                "op": "gte", "value": 1}}, self.ctx).ok)
        self.assertFalse(evaluate({"run_field": {"key": "nope.nope", "value": 1}}, self.ctx).ok)

    def test_unknown_check_is_an_error_not_a_silent_pass(self):
        with self.assertRaises(ValueError):
            evaluate({"file_smells_right": {}}, self.ctx)

    def test_dir_manifest_reports_extra_and_missing(self):
        r = evaluate({"dir_manifest": {"dir": "$WS", "value": ["b.txt"]}}, self.ctx)
        self.assertFalse(r.ok)
        self.assertIn("a.txt", r.detail)


class Scoring(unittest.TestCase):
    def setUp(self):
        self.tasks = {t.id: t for t in load_set(REPO / "tasks", "smoke")}
        self.d = Path(tempfile.mkdtemp())

    def _ctx(self, **kw):
        return GradeContext(workspace=self.d, **kw)

    def test_guard_breaks_strict_but_leaves_partial_alone(self):
        t = self.tasks["S04-clipboard-protect"]
        (self.d / "doc.txt").write_text("验收通过\n", encoding="utf-8")
        g = grade(t, self._ctx(clipboard="CLOBBERED"))
        self.assertFalse(g.strict)
        self.assertEqual(g.partial, 1.0)
        self.assertTrue(any("guard" in v for v in g.violations))

    def test_critical_checkpoint_failure_zeroes_partial(self):
        t = self.tasks["S01-rename"]
        (self.d / "final.txt").write_text("WRONG CONTENT\n", encoding="utf-8")
        g = grade(t, self._ctx())
        self.assertEqual(g.partial, 0.0)
        self.assertFalse(g.strict)

    def test_grader_exception_reports_error_not_zero(self):
        t = self.tasks["S01-rename"]
        t2 = load_task(t.source_path)
        t2.checkpoints[0].check = {"nonexistent_predicate": {}}
        g = grade(t2, self._ctx())
        self.assertIsNotNone(g.error)


class Sandbox(unittest.TestCase):
    def test_refuses_paths_outside_root(self):
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(SandboxError):
            assert_sandboxed(Path.home(), root)
        with self.assertRaises(SandboxError):
            assert_sandboxed(root, root)
        assert_sandboxed(root / "ws", root)

    def test_reset_restores_fixture_and_removes_agent_debris(self):
        root = Path(tempfile.mkdtemp())
        ws = Workspace.create(root, REPO / "fixtures")
        ws.reset("basic_files")
        (ws.ws / "junk.txt").write_text("debris")
        (ws.ws / "draft.txt").unlink()
        elapsed = ws.reset("basic_files")
        self.assertIn("draft.txt", ws.tree())
        self.assertNotIn("junk.txt", ws.tree())
        self.assertLess(elapsed, 1.0, "reset must stay fast enough to run every change")

    def test_sentinel_digest_is_taken_after_reset(self):
        root = Path(tempfile.mkdtemp())
        ws = Workspace.create(root, REPO / "fixtures")
        ws.reset("basic_files")
        d = ws.sentinel_digests(["$WS/keep/reference.txt"])
        self.assertEqual(len(d), 1)
        with self.assertRaises(SandboxError):
            ws.sentinel_digests(["$WS/not-there.txt"])


class MultiWindow(unittest.TestCase):
    """Two documents in one application, which is where defect #12 lived.

    The driver pins one window so that a second document cannot quietly become
    the target. That pin is right, and it was also a dead end: the only way out
    was to leave for another app and come back, which lands on whichever window
    the platform picks. G11 spent 19 actions and 597 seconds discovering that.
    These are the semantics the way out has to have, and both drivers are held
    to them.
    """

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "one.txt").write_text("first\n", encoding="utf-8")
        (self.ws / "two.txt").write_text("second\n", encoding="utf-8")
        self.d = MockDriver(render=False)
        self.d.start(self.ws)
        for name in ("one.txt", "two.txt"):
            self.d.execute(from_json({"kind": "double_click",
                                      "binding": {"element_id": f"row:{name}"}}))

    def _do(self, spec):
        return self.d.execute(from_json(spec))

    def test_every_window_is_listed_and_exactly_one_is_active(self):
        obs = self.d.observe()
        self.assertEqual(len(obs.windows), 2, "both documents must be addressable")
        self.assertEqual(sum(w.active for w in obs.windows), 1)
        self.assertEqual({w.title.split(" ")[0] for w in obs.windows}, {"one.txt", "two.txt"})

    def test_focus_window_changes_which_document_is_acted_on(self):
        before = self.d.observe()
        other = next(w for w in before.windows if not w.active)
        self.assertTrue(self._do({"kind": "focus_window", "text": other.id}).ok)
        after = self.d.observe()
        self.assertTrue(next(w for w in after.windows if w.id == other.id).active)
        # And the contents follow the focus, which is the whole point: reading
        # the second document must not require leaving the application.
        body = next(e for e in after.elements if e.id == "editor:body")
        self.assertEqual(body.value, "first\n")

    def test_an_unknown_window_is_refused_and_the_refusal_names_the_real_ones(self):
        r = self._do({"kind": "focus_window", "text": "no-such-window"})
        self.assertFalse(r.ok)
        self.assertFalse(r.unsupported, "the driver can do this; the id was wrong")
        for w in self.d.observe().windows:
            self.assertIn(w.id, r.detail)

    def test_switching_windows_is_visible_to_no_progress_detection(self):
        # Without this the loop reads a successful focus_window as a repeat of
        # the previous turn and starts unwinding a run that is making progress.
        before = self.d.observe().digest()
        other = next(w for w in self.d.observe().windows if not w.active)
        self._do({"kind": "focus_window", "text": other.id})
        self.assertNotEqual(before, self.d.observe().digest())

    def test_the_mock_refuses_the_menu_instead_of_pretending(self):
        r = self._do({"kind": "menu", "text": "File > Save"})
        self.assertFalse(r.ok)
        self.assertTrue(r.unsupported,
                        "an unmodelled action must report unsupported, not failure")


class LiveRun(unittest.TestCase):
    """The guards on running against somebody's real files.

    Every other task in this project runs on a fixture that is restored before
    it starts, so a mistake costs nothing. `hands do` has no restore, which
    makes refusal the only safety mechanism there is -- and makes the change
    report the only account of what happened.
    """

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "a.txt").write_text("one", encoding="utf-8")
        (self.d / "sub").mkdir()
        (self.d / "sub" / "b.txt").write_text("two", encoding="utf-8")

    def test_a_home_directory_or_system_root_is_refused(self):
        from deskmind_hands.live import TargetRefused, guard_target
        for bad in (str(Path.home()), "/", "/System", "/Applications"):
            with self.assertRaises(TargetRefused, msg=f"{bad} was accepted"):
                guard_target(bad)

    def test_a_missing_path_or_a_file_is_refused(self):
        from deskmind_hands.live import TargetRefused, guard_target
        with self.assertRaises(TargetRefused):
            guard_target(str(self.d / "nope"))
        with self.assertRaises(TargetRefused):
            guard_target(str(self.d / "a.txt"))
        self.assertEqual(guard_target(str(self.d)), self.d.resolve())

    def test_an_edit_that_preserves_length_still_reads_as_modified(self):
        # A size-and-mtime check would miss this, and it is exactly what a
        # careless find-and-replace looks like.
        from deskmind_hands.live import diff, snapshot
        before = snapshot(self.d)
        (self.d / "a.txt").write_text("ONE", encoding="utf-8")
        c = diff(before, snapshot(self.d))
        self.assertEqual(c.modified, ["a.txt"])
        self.assertFalse(c.created or c.deleted)

    def test_creation_and_deletion_are_reported_with_relative_paths(self):
        from deskmind_hands.live import diff, snapshot
        before = snapshot(self.d)
        (self.d / "c.txt").write_text("three", encoding="utf-8")
        (self.d / "sub" / "b.txt").unlink()
        c = diff(before, snapshot(self.d))
        self.assertEqual(c.created, ["c.txt"])
        self.assertEqual(c.deleted, [str(Path("sub") / "b.txt")])

    def test_os_artefacts_are_not_reported_as_the_agent_s_doing(self):
        from deskmind_hands.live import diff, snapshot
        before = snapshot(self.d)
        (self.d / ".DS_Store").write_bytes(b"finder wrote this")
        self.assertFalse(diff(before, snapshot(self.d)).any)

    def test_a_live_task_carries_no_fixture_and_no_checkpoints(self):
        # Any one of these would be destructive or meaningless against real
        # files: a fixture overwrites them, a checkpoint needs an answer nobody
        # has, an oracle needs the task to be solvable in advance.
        from deskmind_hands.live import live_task
        t = live_task("tidy up", self.d, app="com.apple.finder",
                      max_actions=10, wall_clock_s=60)
        self.assertIsNone(t.fixture)
        self.assertEqual(t.checkpoints, [])
        self.assertEqual(t.oracle_effect, [])
        self.assertEqual(t.stage, [])
        self.assertEqual(t.reset_apps, [])


    def test_a_live_workspace_cannot_delete_the_directory_it_points_at(self):
        """The near miss this exists to prevent.

        run_task calls workspace.reset() on every run, and Workspace.reset
        begins with shutil.rmtree. The first live run handed a real directory
        to that call; only assert_sandboxed refusing a path outside the run
        root saved the files, and that guard was written for something else.
        """
        from deskmind_hands.env.workspace import LiveWorkspace, SandboxError
        ws = LiveWorkspace(root=self.d.parent, ws=self.d, fixtures_dir=self.d.parent)
        self.assertEqual(ws.reset(None), 0.0)
        self.assertTrue((self.d / "a.txt").exists(), "reset deleted a live file")
        self.assertTrue((self.d / "sub" / "b.txt").exists())
        with self.assertRaises(SandboxError):
            ws.reset("invoices_zh")
        self.assertTrue((self.d / "a.txt").exists())

    def test_the_plain_workspace_still_resets_so_evals_are_unaffected(self):
        from deskmind_hands.env.workspace import Workspace
        root = Path(tempfile.mkdtemp())
        ws = Workspace.create(root, REPO / "fixtures")
        ws.ws.mkdir(parents=True, exist_ok=True)
        (ws.ws / "stale.txt").write_text("from the previous run", encoding="utf-8")
        ws.reset(None)
        self.assertFalse((ws.ws / "stale.txt").exists(),
                         "an eval workspace must start clean")


    def test_the_snapshot_guard_does_not_block_the_way_out(self):
        """An app with no window must still be leavable.

        The guard refusing every action without a snapshot is right for
        anything aimed inside a window and wrong for the moves that change
        which window is being looked at. With TextEdit's last document closed,
        a live run had focus_app, wait and screenshot all refused with "no
        snapshot yet" and looped until no-progress detection stopped it.
        """
        from deskmind_hands.actions import ActionKind
        from deskmind_hands.drivers.peekaboo import PeekabooDriver
        needs = PeekabooDriver.NEEDS_SNAPSHOT
        for escape in (ActionKind.FOCUS_APP, ActionKind.FOCUS_WINDOW,
                       ActionKind.WAIT, ActionKind.SCREENSHOT,
                       ActionKind.DONE, ActionKind.GIVE_UP):
            self.assertNotIn(escape, needs, f"{escape.value} must survive a lost window")
        for inside in (ActionKind.CLICK, ActionKind.TYPE_TEXT, ActionKind.KEY,
                       ActionKind.SCROLL, ActionKind.MENU):
            self.assertIn(inside, needs, f"{inside.value} acts inside a window")


class MockDesktop(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "a.txt").write_text("x\n")
        self.d = MockDriver(render=False)
        self.d.start(self.ws)

    def _do(self, spec):
        return self.d.execute(from_json(spec))

    def test_modal_blocks_unrelated_interaction(self):
        self.d.inject("modal", {"text": "Confirm?"})
        r = self._do({"kind": "click", "binding": {"element_id": "row:a.txt"}})
        self.assertFalse(r.ok)
        self.assertIn("modal", r.detail)
        self.assertTrue(self._do({"kind": "click", "binding": {"element_id": "btn:modal_ok"}}).ok)
        self.assertTrue(self._do({"kind": "click", "binding": {"element_id": "row:a.txt"}}).ok)

    def test_action_bound_to_a_moved_window_is_refused(self):
        obs = self.d.observe()
        self.d.inject("move_window", {"dx": 100, "dy": 50})
        r = self._do({"kind": "click", "binding": {"element_id": "row:a.txt",
                                                    "observation_id": obs.id}})
        self.assertTrue(r.stale)
        fresh = self.d.observe()
        r2 = self._do({"kind": "click", "binding": {"element_id": "row:a.txt",
                                                     "observation_id": fresh.id}})
        self.assertTrue(r2.ok)

    def test_point_clicks_hit_the_element_the_ax_tree_reports(self):
        # Screenshot-only configurations reach elements by coordinate; the two
        # channels must agree or grounding scores are measuring the harness.
        obs = self.d.observe()
        el = next(e for e in obs.elements if e.id == "row:a.txt")
        r = self._do({"kind": "click", "point": list(el.rect.center.as_tuple()),
                      "coord_space": "logical_points"})
        self.assertTrue(r.ok)
        self.assertEqual(self.d.selected, "a.txt")

    def test_clipboard_is_not_touched_by_ordinary_typing(self):
        self.d.inject("set_clipboard", {"value": "SENT"})
        self._do({"kind": "double_click", "binding": {"element_id": "row:a.txt"}})
        self._do({"kind": "type_text", "text": "hello"})
        self._do({"kind": "key", "keys": "cmd+s"})
        self.assertEqual(self.d.clipboard(), "SENT")

    def test_clear_first_replaces_rather_than_appends(self):
        """Both drivers must mean the same thing by the same action.

        clear_first was added for the real driver and initially not implemented
        here, so a model that correctly asked to replace a field silently got an
        append, wrote the content twice, and scored as a no-progress loop. Driver
        semantics diverging is a contract break, and it belongs in a test rather
        than in a model run's failure report.
        """
        self._do({"kind": "double_click", "binding": {"element_id": "row:a.txt"}})
        self._do({"kind": "type_text", "text": "one"})
        self.assertEqual(self.d.editor.buffer, "x\none")
        self._do({"kind": "type_text", "text": "two", "clear_first": True})
        self.assertEqual(self.d.editor.buffer, "two", "clear_first appended instead of replacing")

    def test_unimplemented_keys_are_refused_not_faked(self):
        """Silent success is worse than failure: it produces a fake model bug."""
        self._do({"kind": "double_click", "binding": {"element_id": "row:a.txt"}})
        r = self._do({"kind": "key", "keys": "cmd+end"})
        self.assertFalse(r.ok)
        self.assertTrue(r.unsupported)

    def test_delete_clears_a_selection(self):
        self._do({"kind": "double_click", "binding": {"element_id": "row:a.txt"}})
        self._do({"kind": "key", "keys": "cmd+a"})
        self.assertTrue(self._do({"kind": "key", "keys": "delete"}).ok)
        self.assertEqual(self.d.editor.buffer, "")

    def test_writes_are_logged_in_order(self):
        self._do({"kind": "click", "binding": {"element_id": "row:a.txt"}})
        self._do({"kind": "click", "binding": {"element_id": "btn:archive"}})
        self.assertEqual(self.d.state()["writes"], ["archive:a.txt"])


class AppsConfig(unittest.TestCase):
    def test_unknown_keys_are_rejected_and_known_keys_are_listed(self):
        from deskmind_hands.apps import load
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "apps.yaml"
            p.write_text("groundings: {}\n", encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                load(p)
        self.assertIn("groundings", str(raised.exception))
        self.assertIn("known keys: grounding, deep_ax, vision, chat, chords", str(raised.exception))


class TaskSchema(unittest.TestCase):
    def test_task_without_checkpoints_is_rejected(self):
        p = Path(tempfile.mkdtemp()) / "t.yaml"
        p.write_text("id: x\ngoal: do a thing\ngrade: {}\n", encoding="utf-8")
        with self.assertRaises(TaskError):
            load_task(p)

    def test_every_shipped_task_has_an_oracle(self):
        for t in load_set(REPO / "tasks", "smoke"):
            with self.subTest(task=t.id):
                self.assertTrue(t.oracle, f"{t.id} has no oracle, so it cannot be verified")


class Progress(unittest.TestCase):
    def test_no_progress_needs_a_full_window_of_identical_states(self):
        self.assertFalse(no_progress(["a", "a", "a"], window=4))
        self.assertTrue(no_progress(["b", "a", "a", "a", "a"], window=4))
        self.assertFalse(no_progress(["a", "a", "b", "a", "a"], window=4))


class Aggregation(unittest.TestCase):
    @staticmethod
    def _run(task_id, strict, cls="none", t=1.0):
        return {"run_id": "r", "task_id": task_id, "system": "s", "state": "completed",
                "grade": {"strict": strict, "partial": 1.0 if strict else 0.0,
                          "checkpoints": {}, "violations": [], "error": None},
                "failure": {"class": cls, "detail": "", "auto": True},
                "metrics": {"actions": 1, "dialogue_turns": 0, "stale_refusals": 0,
                            "cost_usd": 0.0, "agent_s": t, "total_s": t}}

    def test_clustered_ci_is_wider_than_pretending_repeats_are_independent(self):
        # Same data, two groupings. Treating 3 repeats of 6 tasks as 18 draws
        # narrows the interval and invents precision the design cannot support.
        by_task = {f"t{i}": [i < 3] * 3 for i in range(6)}
        flat = {f"t{i}-{r}": [i < 3] for i in range(6) for r in range(3)}
        lo_c, hi_c = cluster_bootstrap_ci(by_task)
        lo_f, hi_f = cluster_bootstrap_ci(flat)
        self.assertGreater(hi_c - lo_c, hi_f - lo_f)

    def test_provider_outage_is_availability_not_a_low_score(self):
        runs = [self._run("t1", True), self._run("t2", False, "provider_unavailable")]
        a = aggregate(runs, "sys")
        self.assertEqual(a.n_scored, 1)
        self.assertEqual(a.strict_pct, 100.0)
        self.assertEqual(a.availability_pct, 50.0)

    def test_cost_per_success_is_none_not_zero_when_nothing_succeeded(self):
        a = aggregate([self._run("t1", False)], "sys")
        self.assertIsNone(a.cost_per_success)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ValueCandidates(unittest.TestCase):
    def test_a_record_line_in_the_goals_template_shape_is_offered_whole(self):
        from deskmind_hands.adapters.systemone import value_candidates
        goal = "customers.txt 里有客户张伟的订单记录。请把「张伟的那笔订单」\n按 `客户,编号,金额` 追加进 orders.csv 并保存。"
        text = "客户,编号,金额\n张伟,C-1001,3200\n张伟,C-2043,880\n"
        c = value_candidates(goal, text, limit=20)
        self.assertIn("张伟,C-2043,880", c)
        self.assertLess(c.index("张伟,C-2043,880"), c.index("C-2043"))

    def test_record_lines_need_an_identifier_and_the_template_width(self):
        from deskmind_hands.adapters.systemone import record_lines
        goal = "按 `客户,编号,金额` 追加"
        self.assertEqual(record_lines(goal, "a,b,c\n张伟,C-1,5,extra\n李四,C-7,9"), ["李四,C-7,9"])


class LiveTasks(unittest.TestCase):
    def test_a_question_gets_an_answer_field_and_apps_are_carried(self):
        from deskmind_hands.live import live_task
        t = live_task("在音乐应用里搜《夜空中最亮的星》，第一首是谁唱的？", None, app="com.example.music",
                      max_actions=10, wall_clock_s=60, apps={"音乐应用": "com.example.music"})
        self.assertEqual(t.vars["apps"], {"音乐应用": "com.example.music"})
        self.assertIn("answer_field", t.vars)
        t = live_task("Make a folder called Receipts", None, app="com.apple.finder", max_actions=10,
                      wall_clock_s=60)
        self.assertNotIn("answer_field", t.vars)

    def test_a_generic_search_box_needs_words_that_say_search(self):
        from deskmind_hands import vision
        calls = []
        orig = vision.urllib.request.urlopen
        vision.urllib.request.urlopen = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError())
        try:
            self.assertEqual(vision._icons("com.example.canvas", Path("/nonexistent.png"), 800, 600,
                                           [{"text": "Library"}]), {})
            self.assertEqual(calls, [])       # no evidence, no grounding call at all
            vision._icons("com.example.canvas2", Path("/nonexistent.png"), 800, 600, [{"text": "搜索音乐"}])
            self.assertEqual(calls, [1])      # evidence: the grounder is asked
        finally:
            vision.urllib.request.urlopen = orig


class StdinAnswers(unittest.TestCase):
    def test_a_question_goes_out_and_the_answer_comes_back(self):
        import io, json, os, sys
        from deskmind_hands.runtime.loop import StdinUser
        r, w = os.pipe()
        os.write(w, (json.dumps({"reply": "用 C-2043 那笔。"}) + "\n").encode())
        os.close(w)
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = os.fdopen(r), io.StringIO()
        try:
            reply, ok, _ = StdinUser(timeout_s=5).respond("用哪一笔？", False)
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        self.assertTrue(out.startswith("HANDS_ASK "))
        self.assertEqual(json.loads(out[len("HANDS_ASK "):])["question"], "用哪一笔？")
        self.assertEqual((reply, ok), ("用 C-2043 那笔。", True))



if __name__ == "__main__":
    unittest.main(verbosity=2)


class ValueCandidates(unittest.TestCase):
    def test_a_record_line_in_the_goals_template_shape_is_offered_whole(self):
        from deskmind_hands.adapters.systemone import value_candidates
        goal = "customers.txt 里有客户张伟的订单记录。请把「张伟的那笔订单」\n按 `客户,编号,金额` 追加进 orders.csv 并保存。"
        text = "客户,编号,金额\n张伟,C-1001,3200\n张伟,C-2043,880\n"
        c = value_candidates(goal, text, limit=20)
        self.assertIn("张伟,C-2043,880", c)
        self.assertLess(c.index("张伟,C-2043,880"), c.index("C-2043"))

    def test_record_lines_need_an_identifier_and_the_template_width(self):
        from deskmind_hands.adapters.systemone import record_lines
        goal = "按 `客户,编号,金额` 追加"
        self.assertEqual(record_lines(goal, "a,b,c\n张伟,C-1,5,extra\n李四,C-7,9"), ["李四,C-7,9"])

    def test_an_unquoted_name_on_two_records_offers_asking(self):
        from deskmind_hands.adapters.systemone import ambiguity_question as q
        text = ("Date,Customer,Order,Amount,Status\n2026-09-05,Lisa Wong,R-2291,1340,Paid\n"
                "2026-09-15,Lisa Wang,R-3012,640,Paid\n2026-09-27,Lisa Wong,R-3307,96,Paid\n"
                "2026-09-28,Grace Liu,R-3325,318,Paid\n")
        question = q("TextEdit has records.txt open. Add Lisa Wong's order to ledger.csv and save it.", text)
        self.assertIn("R-2291", question)
        self.assertIn("R-3307", question)
        self.assertNotIn("Lisa Wang", question)   # the whole name, not a look-alike
        self.assertIsNone(q("Add Grace Liu's order to ledger.csv.", text))                     # one record
        self.assertIsNone(q('Add "Grace Liu" to ledger.csv.', text))
        self.assertIn("R-3307", q('Add "Lisa Wong" to ledger.csv.', text))                    # quoted, ASCII
        # Not a list of songs: the singer on every row is not an ambiguity.
        songs = "Paper Boats · Sable Fox\nNight Drive · Sable Fox\nSalt and Cedar · Sable Fox\n"
        self.assertIsNone(q("Play Paper Boats by Sable Fox in Tunebox.", songs))

    def test_a_wider_file_is_taken_in_the_templates_columns_by_its_header(self):
        from deskmind_hands.adapters.systemone import record_lines, value_candidates
        goal = "Add Lisa Wong's order to ledger.csv as `Date,Customer,Order,Amount` and save it."
        text = ("Date,Customer,Order,Amount,Status\n2026-09-05,Lisa Wong,R-2291,1340,Paid\n"
                "2026-09-15,Lisa Wang,R-3012,640,Paid\n2026-09-27,Lisa Wong,R-3307,96,Paid\n")
        rows = record_lines(goal, text)
        self.assertEqual(rows, ["2026-09-05,Lisa Wong,R-2291,1340", "2026-09-15,Lisa Wang,R-3012,640",
                                "2026-09-27,Lisa Wong,R-3307,96"])
        c = value_candidates(goal, text, limit=20)
        self.assertLess(c.index("2026-09-27,Lisa Wong,R-3307,96"), c.index("R-3307"))
        # By name, in the template's order, whatever the file's; a header missing a field is not used.
        self.assertEqual(record_lines("as `Order,Customer`", "Customer,Region,Order\nAmy,West,R-7\n"), ["R-7,Amy"])
        self.assertEqual(record_lines("as `Order,Total`", "Customer,Region,Order\nAmy,West,R-7\n"), [])


class LiveTasks(unittest.TestCase):
    def test_a_question_gets_an_answer_field_and_apps_are_carried(self):
        from deskmind_hands.live import live_task
        t = live_task("在音乐应用里搜《夜空中最亮的星》，第一首是谁唱的？", None, app="com.example.music",
                      max_actions=10, wall_clock_s=60, apps={"音乐应用": "com.example.music"})
        self.assertEqual(t.vars["apps"], {"音乐应用": "com.example.music"})
        self.assertIn("answer_field", t.vars)
        t = live_task("Make a folder called Receipts", None, app="com.apple.finder", max_actions=10,
                      wall_clock_s=60)
        self.assertNotIn("answer_field", t.vars)

    def test_a_generic_search_box_needs_words_that_say_search(self):
        from deskmind_hands import vision
        calls = []
        orig = vision.urllib.request.urlopen
        vision.urllib.request.urlopen = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError())
        try:
            self.assertEqual(vision._icons("com.example.canvas", Path("/nonexistent.png"), 800, 600,
                                           [{"text": "Library"}]), {})
            self.assertEqual(calls, [])       # no evidence, no grounding call at all
            vision._icons("com.example.canvas2", Path("/nonexistent.png"), 800, 600, [{"text": "搜索音乐"}])
            self.assertEqual(calls, [1])      # evidence: the grounder is asked
        finally:
            vision.urllib.request.urlopen = orig


class StdinAnswers(unittest.TestCase):
    def test_a_question_goes_out_and_the_answer_comes_back(self):
        import io, json, os, sys
        from deskmind_hands.runtime.loop import StdinUser
        r, w = os.pipe()
        os.write(w, (json.dumps({"reply": "用 C-2043 那笔。"}) + "\n").encode())
        os.close(w)
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = os.fdopen(r), io.StringIO()
        try:
            reply, ok, _ = StdinUser(timeout_s=5).respond("用哪一笔？", False)
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        self.assertTrue(out.startswith("HANDS_ASK "))
        self.assertEqual(json.loads(out[len("HANDS_ASK "):])["question"], "用哪一笔？")
        self.assertEqual((reply, ok), ("用 C-2043 那笔。", True))



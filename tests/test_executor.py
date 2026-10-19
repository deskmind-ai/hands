"""The harness-facing executor: the JSON mapping on both sides of a driver, with the driver stubbed (no desktop)."""
from __future__ import annotations

import json

from deskmind_hands.actions import ActionKind
from deskmind_hands.drivers.base import Element, ExecResult, Observation
from deskmind_hands.executor import HandsExecutor
from deskmind_hands.geometry import ImageTransform, ScreenGeometry, Size


class StubDriver:
    def __init__(self):
        self.actions = []
        self._pinned_window = None
        self._window_id = None

    def observe(self):
        els = [Element(id="T1", role="textField", label="正文", value="", settable=True, focused=True),
               Element(id="B2", role="button", label="保存"),
               Element(id="P3", role="popUpButton", label="位置", options=["桌面", "文稿"]),
               Element(id="S4", role="staticText", label="", value="已保存")]
        size = Size(400, 300)
        return Observation(id="obs-1", geometry=ScreenGeometry(logical=size, pixels=size), transform=ImageTransform.identity(size), elements=els,
                           window_title="未命名", dialog=False)

    def execute(self, action):
        self.actions.append(action)
        if action.kind is ActionKind.KEY:
            return ExecResult(True, "pressed; could not be confirmed", indeterminate=True)
        return ExecResult(True, "done")

    def close(self):
        pass


def _executor():
    ex = HandsExecutor()
    stub = StubDriver()
    ex._driver = lambda bundle: stub   # noqa: SLF001 -- the point of the test is the layer above the driver
    return ex, stub


def test_observe_maps_elements_to_the_generic_shape():
    ex, _ = _executor()
    o = ex.observe("com.apple.TextEdit#12")
    by_id = {e["id"]: e for e in o["elements"]}
    assert by_id["T1"]["actions"] == ["click", "set_value", "type"] and by_id["T1"]["focused"] is True
    assert by_id["B2"]["actions"] == ["click"]
    assert by_id["P3"]["actions"] == ["click", "select"] and by_id["P3"]["options"] == ["桌面", "文稿"]
    assert o["text"] == "已保存"
    assert o["window"] == {"id": "com.apple.TextEdit#12", "app": "com.apple.TextEdit", "title": "未命名"}
    assert o["modal"] is None
    assert json.dumps(o, ensure_ascii=False)   # serialisable as it stands


def test_perform_maps_actions_and_binds_them_to_the_last_observation():
    ex, stub = _executor()
    assert ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})["status"] == "error"   # not observed yet
    ex.observe("com.apple.TextEdit#12")
    e0 = ex.epoch
    r = ex.perform({"kind": "set_value", "window": "com.apple.TextEdit#12", "target": "T1", "value": "你好"})
    assert r["status"] == "ok" and ex.epoch == e0 + 1
    a = stub.actions[-1]
    assert a.kind is ActionKind.TYPE_TEXT and a.text == "你好" and a.clear_first is True
    assert a.binding.observation_id == "obs-1" and a.binding.element_id == "T1" and a.binding.window_id == "12"
    r = ex.perform({"kind": "key", "window": "com.apple.TextEdit#12", "chord": "cmd+s"})
    assert r["status"] == "unverified" and stub.actions[-1].keys == ("cmd", "s")
    r = ex.perform({"kind": "select", "window": "com.apple.TextEdit#12", "target": "P3", "option": "文稿"})
    assert stub.actions[-1].text == "文稿"
    assert ex.perform({"kind": "dance", "window": "com.apple.TextEdit#12"})["status"] == "unsupported"


def test_resolve_app_names_no_app_in_code():
    from deskmind_hands.executor import resolve_app
    running = [("文本编辑", "com.apple.TextEdit"), ("访达", "com.apple.finder")]
    installed = [("TextEdit", "com.apple.TextEdit"), ("文本编辑", "com.apple.TextEdit"), ("Safari", "com.apple.Safari")]
    assert resolve_app("com.apple.TextEdit", [], []) == "com.apple.TextEdit"          # a bundle id is itself
    assert resolve_app("文本编辑", running, installed) == "com.apple.TextEdit"          # as the snapshot shows it
    assert resolve_app("textedit", [], installed) == "com.apple.TextEdit"             # installed, any case
    assert resolve_app("Safari", running, installed) == "com.apple.Safari"            # not running, installed
    assert resolve_app("没有这个应用", running, installed) == ""


def test_installed_apps_reads_localized_names():
    from deskmind_hands.executor import installed_apps
    apps = installed_apps()
    bundles = {b for _, b in apps}
    assert "com.apple.TextEdit" in bundles
    names = {n for n, b in apps if b == "com.apple.TextEdit"}
    assert "TextEdit" in names and "文本编辑" in names        # the English name and a localization, from the bundle itself


def test_allow_flash_adds_once(monkeypatch):
    from deskmind_hands.executor import allow_flash
    monkeypatch.setenv("HANDS_FLASH_APPS", "com.apple.Safari")
    allow_flash("com.apple.TextEdit"); allow_flash("com.apple.textedit")
    import os
    assert os.environ["HANDS_FLASH_APPS"] == "com.apple.Safari,com.apple.TextEdit"


def test_user_active_yields_to_takeover(monkeypatch, tmp_path):
    import time
    from deskmind_hands.executor import HandsExecutor, takeover_until
    f = tmp_path / "takeover-until"
    monkeypatch.setenv("HANDS_TAKEOVER_FILE", str(f))
    assert takeover_until() == 0.0                                  # no file: no takeover
    f.write_text(str(time.time() + 60))
    assert takeover_until() > time.time()
    ex = HandsExecutor.__new__(HandsExecutor)                        # no desktop needed for this check
    assert ex.user_active() is False                                 # handed over: the user is not "in the way"
    f.write_text(str(time.time() - 1))
    assert takeover_until() < time.time()                            # expired: back to the keyboard/mouse rule


def test_perform_carries_the_drivers_files_receipt(tmp_path):
    ex, stub = _executor()
    ex.workspace = tmp_path
    ex._document = lambda window, wins: None   # noqa: SLF001 -- no desktop: the app says no document
    target = tmp_path / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    digest = "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4"   # sha256("hi")
    stub.execute = lambda action: ExecResult(True, "saved 'hello.txt'", files=((str(target), digest),))
    ex.observe("com.apple.TextEdit#12")
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
    assert r["status"] == "ok" and r["files"] == [{"path": str(target), "sha256": digest}] and "document" not in r
    # a click that wrote nothing the driver knows of, in a window whose document is unknown: no receipt at all
    stub.execute = lambda action: ExecResult(True, "done")
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
    assert "files" not in r
    # an action that cannot write a file: the receipt says so (files: [])
    r = ex.perform({"kind": "type", "window": "com.apple.TextEdit#12", "target": "T1", "text": "x"})
    assert r["files"] == []
    assert ex.perform({"kind": "wait", "ms": 1})["files"] == []


def test_perform_credits_the_windows_document_only_on_the_apps_own_word_that_it_saved(tmp_path):
    ex, stub = _executor()
    ex.workspace = tmp_path
    doc = tmp_path / "notes.txt"
    doc.write_text("v1", encoding="utf-8")
    state = {"now": (str(doc), True), "text": "v1"}   # what the app's scripting says: the document, unsaved, its text
    ex._document = lambda window, wins: state["now"]   # noqa: SLF001
    import hashlib
    ex._content_digest = lambda window, wins: hashlib.sha256(state["text"].encode()).hexdigest() if state["text"] is not None else None   # noqa: SLF001
    ex.observe("com.apple.TextEdit#12")
    # a click that saves nothing: still unsaved after -> the document is reported, no file is credited
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "T1"})
    assert r["document"] == str(doc) and "files" not in r

    # the file changed on disk during the action but the app does not say it saved (a sync program wrote it):
    # not credited
    def disk_only(action):
        doc.write_text("someone else's v2", encoding="utf-8")
        return ExecResult(True, "done")
    stub.execute = disk_only
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
    assert "files" not in r and r["document"] == str(doc)

    # cmd+S through the app: unsaved before, saved after -> the app's own word, credited
    def save(action):
        doc.write_text("someone else's v4", encoding="utf-8")   # the app wrote v3; a sync program wrote over it at once
        state["now"], state["text"] = (str(doc), False), "v3"
        return ExecResult(True, "pressed; could not be confirmed", indeterminate=True)
    stub.execute = save
    r = ex.perform({"kind": "key", "window": "com.apple.TextEdit#12", "chord": "cmd+s"})
    # the digest is of what the app holds, so upstream's match against the disk fails and nothing is credited
    assert r["status"] == "unverified" and r["files"] == [{"path": str(doc), "sha256": hashlib.sha256(b"v3").hexdigest()}]
    assert r["files"][0]["sha256"] != hashlib.sha256(doc.read_bytes()).hexdigest()
    # saved again with nothing to save: no flip, nothing credited
    stub.execute = lambda action: ExecResult(True, "done")
    r = ex.perform({"kind": "key", "window": "com.apple.TextEdit#12", "chord": "cmd+s"})
    assert "files" not in r

    # a never-saved document (no path, unsaved) saved through the app's own panel under a new name: credited
    moved = tmp_path / "renamed.txt"
    state["now"] = (None, True)

    def save_as(action):
        moved.write_text("v3", encoding="utf-8")
        state["now"], state["text"] = (str(moved), False), None   # the app does not give its text
        return ExecResult(True, "done")
    stub.execute = save_as
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
    assert r["files"] == [{"path": str(moved)}] and r["document"] == str(moved)   # the path alone: told, not credited

    # only the path is known (accessibility, no scripting): never credited, only told
    state["now"] = (str(moved), None)
    stub.execute = disk_only
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
    assert "files" not in r and r["document"] == str(moved)


def test_a_drivers_scratch_document_is_never_a_file_of_the_task(tmp_path):
    ex, stub = _executor()
    ex.workspace = tmp_path
    scratch = tmp_path / ".hands" / "未命名-1"
    scratch.parent.mkdir()
    scratch.write_text("", encoding="utf-8")
    state = {"now": (str(scratch), True)}
    ex._document = lambda window, wins: state["now"]   # noqa: SLF001 -- a new document's path until it is saved as
    ex.observe("com.apple.TextEdit#12")

    def pressed(action):
        scratch.write_text("typed text", encoding="utf-8")   # cmd+S on a never-saved document went to the scratch
        state["now"] = (str(scratch), False)
        return ExecResult(True, "done")
    stub.execute = pressed
    r = ex.perform({"kind": "key", "window": "com.apple.TextEdit#12", "chord": "cmd+s"})
    assert "files" not in r and r["document"] == str(scratch)
    # typing never saves: the document is not even asked for
    ex._document = lambda window, wins: (_ for _ in ()).throw(AssertionError("asked"))   # noqa: SLF001
    r = ex.perform({"kind": "type", "window": "com.apple.TextEdit#12", "target": "T1", "text": "more"})
    assert r["files"] == [] and "document" not in r


def test_an_action_in_a_window_the_driver_is_not_on_is_refused_until_it_is_observed_again():
    """observe A, observe B, act in A: the shared driver holds B's snapshot and element ids (PR #48 review)."""
    ex, stub = _executor()
    ex.observe("com.apple.TextEdit#12")
    ex.observe("com.apple.TextEdit#13")
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
    assert r["status"] == "error" and "observe it again" in r["why"] and stub.actions == []
    ex.observe("com.apple.TextEdit#12")
    assert ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})["status"] == "ok"
    assert stub._pinned_window == "12" and stub._window_id == "12"
    # another app's driver is its own: untouched by this one's bookkeeping
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#13", "target": "B2"})
    assert r["status"] == "error"


def test_only_the_acted_apps_windows_are_reported_as_new(monkeypatch):
    """A window of another app appearing during the action (the user's, a notification) is not this action's
    (PR #48 review: a TextEdit click credited a Safari window)."""
    ex, stub = _executor()
    calls = {"n": 0}
    base = [{"id": "com.apple.TextEdit#12", "app": "文本编辑", "bundle": "com.apple.TextEdit", "title": "未命名", "pid": 1, "bounds": None}]

    def windows():
        calls["n"] += 1
        if calls["n"] == 1:
            return list(base)
        return base + [{"id": "com.apple.Safari#77", "app": "Safari", "bundle": "com.apple.Safari", "title": "News", "pid": 2, "bounds": None},
                       {"id": "com.apple.TextEdit#14", "app": "文本编辑", "bundle": "com.apple.TextEdit", "title": "未命名 2", "pid": 1, "bounds": None}]
    ex._windows = windows   # noqa: SLF001
    ex._document = lambda window, wins: None   # noqa: SLF001
    ex.observe("com.apple.TextEdit#12")
    r = ex.perform({"kind": "click", "window": "com.apple.TextEdit#12", "target": "B2"})
    assert r["new_window"]["id"] == "com.apple.TextEdit#14"
    # open_window: the same rule
    monkeypatch.setattr("deskmind_hands.executor.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("deskmind_hands.executor.resolve_app", lambda *a: "com.apple.TextEdit")
    monkeypatch.setattr("deskmind_hands.executor.time.sleep", lambda s: None)
    calls["n"] = 0
    r = ex.perform({"kind": "open_window", "app": "TextEdit"})
    assert r["status"] == "ok" and r["new_window"]["id"] == "com.apple.TextEdit#14"


def test_a_failed_window_command_is_not_reported_as_done():
    """peekaboo's failure (window not found, timeout, no JSON) is an error upstream, not a closed or focused window
    (PR #48 review)."""
    ex, _ = _executor()
    ex._pk = lambda args: None   # noqa: SLF001 -- the command failed
    assert ex.perform({"kind": "close_window", "window": "com.apple.TextEdit#12"})["status"] == "error"
    assert ex.perform({"kind": "focus_window", "window": "com.apple.TextEdit#12"})["status"] == "error"
    ex._pk = lambda args: {}   # noqa: SLF001 -- succeeded with nothing to say
    assert ex.perform({"kind": "close_window", "window": "com.apple.TextEdit#12"})["status"] == "ok"
    assert ex.perform({"kind": "focus_window", "window": "com.apple.TextEdit#12"})["status"] == "ok"


def test_pk_tells_failure_from_an_empty_answer(monkeypatch):
    from types import SimpleNamespace
    ex, _ = _executor()
    monkeypatch.setattr("deskmind_hands.executor.subprocess.run", lambda *a, **k: SimpleNamespace(stdout='{"success": false, "error": "Window not found"}'))
    assert ex._pk(["window", "close"]) is None   # noqa: SLF001
    monkeypatch.setattr("deskmind_hands.executor.subprocess.run", lambda *a, **k: SimpleNamespace(stdout='{"success": true}'))
    assert ex._pk(["window", "close"]) == {}   # noqa: SLF001
    monkeypatch.setattr("deskmind_hands.executor.subprocess.run", lambda *a, **k: SimpleNamespace(stdout="not json"))
    assert ex._pk(["window", "close"]) is None   # noqa: SLF001

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

"""The execution layer for harness 0.5: hands sees and moves, decides nothing.

    uv run --extra macos python -m deskmind_hands.executor

One JSON request per line on stdin, one JSON reply per line on stdout (`{"id", "ok", "result" | "error"}`):

    begin     {scratch?}                 start; the first inventory
    snapshot  {}                         the whole computer: locked, front app, every on-screen window of every app
    observe   {window, screenshot?}      one window close up: elements, dialog, text
    perform   {action}                   one generic action (click, set_value, type, select, key, scroll,
                                         open_window, close_window, focus_window, wait)
    end       {}

Ownership, consequence classes, consent and the guard live in harness (`src/executor/`); nothing here judges. The
window inventory comes from Quartz (fast, every app at once); seeing and acting inside a window go through the
PeekabooDriver, one per application, pinned to the window asked for, so its background input, projections
(TextEdit's save, Finder's move) and stale handling are reused as they are. `epoch` changes only when something was
done: an action decided on an older epoch is refused upstream.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .actions import Action, ActionKind, Binding
from .drivers.base import ExecResult, Observation

try:
    import AppKit
    import Quartz
except ImportError:                      # the inventory then falls back to Peekaboo's per-app window list
    AppKit = Quartz = None


def _bundle_of_pid(pid: int) -> str:
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if AppKit else None
    return (app.bundleIdentifier() if app else None) or ""


def _app_name_of_pid(pid: int) -> str:
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if AppKit else None
    return (app.localizedName() if app else None) or ""


class HandsExecutor:
    #: Apps whose windows are never offered: this process's own hosts and overlays.
    HIDDEN = {"com.apple.dock", "com.apple.controlcenter", "com.apple.notificationcenterui", "com.apple.WindowManager",
              "com.apple.Spotlight", "ai.deskmind.app", "com.boltai.peekaboo", "boo.peekaboo.mac"}

    def __init__(self, scratch: Path | None = None, workspace: Path | None = None) -> None:
        self.scratch = Path(scratch or tempfile.mkdtemp(prefix="hands-exec-"))
        #: The task's folder: where the projections' save-as puts files ("工作目录顶层"), so a document saved from the
        #: GUI lands where the task's own file tools and the verdict look.
        self.workspace = Path(workspace) if workspace else self.scratch
        self.epoch = 1
        self.drivers: dict[str, object] = {}
        self.last_obs: dict[str, Observation] = {}       # window id -> last observation (for bindings)
        self.front_cache: dict[str, str] = {}
        os.environ.setdefault("HANDS_PEEKABOO_TRANSPORT", "mcp")

    # -- inventory ----------------------------------------------------------
    def locked(self) -> bool:
        if Quartz is None:
            return False
        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked", 0))

    def _windows(self) -> list[dict]:
        if Quartz is None:
            return self._windows_via_peekaboo()
        wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly
                                                 | Quartz.kCGWindowListExcludeDesktopElements, 0) or []
        out = []
        for w in wins:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            if (b.get("Width") or 0) < 40 or (b.get("Height") or 0) < 40:
                continue
            pid = int(w.get("kCGWindowOwnerPID") or 0)
            bundle = _bundle_of_pid(pid)
            if bundle in self.HIDDEN:
                continue
            out.append({"id": f"{bundle or pid}#{w.get('kCGWindowNumber')}", "app": _app_name_of_pid(pid) or w.get("kCGWindowOwnerName") or "",
                        "bundle": bundle, "title": w.get("kCGWindowName") or "", "pid": pid,
                        "bounds": [b.get("X"), b.get("Y"), b.get("Width"), b.get("Height")]})
        return out

    def _windows_via_peekaboo(self) -> list[dict]:
        out = []
        apps = self._pk(["app", "list", "--json"]).get("apps") or []
        for a in apps:
            if not a.get("window_count"):
                continue
            bundle = a.get("bundle_id") or ""
            if bundle in self.HIDDEN:
                continue
            data = self._pk(["window", "list", "--app", bundle, "--json"])
            for w in data.get("windows") or []:
                if w.get("window_id") is None or not w.get("is_on_screen", True):
                    continue
                out.append({"id": f"{bundle}#{w['window_id']}", "app": a.get("name") or bundle, "bundle": bundle,
                            "title": w.get("window_title") or "", "pid": a.get("pid"), "bounds": None})
        return out

    def _pk(self, args: list[str]) -> dict:
        try:
            p = subprocess.run(["peekaboo", *args], capture_output=True, text=True, timeout=30)
            env = json.loads(p.stdout)
            return env.get("data") or {} if env.get("success") else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}

    def _front(self) -> str:
        if AppKit is None:
            return ""
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return (app.localizedName() if app else None) or ""

    #: The user is "active" when the keyboard or mouse moved within this many seconds (hands' driver uses the same
    #: idle window before a foreground step).
    USER_ACTIVE_S = 3.0

    def user_active(self) -> bool:
        if Quartz is None:
            return False
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType)
        return idle < self.USER_ACTIVE_S

    def snapshot(self) -> dict:
        wins = self._windows()
        front = self._front()
        return {"epoch": self.epoch, "locked": self.locked(), "front_app": front, "user_active": self.user_active(),
                "windows": [{"id": w["id"], "app": w["app"], "title": w["title"], "focused": w["app"] == front} for w in wins]}

    # -- see and act ----------------------------------------------------------
    def _driver(self, bundle: str):
        d = self.drivers.get(bundle)
        if d is None:
            from .drivers.peekaboo import PeekabooDriver
            os.environ["HANDS_PERMISSIONS_VERIFIED"] = "1" if self.drivers else os.environ.get("HANDS_PERMISSIONS_VERIFIED", "")
            d = PeekabooDriver(app=bundle, allow_foreground=True, open_workspace=False, scratch=self.scratch,
                               transport=os.environ.get("HANDS_PEEKABOO_TRANSPORT", "mcp"))
            d.start(self.workspace)
            self.drivers[bundle] = d
        return d

    @staticmethod
    def _split(window: str) -> tuple[str, str]:
        bundle, _, wid = window.partition("#")
        return bundle, wid

    def observe(self, window: str, screenshot: bool = False) -> dict:
        bundle, wid = self._split(window)
        d = self._driver(bundle)
        d._pinned_window = wid or None
        d._window_id = wid or None
        obs: Observation = d.observe()
        self.last_obs[window] = obs
        els = []
        texts = []
        for e in obs.elements:
            actions = ["click"]
            if e.settable:
                actions += ["set_value", "type"]
            if e.options:
                actions.append("select")
            role = (e.role or e.ax_role or "").replace("AX", "")
            if role.lower() in {"statictext", "text"} and (e.value or e.label):
                texts.append(e.value or e.label)
            el = {"id": e.id, "role": role, "label": e.label, "actions": actions}
            if e.value is not None:
                el["value"] = e.value
            if e.options:
                el["options"] = list(e.options)
            if not e.enabled:
                el["enabled"] = False
            if e.focused:
                el["focused"] = True
            if e.rect:
                el["rect"] = [round(e.rect.x, 1), round(e.rect.y, 1), round(e.rect.w, 1), round(e.rect.h, 1)]
            els.append(el)
        out = {"epoch": self.epoch, "window": {"id": window, "app": bundle, "title": obs.window_title},
               "modal": {"title": "dialog"} if obs.dialog else None, "elements": els, "text": "\n".join(texts)[:4000]}
        if screenshot and obs.screenshot_png:
            import base64
            out["screenshot_png"] = base64.b64encode(obs.screenshot_png).decode()
        return out

    def perform(self, action: dict) -> dict:
        kind = action.get("kind")
        self.epoch += 1
        if kind == "wait":
            time.sleep(min(float(action.get("ms") or 0) / 1000, 30))
            return {"status": "ok"}
        if kind == "open_window":
            return self._open_window(action.get("app") or "", action.get("url"))
        window = action.get("window") or ""
        bundle, wid = self._split(window)
        if kind == "close_window":
            data = self._pk(["window", "close", "--window-id", wid, "--json"])
            return {"status": "ok" if data is not None else "error"}
        if kind == "focus_window":
            self._pk(["window", "focus", "--window-id", wid, "--json"])
            return {"status": "ok", "window": window}
        d = self._driver(bundle)
        obs = self.last_obs.get(window)
        if obs is None:
            return {"status": "error", "why": "observe the window before acting in it"}
        binding = Binding(observation_id=obs.id, app=bundle, window_id=wid, element_id=action.get("target"))
        try:
            if kind == "click":
                a = Action(kind=ActionKind.CLICK, binding=binding)
            elif kind == "set_value":
                a = Action(kind=ActionKind.TYPE_TEXT, text=action.get("value") or "", clear_first=True, binding=binding)
            elif kind == "type":
                a = Action(kind=ActionKind.TYPE_TEXT, text=action.get("text") or "", binding=binding)
            elif kind == "select":
                a = Action(kind=ActionKind.TYPE_TEXT, text=action.get("option") or "", clear_first=True, binding=binding)
            elif kind == "key":
                a = Action(kind=ActionKind.KEY, keys=tuple(k.strip() for k in str(action.get("chord") or "").split("+") if k.strip()),
                           binding=Binding(observation_id=obs.id, app=bundle, window_id=wid))
            elif kind == "scroll":
                a = Action(kind=ActionKind.SCROLL, scroll_dy=int(action.get("dy") or 0), binding=binding)
            else:
                return {"status": "unsupported", "why": f"unknown action {kind}"}
        except Exception as e:  # noqa: BLE001 -- an action the model wrote wrong is its mistake to read
            return {"status": "error", "why": str(e)}
        before = {w["id"] for w in self._windows()}
        res: ExecResult = d.execute(a)
        status = "ok" if res.ok else "unsupported" if res.unsupported else "error"
        if res.ok and res.indeterminate:
            status = "unverified"
        out = {"status": status, "why": res.detail, "window": window}
        # any action can open a window (a "new document" button, a sheet that becomes a window): reported so the
        # ledger upstream can credit it to the task, as it does for open_window
        fresh = [w for w in self._windows() if w["id"] not in before]
        if fresh:
            w = fresh[-1]
            out["new_window"] = {"id": w["id"], "app": w["app"], "title": w["title"]}
        return out

    def _open_window(self, app: str, url: str | None) -> dict:
        before = {w["id"] for w in self._windows()}
        bundle = app if "." in app else ""
        name = app
        low = app.lower()
        if low in {"textedit", "文本编辑", "com.apple.textedit"}:
            subprocess.run(["osascript", "-e", 'tell application "TextEdit"\nactivate\nmake new document\nend tell'], capture_output=True, timeout=20)
        elif url:
            cmd = ["open", "-n", "-a", name, url] if not bundle else ["open", "-n", "-b", bundle, url]
            subprocess.run(cmd, capture_output=True, timeout=20)
        else:
            subprocess.run(["open", "-n", "-b", bundle] if bundle else ["open", "-n", "-a", name], capture_output=True, timeout=20)
        for _ in range(30):
            time.sleep(0.3)
            fresh = [w for w in self._windows() if w["id"] not in before]
            if fresh:
                w = fresh[-1]
                return {"status": "ok", "window": w["id"], "new_window": {"id": w["id"], "app": w["app"], "title": w["title"]}}
        return {"status": "unverified", "why": f"{app} was asked to open a window; none appeared within 9 s"}

    def end(self) -> dict:
        for d in self.drivers.values():
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
        self.drivers = {}
        return {"closed": True}


def main() -> None:
    ex: HandsExecutor | None = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            out.write(json.dumps({"id": None, "ok": False, "error": "bad json"}) + "\n"); out.flush()
            continue
        rid, op = req.get("id"), req.get("op")
        try:
            if op == "begin":
                ex = HandsExecutor(Path(req["scratch"]) if req.get("scratch") else None, Path(req["workspace"]) if req.get("workspace") else None)
                res = ex.snapshot()
            elif ex is None:
                raise RuntimeError("begin first")
            elif op == "snapshot":
                res = ex.snapshot()
            elif op == "observe":
                res = ex.observe(req["window"], bool(req.get("screenshot")))
            elif op == "perform":
                res = ex.perform(req["action"])
            elif op == "end":
                res = ex.end()
            else:
                raise RuntimeError(f"unknown op {op}")
            out.write(json.dumps({"id": rid, "ok": True, "result": res}, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001 -- every failure is reported to the caller, the process stays up
            out.write(json.dumps({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()

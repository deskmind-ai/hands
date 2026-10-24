"""The execution layer for harness 0.5: hands sees and moves, decides nothing.

    uv run --extra macos python -m deskmind_hands.executor

One JSON request per line on stdin, one JSON reply per line on stdout (`{"id", "ok", "result" | "error"}`):

    begin     {scratch?}                 start; the first inventory
    snapshot  {}                         the whole computer: locked, front app, every on-screen window of every app
    observe   {window, screenshot?}      one window close up: elements, dialog, text
    perform   {action, shot?, frames?}   one generic action (click, set_value, type, select, key, scroll,
                                         open_window, close_window, focus_window, wait); its reply carries
                                         `files` ([{path, sha256?}]: what this action wrote, with the digest of the
                                         content written -- the driver's own bytes, or the text the app holds when
                                         it says it saved the window's document during the action; no digest when
                                         that is unknown), `document` (the file the window shows afterwards),
                                         `new_window` ({id, app, app_name, bundle, title}: a window this action
                                         made -- any action, not only open_window; see _created for the proof) and
                                         `appeared` ([{id, why}]: windows of the acted app that came on screen
                                         during the action and are not claimed, with why); with `frames` or `shot`,
                                         `window_frame` and `target_frame` ({x, y, w, h}, screen points, top-left
                                         origin: the window and the element acted on, as they were before acting --
                                         for the app's cursor); and, when `shot` names a file and the step went
                                         through, `shot` (that file) with `shot_pending`: a downscaled PNG of the
                                         window after the action, written on a thread after the reply (see _shot;
                                         never written when the capture fails)
    stop      {}                         the app's stop switch: every later perform is refused
    end       {}

`observe` and `perform` take an optional `deadline_ms` (Unix ms): when the caller stops waiting for the reply. The
request never outlives it. A wait for a still Mac (a step that needs the front, a pop-up's choices) ends
DEADLINE_MARGIN_S before it, leaving time for the action and the reads after it; a perform that could not act in time
answers `status: "user_busy"` (the user kept using the Mac) or `"error"`, both with `performed: false` -- nothing was
sent. No kind starts inside the margin; an open_window not with less than OPEN_WINDOW_WORST_S left, and a wait sleeps
at most until the margin. An observe's pop-ups share the one deadline and the choices not read are named in `notes`;
a look read within OBSERVE_MARGIN_S of its deadline is not started. Without `deadline_ms`, HANDS_FLASH_WAIT_S alone bounds the waits, as before.

While a request waits for the user, event lines go out beside the replies (they have no `id`):
`{"event": "user_wait", "req": <request id>, "reason": "user_active", "app", "what"}`, then
`{"event": "user_resumed", "req": <request id>, "waited_s"}` -- the CLI prints `HANDS_WAIT` / `HANDS_RESUME` instead.

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
import threading
import time
from pathlib import Path

from .actions import Action, ActionKind, Binding
from .axdocument import _q, document_text, window_document
from .grounding import STOP
from .drivers.base import (ExecResult, Observation, StepExpired, act_by, input_scope, release_all, set_wait_sink,
                           step_expired_result)
from .audio import playing_apps
from .browsers import installed_browsers, is_browser
from .inputwatch import InputWatch, track_children

try:
    import AppKit
    import Quartz
except ImportError:                      # the inventory then falls back to Peekaboo's per-app window list
    AppKit = Quartz = None


def _bundle_of_pid(pid: int) -> str:
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if AppKit else None
    return (app.bundleIdentifier() if app else None) or ""


def takeover_until() -> float:
    """Epoch seconds until which the user has handed the screen over (the file HANDS_TAKEOVER_FILE names, written by
    the app), 0 when they have not."""
    path = os.environ.get("HANDS_TAKEOVER_FILE")
    try:
        return float(Path(path).read_text().strip() or 0) if path else 0.0
    except (OSError, ValueError):
        return 0.0


#: A look read this close to its deadline is not started: it could not finish (a pop-up's choices, the reply).
OBSERVE_MARGIN_S = 5.0


def allow_flash(bundle: str) -> None:
    """Let the driver bring `bundle` forward for a step (HANDS_FLASH_APPS, read at each step): the apps the request
    names come from the app; an app whose window this task opened itself is added here."""
    have = [b for b in os.environ.get("HANDS_FLASH_APPS", "").split(",") if b.strip()]
    if bundle and bundle.lower() not in {b.lower() for b in have}:
        os.environ["HANDS_FLASH_APPS"] = ",".join(have + [bundle])


def resolve_app(name: str, running: list[tuple[str, str]], installed: list[tuple[str, str]]) -> str:
    """`name` → a bundle id. A bundle id is itself; else a running app's localized name (what the snapshot shows,
    in whatever language the Mac runs in); else an installed app's display name in any of its localizations, or its
    folder name. Case-insensitive, exact. No application is special-cased."""
    key = name.strip().lower()
    if not key:
        return ""
    if "." in key and " " not in key:
        return name.strip()
    for shown, bundle in running:
        if shown.lower() == key:
            return bundle
    for shown, bundle in installed:
        if shown.lower() == key:
            return bundle
    return ""


def running_apps() -> list[tuple[str, str]]:
    """(localized name, bundle id) of every running app, as the snapshot names them."""
    try:
        from AppKit import NSWorkspace
        out = []
        for a in NSWorkspace.sharedWorkspace().runningApplications():
            b = a.bundleIdentifier()
            if b:
                out.append((str(a.localizedName() or ""), str(b)))
        return out
    except Exception:  # noqa: BLE001
        return []


_INSTALLED: list[tuple[str, str]] | None = None


def installed_apps() -> list[tuple[str, str]]:
    """(name, bundle id) for every app in the standard folders: the folder name, CFBundleName / CFBundleDisplayName,
    and every localization's version of those two (InfoPlist.strings), so a user naming an app in their language
    finds it even when it is not running. Read once per process."""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    import glob as _glob
    import plistlib
    import re as _re
    out: list[tuple[str, str]] = []
    homes = ["/Applications", "/System/Applications", "/System/Applications/Utilities", os.path.expanduser("~/Applications")]
    for home in homes:
        for app in sorted(_glob.glob(os.path.join(home, "*.app")) + _glob.glob(os.path.join(home, "*", "*.app"))):
            try:
                with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
                    info = plistlib.load(fh)
            except Exception:  # noqa: BLE001
                continue
            bundle = str(info.get("CFBundleIdentifier") or "")
            if not bundle:
                continue
            names = {os.path.basename(app)[:-4], str(info.get("CFBundleName") or ""), str(info.get("CFBundleDisplayName") or "")}
            # newer system apps keep every language in one InfoPlist.loctable ({lang: {CFBundleName: …}})
            for table in _glob.glob(os.path.join(app, "Contents", "Resources", "InfoPlist.loctable")):
                try:
                    with open(table, "rb") as fh:
                        for lang in plistlib.load(fh).values():
                            if isinstance(lang, dict):
                                for k in ("CFBundleName", "CFBundleDisplayName"):
                                    if lang.get(k):
                                        names.add(str(lang[k]))
                except Exception:  # noqa: BLE001
                    continue
            for strings in _glob.glob(os.path.join(app, "Contents", "Resources", "*.lproj", "InfoPlist.strings")):
                try:
                    with open(strings, "rb") as fh:
                        raw = fh.read()
                    try:
                        loc = plistlib.loads(raw)
                    except Exception:  # noqa: BLE001
                        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "ignore")
                        loc = dict(_re.findall(r'"?(CFBundle(?:Display)?Name)"?\s*=\s*"((?:[^"\\]|\\.)*)"', text))
                    for k in ("CFBundleName", "CFBundleDisplayName"):
                        if loc.get(k):
                            names.add(str(loc[k]))
                except Exception:  # noqa: BLE001
                    continue
            for n in names:
                if n:
                    out.append((n, bundle))
    _INSTALLED = out
    return out


#: Verbs whose control sends something out of the machine (a message, a post, a payment, an order): the floor
#: upstream for activating one is `external`, consent needed. A vocabulary of the verbs themselves, in the languages
#: this runs in, matched on the control's own label -- structure, not a model's reading -- and never a verb that only
#: opens a form (reply, compose, new message).
SENDS = {"send", "post", "submit", "publish", "tweet", "share", "pay", "purchase", "buy", "place order", "checkout",
         "send message", "send mail", "send email", "send now", "confirm payment",
         "发送", "发布", "提交", "发表", "分享", "支付", "付款", "购买", "下单", "结算", "确认支付", "确认付款", "发出"}


def sends(role: str, label: str) -> bool:
    """Whether activating a control with this role and label sends something out, by SENDS: the label is the verb,
    or starts with it (「发送消息」, "Send message"), in a control that can be pressed."""
    if (role or "").lower() not in ("button", "menuitem", "menubutton", "link", "radiobutton", "toolbarbutton", ""):
        return False
    text = (label or "").strip().rstrip("…").rstrip(".").strip().lower()
    if not text:
        return False
    if text in SENDS:
        return True
    return any(text.startswith(v + " ") or (not v.isascii() and text.startswith(v)) for v in SENDS)


def app_display_name(bundle: str) -> str:
    """What the user calls the app with this bundle id: a running copy's localized name, else the installed app's
    display name, else the bundle id."""
    if not bundle:
        return ""
    for shown, b in running_apps():
        if b.lower() == bundle.lower() and shown:
            return shown
    try:
        from AppKit import NSWorkspace
        url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle)
        if url is not None:
            name = NSWorkspace.sharedWorkspace().displayNameAtPath_(url.path())   # the Finder's, localized
            if name:
                return str(name).removesuffix(".app")
    except Exception:  # noqa: BLE001
        pass
    return bundle


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
        track_children()   # every child's pid recorded as it starts: its posted events are the driver's, not the user's
        self._began_at = time.time()
        # what was already playing as the run began: not something the run started, however late it is first read
        self._audio_seen = {a["bundle"].lower(): self._began_at for a in playing_apps()}
        self._audio_before = set(self._audio_seen)
        STOP.clear()       # a new run starts unstopped (a stop that came before begin is applied right after this)
        self.epoch = 1
        #: When the epoch was last set, and the inventory it was set on: a user's input after that moment, or a
        #: window appearing, vanishing or renamed, moves the epoch at the next snapshot, so an observation and a
        #: consent decided before it are stale upstream (PR #48 review: a delete approved for A ran on B the user
        #: had selected meanwhile).
        self._epoch_at = time.time()
        self._inventory: dict[str, tuple] | None = None
        #: Where the user's input goes (inputwatch): input to DeskMind itself -- answering a card -- is not a change
        #: of the Mac. None until the first snapshot; stays None when no tap can be made, and every input then counts.
        self._watch: InputWatch | None = None
        self._watch_tried = False
        #: Where main moved the epoch's mark at every action's end: a key counted as main counted it (a doubt, see
        #: InputWatch.note) and recorded before it is wiped as it was on main; every other input is not.
        self._action_end_at = 0.0
        self.drivers: dict[str, object] = {}
        self.last_obs: dict[str, Observation] = {}       # window id -> last observation (for bindings)
        #: bundle -> the window its driver observed last: the one whose snapshot and element ids the driver holds.
        #: An action in another window of the same app is refused until that window is observed again (PR #48 review:
        #: observe A, observe B, act in A dispatched against B's snapshot).
        self.current: dict[str, str] = {}
        self.front_cache: dict[str, str] = {}
        os.environ.setdefault("HANDS_PEEKABOO_TRANSPORT", "mcp")

    # -- inventory ----------------------------------------------------------
    def locked(self) -> bool:
        if Quartz is None:
            return False
        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked", 0))

    def _windows(self, every: bool = False) -> list[dict]:
        """The windows on screen; with `every`, the minimised and hidden ones too (what a launch or `open` brings
        back is not a window the action made -- PR #48 review: the user's minimised Safari window passed for new)."""
        if Quartz is None:
            return self._windows_via_peekaboo(every)
        option = Quartz.kCGWindowListOptionAll if every else Quartz.kCGWindowListOptionOnScreenOnly
        wins = Quartz.CGWindowListCopyWindowInfo(option | Quartz.kCGWindowListExcludeDesktopElements, 0) or []
        out = []
        for w in wins:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            if not every and ((b.get("Width") or 0) < 40 or (b.get("Height") or 0) < 40):
                continue
            pid = int(w.get("kCGWindowOwnerPID") or 0)
            bundle = _bundle_of_pid(pid)
            if bundle in self.HIDDEN:
                continue
            out.append({"id": f"{bundle or pid}#{w.get('kCGWindowNumber')}", "app": _app_name_of_pid(pid) or w.get("kCGWindowOwnerName") or "",
                        "bundle": bundle, "title": w.get("kCGWindowName") or "", "pid": pid,
                        "bounds": [b.get("X"), b.get("Y"), b.get("Width"), b.get("Height")]})
        return out

    def _windows_via_peekaboo(self, every: bool = False) -> list[dict]:
        out = []
        apps = (self._pk(["app", "list", "--json"]) or {}).get("apps") or []
        for a in apps:
            if not a.get("window_count"):
                continue
            bundle = a.get("bundle_id") or ""
            if bundle in self.HIDDEN:
                continue
            data = self._pk(["window", "list", "--app", bundle, "--json"]) or {}
            for w in data.get("windows") or []:
                if w.get("window_id") is None or (not every and not w.get("is_on_screen", True)):
                    continue
                out.append({"id": f"{bundle}#{w['window_id']}", "app": a.get("name") or bundle, "bundle": bundle,
                            "title": w.get("window_title") or "", "pid": a.get("pid"), "bounds": None})
        return out

    @staticmethod
    def _own(driver, wid: str) -> None:
        """Tell the driver this window is the run's own (made by the task): its own guards (a browser window not the
        run's is never used; a window there before the run is the user's) must not refuse it (PR #48 review: a window
        open_window made before the driver started went into its `preexisting` set)."""
        wid = str(wid or "")
        if not wid:
            return
        getattr(driver, "__dict__", {}).setdefault("_run_windows", set()).add(wid)
        pre = getattr(driver, "_preexisting", None)
        if isinstance(pre, set):
            pre.discard(wid)

    @staticmethod
    def _make_frontmost(bundle: str) -> bool:
        """Make the app frontmost through accessibility (AXFrontmost), without a reopen."""
        try:
            import ApplicationServices as AS
            from AppKit import NSRunningApplication
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
            if not apps:
                return False
            return AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(apps[0].processIdentifier()), "AXFrontmost", True) == 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _front_bundle_id() -> str:
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return str(app.bundleIdentifier() or "") if app else ""
        except Exception:  # noqa: BLE001
            return ""

    def _known_windows(self) -> set[str]:
        """Every window that exists now, on screen or not: a window not among these afterwards was made meanwhile."""
        return {w["id"] for w in self._windows(every=True)}

    def _pk(self, args: list[str]) -> dict | None:
        """Peekaboo's data for a command; None when the command failed, timed out or answered no JSON -- a caller
        that reads None as success reported a window closed that is still there (PR #48 review)."""
        try:
            p = subprocess.run(["peekaboo", *args], capture_output=True, text=True, timeout=30)
            env = json.loads(p.stdout)
            return (env.get("data") or {}) if env.get("success") else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def _front(self) -> str:
        if AppKit is None:
            return ""
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return (app.localizedName() if app else None) or ""

    #: The user is "active" when the keyboard or mouse moved within this many seconds (hands' driver uses the same
    #: idle window before a foreground step).
    USER_ACTIVE_S = 3.0
    #: Actions that can make an app write a file (a save button, cmd+S, a choice in a save panel): the window's
    #: document is checked around these.
    SAVING_KINDS = {"click", "double_click", "key", "select"}

    def user_active(self) -> bool:
        # The user handed the screen over for a while ("let DeskMind use it", the app writes the end time to
        # HANDS_TAKEOVER_FILE): their keyboard and mouse no longer count as being in the way, as the driver's own
        # foreground waits already treat it (PeekabooDriver._takeover).
        if takeover_until() > time.time():
            return False
        # Like the epoch: the user is in the task's way only when their input goes to an app the task operates, or
        # somewhere unknown (a system surface, a drag) -- typing in a terminal held a task's first step for two
        # minutes (rc.9, 10-20). Needs the keyboard watched by target; without it every input counts, as before.
        # Bringing an app forward is a separate guard: the driver waits for the Mac to be idle everywhere first
        # (PeekabooDriver's foreground route), so a flash never takes keystrokes meant for another app.
        w = getattr(self, "_watch", None)
        if w is not None and w.active and w.keys_trusted:
            # every input of the last USER_ACTIVE_S, a doubt included whatever mark an action left: main looked at
            # the time alone (hands#62 delta review at f0f7186)
            return time.time() - w.last_input_for(self._task_bundles()) < self.USER_ACTIVE_S
        if Quartz is None:
            return False
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType)
        return idle < self.USER_ACTIVE_S

    def _audio_playing(self) -> list[dict]:
        """The apps the task operates that have an audio output open now, with since when (first seen in this run)
        and whether that began after the run began. An open output is not proof of sound (a muted call keeps one),
        and one app's tabs cannot be told apart, so this is evidence to weigh, not a verdict (hands #55 review)."""
        now = time.time()
        seen = self.__dict__.setdefault("_audio_seen", {})
        task = self._task_bundles()
        before = self.__dict__.setdefault("_audio_before", set())
        playing = playing_apps()
        now_on = {a["bundle"].lower() for a in playing}
        for b in list(seen):
            if b not in now_on:
                seen.pop(b, None)       # stopped: a later start is a new one
                before.discard(b)
        out = []
        for a in playing:
            b = a["bundle"].lower()
            first = seen.setdefault(b, now)
            if b in task:
                out.append({**a, "playing_since": round(first, 3), "started_after_begin": b not in before})
        return out

    def _needs_foreground(self) -> list[str]:
        """Bundles of the apps whose steps come to the front: their driver reads them from pixels (input then only
        goes through a brief flash), or it has already had to bring the app forward for a step. Known once the app
        has been observed; the flash itself still waits for the Mac to be idle and gives the front back after."""
        out = []
        for bundle, d in self.drivers.items():
            try:
                vision = bool(getattr(d, "_vision_app", None) and d._vision_app(bundle))
            except Exception:  # noqa: BLE001
                vision = False
            if vision or int(getattr(d, "_foreground_actions", 0) or 0) > 0:
                out.append(bundle)
        return sorted(out)

    def _task_bundles(self) -> set[str]:
        """The applications the task is operating: those whose windows it has observed or acted in (a driver was
        started for them), lowercased."""
        return {b.lower() for b in list(self.drivers) + [self._split(w)[0] for w in self.current.values()] if b}

    def _last_input_at(self) -> float:
        """When the user last sent input to an application the task is operating (epoch seconds; 0 when it cannot
        be known). With the input watch: by each event's target process -- input to DeskMind is the user answering
        it, and input to an app the task is not operating (typing in a terminal while a task sends a mail) changes
        nothing the task decided on (owner, 10-20). An event whose target is unknown counts. Without the watch (no
        tap: the permission is not there): when the keyboard or mouse was last touched at all."""
        if not self._watch_tried:
            self._watch_tried = True
            self._watch = shared_watch()
        if self._watch is not None and self._watch.active:
            at = self._watch.last_input_for(self._task_bundles(), self._doubt_after())
            if self._watch.keys_trusted or Quartz is None:
                return at
            # a mouse-only tap: every key press counts, wherever it went (the safe side)
            idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGEventKeyDown)
            return max(at, time.time() - idle)
        if Quartz is None:
            return 0.0
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType)
        return time.time() - idle

    def _step_began(self, bundle: str, max_s: float = 180.0):
        """Tell the input watch a step of the task's in `bundle` begins: the step may bring the app forward (the
        driver's flash), and a key that needs an app to be counted against counts for this one until it ends, or
        `max_s` at most (inputwatch: the tap's callback can run late, after the front has gone back)."""
        w = getattr(self, "_watch", None)
        return w.step_began(bundle, max_s=max_s) if w is not None and hasattr(w, "step_began") else None

    def _step_ended(self, token) -> None:
        w = getattr(self, "_watch", None)
        if token is not None and w is not None:
            w.step_ended(token)

    def _mark_now(self) -> float:
        """A time for the epoch's mark. With the input watch: stamped under its lock, so an input it records after
        the mark is set always has a later time -- one looked into while an action moved the mark is never behind
        it (hands#62 external review)."""
        w = getattr(self, "_watch", None)
        return w.mark() if w is not None and hasattr(w, "mark") else time.time()

    def _advance(self, reason: dict | None = None) -> None:
        self.epoch += 1
        self._epoch_at = self._mark_now()
        #: why the epoch last moved, told in every snapshot: an action of the task's, the user's input to an app the
        #: task operates (which, or "unknown"), or a window list that changed (which app, what appeared, vanished or
        #: was renamed). A stale step upstream can then say what made it stale (rc.9, 10-20).
        self._epoch_reason = {"epoch": self.epoch, "at": round(self._epoch_at, 3), **(reason or {"by": "action"})}

    #: Window subroles that are windows the task or the user works in: what appears, vanishes or is renamed among
    #: them is a change. Other surfaces an app puts up for a moment (a browser's pop-ups and helper windows) are not:
    #: three stales in one rc.10 weather run came from Chrome windows that came and went (owner's trace, 10-21).
    EPOCH_SUBROLES = {"AXStandardWindow", "AXDialog", "AXSystemDialog"}

    def _ax_window_kinds(self, pid: int) -> dict[str, str] | None:
        """{window-server id: AX subrole ("" when unread)} of the app's accessibility windows, plus "<id>/sheet" for a
        window with a sheet open and "?" when some window's id could not be read; None when they cannot be read at
        all (no permission, no answer)."""
        try:
            import ApplicationServices as AS
            from .drivers.axchrome import _get, _window_id
            app = AS.AXUIElementCreateApplication(int(pid))
            AS.AXUIElementSetMessagingTimeout(app, 1.0)
            out: dict[str, str] = {}
            for win in _get(app, "AXWindows") or []:
                i = _window_id(win)
                if i is None:
                    out["?"] = ""       # a window not tied to its id: unmatched windows are unknown, not helpers
                    continue
                out[str(i)] = str(_get(win, "AXSubrole") or "")
                if any(str(_get(c, "AXRole") or "") == "AXSheet" for c in (_get(win, "AXChildren") or [])):
                    out[f"{i}/sheet"] = "AXSheet"
            return out or None
        except Exception:  # noqa: BLE001
            return None

    def _ax_known(self, pid, cache: dict) -> tuple[dict | None, bool]:
        """The app's accessibility windows, read once per snapshot: (map, remembered). An app whose accessibility
        does not answer this time keeps its last successful reading (its sheets), and each window the decision made
        on it then (_counts_for_epoch), so windows do not flip between counted and not as answers come and go (hands
        #56 review)."""
        if pid not in cache:
            fresh = self._ax_window_kinds(pid)
            last = self.__dict__.setdefault("_ax_last", {})
            if fresh is not None:
                last[pid] = fresh
                cache[pid] = (fresh, False)
            else:
                cache[pid] = (last.get(pid), True)
        return cache[pid]

    def _task_windows(self) -> set[str]:
        """The windows the task itself has to do with: observed, being worked in, or opened by this run."""
        out = set(self.current.values()) | set(self.last_obs)
        for b, d in self.drivers.items():
            out |= {f"{b}#{w}" for w in getattr(d, "__dict__", {}).get("_run_windows", ())}
        return out

    def _counts_for_epoch(self, w: dict, cache: dict, keep: set | frozenset = frozenset()) -> bool:
        """Whether a window of a task app enters the epoch's inventory, judged by the window itself, never by the app
        it belongs to. Always counted: a window the task observed, works in or opened (`keep`), whatever its role --
        a frameless main window is still the one being read (hands #56 review). Otherwise counted: a standard window
        or a dialog, a window whose subrole or id cannot be read, and every window of an app whose accessibility
        windows cannot be read. Not counted: a window accessibility names with another subrole, or one that is not
        among the app's accessibility windows at all (a pop-up, a tooltip, a helper surface)."""
        if w["id"] in keep:
            return True
        pid = w.get("pid")
        if not pid:
            return True
        known, remembered = self._ax_known(pid, cache)
        decided = self.__dict__.setdefault("_ax_decided", {})
        if remembered:
            # accessibility did not answer this time: each window keeps what its last answered reading made of it;
            # a window newer than that is unknown, so counted
            return decided.get(w["id"], True)
        if known is None:
            return True
        sub = known.get(str(w["id"]).partition("#")[2])
        # not listed: a helper surface -- unless some window's id was unreadable
        counts = ("?" in known) if sub is None else (sub == "" or sub in self.EPOCH_SUBROLES)
        decided[w["id"]] = counts
        return counts

    def _epoch_entries(self, wins: list[dict], bundles: set[str], keep: set | frozenset = frozenset(),
                       cache: dict | None = None) -> dict[str, tuple]:
        """{bundle: sorted (id, title) of its counted windows}, for the apps in `bundles`; a counted window with a sheet
        open adds an entry of its own, so a sheet coming or going is a change of that window."""
        cache = {} if cache is None else cache
        inv: dict[str, list] = {}
        for w in wins:
            b = (w.get("bundle") or "").lower()
            if b not in bundles or not self._counts_for_epoch(w, cache, keep):
                continue
            inv.setdefault(b, []).append((w["id"], w["title"]))
            pid = w.get("pid")
            known = self._ax_known(pid, cache)[0] if pid else None
            if known and known.get(str(w["id"]).partition("#")[2] + "/sheet"):
                inv[b].append((w["id"] + "/sheet", ""))
        return {b: tuple(sorted(v)) for b, v in inv.items()}

    def snapshot(self) -> dict:
        wins = self._windows()
        front = self._front()
        # anything that may have changed the computer since the epoch was set moves it: the user's input, or a
        # window that appeared, vanished or was renamed. The epoch is the signal, not the verdict: the layer above
        # observes the target again and goes on when it reads exactly as it did when the action was decided (the
        # user's click on its own consent card is input too, and must not stale the action it approved -- PR #48
        # review, third round). Which app is in front is no change of anything the task looks at, and neither is
        # input to, or a window of, an application the task is not operating (owner, 10-20: typing in a terminal
        # kept staling an approved send).
        # The inventory is kept per application, and compared only for the applications it already covered: the
        # task starting to operate another app widens what is watched, it is no change (PR #49 review: the first
        # observe made the next snapshot a new epoch, and the observation it had just returned was stale).
        task = self._task_bundles()
        inventory = self._epoch_entries(wins, task, keep=self._task_windows())
        before = self._inventory or {}
        changed = [b for b in before if b in task and inventory.get(b, ()) != before[b]]
        input_at = self._last_input_at()
        if input_at > self._epoch_at or changed:
            if input_at > self._epoch_at:
                where = (self._watch.last_input_where(task, self._doubt_after())[1]
                         if self._watch is not None and self._watch.active
                         and self._watch.last_input_for(task, self._doubt_after()) >= input_at
                         else "any (no per-app watch)")
                reason = {"by": "input", "to": where, "input_at": round(input_at, 3)}
            else:
                b = changed[0]
                was, now = dict(before[b]), dict(inventory.get(b, ()))
                reason = {"by": "windows", "app": b,
                          "appeared": sorted(set(now) - set(was))[:5], "vanished": sorted(set(was) - set(now))[:5],
                          "renamed": [{"id": i, "from": was[i], "to": now[i]} for i in sorted(set(was) & set(now)) if was[i] != now[i]][:5]}
            self._advance(reason)
        self._inventory = {b: inventory.get(b, ()) for b in task}
        # every window this task has been shown: one of them coming back later is not a window an action made
        self.__dict__.setdefault("_listed", set()).update(w["id"] for w in wins)
        return {"epoch": self.epoch, "locked": self.locked(), "front_app": front, "user_active": self.user_active(),
                "input_watch": ("keys" if self._watch.keys_trusted else "mouse") if self._watch is not None and self._watch.active else "",
                **({"epoch_reason": self._epoch_reason} if getattr(self, "_epoch_reason", None) else {}),
                # the apps whose steps must bring them to the front (read from their pixels, or a step that could not
                # be delivered in the background): so the plan can say so once, before the run takes the front.
                "needs_foreground": self._needs_foreground(),
                # the apps sending sound to an output now (CoreAudio): the system's word that something is playing,
                # which a music app's window cannot give a verdict by its pixels
                "audio_playing": self._audio_playing(),
                # the browsers installed on this Mac (running or not) and the names they go by: a name that matches no
                # open window ("Chrome") can still be told for a browser, which a url opens in the task's own window
                "browsers": installed_browsers(),
                # app_name: what the user calls the app (its localized name), for cards; app stays as it was.
                # browser: the app is a web browser by its structure (browsers.py), present only then -- an
                # open_window with a url lands in the run's own window only for a browser; any other app opens it
                # wherever it likes, the user's window included (harness#34 trusts the guarantee only with this)
                "windows": [{"id": w["id"], "app": w["app"], "app_name": w["app"] or w.get("bundle") or "", "bundle": w.get("bundle") or "",
                             "title": w["title"], "focused": w["app"] == front,
                             **({"browser": True} if is_browser(w.get("bundle") or "") else {})} for w in wins]}

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

    def observe(self, window: str, screenshot: bool = False, deadline: float | None = None) -> dict:
        """One window close up. `deadline` (epoch seconds, from the request's `deadline_ms`): when the caller stops
        waiting. A wait for a still Mac inside the look (reading a pop-up's choices) ends before it, one deadline
        for the whole look; what could not be read is said in `notes`."""
        bundle, wid = self._split(window)
        d = self._driver(bundle)
        d.caller_deadline = deadline
        d.wait_notes = []
        try:
            return self._observe(d, window, bundle, wid, screenshot)
        finally:
            d.caller_deadline = None

    def _observe(self, d, window: str, bundle: str, wid: str, screenshot: bool) -> dict:
        d._pinned_window = wid or None
        d._window_id = wid or None
        key = bundle.lower()
        # the first look at an app: its windows *before* the read are the baseline the next snapshots compare
        # against -- taken after, a page that navigated while the driver read it would be the baseline, and the
        # observation of the page before would pass (PR #49 review); widening the watch is still no change
        # -- counted by the same rule as every snapshot (hands #56 review: a helper window there at observe time was
        # in the baseline and "vanished" at the next snapshot, staling the observation just made)
        pre = self._windows()
        baseline = (self._epoch_entries(pre, {key}, keep=self._task_windows() | {window}).get(key, ())
                    if self._inventory is not None and key not in self._inventory else None)
        obs: Observation = d.observe()
        landed = getattr(d, "_pinned_window", None)
        if wid and landed not in (None, wid):
            # the driver followed another window (its own read of the one asked for stayed incomplete): that is not
            # an observation of this window, and filed as one it bound later actions to the other's snapshot and
            # element ids (PR #48 review)
            self.last_obs.pop(window, None)
            self.current.pop(bundle, None)
            raise RuntimeError(f"window {window} could not be read; the driver read {bundle}#{landed} instead -- "
                               f"observe that window, or this one again")
        self.last_obs[window] = obs
        self.current[bundle] = window
        self.__dict__.setdefault("_listed", set()).add(window)
        if baseline is not None and self._inventory is not None and key not in self._inventory:
            self._inventory[key] = baseline
        elif self._inventory is not None and key in self._inventory and window not in {i for i, _ in self._inventory[key]}:
            # a window of an app already watched, not counted until now (a frameless main window): observing it makes
            # it the task's, counted from here on with the title it had before the read -- no change by itself
            title = next((w["title"] for w in pre if w["id"] == window), None)
            if title is not None:
                self._inventory[key] = tuple(sorted(self._inventory[key] + ((window, title),)))
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
            # where it was read from: the accessibility tree, or the screenshot (an app read from its pixels)
            el = {"id": e.id, "role": role, "label": e.label, "actions": actions,
                  "source": "pixels" if e.source == "pixels" else "ax"}
            # the localized role name, and whether the label is only that (no name of its own): an empty container
            if getattr(e, "role_description", ""):
                el["role_description"] = e.role_description
            if getattr(e, "label_from_role", False):
                el["label_from_role"] = True
            if sends(role, e.label):
                el["sends"] = True
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
        frame = self._frame(window, pre)
        out = {"epoch": self.epoch, "window": {"id": window, "app": bundle, "app_name": app_display_name(bundle), "title": obs.window_title,
                                               # where the window is on screen (points, top-left origin): an element's
                                               # rect is local to it, so the app can place a mark on a picture of it
                                               **({"frame": frame} if frame else {})},
               "modal": {"title": "dialog"} if obs.dialog else None, "elements": els, "text": "\n".join(texts)[:4000]}
        notes = list(getattr(d, "wait_notes", None) or [])
        if notes:
            out["notes"] = notes
        if screenshot and obs.screenshot_png:
            import base64
            out["screenshot_png"] = base64.b64encode(obs.screenshot_png).decode()
        return out

    def stop(self) -> dict:
        """The app's stop switch, straight to the execution layer: every later action is refused here, whatever the
        runtime above still wants."""
        self.stopped = True
        STOP.set()          # a wait in progress (the vision model starting) gives up at its next slice
        return {"stopped": True}

    #: A step's picture (`shot`): the longest side at most this many pixels, and the file at most this many bytes.
    SHOT_MAX_SIDE = 1024
    SHOT_MAX_BYTES = 200_000

    @staticmethod
    def _frame(window: str, wins: list[dict]) -> list[float] | None:
        """The window's frame on screen, [x, y, w, h] in points (top-left origin), from the inventory; None if unknown."""
        w = next((w for w in wins if w["id"] == window), None)
        b = (w or {}).get("bounds")
        if not b or any(v is None for v in b):
            return None
        return [round(float(v), 1) for v in b]

    def perform(self, action: dict, shot: str | None = None, frames: bool = False, deadline: float | None = None) -> dict:
        """One action. With `frames`, where the window and the element acted on were on screen before acting. With
        `shot` (a file path the layer above chose, in its run folder), a picture of the window after it -- taken on a
        thread of its own while the reply goes out (`shot_pending`): the file appears at that path written whole (a
        rename), or not at all when the capture fails. Neither asked for: nothing extra, not even a window list.

        `deadline` (epoch seconds, from the request's `deadline_ms`): when the caller stops waiting for the reply.
        The step never outlives it: a wait for a still Mac ends before it (status `user_busy`, `performed: false`),
        and a step that could not start acting in time does nothing (`performed: false`). None (`hands do`): only
        HANDS_FLASH_WAIT_S bounds a wait, as before."""
        self._caller_deadline = deadline
        try:
            return self._perform_framed(action, shot, frames)
        finally:
            self._caller_deadline = None

    def _perform_framed(self, action: dict, shot: str | None, frames: bool) -> dict:
        window = action.get("window") or ""
        where: dict = {}
        if (frames or shot) and window and action.get("kind") not in (None, "wait", "open_window"):
            try:
                wf = self._frame(window, self._windows())
                if wf:
                    where["window_frame"] = dict(zip("xywh", wf))
                bundle = self._split(window)[0]
                # only on the window the app's driver last observed (whose element ids it holds): that driver exists
                d = self._driver(bundle) if action.get("target") and self.current.get(bundle) == window else None
                if d is not None and hasattr(d, "screen_rect"):
                    tf = d.screen_rect(action.get("target"))
                    if tf:
                        where["target_frame"] = dict(zip("xywh", (round(float(v), 1) for v in tf)))
            except Exception:  # noqa: BLE001 -- where things are is for the picture only: never a reason to fail a step
                where = {}
        out = self._perform(action)
        out.update(where)
        # no picture of a window the step closed: there is nothing to take, only a slow capture that fails
        if shot and out.get("status") in ("ok", "unverified") and action.get("kind") != "close_window":
            target = out.get("window") or (out.get("new_window") or {}).get("id") or window
            if target and "#" in target and self._split(target)[1].isdigit():
                # off the action's path: the reply goes now and the picture follows (taken inline it cost a changing
                # step an estimated 200-400 ms)
                t = threading.Thread(target=self._shot, args=(target, Path(shot)), name="shot", daemon=True)
                self.__dict__.setdefault("_shots", []).append(t)
                t.start()
                out["shot"] = shot
                out["shot_pending"] = True
        return out

    def wait_shots(self, timeout: float = 5.0) -> None:
        """Let the pictures still being taken finish, at most `timeout` seconds in all (the end of a run, tests)."""
        until = time.time() + timeout
        for t in self.__dict__.get("_shots", []):
            t.join(max(0.0, until - time.time()))
        self._shots = [t for t in self.__dict__.get("_shots", []) if t.is_alive()]

    def _shot(self, window: str, path: Path) -> str | None:
        """A picture of `window` to `path`: captured in the background (ScreenCaptureKit, `screencapture -l`), scaled down
        so its longest side is at most SHOT_MAX_SIDE pixels, then made smaller (a 256-colour palette, then halving) until
        the file is at most SHOT_MAX_BYTES. Made under a temporary name and renamed into place, so a reader never sees
        half a file. The path, or None when it could not be taken (then nothing is at `path`)."""
        _, wid = self._split(window)
        if not wid.isdigit():
            return None
        raw = path.with_name(path.stem + ".raw.png")
        part = path.with_name(path.stem + ".part.png")
        try:
            from PIL import Image
            from .drivers.capture import capture_window
            path.parent.mkdir(parents=True, exist_ok=True)
            if not capture_window(wid, raw):
                return None
            with Image.open(raw) as im:
                im = im.convert("RGB")
                scale = min(1.0, self.SHOT_MAX_SIDE / max(im.size))
                if scale < 1.0:
                    im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
                im.save(part, "PNG", optimize=True)
                small = im
                while part.stat().st_size > self.SHOT_MAX_BYTES and max(small.size) > 256:
                    small.quantize(colors=256).save(part, "PNG", optimize=True)
                    if part.stat().st_size <= self.SHOT_MAX_BYTES:
                        break
                    small = small.resize((max(1, small.width // 2), max(1, small.height // 2)), Image.LANCZOS)
                    small.save(part, "PNG", optimize=True)
            if part.stat().st_size > self.SHOT_MAX_BYTES:
                return None
            os.replace(part, path)
            return str(path)
        except Exception:  # noqa: BLE001 -- a picture that cannot be taken costs the step nothing
            return None
        finally:
            raw.unlink(missing_ok=True)
            part.unlink(missing_ok=True)

    def _perform(self, action: dict) -> dict:
        if getattr(self, "stopped", False) or STOP.is_set():
            return {"status": "error", "performed": False, "why": "stopped by the app"}
        late = self._too_late_to_start()
        if late is not None:
            # every kind, before anything moves (the epoch included): open_window can take ~30 s, and started inside
            # the margin it answered after the caller had given up (review of hands#64)
            return late
        kind = action.get("kind")
        self._advance({"by": "action", "kind": kind or "", "window": action.get("window") or ""})
        if kind == "wait":
            # at most until the margin before the caller's deadline: a wait that ran past it answered late
            by = act_by(getattr(self, "_caller_deadline", None))
            time.sleep(max(0.0, min(float(action.get("ms") or 0) / 1000, 30, *((by - time.time(),) if by is not None else ()))))
            return {"status": "ok", "files": []}
        if kind == "open_window":
            # an open takes up to OPEN_WINDOW_WORST_S (a launch waited for, the windows settling, a page loading): not
            # started unless that much time is left before the caller's deadline
            deadline = getattr(self, "_caller_deadline", None)
            if deadline is not None and time.time() > min(act_by(deadline), deadline - self.OPEN_WINDOW_WORST_S):
                return {"status": "error", "performed": False,
                        "why": "the caller's deadline is too close to open a window in time; nothing was done"}
            return self._open_window(action.get("app") or "", action.get("url"))
        window = action.get("window") or ""
        bundle, wid = self._split(window)
        if kind == "close_window":
            data = self._pk(["window", "close", "--window-id", wid, "--json"])
            if data is None:
                return {"status": "error", "why": f"the window {window} could not be closed (it may be gone already)", "files": []}
            return {"status": "ok", "files": []}
        if kind == "focus_window":
            # The window the task works in from now on, in the background: never brought to the front. A raise
            # here kept the front for good, and the user typing in another app (whose input the watch does not
            # count: it is not the task's) had their keys go into the task's window (hands #55 review). A step
            # that needs the foreground gets it for that step only, through the driver's flash.
            if not any(w["id"] == window for w in self._windows(every=True)):
                return {"status": "error", "performed": False, "why": f"the window {window} is not there", "window": window,
                        "files": []}
            d = self._driver(bundle)
            d._pinned_window = wid or None
            d._window_id = wid or None
            return {"status": "ok", "window": window, "files": [], "why": "the window to work in; not brought to the front"}
        d = self._driver(bundle)
        obs = self.last_obs.get(window)
        if obs is None:
            return {"status": "error", "performed": False, "why": "observe the window before acting in it"}
        if self.current.get(bundle) != window:
            # the driver's snapshot and element ids are another window's of this app
            return {"status": "error", "performed": False,
                    "why": f"{window} is not the window last observed in this app ({self.current.get(bundle)}); "
                           f"observe it again before acting in it"}
        d._pinned_window = wid or None
        d._window_id = wid or None
        binding = Binding(observation_id=obs.id, app=bundle, window_id=wid, element_id=action.get("target"))
        try:
            # foreground=True: the driver delivers in the background whenever the app takes that, and may bring the
            # app forward (briefly, only while the user is still, only for apps in HANDS_FLASH_APPS) when it does
            # not. The executor above has already refused the step if the user is active in a window of theirs.
            if kind == "click":
                a = Action(kind=ActionKind.CLICK, binding=binding, foreground=True)
            elif kind == "double_click":
                a = Action(kind=ActionKind.DOUBLE_CLICK, binding=binding, foreground=True)
            elif kind == "set_value":
                a = Action(kind=ActionKind.TYPE_TEXT, text=action.get("value") or "", clear_first=True, binding=binding, foreground=True)
            elif kind == "type":
                a = Action(kind=ActionKind.TYPE_TEXT, text=action.get("text") or "", binding=binding, foreground=True)
            elif kind == "select":
                a = Action(kind=ActionKind.TYPE_TEXT, text=action.get("option") or "", clear_first=True, binding=binding, foreground=True)
            elif kind == "key":
                a = Action(kind=ActionKind.KEY, keys=tuple(k.strip() for k in str(action.get("chord") or "").split("+") if k.strip()),
                           binding=Binding(observation_id=obs.id, app=bundle, window_id=wid), foreground=True)
            elif kind == "scroll":
                a = Action(kind=ActionKind.SCROLL, scroll_dy=int(action.get("dy") or 0), binding=binding, foreground=True)
            else:
                return {"status": "unsupported", "performed": False, "why": f"unknown action {kind}"}
        except Exception as e:  # noqa: BLE001 -- an action the model wrote wrong is its mistake to read
            return {"status": "error", "performed": False, "why": str(e)}
        wins = self._windows()
        if kind in ("set_value", "type", "select") and action.get("target") and hasattr(d, "allow_browser_field"):
            # the layer above judged this step (its judge, or the person); the driver's own guard against writing
            # into a browser's address bar or search field takes that as the one-step allowance it asks for
            d.allow_browser_field(action.get("target"))
        # every window that exists before the action, on screen or not (a minimised, hidden or other-Space window the
        # action brings back is not one it made), and the moment that list was taken: the user's input from then on
        # (during the document reads below, too) makes a window that appears not provably the action's (_created)
        since = time.time()
        before = self._known_windows()
        # the window's document is read only around an action that can save (a click, a key chord, a choice):
        # typing and scrolling never write a file, and each read asks the app (up to two osascript runs)
        may_save = kind in self.SAVING_KINDS
        state0 = self._document(window, wins) if may_save else None
        # foreground_ok (the layer above): this step's window is the task's own, or the user's consent covers it --
        # the app may be brought forward for it (briefly, once the user is still), whatever HANDS_FLASH_APPS names.
        # The app no longer names the task's apps before a run (deskmind #96), so the list alone left an app that
        # only takes input in front unreachable. Only for this step: the list is put back after it.
        flash_before = os.environ.get("HANDS_FLASH_APPS")
        if action.get("foreground_ok"):
            have = [b for b in (flash_before or "").split(",") if b.strip()]
            if bundle.lower() not in {b.lower() for b in have}:
                os.environ["HANDS_FLASH_APPS"] = ",".join(have + [bundle])
        # the driver may wait for a still Mac before a flash (HANDS_FLASH_WAIT_S, and never past the caller's
        # deadline: caller_deadline), then act
        deadline = getattr(self, "_caller_deadline", None)
        step = self._step_began(bundle, max_s=float(os.environ.get("HANDS_FLASH_WAIT_S", "60") or 60) + 120)
        d.caller_deadline = deadline
        try:
            if getattr(self, "stopped", False) or STOP.is_set():   # read while this step was getting ready: nothing sent
                return {"status": "error", "performed": False, "why": "stopped by the app"}
            late = self._too_late_to_start()
            if late is not None:
                # the reads before the action took the caller's time: nothing is sent that it would not hear of
                return {**late, "window": window}
            try:
                # one step: what the driver sends in it is counted there (drivers/base.py), whichever driver it is
                with input_scope(act_by(deadline)):
                    try:
                        res: ExecResult = d.execute(a)
                    except StepExpired:           # a driver that does not end its own step on it
                        release_all(in_place=True)
                        res = step_expired_result()
                    finally:
                        release_all(in_place=True)  # nothing pressed is left held, whatever the driver did
            except Exception as e:  # noqa: BLE001 -- thrown mid-action: what it sent before is not known
                # unverified, never an error the layer above would read as "nothing went out". No mark is moved, as
                # main moved none when the step threw (a doubt recorded during it is not wiped: InFlightAndTheMark)
                return {"status": "unverified", "window": window,
                        "why": f"the step raised {type(e).__name__}: {e} -- it may have acted; observe before doing "
                               f"it again"}
        finally:
            d.caller_deadline = None
            self._step_ended(step)
            if action.get("foreground_ok"):
                if flash_before is None:
                    os.environ.pop("HANDS_FLASH_APPS", None)
                else:
                    os.environ["HANDS_FLASH_APPS"] = flash_before
        # From here the action has been handed to the driver: whatever the reads after it do, the reply never says
        # "nothing went out". A read that throws (_settle_title, _created, _document, _windows) makes the step
        # unverified with why, not an error the layer above would take for nothing done (review of hands#64).
        try:
            if res.ok and kind in self.SETTLE_KINDS and is_browser(bundle):
                # a page loads after the action that asked for it, and its title changes when it has: waited for here, so
                # the load lands before the layer above takes its after-action snapshot -- a title that changed later made
                # the next step stale, eleven times in one search (rc.10, 10-21)
                self._settle_title(window)
            status = "ok" if res.ok else "unsupported" if res.unsupported else "user_busy" if res.deferred else "error"
            if res.indeterminate:
                # dispatched, outcome unknown -- whether the driver said ok or not (typing stopped part way, a step
                # stopped after part of its input went): never "error", which reads as nothing done
                status = "unverified"
            out = {"status": status, "why": res.detail, "window": window}
            if res.performed is False:
                # the driver knows nothing was sent: a refusal before acting (stale, unsupported, waited out for the
                # user, the deadline). A harness that does not know user_busy takes it as an error, as before
                out["performed"] = False
            # any action can open a window (a "new document" button in an open panel, cmd+N, a browser's pop-up). Two
            # things are told apart (_created): which windows the driver may use and follow (the action's: absent before
            # in any state, the acted app's, no input of the user's meanwhile), and which one the task made -- new_window,
            # credited to the task upstream so steps in it ask no card -- which needs positive proof that it holds nothing
            # of the user's; a window that appeared and is not claimed is told apart, with why
            usable, made, appeared, wins = self._created(bundle, before, since)
            for w in usable:
                self._own(d, self._split(w["id"])[1])
            if made is not None:
                out["new_window"] = made
            if appeared:
                out["appeared"] = appeared
            # the files receipt: what the driver knows it wrote, plus the window's own document when the app itself says
            # it saved it during this action -- unsaved changes before (modified true), none after (false). A document
            # that merely changed on disk is not credited: a sync program may have written it at that moment (PR #16
            # review, eighth round). Read from the window, so no app is named here; the layer above credits a screen
            # action's files by this alone and checks each path's version itself.
            state1 = self._document(window, wins) if may_save else None
            doc1 = state1[0] if state1 else None
            files = list(res.files)
            if doc1 and state0 and state0[1] is True and state1[1] is False and self._reportable(doc1):
                # the digest of what the app holds (its document's text), never of the file on disk: a file another
                # program rewrote after the app saved then fails the match upstream and stays unconfirmed. No text
                # (an app that does not answer, a document that is not text): the path alone, told but not credited.
                files.append((doc1, self._content_digest(window, wins)))
            # `files: []` is a receipt too: this action could not have written a file (typing, scrolling). Around an
            # action that could, the field is there only when a write is known; left out, the driver does not know
            # (an app's own save panel in an app that says nothing of its document), and the layer above credits nothing.
            if files:
                seen: dict[str, str | None] = {}
                for path, digest in files:
                    seen[path] = seen.get(path) or digest
                out["files"] = [{"path": p, **({"sha256": seen[p]} if seen[p] else {})} for p in sorted(seen)]
            elif not may_save:
                out["files"] = []
            if doc1:
                out["document"] = doc1
            if not self._input_by_source():
                # The input this action itself sent is not the user's (PR #48, 10-19, before events could be told by
                # their source). Where they can -- the tap watches keys, and the driver's own events are skipped by
                # their source process (InputWatch.posted_by_us) -- the mark is not moved: moved, it wiped the user's
                # own edit of the task's window during the action, and the next step was decided on a view it did not
                # match (hands#62 re-review). Without the tap (the HID idle time) or without keys (a mouse-only tap
                # counts key presses by the idle time), ours and theirs cannot be told apart: moved as before.
                self._epoch_at = self._mark_now()
        except Exception as e:  # noqa: BLE001
            # no mark is moved, as main moved none when a read here threw
            gone = res.performed is False
            return {"status": "error" if gone else "unverified", "window": window,
                    "why": f"{res.detail}; reading the windows after the action failed ({type(e).__name__}: {e})"
                           + ("" if gone else " -- it may have taken effect; observe before doing it again"),
                    **({"performed": False} if gone else {})}
        # where main's mark went: a key counted only as main counted it, recorded before this, is wiped as on main
        self._action_end_at = self._mark_now()
        return out

    def _too_late_to_start(self) -> dict | None:
        """The reply for a step that may no longer start for its caller: past its deadline less DEADLINE_MARGIN_S
        (base.act_by), the time kept for the action and the reads after it. None when it may start, or no caller set
        a deadline."""
        by = act_by(getattr(self, "_caller_deadline", None))
        if by is None or time.time() <= by:
            return None
        return {"status": "error", "performed": False,
                "why": "the caller's deadline came before the step could act; nothing was done"}

    def _doubt_after(self) -> float:
        return getattr(self, "_action_end_at", 0.0)

    def _input_by_source(self) -> bool:
        """Whether every input the epoch counts is told by its source, so the driver's own is already left out."""
        w = getattr(self, "_watch", None)
        return bool(w is not None and w.active and w.keys_trusted)

    def _document(self, window: str, wins: list[dict]) -> tuple[str | None, bool | None] | None:
        """(path, modified) of the document the window shows, asked of the app; None when the app does not say or the
        window is gone. `modified` is None when only the path is known (accessibility, not the app's scripting)."""
        info = next((w for w in wins if w["id"] == window), None)
        if info is None or not info.get("pid"):
            return None
        return window_document(int(info["pid"]), info.get("bundle") or "", info.get("title") or "", info.get("bounds"))

    def _content_digest(self, window: str, wins: list[dict]) -> str | None:
        """SHA-256 of the text the window's app holds for its document, as the app saves plain text (UTF-8, LF);
        None when the app does not say."""
        import hashlib
        info = next((w for w in wins if w["id"] == window), None)
        if info is None:
            return None
        text = document_text(info.get("bundle") or "", info.get("title") or "", info.get("bounds"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None

    def _reportable(self, path: str) -> bool:
        """A path that can be a task's file: it exists, and it is not a driver's scratch under the task folder's
        .hands/ (the staged 未命名-N a new document is opened from is the document's path until it is saved as)."""
        full = Path(os.path.realpath(path))
        scratch = Path(os.path.realpath(str(self.workspace))) / ".hands"
        if full == scratch or scratch in full.parents:
            return False
        return full.exists()

    def _created(self, bundle: str, before: set, since: float) -> tuple[list[dict], dict | None, list[dict], list[dict]]:
        """The windows this action brought up that the driver may use, the one it made if that can be proved, and the
        windows that appeared and are not claimed (each with why); with the windows on screen after the action.

        The action's (the driver may use and follow them: a browser's pop-up, cmd+N in the task's own window): on
        screen after it, absent before it in any state (_known_windows: a minimised, hidden or other-Space window the
        action brought back is the user's), of the app acted in, and no input of the user's reached an app the task
        operates since the list before was taken.

        Made by the task -- new_window -- when, besides, by structure only, never by an app's name:
        (a) the above: it appeared through this action alone (the epoch moved by the action, not the user);
        (b) it was never listed in this task before;
        (c) it holds no saved document and no content: the app says its document has no file (or only the driver's
            own empty staging file under the task folder's .hands/) and no unsaved changes, and the document's text,
            read from the app, is empty -- a user's file shows its file, a reply opened with the quoted mail has text;
        (d) it belongs to the app acted in (another app's window appearing meanwhile is not this action's: PR #48
            review, a TextEdit click credited a Safari window).
        Positive proof only: what cannot be read (no title, an app that does not answer, a text that cannot be read)
        is not claimed. Exactly one such window is claimed; two that both qualify are not told apart, and neither is."""
        wins = self._windows()
        fresh = [w for w in wins if w["id"] not in before and (w.get("bundle") or "") == bundle]
        if not fresh:
            return [], None, [], wins
        # input at the very instant the list was taken cannot be proved to come before it: counted (>=), so the
        # window is not claimed -- the safe side (and with a microsecond clock the two reads can be equal)
        if self._last_input_at() >= since:
            why = "the user's input reached an app the task operates while the action ran"
            return [], None, [{"id": w["id"], "why": why} for w in fresh], wins
        listed = self.__dict__.setdefault("_listed", set())
        made: list[dict] = []
        appeared: list[dict] = []
        for w in fresh:
            why = "listed earlier in this task" if w["id"] in listed else self._not_blank(w)
            if why:
                appeared.append({"id": w["id"], "why": why})
            else:
                made.append(w)
        if len(made) > 1:
            appeared += [{"id": w["id"], "why": "more than one window appeared that could be the one made"} for w in made]
            made = []
        if not made:
            return fresh, None, appeared, wins
        w = made[0]
        return (fresh, {"id": w["id"], "app": w["app"], "app_name": w["app"] or app_display_name(w.get("bundle") or ""),
                        "bundle": w.get("bundle") or "", "title": w["title"]}, appeared, wins)

    def _not_blank(self, w: dict) -> str | None:
        """Why the window cannot be proved to hold no saved document and no content; None when it is proved."""
        if not w.get("pid") or not (w.get("title") or "").strip():
            return "its title or its app could not be read"
        state = self._document(w["id"], [w])
        if state is None:
            return "its document could not be read"
        if state[0] and not self._staging(state[0]):
            return "it shows a file"
        if state[1] is True:
            return "its document has unsaved changes"
        text = self._text(w)
        if text is None:
            return "its content could not be read"
        if text.strip():
            return "it holds content"
        return None

    @staticmethod
    def _text(w: dict) -> str | None:
        """The text of the window's document as its app holds it; None when the app does not say."""
        return document_text(w.get("bundle") or "", w.get("title") or "", w.get("bounds"))

    def _staging(self, path: str) -> bool:
        """A path under the task folder's .hands/: the driver's own scratch (the empty file a new document is opened
        from), never a file of the user's."""
        full = Path(os.path.realpath(path))
        scratch = Path(os.path.realpath(str(self.workspace))) / ".hands"
        return scratch in full.parents

    def _open_window(self, app: str, url: str | None) -> dict:
        """Open a window of `app` (a bundle id, or a name as the snapshot shows it) and wait for it. No app is named
        in code: the name is resolved to a bundle id generically, and a new document is asked for the way any
        document-based app answers (its own scripting, by bundle id), then by the keyboard. The whole of it is a step
        for the input watch -- under way, so a key with no app target meanwhile counts as on main: `open -g -b` with
        a URL, `make new document` and the run's own browser window can each bring an app forward (hands#62 delta
        review at f0f7186: only the launch was marked)."""
        step = self._step_began((app or "").lower())
        try:
            return self._open_window_now(app, url)
        finally:
            self._step_ended(step)

    def _open_window_now(self, app: str, url: str | None) -> dict:
        bundle = resolve_app(app, running_apps(), installed_apps())
        if not bundle:
            return {"status": "unsupported", "why": f"no application called {app!r} is running or installed"}
        was_running = bundle in {b for _, b in running_apps()}
        launched: list[dict] = []
        if not was_running:
            # Not running: launched first, on its own. What it brings back then -- the last session's windows, as
            # Safari and TextEdit do -- is the user's, not a window this action made (PR #48 review, third round).
            # An app slow to start (a music player took longer than the 8 s this waited, 10-20) is waited for up to
            # LAUNCH_WAIT_S, until a window of it appears.
            before_launch = self._known_windows()
            front_before = self._front_bundle_id()
            # some apps take the front on their first launch: see below. The whole launch is marked (when the app
            # took the front is not known), and the mark is closed whatever the launch raised (hands#62 re-review)
            step = self._step_began(bundle, max_s=20 + self.LAUNCH_WAIT_S + 10)
            launched_at = time.time()
            # The input watch is told the app may have the front from the launch's start, before it is launched, and
            # until after the give-back below, whatever the launch raised: told only once the launch had found it in
            # front, a key whose callback ran during the launch found no flash, and with the front meanwhile in
            # another app it was put on that app for good (hands#62 review at af55263, the same late telling as the
            # driver's flash). When the app did not come forward the flash was not needed: a key in another app
            # then counts for this one too, which can only make a step stale, never miss the user's input to it.
            from .inputwatch import front_given_back, front_taken
            taken = front_taken(bundle, launched_at)
            try:
                # in the background (-g): opening an app is no reason to take the front from the user; it comes
                # forward only for a step, by the flash's rules (the user still, the step allowed) -- `open -b` did
                # take it, and a music app kept jumping in front of the user's work (rc.10, 10-21)
                subprocess.run(["open", "-g", "-b", bundle], capture_output=True, timeout=self.OPEN_CMD_S)
                launched = self._launch_windows(bundle, before_launch, self.LAUNCH_WAIT_S)
                if front_before and front_before != bundle and self._front_bundle_id() == bundle:
                    # some apps take the front on their first launch whatever -g says: given back at once
                    self._make_frontmost(front_before)
            finally:
                front_given_back(taken)
                self._step_ended(step)
            self._settled(bundle, self.SETTLED_S)
        d = self._driver(bundle)      # started now, so the windows it finds are the ones there before the task's
        before = self._known_windows()
        # A window this open's launch brought up is one the task opened (opened_window): the task may look at it,
        # move about in it and bring the app forward -- but what is in it stays the user's (a mailbox, a library, a
        # restored chat), so it is not new_window, which says the task made the window and its content (hands #54
        # review: "no document" could not tell a music player's main window from Mail's mailbox, and a read that
        # failed looked the same). Only proof that this action made a window -- the id the app's scripting returned
        # for a new document, the run's own browser window -- is new_window. Never by the app's name; a browser
        # (any app that opens https: links) is not handled this way, the run has its own browser window.
        # A launch window that shows a document (the last session's file, restored) is not even that: the task asked
        # for a window to work in, and a new document is asked for below.
        if launched and not url and not is_browser(bundle):
            wins = self._windows()
            shown = [w for w in launched if any(x["id"] == w["id"] for x in wins) and self._document(w["id"], wins) is None]
            if shown:
                w = shown[-1]
                allow_flash(bundle)              # the task opened the app: it may bring it forward for its steps
                return {"status": "ok", "window": w["id"],
                        "opened_window": {"id": w["id"], "app": w["app"], "app_name": w["app"] or app_display_name(w.get("bundle") or ""), "title": w["title"]},
                        "why": "the window the app showed when this opened it; what is in it is the user's"}
        # Whose the window is: the task's on proof that this action made it -- the window id the app's scripting
        # returned for the document it made, or a browser window the driver opened and told apart by its marker.
        # A window that merely appeared after the ask is the task's only when the app was running already (a
        # running app brings no session back); after a launch it may be the last session's, the user's: returned
        # to observe, not claimed (PR #48 review, fourth round: the restored #18000 was claimed when the ask failed).
        proof: str | None = None
        fresh: list[dict] = []
        if is_browser(bundle) and hasattr(d, "_own_browser_window"):
            # a browser, with an address or without: the run's own window (made, or the one it already has) -- never
            # a new document or cmd+N, which go to whatever browser window is in front, and never the user's window
            # (the driver opens it where the browser's scripting allows, and says why not where it does not)
            proof = d._own_browser_window(bundle, url or "")
            if proof and str(proof) in {self._split(b)[1] for b in before} and not url:
                return {"status": "ok", "window": f"{bundle}#{proof}", "why": "the task's own browser window, already open"}
            if proof and str(proof) in {self._split(b)[1] for b in before}:
                # the run's own browser window from earlier, given back as it is: the page asked for now has to be
                # loaded in it, and that checked, before it is answered (PR #48 review, fifth round: the second
                # address was never opened). Not a new window.
                window = f"{bundle}#{proof}"
                loaded = self._navigate(d, bundle, str(proof), url)
                if loaded:
                    return {"status": "ok", "window": window, "why": f"loaded {url} in the task's own browser window"}
                return {"status": "unverified", "window": window,
                        "why": f"the task's own browser window is there, but {url} could not be confirmed loaded in it"}
        elif url:
            subprocess.run(["open", "-g", "-b", bundle, url], capture_output=True, timeout=self.OPEN_CMD_S)
            fresh = self._fresh_windows(before, self.FRESH_S - 0.5, bundle)
        else:
            # the app's own scripting only: cmd+N goes to whichever app is in front, the user's included, and a
            # check that it is this app races with the user switching (hands #55 review)
            proof = self._new_document(bundle)
        if proof:
            fresh = [w for w in self._fresh_windows(set(), self.FRESH_S, bundle) if self._split(w["id"])[1] == str(proof)]
        if not fresh:
            return {"status": "unverified", "why": f"{app} ({bundle}) was asked to open a window; none appeared"}
        w = fresh[-1]
        owned = bool(proof) or (was_running and self._split(w["id"])[1] not in {self._split(b)[1] for b in before})
        if not owned:
            return {"status": "ok", "window": w["id"],
                    "why": f"a window of {app} is there, but it may be one the app brought back when it launched, not one this action made"}
        allow_flash(bundle)   # a window the task opened is the task's: the app may be brought forward for it
        self._own(d, self._split(w["id"])[1])
        return {"status": "ok", "window": w["id"], "new_window": {"id": w["id"], "app": w["app"], "app_name": w["app"] or app_display_name(w.get("bundle") or ""), "bundle": w.get("bundle") or "", "title": w["title"]}}

    def _navigate(self, d, bundle: str, wid: str, url: str) -> bool:
        """Load `url` in the run's own browser window `wid` (the window server's id) and check that the window's page
        is now that address. The window is addressed by the id the browser's own script knows it by, which the
        driver kept when it made the window (Safari's is the window server's; Chromium's is the browser's own, and
        a window server id sent to Chromium names some other window -- PR #48 review, sixth round); without that id
        nothing is sent. A file:// page in Safari is served over http by the driver, so the address expected back is
        the served one. False when it cannot be shown that the page went."""
        key = bundle.lower()
        script_id = str(getattr(d, "_own_browser_script", {}).get(key) or "")
        if not script_id:
            return False
        expected = url
        if key == "com.apple.safari" and hasattr(d, "_load_in_own_window"):
            if url.lower().startswith("file:") and hasattr(d, "_serve_page"):
                # served once, here; the loader is given the served address, so it does not serve the file again
                # (a second _serve_page restarts the page server on another port, and the address expected back is
                # then not the one that loaded -- PR #48 review, seventh round)
                served = d._serve_page(url)
                if not served or served.startswith("!"):
                    return False
                expected = url = served
            d._load_in_own_window(wid, url)
        else:
            u = _q(url)
            for form in (f'set URL of active tab of window id {int(script_id)} to "{u}"',
                         f'set URL of current tab of window id {int(script_id)} to "{u}"'):
                try:
                    p = subprocess.run(["osascript", "-e", f'tell application id "{_q(bundle)}" to {form}'], capture_output=True, text=True, timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if p.returncode == 0:
                    break
        want = expected.rstrip("/")
        for _ in range(12):
            now = self._page_of(bundle, script_id).rstrip("/")
            if now and (now == want or now.startswith(want + "/") or now.startswith(want + "?") or now.startswith(want + "#")):
                return True
            time.sleep(0.25)
        return False

    def _page_of(self, bundle: str, script_id: str) -> str:
        """The address the browser window shows now, by its scripting (the window by the id that script knows);
        "" when it does not say."""
        for form in (f"URL of current tab of window id {int(script_id)}", f"URL of active tab of window id {int(script_id)}"):
            try:
                p = subprocess.run(["osascript", "-e", f'tell application id "{_q(bundle)}" to get {form}'], capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                continue
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        return ""

    def _new_document(self, bundle: str) -> str | None:
        """Ask the app for a new document the way any scriptable document app answers (`make new document`), and
        return the window id it got -- the app's own word that this window was made now (NSWindow's number, which is
        its CGWindowID). None when the app does not answer, or does not say which window."""
        script = (f'tell application id "{_q(bundle)}"\nset d to make new document\n'
                  'repeat with w in windows\ntry\nif document of w is d then return id of w\nend try\nend repeat\n'
                  'try\nreturn id of window 1\nend try\nreturn ""\nend tell')
        try:
            p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=self.OPEN_CMD_S)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = (p.stdout or "").strip() if p.returncode == 0 else ""
        return out if out.isdigit() else None

    #: Actions after which a browser page may load (its title then changes).
    SETTLE_KINDS = {"click", "double_click", "key", "set_value", "type", "select"}
    #: The title must hold this long to count as settled, waited for at most SETTLE_MAX_S.
    SETTLE_STILL_S = 0.6
    SETTLE_MAX_S = 4.0

    def _settle_title(self, window: str) -> None:
        """Wait until the window's title has held for SETTLE_STILL_S (at most SETTLE_MAX_S), or the app's stop."""
        t0 = time.time()
        last, since = None, time.time()
        while time.time() - t0 < self.SETTLE_MAX_S and not STOP.is_set():
            title = next((w["title"] for w in self._windows() if w["id"] == window), None)
            if title != last:
                last, since = title, time.time()
            elif time.time() - since >= self.SETTLE_STILL_S:
                return
            time.sleep(0.15)

    #: How long a launched app is waited for to show its first window.
    LAUNCH_WAIT_S = 20.0
    #: The bound on one `open -g` or one `make new document` (osascript).
    OPEN_CMD_S = 20.0
    #: How long a just-launched app's windows are given to stop changing (_settled).
    SETTLED_S = 10.0
    #: How long a window asked for is looked for (_fresh_windows).
    FRESH_S = 3.0
    #: The longest an open_window takes, start to reply, along its real path: the launch (`open -g`, OPEN_CMD_S), its
    #: windows waited for (LAUNCH_WAIT_S) and settling (SETTLED_S), then the window asked of the app (`open -g` with
    #: the url, or `make new document`, OPEN_CMD_S) and looked for (FRESH_S): 73 s. It does not start with less left.
    #: (A browser's own window and its page check go through the browser's scripting, each call bounded; a browser
    #: that answers slowly to every one of them can take longer, and the caller then records it unverified.)
    OPEN_WINDOW_WORST_S = OPEN_CMD_S + LAUNCH_WAIT_S + SETTLED_S + OPEN_CMD_S + FRESH_S


    def _launch_windows(self, bundle: str, before: set, seconds: float) -> list[dict]:
        """The windows of `bundle` that appeared after its launch: waited for up to `seconds` until the first, then a
        moment more for others coming with it."""
        t0 = time.time()
        found: list[dict] = []
        while time.time() - t0 < seconds:
            time.sleep(0.3)
            found = [w for w in self._windows() if w["id"] not in before and w["bundle"] == bundle]
            if found:
                time.sleep(1.0)
                return [w for w in self._windows() if w["id"] not in before and w["bundle"] == bundle]
        return found

    def _settled(self, bundle: str, timeout: float = 10.0) -> None:
        """Wait for a just-launched app to be running and its windows to stop changing (two readings alike)."""
        last = None
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.5)
            if bundle not in {b for _, b in running_apps()}:
                continue
            now = self._known_windows()
            if now == last:
                return
            last = now

    def _fresh_windows(self, before: set, seconds: float, bundle: str) -> list[dict]:
        """New windows of `bundle` within `seconds`: only that app's, as the window asked for is its (PR #48 review)."""
        for _ in range(int(seconds / 0.3)):
            time.sleep(0.3)
            fresh = [w for w in self._windows() if w["id"] not in before and w["bundle"] == bundle]
            if fresh:
                return fresh
        return []

    def end(self) -> dict:
        self.wait_shots()
        for d in self.drivers.values():
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
        self.drivers = {}
        return {"closed": True}


#: The process's input watch: one tap and one front poller for every executor `begin` makes (hands#62 review at
#: e8fa010: each begin started another tap and another 50 Hz poller, and nothing stopped them).
_SHARED_WATCH: dict = {}
_SHARED_LOCK = threading.Lock()


def shared_watch() -> InputWatch | None:
    """The process's input watch, started the first time it can be; None when no tap can be made now. Only a watch
    that started is kept: one that could not (the permission not yet granted) is tried again by the next executor,
    as each run tried its own before (hands#62 review at f0f7186). Made under the lock, so two never start at once."""
    with _SHARED_LOCK:
        w = _SHARED_WATCH.get("watch")
        if w is not None and not w.healthy():
            # checked at every reuse against what holds now: Input Monitoring granted or revoked since it started
            # (either way the watch says the wrong thing of keys), the tap disabled, the front's poller dead -- made
            # anew, as main made a watch for each run (hands#62 reviews at 45e67fc and 714c989). Without keys now,
            # the new one is mouse-only and key presses count by the HID idle time, as on main.
            w.stop()
            _SHARED_WATCH.pop("watch", None)
            w = None
        if w is None:
            w = InputWatch()
            if not w.start():
                return None
            _SHARED_WATCH["watch"] = w
        return w


def main() -> None:
    """One request at a time, in order -- except `stop`, which is handled the moment it is read, on its own thread:
    a stop that came while a step was still waiting (for the vision model, for the user) was read only after that
    step had returned, and its click went ahead (hands #54 review)."""
    import queue
    import threading

    out = sys.stdout
    out_lock = threading.Lock()
    state: dict = {"ex": None}
    requests: "queue.Queue[dict | None]" = queue.Queue()

    def write(obj: dict) -> None:
        with out_lock:
            out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            out.flush()

    def read() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                write({"id": None, "ok": False, "error": "bad json"})
                continue
            if req.get("op") == "stop":
                ex = state["ex"]
                if ex is None:
                    # before begin has made the executor: kept, and applied as it is made (hands #54 review)
                    state["stop_pending"] = True
                    STOP.set()
                    write({"id": req.get("id"), "ok": True, "result": {"stopped": True, "pending": True}})
                else:
                    write({"id": req.get("id"), "ok": True, "result": ex.stop()})
                continue
            requests.put(req)
        requests.put(None)

    threading.Thread(target=read, name="requests", daemon=True).start()
    # A wait for the user inside a request is told as a JSON event line ({"event": "user_wait" | "user_resumed",
    # "req": the request's id, ...}), under the reply lock: the plain HANDS_WAIT line the CLI prints was not JSON and
    # harness dropped it. No "id" key: a harness that does not know events reads no reply into it.
    set_wait_sink(lambda kind, body: write({"event": kind, "req": state.get("rid"), **body}))
    while True:
        req = requests.get()
        if req is None:
            break
        rid, op = req.get("id"), req.get("op")
        state["rid"] = rid
        ex = state["ex"]
        # `deadline_ms` (Unix ms, optional): when the caller stops waiting for this reply. A request read too late
        # for it (queued behind one that took long) is not carried out: a perform past the deadline less the margin
        # for acting (base.act_by, as every step's own check), a look past the deadline itself.
        dl = req.get("deadline_ms")
        deadline = float(dl) / 1000 if isinstance(dl, (int, float)) and not isinstance(dl, bool) else None
        if op == "perform" and deadline is not None and time.time() > act_by(deadline):
            write({"id": rid, "ok": True, "result": {"status": "error", "performed": False,
                                                     "why": "read too late for the caller's deadline; nothing was done"}})
            continue
        if op == "observe" and deadline is not None and time.time() > deadline - OBSERVE_MARGIN_S:
            write({"id": rid, "ok": False, "error": "read after the caller's deadline; not looked at"})
            continue
        extra = {"deadline": deadline} if deadline is not None else {}
        try:
            if op == "begin":
                ex = state["ex"] = HandsExecutor(Path(req["scratch"]) if req.get("scratch") else None, Path(req["workspace"]) if req.get("workspace") else None)
                if state.get("stop_pending"):
                    ex.stop()
                res = ex.snapshot()
            elif ex is None:
                raise RuntimeError("begin first")
            elif op == "snapshot":
                res = ex.snapshot()
            elif op == "observe":
                res = ex.observe(req["window"], bool(req.get("screenshot")), **extra)
            elif op == "perform":
                res = (ex.perform(req["action"], req.get("shot") or None, bool(req.get("frames")), **extra)
                       if req.get("shot") or req.get("frames") or extra else ex.perform(req["action"]))
            elif op == "end":
                res = ex.end()
            else:
                raise RuntimeError(f"unknown op {op}")
            write({"id": rid, "ok": True, "result": res})
        except Exception as e:  # noqa: BLE001 -- every failure is reported to the caller, the process stays up
            write({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})
        state["rid"] = None
    set_wait_sink(None)


if __name__ == "__main__":
    main()

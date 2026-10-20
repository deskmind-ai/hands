"""The execution layer for harness 0.5: hands sees and moves, decides nothing.

    uv run --extra macos python -m deskmind_hands.executor

One JSON request per line on stdin, one JSON reply per line on stdout (`{"id", "ok", "result" | "error"}`):

    begin     {scratch?}                 start; the first inventory
    snapshot  {}                         the whole computer: locked, front app, every on-screen window of every app
    observe   {window, screenshot?}      one window close up: elements, dialog, text
    perform   {action}                   one generic action (click, set_value, type, select, key, scroll,
                                         open_window, close_window, focus_window, wait); its reply carries
                                         `files` ([{path, sha256?}]: what this action wrote, with the digest of the
                                         content written -- the driver's own bytes, or the text the app holds when
                                         it says it saved the window's document during the action; no digest when
                                         that is unknown) and `document` (the file the window shows afterwards)
    stop      {}                         the app's stop switch: every later perform is refused
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
from .axdocument import _q, document_text, window_document
from .grounding import STOP
from .drivers.base import ExecResult, Observation
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


_BROWSERS: set[str] | None = None


def is_browser(bundle: str) -> bool:
    """Whether the app is a web browser, by what it registers, not by a list of names: an app the system offers for
    opening https: links (NSWorkspace URLsForApplicationsToOpenURL). Read once per process."""
    global _BROWSERS
    if _BROWSERS is None:
        found: set[str] = set()
        try:
            from AppKit import NSBundle, NSURL, NSWorkspace
            for u in NSWorkspace.sharedWorkspace().URLsForApplicationsToOpenURL_(NSURL.URLWithString_("https://example.com/")) or []:
                b = NSBundle.bundleWithURL_(u)
                if b is not None and b.bundleIdentifier():
                    found.add(str(b.bundleIdentifier()).lower())
        except Exception:  # noqa: BLE001 -- not macOS: none known
            pass
        _BROWSERS = found
    return bundle.lower() in _BROWSERS


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
        if w is not None and w.active and w.keys:
            return time.time() - w.last_input_for(self._task_bundles()) < self.USER_ACTIVE_S
        if Quartz is None:
            return False
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType)
        return idle < self.USER_ACTIVE_S

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
            w = InputWatch()
            self._watch = w if w.start() else None
        if self._watch is not None and self._watch.active:
            at = self._watch.last_input_for(self._task_bundles())
            if self._watch.keys or Quartz is None:
                return at
            # a mouse-only tap: every key press counts, wherever it went (the safe side)
            idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGEventKeyDown)
            return max(at, time.time() - idle)
        if Quartz is None:
            return 0.0
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType)
        return time.time() - idle

    def _advance(self, reason: dict | None = None) -> None:
        self.epoch += 1
        self._epoch_at = time.time()
        #: why the epoch last moved, told in every snapshot: an action of the task's, the user's input to an app the
        #: task operates (which, or "unknown"), or a window list that changed (which app, what appeared, vanished or
        #: was renamed). A stale step upstream can then say what made it stale (rc.9, 10-20).
        self._epoch_reason = {"epoch": self.epoch, "at": round(self._epoch_at, 3), **(reason or {"by": "action"})}

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
        inventory: dict[str, tuple] = {}
        for w in wins:
            b = (w.get("bundle") or "").lower()
            if b in task:
                inventory.setdefault(b, [])
                inventory[b].append((w["id"], w["title"]))
        inventory = {b: tuple(sorted(v)) for b, v in inventory.items()}
        before = self._inventory or {}
        changed = [b for b in before if b in task and inventory.get(b, ()) != before[b]]
        input_at = self._last_input_at()
        if input_at > self._epoch_at or changed:
            if input_at > self._epoch_at:
                where = (self._watch.last_input_where(task)[1] if self._watch is not None and self._watch.active
                         and self._watch.last_input_for(task) >= input_at else "any (no per-app watch)")
                reason = {"by": "input", "to": where, "input_at": round(input_at, 3)}
            else:
                b = changed[0]
                was, now = dict(before[b]), dict(inventory.get(b, ()))
                reason = {"by": "windows", "app": b,
                          "appeared": sorted(set(now) - set(was))[:5], "vanished": sorted(set(was) - set(now))[:5],
                          "renamed": [{"id": i, "from": was[i], "to": now[i]} for i in sorted(set(was) & set(now)) if was[i] != now[i]][:5]}
            self._advance(reason)
        self._inventory = {b: inventory.get(b, ()) for b in task}
        return {"epoch": self.epoch, "locked": self.locked(), "front_app": front, "user_active": self.user_active(),
                "input_watch": ("keys" if self._watch.keys else "mouse") if self._watch is not None and self._watch.active else "",
                **({"epoch_reason": self._epoch_reason} if getattr(self, "_epoch_reason", None) else {}),
                # the apps whose steps must bring them to the front (read from their pixels, or a step that could not
                # be delivered in the background): so the plan can say so once, before the run takes the front.
                "needs_foreground": self._needs_foreground(),
                # app_name: what the user calls the app (its localized name), for cards; app stays as it was
                "windows": [{"id": w["id"], "app": w["app"], "app_name": w["app"] or w.get("bundle") or "", "bundle": w.get("bundle") or "",
                             "title": w["title"], "focused": w["app"] == front} for w in wins]}

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
        key = bundle.lower()
        # the first look at an app: its windows *before* the read are the baseline the next snapshots compare
        # against -- taken after, a page that navigated while the driver read it would be the baseline, and the
        # observation of the page before would pass (PR #49 review); widening the watch is still no change
        baseline = (tuple(sorted((w["id"], w["title"]) for w in self._windows() if (w.get("bundle") or "").lower() == key))
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
        if baseline is not None and self._inventory is not None and key not in self._inventory:
            self._inventory[key] = baseline
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
        out = {"epoch": self.epoch, "window": {"id": window, "app": bundle, "app_name": app_display_name(bundle), "title": obs.window_title},
               "modal": {"title": "dialog"} if obs.dialog else None, "elements": els, "text": "\n".join(texts)[:4000]}
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

    def perform(self, action: dict) -> dict:
        if getattr(self, "stopped", False):
            return {"status": "error", "why": "stopped by the app"}
        kind = action.get("kind")
        self._advance({"by": "action", "kind": kind or "", "window": action.get("window") or ""})
        if kind == "wait":
            time.sleep(min(float(action.get("ms") or 0) / 1000, 30))
            return {"status": "ok", "files": []}
        if kind == "open_window":
            return self._open_window(action.get("app") or "", action.get("url"))
        window = action.get("window") or ""
        bundle, wid = self._split(window)
        if kind == "close_window":
            data = self._pk(["window", "close", "--window-id", wid, "--json"])
            if data is None:
                return {"status": "error", "why": f"the window {window} could not be closed (it may be gone already)", "files": []}
            return {"status": "ok", "files": []}
        if kind == "focus_window":
            if self._pk(["window", "focus", "--window-id", wid, "--json"]) is None:
                return {"status": "error", "why": f"the window {window} could not be brought forward", "window": window, "files": []}
            return {"status": "ok", "window": window, "files": []}
        d = self._driver(bundle)
        obs = self.last_obs.get(window)
        if obs is None:
            return {"status": "error", "why": "observe the window before acting in it"}
        if self.current.get(bundle) != window:
            # the driver's snapshot and element ids are another window's of this app
            return {"status": "error", "why": f"{window} is not the window last observed in this app "
                                              f"({self.current.get(bundle)}); observe it again before acting in it"}
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
                return {"status": "unsupported", "why": f"unknown action {kind}"}
        except Exception as e:  # noqa: BLE001 -- an action the model wrote wrong is its mistake to read
            return {"status": "error", "why": str(e)}
        wins = self._windows()
        if kind in ("set_value", "type", "select") and action.get("target") and hasattr(d, "allow_browser_field"):
            # the layer above judged this step (its judge, or the person); the driver's own guard against writing
            # into a browser's address bar or search field takes that as the one-step allowance it asks for
            d.allow_browser_field(action.get("target"))
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
        try:
            if getattr(self, "stopped", False):     # read while this step was getting ready: nothing is sent
                return {"status": "error", "why": "stopped by the app"}
            res: ExecResult = d.execute(a)
        finally:
            if action.get("foreground_ok"):
                if flash_before is None:
                    os.environ.pop("HANDS_FLASH_APPS", None)
                else:
                    os.environ["HANDS_FLASH_APPS"] = flash_before
        status = "ok" if res.ok else "unsupported" if res.unsupported else "error"
        if res.ok and res.indeterminate:
            status = "unverified"
        out = {"status": status, "why": res.detail, "window": window}
        # any action can open a window (a "new document" button, a sheet that becomes a window): reported so the
        # ledger upstream can credit it to the task, as it does for open_window
        wins = self._windows()
        # ... a window of the acted app only, and one that did not exist before (a minimised window brought back is
        # not new): another app's window appearing meanwhile (the user's, a notification) is not this action's
        # (PR #48 review: a TextEdit click credited a Safari window; a restored window passed for made)
        fresh = [w for w in wins if w["id"] not in before and w["bundle"] == bundle]
        if fresh:
            w = fresh[-1]
            out["new_window"] = {"id": w["id"], "app": w["app"], "title": w["title"]}
            self._own(d, self._split(w["id"])[1])
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
        self._epoch_at = time.time()     # the input this action itself sent is not the user's
        return out

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

    def _open_window(self, app: str, url: str | None) -> dict:
        """Open a window of `app` (a bundle id, or a name as the snapshot shows it) and wait for it. No app is named
        in code: the name is resolved to a bundle id generically, and a new document is asked for the way any
        document-based app answers (its own scripting, by bundle id), then by the keyboard."""
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
            subprocess.run(["open", "-b", bundle], capture_output=True, timeout=20)
            launched = self._launch_windows(bundle, before_launch, self.LAUNCH_WAIT_S)
            self._settled(bundle)
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
        if url and is_browser(bundle) and bundle.lower() in getattr(d, "BROWSERS", set()) and hasattr(d, "_own_browser_window"):
            proof = d._own_browser_window(bundle, url)
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
            subprocess.run(["open", "-b", bundle, url], capture_output=True, timeout=20)
            fresh = self._fresh_windows(before, 2.5, bundle)
        else:
            proof = self._new_document(bundle)
            if not proof:
                subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "n" using command down'], capture_output=True, timeout=20)
                fresh = self._fresh_windows(before, 3, bundle)
        if proof:
            fresh = [w for w in self._fresh_windows(set(), 3, bundle) if self._split(w["id"])[1] == str(proof)]
        if not fresh:
            return {"status": "unverified", "why": f"{app} ({bundle}) was asked to open a window; none appeared"}
        w = fresh[-1]
        owned = bool(proof) or (was_running and self._split(w["id"])[1] not in {self._split(b)[1] for b in before})
        if not owned:
            return {"status": "ok", "window": w["id"],
                    "why": f"a window of {app} is there, but it may be one the app brought back when it launched, not one this action made"}
        allow_flash(bundle)   # a window the task opened is the task's: the app may be brought forward for it
        self._own(d, self._split(w["id"])[1])
        return {"status": "ok", "window": w["id"], "new_window": {"id": w["id"], "app": w["app"], "app_name": w["app"] or app_display_name(w.get("bundle") or ""), "title": w["title"]}}

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
        script = (f'tell application id "{_q(bundle)}"\nactivate\nset d to make new document\n'
                  'repeat with w in windows\ntry\nif document of w is d then return id of w\nend try\nend repeat\n'
                  'try\nreturn id of window 1\nend try\nreturn ""\nend tell')
        try:
            p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = (p.stdout or "").strip() if p.returncode == 0 else ""
        return out if out.isdigit() else None

    #: How long a launched app is waited for to show its first window.
    LAUNCH_WAIT_S = 20.0

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
        for d in self.drivers.values():
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
        self.drivers = {}
        return {"closed": True}


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
                write({"id": req.get("id"), "ok": ex is not None, **({"result": ex.stop()} if ex is not None else {"error": "begin first"})})
                continue
            requests.put(req)
        requests.put(None)

    threading.Thread(target=read, name="requests", daemon=True).start()
    while True:
        req = requests.get()
        if req is None:
            break
        rid, op = req.get("id"), req.get("op")
        ex = state["ex"]
        try:
            if op == "begin":
                ex = state["ex"] = HandsExecutor(Path(req["scratch"]) if req.get("scratch") else None, Path(req["workspace"]) if req.get("workspace") else None)
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
            write({"id": rid, "ok": True, "result": res})
        except Exception as e:  # noqa: BLE001 -- every failure is reported to the caller, the process stays up
            write({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()

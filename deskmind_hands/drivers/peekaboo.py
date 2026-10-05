"""Host-desktop driver via the Peekaboo CLI.

Requires upstream Peekaboo 4.3.1 or later (with the MCP `see` fix); verified on macOS 27.0 with a zh-Hans system
locale.
Three things surfaced on the real desktop that a mock can never teach, and each
one silently breaks clicking if you get it wrong:

1. **Target apps by bundle id.** On a Chinese-locale system ``--app Finder``
   fails while ``--app com.apple.finder`` and ``--app 访达`` both work. Localised
   display names are not a stable selector, and English ones are not either.

2. **Element bounds and the screenshot use different frames.** ``ui_elements``
   are app-scoped and expressed in *global display points*; the image is a
   window crop at ``delivered_image_size``. Elements legitimately sit outside the
   captured rect. Treating a bound as an image pixel puts every click in the
   wrong place, and the run just looks like weak grounding.

3. **The scale is not a round number and must not be assumed.** One real capture
   delivered 460x218 for an 820x374 logical window -- neither 1x nor 2x, and not
   even the same ratio on both axes. ``coordinate_context`` carries the real
   numbers; we derive the transform from it every single observation.

Peekaboo's ``snapshot_id`` is the same idea as this harness's observation
binding, so actions pin to it rather than to coordinates wherever possible.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from ..actions import Action, ActionKind, Binding
from ..geometry import (ImageTransform, Point, Rect, ScreenGeometry, Size,
                        clamp_to_screen, to_logical)
from ..apps import APPS
from ..grounding import VOCAB, name_unnamed
from ..vision import GENERIC_CONTROLS, VISION_APPS, ground_one, vision_elements
from .base import (USER_BUSY, Driver, DriverUnavailable, Element, ExecResult, Observation, effect_notes,
                   WindowRef)

#: Verified against `peekaboo help <cmd>` for 4.3.1. Collected here so a CLI
#: change is one edit rather than an archaeology exercise.
#: Capture in this process instead of through Peekaboo's Bridge daemon. The daemon that `see` auto-starts is not
#: recognised as a proven ScreenCaptureKit owner (a debug build has no code-signature identity to prove), so every
#: capture was refused with CAPTURE_FAILED / "ownership support cannot be proven". Caller-local ownership is also
#: what a harness wants: it refuses on contention instead of quietly sharing the lane with the menu-bar app.
CAPTURE = ["--capture-engine", "modern"]

#: Bundle ids for the app names staging commands use.
STAGE_APP_BUNDLES = {"TextEdit": "com.apple.TextEdit", "Safari": "com.apple.Safari", "Finder": "com.apple.finder",
                     "Notes": "com.apple.Notes", "Preview": "com.apple.Preview"}

#: Commands the live MCP session carries when the driver runs with transport="mcp". Everything else is
#: already background-safe through the CLI (window lists, dialogs) or has no snapshot to lose.
#: The projection layer (synthetic "move to" dropdowns, new-folder name + create, save button) can be switched off
#: with HANDS_PROJECTION=0, to measure a planner on the raw accessibility tree: whether desktop needs new verbs or
#: only a new state shape is exactly the on/off difference.
PROJECTION = os.environ.get("HANDS_PROJECTION", "1") != "0"

MCP_ROUTED = {"see", "see_window", "click_on", "click_on_fg"}



#: Commands that reject --no-remote. None do, as it turns out -- and leaving `permissions` out of the flag was
#: enough to auto-start a Bridge daemon on driver construction, which then refused every capture in a run that was
#: already in flight.
NO_REMOTE_UNSUPPORTED: set[str] = set()

#: Apps whose accessibility tree lives deep inside a web view (see PeekabooDriver.start).
DEEP_AX_APPS = APPS.deep_ax
#: The one chat app hands may act in, and the one conversation there it may see (the apps file's `chat:`).
CHAT_APP = APPS.chat_bundle
CHAT_CONVERSATION = APPS.chat_conversation
CHAT_PLACEHOLDERS = APPS.chat_placeholders
DEEP_AX_ENV = {"PEEKABOO_AX_MAX_DEPTH": "60", "PEEKABOO_AX_MAX_ELEMENTS": "1500", "PEEKABOO_AX_MAX_CHILDREN": "400"}

CMD = {
    "see":         ["see", "--app", "{app}", "--json", "--path", "{path}", "--format", "png", *CAPTURE],
    "see_window":  ["see", "--app", "{app}", "--window-id", "{window}", "--json",
                    "--path", "{path}", "--format", "png", *CAPTURE],
    "window_list": ["window", "list", "--app", "{app}", "--json"],
    "window_list_of": ["window", "list", "--app", "{app}", "--json"],
    "permissions": ["permissions", "status", "--json"],
    "activate":    ["app", "launch", "--bundle-id", "{app}", "--wait-ready",
                    "--foreground", "--json"],
    "activate_by_name": ["app", "launch", "{app}", "--wait-ready", "--foreground", "--json"],
    "click_on":    ["click", "--on", "{element}", "--snapshot", "{snapshot}", "--json"],
    "click_on_fg": ["click", "--on", "{element}", "--snapshot", "{snapshot}",
                    "--foreground", "--json"],
    "click_at":    ["click", "--at", "{x},{y}", "--snapshot", "{snapshot}",
                    "--app", "{app}", "--json"],
    # Screen-absolute, foreground, no snapshot: the only click Peekaboo will dispatch when no Bridge host can
    # prove ScreenCaptureKit ownership (an ad-hoc signed debug build cannot).
    "click_at_fg": ["click", "--at", "{x},{y}", "--foreground", "--json"],
    "type":        ["type", "{text}", "--snapshot", "{snapshot}", "--json"],
    "type_clear":  ["type", "{text}", "--snapshot", "{snapshot}", "--clear", "--json"],
    "type_fg":     ["type", "{text}", "--foreground", "--json"],
    "paste_fg":    ["paste", "{text}", "--foreground", "--json"],
    # Writes an accessibility value with no keystrokes at all. Legitimate for
    # fixture setup and effect oracles; NOT an agent action -- using it in a
    # graded GUI run would be bypassing the interface the run claims to measure.
    "set_value":   ["set-value", "{text}", "--on", "{element}", "--snapshot", "{snapshot}", "--json"],
    "press":       ["press", "{chord}", "--app", "{app}", "--window-id", "{window}", "--json"],
    "press_fg":    ["press", "{chord}", "--app", "{app}", "--foreground", "--json"],
    "press_fg_window": ["press", "{chord}", "--app", "{app}", "--window-id", "{window}",
                        "--foreground", "--json"],
    "window_focus": ["window", "focus", "--window-id", "{window}", "--json"],
    # Scoped to one app on purpose: the global form is an Escape sent to whatever happens to be frontmost, which
    # on a shared desktop is the user's work.
    "dialog_dismiss": ["dialog", "dismiss", "--app", "{app}", "--json"],
    "dialog_click":   ["dialog", "click", "--button", "{button}", "--app", "{app}", "--json"],
    # Menu paths are the only route to destinations with no on-screen control
    # and no locale-stable chord. Background by default: --app targets this
    # app's menu bar without stealing the foreground.
    "menu_click":  ["menu", "click", "--app", "{app}", "--path", "{path}", "--json"],
    "menu_list":   ["menu", "list", "--app", "{app}", "--json"],
    "window_close": ["window", "close", "--window-id", "{window}", "--json"],
    "window_resize": ["window", "resize", "--window-id", "{window}",
                      "--width", "{w}", "--height", "{h}", "--json"],
    # Background scroll must name an accessibility-scrollable element; only the
    # foreground variant may scroll "wherever the pointer is".
    "scroll_on":   ["scroll", "--direction", "{direction}", "--amount", "{amount}",
                    "--on", "{element}", "--snapshot", "{snapshot}", "--json"],
    "scroll_fg":   ["scroll", "--direction", "{direction}", "--amount", "{amount}",
                    "--app", "{app}", "--foreground", "--json"],
}

#: Peekaboo already encodes "dispatched but unconfirmed" as `success: false`
#: ("Typing did not return an accepted outcome."), so there is no separate
#: confirmation field to look for -- an earlier version of this driver invented
#: three and would have failed every successful type. Trust `success`; record
#: these as the evidence of what actually landed.
EVIDENCE_KEYS = ("typedText", "literalCharactersTyped", "deliveryMode",
                 "clickedElement", "targetWindowID")


def _app_running(process_name: str) -> bool:
    """Whether an app is running, asked without AppleScript (which launches the app it names)."""
    try:
        return subprocess.run(["/usr/bin/pgrep", "-x", process_name], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class _ascii_input_source:
    """For the brief foreground: an ASCII keyboard layout while keys are sent, the user's input source afterwards.
    With a Chinese input method in Chinese mode (the user had been typing in another app), Return went to the input
    method, and a web page's search box held the pasted query without ever searching it.

    The switch is made in a child process that holds it until restore(). Asking Text Input Sources anything registers
    the asking process with LaunchServices as an app, and from then on every process it starts (peekaboo, osascript)
    adds a Dock tile for the app that started the run (iTerm, from a terminal) that macOS never removes: about ten a
    gym episode. A child that asks and exits leaves none, and this process stays unregistered."""

    _CHILD = (
        "import ctypes, ctypes.util, sys\n"
        "c = ctypes.cdll.LoadLibrary(ctypes.util.find_library('Carbon'))\n"
        "c.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p\n"
        "c.TISCopyCurrentASCIICapableKeyboardInputSource.restype = ctypes.c_void_p\n"
        "c.TISSelectInputSource.argtypes = [ctypes.c_void_p]\n"
        "prev = c.TISCopyCurrentKeyboardInputSource()\n"
        "c.TISSelectInputSource(c.TISCopyCurrentASCIICapableKeyboardInputSource())\n"
        "print('switched', flush=True)\n"
        "sys.stdin.read()\n"
        "if prev: c.TISSelectInputSource(prev)\n"
    )

    def __init__(self) -> None:
        self._proc = None
        try:
            self._proc = subprocess.Popen([sys.executable, "-c", self._CHILD], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            ready = [False]

            def wait() -> None:
                ready[0] = self._proc.stdout.readline().strip() == "switched"   # type: ignore[union-attr]

            import threading
            t = threading.Thread(target=wait, daemon=True)
            t.start()
            t.join(2.0)   # keys go out regardless; at worst they meet the user's input method, as before the switch
        except (OSError, ValueError):
            self._proc = None

    def restore(self) -> None:
        if not self._proc:
            return
        try:
            self._proc.stdin.close()   # type: ignore[union-attr]
            self._proc.wait(timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            self._proc.kill()


class _MCPSession:
    """One long-lived `peekaboo mcp serve` process, so a snapshot outlives the call that minted it.

    Every CLI invocation is its own process, and without a Bridge host that can prove ScreenCaptureKit ownership
    (an ad-hoc signed build cannot) a snapshot dies with the process that took it. That is what forced this driver
    into the foreground: element-addressed clicks failed with SNAPSHOT_NOT_FOUND and fell back to screen
    coordinates, typing went through the keyboard. Inside one session the same snapshot is still alive for the
    next call, so observe -> click -> set_value all stay in the background.
    """

    def __init__(self, binary: str, timeout_s: float, env: dict | None = None) -> None:
        import queue
        import threading
        self.timeout_s = timeout_s
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._id = 0
        self.proc = subprocess.Popen([binary, "mcp", "serve", "--no-remote"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                     env={**os.environ, **(env or {})})

        def pump() -> None:
            for line in self.proc.stdout:           # type: ignore[union-attr]
                try:
                    self._q.put(json.loads(line))
                except ValueError:
                    continue
        threading.Thread(target=pump, daemon=True).start()
        self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "hands", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        # What each tool accepts: newer Peekaboo takes options an older build rejects outright ("Unknown
        # parameter"), so optional ones are sent only where the schema lists them.
        listed = self._request("tools/list", {}).get("result", {}).get("tools", [])
        self.params = {t.get("name"): set(((t.get("inputSchema") or {}).get("properties") or {}))
                       for t in listed}

    def accepts(self, tool: str, param: str) -> bool:
        return param in self.params.get(tool, set())

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")   # type: ignore[union-attr]
        self.proc.stdin.flush()                          # type: ignore[union-attr]

    def _request(self, method: str, params: dict) -> dict:
        import queue
        self._id += 1
        want = self._id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return {"error": {"message": f"timed out after {self.timeout_s}s"}}
            try:
                msg = self._q.get(timeout=left)
            except queue.Empty:
                continue
            if msg.get("id") == want:
                return msg

    def tool(self, name: str, arguments: dict) -> tuple[bool, dict, str, str]:
        """(ok, _meta, text, error detail) for one tool call."""
        r = self._request("tools/call", {"name": name, "arguments": {k: v for k, v in arguments.items()
                                                                      if v is not None}})
        if "error" in r:
            return False, {}, "", str(r["error"].get("message", r["error"]))[:400]
        res = r.get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
        meta = res.get("_meta") or {}
        if res.get("isError"):
            return False, meta, text, text[:400] or "tool error"
        return True, meta, text, ""

    def close(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


#: What the planner is told when Peekaboo cut the accessibility tree short (its element, depth or children limit, or
#: its traversal deadline for an app that answers slowly): the list is not the whole window.
TRUNCATED_NOTE = ("Only part of this window could be read (its accessibility tree was cut short), so some controls "
                  "or text may be missing from the list; scroll, or look at a smaller part of the window.")


def ax_truncation_note(data: dict) -> str | None:
    """The note for an observation whose tree Peekaboo truncated: from the CLI's `truncation` object, or the warning
    line the MCP `see` puts in its text (kept as `truncation_warning`). None for a whole tree."""
    t = data.get("truncation") or {}
    if isinstance(t, dict) and any(v is True for k, v in t.items() if k.endswith("_reached")):
        return TRUNCATED_NOTE
    if data.get("truncation_warning"):
        return TRUNCATED_NOTE
    return None


def _mcp_see_as_cli_data(meta: dict, text: str) -> dict:
    """The MCP `see` result reshaped into what the CLI's `see --json` returns, so parsing is shared."""
    cc = dict(meta.get("coordinate_context") or {})
    lb = cc.get("logical_bounds")
    if isinstance(lb, dict):
        cc["logical_bounds"] = [[lb.get("x", 0), lb.get("y", 0)],
                                [lb.get("x", 0) + lb.get("width", 0), lb.get("y", 0) + lb.get("height", 0)]]
    di = cc.get("delivered_image_size")
    if isinstance(di, dict):
        cc["delivered_image_size"] = [di.get("width", 0), di.get("height", 0)]
    elements = []
    for e in meta.get("ui_elements") or []:
        e = dict(e)
        ax = e.get("role") or ""
        if ax.startswith("AX"):                 # CLI JSON says role=textField, ax_role=AXTextField
            e["ax_role"] = ax
            e["role"] = ax[2:3].lower() + ax[3:]
        elements.append(e)
    app = re.search(r"^Application: (.+)$", text, re.M)
    # The coordinate context leaves the title out for Finder windows; the summary line still has it. It is the
    # "where am I" of a file manager, and the scripting channel resolves the current folder from it -- without it
    # a move went to the workspace root instead of the folder the window showed.
    win = re.search(r"^(?:\[win\] )?Window: (.+)$", text, re.M)
    return {"snapshot_id": meta.get("snapshot_id"), "coordinate_context": cc, "ui_elements": elements,
            "application_name": app.group(1).strip() if app else "",
            "window_title": (cc.get("window") or {}).get("title") or (win.group(1).strip() if win else ""),
            "is_dialog": "dialog" in (text[:400].lower()),
            "truncation_warning": next((ln.strip() for ln in text.splitlines()
                                        if re.search(r"AX tree truncated|traversal was truncated", ln)), "")}



_UNREADABLE = ("window {target} cannot be read right now (its accessibility tree stays incomplete); still observing "
               "the current window -- continue here, or switch to it later")

def _bad_file_name(name: str) -> str | None:
    """Why a name cannot be a file name, or None. A rename is also where a planner that picked the wrong value
    shows it: in a three-file save task a planner renamed 临时2.txt to the three lines of the documents' contents, newlines included, and Finder
    took it."""
    if "\n" in name or "\r" in name:
        return (f"{name[:40]!r} has line breaks and is not a file name -- that looks like a document's text; "
                f"choose the name the goal gives")
    if "/" in name or ":" in name:
        return f"{name!r} contains '/' or ':', which a file name cannot"
    return None

#: Opt-in (HANDS_NAME_ROWS=1): a table row or cell the platform names only by its role ("row", "cell" -- web tables
#: and many list views) is named by the text inside it, so the planner can tell the rows apart and act on one. The
#: gym's pages list songs this way; off by default until a diag run shows what it changes on the desktop suite.

_ROLE_NAMES = {"row", "cell", "行", "单元格", ""}


def _name_rows(els: list) -> None:
    texts = [e for e in els if (e.role or "").lower() in ("statictext", "text") and e.label and e.rect]
    for e in els:
        if (e.role or "").lower() not in ("row", "cell", "axrow", "axcell") or not e.rect:
            continue
        if (e.label or "").strip().lower() not in _ROLE_NAMES:
            continue
        r = e.rect
        inside = [t for t in texts
                  if r.x <= t.rect.x + t.rect.w / 2 <= r.x + r.w and r.y <= t.rect.y + t.rect.h / 2 <= r.y + r.h]
        inside.sort(key=lambda t: (round(t.rect.y / 8), t.rect.x))
        if inside:
            e.label = " · ".join(t.label for t in inside)


def parse_textedit_windows(out: str, root: str) -> set[tuple[str, int, int]]:
    """TextEdit's "name<TAB>path<TAB>left<TAB>top" lines -> (name, left, top) of the windows whose document is
    under root (see PeekabooDriver._open_workspace_documents)."""
    wins = set()
    root = os.path.realpath(root)   # /var and /private/var are one folder
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and os.path.realpath(parts[1]).startswith(root.rstrip(os.sep) + os.sep):
            try:
                wins.add((parts[0].strip(), int(float(parts[2])), int(float(parts[3]))))
            except ValueError:
                continue
    return wins


def window_key(w: dict) -> tuple[str, int, int]:
    """A window as (title, left, top), comparable with parse_textedit_windows'."""
    b = w.get("bounds") or {}
    return ((w.get("window_title") or "").strip(), int(b.get("x", -1)), int(b.get("y", -1)))


def fresh_windows(now: set[str], seen: set[str], pinned: str | None) -> list[str]:
    """Windows that appeared since the last good observation, other than the pinned one (the newest last)."""
    return sorted(now - seen - {pinned})


def new_window_note(before: list[dict], after: list[dict]) -> str | None:
    """A line saying what a step opened in its app: a new window by its title, or a sheet (a new untitled window
    inside one that was there). None when nothing new appeared."""
    known = {w["id"] for w in before}
    notes = []
    for w in after:
        if w["id"] in known:
            continue
        b = w.get("bounds") or {}
        inside = any(_contains(o.get("bounds") or {}, b) for o in before)
        if not w.get("title") and inside:
            notes.append("a sheet appeared")
        else:
            notes.append(f"opened a new window {w['title']!r}" if w.get("title") else "opened a new window")
    return "; ".join(dict.fromkeys(notes[:3])) or None


def _contains(outer: dict, inner: dict) -> bool:
    try:
        return (outer["X"] <= inner["X"] and outer["Y"] <= inner["Y"]
                and inner["X"] + inner["Width"] <= outer["X"] + outer["Width"]
                and inner["Y"] + inner["Height"] <= outer["Y"] + outer["Height"])
    except (KeyError, TypeError):
        return False




#: A goal that adds to a document, and one that changes or removes what is in it.
_ADDS = re.compile(r"\b(?:add|append|insert|put|copy|write|record|log)\b|追加|添加|加入|加到|写入|写进|插入|补上|记下", re.I)
_CHANGES = re.compile(r"\b(?:change|replace|rewrite|edit|update|correct|fix|remove|delete|clear|empty|overwrite|"
                      r"sort|reorder|rename|keep only)\b|修改|改成|改为|替换|更新|删除|删掉|清空|覆盖|重写|排序|只保留",
                      re.I)


def lines_lost(goal: str, current: str, new: str) -> list[str]:
    """Lines of `current` (a document's text) that replacing it with `new` would drop, when the goal only adds; [] when
    the goal changes or removes anything, when the text is one line, or when nothing is dropped.

    With ledger.csv holding a header and one row, G18b answered "add Lisa Wong's order" by replacing the whole text
    with the header and the new row; Mark Chen's row was gone (10-01, app run). A replace is how an edit is made, so
    only a goal that asks to add, and nothing else, is held to keeping what is there."""
    if not _ADDS.search(goal or "") or _CHANGES.search(goal or ""):
        return []
    have = [l.strip() for l in (current or "").splitlines() if l.strip()]
    if len(have) < 2:
        return []
    keep = {l.strip() for l in (new or "").splitlines() if l.strip()}
    return [l for l in have if l not in keep]

def hid_idle_seconds() -> float:
    """Seconds since the last keyboard, mouse or trackpad event, read in-process (the HID system's own count, what
    `ioreg`'s HIDIdleTime reports).

    It was read by running `ioreg` through the shell, found on PATH. The app starts hands with a PATH of its own
    bundle, /usr/bin and /bin -- ioreg is in /usr/sbin -- so the read came back empty, empty read as 0 seconds, and
    every step that brings an app forward waited for a user who was not there: a music-app run sat "paused while you
    use your Mac" for 23 minutes with nobody at the Mac (10-01). A read that fails is taken as "not touched": a wait
    that never ends is worse than a step taken while someone is there, which the flash still guards against by
    checking again once the app is in front."""
    try:
        import Quartz
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState,
                                                                   Quartz.kCGAnyInputEventType))
    except Exception:   # noqa: BLE001
        try:
            out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):   # no ioreg either (not macOS): not touched, as documented
            return 1e9
        m = re.search(r'"HIDIdleTime" = (\d+)', out or "")
        return int(m.group(1)) / 1e9 if m else 1e9


#: Seconds the current run's driver has waited on the user so far: the driver's own user_wait_s, kept where a host
#: running hands in-process (the gym) can read it between steps without a handle on the driver.
WAITED_S = 0.0

class _Faults:
    """Test-only faults, on cue (HANDS_FAULTS=user_busy@3,capture_error@2,ghost_window@1): what real desktops did to
    runs on 09-30, reproduced at observation n so the harness's handling of them is tested (docs/harness-eval.md,
    layer 3). Nothing here sends input or changes the desktop: the driver is only told the user is busy, a read
    fails once, or the window list gains an untitled window with no accessibility behind it."""

    def __init__(self, spec: str) -> None:
        self.at: dict[str, int] = {}
        for part in filter(None, (x.strip() for x in spec.split(","))):
            name, _, n = part.partition("@")
            self.at[name] = int(n or 1)
        self.used: set[str] = set()
        self.busy_until = 0.0

    def reset(self) -> None:
        """A new run: every fault fires again at its observation (the gym runs many tasks in one process)."""
        self.used.clear()
        self.busy_until = 0.0

    def observed(self, n: int) -> None:
        if self.at.get("user_busy") == n and "user_busy" not in self.used:
            self.used.add("user_busy")
            self.busy_until = time.time() + float(os.environ.get("HANDS_FAULT_BUSY_S", "6"))

    def once(self, name: str, n: int) -> bool:
        if self.at.get(name) == n and name not in self.used:
            self.used.add(name)
            return True
        return False

    def since(self, name: str, n: int) -> bool:
        return name in self.at and n >= self.at[name]


FAULTS = _Faults(os.environ.get("HANDS_FAULTS", ""))

#: What the injected ghost window looks like: Safari's 500x500 one from 09-30.
_GHOST = {"window_id": 999999, "window_title": "", "bounds": {"x": 0, "y": 600, "width": 500, "height": 500},
          "is_on_screen": True, "observation_capability": "pixels_only",
          "observation_capability_reason": "no_matching_accessibility_window"}


def _transient_read(detail: str) -> bool:
    """A failed read worth making again (deskmind_hands/errors.py decides): an incomplete accessibility tree, or a
    window that changed while it was being captured."""
    from ..errors import transient
    return transient(detail)


class PeekabooDriver:
    name = "peekaboo"

    def __init__(self, *, app: str = "com.apple.finder", binary: str = "peekaboo",
                 timeout_s: float = 30.0, scratch: Path | None = None,
                 allow_foreground: bool = False, pin_window: bool = True,
                 open_workspace: bool = False,
                 stage_commands: list[str] | None = None,
                 reset_apps: list[str] | None = None,
                 transport: str = "cli") -> None:
        found = shutil.which(binary) or str(Path.home() / ".local/bin" / binary)
        if not Path(found).exists():
            raise DriverUnavailable(
                f"{binary!r} not found. Install it, then grant Screen Recording to the "
                "Peekaboo host app (granting the terminal does not count).")
        self.bin = found
        #: "mcp" keeps one Peekaboo process alive for the whole run so that everything stays in the background;
        #: "cli" is the original one-process-per-command path, kept for comparison and as the fallback.
        self.transport = transport
        self._mcp: _MCPSession | None = None
        self.app = app
        #: The task's own app, which stays switchable-back-to after a switch away (see _windows).
        self._home_app = app
        self.timeout_s = timeout_s
        self.scratch = Path(scratch) if scratch else Path.cwd()
        self.allow_foreground = allow_foreground
        self._snapshot: str | None = None
        self._window_id: str | None = None
        # Lock onto the first window seen. Without this, a second document in the
        # same app can quietly become the target halfway through a task.
        self._pin_window = pin_window
        self.open_workspace = open_workspace
        self.stage_commands = list(stage_commands or [])
        #: Apps named by the staging commands ("open -a TextEdit ..."), whose windows are offered as windows.
        #: A `hands do` run with a folder attached: its documents are offered to open (see _folder_documents), and
        #: TextEdit gets a close button. Off for task sets, whose observations stay as their models were trained on.
        self.offer_folder_files = False
        #: Documents this run closed (see _close_document).
        self._closed_documents: set[str] = set()
        self._stage_apps = [m.group(1) for c in self.stage_commands
                            for m in re.finditer(r"open\s+(?:-g\s+)?-a\s+\"?([\w ]+?)\"?\s", c + " ")]
        self._window_app: dict[str, str] = {}
        self.reset_apps = list(reset_apps or [])
        #: A window this driver opened, and is therefore responsible for closing.
        self._staged_window: str | None = None
        #: Counted, because "runs in the background without disturbing you" is a
        #: product claim that has to be measured, not asserted.
        self._foreground_actions = 0
        #: Commands carried out through the app's scripting interface instead of the GUI. Reported separately:
        #: a result reached this way is not a GUI-only result, and the two are never added together.
        self._scripted_actions = 0
        #: Actions on synthetic controls (see _project_finder). A third column, never merged with the other two.
        self._projected_actions = 0
        self._move_options: dict = {}
        self._pending_folder = ""
        #: This run's own file changes, newest last, each with what reverses it (see _undo_last).
        self._undo: list[dict] = []
        #: Messages sent in the chat app this run (see _chat_flash_click): at most one.
        self._chat_sends = 0
        #: Times the driver had to take the foreground because a background write
        #: was structurally impossible. Distinct from the model asking for it.
        self._escalations = 0
        #: Accessibility reads that came back incomplete and were retried.
        #: Counted because "the tree is sometimes not ready" is a property of the
        #: platform worth quantifying rather than absorbing silently.
        self._incomplete_reads = 0
        # Naming a bridge socket keeps the CLI from auto-starting its Bridge daemon (an explicit socket disables
        # auto-start). That matters because a live daemon refuses *every* capture here -- a debug build has no
        # code-signature identity, so its own daemon cannot be proven a safe ScreenCaptureKit owner, and the
        # refusal happens at admission even with --no-remote. The path deliberately never exists.
        self._origin = Point(0, 0)
        self._rects: dict[str, Rect] = {}
        self._element_window: dict[str, str] = {}
        self._element_value: dict[str, str] = {}
        self._labels_by_id: dict[str, str] = {}
        self._element_role: dict[str, str] = {}
        self._popups: dict[str, tuple] = {}          # pop-up element id -> its screen frame (see _attach_popup_options)
        self._menu_shortcuts: dict[str, dict[str, str]] = {}
        self._target_pid: str | None = None
        self._window_title = ""
        #: Scripting channel state: what a command just created (so the naming step can reach it) and what a
        #: copy put aside (so the paste that moves can move it). Both are things the GUI keeps in focus and
        #: pasteboard state that a background run does not have.
        self._just_created: Path | None = None
        self._copied: list[str] = []
        self._last_clicked_label = ""
        #: The last text written blind to the keyboard focus, cleared by any other action.
        self._blind_write: str | None = None
        #: Consecutive presses of one chord, cleared by any other action.
        self._chord_repeat: dict[str, int] = {}
        self._env = {**os.environ, "PEEKABOO_BRIDGE_SOCKET": str(self.scratch / "no-bridge.sock")}
        self._pinned_window: str | None = None
        self._last_geometry: ScreenGeometry | None = None
        self._obs_n = 0
        self._app_name = ""
        self._clipboard_saved: str | None = None
        # A host that has already verified Screen Recording for itself (the DeskMind helper, whose child Peekaboo
        # inherits its TCC identity) says so, and the 1.2 s `peekaboo permissions` round trip is skipped.
        if os.environ.get("HANDS_PERMISSIONS_VERIFIED") != "1":
            self._require_permissions()

    def _require_permissions(self) -> None:
        ok, data, detail = self._run("permissions")
        blob = json.dumps(data) if ok else detail
        if "Screen Recording" in blob and "Not Granted" in blob:
            raise DriverUnavailable(
                "Screen Recording is not granted to the Peekaboo host app. "
                "Run `peekaboo permissions status` and grant it to that app.")

    def capabilities(self) -> set[str]:
        # Narrower than the mock on purpose: a driver that claims an action it
        # silently drops turns a harness gap into a fake model failure.
        return {"screenshot", "ax", "clipboard", "focus_window", "menu"}

    # -- process ----------------------------------------------------------

    def _run(self, key: str, _extra: list[str] | None = None, **fmt) -> tuple[bool, dict, str]:
        """Run one command and unwrap Peekaboo's ``{success, data, error}`` envelope."""
        if self._mcp is not None and key in MCP_ROUTED:
            return self._run_mcp(key, _extra or [], **fmt)
        # --no-remote on every command, not just captures. A live Bridge daemon refuses every capture on this
        # machine (a debug build has no code-signature identity, so its own daemon cannot be proven a safe
        # ScreenCaptureKit owner) and nothing auto-starts one while every command stays caller-local. Pointing
        # PEEKABOO_BRIDGE_SOCKET at an unused path also stops the daemon, but then every command that is not a
        # capture fails with BRIDGE_UNAVAILABLE -- including `window list`, which is how staging finds its window.
        args = ([a.format(**fmt) for a in CMD[key]] + list(_extra or [])
                + (["--no-remote"] if key not in NO_REMOTE_UNSUPPORTED else []))
        try:
            proc = subprocess.run([self.bin, *args], capture_output=True, text=True,
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return False, {}, f"timed out after {self.timeout_s}s"
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return False, {}, (proc.stderr or proc.stdout or "no output").strip()[:300]
        if not env.get("success"):
            err = env.get("error") or {}
            # The failure envelope carries a recovery hint ("Observe the target
            # before retrying...", "Follow the canonical escalation metadata...").
            # It is written for whoever has to decide what to do next -- which is
            # the model -- so it goes back in the tool result rather than being
            # dropped on the floor.
            parts = [f"{err.get('code', 'ERROR')}: {err.get('message', '')}"]
            if err.get("hint"):
                parts.append(f"Hint: {err['hint']}")
            return False, {}, " ".join(parts)[:400]
        return True, env.get("data") or {}, ""

    # -- lifecycle --------------------------------------------------------

    def start(self, workspace: Path) -> None:
        if self.transport == "mcp" and self._mcp is None:
            # Electron apps (chat clients) nest their content in a web view far below Peekaboo's default AX depth of 12:
            # at 12 the chat app showed 40 elements, all containers; at 60 it showed 944, with chat names and messages.
            deep = self.app.lower() in DEEP_AX_APPS
            # The no-bridge socket goes to the MCP server too. It was only set for CLI calls, and when the Claude
            # desktop app started its own Peekaboo bridge the MCP server found that one and every capture was refused
            # ("Safe ScreenCaptureKit ownership support cannot be proven"): 24 runs ended before their first step.
            env = {"PEEKABOO_BRIDGE_SOCKET": self._env["PEEKABOO_BRIDGE_SOCKET"], **(DEEP_AX_ENV if deep else {})}
            self._mcp = _MCPSession(self.bin, self.timeout_s, env=env)
        self._start(workspace)

    def _start(self, workspace: Path) -> None:
        self.ws = Path(workspace)
        FAULTS.reset()
        self._closed_documents = set()
        self._doc_baseline = {}
        self.user_wait_s = 0.0
        global WAITED_S
        WAITED_S = 0.0
        self._undo = []
        self._chat_sends = 0
        # Reset before staging, not after: staging is what discovers and pins the
        # target window, and clearing state afterwards threw that away, leaving
        # every observation free to land on whichever window the app preferred.
        self._snapshot = None
        self._pinned_window = None
        self._window_id = None
        self._obs_n = 0
        # The user's clipboard is theirs. Save now, restore in close(), so a
        # benchmark run can never cost someone what they had copied.
        self._clipboard_saved = self.clipboard()
        front_before = self._frontmost()
        for bundle in self.reset_apps:
            self._close_all_windows(bundle)
        if self.app.lower() == "com.apple.finder" or "Finder" in self._stage_apps:
            # Windows an earlier run opened on its own subfolders (项目, archive, ...) are not titled `ws` and were
            # never closed; the next run saw them as its own and spent ten actions switching between its window and
            # a previous run's. Only windows showing a folder under this harness's runs directory are closed.
            # By index: `repeat with w in every Finder window` yields references whose properties cannot be read,
            # so inside the try every close silently failed and 27 stale windows piled up -- all offered to the
            # planner as places to switch to, alongside the user's own Downloads window.
            self._osa('tell application "Finder"\n'
                      '  set n to count of Finder windows\n'
                      '  repeat with i from n to 1 by -1\n'
                      '    try\n'
                      '      set w to Finder window i\n'
                      '      if (POSIX path of ((target of w) as alias)) contains "/hands/runs/" then close w\n'
                      '    end try\n'
                      '  end repeat\n'
                      'end tell')
        if "Safari" in self._stage_apps:
            # Every run of a Safari task opened its page in a new window and nothing closed them; a few rounds in,
            # Safari's accessibility stopped answering altogether. Only this harness's own pages are closed -- by
            # URL, under the runs directory -- never the user's tabs.
            if _app_running("Safari"):   # not asked by AppleScript, which would launch it (see _close_all_windows)
                self._osa('tell application "Safari" to close (every document whose URL contains "/hands/runs/")')
        # TextEdit windows that exist before this run are not this run's, whatever they are called: a leftover
        # "未命名-1" from an earlier run shares its title with this run's scratch document, and a planner switched to
        # it and ended on its unreadable accessibility tree.
        self._preexisting: set[str] = set()
        #: Finder windows open before staging: a staged folder's window is one that was not there before. Title and
        #: position cannot tell it apart -- every new Finder window opens at the same place, and one task's 「桌面」
        #: window had a twin left from the run before, same title, same bounds.
        self._pre_finder = self._window_ids() if self.app.lower() == "com.apple.finder" else set()
        for bundle in {self.app} | {STAGE_APP_BUNDLES.get(n, n) for n in self._stage_apps}:
            if bundle.lower() == "com.apple.textedit":
                ok_w, data_w, _ = self._run("window_list", app=bundle)
                # Except the documents of this run's own folder: a request made from the app often starts with the
                # user's files already open, and with them left out the planner could never switch to the file it
                # was to write (a document-editing request: 0/5 in general mode, 3/3 with the same files staged by the harness).
                ours = self._open_workspace_documents() if _app_running("TextEdit") else set()
                self._preexisting |= {str(w.get("window_id")) for w in ((data_w or {}).get("windows") or [])
                                      if ok_w and w.get("window_id") is not None and window_key(w) not in ours}
        for cmd in self.stage_commands:
            # Task files open documents with `open -a App file`; add -g so staging never raises the app. Written
            # here rather than in thirteen task files so a new task cannot forget it.
            cmd = re.sub(r"(^|[;&|]\s*)open\s+(?!-g)", r"\1open -g ", cmd)
            # A TextEdit this launches comes up without restoring windows: after a force quit it reopened the old
            # ones, hidden, and they were offered as this task's windows. Applies to a fresh launch only.
            if re.search(r"open -g -a \"?TextEdit\"?\s", cmd + " ") and "--args" not in cmd:
                cmd += " --args -ApplePersistenceIgnoreState YES"
            subprocess.run(["/bin/sh", "-c", cmd], cwd=str(self.ws), capture_output=True,
                           timeout=30, env={**os.environ, "WS": str(self.ws)})
            time.sleep(1.2)
        if self.stage_commands:
            # Whatever the staging opened is what we observe; pin its window.
            shot = self.scratch / "_stage.png"
            ok, data, _ = self._run("see", app=self.app, path=str(shot)) if self.app.lower() != "com.apple.finder" \
                else (True, {}, "")
            shot.unlink(missing_ok=True)
            if ok:
                # Finder's front window can be a stale search window `see` cannot read, and the pin below then never
                # ran (a three-file save task: every observation "no window open"). Finder's window is found without `see`.
                win = (data.get("coordinate_context") or {}).get("window") or {}
                self._pinned_window = str(win.get("window_id", "")) or None
                real = self._real_window_ids()
                if real and self._pinned_window not in real:
                    self._pinned_window = real[0]     # never pin the ghost; the largest real window instead
                if self.app.lower() == "com.apple.finder":
                    # A Finder task pins the workspace's own window, never whichever Finder window is in front:
                    # one task staged `open -g "$WS"` and was pinned to the user's Downloads window, where it clicked
                    # the user's files (every click failed; nothing changed).
                    # By title and position: the sandbox's 「桌面」 folder shares its title with the user's own
                    # Desktop window, so the title alone could pin the user's window. The window may also still be
                    # opening, so this is retried for a few seconds.
                    root = self._q(str(Path(os.path.realpath(str(self.ws)))) + "/")
                    ours = None
                    for _ in range(10):
                        ok_n, out = self._osa('tell application "Finder"\n'
                                              '  repeat with i from 1 to count of Finder windows\n'
                                              '    try\n'
                                              '      set t to POSIX path of ((target of Finder window i) as alias)\n'
                                              f'      if t starts with "{root}" then\n'
                                              '        set b to bounds of Finder window i\n'
                                              '        return (name of Finder window i) & "|" & (item 1 of b) & "|" & (item 2 of b)\n'
                                              '      end if\n'
                                              '    end try\n'
                                              '  end repeat\n'
                                              '  return ""\n'
                                              'end tell')
                        if ok_n and out.strip():
                            name, bx, by = out.strip().split("|")[:3]
                            ok_w, data_w, _ = self._run("window_list", app=self.app)
                            same = [str(w.get("window_id")) for w in ((data_w or {}).get("windows") or []) if ok_w
                                    and (w.get("window_title") or "").strip() == name.strip()
                                    and abs(float((w.get("bounds") or {}).get("x", -1e9)) - float(bx)) < 3]
                            fresh = [w for w in same if w not in self._pre_finder]
                            if fresh:
                                ours = max(fresh, key=int)
                            elif same:
                                # `hands do` on a folder that already has its own window open (the last run's, or
                                # the user's): `open` brings no new one. Finder says that window's target is the
                                # workspace itself, so it is ours to use. (A bench workspace is a fresh directory
                                # per run, so there this never matches a stale window.)
                                ours = max(same, key=int)
                            if ours:
                                break
                        time.sleep(0.5)
                    self._pinned_window = ours
                    if ours is None:
                        raise DriverUnavailable(f"staging opened no Finder window on the workspace {self.ws}")
        elif self.open_workspace:
            self._stage_finder_window()
        # Staging is not allowed to keep the user's focus. If an app came forward anyway -- a first launch that
        # ignores -g, a sheet -- the user's app is given the front back, and the incident is counted, because a
        # steal that is quietly repaired is still a steal.
        front_after = self._frontmost()
        # Only a front taken by one of THIS task's apps is a steal. The first version compared before and after,
        # and when the user had switched apps themselves in between it "gave the front back" -- pulling them away
        # from the app they had just chosen, eight times in one round, and counting each as ours. The user's own
        # switches are not touched and not counted.
        ours = {self.app.lower()} | {STAGE_APP_BUNDLES.get(n, n).lower() for n in self._stage_apps}
        ours_names = {"finder", "访达", "textedit", "文本编辑", "safari", "safari浏览器"}
        taken = front_after and front_after != front_before and (
            front_after.lower() in ours_names or front_after.lower() in ours)
        if taken and front_before and self._mcp is not None:
            self._foreground_actions += 1
            self._osa(f'tell application "{self._q(front_before)}" to activate')

    def _runs_roots(self) -> list[str]:
        """The runs directory every staged workspace lives in (DESKMIND_RUNS_DIR, else the workspace's nearest
        ancestor named `runs`, else its run directory), as given and resolved, each ending in a slash."""
        ws = Path(getattr(self, "ws", "") or ".")
        root = os.environ.get("DESKMIND_RUNS_DIR") or next(
            (str(p) for p in [ws, *ws.parents] if p.name == "runs"), str(ws.parent))
        return sorted({r.rstrip("/") + "/" for r in (root, os.path.realpath(root))})

    def _close_all_windows(self, bundle: str) -> int:
        """Close every window of an application before a run starts.

        Leaving them open lets one run's documents and tabs leak into the next,
        and the agent has no way to tell an inherited document from the one the
        task means.
        """
        if bundle.lower() == "com.apple.textedit":
            # Closing a TextEdit window that holds unsaved changes raises a "save changes?" sheet, and TextEdit
            # comes to the front to show it -- 26 seconds of it in one round, left behind by a run the endpoint cut
            # off mid-edit. Its scripting interface closes documents without asking, and without activating.
            # Any script that names TextEdit launches it -- `if it is running`, even `if application "TextEdit" is
            # running then tell ...`, since compiling the tell loads its dictionary. A bare launch opens TextEdit's
            # 「打开」 panel, which the document staged a moment later hides but never closes: the hidden "Save Panel
            # Accessory View" window a later run found. So the check is made outside AppleScript.
            # Only documents inside the runs directory are closed: sandbox files an earlier run left open. A document
            # of the user's own -- saved elsewhere, or untitled with no path at all -- is never closed: `close every
            # document saving no` threw away whatever the user had not saved, from the app's Try examples (10-02).
            if not _app_running("TextEdit"):
                return 0
            ok = False
            for root in self._runs_roots():
                ok |= self._osa(f'tell application "TextEdit" to close (every document whose path starts with '
                                f'"{self._q(root)}") saving no')[0]
            time.sleep(0.5)
            return 1 if ok else 0
        ok, data, _ = self._run("window_list_of", app=bundle)
        if not ok:
            return 0
        n = 0
        for w in (data.get("windows") or []):
            wid = w.get("window_id")
            if wid is not None and self._run("window_close", window=str(wid))[0]:
                n += 1
        if n:
            time.sleep(0.8)
        return n

    #: A goal that asks for something on the web: typing into a browser's own fields is then what it wants.
    WEB_INTENT = re.compile(r"https?://|www\.|\.(?:com|org|net|cn)\b|\b(?:url|website|web ?page|web|online|internet|"
                            r"google|bing|search the|look (?:it )?up|browse)\b|网址|网站|网页|上网|百度|谷歌|必应|"
                            r"搜索引擎|在浏览器|浏览器里", re.I)

    def _keep_baseline(self, els) -> None:
        """A document's text as this run first saw it, per window title: what was there before the run, which is the
        user's (lines_lost protects only that -- the run may redo what it wrote itself)."""
        base = self.__dict__.setdefault("_doc_baseline", {})
        title = (getattr(self, "_window_title", "") or "").strip()
        for e in els:
            if title and title not in base and e.settable and e.value is not None and (
                    (e.ax_role or "").lower() == "axtextarea" or "\n" in (e.value or "")):
                base[title] = e.value or ""

    def _lines_lost_refusal(self, action: Action) -> str | None:
        """Why replacing a document's text is refused: the goal asks to add to it, and the new text drops lines it
        had before the run (lines_lost against the run's first look). None otherwise. The run's own lines are its
        to redo: protecting them too, G04 had its whole-text rewrite refused, was left with appending only, and
        appended the same text eleven times (10-02)."""
        if not action.clear_first:
            return None
        current = getattr(self, "_element_value", {}).get(action.binding.element_id or "", "")
        lost = lines_lost(getattr(self, "goal", "") or "", current, action.text or "")
        base = getattr(self, "_doc_baseline", {}).get((getattr(self, "_window_title", "") or "").strip())
        if base is not None:
            before = {l.strip() for l in base.splitlines() if l.strip()}
            lost = [l for l in lost if l in before]
        if not lost:
            return None
        # The instruction first: the planner's history keeps the first 120 characters of an error.
        return (f"not written: the goal only adds -- append instead; replacing the text would remove "
                f"{len(lost)} line(s) it has, the first {lost[0]!r}")

    def _browser_field_refusal(self, eid: str) -> str | None:
        """Why typing into `eid` is refused: it is a browser's own field (the address bar), which sends what is typed to
        a website, and the goal asks for nothing on the web. None otherwise, or when it cannot be told."""
        r = getattr(self, "_rects", {}).get(eid)
        if r is None or self.WEB_INTENT.search(getattr(self, "goal", "") or ""):
            return None
        try:
            from deskmind_hands.drivers import axchrome
            pid = self._pid_of(self.app)
            frames = axchrome.fields_outside_page(pid) if pid else []
        except Exception:   # noqa: BLE001 -- a structure that cannot be read is no refusal
            return None
        if axchrome.at(frames, self._origin.x + r.x + r.w / 2, self._origin.y + r.y + r.h / 2):
            label = self._labels_by_id.get(eid, eid)
            return (f"not typed into {label!r}: it is the browser's own field (its address bar), and what is typed "
                    f"there goes to a website, while the goal asks for nothing on the web. Type into the page, or "
                    f"into the document the goal names.")
        return None

    def _window_frame(self) -> tuple | None:
        """Where the observed window is and how big, from the window server (cheap: no accessibility walk)."""
        wid = self._pinned_window or self._window_id
        if not wid or not str(wid).isdigit():
            return None
        try:
            import Quartz
            info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, int(wid)) or []
        except Exception:   # noqa: BLE001 -- no answer is no check, never a failure
            return None
        b = info[0].get("kCGWindowBounds") if info else None
        return (round(b["X"]), round(b["Y"]), round(b["Width"]), round(b["Height"])) if b else None

    def _moved_since_seen(self) -> str | None:
        """Why a point taken from the last observation no longer lands where it did, or None: the window was moved or
        resized in the seconds the planner took (cua binds a coordinate click to its capture the same way)."""
        seen = getattr(self, "_seen_frame", None)
        now = self._window_frame() if seen else None
        if seen and now and now != seen:
            return (f"the window moved or changed size since it was seen ({seen} -> {now}); a point taken from that "
                    f"look would land elsewhere -- observe again")
        return None

    def _window_ids(self) -> set[str]:
        ok, data, _ = self._run("window_list", app=self.app)
        if not ok:
            return set()
        return {str(w.get("window_id")) for w in (data.get("windows") or [])
                if w.get("window_id") is not None}


    #: Buttons that only acknowledge. Nothing here discards work or deletes anything: a harness may clear an alert
    #: it left behind, it may not answer a question on the user's behalf.
    ACK_BUTTONS = ("好", "OK", "确定", "Got It", "Done", "关闭")

    def _clear_stale_alert(self) -> None:
        """Dismiss an alert this application is holding, so staging can talk to it at all.

        `dialog dismiss` answers "No unique enabled AXPress dialog button matched the request" for a plain warning,
        so the fallback is to click its acknowledgement button by name. This is not optional politeness: while such
        an alert is up the app refuses every window call and AppleScript reports it busy, and three rounds of this
        measurement lost every remaining Finder task to one unattended "the name docs is already taken"."""
        ok, _, detail = self._run("dialog_dismiss", app=self.app)
        if ok:
            return
        # No dialog at all -- the usual case -- is said plainly; only an alert whose button went unrecognised needs
        # the buttons tried by name. Trying all six every run cost 3.9 s of every task's start.
        if "No dialog matched" in detail:
            return
        for button in self.ACK_BUTTONS:
            ok, _, _ = self._run("dialog_click", button=button, app=self.app)
            if ok:
                self._escalations += 1
                time.sleep(0.5)
                return

    def _close_window(self, wid: str) -> bool:
        """Close the window this driver staged, by name, through AppleScript.

        Everything else was tried: Peekaboo refuses `window close` for Finder, cmd+W needs Finder frontmost and
        reports success without closing anything when it is not, and the CLI declines to bring an app forward
        without consent it has no flag for. Meanwhile windows accumulate -- five titled `ws` at once -- and a
        Finder left with an inline rename pending goes busy (-15260) and stops answering anything at all.
        Best effort: a failure here costs a stray window, not a run."""
        script = ('tell application "Finder"\n'
                  '  set n to count of Finder windows\n'
                  '  repeat with i from n to 1 by -1\n'
                  '    set w to Finder window i\n'
                  f'    if (name of w) is "{self._q(self.ws.name)}" then close w\n'
                  '  end repeat\n'
                  'end tell')
        try:
            subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return wid not in self._window_ids()

    def _windows_titled(self, title: str) -> list[str]:
        ok, data, _ = self._run("window_list", app=self.app)
        if not ok:
            return []
        return [str(w.get("window_id")) for w in (data.get("windows") or [])
                if (w.get("window_title") or w.get("title") or "").strip() == title
                and w.get("window_id") is not None]

    def _window_titled(self, title: str, tries: int = 3) -> str | None:
        """The id of this app's window whose title is exactly ``title`` (Finder titles a window after its folder).

        Retried: this app reports every window title as null on some reads (`accessibility_enumeration_incomplete`),
        and treating one such read as "the workspace is not open" would refuse a run that was staged correctly."""
        for attempt in range(tries):
            wid = self._window_titled_once(title)
            if wid is not None:
                return wid
            if attempt + 1 < tries:
                self._incomplete_reads += 1
                time.sleep(0.8)
        return None

    def _window_titled_once(self, title: str) -> str | None:
        ok, data, _ = self._run("window_list", app=self.app)
        if not ok:
            return None
        hits = [str(w.get("window_id")) for w in (data.get("windows") or [])
                if (w.get("window_title") or w.get("title") or "").strip() == title
                and w.get("window_id") is not None]
        # Every run's workspace is a directory called `ws`, so several stale windows carry the same title. Pinning
        # one of them would silently measure an earlier run's directory; ambiguity is a refusal, not a coin toss.
        return hits[0] if len(hits) == 1 else None

    def _menu_options(self, path: str) -> str:
        """What the model should have been able to read before it guessed.

        A menu path is an argument the observation never showed -- the element
        list covers the window, not the menu bar -- so the model supplies it from
        memory of what macOS menus are usually called. That produced 保存 vs 存储,
        four wasted actions and a run that cost six times its twin. Rather than
        spend tokens listing every menu on every observation, the options are
        returned at the moment a path misses, at the deepest point that matched.
        """
        ok, data, _ = self._run("menu_list", app=self.app)
        if not ok:
            return ""
        level = data.get("menu_structure") or []
        walked: list[str] = []
        for want in [p.strip() for p in path.split(">") if p.strip()]:
            titles = [m.get("title", "") for m in level]
            hit = next((m for m in level if m.get("title") == want), None)
            if hit is None:
                where = " > ".join(walked) or "the menu bar"
                names = [t for t in titles if t]
                shown = ", ".join(repr(t) for t in names[:12])
                more = f" (+{len(names) - 12} more)" if len(names) > 12 else ""
                return f"No {want!r} under {where}. Available: {shown}{more}"
            walked.append(want)
            level = hit.get("items") or []
        return ""

    @staticmethod
    def _ghost(w: dict) -> bool:
        """A window that is not one: tiny, untitled, and pixels-only with no accessibility window behind it.

        Safari keeps a 64x64 one at the screen edge. Staging pinned it, every observation then read "AX tree
        incomplete", and it is also the id focus_window was sent to 241 times in one run -- the same window
        behind two failures that looked unrelated."""
        b = w.get("bounds") or {}
        tiny = (b.get("width", 1000) or 0) < 150 or (b.get("height", 1000) or 0) < 100
        blind = (w.get("observation_capability") == "pixels_only"
                 or w.get("observation_capability_reason") == "no_matching_accessibility_window")
        untitled = not (w.get("window_title") or "").strip()
        # Untitled with no accessibility window behind it is not one either, at any size: Safari keeps 500x500 and
        # 558x121 ones, and a D1 run spent its first two steps switching to them ("cannot be read right now").
        return (tiny and (blind or untitled)) or (blind and untitled)

    def _real_window_ids(self) -> list[str]:
        """Ids of this app's real windows, largest first."""
        ok, data, _ = self._run("window_list", app=self.app)
        if not ok:
            return []
        ws = [w for w in (data.get("windows") or []) if w.get("window_id") is not None and not self._ghost(w)]
        ws.sort(key=lambda w: -((w.get("bounds") or {}).get("width", 0) * (w.get("bounds") or {}).get("height", 0)))
        return [str(w["window_id"]) for w in ws]

    def _workspace_folder_names(self) -> set[str]:
        ws = getattr(self, "ws", None)
        root = Path(os.path.realpath(str(ws))) if ws else None
        if root is None or not root.is_dir():
            return set()
        return {root.name} | {p.name for p in root.rglob("*") if p.is_dir()}

    def _windows(self) -> list[WindowRef]:
        """Every window of the focused app, so the pinned one is not the only one.

        Peekaboo reports a window's title as an empty string more often than not
        (``accessibility_enumeration_incomplete``), and an untitled row is not
        addressable by a model reading a list. Fall back to the window index so
        every entry says something, and keep the id, which is what actually
        selects.
        """
        ok, data, _ = self._run("window_list", app=self.app)
        if not ok:
            return []
        if FAULTS.since("ghost_window", self._obs_n):
            data = {**data, "windows": list(data.get("windows") or []) + [dict(_GHOST)]}
        out: list[WindowRef] = []
        for w in (data.get("windows") or []):
            wid = w.get("window_id")
            if wid is None or self._ghost(w):
                continue
            wid = str(wid)
            if wid in getattr(self, "_unreadable", set()):
                continue
            title = (w.get("window_title") or "").strip()
            # A Finder window is offered only if it shows a folder of this workspace. Anything else -- another
            # run's folder, the user's Downloads -- is not a place this task may go, and is not shown.
            if (self.app.lower() == "com.apple.finder" and title and wid != self._pinned_window
                    and title not in self._workspace_folder_names()):
                continue
            # Likewise a TextEdit window is offered only if it shows a document of this workspace. C013 switched to
            # an "(untitled, index 2)" window that was not the task's -- the user's own, or left from before --
            # and the run ended on its unreadable accessibility tree.
            if (self.app.lower() == "com.apple.textedit" and wid != self._pinned_window
                    and (title not in self._workspace_file_names()
                         or wid in getattr(self, "_preexisting", set()))):
                continue
            out.append(WindowRef(
                id=wid,
                title=title or f"(untitled, index {w.get('window_index', '?')})",
                active=(wid == self._pinned_window or
                        (self._pinned_window is None and wid == self._window_id)),
                on_screen=bool(w.get("is_on_screen", True)),
            ))
        # The windows of the other apps this task staged, listed as windows. A task that reads in Safari and
        # writes in TextEdit needs an app switch, and FOCUS_APP -- a verb the planner was never trained on -- was
        # passed over at 0.98 for BLOCKED; switching windows is something it does readily. So the other app's
        # document is simply another window, named with its app.
        # The task's own app is one of them once the run has switched away: a three-file save task saved its files in TextEdit
        # and then had to rename them in Finder, but only TextEdit's windows were offered, and the planners
        # tried all gave up there, 6 runs of 6.
        if self._mcp is not None:
            home = {v.lower(): k for k, v in STAGE_APP_BUNDLES.items()}.get(self._home_app.lower(), self._home_app)
            for name in dict.fromkeys(self._stage_apps + [home]):
                bundle = STAGE_APP_BUNDLES.get(name, name)
                if bundle.lower() == self.app.lower():
                    continue
                ok2, data2, _ = self._run("window_list", app=bundle)
                for w in ((data2 or {}).get("windows") or []) if ok2 else []:
                    if w.get("window_id") is None or self._ghost(w):
                        continue
                    wid = str(w["window_id"])
                    if wid in getattr(self, "_unreadable", set()):
                        continue
                    if (bundle.lower() == "com.apple.finder"
                            and (w.get("window_title") or "").strip() not in self._workspace_folder_names()):
                        continue
                    if (bundle.lower() == "com.apple.textedit"
                            and ((w.get("window_title") or "").strip() not in self._workspace_file_names()
                                 or wid in getattr(self, "_preexisting", set()))):
                        continue
                    self._window_app[wid] = bundle
                    t = (w.get("window_title") or "").strip()
                    if bundle.lower() == "com.apple.textedit" and re.fullmatch(r"未命名-\d+", t):
                        t += "：新建的空白纯文本文稿，可写入内容并另存为"   # a scratch document says what it is for
                    out.append(WindowRef(id=wid, title=f"{t} ({name})",
                                         active=False, on_screen=bool(w.get("is_on_screen", True))))
        return out

    def _stage_finder_window(self) -> None:
        """Put the workspace on screen in a state whose contents can be read and typed into.

        Three things have to be true before a Finder task is even attemptable,
        and each was learned by watching a run fail on it:

        - List view. `open <dir>` inherits the last view mode; in column view the
          window showed the folder's ancestors with the target folder's own
          column past the right edge, so the files under test were absent from
          the accessibility tree entirely. Cmd-2 rather than the View menu
          because menu titles are localised, and rather than AppleScript because
          that needs an Automation grant this does not.
        - Real keyboard focus on the target window. Background typing into an
          unfocused window is not slow, it is impossible: "such a window also
          refuses accessibility focus requests, so no amount of retrying moves
          focus there in the background."
        - A pinned window id, so later observations cannot drift to another
          window of the same app.

        Other windows of this app are left alone; they may be the user's.
        """
        # Identify the staged window by difference rather than by asking which
        # window is frontmost. Nine windows accumulated across one debugging
        # session, `see` picked one of the old ones, and typing then failed
        # against a window that did not hold focus -- which reads exactly like a
        # model that cannot type.
        # Identify the staged window as "titled after the workspace and not open before we started". Closing the
        # leftovers first was the earlier idea and it does not work: Peekaboo refuses `window close` for Finder,
        # cmd+W needs a foreground Finder, and Finder itself goes busy (-15260) while an inline rename is pending
        # from an earlier run. Nothing has to be closed to know which window is ours.
        # An alert left over from an earlier run makes this application refuse everything -- windows cannot be
        # listed, closed or opened, and AppleScript answers "busy" (-15260). Three separate rounds lost every
        # remaining Finder task to one unattended "the name docs is already taken". Clearing it costs one call.
        self._clear_stale_alert()
        # Every run's workspace directory is called `ws`, so a leftover window carries the title this staging is
        # about to look for, and Finder may answer `open` by reusing one instead of making a new one -- leaving
        # nothing to identify. Closing them first is safe now that an alert can no longer wedge the app.
        for old in self._windows_titled(self.ws.name):
            self._close_window(old)
        before = self._window_ids()
        if not before:
            # An empty read is flakiness -- unless Finder really has no window, which is common and was paid for
            # with a blind 0.8 s wait on every such start. Finder is asked directly first.
            ok_n, n = self._osa('tell application "Finder" to count of Finder windows')
            if not (ok_n and n.strip() == "0"):
                self._incomplete_reads += 1
                time.sleep(0.3)
                before = self._window_ids()
        # -g: open without bringing Finder forward. Staging used to be where most of the visible focus theft came
        # from -- every task began by raising a window over whatever the user was doing.
        subprocess.run(["/usr/bin/open", "-g", str(self.ws)], capture_output=True, timeout=15)
        wid = None
        # Polled, not slept on: each check is itself a Peekaboo call (~0.25 s), so a short pause between checks is
        # enough; the window usually exists by the first or second look. Same ~4 s budget as before.
        for i in range(14):
            time.sleep(0.05 if i < 4 else 0.3)
            fresh = [w for w in self._windows_titled(self.ws.name) if w not in before]
            if fresh:
                wid = sorted(fresh)[-1]
                break
            new_ids = self._window_ids() - before
            if new_ids:                    # a new window whose title has not been enumerated yet
                wid = sorted(new_ids)[-1]
                break
        if wid is None:
            raise DriverUnavailable(
                f"staging opened no {self.app} window for the workspace {self.ws}: the folder did not open, or the "
                f"app is stuck (Finder reports busy while an inline rename from an earlier run is pending)")
        self._staged_window = wid
        if not wid:
            return
        # List view, set through AppleScript on this window only: cmd+2 needed Finder frontmost, so every task
        # began by stealing the foreground to change a view setting. Staging is the harness's job, not the
        # agent's, so a scripting route here measures nothing it should not.
        # By the folder the window shows, not by id: Finder's scripting ids are its own numbering and do not match
        # the CGWindowID Peekaboo reports. And by the resolved path, compared window by window -- `whose target
        # is` never matched, because /tmp is really /private/tmp and the alias comparison is literal.
        folder = self._q(os.path.realpath(str(self.ws)) + "/")
        script = ('tell application "Finder"\n'
                  '  repeat with i from 1 to (count of Finder windows)\n'
                  '    set w to Finder window i\n'
                  '    try\n'
                  f'      if (POSIX path of (target of w as alias)) is "{folder}" then set current view of w to list view\n'
                  '    end try\n'
                  '  end repeat\n'
                  'end tell')
        try:
            subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        # The view switch is drawn by the time the osascript returns; the first observation retries on an
        # incomplete tree anyway, so this only needs to cover the redraw (was 0.8 s).
        time.sleep(0.3)
        self._pinned_window = wid

    def close(self) -> None:
        # Close only what this driver opened. Leaving them behind poisons the
        # next run's focus and, on a developer's own machine, is just litter.
        if self._staged_window:
            self._close_window(self._staged_window)
            self._staged_window = None
        if self.stage_commands and _app_running("TextEdit"):
            # The documents a staged task opened in its sandbox. Each task closed the ones before it at its start, so
            # the last task of a set left its own open: bench G13's source.txt stayed in TextEdit, and a later run
            # from the app, its own document closed, went on to that window and typed its rows into it (10-01).
            # Only documents inside this run's workspace, never a live run's folder (no stage commands there).
            for root in {str(self.ws), os.path.realpath(str(self.ws))}:
                self._osa(f'tell application "TextEdit" to close (every document whose path starts with '
                          f'"{self._q(root.rstrip("/") + "/")}") saving no')
        if self._clipboard_saved is not None:
            self._set_clipboard(self._clipboard_saved)
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None

    # -- scripting channel ------------------------------------------------

    def _frontmost(self) -> str:
        ok, out = self._osa('tell application "System Events" to get name of first process whose frontmost is true')
        return out if ok else ""

    def _osa(self, script: str) -> tuple[bool, str]:
        try:
            p = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        return p.returncode == 0, (p.stdout.strip() or p.stderr.strip())

    @staticmethod
    def _q(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def _finder_folder(self) -> Path | None:
        """The folder the observed Finder window shows: by its title, preferring one inside the workspace."""
        title = self._q(self._window_title or self.ws.name)
        ok, out = self._osa('tell application "Finder"\n'
                            '  set out to ""\n'
                            '  repeat with i from 1 to (count of Finder windows)\n'
                            '    set w to Finder window i\n'
                            f'    if name of w is "{title}" then set out to out & (POSIX path of (target of w as alias)) & linefeed\n'
                            '  end repeat\n'
                            '  return out\n'
                            'end tell')
        paths = [Path(x) for x in out.splitlines() if x.strip()] if ok else []
        root = Path(os.path.realpath(str(self.ws)))
        inside = [Path(os.path.realpath(str(p))) for p in paths]
        inside = [p for p in inside if p == root or root in p.parents]
        # Inside the workspace or nothing. The old fallback took any window with a matching title, and one run
        # resolved the current folder to "/" -- after which a scripted create, move or rename would have acted
        # outside the workspace. With no folder, every scripted Finder operation refuses instead.
        return inside[0] if inside else None

    def _scripted_command(self, chord: str) -> ExecResult | None:
        """Carry out a command through the application's own scripting interface, in the background.

        A command chord only reaches the frontmost app and a background click on its menu item is accepted and
        does nothing, so the one way to save, create or move without taking the user's focus is to ask the app to
        do it. The planner's action space does not change: it still says cmd+S; the driver chooses the channel.
        Returns None when this app/chord has no scripted equivalent, so the caller falls through."""
        app = self.app.lower()
        canon = self._canon(chord)
        if app == "com.apple.textedit" and canon == self._canon("cmd+s"):
            # A scratch document saved in place is the hidden file it was staged from: "saved '未命名-1'" was
            # reported as a success, and a planner pressed cmd+S on it again and again.
            # It is saved the way 保存 saves it: by 另存为, once a name and a place are set.
            scratch = (Path(os.path.realpath(str(self.ws))) / ".hands" / (self._window_title or "")
                       if getattr(self, "ws", None) is not None else None)
            if scratch is not None and self._window_title and scratch.exists():
                pending = getattr(self, "_pending_saveas", {})
                if pending.get("window") not in (None, self._window_id):
                    pending = {}
                if not (pending.get("name") and pending.get("folder")):
                    return ExecResult(False, f"{self._window_title!r} has never been saved: fill in 另存为：文件名 "
                                             f"and 另存为：位置 first, then save")
                return self._textedit_projected("syn:saveas:do", Action(kind=ActionKind.CLICK))
            return self._textedit_save(self._window_title)
        if app == "com.apple.finder":
            folder = self._finder_folder()
            if folder is None:
                return None
            if canon == self._canon("cmd+shift+n") and self._pending_folder:
                # A name already typed into the projected form is what this creates: the planner mixed the two
                # routes (fill the form, then press the chord), and both should end in the same folder.
                name, self._pending_folder = self._pending_folder, ""
                if (folder / name).exists():
                    return ExecResult(False, f"{name!r} already exists in {folder.name!r}")
                ok, out = self._osa(f'tell application "Finder" to make new folder at (POSIX file '
                                    f'"{self._q(str(folder))}" as alias) with properties {{name:"{self._q(name)}"}}')
                if ok:
                    self._undo.append({"kind": "mkdir", "path": folder / name})
                self._scripted_actions += 1
                return ExecResult(ok, f"created folder {name!r} in {folder.name!r} (scripted)" if ok
                                  else f"could not create {name!r}: {out[:160]}")
            if canon == self._canon("cmd+shift+n"):
                ok, out = self._osa(f'tell application "Finder" to POSIX path of ((make new folder at '
                                    f'(POSIX file "{self._q(str(folder))}" as alias)) as alias)')
                if not ok:
                    return ExecResult(False, f"could not create a folder in {folder}: {out[:160]}")
                self._just_created = Path(out.rstrip("/"))
                self._undo.append({"kind": "mkdir", "path": self._just_created})
                self._scripted_actions += 1
                return ExecResult(True, f"created {self._just_created.name!r} in {folder.name!r} (scripted). It is "
                                        f"selected and waiting for its name: type the name into the keyboard focus")
            if canon == self._canon("cmd+c") and self._last_clicked_label:
                src = folder / self._last_clicked_label
                if src.exists():
                    self._copied = [str(src)]
                    self._scripted_actions += 1
                    return ExecResult(True, f"copied {src.name!r} (scripted)")
            # Compared in canonical form: _canon sorts modifiers, so "option+cmd+v" arrives as "cmd+option+v" and
            # a literal comparison meant the move this channel exists for never ran once.
            if canon == self._canon("option+cmd+v") and self._copied:
                if all(Path(os.path.realpath(src)).parent == folder for src in self._copied):
                    # Moving a file into the folder it is already in is a no-op Finder reports as done. Twice the
                    # planner went up, came back down and pasted in place; said plainly, it is a refusal.
                    return ExecResult(False, f"{', '.join(Path(x).name for x in self._copied)} is already in "
                                             f"{folder.name!r}: go to the destination first (KEY cmd+up for the "
                                             f"enclosing folder, or OPEN a folder), then paste")
                moved = []
                for src in self._copied:
                    ok, out = self._osa(f'tell application "Finder" to move (POSIX file "{self._q(src)}" as alias) '
                                        f'to (POSIX file "{self._q(str(folder))}" as alias)')
                    if not ok:
                        return ExecResult(False, f"could not move {Path(src).name!r} into {folder.name!r}: {out[:160]}")
                    moved.append(Path(src).name)
                    self._undo.append({"kind": "move", "from": Path(src), "to": folder / Path(src).name})
                self._copied = []
                self._scripted_actions += 1
                # One "moved '<file>' to '<folder>'" per file, the form moves are recorded and checked in: "moved
                # a.log into 'keep'" matched nothing, and the move looked undone (audit 09-26).
                return ExecResult(True, "; ".join(f"moved {m!r} to {folder.name!r}" for m in moved) + " (scripted)")
            if canon in (self._canon("cmd+up"), self._canon("cmd+[")):
                # Never above the workspace: at its top, "up" leaves the sandbox. It did, on a real-task run -- the
                # window showed the run directory, the projected controls vanished, and the planner flailed.
                if Path(os.path.realpath(str(folder))) == Path(os.path.realpath(str(self.ws))):
                    return ExecResult(False, "already at the top of the workspace; there is nothing above it to go to")
                # Up one level, done to the window itself: the chord needs Finder frontmost, and the window's
                # target is scriptable. Found by the folder it shows now, so this is the window being observed.
                parent = folder.parent
                title = self._q(self._window_title or folder.name)
                ok, out = self._osa('tell application "Finder"\n'
                                    '  repeat with i from 1 to (count of Finder windows)\n'
                                    '    set w to Finder window i\n'
                                    f'    if name of w is "{title}" and (POSIX path of (target of w as alias)) is "{self._q(str(folder))}/" then\n'
                                    f'      set target of w to (POSIX file "{self._q(str(parent))}" as alias)\n'
                                    '      return "ok"\n'
                                    '    end if\n'
                                    '  end repeat\n'
                                    '  return "none"\n'
                                    'end tell')
                if not ok or out != "ok":
                    return ExecResult(False, f"could not go up from {folder.name!r}: {out[:160]}")
                self._scripted_actions += 1
                time.sleep(0.8)
                return ExecResult(True, f"went up from {folder.name!r} to {parent.name!r} (scripted)")
            if canon == self._canon("option+cmd+v") and not self._copied:
                return ExecResult(False, "nothing has been copied: select the file (CLICK its row), KEY cmd+c, "
                                         "then paste at the destination")
            if canon in ("return", "escape") and self._just_created is None:
                return ExecResult(True, "nothing is waiting for confirmation (the last change was applied directly)")
        return None

    def _scripted_rename(self, name: str) -> ExecResult | None:
        """Name what the last scripted command created: the naming step the GUI does in an inline field."""
        if self._just_created is None or not self._just_created.exists():
            return None
        target = self._just_created
        if _bad_file_name(name):
            return ExecResult(False, _bad_file_name(name))
        ok, out = self._osa(f'tell application "Finder" to set name of (POSIX file "{self._q(str(target))}" as alias) '
                            f'to "{self._q(name)}"')
        if not ok:
            return ExecResult(False, f"could not rename {target.name!r} to {name!r}: {out[:160]}")
        self._just_created = None
        self._scripted_actions += 1
        # The undo removes the folder by its new path; left on the old one it offered to delete a folder that was
        # no longer there. And reported as the folder it now is: "named '未命名文件夹' 'archive'" was never read as
        # an effect, so the folder the goal asked for was never counted as made (audit 09-26).
        for u in reversed(self._undo):
            if u.get("kind") == "mkdir" and Path(u["path"]) == target:
                u["path"] = target.parent / name
                break
        return ExecResult(True, f"created folder {name!r} in {target.parent.name!r} (scripted, named from "
                                f"{target.name!r})")

    def _readable(self, app: str, wid: str, attempts: int = 6) -> bool:
        """Whether a window's accessibility tree can be read now, checked before switching to it.

        A background TextEdit window, switched to, sometimes stays "AX tree incomplete" past every retry of the
        next observation, and the run ended there as an environment failure, in several tasks
        and with several planners. Checked first, the switch is refused and the planner keeps a window it can
        see, told why."""
        probe = self.scratch / "focus-probe.png"
        for attempt in range(1, attempts + 1):
            ok, _, detail = self._run("see_window", app=app, window=wid, path=str(probe))
            if ok or "incomplete" not in (detail or "").lower():
                return True
            self._incomplete_reads += 1
            time.sleep(0.4 * attempt)
        # And no longer offered: refused with the reason, it was chosen again 16 times in one run.
        self.__dict__.setdefault("_unreadable", set()).add(str(wid))
        return False

    # -- observation ------------------------------------------------------

    #: A browser's own controls: a read with nothing but these has not got the page yet.
    BROWSER_CHROME_ROLES = {"button", "textfield", "image", "group", "window", "splitgroup", "tabgroup", "toolbar",
                            "menubutton", "scrollarea", "radiobutton", "checkbox", "popupbutton", "statictext_toolbar"}
    BROWSERS = {"com.apple.safari", "com.google.chrome", "com.microsoft.edgemac", "org.mozilla.firefox",
                "com.brave.browser", "company.thebrowser.browser"}

    def _page_not_read(self, data: dict | None) -> bool:
        """Whether a successful read of a browser window holds its controls only, not the page."""
        if self.app.lower() not in self.BROWSERS or not data:
            return False
        roles = [(e.get("role") or "").replace("AX", "").lower() for e in (data.get("ui_elements") or [])]
        return bool(roles) and all(r in self.BROWSER_CHROME_ROLES for r in roles)

    def observe(self) -> Observation:
        self._obs_n += 1
        FAULTS.observed(self._obs_n)
        obs_id = f"obs-{self._obs_n:04d}"
        shot = self.scratch / f"{obs_id}.png"
        # An incomplete accessibility read is transient and upstream says so in
        # words: "Retry once to obtain a fresh observation." Two attempts were
        # not enough -- a live run died after 19 productive actions because the
        # third read would have worked and was never made. The tree is typically
        # incomplete while a sheet animates, so the backoff grows rather than
        # hammering at a fixed interval.
        # The first read of a run gets longer: right after staging, Safari's page is often still loading, and five
        # tries over ~6 s were not enough -- G03 was scored "environment" in two of four diag rounds for it.
        first = self._obs_n == 1
        attempts = 8 if first else 5
        if self._vision_app() and self._pinned_window is None:
            # The main window, by size. The music app opened a 500x500 side window mid-session and `see` captured that
            # one from then on: every observation of four runs was the same 78 words, and the search results the
            # planner had asked for were never on it.
            ok_w, data_w, _ = self._run("window_list", app=self.app)
            wins = [w for w in ((data_w or {}).get("windows") or []) if ok_w and w.get("window_id") is not None]
            if wins:
                big = max(wins, key=lambda w: (w.get("bounds") or {}).get("width", 0) * (w.get("bounds") or {}).get("height", 0))
                self._pinned_window = str(big["window_id"])
        for attempt in range(1, attempts + 1):
            if FAULTS.once("capture_error", self._obs_n):
                ok, data, detail = False, {}, ("Failed to capture UI state: Desktop observation target changed during "
                                               "capture (injected fault)")
            elif self._pinned_window:
                ok, data, detail = self._run("see_window", app=self.app,
                                             window=self._pinned_window, path=str(shot))
            else:
                ok, data, detail = self._run("see", app=self.app, path=str(shot))
            if ok and attempt < attempts and self._page_not_read(data):
                # A browser whose page is not in the tree yet: WebKit builds it for the first client that asks, and the
                # first read got the toolbar and tabs only. Taken as the screen, the planner saw no table, went to the
                # document and wrote "parts.csv" into it fourteen times (D1 in the app).
                self._incomplete_reads += 1
                time.sleep((0.6 if first else 0.4) * attempt)
                continue
            if ok or not _transient_read(detail):
                break
            if self._vision_app():
                ok, data, detail = self._vision_capture(shot)   # no tree needed: the window's pixels are enough
                break
            self._incomplete_reads += 1
            if attempt == attempts:
                break
            time.sleep((0.6 if first else 0.4) * attempt)
        if not ok and self._pinned_window and "incomplete" in (detail or "").lower():
            # The action opened a window of its own and the pinned one stopped answering: follow the new window.
            # A double-click on a folder opened it in a second Finder window, and every read of the first came back
            # "AX tree incomplete" until the run ended as an environment failure -- nine diag runs in one night.
            fresh = fresh_windows(self._window_ids(), getattr(self, "_seen_windows", set()), self._pinned_window)
            if fresh:
                self._followed_windows = getattr(self, "_followed_windows", 0) + 1   # reported in usage()
                self._pinned_window = fresh[-1]
                ok, data, detail = self._run("see_window", app=self.app, window=self._pinned_window, path=str(shot))
        if ok:
            self._seen_windows = self._window_ids()
        if not ok:
            if "WINDOW_NOT_FOUND" in detail or "not found" in detail.lower():
                # An app with nothing open has no window. That is a state the
                # agent created and can undo, not an environment failure, so it
                # comes back as an observation saying so rather than as an
                # exception that would be scored as unavailability.
                self._snapshot = None
                fallback = Size(1512, 982)
                # Nothing open yet is where a request to open the folder's documents starts: they are still offered.
                self._window_title = ""
                docs = self._folder_documents() if PROJECTION and self.offer_folder_files else []
                self._labels_by_id = {e.id: e.label or "" for e in docs}
                self._element_role = {e.id: e.role or "" for e in docs}
                self._rects, self._element_value = {}, {e.id: "" for e in docs}
                return Observation(
                    id=obs_id,
                    geometry=ScreenGeometry(fallback, fallback),
                    transform=ImageTransform.identity(fallback),
                    screenshot_png=None, elements=docs,
                    focused_app=f"{self.app} (no window open)",
                    accepts_keys=False,
                    layout_version=self._obs_n)
            raise DriverUnavailable(f"see failed: {detail}")

        if not self._vision_app() and self._sparse_tree(data):
            # Seen for the first time with (almost) nothing in its tree: from now on this app is read from its pixels.
            self._auto_vision.add(self.app.lower())
        geometry, transform, origin = self._frames(data)
        self._last_geometry = geometry
        self._origin = origin
        self._seen_frame = self._window_frame()
        self._snapshot = data.get("snapshot_id")
        self._app_name = data.get("application_name", "")
        self._window_title = data.get("window_title") or ""
        win = (data.get("coordinate_context") or {}).get("window") or {}
        self._window_id = str(win.get("window_id", "")) or None
        if self._pin_window and self._pinned_window is None:
            self._pinned_window = self._window_id

        png = shot.read_bytes() if shot.exists() else None
        self._shot = shot
        elements = self._element_cache(data, origin)
        shot.unlink(missing_ok=True)
        self._last_sig = self._signature(elements, data.get("window_title") or "")

        return Observation(
            id=obs_id, geometry=geometry, transform=transform,
            screenshot_png=png,
            elements=elements,
            focused_app=self._app_name,
            window_title=data.get("window_title") or "",
            windows=self._windows(),
            dialog=bool(data.get("is_dialog")),
            notes=[n for n in (ax_truncation_note(data),) if n],
            accepts_keys=not self._vision_app(),
            # Peekaboo mints a new snapshot id per observation; reusing an old
            # one is exactly the staleness this field exists to detect.
            layout_version=self._obs_n,
        )

    def _frames(self, data: dict) -> tuple[ScreenGeometry, ImageTransform, Point]:
        """Derive the coordinate frames from what the capture actually reported."""
        cc = data.get("coordinate_context") or {}
        (x0, y0), (x1, y1) = cc.get("logical_bounds") or [[0, 0], [1440, 900]]
        iw, ih = cc.get("delivered_image_size") or [x1 - x0, y1 - y0]
        logical = Size(max(1, int(x1 - x0)), max(1, int(y1 - y0)))
        pixels = Size(max(1, int(iw)), max(1, int(ih)))
        # ScreenGeometry's two axes scale independently, which is required here:
        # the observed capture was 460x218 for an 820x374 window.
        return ScreenGeometry(logical, pixels), ImageTransform.identity(pixels), Point(x0, y0)

    def _element_cache(self, data: dict, origin: Point) -> list[Element]:
        """Elements, plus a id -> window-local rect map kept for the foreground fallback below."""
        if self._vision_app() and getattr(self, "_shot", None) is not None and self._last_geometry:
            # No accessibility tree to read: the screenshot's words (on-device OCR) and grounded icons instead.
            geo = self._last_geometry
            els = vision_elements(self.app, self._shot, geo.logical.w, geo.logical.h, self._app_name,
                                  self._window_id or "", absent=self.__dict__.setdefault("_gen_absent", set()),
                                  fields=self.__dict__.setdefault("_placed", {}))
            # Kept for placing a generic control the planner chooses later (the observation's own file is removed).
            keep = self.scratch / "_vision_last.png"
            try:
                keep.write_bytes(self._shot.read_bytes())
                self._vision_last = keep
            except OSError:
                self._vision_last = None
            self._rects = {e.id: e.rect for e in els if e.rect}
            self._element_window = {e.id: e.window_id for e in els}
            self._element_value = {e.id: e.value or "" for e in els}
            self._labels_by_id = {e.id: e.label or "" for e in els}
            self._element_role = {e.id: e.role or "" for e in els}
            return els
        els = self._elements(data, origin)
        # A scroll bar's value is settable in the accessibility tree (it is a position), and a planner looking for
        # somewhere to put a document's text wrote it into two of them. Not a text target.
        for e in els:
            if "scroll" in (e.role or "").lower() or "scroll" in (e.ax_role or "").lower():
                e.settable = False
        # The title bar's document menu (rename, move, lock, versions) opens a popover that blocks the document:
        # after a click on it, TextEdit's accessibility reads came back incomplete and scripted saves timed out
        # for the rest of the run. Nothing in a task needs it; it is window chrome.
        els = [e for e in els if not ((e.role or "").lower() in ("menubutton", "axmenubutton")
                                      and (e.label or "") in ("文稿操作", "Document Actions"))]
        if self.app.lower() == "com.apple.finder" and self._mcp is not None:
            # Say which rows are folders. To a planner every Finder row is the same text field, so it opened
            # files to "go into" them -- each open launched another app -- while the folder it wanted was the next
            # row down. The driver can see the file system; the mock desktop always said dir/file, this now does.
            self._window_title = data.get("window_title") or self._window_title
            folder = self._finder_folder()
            if folder is not None:
                for e in els:
                    if (e.role or "").lower() == "textfield" and e.label and (folder / e.label).exists():
                        e.role = "folder" if (folder / e.label).is_dir() else "file"
                # Pressing the window element itself raises the window -- the likeliest source of Finder sitting in
                # front for six uncounted seconds. Windows are switched with FOCUS_WINDOW, not clicked.
                els = [e for e in els if (e.role or "").lower() not in ("window", "axwindow")]
                # Sidebar favourites (Desktop, Documents, ...) are rows too, and opening one leaves the workspace.
                # Only the rows of the folder the window shows are offered; buttons and fields stay.
                els = [e for e in els if (e.role or "").lower() not in ("row", "cell", "axrow", "axcell",
                                                                         "outline", "listitem")]
                if PROJECTION:
                    els += self._project_finder(folder, els)
        elif (PROJECTION and self.app.lower() == "com.apple.textedit" and self._mcp is not None
              and self._window_title):
            els += [Element(id="syn:save", role="button", label="保存", synthetic=True, app=self._app_name,
                            window_id=self._window_id or "")]
            if self.offer_folder_files:
                els += [Element(id="syn:close", role="button", label="关闭", synthetic=True, app=self._app_name,
                                window_id=self._window_id or "")]
            els += self._project_textedit()
        elif self.app.lower() == CHAT_APP:
            els = self._chat_scope(els)
        if PROJECTION and self._mcp is not None and self.offer_folder_files and self.app.lower() != "com.apple.finder":
            els += self._folder_documents()
        if self.app.lower() in VOCAB and getattr(self, "_shot", None) is not None:
            geo = self._last_geometry
            if geo is not None:
                name_unnamed(self.app, els, self._shot, geo.logical.w, geo.logical.h)
        self._attach_popup_options(els, origin)
        self._rects = {e.id: e.rect for e in els if e.rect}
        self._element_window = {e.id: e.window_id for e in els}
        self._element_value = {e.id: e.value or "" for e in els}
        self._keep_baseline(els)
        self._labels_by_id = {e.id: e.label or "" for e in els}
        self._element_role = {e.id: e.role or "" for e in els}
        return els

    def _chat_flash_click(self, eid: str) -> ExecResult:
        """A chat-app button, clicked with the chat app brought to the front for about a second -- by the user's leave.

        Nothing reaches the chat app's buttons from the background: AXPress, Peekaboo's coordinate click (itself an AX
        press), Return by window-targeted events, CGEventPostToPid and SkyLight's SLEventPostToPid with the window
        made key all left the send button untouched (a hover tooltip did appear: moves arrive, clicks do not). So the
        click is a real one, only when the user is not in the chat app and has not touched the keyboard or mouse for 3 s:
        The chat app forward, one click at the button, the pointer put back, the user's app given the front again with
        `open -b` -- AppleScript `activate` of the user's app was refused from here and left the chat app in front 24 s."""
        label = self._labels_by_id.get(eid, "")
        if label.startswith("发送") and self._chat_sends >= 1:
            # Sending is outward and cannot be taken back. The 4B, not recognising its own sent message as the goal
            # met, wrote and sent the same message nine times in seven minutes (and brought the chat app forward each
            # time) until the run was killed by hand. One send per run; the next one is refused.
            return ExecResult(False, "a message was already sent in this run; sending is not repeated -- if the "
                                     "goal is met, finish (DONE)")
        if os.environ.get("HANDS_CHAT_FLASH") != "1":
            return ExecResult(False, "the chat app's buttons only respond to a foreground click, which this run is not "
                                     "allowed to make")
        import Quartz
        # The user being busy is not the planner's failure: told "not made", the 4B chose 发送 again, then wandered
        # into the message box until the run ended as a loop. The driver waits (up to 60 s) instead.
        deadline = time.time() + 60
        t_wait = time.time()
        while True:
            prev = self._front_bundle()
            idle = hid_idle_seconds()
            if prev != CHAT_APP and idle >= 3:
                break
            if time.time() > deadline:
                self._add_wait(time.time() - t_wait)
                return ExecResult(False, f"{USER_BUSY} (in the chat app, or typing); the click was not made -- wait")
            time.sleep(1)
        self._add_wait(time.time() - t_wait)
        moved = self._moved_since_seen()
        if moved:
            return ExecResult(False, moved, stale=True)
        r = self._rects[eid]
        x, y = self._origin.x + r.x + r.w / 2, self._origin.y + r.y + r.h / 2
        cur = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        t0 = time.time()
        self._foreground_actions += 1
        try:
            subprocess.run(["/usr/bin/open", "-b", CHAT_APP], capture_output=True, timeout=10)
            for _ in range(30):
                if self._front_bundle() == CHAT_APP:
                    break
                time.sleep(0.05)
            else:
                return ExecResult(False, "the chat app did not come to the front; nothing was clicked")
            time.sleep(0.15)
            for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                   Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft))
                time.sleep(0.05)
            time.sleep(0.2)
        finally:
            Quartz.CGWarpMouseCursorPosition(cur)
            Quartz.CGAssociateMouseAndMouseCursorPosition(True)
            for _ in range(20):
                subprocess.run(["/usr/bin/open", "-b", prev], capture_output=True, timeout=10)
                time.sleep(0.1)
                if self._front_bundle() == prev:
                    break
        self._snapshot = None
        if label.startswith("发送"):
            self._chat_sends += 1
        return ExecResult(True, f"clicked {self._labels_by_id.get(eid, eid)!r} (the chat app in front "
                                f"{time.time() - t0:.1f}s, then {prev} again)")

    @staticmethod
    def _hid_idle() -> float:
        """Seconds since the user last touched the keyboard, mouse or trackpad."""
        if time.time() < FAULTS.busy_until:
            return 0.0
        return hid_idle_seconds()

    @staticmethod
    def _takeover() -> bool:
        """The user has handed the screen over for a while (the app writes the end time, epoch seconds, to the file
        HANDS_TAKEOVER_FILE names): no waiting for them to stop, as a tool that owns the screen would."""
        path = os.environ.get("HANDS_TAKEOVER_FILE")
        try:
            return bool(path) and float(Path(path).read_text().strip() or 0) > time.time()
        except (OSError, ValueError):
            return False

    def _flash(self, bundle: str, perform, what: str = "") -> tuple[bool, str]:
        """Bring `bundle` forward for as long as `perform()` takes, then give the user's app the front back.

        For apps that ignore every background input (the chat app, the music app: AX, PostToPid and SkyLight
        clicks all do nothing), and only for apps the user allowed (HANDS_FLASH_APPS, comma-separated bundle ids).
        Never while the user is in that app or has touched the keyboard or mouse in the last 3 s. The driver waits
        for that -- up to HANDS_FLASH_WAIT_S (60 s by default; the app allows a long wait, since a person at their
        Mac is not a failure) -- and says so on stdout (`HANDS_WAIT`, then `HANDS_RESUME`) so the app can show
        "paused while you use your Mac". While the user has handed the screen over (_takeover) it does not wait.
        If the user touches anything while the app is being brought forward, the step is abandoned before any
        input is sent, the front is given back, and the wait starts again."""
        if self._background_input(bundle):
            # WebKit apps take the same clicks and keys delivered to their window in the background (see
            # skylight.py): nothing comes forward. The keyboard focus is borrowed for the moment the input takes,
            # though -- the user's app keeps the front but not the keys -- so it is not borrowed while they type or
            # move the mouse, and it is given back straight after. Kept, it left the user typing into nothing.
            waited = self._wait_until_still(bundle, what)
            if waited is not None:
                return False, waited
            bg = self._background_quartz(bundle)
            if bg is not None:
                q, token = bg
                from deskmind_hands.drivers import skylight
                t0 = time.time()
                try:
                    detail = perform(q)
                finally:
                    skylight.give_focus_back(token)
                return True, f"{detail} (in the background, {bundle} not brought forward, {time.time() - t0:.1f}s)"
        allowed = {b.strip().lower() for b in os.environ.get("HANDS_FLASH_APPS", "").split(",") if b.strip()}
        if bundle.lower() not in allowed:
            return False, f"{bundle} only responds in the foreground, which this run is not allowed to use"
        import Quartz
        deadline = time.time() + float(os.environ.get("HANDS_FLASH_WAIT_S", "60"))
        waited = False
        # How long the user must have been still. It doubles each time they touch something while the app is being
        # brought forward (to 30 s): at a flat 3 s the app jumped forward and back again every few seconds while
        # someone was reading and nudging the mouse now and then.
        still = 3.0
        # Watching, not working: in the app that started the run (HANDS_SPECTATOR_APPS) the user is watching it, and
        # in the target app itself they are looking at what it will change. Both waited for a still mouse like any
        # other app, and a run watched from DeskMind's own window sat "paused" at its first step.
        spectator = {b.strip().lower() for b in os.environ.get("HANDS_SPECTATOR_APPS", "").split(",") if b.strip()}
        while True:
            prev = self._front_bundle()
            watching = prev.lower() in spectator or prev.lower() == bundle.lower()
            if self._takeover() or self._hid_idle() >= (1.0 if watching else still):
                cur = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
                t0 = time.time()
                subprocess.run(["/usr/bin/open", "-b", bundle], capture_output=True, timeout=10)
                for _ in range(30):
                    if self._front_bundle().lower() == bundle.lower():
                        break
                    time.sleep(0.05)
                else:
                    self._give_front_back(prev)
                    return False, f"{bundle} did not come to the front; nothing was done"
                time.sleep(0.15)
                if not self._takeover() and not watching and self._hid_idle() < 0.3:
                    # Touched while it came forward: nothing has been sent yet, so step back and wait again.
                    self._give_front_back(prev)
                    waited = self._announce_wait(waited, bundle, what)
                    still = min(still * 2, 30.0)
                    continue
                if waited:
                    self._resumed()
                self._foreground_actions += 1
                ime = _ascii_input_source()
                try:
                    detail = perform(Quartz)
                finally:
                    ime.restore()
                    Quartz.CGWarpMouseCursorPosition(cur)
                    Quartz.CGAssociateMouseAndMouseCursorPosition(True)
                    if prev.lower() != bundle.lower():
                        self._give_front_back(prev)
                return True, f"{detail} ({bundle} in front {time.time() - t0:.1f}s, then {prev} again)"
            if time.time() > deadline:
                if waited:
                    self._resumed()
                return False, f"{USER_BUSY}; the step was not carried out -- wait"
            waited = self._announce_wait(waited, bundle, what)
            time.sleep(1)

    #: Apps whose windows take clicks and keys in the background (WebKit: Safari, the gym's host). Measured on 09-30;
    #: CEF and Electron apps (the music app, the chat app) ignore them and keep the brief foreground.
    #: HANDS_BG_INPUT_APPS adds bundle ids; HANDS_BG_INPUT=0 turns the route off.
    BACKGROUND_INPUT_APPS = {"com.apple.safari", "ai.deskmind.gymhost"}

    def _background_input(self, bundle: str) -> bool:
        if os.environ.get("HANDS_BG_INPUT", "1") == "0":
            return False
        extra = {b.strip().lower() for b in os.environ.get("HANDS_BG_INPUT_APPS", "").split(",") if b.strip()}
        if bundle.lower() not in self.BACKGROUND_INPUT_APPS | extra:
            return False
        from deskmind_hands.drivers import skylight
        return skylight.available()

    @staticmethod
    def _pid_of(bundle: str, all_wins=None) -> int | None:
        """The process of `bundle` that has windows, asked afresh (the running-apps list goes stale in a process
        without a run loop); the newest if an old one is still going away."""
        import Quartz
        from AppKit import NSRunningApplication
        if all_wins is None:
            all_wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0) or []
        pids = {w.get("kCGWindowOwnerPID") for w in all_wins if w.get("kCGWindowLayer") == 0}
        mine = [p for p in pids if p and (getattr(NSRunningApplication.runningApplicationWithProcessIdentifier_(p),
                                                    "bundleIdentifier", lambda: None)() or "").lower() == bundle.lower()]
        return max(mine) if mine else None

    #: A pop-up's own prompt, not a choice: "Choose…", "Select…", "请选择".
    POPUP_PROMPT = re.compile(r"^(?:choose|select|pick|please select|请选择|--)|[…]$|\.\.\.$", re.I)

    def _attach_popup_options(self, els: list[Element], origin: Point) -> None:
        """A pop-up button (a web page's <select>, an AppKit pop-up) carries its choices, read through
        accessibility, so the planner can SELECT one: read as a plain button with only its current value, the
        expense form's category could never be chosen (D5). Cached per window, label and place; the menu is opened
        and closed once to read them, in the background."""
        cands = [e for e in els if (e.ax_role or "").lower() == "axpopupbutton" and not e.options and e.rect]
        self._popups = {}
        if not cands or os.environ.get("HANDS_AX_POPUPS", "1") == "0" or self._vision_app():
            return
        from deskmind_hands.drivers import axpopup
        try:
            pid = self._pid_of(self.app)
            found = axpopup.popups(pid) if pid else []
        except Exception:   # noqa: BLE001 -- a pop-up that cannot be read keeps the choices it had, if any
            found = []
        frames = [(i, (origin.x + e.rect.x, origin.y + e.rect.y, e.rect.w, e.rect.h)) for i, e in enumerate(cands)]
        cache = self.__dict__.setdefault("_popup_options", {})
        matched = axpopup.match(frames, found, titles={i: e.label for i, e in enumerate(cands)})
        for i, frame in frames:
            if i in matched:
                continue
            # Not found this look (the accessibility read came back without it): the choices this window's pop-up of
            # that label had stay on it, and choosing re-reads the pop-up at the element's place. Left optionless,
            # the expense category could not be chosen at the last step and an oracle drive gave up (10-02).
            e = cands[i]
            known = {tuple(v) for k, v in cache.items() if k[:2] == (e.window_id, e.label) and v}
            if len(known) == 1:
                e.options = list(next(iter(known)))
                self._popups[e.id] = frame
        for i, pop in matched.items():
            e = cands[i]
            key = (e.window_id, e.label, round(pop["frame"][0]), round(pop["frame"][1]))
            if key not in cache:
                # Reading the choices opens the menu for a moment, and an open menu has the keyboard: not while the
                # user is typing or moving the mouse. Waited for, as every background input is (and said so on
                # stdout): skipped instead, a select stayed optionless for as long as the user kept working, and a
                # run whose next step was to choose the category could not -- an oracle drive gave up on it as
                # unreachable (09-30). Only a wait that runs out leaves it for the next look.
                if self._wait_until_still(self.app, f"read the choices of {e.label!r}") is not None:
                    continue
                cache[key] = [o for o in axpopup.options(pop["el"]) if not self.POPUP_PROMPT.search(o)]
            if cache[key]:
                e.options = list(cache[key])
                self._popups[e.id] = pop["frame"]

    def _select_popup(self, eid: str, option: str) -> ExecResult:
        from deskmind_hands.drivers import axpopup
        frame = self._popups.get(eid)
        pid = self._pid_of(self.app)
        pop = axpopup.match([(0, frame)], axpopup.popups(pid) if pid else [],
                            titles={0: self._labels_by_id.get(eid, "")}).get(0) if frame else None
        if pop is None:
            return ExecResult(False, f"{self._labels_by_id.get(eid, eid)!r} is no longer on screen; observe again")
        waited = self._wait_until_still(self.app, f"choose {option!r}")
        if waited is not None:
            return ExecResult(False, waited)
        ok, detail = axpopup.select(pop["el"], option)
        return self._settle(ok, f"{detail} in {self._labels_by_id.get(eid, eid)!r} (in the background)")

    def _wait_until_still(self, bundle: str, what: str = "") -> str | None:
        """Wait (up to HANDS_FLASH_WAIT_S) until the user has not touched the keyboard or mouse for a moment, saying
        so on stdout as the flash does; None when it is fine to go on, else why not. No wait while the user has
        handed the screen over (_takeover)."""
        if self._takeover():
            return None
        deadline = time.time() + float(os.environ.get("HANDS_FLASH_WAIT_S", "60"))
        still = float(os.environ.get("HANDS_BG_STILL_S", "5"))
        waited = False
        while self._hid_idle() < still:
            if time.time() > deadline:
                if waited:
                    self._resumed()
                return f"{USER_BUSY}; the step was not carried out -- wait"
            waited = self._announce_wait(waited, bundle, what)
            time.sleep(0.5)
        if waited:
            self._resumed()
        return None

    def _background_quartz(self, bundle: str):
        """A Quartz stand-in that delivers to `bundle`'s window (the pinned one, else its largest), focused without
        being raised; None when the app or window cannot be found."""
        import Quartz
        from AppKit import NSRunningApplication
        from deskmind_hands.drivers import skylight
        # The app's process from the windows on screen, each asked for its bundle: the running-apps list is read
        # once per process and goes stale, and the gym's host, relaunched for every task, was not in it -- its first
        # step (the search) took the foreground.
        all_wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0) or []
        pid = self._pid_of(bundle, all_wins)
        if pid is None:
            return None
        wins = [w for w in all_wins if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0]
        pinned = [w for w in wins if str(w.get("kCGWindowNumber")) == str(self._pinned_window or self._window_id)]
        if not (pinned or wins):
            return None
        w = (pinned or sorted(wins, key=lambda w: -w["kCGWindowBounds"]["Width"] * w["kCGWindowBounds"]["Height"]))[0]
        wid, b = int(w["kCGWindowNumber"]), w["kCGWindowBounds"]
        token = skylight.focus_without_raise(pid, wid)
        if token is None:
            return None
        return skylight.BackgroundQuartz(pid, wid, (b["X"], b["Y"])), token

    def _announce_wait(self, already: bool, bundle: str = "", what: str = "") -> bool:
        """Say once that the step is waiting for the user, and what it will do: a paused run that did not say what
        it was about to do read as frozen. The wait is timed from here (see _resumed)."""
        if not already:
            self._wait_t0 = time.time()
            print("HANDS_WAIT " + json.dumps({"reason": "user_active", "app": bundle, "what": what},
                                             ensure_ascii=False), flush=True)
        return True

    def _add_wait(self, seconds: float) -> None:
        global WAITED_S
        self.user_wait_s = getattr(self, "user_wait_s", 0.0) + seconds
        WAITED_S = self.user_wait_s

    def _resumed(self) -> None:
        """The wait is over: said on stdout, and its length added to user_wait_s -- time the run spent on the user, not
        on the task, which the run's wall clock does not count. Counted, it ran gym tasks out of time while the user
        worked at the Mac: 3 of 9 expense runs ended "wall clock 385 s > 360 s" after a few steps (10-01)."""
        t0 = self.__dict__.pop("_wait_t0", None)
        if t0 is not None:
            self._add_wait(time.time() - t0)
        print("HANDS_RESUME", flush=True)

    def _give_front_back(self, prev: str) -> None:
        for _ in range(20):
            subprocess.run(["/usr/bin/open", "-b", prev], capture_output=True, timeout=10)
            time.sleep(0.1)
            if self._front_bundle() == prev:
                break

    @staticmethod
    def _post_click(Q, x: float, y: float, count: int = 1) -> None:
        """A left click at (x, y); count=2 is the second click of a double click. Its events must say so (the click
        state): two plain clicks are two single clicks to every app, and a vision double-click opened nothing -- a
        song row never played, a gym card never started."""
        kinds = (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp) if count > 1 else \
            (Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp)
        for kind in kinds:
            ev = Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft)
            if kind != Q.kCGEventMouseMoved:
                Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, count)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.05 if count == 1 else 0.02)

    #: The key codes this driver presses: A and V (with cmd: select all, paste), Return.
    _KEY_CODES = (0, 9, 36)
    #: The key events, serialized: "<key code>:<1 down | 0 up>" -> bytes. Made once per process (see _key_event).
    _key_blobs: dict = {}
    _KEY_CHILD = (
        "import base64, json, Quartz as Q\n"
        "print(json.dumps({f'{k}:{int(d)}': base64.b64encode(bytes(Q.CGEventCreateData(None,"
        " Q.CGEventCreateKeyboardEvent(None, k, d)))).decode() for k in %r for d in (True, False)}))\n"
    )

    @classmethod
    def _key_event(cls, Q, keycode: int, down: bool):
        """A key event for `keycode`, rebuilt from one a child process made.

        Not CGEventCreateKeyboardEvent here: it reads the keyboard layout, and asking Text Input Sources anything
        registers the asking process with LaunchServices as an app -- after which every process it starts (peekaboo,
        osascript) leaves a Dock tile for the app responsible for the run (see _ascii_input_source). The first
        background paste of a gym episode did that, and four episodes left 48 tiles. A child asks once, for every
        key this driver presses, and hands the events back as data; rebuilding them asks nothing. An event built by
        hand (type, key code, characters) is not the same thing: cmd+A and cmd+V built that way did nothing in a web
        view. If the child cannot be run, the events are made here as before."""
        if not cls._key_blobs:
            import base64
            try:
                out = subprocess.run([sys.executable, "-c", cls._KEY_CHILD % (cls._KEY_CODES,)], capture_output=True,
                                     text=True, timeout=20).stdout
                cls._key_blobs = {k: base64.b64decode(v) for k, v in json.loads(out).items()}
            except (OSError, ValueError, subprocess.SubprocessError):
                cls._key_blobs = {"failed": b""}
        raw = cls._key_blobs.get(f"{keycode}:{int(down)}")
        if not raw:
            return Q.CGEventCreateKeyboardEvent(None, keycode, down)
        from Foundation import NSData
        return Q.CGEventCreateFromData(None, NSData.dataWithBytes_length_(raw, len(raw)))

    @classmethod
    def _post_key(cls, Q, keycode: int, flags: int = 0) -> None:
        for down in (True, False):
            ev = cls._key_event(Q, keycode, down)
            if flags:
                Q.CGEventSetFlags(ev, flags)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.03)

    @classmethod
    def _post_text(cls, Q, text: str) -> None:
        for i in range(0, len(text), 16):
            chunk = text[i:i + 16]
            for down in (True, False):
                ev = cls._key_event(Q, 0, down)
                Q.CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
                Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.03)

    #: Apps found at run time to show no accessibility structure: observed and acted on through the screenshot
    #: (OCR, grounded icons, the foreground flash) like the ones VISION_APPS names in advance.
    _auto_vision: set = set()   # class-level on purpose: learnt once per process, for every run after

    def _vision_app(self, app: str | None = None) -> bool:
        a = (app or self.app).lower()
        # HANDS_VISION_APPS=<bundle,...>: through the screenshot even when the tree is rich (the gym's vision rows).
        forced = {x.strip().lower() for x in os.environ.get("HANDS_VISION_APPS", "").split(",") if x.strip()}
        return a in VISION_APPS or a in self._auto_vision or a in forced

    #: Fewer accessibility elements than this in a whole window, besides its close/minimise/zoom buttons, is an app
    #: whose content is drawn rather than described (Chromium Embedded, games, canvases): the music app has exactly one.
    SPARSE_TREE = 3

    def _sparse_tree(self, data: dict) -> bool:
        if self.app.lower() in ("com.apple.finder", "com.apple.textedit"):
            return False
        chrome = ("close button", "zoom button", "minimize button", "full screen button", "关闭按钮",
                  "缩放按钮", "最小化按钮", "全屏幕按钮")
        rows = [e for e in (data.get("ui_elements") or [])
                if (e.get("label") or e.get("title") or "").strip().lower() not in chrome
                and (e.get("role") or "").lower() not in ("axwindow", "window", "axgroup", "group")]
        return len(rows) < self.SPARSE_TREE

    def _vision_act(self, action: Action, submit: bool = True) -> ExecResult:
        """Act on an element seen in the screenshot: a click at its centre, or -- for the search box -- click, select
        all, type, Return. Only in the foreground (see _flash)."""
        eid = action.binding.element_id or ""
        generic = eid.startswith(("gen:", "desc:"))
        clicky = action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
        placed = self.__dict__.setdefault("_placed", {})     # generic field name -> where it was found and used
        before = None
        if generic and eid[4:] in placed:
            self._rects[eid] = placed[eid[4:]]
            generic_found = True
        else:
            generic_found = False
        if generic and not generic_found:
            # Placed now, on the screen the planner chose it from. A described target's grounding query is its value.
            name = self._element_value.get(eid) if eid.startswith("desc:") else eid[4:]
            name = name or eid
            shot, geo = getattr(self, "_vision_last", None), self._last_geometry
            pt = ground_one(shot, geo.logical.w, geo.logical.h, GENERIC_CONTROLS.get(name, name)) \
                if shot is not None and geo else None
            if pt is None:
                return ExecResult(False, f"could not place {name!r} on the screen (the grounder did not answer)")
            self._rects[eid] = Rect(pt[0] - 10, pt[1] - 10, 20, 20)
            before = self._window_digest()
        r = self._rects.get(eid)
        if r is None:
            return ExecResult(False, f"{eid} is not on the current screen; observe again")
        if before is None and clicky and eid not in ("icon:scroll_down", "icon:scroll_up"):
            # Every click in a screenshot-only app is checked against the pixels: text is not always a control, and
            # a planner clicked a page heading and the search box's own text thirty times, each "clicked", while
            # nothing on screen moved and nothing told it so.
            before = self._window_digest()
        moved = self._moved_since_seen()
        if moved:
            return ExecResult(False, moved, stale=True)
        x, y = self._origin.x + r.x + r.w / 2, self._origin.y + r.y + r.h / 2
        label = self._labels_by_id.get(eid, eid)
        # The item the previous step opened, opened again straight after: not done. A second double-click on a song
        # that had just started playing paused it, the screen check did not see the small play icon change, and the
        # run said DONE over a paused song. Anything else done in between and it may be opened again.
        last_open = self.__dict__.pop("_last_open", None)
        if action.kind is ActionKind.DOUBLE_CLICK and eid.startswith("ocr:") and last_open \
                and self._same_spot(last_open, label, r):
            self._last_open = last_open
            # Not a failure: the item is open, which is what opening it asks for. Told it failed, the planner tried
            # the row beside it (the studio cut), which made the live one openable again, and went round for 14 steps.
            return ExecResult(True, f"{label!r} is already open (playing) -- the previous step opened it -- so it was "
                                    f"not clicked again (that would undo it)")
        # Scrolling these two is what they are for: a planner that chose SCROLL on "向下滚动页面" was told the
        # operation did not apply, and in the app that ended the run.
        if eid in ("icon:scroll_down", "icon:scroll_up") and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK,
                                                                           ActionKind.SCROLL):
            lines = -8 if eid.endswith("down") else 8
            def perform(Q):
                Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y),
                                                                          Q.kCGMouseButtonLeft))
                for _ in range(3):
                    Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine,
                                                                                    1, lines))
                    time.sleep(0.05)
                time.sleep(0.3)
                return f"scrolled {'down' if lines < 0 else 'up'}"
        elif action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            def perform(Q):
                self._post_click(Q, x, y)
                if action.kind is ActionKind.DOUBLE_CLICK:
                    self._post_click(Q, x, y, count=2)
                time.sleep(0.25)
                return f"clicked {label!r}"
        elif action.kind is ActionKind.TYPE_TEXT and self._element_role.get(eid, "").lower() in ("textfield",
                                                                                              "textarea"):
            # Return submits a search box; in a text area (a reply, a note) it would add a line instead.
            submit = submit and self._element_role.get(eid, "").lower() == "textfield"
            text = action.text or ""
            def perform(Q):
                # Pasted, not typed: with the user's Sogou Pinyin input method active, synthesized key events went
                # to the IME and nothing reached the box -- nine runs searched for "". The user's clipboard,
                # every item and type, is put back straight after.
                from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString
                pb = NSPasteboard.generalPasteboard()
                saved = [{t: it.dataForType_(t) for t in it.types()} for it in (pb.pasteboardItems() or [])]
                try:
                    pb.clearContents()
                    pb.setString_forType_(text, NSPasteboardTypeString)
                    self._post_click(Q, x, y)
                    time.sleep(0.2)
                    self._post_key(Q, 0, Q.kCGEventFlagMaskCommand)  # cmd+A: replace what is there
                    self._post_key(Q, 9, Q.kCGEventFlagMaskCommand)  # cmd+V
                    # Long enough for the paste to land and cmd to be released before Return: at 0.15 s a web
                    # page's search box took the pasted query and never searched.
                    time.sleep(0.4)
                    if submit:
                        self._post_key(Q, 36)                        # Return: a search box searches on Enter
                        time.sleep(0.4)
                finally:
                    pb.clearContents()
                    items = []
                    for d in saved:
                        item = NSPasteboardItem.alloc().init()
                        for t, data in d.items():
                            if data is not None:
                                item.setData_forType_(data, t)
                        items.append(item)
                    if items:
                        pb.writeObjects_(items)
                return f"entered {text!r} into {label!r}" + (" and pressed Return" if submit else "")
        else:
            return ExecResult(False, f"{action.kind.value} does not apply to {label!r}: click it, or type into "
                                     f"the search box")
        kind = action.kind.value.replace("_", " ")
        what = (f"type {action.text!r} into {label!r}" if action.kind is ActionKind.TYPE_TEXT
                else f"{kind} {label!r}")
        ok, detail = self._flash(self.app, perform, what=what)
        self._snapshot = None
        time.sleep(0.8)                                              # let the page update before the next look
        absent = self.__dict__.setdefault("_gen_absent", set())
        if ok and before is not None and self._window_digest() == before:
            shown = self._labels_by_id.get(eid, eid)
            if generic and not generic_found:
                # Nothing moved: most likely there is no such control here, and the grounder pointed at the nearest
                # thing. Said plainly, and not offered again in this run.
                absent.add(shown if eid.startswith("desc:") else eid[4:])
                return ExecResult(False, f"clicked where {shown!r} would be and nothing on screen changed: there is "
                                         f"probably no such control here, so it is no longer offered")
            if eid.startswith("ocr:") and self._took_effect_before(shown, self._rects.get(eid)):
                # The same words, in the same place, changed the screen when clicked earlier in this run: a second
                # click on a song already playing or a tab already open changes nothing. Not text -- told for text,
                # the planner went to a neighbour and undid what the first did.
                if action.kind is ActionKind.DOUBLE_CLICK:
                    # An item opened again (a song, a file): kept on offer. Withdrawn, the row beside it -- the
                    # studio cut next to the live one -- was the planner's next pick, and the two were swapped back
                    # and forth for five steps.
                    return ExecResult(False, f"opened {shown!r} again and nothing on screen changed: it is already "
                                             f"open (playing) since it was opened before -- check whether the goal is "
                                             f"done")
                # A control pressed again (a search button that has searched): withdrawn, or it was pressed until the
                # run stalled.
                absent.add("text:" + shown)
                return ExecResult(False, f"clicked {shown!r} again and nothing on screen changed: it already took "
                                         f"effect when it was clicked before, so it is no longer offered -- check "
                                         f"whether the goal is done, or go on to the next step")
            if eid.startswith("ocr:"):
                # Words that do nothing when clicked (a heading, a label, a field's own text): said, and not offered
                # again in this run, so the planner has to pick something else.
                absent.add("text:" + shown)
                return ExecResult(False, f"clicked {shown!r} and nothing on screen changed: it is text, not a "
                                         f"control; it is no longer offered -- choose something else")
            return ExecResult(False, f"clicked {shown!r} and nothing on screen changed")
        if ok and before is not None and eid.startswith("ocr:") and eid in self._rects:
            self.__dict__.setdefault("_ocr_worked", []).append((self._labels_by_id.get(eid, eid), self._rects[eid]))
            if action.kind is ActionKind.DOUBLE_CLICK:
                self._last_open = (self._labels_by_id.get(eid, eid), self._rects[eid])
        if ok and generic and action.kind is ActionKind.TYPE_TEXT:
            # The field is where the grounder put it; kept, so its text is read as its value from now on and it is
            # not placed again (see vision_elements' `fields`).
            placed[eid[4:]] = self._rects[eid]
        return ExecResult(ok, detail)

    #: How far apart (points) two readings of the same words may be, and how alike their text, to be the same thing:
    #: OCR ids change between looks and a character or two of a line can read differently each time.
    SAME_SPOT_PX = 12
    SAME_SPOT_TEXT = 0.6

    def _same_spot(self, seen: tuple, label: str, rect) -> bool:
        """Whether (label, rect) is the thing `seen` was: the same place, near enough the same words."""
        import difflib
        lab, r = seen
        if not rect or not r:
            return False
        return (abs(r.x + r.w / 2 - (rect.x + rect.w / 2)) <= self.SAME_SPOT_PX
                and abs(r.y + r.h / 2 - (rect.y + rect.h / 2)) <= self.SAME_SPOT_PX
                and difflib.SequenceMatcher(None, lab, label).ratio() >= self.SAME_SPOT_TEXT)

    def _took_effect_before(self, label: str, rect) -> bool:
        """Whether a click earlier in this run on the same words at the same place changed the screen."""
        return any(self._same_spot(seen, label, rect) for seen in self.__dict__.get("_ocr_worked", []))

    def screen_rect(self, element_id: str) -> list[float] | None:
        """Where an element of the last observation is on the screen: [x, y, w, h], points, top-left origin."""
        r = self._rects.get(element_id or "")
        return r.on_screen(self._origin) if r is not None else None

    def _window_digest(self) -> bytes | None:
        """A coarse fingerprint of the pinned window's pixels, to tell whether a click changed anything."""
        wid = self._pinned_window or self._window_id
        if not wid:
            return None
        tmp = self.scratch / "_digest.png"
        from .capture import capture_window
        if not capture_window(wid, tmp):
            return None
        from PIL import Image
        with Image.open(tmp) as im:
            small = im.convert("L").resize((48, 32))
            # Quantized, so a blinking caret or an animated spinner is not "a change".
            data = bytes(v // 24 for v in small.tobytes())
        tmp.unlink(missing_ok=True)
        return data

    def _front_bundle(self) -> str:
        return subprocess.run(["osascript", "-e", 'tell application "System Events" to get bundle identifier of '
                               'first process whose frontmost is true'], capture_output=True, text=True,
                              timeout=10).stdout.strip()

    def _vision_capture(self, shot: Path) -> tuple[bool, dict, str]:
        """A screenshot of the pinned window without the accessibility read, for apps observed by their pixels.
        The music app's one-element tree sometimes came back "incomplete", and `see` then returned nothing at all: two
        runs ended as environment failures before their first step. `screencapture -l` takes a window by its id,
        in the background."""
        import Quartz
        wid = self._pinned_window
        info = next((w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
                     if str(w.get("kCGWindowNumber")) == str(wid)), None)
        if not wid or info is None:
            return False, {}, "no window to capture"
        from .capture import capture_window
        if not capture_window(wid, shot):
            return False, {}, "screencapture failed"
        from PIL import Image
        b = info["kCGWindowBounds"]
        W, H = int(b["Width"]), int(b["Height"])
        with Image.open(shot) as im:
            if im.size != (W, H):
                im.resize((W, H)).save(shot)
        data = {"snapshot_id": f"vision-{self._obs_n}", "ui_elements": [],
                "coordinate_context": {"logical_bounds": [[b["X"], b["Y"]], [b["X"] + W, b["Y"] + H]],
                                       "delivered_image_size": [W, H], "window": {"window_id": int(wid)}},
                "application_name": self._app_name or self.app, "window_title": ""}
        return True, data, ""

    def _chat_scope(self, els: list[Element]) -> list[Element]:
        """The chat app, cut down to the configured conversation. The window also shows the chat list -- other chats' names and their
        latest messages -- and what the planner sees goes to a cloud model. Only the open conversation's pane (and
        the navigation rail and window buttons, which carry no content) is kept, and only when that conversation
        is the configured one; anywhere else the run stops before the planner sees anything."""
        header = [e for e in els if (e.label or "").strip() == CHAT_CONVERSATION and e.rect
                  and e.rect.y < 60 and e.rect.x > 300]
        rail = [e for e in els if e.rect and (e.rect.x + e.rect.w <= 70 or e.rect.y + e.rect.h <= 26)]
        if not header:
            # Not the configured conversation: the user has moved the chat app elsewhere. Offered the rail alone, a
            # planner clicked a navigation tab and gave up; a run has nothing to do here, so it stops as an environment problem.
            raise DriverUnavailable(f"the chat app is not showing {CHAT_CONVERSATION!r}; leave that conversation open")
        left = header[0].rect.x - 40
        pane = [e for e in els if e.rect and e.rect.x >= left and e not in rail]
        for e in pane:
            if (e.ax_role or "").lower() == "axtextarea" and e.settable:
                # The message box's accessibility label is its own content (it read back the draft text itself
                # for a draft), and an empty box holds zero-width spaces on blank lines. Read raw, the planner treated
                # "\u200b\n...\u200b" as text already there and replaced into it five times.
                e.label = "消息输入框"
                # An empty box also reads back as its placeholder (`chat.placeholders` in the apps file).
                e.value = "\n".join(l for l in (e.value or "").replace("\u200b", "").splitlines()
                                    if l.strip() and l.strip() not in CHAT_PLACEHOLDERS)
        return rail + pane

    def _elements(self, data: dict, origin: Point) -> list[Element]:
        """Convert Peekaboo elements, keeping bounds in the window-local frame.

        Bounds arrive in global display points; the rest of the harness works in
        the window's own logical space, so the window origin comes off here --
        once, in the one place that knows both frames.
        """
        out: list[Element] = []
        for e in data.get("ui_elements") or []:
            b = e.get("bounds") or {}
            rect = None
            if b:
                rect = Rect(float(b.get("x", 0)) - origin.x, float(b.get("y", 0)) - origin.y,
                            float(b.get("width", 0)), float(b.get("height", 0)))
            out.append(Element(
                id=e.get("id", ""),
                role=e.get("role") or e.get("ax_role", ""),
                ax_role=e.get("ax_role", ""),
                settable=bool(e.get("is_value_settable", False)),
                focused=bool(e.get("is_selected", False)),
                # Labels come back in the system language. That is a feature for
                # a Chinese-language product: the structured channel is already
                # Chinese text, with no OCR in the path.
                label=e.get("label") or e.get("title") or e.get("role_description", ""),
                rect=rect,
                value=e.get("value"),
                enabled=bool(e.get("is_enabled", True)),
                app=self._app_name,
                window_id=self._window_id or "",
            ))
        if os.environ.get("HANDS_NAME_ROWS") == "1":
            _name_rows(out)
        return out

    # -- execution --------------------------------------------------------

    #: Actions that address something *inside* the observed window and so need
    #: the snapshot the observation minted. Everything else addresses an
    #: application or a window by name or id, and must stay available even when
    #: there is no snapshot -- see the guard below.
    NEEDS_SNAPSHOT = frozenset({
        ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK,
        ActionKind.TYPE_TEXT, ActionKind.KEY, ActionKind.SCROLL, ActionKind.DRAG,
        ActionKind.MENU,
    })

    def _ours(self, name: str) -> bool:
        """Is this frontmost-app name one of the apps this task drives?"""
        n = (name or "").lower()
        return n in {"finder", "访达", "textedit", "文本编辑", "safari", "safari浏览器"} or n == self.app.lower()

    #: Opt-in (HANDS_CONFIRM_BY_DIFF=1): a click the platform could not confirm is checked by observing once more
    #: and comparing the window with how it was before. SwiftUI buttons answer every background click with "did not
    #: return a confirmed outcome" and act on it anyway -- DeskMind's own Download, Start and language menu did, each
    #: time. Off by default: it costs an observation per such click and changes what a diag run records.
    CONFIRM_BY_DIFF = os.environ.get("HANDS_CONFIRM_BY_DIFF") == "1"
    _last_sig: tuple | None = None

    @staticmethod
    def _signature(elements, title: str) -> tuple:
        """What a click can change that a person would notice: the window's title and each element's role, label and
        value. Not positions, which shift with any relayout."""
        return (title, tuple(sorted((e.role or "", e.label or "", str(e.value or "")) for e in elements)))

    def _confirm_by_diff(self, res: ExecResult) -> ExecResult:
        before = self._last_sig
        if before is None:
            return res
        time.sleep(0.3)   # a SwiftUI state change lands on the next runloop turn, not with the click's reply
        self._snapshot = None
        self.observe()
        after = self._last_sig
        if after is None or after == before:
            return res
        changed = len(set(after[1]) ^ set(before[1]))
        what = "the window title changed" if after[0] != before[0] else f"{changed} elements changed"
        return ExecResult(True, f"click confirmed by the window changing ({what}); the platform's own "
                                f"confirmation was missing. {res.detail[:200]}")

    def execute(self, action: Action) -> ExecResult:
        if self.CONFIRM_BY_DIFF and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK):
            res = self._execute_outer(action)
            return self._confirm_by_diff(res) if res.indeterminate else res
        return self._execute_outer(action)

    def _execute_outer(self, action: Action) -> ExecResult:
        if self._mcp is None:
            return self._execute_inner(action)
        # Checked after every action, not assumed. The same background click on a Finder row left the user's app
        # in front in one test and put Finder in front for 100 seconds in a later run, counted as nothing. Now a
        # front taken by one of this task's apps is counted the moment it happens and handed straight back.
        before = self._frontmost()
        t0 = time.time()
        wins_before = self._task_windows()
        res = self._execute_inner(action)
        after = self._frontmost()
        if before and after and after != before and self._ours(after) and not self._ours(before):
            self._foreground_actions += 1
            self._osa(f'tell application "{self._q(before)}" to activate')
            res = replace(res, detail=f"{res.detail} [took the foreground ({after}); given back to {before}]")
        elif (before and after and after != before and not self._ours(before) and not action.foreground
              and self._hid_idle() >= time.time() - t0):
            # Something else took the front while the step ran -- a helper app, a notification, an app the click
            # launched -- and the user did not do it (untouched since the step began): the user's app gets it back
            # (cua's focus guard). An intended foreground step, or the user switching apps, is left alone.
            self._osa(f'tell application "{self._q(before)}" to activate')
            if effect_notes():
                res = replace(res, detail=f"{res.detail} [{after} took the front during the step; given back to {before}]")
        note = new_window_note(wins_before, self._task_windows()) if effect_notes() else None
        if note:
            res = replace(res, detail=f"{res.detail} -- {note}")
        return res

    def _task_windows(self) -> list[dict]:
        """The target app's ordinary on-screen windows, {id, title, bounds}: compared before and after a step to
        say when it opened a window or a sheet (cua's window-change note)."""
        try:
            import Quartz
            wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly
                                                     | Quartz.kCGWindowListExcludeDesktopElements, 0) or []
            pid = self._pid_of(self.app, wins)
        except Exception:   # noqa: BLE001 -- a note is optional; the step's result is not
            return []
        return [{"id": int(w["kCGWindowNumber"]), "title": w.get("kCGWindowName") or "",
                 "bounds": dict(w.get("kCGWindowBounds") or {})}
                for w in wins if pid and w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0]

    def _execute_inner(self, action: Action) -> ExecResult:
        # A synthetic control (open a document of the folder, save, close, ...) is carried out by script and needs no
        # Peekaboo snapshot. After the last document window was closed there is none, and "open parts.csv" was
        # refused as "observe before acting" until the run ended as a loop (D1 from the folder, 09-30/10-01).
        if (action.binding.element_id or "").startswith("syn:") and self._mcp is not None:
            projected = self._execute_projected(action)
            if projected is not None:
                return projected
        if self._snapshot is None and action.kind in self.NEEDS_SNAPSHOT:
            # Everything not in that set stays available deliberately. An app
            # whose last window closed produces no snapshot, and the guard used
            # to swallow focus_app, focus_window, wait and screenshot along with
            # the rest -- so the only moves that could leave the dead end were
            # refused by it. A live run spent six actions and 435 seconds
            # discovering that and gave up with "4 identical observations".
            return ExecResult(False, "no snapshot yet; observe before acting")
        k = action.kind
        vid = action.binding.element_id or ""
        # Before every route that writes: the projected one (an append pasted into a search field and submitted with
        # Return) wrote a parts row into Safari's address bar while this check sat after it (app run, 10-01).
        if k is ActionKind.TYPE_TEXT and vid and not vid.startswith("syn:"):
            refused = self._browser_field_refusal(vid) or self._lines_lost_refusal(action)
            if refused:
                return ExecResult(False, refused)
        if vid.startswith(("ocr:", "icon:", "gen:", "desc:")):
            return self._vision_act(action)
        projected = self._execute_projected(action) if self._mcp is not None else None
        if projected is not None:
            return projected
        if k is ActionKind.TYPE_TEXT and vid in getattr(self, "_popups", {}):
            # SELECT on a pop-up (the adapter sends it as a set-to-this-text on the element): chosen in its menu.
            return self._select_popup(vid, (action.text or "").strip())
        if (k is ActionKind.CLICK and self._mcp is not None and self.app.lower() == "com.apple.finder"
                and action.binding.element_id
                and (self._element_role.get(action.binding.element_id) or "").lower() == "folder"):
            # A folder is a link: clicking it goes in, as it does on every page the planner has seen.
            action = Action(kind=ActionKind.DOUBLE_CLICK, binding=action.binding, raw=action.raw)
            k = action.kind
        if k is not ActionKind.TYPE_TEXT:
            self._blind_write = None
        if k is not ActionKind.KEY:
            self._chord_repeat = {}

        if k is ActionKind.WAIT:
            time.sleep(min(action.duration_s, 30))
            return ExecResult(True, f"waited {action.duration_s}s")
        if k is ActionKind.SCREENSHOT:
            return ExecResult(True, "observation refreshed")

        if k is ActionKind.FOCUS_APP:
            target = (action.text or "").strip()
            if target.lower() == self.app.lower():
                # Reported as a success this was a no-op the loop could not see: one run switched to the window it
                # was already on 188 times, each call "confirmed", until the wall clock ended it. An action that
                # cannot change anything is not a success.
                return ExecResult(False, f"already observing {target}; that action cannot change anything -- "
                                         f"choose a different target or a different operation")
            # Bundle ids are the reliable selector: on a zh-Hans system
            # `--app Finder` resolves for some commands and not others, while
            # com.apple.finder always does.
            if self._mcp is not None:
                # The same as switching windows: observe the other app, do not raise it. `see` resolves an app
                # that is running in the background; launching one that is not is still done with -g.
                if "." in target:
                    subprocess.run(["/usr/bin/open", "-g", "-b", target], capture_output=True, timeout=15)
                else:
                    subprocess.run(["/usr/bin/open", "-g", "-a", target], capture_output=True, timeout=15)
                self.app = target
                self._target_pid = None
                self._pinned_window = None
                self._window_id = None
                self._snapshot = None
                return ExecResult(True, f"now observing {target}; it was not brought forward")
            cmd = "activate" if "." in target else "activate_by_name"
            ok, _, detail = self._run(cmd, app=target)
            if ok:
                self.app = target
                self._target_pid = None
                self._pinned_window = None
                self._window_id = None
                self._foreground_actions += 1
            return self._settle(ok, detail or f"switched to {target}")

        if k is ActionKind.FOCUS_WINDOW:
            target = (action.text or "").strip()
            known = {w.id: w for w in self._windows()}
            if self._mcp is not None and target in self._window_app:
                # A window of another staged app: the switch is an app switch, still without raising anything.
                if not self._readable(self._window_app[target], target):
                    return ExecResult(False, _UNREADABLE.format(target=target))
                self.app = self._window_app[target]
                self._target_pid = None
                self._pinned_window = target
                self._window_id = target
                self._snapshot = None
                return self._settle(True, f"now observing window {target} ({known[target].title if target in known else target}); "
                                          f"it was not brought forward")
            if target not in known:
                # Naming a window that is not there is a model error worth
                # answering with the list rather than a bare refusal: the whole
                # point of this action is that the ids come from the observation.
                have = ", ".join(f"{w.id} ({w.title})" for w in known.values()) or "none"
                return ExecResult(False, f"no window {target!r} in {self.app}; windows: {have}")
            if target == self._pinned_window:
                # Same reasoning as focus_app above: 188 of these ended a run at the wall clock, every one of them
                # "confirmed", because switching to the window you are already on cannot change anything.
                return ExecResult(False, f"already observing window {target}; that action cannot change anything "
                                         f"-- choose a different window or a different operation")
            if self._mcp is not None and not self._readable(self.app, target):
                return ExecResult(False, _UNREADABLE.format(target=target))
            if self._mcp is not None:
                # In the background, switching windows means observing a different one, not raising it: see takes
                # a window id and every action is addressed through that snapshot. `window focus` brought TextEdit
                # to the front for 37 seconds of a run that was otherwise invisible.
                self._pinned_window = target
                self._window_id = target
                return self._settle(True, f"now observing window {target} ({known[target].title}); it was not "
                                          f"brought forward")
            ok, _, detail = self._run("window_focus", window=target)
            if ok:
                # Re-pin rather than unpin. The pin is what keeps a second
                # document from silently becoming the target; this action moves
                # it deliberately, which is the difference between switching
                # windows and drifting between them.
                self._pinned_window = target
                self._window_id = target
                self._foreground_actions += 1
            return self._settle(ok, detail or
                                f"focused window {target} ({known[target].title})")

        if k is ActionKind.MENU:
            path = (action.text or "").strip()
            ok, _, detail = self._run("menu_click", app=self.app, path=path)
            if not ok:
                # A half-traversed menu is left hanging open and blocks the next
                # action, so close it before saying anything else.
                self._run("press_fg", chord="escape", app=self.app)
                self._foreground_actions += 1
                detail = f"{detail} {self._menu_options(path)}".strip()
            return self._settle(ok, detail or f"menu {path}")

        if k in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK):
            eid = action.binding.element_id
            if eid:
                self._last_clicked_label = self._labels_by_id.get(eid, "")
            if (k is ActionKind.CLICK and eid and self.app.lower() == CHAT_APP
                    and (self._element_role.get(eid) or "").lower() == "button" and eid in self._rects):
                return self._chat_flash_click(eid)
            # A double click was sent as a single click all along: CLICK, DOUBLE_CLICK and RIGHT_CLICK shared one
            # command with no modifier, so every OPEN on the real desktop only selected. Every "go into the 2026
            # folder" task was unreachable before a model was ever asked -- and the trace said "double_click".
            modifier = ["--double"] if k is ActionKind.DOUBLE_CLICK else ["--right"] if k is ActionKind.RIGHT_CLICK else []
            if (k is ActionKind.DOUBLE_CLICK and self._mcp is not None and eid
                    and self.app.lower() == "com.apple.finder"):
                # Double-clicking a FILE launches its default app, and a launching app comes to the front: one run
                # double-clicked a .log and Console sat in front of the user for 56 seconds. A folder opens in the
                # same window and stays in the background; a file is opened with -g, which never raises.
                folder = self._finder_folder()
                name = self._labels_by_id.get(eid, "")
                if folder and name and (folder / name).is_file():
                    # Not even with -g: a .log opens in Console, and Console comes to the front on launch whatever
                    # it is told (seven seconds of it in one collection round). Nothing in these tasks needs a file
                    # opened from Finder -- the documents a task edits are opened at staging -- so it is refused.
                    return ExecResult(False, f"{name!r} is a file: opening it would launch another application in "
                                             f"front of the user. Folders open; files are renamed, moved or left.")
            if (eid and self._mcp is not None and k is ActionKind.CLICK
                    and (self._element_role.get(eid) or "").lower() in ("textarea", "textfield", "axtextarea")
                    and self.app.lower() != "com.apple.finder"):
                # Click-to-focus is a browser habit, and in the background it is both unnecessary -- a value write
                # needs no focus -- and misleading: the click comes back "did not report AXFocused", which reads
                # as failure, and one run gave up right there with the value it needed already chosen.
                return self._settle(False, f"no need to click {self._labels_by_id.get(eid) or eid!r} first: type "
                                           f"into it directly (TYPE_TEXT / APPEND_TEXT); it does not need focus")
            if eid:
                fg = action.foreground and self.allow_foreground
                if self._mcp is not None:
                    fg = False                   # the session keeps the snapshot alive: no reason to take focus
                self._foreground_actions += int(fg)
                ok, _, detail = self._run("click_on_fg" if fg else "click_on", _extra=modifier,
                                          element=eid, snapshot=self._snapshot)
                if not ok and "SNAPSHOT_NOT_FOUND" in detail and self.allow_foreground:
                    # Without a Bridge host that can prove ScreenCaptureKit ownership, a snapshot cannot outlive
                    # the `see` process that minted it, so element-addressed clicks are unusable on this machine.
                    # The element's own rect from that same observation still is: click its centre in the
                    # foreground. Staleness stays guarded by layout_version, so this weakens the addressing, not
                    # the binding check.
                    rect = self._rects.get(eid)
                    if rect is not None:
                        gx = int(self._origin.x + rect.x + rect.w / 2)
                        gy = int(self._origin.y + rect.y + rect.h / 2)
                        self._escalations += 1
                        self._foreground_actions += 1
                        ok, _, detail = self._run("click_at_fg", _extra=modifier, x=gx, y=gy)
                        detail = f"{detail} (foreground fallback at {gx},{gy}: no snapshot host)"
            elif action.point is not None:
                # `--at` takes window-local *logical points*; the model saw the
                # delivered image and answers in its pixels. Skipping this
                # conversion put a click 1.7x too far right and down and reported
                # "no pressable element there" -- the exact failure this module
                # exists to prevent, on the one path that was never wired to it.
                pt = to_logical(action.point, action.coord_space, self._geometry())
                x, y = clamp_to_screen(pt, self._geometry()).rounded()
                ok, _, detail = self._run("click_at", x=x, y=y,
                                          snapshot=self._snapshot, app=self.app)
            else:
                return ExecResult(False, "click needs an element id or a point")
            return self._settle(ok, detail or "clicked")

        if (k is ActionKind.TYPE_TEXT and self._mcp is not None and action.binding.element_id
                and self.app.lower() == "com.apple.finder"):
            # A row's text in a Finder list is the file's name, and writing it is a rename. Finder does not take
            # that as an accessibility value outside its inline editor, so the rename goes through Finder itself,
            # addressed by the row's current name in the folder the window shows.
            folder = self._finder_folder()
            old = self._labels_by_id.get(action.binding.element_id, "")
            new = (action.text or "").strip()
            if folder and old and new and old == new:
                # Renaming a file to the name it has is the same no-op as focusing the window you are on: reported
                # as a success it let a finished task spend twelve more actions renaming february.log to itself.
                return self._settle(False, f"{old!r} already has that name; nothing to rename -- if the goal is "
                                           f"met, finish")
            if folder and old and new and _bad_file_name(new):
                return self._settle(False, _bad_file_name(new))
            if folder and old and new and (folder / old).exists():
                ok, out = self._osa(f'tell application "Finder" to set name of (POSIX file '
                                    f'"{self._q(str(folder / old))}" as alias) to "{self._q(new)}"')
                if ok:
                    self._scripted_actions += 1
                    self._undo.append({"kind": "rename", "from": folder / old, "to": folder / new})
                    return self._settle(True, f"renamed {old!r} to {new!r} (scripted)")
                return self._settle(False, f"could not rename {old!r} to {new!r}: {out[:160]}")

        if k is ActionKind.TYPE_TEXT and self._mcp is not None and action.binding.element_id:
            # Write the value through the accessibility API in the session that holds the snapshot. No keyboard,
            # so no focus to take and no input method to turn "archive" into "a'r'chi'v'e"; and Peekaboo reads
            # it back, so the result is verified rather than dispatched-and-hoped.
            eid = action.binding.element_id
            new = action.text or ""
            if not action.clear_first:
                current = self._element_value.get(eid, "")
                if new.strip() and current.rstrip().endswith(new.strip()):
                    # Appending exactly what the text already ends with duplicates it; it is the append form of
                    # writing a field its own value, and a finished run took it as the way to "try again".
                    return self._settle(False, "the text already ends with exactly this; nothing changed -- if "
                                               "the goal is met, save or finish")
                new = current + new
            elif eid in self._element_value and self._element_value[eid] == new:
                # Writing a field the value it already holds changes nothing, and reported as "set, verified" it let
                # a finished task write the same twelve characters eleven times until the budget ran out.
                return self._settle(False, f"the field already holds exactly this text; nothing changed -- if the "
                                           f"goal is met, save or finish")
            label = (self._labels_by_id.get(eid) or "")
            searchy = bool(re.search(r"搜索|查找|search|find", label, re.I))
            allowed = {b.strip().lower() for b in os.environ.get("HANDS_FLASH_APPS", "").split(",") if b.strip()}
            if searchy and (self.app.lower() in allowed or self._background_input(self.app)) and eid in self._rects:
                # A search box searches on Return, and a value written through accessibility submits nothing: in a
                # web page the query sat in the box while the list below stayed as it was, and the planner played
                # the nearest-looking song from it. Return cannot be sent to a page in the background (Peekaboo
                # refuses it), so where the user allowed the brief foreground it is typed there: click, paste,
                # Return, as in a screenshot-only app.
                return self._vision_act(action)
            if (action.clear_first and self._background_input(self.app) and eid in self._rects
                    and self._element_role.get(eid, "").lower() in ("textfield", "textarea")):
                # A web page's field in an app that takes background input: clicked and pasted into, as a person
                # would. The accessibility write shows in the field but is not the page's own value, and the paste
                # that followed it to make it so landed in the field focused before -- the merchant took the date.
                return self._vision_act(action, submit=False)
            ok, meta, text, detail = self._mcp.tool("set_value", {"on": eid, "snapshot": self._snapshot,
                                                                   "value": new})
            effect = meta.get("effect")
            unverified = (ok and effect not in (None, "confirmed")) or (not ok and "could not be verified" in
                                                                          (detail or ""))
            if (unverified and os.environ.get("HANDS_WEB_TYPE_FALLBACK") == "1" and action.clear_first
                    and (self.app.lower() in allowed or self._background_input(self.app)) and eid in self._rects):
                # Opt-in (the gym): a web page takes the accessibility write without passing it to the page's own
                # field -- the reply box stayed empty through five writes, each "accepted, not verified". Where the
                # brief foreground is allowed, the text is pasted into the field instead, without Return.
                return self._vision_act(action, submit=False)
            if ok and effect not in (None, "confirmed"):
                return self._settle(True, f"set {len(new)} chars in the background ({effect})")
            return self._settle(ok, f"set {len(new)} chars in the background, verified" if ok else detail)

        if k is ActionKind.TYPE_TEXT:
            text = action.text or ""
            # Foreground typing goes wherever the keyboard focus is, so an element from another window is not a
            # target, it is a hazard: one run sent the folder name into a Finder window the model had opened a
            # moment earlier and the driver still reported success. The binding names a window; honour it.
            eid = action.binding.element_id
            if eid and self._pinned_window and self._element_window.get(eid) not in (None, "", self._pinned_window):
                return ExecResult(False, f"{eid} belongs to window {self._element_window.get(eid)}, not the pinned "
                                         f"window {self._pinned_window}; observe the window you mean first")
            if "\n" in text and action.clear_first:
                return self._type_multiline(text)
            # Only the set-the-field form can be verified by value readback.
            # Insert-at-caret is dispatched but unconfirmable, and Peekaboo says
            # so rather than pretending; we pass that through unchanged.
            if action.foreground and self.allow_foreground:
                # A blind write cannot be read back -- an item in inline rename mode is not in the tree at all --
                # so it is allowed once and then refused until something else happens. A planner pasted the folder name
                # four times into a field it could not see and named the folder "archivearchivearchivearchive".
                # Refusing is the only enforcement available: the notice in the loop was ignored.
                if self._mcp is not None and not action.binding.element_id:
                    named = self._scripted_rename(text)
                    if named is not None:
                        return self._settle(named.ok, named.detail)
                if self._blind_write == text:
                    return ExecResult(False, f"{text!r} was already written to the focused field and a blind write "
                                             f"cannot be read back; confirm it with Return, or observe evidence "
                                             f"that it did not land, before writing again")
                self._blind_write = text
                return self._type_foreground(text, clear_first=action.clear_first,
                                             element_bound=bool(action.binding.element_id))
            cmd = "type_clear" if action.clear_first else "type"
            ok, data, detail = self._run(cmd, text=text, snapshot=self._snapshot)
            if not ok and "different window" in detail and self.allow_foreground:
                # The product should absorb this, not the model. A field that
                # reports as belonging to another window can never be written to
                # in the background, however many times it is retried -- so
                # escalating is the only path forward, and asking the model to
                # remember that would be handing it our platform homework.
                # Counted, because taking the foreground is user-visible.
                self._escalations += 1
                return self._type_foreground(text)
            if not ok and not action.clear_first:
                # Peekaboo cannot confirm an insert, but we can: observe again
                # and see whether the field now contains what was typed.
                self._snapshot = None
                obs = self.observe()
                if any(text in (e.value or "") for e in obs.elements if e.settable):
                    return ExecResult(True, f"inserted {len(text)} chars, verified by readback")
                detail = ("insert-at-caret typing did not land; pass clear_first to set the "
                          f"whole field instead. Upstream said: {detail}")
            return self._evidence(ok, data, detail, "type")

        if k is ActionKind.KEY and self._mcp is not None:
            chord = "+".join(action.keys)
            # The repeat guard lived in the CLI branch, so the scripting channel walked straight past it and made
            # four folders before the first one was named. It belongs in front of every channel.
            if self._canon(chord) in ("cmd+shift+n", "cmd+n") and self._chord_repeat.get(chord, 0) >= 1:
                return ExecResult(False, f"{chord} was already pressed and pressing it again creates a second item. "
                                         f"The one it created is waiting for its name: type the name into the "
                                         f"keyboard focus (TYPE_FOCUSED).")
            self._chord_repeat = {chord: self._chord_repeat.get(chord, 0) + 1}
            scripted = self._scripted_command(chord)
            if scripted is not None:
                return self._settle(scripted.ok, scripted.detail)

        if k is ActionKind.KEY and self._mcp is not None:
            # No menu route. A background click on the menu item that owns a chord is accepted and not executed
            # (the one save that seemed to work was macOS autosave), and it came back "done in the background" --
            # a planner was told a folder had been created when nothing had. Without a scripted equivalent, a
            # chord in background mode is refused, with the reason.
            chord = "+".join(action.keys)
            return self._settle(False, f"{chord} has no background route in this app; it would have to take the "
                                       f"user's keyboard focus, which this run does not do -- use another action")

        if k is ActionKind.KEY:
            chord = "+".join(action.keys)
            # A command chord that creates something creates another one every time it is pressed. The loop warns
            # after two and the planner presses on: one run made eighteen stray folders finishing a task whose
            # first folder was already correct. So the third identical chord in a row is refused here, with the
            # reason in the words the planner reads back as `error` -- which is the channel that demonstrably moves
            # its next choice. Any other action clears the count.
            # Creating chords only -- the same policy as the background path. Applied to every chord it refused
            # the first press of anything the background path had already counted, which is every chord with no
            # scripted or menu route.
            if self._canon(chord) in ("cmd+shift+n", "cmd+n") and self._chord_repeat.get(chord, 0) >= 1 \
                    and self._mcp is None:
                # Naming the way out matters more than naming the problem: told only that the chord was refused,
                # the planner pressed it again until the failure cap ended the run. What it actually needs to know
                # is that the thing it created is waiting for a name it cannot see.
                return ExecResult(False,
                                  f"{chord} was already pressed and pressing it again creates a second item. "
                                  f"What it created is selected and waiting for its name, and macOS does not put "
                                  f"an item being renamed in the accessibility tree -- so it is not in the element "
                                  f"list and no click will find it. Type the name into the keyboard focus "
                                  f"(TYPE_FOCUSED); it is confirmed for you.")
            self._chord_repeat = {chord: self._chord_repeat.get(chord, 0) + 1}
            if not self._window_id:
                return ExecResult(False, "no window pinned; observe before pressing keys")
            # A background chord is reported "confirmed" and does nothing until the app happens to come forward:
            # a planner pressed cmd+shift+N eight times in Finder, every press came back ok with deliveryMode background,
            # and no folder appeared. Worse, they are not dropped but queued -- pressing in the background and then
            # escalating delivered both, and the run created two folders where the task wanted one. macOS routes a
            # command chord through the frontmost app, so when the foreground is permitted, that is the only route.
            if self.allow_foreground:
                self._foreground_actions += 1
                ok, data, detail = self._run("press_fg_window", chord=chord, app=self.app,
                                             window=self._window_id)
            else:
                ok, data, detail = self._run("press", chord=chord, app=self.app,
                                             window=self._window_id)
            if ok:
                # A command chord changes the window after the event is delivered, and the accessibility tree lags
                # behind that. Observing immediately showed a Finder window with no new folder in it twice in a
                # row, so the planner -- correctly, on the evidence it was given -- pressed again and the run
                # created a second folder. Waiting for the tree to stop growing is the difference between
                # measuring the model and measuring our own impatience.
                time.sleep(self.CHORD_SETTLE_S)
            return self._evidence(ok, data, detail, f"press {chord}")

        if k is ActionKind.SCROLL:
            d = ("down" if action.scroll_dy > 0 else "up" if action.scroll_dy < 0 else
                 "right" if action.scroll_dx > 0 else "left")
            amount = abs(action.scroll_dy or action.scroll_dx) or 3
            eid = action.binding.element_id
            if eid:
                ok, _, detail = self._run("scroll_on", direction=d, amount=amount,
                                          element=eid, snapshot=self._snapshot)
            elif action.foreground and self.allow_foreground:
                self._foreground_actions += 1
                ok, _, detail = self._run("scroll_fg", direction=d, amount=amount, app=self.app)
            else:
                return ExecResult(False,
                                  "background scroll must name a scrollable element: pass "
                                  "element_id (a list or scroll area from the element list), "
                                  "or foreground=true to scroll the focused window")
            return self._settle(ok, detail or f"scrolled {d} by {amount}")

        if k is ActionKind.DRAG:
            return ExecResult(False, "drag is not wired to this driver yet",
                              unsupported=True)
        return ExecResult(True, f"{k.value} noted")

    def _type_foreground(self, text: str, clear_first: bool = False, element_bound: bool = True) -> ExecResult:
        """Type into whatever holds keyboard focus, then prove it landed.

        Some editable fields are not in the window that owns them: Finder's
        inline rename field reports as belonging to a different window, so a
        snapshot-targeted write is refused no matter how the window is focused.
        Foreground input reaches it, but Peekaboo calls foreground typing
        dispatch-only and will not confirm it -- rightly, since it cannot know
        where a global keystroke landed. So we verify the same way we verify
        multi-line writes: observe again and read the value back.
        """
        # No blind cmd+A before a blind paste, however much "replace" is wanted. When the rename field is not in
        # edit mode after all, cmd+A selects the window's files instead of the field's text and the paste then
        # writes files into the folder -- one run left two "已粘贴 ....txt" behind that way. A wrong name is
        # recoverable; inventing files in the user's directory is not the kind of mistake this driver gets to make.
        self._foreground_actions += 1
        # Paste, not keystrokes. A Chinese input method turns synthesized typing into pinyin composition: the
        # folder name came out as "a'r'chi'v'e". `paste` sets the clipboard, sends cmd+V and restores the previous
        # contents in one operation, so the text arrives as text whatever IME is active.
        ok, _, detail = self._run("paste_fg", text=text)
        if not ok and "unknown" in detail.lower():
            ok, _, detail = self._run("type_fg", text=text)
        if ok and not element_bound:
            # A blind write goes into a field nobody can read back -- an item in inline rename mode is not in the
            # tree at all -- so leaving it uncommitted leaves the run in a state it cannot inspect: the planner
            # retyped the name five times because nothing it could see had changed. Typing a name and confirming
            # it is one gesture; the driver completes it rather than handing back a half-done edit.
            self._run("press_fg_window", chord="return", app=self.app, window=self._window_id)
            self._foreground_actions += 1
            time.sleep(self.CHORD_SETTLE_S)
            detail = f"{detail} (committed with Return)"
        # Peekaboo declines to confirm foreground typing at all -- it cannot know
        # where a global keystroke landed -- so its `success: false` here is the
        # expected outcome, not a failure. Treating it as one made a rename that
        # actually worked report as broken. The readback is the real verdict.
        self._snapshot = None
        obs = self.observe()
        fields = [e.value for e in obs.elements if e.settable and e.value]
        if any(text in (v or "") for v in fields):
            return self._settle(True, f"typed {text!r} in the foreground, verified by readback")
        # Say what the field actually holds. Finder's rename field pre-selects
        # only the base name, so typing "final.txt" over "draft.txt" yields
        # "final.txt.txt" -- a difference the agent can only correct if it is told.
        self._snapshot = None
        return ExecResult(
            True, indeterminate=True,
            detail=(f"typed {text!r} in the foreground. This field is not in the observable "
                    "window, so the write could not be verified here -- confirm it by the "
                    "consequence of committing it. Note that a rename field usually "
                    "pre-selects the base name only, so an extension typed in is kept twice."))

    def _type_multiline(self, text: str) -> ExecResult:
        """Type text containing newlines, then prove it landed by reading it back.

        A newline is a Return keypress, not a literal character, so Peekaboo's
        own confirmation (which compares literal text) cannot cover it -- a
        trailing newline alone is enough to make a correct write report failure.
        Real keystrokes are still used, because setting the accessibility value
        directly would bypass the interface a GUI run exists to measure. The
        write is then verified the only honest way available: observe again and
        compare what the field actually holds.
        """
        segments = text.split("\n")
        ok, _, detail = self._run("type_clear", text=segments[0], snapshot=self._snapshot)
        if not ok and "different window" in detail and self.allow_foreground:
            # The single-line path escalates here and this one did not, so the
            # very same write succeeded or failed depending on whether the text
            # happened to contain a newline. The symptom was a driver that
            # could not type Chinese with line breaks; the cause was one
            # recovery living on one of two branches.
            self._escalations += 1
            return self._type_foreground(text)
        if not ok:
            return self._settle(False, f"multiline type failed on first segment: {detail}")
        for seg in segments[1:]:
            self._snapshot = None
            self.observe()
            if not self._window_id:
                return self._settle(False, "lost the window mid-typing")
            ok, _, detail = self._run("press", chord="return", app=self.app,
                                      window=self._window_id)
            if not ok:
                return self._settle(False, f"return between lines failed: {detail}")
            if seg:
                self._snapshot = None
                self.observe()
                ok, _, detail = self._run("type", text=seg, snapshot=self._snapshot)

        self._snapshot = None
        obs = self.observe()
        got = next((e.value for e in obs.elements if e.settable and e.value is not None), None)
        if got is None:
            return self._settle(False, "cannot read the field back to verify the write")
        if got.rstrip("\n") != text.rstrip("\n"):
            return self._settle(False,
                                f"readback mismatch: field holds {got[:60]!r}, wrote {text[:60]!r}")
        return self._settle(True, f"typed {len(segments)} lines, verified by readback")

    def _geometry(self) -> ScreenGeometry:
        """The frame the last observation reported, for converting a model point."""
        return self._last_geometry or ScreenGeometry(Size(1512, 982), Size(1512, 982))

    #: The platform tells us, in words, when an outcome is unknowable. Reading
    #: that as failure invites a retry it explicitly warns against, and the work
    #: was very likely done -- a coordinate click reported this while the row it
    #: aimed at became selected.
    INDETERMINATE_MARKERS = ("outcome is indeterminate", "do not retry",
                             "response was lost",
                             # A background double click into a Finder folder comes back like this every time, and
                             # the folder opened every time: the three observations of one run went root -> 2026 ->
                             # 02. Read as failure, it told the planner it had failed at the one thing it had done
                             # right, and five of them ended the run on the consecutive-failure cap.
                             "did not return a confirmed outcome")

    #: How long a command chord needs before its effect is in the accessibility tree. Measured, not guessed: a new
    #: folder in Finder was absent from two consecutive observations taken right after the press, and present on
    #: every observation taken 1.2s later. There is no cheap signal to poll for -- `window list` says nothing about
    #: a window's contents and a second capture costs as much as the observation the loop is about to take anyway.
    CHORD_SETTLE_S = 1.2

    def _app_selector(self) -> str:
        """PID, not bundle id. Resolving a bundle id walks every running app, and on a managed Mac one of them
        (a security agent, here) has no process-generation identity -- so every background menu click was refused
        as "inventory incomplete", every time, and the retry could never succeed."""
        return f"PID:{self._target_pid}" if self._target_pid else self.app

    _MODS = {"⌘": "cmd", "⇧": "shift", "⌥": "option", "⌃": "ctrl"}

    @classmethod
    def _canon(cls, chord: str) -> str:
        parts = [p.strip().lower() for p in chord.replace("alt", "option").split("+") if p.strip()]
        mods = sorted(p for p in parts if p in ("cmd", "shift", "option", "ctrl"))
        keys = [p for p in parts if p not in ("cmd", "shift", "option", "ctrl")]
        return "+".join(mods + keys)

    def _menu_path_for_chord(self, chord: str) -> str | None:
        """The menu path whose item shows this shortcut, from the app's own menu listing. Cached per app."""
        if self._mcp is None:
            return None
        cache = self._menu_shortcuts.get(self.app)
        if cache is None:
            ok, _, text, _ = self._mcp.tool("menu", {"action": "list", "app": self._app_selector()})
            cache = {}
            top = None
            for line in (text.splitlines() if ok else []):
                m = re.match(r"^📁 (.+)$", line.strip())
                if m:
                    top = m.group(1).strip()
                    continue
                m = re.match(r"^\s{2}• (.+?) \(([⌘⇧⌥⌃]+)(.+)\)\s*$", line)   # top-level items only
                if m and top:
                    mods = "+".join(self._MODS[c] for c in m.group(2))
                    cache.setdefault(self._canon(f"{mods}+{m.group(3)}"), f"{top} > {m.group(1).strip()}")
            self._menu_shortcuts[self.app] = cache
        return cache.get(self._canon(chord))

    # -- projection -------------------------------------------------------

    def _project_finder(self, folder: Path, els: list[Element]) -> list[Element]:
        """Finder's capabilities as the web controls a planner already knows.

        Moving a file in Finder is select, cmd+C, go to the destination, option+cmd+V -- four decisions, and the
        one sequence both planners kept losing their way in. As a page it is one dropdown per file: "move 「a.log」
        to", with the places it can go. Creating a named folder is a form (a name field and a button), and
        folders are links. Every one of these is synthetic -- flagged, carried out through Finder's scripting, and
        counted apart."""
        root = Path(os.path.realpath(str(self.ws)))
        dests: list[tuple[str, Path]] = []
        q = folder
        while q != root and str(q).startswith(str(root)) and q != q.parent:
            q = q.parent
            label = f"工作目录顶层 {q.name}" if q == root else f"上级文件夹 {q.name}"
            dests.append((label, q))
        rows = [e for e in els if (e.role or "").lower() in ("folder", "file")]
        for e in rows:
            if (e.role or "").lower() == "folder":
                dests.append((f"子文件夹 {e.label}", folder / e.label))
        # Every other folder in the workspace too, by its path from the top. Only up and down were offered, so a
        # file in 项目 could not be moved to misc next to it: the one right answer was not among the options, and
        # the planner moved it to the top instead.
        known = {p for _, p in dests} | {folder}
        others = sorted(p for p in root.rglob("*") if p.is_dir() and not any(x.startswith(".") for x in
                                                                              p.relative_to(root).parts))
        for p in others[:40]:
            if p not in known:
                label = (f"同级文件夹 {p.name}" if p.parent == folder.parent
                         else f"文件夹 {p.relative_to(root).as_posix()}")
                dests.append((label, p))
        out: list[Element] = []
        self._move_options = {}
        for e in rows:
            if (e.role or "").lower() != "file":
                continue
            options = {label: path for label, path in dests if path != folder}
            if not options:
                continue
            sid = f"syn:move:{e.label}"
            self._move_options[sid] = {"src": folder / e.label, "to": options}
            out.append(Element(id=sid, role="combobox", label=f"移动「{e.label}」到", value="（原位置）",
                               options=list(options), synthetic=True, app=e.app, window_id=e.window_id))
        out += self._project_finder_view(folder)
        undo = self._undo_label()
        if undo:
            out.append(Element(id="syn:undo", role="button", label=undo, synthetic=True, app=self._app_name,
                               window_id=self._window_id or ""))
        # The name typed so far is the field's value. Shown empty after a successful fill, the field looked untouched:
        # the oracle labelled "type the name again" there (202 DAgger rows, DeskMind Brain 09-26) and every checkpoint
        # learned to retype instead of clicking 新建文件夹.
        out.append(Element(id="syn:newfolder:name", role="textbox", label="新建文件夹的名称",
                           value=getattr(self, "_pending_folder", "") or "",
                           settable=True, synthetic=True, app=self._app_name, window_id=self._window_id or ""))
        out.append(Element(id="syn:newfolder:create", role="button", label="新建文件夹", synthetic=True,
                           app=self._app_name, window_id=self._window_id or ""))
        return out

    def _open_workspace_documents(self) -> set[tuple[str, int, int]]:
        """The TextEdit windows open now whose document is in this run's workspace, as (title, left, top): asked of
        TextEdit by path, and told apart by where the window is -- two windows can have the same title (a parts.csv
        from an earlier run was taken for this one's, and the rows went into it)."""
        if getattr(self, "ws", None) is None:
            return set()
        root = os.path.realpath(str(self.ws))
        ok, out = self._osa('tell application "TextEdit"\nset r to ""\nrepeat with w in windows\ntry\n'
                            'set b to bounds of w\nset r to r & (name of w) & tab & (path of document of w) & tab & '
                            '(item 1 of b) & tab & (item 2 of b) & linefeed\nend try\nend repeat\nreturn r\nend tell')
        return parse_textedit_windows(out, root) if ok else set()

    def _workspace_file_names(self) -> set[str]:
        """Names of the files in this run's workspace, its scratch documents included: the titles its TextEdit
        windows can have."""
        if getattr(self, "ws", None) is None:
            return set()
        return {p.name for p in Path(os.path.realpath(str(self.ws))).rglob("*") if p.is_file()}

    def _ws_folders(self) -> dict[str, Path]:
        """Every folder of the workspace, by its path from the top (the top itself is 工作目录顶层)."""
        root = Path(os.path.realpath(str(self.ws)))
        out = {"工作目录顶层": root}
        for p in sorted(root.rglob("*"))[:400]:
            rel = p.relative_to(root)
            if p.is_dir() and not any(x.startswith(".") for x in rel.parts):
                out[rel.as_posix()] = p
        return out

    def _project_textedit(self) -> list[Element]:
        """TextEdit's new-document and save-as, as a page: a button, a name field, a place dropdown, a button.

        Save-as is a sheet with a name field, a location popup, a format popup and an extension checkbox -- in the
        foreground, keyboard-driven. Through TextEdit's scripting it is one call that never takes the focus. A new
        document is an empty plain-text file opened in TextEdit, so it is plain text from the start (the Format >
        Make Plain Text step is folded into the button's name, not left to a background menu click that is
        accepted and does nothing)."""
        if getattr(self, "ws", None) is None:
            return []
        w = self._window_id or ""
        folders = self._ws_folders()
        self._saveas_folders = folders
        pending = getattr(self, "_pending_saveas", {})
        if pending.get("window") not in (None, w):
            pending = {}      # filled in for another document's window
        return [
            Element(id="syn:newdoc", role="button", label="新建纯文本文稿", synthetic=True, app=self._app_name,
                    window_id=w),
            Element(id="syn:saveas:name", role="textbox", label="另存为：文件名", value=pending.get("name", ""),
                    settable=True, synthetic=True, app=self._app_name, window_id=w),
            Element(id="syn:saveas:folder", role="combobox", label="另存为：位置",
                    value=pending.get("folder", "（请选择）"), options=list(folders), synthetic=True,
                    app=self._app_name, window_id=w),
            Element(id="syn:saveas:do", role="button", label="另存为", synthetic=True, app=self._app_name,
                    window_id=w),
        ]

    def _textedit_save(self, name: str) -> ExecResult:
        """Save a TextEdit document without waiting for TextEdit's reply, then check that it took.

        From 09-24 on, TextEdit in the background completes a scripted save (the file is written, `modified`
        turns false) and never answers the Apple Event: every save timed out, and was reported as a failure the
        planner retried. The save is sent ignoring the reply; success is the document no longer being modified."""
        doc = self._q(name)
        self._osa('ignoring application responses\n'
                  f'  tell application "TextEdit" to save (first document whose name is "{doc}")\n'
                  'end ignoring')
        for _ in range(25):
            time.sleep(0.2)
            ok, out = self._osa(f'tell application "TextEdit" to get modified of (first document whose name is "{doc}")')
            if ok and out.strip() == "false":
                self._scripted_actions += 1
                return ExecResult(True, f"saved {name!r} (scripted, in the background)")
        return ExecResult(False, f"could not confirm that {name!r} was saved")

    def _textedit_projected(self, eid: str, action: Action) -> ExecResult | None:
        pending = self.__dict__.setdefault("_pending_saveas", {})
        if pending.get("window") not in (None, self._window_id):
            # Filled in another document's window: not this one's. Shown and applied here it saved notes.md's text
            # under the name typed for 未命名-1 (audit 09-26).
            pending.clear()
        if eid == "syn:newdoc" and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            pending.clear()
            scratch = Path(os.path.realpath(str(self.ws))) / ".hands"
            scratch.mkdir(exist_ok=True)
            n = 1
            while (scratch / f"未命名-{n}").exists():
                n += 1
            doc = scratch / f"未命名-{n}"
            doc.write_text("", encoding="utf-8")
            before = self._window_ids()
            subprocess.run(["/usr/bin/open", "-g", "-a", "TextEdit", str(doc), "--args", "-ApplePersistenceIgnoreState", "YES"], capture_output=True, timeout=15)
            # The new window is the one that was not there before. Found by title it was missed whenever the
            # window list came back without titles, and the planner, told "did not open", opened four more.
            wid = None
            for _ in range(20):
                time.sleep(0.25)
                new = self._window_ids() - before
                if new:
                    wid = sorted(new)[-1]
                    break
            wid = wid or self._window_titled(doc.name, tries=2)
            if wid:
                self._pinned_window = wid
                self._window_id = wid
            self._scripted_actions += 1
            return self._settle(bool(wid), f"opened a new plain-text document {doc.name!r}; it is now the window "
                                           f"being observed" if wid else "the new document did not open")
        if eid == "syn:saveas:name" and action.kind is ActionKind.TYPE_TEXT:
            name = (action.text or "").strip()
            # Refused where it is typed, with the reason: accepted here and refused at 另存为 as "type a file
            # name first", a URL line typed as the name looked like an empty field to the planner.
            if not name or "/" in name or ":" in name or "\n" in name or len(name) > 120:
                return ExecResult(False, f"{name[:60]!r} cannot be a file name (no '/', ':' or line breaks); "
                                         f"type just the file's name, e.g. something.txt")
            pending["name"] = name
            pending["window"] = self._window_id
            return ExecResult(True, f"save-as name set to {name!r}")
        if eid == "syn:saveas:folder" and action.kind is ActionKind.TYPE_TEXT:
            choice = (action.text or "").strip()
            if choice not in getattr(self, "_saveas_folders", {}):
                return ExecResult(False, f"{choice!r} is not one of the places offered")
            pending["folder"] = choice
            pending["window"] = self._window_id
            return ExecResult(True, f"save-as place set to {choice!r}")
        if eid == "syn:saveas:do" and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            name, place = pending.get("name", ""), pending.get("folder", "")
            if not name or "/" in name:
                return ExecResult(False, "type a file name into 另存为：文件名 first")
            folder = getattr(self, "_saveas_folders", {}).get(place)
            if folder is None:
                return ExecResult(False, "choose a place in 另存为：位置 first")
            target = folder / name
            if target.exists():
                return ExecResult(False, f"{name!r} already exists in {place!r}")
            # Written by us, reopened by TextEdit. TextEdit's own `save ... in` wrote a copy and then hung without
            # re-pointing the document at it (09-24), so the document stayed the scratch file and every later
            # save went there. The document's text is written to the target as plain UTF-8, the scratch copy is
            # closed unsaved, and the target is opened in the background as the document from now on.
            doc = self._q(self._window_title)
            ok, text = self._osa(f'tell application "TextEdit" to get text of (first document whose name is "{doc}")')
            if not ok:
                return self._settle(False, f"could not read the document to save it as {name!r}: {text[:160]}")
            target.write_text(text.replace("\r", "\n"), encoding="utf-8")
            self._osa(f'tell application "TextEdit" to close (first document whose name is "{doc}") saving no')
            old = Path(os.path.realpath(str(self.ws))) / ".hands" / self._window_title
            if old.exists():
                old.unlink()
            before = self._window_ids()
            subprocess.run(["/usr/bin/open", "-g", "-a", "TextEdit", str(target), "--args", "-ApplePersistenceIgnoreState", "YES"], capture_output=True, timeout=15)
            wid = None
            for _ in range(20):
                time.sleep(0.25)
                new = self._window_ids() - before
                if new:
                    wid = sorted(new)[-1]
                    break
            if wid:
                self._pinned_window = wid
                self._window_id = wid
            self.__dict__["_pending_saveas"] = {}
            self._scripted_actions += 1
            # The document is now called by its new name; later saves address it by that name. Left stale, every
            # cmd+S after a save-as tried to save "未命名-2" and failed.
            self._window_title = name
            # "saved '<name>'" first, the form a save is recorded in: "saved the document as ..." was never read as
            # a save, and the oracle labelled one more 保存 after every save-as (audit 09-26).
            return self._settle(True, f"saved {name!r} in {place!r}, as a new document (projected)")
        return None

    #: Finder's view modes, by the names its View menu uses.
    FINDER_VIEWS = {"图标": "icon view", "列表": "list view", "分栏": "column view", "画廊": "flow view"}

    def _finder_window_script(self, folder: Path, body: str) -> tuple[bool, str]:
        """Run `body` against the Finder window showing `folder` (bound to w), found by index -- `whose target is`
        does not match, and `repeat with w in` yields references whose properties cannot be read."""
        p = self._q(str(Path(os.path.realpath(str(folder)))) + "/")
        return self._osa('tell application "Finder"\n'
                         '  repeat with i from 1 to count of Finder windows\n'
                         '    set w to Finder window i\n'
                         '    try\n'
                         f'      if POSIX path of ((target of w) as alias) is "{p}" then\n'
                         f'{body}\n'
                         '      end if\n'
                         '    end try\n'
                         '  end repeat\n'
                         '  return "not found"\n'
                         'end tell')

    def _project_finder_view(self, folder: Path) -> list[Element]:
        """The window's view mode (Finder's View menu, foreground-only) as a dropdown carried out by scripting.

        List-view columns are not offered: `set visible of column ... of list view options` is accepted and does
        nothing on this macOS (size, label and comment columns all read back unchanged), so a checkbox for them
        would report a change that never happened."""
        ok, out = self._finder_window_script(folder, "        return (current view of w as string)")
        if not ok or out.strip() == "not found":
            return []
        view = next((k for k, v in self.FINDER_VIEWS.items() if v == out.strip()), out.strip())
        return [Element(id="syn:view", role="combobox", label="显示方式", value=view, options=list(self.FINDER_VIEWS),
                        synthetic=True, app=self._app_name, window_id=self._window_id or "")]

    def _finder_view_projected(self, eid: str, action: Action) -> ExecResult | None:
        folder = self._finder_folder()
        if folder is None:
            return ExecResult(False, "cannot tell which folder this window shows")
        if eid == "syn:view" and action.kind is ActionKind.TYPE_TEXT:
            mode = self.FINDER_VIEWS.get((action.text or "").strip())
            if mode is None:
                return ExecResult(False, f"{action.text!r} is not one of 图标 / 列表 / 分栏 / 画廊")
            ok, out = self._finder_window_script(folder, f"        set current view of w to {mode}\n"
                                                         "        return \"ok\"")
            ok = ok and out.strip() == "ok"
            return self._settle(ok, f"view set to {action.text.strip()} (projected)" if ok
                                else f"could not change the view: {out[:120]}")
        return None

    def _undo_label(self) -> str | None:
        """What the undo button says it will do -- the planner sees exactly what goes back, not a bare cmd+Z."""
        if not self._undo:
            return None
        u = self._undo[-1]
        if u["kind"] == "rename":
            return f"撤销上一步：把「{u['to'].name}」改回「{u['from'].name}」"
        if u["kind"] == "move":
            return f"撤销上一步：把「{u['to'].name}」移回「{u['from'].parent.name}」"
        return f"撤销上一步：删除刚建的文件夹「{u['path'].name}」"

    def _undo_last(self) -> ExecResult:
        """Reverse this run's most recent file change: rename back, move back, or remove the (empty) folder it made.

        Only this run's own changes, only inside the workspace, and only when nothing has moved underneath: the
        file is where the record says, the name or place it goes back to is free, the folder is empty. Anything
        else is refused and the record kept. Done on the file system directly -- Finder shows it on its own."""
        if not self._undo:
            return ExecResult(False, "nothing of this run's to undo")
        root = Path(os.path.realpath(str(self.ws)))
        u = self._undo[-1]

        def inside(p: Path) -> bool:
            return str(Path(os.path.realpath(str(p)))).startswith(str(root) + os.sep) or \
                Path(os.path.realpath(str(p))) == root

        paths = [u["from"], u["to"]] if u["kind"] != "mkdir" else [u["path"]]
        if not all(inside(p) for p in paths):
            return ExecResult(False, "that change is outside the workspace; not undone")
        try:
            if u["kind"] in ("rename", "move"):
                if not u["to"].exists():
                    return ExecResult(False, f"{u['to'].name!r} is no longer where it was put; not undone")
                if u["from"].exists():
                    return ExecResult(False, f"{u['from'].name!r} is taken again; not undone")
                os.rename(u["to"], u["from"])
                self._undo.pop()
                self._scripted_actions += 1
                if u["kind"] == "rename":
                    return self._settle(True, f"renamed {u['to'].name!r} to {u['from'].name!r} (undo)")
                return self._settle(True, f"moved {u['from'].name!r} to {u['from'].parent.name!r} (undo)")
            p = u["path"]
            if not p.is_dir():
                return ExecResult(False, f"{p.name!r} is not there any more; not undone")
            if any(x.name != ".DS_Store" for x in p.iterdir()):
                return ExecResult(False, f"{p.name!r} is not empty any more; not undone")
            for x in p.iterdir():
                x.unlink()
            p.rmdir()
            self._undo.pop()
            self._scripted_actions += 1
            return self._settle(True, f"removed folder {p.name!r} (undo)")
        except OSError as exc:
            return ExecResult(False, f"undo failed: {exc}")

    #: Which of a run's apps opens which documents of the attached folder (see _folder_documents).
    DOCUMENT_OPENERS = {"com.apple.TextEdit": (".txt", ".csv", ".md", ".json", ".log", ".tsv"),
                        "com.apple.Preview": (".png", ".jpg", ".jpeg", ".pdf", ".heic", ".gif")}

    def _opener(self, path: Path) -> str | None:
        """The run's own app that opens `path`, if one does: only the apps the request names, never another."""
        allowed = {self.app.lower()} | {STAGE_APP_BUNDLES.get(n, n).lower() for n in self._stage_apps}
        for bundle, exts in self.DOCUMENT_OPENERS.items():
            if bundle.lower() in allowed and path.suffix.lower() in exts:
                return bundle
        return None

    def _folder_documents(self, limit: int = 12) -> list[Element]:
        """The attached folder's documents that one of the run's apps opens, as things to open. The planner is
        told nothing else about the folder: asked to "open parts.csv from the attached folder", a planner with only
        Safari and TextEdit searched the web for "parts.csv" until its budget ran out (D1, 09-30)."""
        ws = getattr(self, "ws", None)
        if ws is None or not Path(ws).is_dir():
            return []
        out = []
        for p in sorted(Path(ws).iterdir(), key=lambda p: p.name.lower()):
            if p.name.startswith(".") or not p.is_file() or p.name == (self._window_title or "") \
                    or p.name in getattr(self, "_closed_documents", ()) or self._opener(p) is None:
                continue
            out.append(Element(id=f"syn:openfile:{p.name}", role="document", label=p.name, synthetic=True,
                               app=self._app_name, window_id=self._window_id or ""))
            if len(out) >= limit:
                break
        return out

    def _open_document(self, name: str) -> ExecResult:
        p = Path(self.ws) / name
        # A name from the folder's own listing: never a path out of it.
        bundle = self._opener(p) if ("/" not in name and not name.startswith(".") and p.is_file()) else None
        if bundle is None:
            return ExecResult(False, f"{name!r} is not a document of the attached folder that this run can open")
        self.app, self._target_pid, self._snapshot = bundle, None, None
        wid = self._window_titled_once(name)
        opened = wid is None
        if opened:
            before = self._window_ids()
            subprocess.run(["/usr/bin/open", "-g", "-b", bundle, str(p)], capture_output=True, timeout=15)
            # The new window is the one that was not there before (see syn:newdoc); by title only as a fallback.
            for _ in range(20):
                time.sleep(0.25)
                new = self._window_ids() - before
                if new:
                    wid = sorted(new)[-1]
                    break
            wid = wid or self._window_titled(name, tries=2)
        if wid:
            self._pinned_window = self._window_id = wid
        self._scripted_actions += 1
        where = next((n for n, b in STAGE_APP_BUNDLES.items() if b == bundle), bundle)
        return self._settle(bool(wid), (f"opened {name!r} in {where}" if opened else f"{name!r} was already open")
                            + "; it is now the window being observed (it was not brought forward)"
                            if wid else f"{name!r} did not open")

    def _close_document(self) -> ExecResult:
        """Close the observed TextEdit document -- only a saved one: closing must never throw work away."""
        title = (self._window_title or "").strip()
        if not title:
            return ExecResult(False, "no document window is being observed")
        doc = f'(first document whose name is "{self._q(title)}")'
        ok, out = self._osa(f'tell application "TextEdit" to get modified of {doc}')
        if not ok:
            return ExecResult(False, f"cannot find the document {title!r} to close")
        if out.strip() == "true":
            return ExecResult(False, f"{title!r} has unsaved changes: click 保存 first, then close it")
        ok, out = self._osa(f'tell application "TextEdit" to close {doc}')
        if ok:
            # Done with: not offered to open again. Offered, G18b reopened the file it had just been told to close.
            self.__dict__.setdefault("_closed_documents", set()).add(title)
            self._pinned_window = self._window_id = None
            self._snapshot = None
            self._scripted_actions += 1
        return self._settle(ok, f"closed {title!r}" if ok else f"could not close {title!r}: {out[:160]}")

    def _execute_projected(self, action: Action) -> ExecResult | None:
        """Carry out an action on a synthetic control, or None if the action is not on one."""
        eid = action.binding.element_id or ""
        if not eid.startswith("syn:"):
            return None
        self._projected_actions += 1
        if eid.startswith("syn:move:") and action.kind is ActionKind.TYPE_TEXT:
            spec = self._move_options.get(eid)
            dest = spec["to"].get((action.text or "").strip()) if spec else None
            if not spec or dest is None:
                return ExecResult(False, f"{action.text!r} is not one of the places offered for {eid[9:]!r}")
            ok, out = self._osa(f'tell application "Finder" to move (POSIX file "{self._q(str(spec["src"]))}" as alias) '
                                f'to (POSIX file "{self._q(str(dest))}" as alias)')
            if ok:
                self._undo.append({"kind": "move", "from": Path(spec["src"]), "to": Path(dest) / spec["src"].name})
            return self._settle(ok, f"moved {spec['src'].name!r} to {dest.name!r} (projected)" if ok
                                else f"could not move {spec['src'].name!r}: {out[:160]}")
        if eid == "syn:newfolder:name" and action.kind is ActionKind.TYPE_TEXT:
            if _bad_file_name((action.text or "").strip()):
                return ExecResult(False, _bad_file_name((action.text or "").strip()))
            self._pending_folder = (action.text or "").strip()
            self._pending_folder_in = self._finder_folder()
            return ExecResult(True, f"folder name set to {self._pending_folder!r}; click 新建文件夹 to create it")
        if eid == "syn:newfolder:create" and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            name = getattr(self, "_pending_folder", "")
            folder = self._finder_folder()
            if name and getattr(self, "_pending_folder_in", None) not in (None, folder):
                # Typed in one folder, created in another after navigating: the name is not for this one.
                self._pending_folder = ""
                return ExecResult(False, f"the name {name!r} was typed in {self._pending_folder_in.name!r}, not "
                                         f"here; type the folder's name again in this folder")
            if not name:
                return ExecResult(False, "type the folder's name into 新建文件夹的名称 first")
            if folder is None:
                return ExecResult(False, "cannot tell which folder this window shows")
            if (folder / name).exists():
                return ExecResult(False, f"{name!r} already exists in {folder.name!r}")
            ok, out = self._osa(f'tell application "Finder" to make new folder at (POSIX file "{self._q(str(folder))}" '
                                f'as alias) with properties {{name:"{self._q(name)}"}}')
            self._pending_folder = ""
            if ok:
                self._undo.append({"kind": "mkdir", "path": folder / name})
            return self._settle(ok, f"created folder {name!r} in {folder.name!r} (projected)" if ok
                                else f"could not create {name!r}: {out[:160]}")
        if eid == "syn:undo" and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            return self._undo_last()
        if eid.startswith("syn:openfile:") and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            return self._open_document(eid[len("syn:openfile:"):])
        if eid == "syn:close" and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            return self._close_document()
        if eid == "syn:view":
            r = self._finder_view_projected(eid, action)
            if r is not None:
                return r
        if eid.startswith(("syn:newdoc", "syn:saveas:")):
            r = self._textedit_projected(eid, action)
            if r is not None:
                return r
        if eid == "syn:save" and action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            # A document never saved is saved by choosing a name and a place, as TextEdit's cmd+S opens the save
            # sheet for it: a planner filled in 另存为 and then pressed 保存, which "saved" the scratch file in place.
            scratch = Path(os.path.realpath(str(self.ws))) / ".hands" / self._window_title
            if getattr(self, "ws", None) is not None and scratch.exists():
                pending = getattr(self, "_pending_saveas", {})
                if pending.get("window") not in (None, self._window_id):
                    pending = {}
                if not (pending.get("name") and pending.get("folder")):
                    return ExecResult(False, "this document has never been saved: fill in 另存为：文件名 and "
                                             "另存为：位置 first, then save")
                return self._textedit_projected("syn:saveas:do", Action(kind=ActionKind.CLICK,
                                                                         binding=action.binding))
            r = self._scripted_command("cmd+s")
            return r if r is not None else ExecResult(False, "nothing to save here")
        if action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK) and eid in ("syn:saveas:name",
                                                                                   "syn:newfolder:name"):
            return ExecResult(False, f"no need to click {self._labels_by_id.get(eid, eid)!r} first: type into it "
                                     f"directly (TYPE_TEXT)")
        return ExecResult(False, f"{action.kind.value} does not apply to {eid}")

    def _run_mcp(self, key: str, extra: list[str], **fmt) -> tuple[bool, dict, str]:
        """The same commands through the live session. Returns the CLI's data shape so parsing is shared."""
        assert self._mcp is not None
        if key in ("see", "see_window"):
            # include_elements: the element table as data in _meta.ui_elements. Opt-in upstream since
            # openclaw/Peekaboo#839; older Peekaboo builds return it always and reject the flag, so it goes only
            # where the tool's schema lists it.
            args = {"app_target": fmt.get("app"), "path": fmt.get("path"),
                    "window_id": int(fmt["window"]) if fmt.get("window") else None}
            if self._mcp.accepts("see", "include_elements"):
                args["include_elements"] = True
            ok, meta, text, detail = self._mcp.tool("see", args)
            receipt = (meta or {}).get("target_receipt") or {}
            if receipt.get("pid"):
                self._target_pid = str(receipt["pid"])   # what a background menu click has to name the app by
            return ok, (_mcp_see_as_cli_data(meta, text) if ok else {}), detail
        if key in ("click_on", "click_on_fg"):
            ok, meta, text, detail = self._mcp.tool("click", {
                "on": fmt.get("element"), "snapshot": fmt.get("snapshot"), "background": True,
                "double": "--double" in extra or None, "right": "--right" in extra or None})
            # What Peekaboo actually did, not what was asked: a click can land through the accessibility API
            # (background) or through global mouse events (the user's cursor, the user's foreground). One run
            # showed Finder in front for six seconds with foreground_actions at zero -- a count that trusts the
            # request instead of the receipt is not a count.
            mode = str((meta or {}).get("delivery_mode") or "")
            mech = str((meta or {}).get("delivery_mechanism") or "")
            if mode == "foreground" or mech in ("global_events", "cg_event", "hid"):
                self._foreground_actions += 1
                detail = f"{detail or text[:200]} [delivered in the foreground: {mech or mode}]"
            return ok, meta, detail or text[:200]
        raise KeyError(key)

    def _settle(self, ok: bool, detail: str) -> ExecResult:
        # Any action can move a window, so the snapshot is retired immediately.
        # The loop observes again before the next action, which is what makes a
        # stale binding impossible rather than merely unlikely.
        self._snapshot = None
        low = detail.lower()
        # The chat app's message box accepts a value write and never reads it back the way it was written (zero-width
        # spaces are added around it), so every write "could not be verified" -- and five in a row ended the run as
        # a loop while each had landed. The next observation shows what is in the box.
        chat_write = self.app.lower() == CHAT_APP and "could not be verified" in low
        if not ok and (chat_write or any(m in low for m in self.INDETERMINATE_MARKERS)):
            return ExecResult(True, indeterminate=True,
                              detail=("dispatched but the outcome could not be confirmed; "
                                      "upstream says not to retry -- observe and check "
                                      f"before acting again. {detail[:250]}"))
        return ExecResult(ok, detail[:400])

    def _evidence(self, ok: bool, data: dict, detail: str, what: str) -> ExecResult:
        """Carry what actually landed into the trace, not just a boolean."""
        if ok:
            ev = {k: data[k] for k in EVIDENCE_KEYS if k in data}
            detail = f"{what} confirmed {ev}" if ev else f"{what} confirmed"
        return self._settle(ok, detail)

    def inject(self, event: str, params: dict) -> ExecResult:
        # Faults belong in the app or the environment, not in the driver, so the
        # identical event can reach a competing product too.
        return ExecResult(False, f"{event!r} must be injected through the app or environment",
                          unsupported=True)

    # -- state ------------------------------------------------------------

    def state(self) -> dict:
        return {"focused_app": self._app_name, "window_id": self._window_id,
                "pinned_window": self._pinned_window,
                "snapshot": self._snapshot, "layout_version": self._obs_n,
                "foreground_actions": self._foreground_actions,
                "scripted_actions": self._scripted_actions,
                "projected_actions": self._projected_actions,
                "transport": self.transport,
                "focus_escalations": self._escalations,
                "incomplete_reads": self._incomplete_reads,
                "followed_new_windows": getattr(self, "_followed_windows", 0)}

    def clipboard(self) -> str | None:
        try:
            proc = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True, timeout=5)
            return proc.stdout if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _set_clipboard(self, value: str) -> None:
        try:
            subprocess.run(["/usr/bin/pbcopy"], input=value, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


assert isinstance(PeekabooDriver.__new__(PeekabooDriver), Driver)

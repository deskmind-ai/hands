"""Which file a window is: the document the window shows, asked of the app generically.

Two generic sources, no app named in code:

1. Accessibility: a document-based Cocoa app (TextEdit, Pages, Preview, Xcode) sets AXDocument on each document
   window to the file's URL, moved when the document is saved under a new name. Read through System Events, which
   reads any app's AX attributes in the background. An app that does not set it (Chromium and Electron apps, some
   Office versions) gives none.
2. Scripting: the Standard Suite of any scriptable document app answers `path of document of window`, and does so
   even when the app exposes no accessibility windows yet -- a TextEdit launched in the background (`open -g`)
   had no AX windows at all ten seconds after launch (10-19), while its scripting answered at once.

It is how the execution layer tells which file a save through the app's own panel or cmd+S went to: the window's
document before and after the action, and whether that file changed meanwhile.

Not PyObjC: on this macOS (27.2) ApplicationServices.AXUIElementCopyAttributeValue returned the application element
in place of every window, for every app (10-19), so the bridge cannot enumerate windows.

Matching: the window is found by its title, and among same-titled windows by its position (two windows can share a
title, as _open_workspace_documents found).
"""
from __future__ import annotations

import subprocess
from urllib.parse import unquote, urlparse

TIMEOUT_S = 3.0   # per osascript run: a busy app cannot stall a step for long

#: Bundles whose scripting did not answer in time (iTerm2 hung on `count documents` for good, 10-19): asked once per
#: process, never again. An app that answered, even with nothing, is asked each time (an unsaved document has no
#: path yet, and will after the save-as).
_NO_SCRIPTING: set[str] = set()


def _posix(doc: str) -> str | None:
    """A document value (a file:// URL, or a POSIX or HFS path) as a POSIX path, or None."""
    s = (doc or "").strip()
    if not s:
        return None
    if s.startswith("file://"):
        return unquote(urlparse(s).path) or None
    if s.startswith("/"):
        return s
    if ":" in s and not s.startswith("http"):   # an HFS path "Macintosh HD:Users:..." from an older Standard Suite
        parts = s.split(":")[1:]
        return "/" + "/".join(p.replace("/", ":") for p in parts) if parts else None
    return None


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osa(script: str) -> str | None:
    """The script's output; "" when it failed; None when it did not answer in time."""
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return ""
    return p.stdout if p.returncode == 0 else ""


def _pick(lines: str, title: str, bounds: list | None) -> str | None:
    """Among "name<TAB>document<TAB>x<TAB>y" lines, the document of the window titled `title`; among several, the one
    at `bounds`' position; a single untitled match when the title is empty."""
    rows = []
    for line in lines.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, doc = parts[0], _posix(parts[1])
        if not doc:
            continue
        xy = None
        if len(parts) >= 4:
            try:
                xy = (float(parts[2]), float(parts[3]))
            except ValueError:
                xy = None
        rows.append((name, doc, xy))
    same = [r for r in rows if r[0] == (title or "")]
    if len(same) > 1 and bounds and None not in bounds[:2]:
        x, y = float(bounds[0]), float(bounds[1])
        near = [r for r in same if r[2] is not None and abs(r[2][0] - x) < 2 and abs(r[2][1] - y) < 2]
        same = near or same
    if not same and len(rows) == 1 and not title:
        same = rows
    return same[0][1] if same else None


def window_document(pid: int, bundle: str, title: str, bounds: list | None) -> str | None:
    """The POSIX path of the document shown by the window titled `title` of the app `bundle` (pid `pid`), at
    `bounds` ([x, y, w, h] in screen points, or None), or None when the app does not say."""
    # 1. accessibility, through System Events (any app that exposes its windows and sets AXDocument)
    ax = _osa(f'tell application "System Events" to tell (first process whose unix id is {int(pid)})\n'
              'set r to ""\nrepeat with w in windows\ntry\n'
              'set p to position of w\n'
              'set r to r & (name of w) & tab & (value of attribute "AXDocument" of w) & tab & (item 1 of p) & tab & (item 2 of p) & linefeed\n'
              'end try\nend repeat\nreturn r\nend tell')
    doc = _pick(ax or "", title, bounds)
    if doc:
        return doc
    # 2. the app's own scripting (the Standard Suite: a document window's document has a path)
    if not bundle or bundle in _NO_SCRIPTING:
        return None
    sc = _osa(f'tell application id "{_q(bundle)}"\nset r to ""\nrepeat with w in windows\ntry\n'
              'set b to bounds of w\n'
              'set r to r & (name of w) & tab & (POSIX path of (path of document of w as text)) & tab & (item 1 of b) & tab & (item 2 of b) & linefeed\n'
              'on error\ntry\n'
              'set r to r & (name of w) & tab & (path of document of w) & tab & "" & tab & "" & linefeed\n'
              'end try\nend try\nend repeat\nreturn r\nend tell')
    if sc is None:
        _NO_SCRIPTING.add(bundle)
        return None
    return _pick(sc, title, bounds)

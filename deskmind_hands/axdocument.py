"""Which file a window is: the document the window shows, asked of the app generically.

Two generic sources, no app named in code:

1. Accessibility: a document-based Cocoa app (TextEdit, Pages, Preview, Xcode) sets AXDocument on each document
   window to the file's URL, moved when the document is saved under a new name. Read through System Events, which
   reads any app's AX attributes in the background. An app that does not set it (Chromium and Electron apps, some
   Office versions) gives none.
2. Scripting: the Standard Suite of any scriptable document app answers `path of document of window`, and does so
   even when the app exposes no accessibility windows yet -- a TextEdit launched in the background (`open -g`)
   had no AX windows at all ten seconds after launch (10-19), while its scripting answered at once.

It is how the execution layer tells which file a save through the app's own panel or cmd+S went to, and whether a
save happened at all: the window's document before and after the action, with the document's `modified` flag from
the Standard Suite. A file that merely changed on disk proves nothing (a sync program may have written it at that
moment); a document that was unsaved before the action and saved after it is the app's own word that it saved.

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


#: What is known of a window's document: its path (None for a document never saved, or an app that does not say) and
#: whether it has unsaved changes (None when the app's scripting did not answer that).
DocState = tuple[str | None, bool | None]


def _pick(lines: str, title: str, bounds: list | None) -> DocState | None:
    """Among "name<TAB>document<TAB>x<TAB>y<TAB>modified" lines, the state of the window titled `title`; among
    several, the one at `bounds`' position; a single untitled match when the title is empty. None: no such window."""
    rows = []
    for line in lines.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, doc = parts[0], _posix(parts[1])
        xy = None
        if len(parts) >= 4:
            try:
                xy = (float(parts[2]), float(parts[3]))
            except ValueError:
                xy = None
        modified = {"true": True, "false": False}.get(parts[4].strip().lower()) if len(parts) >= 5 else None
        if doc is None and modified is None:
            continue
        rows.append((name, doc, xy, modified))
    same = [r for r in rows if r[0] == (title or "")]
    # position tells same-titled windows apart on the accessibility rows; an app's scripted `bounds` is not the
    # screen rectangle (TextEdit, 10-19), so on those rows nothing is near. Two windows with the same title that
    # cannot be told apart are unknown, never "the first one" (PR #48 review: the user's notes.txt for the task's).
    if len(same) > 1 and bounds and None not in bounds[:2]:
        x, y = float(bounds[0]), float(bounds[1])
        near = [r for r in same if r[2] is not None and abs(r[2][0] - x) < 2 and abs(r[2][1] - y) < 2]
        same = near
    if not same and len(rows) == 1 and not title:
        same = rows
    return (same[0][1], same[0][3]) if len(same) == 1 else None


def window_document(pid: int, bundle: str, title: str, bounds: list | None) -> DocState | None:
    """(path, modified) of the document shown by the window titled `title` of the app `bundle` (pid `pid`), at
    `bounds` ([x, y, w, h] in screen points, or None); None when the app does not say. `modified` is known only
    through the app's scripting (accessibility has no such attribute), so a state from AXDocument alone is
    (path, None)."""
    # 1. accessibility, through System Events (any app that exposes its windows and sets AXDocument)
    ax = _osa(f'tell application "System Events" to tell (first process whose unix id is {int(pid)})\n'
              'set r to ""\nrepeat with w in windows\ntry\n'
              'set p to position of w\n'
              'set r to r & (name of w) & tab & (value of attribute "AXDocument" of w) & tab & (item 1 of p) & tab & (item 2 of p) & linefeed\n'
              'end try\nend repeat\nreturn r\nend tell')
    from_ax = _pick(ax or "", title, bounds)
    # 2. the app's own scripting (the Standard Suite: a document window's document has a path and a modified flag;
    #    a document never saved has the flag and no path). Asked even when accessibility gave the path: only the
    #    scripting says whether the document is saved.
    if bundle and bundle not in _NO_SCRIPTING:
        sc = _osa(f'tell application id "{_q(bundle)}"\nset r to ""\nrepeat with w in windows\ntry\n'
                  'set d to document of w\nset p to ""\nset m to ""\nset x to ""\nset y to ""\n'
                  'try\nset p to POSIX path of (path of d as text)\non error\ntry\nset p to (path of d as text)\nend try\nend try\n'
                  'try\nset m to (modified of d) as text\nend try\n'
                  'try\nset b to bounds of w\nset x to item 1 of b\nset y to item 2 of b\nend try\n'
                  'set r to r & (name of w) & tab & p & tab & x & tab & y & tab & m & linefeed\n'
                  'end try\nend repeat\nreturn r\nend tell')
        if sc is None:
            _NO_SCRIPTING.add(bundle)
        else:
            from_sc = _pick(sc, title, bounds)
            if from_sc is not None:
                if from_ax and from_ax[0] and from_sc[0] and from_sc[0] != from_ax[0]:
                    return (from_ax[0], None)      # the scripting matched another window of that title: its flag is not this one's
                return (from_sc[0] or (from_ax[0] if from_ax else None), from_sc[1])
    return from_ax


def document_text(bundle: str, title: str, bounds: list | None) -> str | None:
    """The text of the document shown by the window titled `title` of the app `bundle`, from the app's own scripting
    (the Standard Suite's `text of document`), character for character: a document's line breaks are what it holds
    (TextEdit saves LF as LF and CR as CR, 10-19), so nothing is normalised. None when the app does not answer or the
    document has no text (a drawing, a spreadsheet). What the app holds, not what is on disk: the digest of a save is
    taken from this, so a file another program rewrote after the app saved never matches it (PR #16 review,
    eleventh round)."""
    if not bundle or bundle in _NO_SCRIPTING:
        return None
    # by title only: an app's scripted `bounds of window` is not the screen rectangle Quartz reports (TextEdit gave
    # 152,73 for a window Quartz placed at 212,114 on 10-19), so `bounds` cannot tell same-titled windows apart here
    del bounds
    # one window of that title, or unknown: two cannot be told apart here, and the first one's text was another
    # document's (PR #48 review)
    # a window of that title whose text cannot be read (an app with no `text` on its documents: the read errors, or
    # gives no value) makes the answer unknown, never "" -- counted only after the read, an error used to leave the
    # empty start value standing, and an iWork-like document read as blank (hands#60 review)
    out = _osa(f'tell application id "{_q(bundle)}"\nset n to 0\nset bad to false\nset t to ""\nrepeat with w in windows\n'
               'set nm to ""\ntry\nset nm to (name of w)\nend try\n'
               f'if nm is "{_q(title or "")}" then\nset n to n + 1\ntry\nset v to (text of document of w)\n'
               'if v is missing value then\nset bad to true\nelse\nset t to (v as text)\nend if\n'
               'on error\nset bad to true\nend try\nend if\nend repeat\n'
               'if bad then return "unread"\nif n is 1 then return "ok" & linefeed & t\nreturn "n=" & n\nend tell')
    if out is None:
        _NO_SCRIPTING.add(bundle)
        return None
    if not out.startswith("ok\n"):
        return None
    text = out[3:]
    if text.endswith("\n"):          # osascript's own line end after the value
        text = text[:-1]
    return text

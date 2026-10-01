"""A deterministic virtual desktop backed by a real workspace directory.

This is not a toy. It exists because the harness itself has to be testable: a
task, its grader, its reset and its injections must be verifiable without a real
screen, a real model or a real API bill. Actions here mutate the *actual*
filesystem, so the same grader that scores a mock run scores a Peekaboo run
unchanged -- which is what makes the oracle check in ``verify-tasks`` meaningful.

Two apps and a decoy are enough to exercise every mechanism the reliability
suite cares about: selection, rename, text entry, save, clipboard, modals,
focus theft and window movement invalidating a binding.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..actions import Action, ActionKind
from ..geometry import ImageTransform, Rect, ScreenGeometry, Size, clamp_to_screen, to_logical
from .base import Driver, Element, ExecResult, Observation, WindowRef

LOGICAL = Size(1440, 900)
PIXELS = Size(2880, 1800)

FILES_WIN = Rect(40, 60, 620, 700)
EDITOR_WIN = Rect(700, 60, 660, 700)
NOTES_WIN = Rect(210, 520, 400, 260)
MODAL_WIN = Rect(500, 340, 440, 210)

ROW_H = 32
ROW_TOP = 150
ROW_X = 60
ROW_W = 580
MAX_ROWS = 15


@dataclass
class _Editor:
    doc: str | None = None       # path relative to workspace
    buffer: str = ""
    saved: str = ""
    select_all: bool = False

    @property
    def dirty(self) -> bool:
        return self.buffer != self.saved


class MockDriver:
    """Driver protocol over an in-process desktop model."""

    name = "mock"

    def __init__(self, *, render: bool = True) -> None:
        self.ws: Path = Path(".")
        self._render = render
        self.focused_app = "Files"
        self.selected: str | None = None
        self.rename_mode = False
        self.rename_buffer = ""
        #: One entry per open document window, keyed by window id. TextEdit
        #: opens a *window per document*, and a task that stages two of them was
        #: what exposed defect #12 -- so the mock has to be able to be in that
        #: state too, or the contract test cannot exist.
        self.editors: dict[str, _Editor] = {"editor": _Editor()}
        self.editor_window = "editor"
        self._clipboard = ""
        self.modal: str | None = None
        self.scroll_offset = 0
        self.layout_version = 0
        self.win_offset = (0.0, 0.0)     # applied to the Files window by injections
        self.writes: list[str] = []      # every filesystem mutation, in order
        self._obs_layout: dict[str, int] = {}
        self._obs_n = 0
        self._font = None

    @property
    def editor(self) -> _Editor:
        """The document window currently being acted on."""
        return self.editors[self.editor_window]

    def _open_in_editor(self, name: str, text: str) -> str:
        """Open a document, reusing an empty window and otherwise adding one.

        This is the behaviour that matters for the harness: a second document
        does not replace the first, it appears beside it, and only one of them
        is the target of the next keystroke.
        """
        for wid, ed in self.editors.items():
            if ed.doc == name:
                self.editor_window = wid
                return wid
        empty = next((w for w, e in self.editors.items() if e.doc is None), None)
        wid = empty or f"editor{len(self.editors) + 1}"
        self.editors[wid] = _Editor(doc=name, buffer=text, saved=text)
        self.editor_window = wid
        return wid

    def _window_refs(self) -> list[WindowRef]:
        app = self.focused_app
        if app == "Editor":
            return [WindowRef(id=w, title=(e.doc or "no document")
                              + (" (edited)" if e.dirty else ""),
                              active=(w == self.editor_window))
                    for w, e in self.editors.items()]
        wid = {"Files": "files", "Notes": "notes"}.get(app, "files")
        return [WindowRef(id=wid, title=self.ws.name, active=True)]

    # -- lifecycle ---------------------------------------------------------

    def capabilities(self) -> set[str]:
        return {"screenshot", "ax", "clipboard", "inject", "background_read",
                "focus_window"}

    def start(self, workspace: Path) -> None:
        self.ws = Path(workspace)
        self.focused_app = "Files"
        self.selected = None
        self.rename_mode = False
        self.rename_buffer = ""
        self.editors = {"editor": _Editor()}
        self.editor_window = "editor"
        self.modal = None
        self.scroll_offset = 0
        self.layout_version = 0
        self.win_offset = (0.0, 0.0)
        self.writes = []
        self._obs_layout = {}
        self._obs_n = 0

    def close(self) -> None:
        return None

    # -- world model -------------------------------------------------------

    def _files(self) -> list[str]:
        if not self.ws.is_dir():
            return []
        return sorted(p.name for p in self.ws.iterdir() if not p.name.startswith("."))

    def _files_win(self) -> Rect:
        dx, dy = self.win_offset
        return Rect(FILES_WIN.x + dx, FILES_WIN.y + dy, FILES_WIN.w, FILES_WIN.h)

    def _elements(self) -> list[Element]:
        els: list[Element] = []
        fw = self._files_win()
        dx, dy = self.win_offset

        els.append(Element(id="win:files", role="window", label="Files",
                           rect=fw, app="Files", window_id="files"))
        for name, rx in (("rename", 0), ("archive", 110), ("newfolder", 220)):
            els.append(Element(
                id=f"btn:{name}", role="button", label=name.capitalize(),
                rect=Rect(fw.x + 20 + rx, fw.y + 22, 100, 28),
                enabled=(self.selected is not None) if name in ("rename", "archive") else True,
                app="Files", window_id="files"))

        files = self._files()
        for i, name in enumerate(files[self.scroll_offset:self.scroll_offset + MAX_ROWS]):
            els.append(Element(
                id=f"row:{name}", role="row", label=name,
                rect=Rect(ROW_X + dx, ROW_TOP + dy + i * ROW_H, ROW_W, ROW_H - 2),
                focused=(name == self.selected),
                value="dir" if (self.ws / name).is_dir() else "file",
                app="Files", window_id="files"))

        if self.rename_mode:
            els.append(Element(id="field:rename", role="textfield", label="New name",
                               rect=Rect(fw.x + 20, fw.y + fw.h - 60, 400, 30),
                               value=self.rename_buffer, focused=True, settable=True,
                               app="Files", window_id="files"))

        # Every document window is listed; only the focused one exposes its
        # contents. A model that wants to read or write the other has to say so
        # with focus_window, which is exactly the move that did not exist before.
        for wid, ed in self.editors.items():
            active = wid == self.editor_window
            els.append(Element(id=f"win:{wid}", role="window",
                               label=f"Editor - {ed.doc or 'no document'}"
                                     + (" (edited)" if ed.dirty else "")
                                     + ("" if active else " [background]"),
                               rect=EDITOR_WIN, app="Editor", window_id=wid))
        aw = self.editor_window
        els.append(Element(id="btn:save", role="button", label="Save",
                           rect=Rect(EDITOR_WIN.x + 20, EDITOR_WIN.y + 22, 90, 28),
                           enabled=self.editor.doc is not None,
                           app="Editor", window_id=aw))
        els.append(Element(id="editor:body", role="textarea", label="Document body",
                           rect=Rect(EDITOR_WIN.x + 20, EDITOR_WIN.y + 80,
                                     EDITOR_WIN.w - 40, EDITOR_WIN.h - 110),
                           value=self.editor.buffer, settable=True,
                           focused=(self.focused_app == "Editor"),
                           app="Editor", window_id=aw))

        els.append(Element(id="win:notes", role="window", label="Notes",
                           rect=NOTES_WIN, app="Notes", window_id="notes"))

        if self.modal:
            els.append(Element(id="win:modal", role="dialog", label=self.modal,
                               rect=MODAL_WIN, app="Files", window_id="modal"))
            els.append(Element(id="btn:modal_ok", role="button", label="OK",
                               rect=Rect(MODAL_WIN.x + 250, MODAL_WIN.y + 150, 80, 30),
                               app="Files", window_id="modal"))
            els.append(Element(id="btn:modal_cancel", role="button", label="Cancel",
                               rect=Rect(MODAL_WIN.x + 340, MODAL_WIN.y + 150, 80, 30),
                               app="Files", window_id="modal"))
        return els

    # -- observation -------------------------------------------------------

    def observe(self) -> Observation:
        self._obs_n += 1
        obs_id = f"obs-{self._obs_n:04d}"
        self._obs_layout[obs_id] = self.layout_version
        els = self._elements()
        geometry = ScreenGeometry(LOGICAL, PIXELS)
        obs = Observation(
            id=obs_id,
            geometry=geometry,
            transform=ImageTransform.fit(PIXELS, 1456),
            screenshot_png=self._screenshot(els) if self._render else None,
            elements=els,
            focused_app=self.modal and "Files(modal)" or self.focused_app,
            window_title=self.ws.name,
            windows=self._window_refs(),
            layout_version=self.layout_version,
        )
        return obs

    def _screenshot(self, els: list[Element]) -> bytes | None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return None
        scale = PIXELS.w / LOGICAL.w
        img = Image.new("RGB", PIXELS.as_tuple(), (238, 238, 242))
        d = ImageDraw.Draw(img)
        if self._font is None:
            self._font = self._load_font(ImageFont)
        for e in els:
            if not e.rect:
                continue
            r = [e.rect.x * scale, e.rect.y * scale,
                 (e.rect.x + e.rect.w) * scale, (e.rect.y + e.rect.h) * scale]
            if e.role == "window":
                d.rectangle(r, fill=(252, 252, 253), outline=(180, 180, 188), width=3)
                d.rectangle([r[0], r[1], r[2], r[1] + 40 * scale / 2],
                            fill=(228, 228, 234))
            elif e.role == "dialog":
                d.rectangle(r, fill=(255, 255, 255), outline=(90, 90, 100), width=4)
            elif e.role == "button":
                d.rectangle(r, fill=(226, 232, 240) if e.enabled else (240, 240, 242),
                            outline=(150, 150, 160), width=2)
            elif e.role == "row":
                d.rectangle(r, fill=(200, 220, 250) if e.focused else (252, 252, 253),
                            outline=(225, 225, 230), width=1)
            elif e.role in ("textfield", "textarea"):
                d.rectangle(r, fill=(255, 255, 255), outline=(120, 140, 200) if e.focused else (200, 200, 208), width=2)
            label = e.label or ""
            if e.value and e.role in ("textfield", "textarea"):
                label = e.value[:600]
            if label and e.role != "window":
                self._text(d, (r[0] + 10, r[1] + 8), label, (30, 30, 36))
            elif label:
                self._text(d, (r[0] + 12, r[1] + 6), label, (60, 60, 70))
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False, compress_level=1)
        return buf.getvalue()

    def _load_font(self, ImageFont):
        for path, idx in (("/System/Library/Fonts/PingFang.ttc", 2),
                          ("/System/Library/Fonts/STHeiti Light.ttc", 0),
                          ("/System/Library/Fonts/Helvetica.ttc", 0)):
            try:
                return ImageFont.truetype(path, 26, index=idx)
            except (OSError, ValueError):
                continue
        return ImageFont.load_default()

    def _text(self, d, xy, s: str, fill) -> None:
        lines = s.split("\n")[:18]
        for i, ln in enumerate(lines):
            d.text((xy[0], xy[1] + i * 30), ln[:70], fill=fill, font=self._font)

    # -- execution ---------------------------------------------------------

    def _hit(self, action: Action, obs_els: list[Element] | None = None) -> Element | None:
        els = obs_els or self._elements()
        if action.binding.element_id:
            return next((e for e in els if e.id == action.binding.element_id), None)
        if action.point is None:
            return None
        geometry = ScreenGeometry(LOGICAL, PIXELS)
        pt = to_logical(action.point, action.coord_space, geometry, ImageTransform.fit(PIXELS, 1456))
        pt = clamp_to_screen(pt, geometry)
        # Topmost-last wins, matching draw order.
        hits = [e for e in els if e.rect and e.rect.contains(pt)
                and e.role not in ("window",)]
        return hits[-1] if hits else None

    def execute(self, action: Action) -> ExecResult:
        bid = action.binding.observation_id
        if bid and self._obs_layout.get(bid, self.layout_version) != self.layout_version:
            return ExecResult(False, f"binding {bid} predates layout v{self.layout_version}", stale=True)

        k = action.kind
        if self.modal and not (action.binding.element_id or "").startswith("btn:modal") \
           and k in {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.TYPE_TEXT}:
            el = self._hit(action)
            if not (el and el.window_id == "modal"):
                return ExecResult(False, f"blocked by modal dialog: {self.modal}")

        if k is ActionKind.WAIT:
            return ExecResult(True, f"waited {action.duration_s}s")
        if k is ActionKind.SCREENSHOT:
            return ExecResult(True, "observation refreshed")
        if k is ActionKind.FOCUS_APP:
            want = (action.text or "").strip()
            known = {"files": "Files", "editor": "Editor", "notes": "Notes"}
            app = known.get(want.lower().split(".")[-1])
            if app is None:
                return ExecResult(False, f"no app {want!r} on this desktop; "
                                         f"known: {sorted(known.values())}")
            self.focused_app = app
            return ExecResult(True, f"switched to {app}")
        if k is ActionKind.FOCUS_WINDOW:
            want = (action.text or "").strip()
            known = {w.id: w for w in self._window_refs()}
            if want not in known:
                have = ", ".join(f"{w.id} ({w.title})" for w in known.values()) or "none"
                return ExecResult(False,
                                  f"no window {want!r} in {self.focused_app}; windows: {have}")
            if self.focused_app == "Editor":
                if want == self.editor_window:
                    return ExecResult(True, f"already observing window {want}")
                self.editor_window = want
                self.layout_version += 1
            return ExecResult(True, f"focused window {want} ({known[want].title})")
        if k is ActionKind.MENU:
            # No menu bar is modelled here. Saying so is the point: a driver
            # that answers ok for an action it never performed is defect #1.
            return ExecResult(False, "this desktop has no menu bar", unsupported=True)
        if k is ActionKind.SCROLL:
            n = max(0, len(self._files()) - MAX_ROWS)
            self.scroll_offset = max(0, min(n, self.scroll_offset + (1 if action.scroll_dy > 0 else -1)))
            return ExecResult(True, f"scroll_offset={self.scroll_offset}")
        if k is ActionKind.DRAG:
            return ExecResult(False, "drag is not modelled by the mock desktop", unsupported=True)
        if k in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK):
            return self._click(action, double=(k is ActionKind.DOUBLE_CLICK))
        if k is ActionKind.TYPE_TEXT:
            return self._type(action.text or "", clear_first=action.clear_first)
        if k is ActionKind.KEY:
            return self._key(action.keys)
        return ExecResult(True, f"{k.value} noted")

    def _click(self, action: Action, *, double: bool) -> ExecResult:
        el = self._hit(action)
        if el is None:
            return ExecResult(False, "click landed on empty background")

        if el.id.startswith("btn:modal"):
            dismissed = self.modal
            self.modal = None
            self.layout_version += 1
            return ExecResult(True, f"dismissed modal {dismissed!r} via {el.id}")

        if el.id.startswith("row:"):
            name = el.id.split(":", 1)[1]
            self.selected = name
            self.focused_app = "Files"
            if double:
                p = self.ws / name
                if p.is_file():
                    try:
                        text = p.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as exc:
                        return ExecResult(False, f"cannot open {name}: {exc}")
                    wid = self._open_in_editor(name, text)
                    self.focused_app = "Editor"
                    self.layout_version += 1
                    return ExecResult(True, f"opened {name} in Editor window {wid}")
                return ExecResult(False, f"{name} is not an openable file")
            return ExecResult(True, f"selected {name}")

        if el.id == "btn:rename":
            if not self.selected:
                return ExecResult(False, "rename needs a selection")
            self.rename_mode = True
            self.rename_buffer = self.selected
            self.layout_version += 1
            return ExecResult(True, f"rename field open for {self.selected}")

        if el.id == "btn:archive":
            if not self.selected:
                return ExecResult(False, "archive needs a selection")
            src = self.ws / self.selected
            dst_dir = self.ws / "archive"
            dst_dir.mkdir(exist_ok=True)
            dst = dst_dir / (self.selected + ".bak")
            if src.is_file():
                dst.write_bytes(src.read_bytes())
                self.writes.append(f"archive:{self.selected}")
                return ExecResult(True, f"archived {self.selected}")
            return ExecResult(False, "only files can be archived")

        if el.id == "btn:newfolder":
            n = 1
            while (self.ws / f"untitled folder {n}").exists():
                n += 1
            (self.ws / f"untitled folder {n}").mkdir()
            self.writes.append(f"mkdir:untitled folder {n}")
            return ExecResult(True, f"created untitled folder {n}")

        if el.id == "btn:save":
            return self._save()

        if el.id == "editor:body":
            self.focused_app = "Editor"
            self.editor.select_all = False
            return ExecResult(True, "editor focused")

        if el.id == "field:rename":
            return ExecResult(True, "rename field focused")

        if el.id == "win:notes":
            self.focused_app = "Notes"
            return ExecResult(True, "Notes focused")

        return ExecResult(True, f"clicked {el.id}")

    def _type(self, text: str, *, clear_first: bool = False) -> ExecResult:
        if self.rename_mode:
            self.rename_buffer = text
            return ExecResult(True, f"rename buffer = {text!r}")
        if self.focused_app == "Editor":
            if self.editor.doc is None:
                return ExecResult(False, "no document open in Editor")
            if clear_first or self.editor.select_all:
                self.editor.buffer = text
                self.editor.select_all = False
                how = "replaced"
            else:
                self.editor.buffer += text
                how = "appended"
            return ExecResult(True, f"{how} {len(text)} chars in {self.editor.doc}")
        return ExecResult(False, f"nothing accepts text input (focus={self.focused_app})")

    def _key(self, keys: tuple[str, ...]) -> ExecResult:
        chord = "+".join(keys)
        if chord == "escape":
            if self.modal:
                self.modal = None
                self.layout_version += 1
                return ExecResult(True, "modal dismissed")
            if self.rename_mode:
                self.rename_mode = False
                self.layout_version += 1
                return ExecResult(True, "rename cancelled")
            return ExecResult(True, "escape ignored")

        if chord in ("return", "enter"):
            if self.rename_mode:
                return self._commit_rename()
            return ExecResult(True, "return ignored")

        if chord == "cmd+s":
            return self._save()
        if chord in ("delete", "backspace"):
            if self.focused_app != "Editor" or self.editor.doc is None:
                return ExecResult(False, "nothing focused to delete from")
            if self.editor.select_all:
                self.editor.buffer = ""
                self.editor.select_all = False
                return ExecResult(True, "deleted selection")
            if not self.editor.buffer:
                return ExecResult(True, "document already empty")
            self.editor.buffer = self.editor.buffer[:-1]
            return ExecResult(True, "deleted one character")
        if chord == "cmd+a":
            if self.focused_app == "Editor":
                self.editor.select_all = True
                return ExecResult(True, "select all in Editor")
            return ExecResult(True, "select all ignored")
        if chord == "cmd+c":
            if self.focused_app == "Editor" and self.editor.select_all:
                self._clipboard = self.editor.buffer
                return ExecResult(True, f"copied {len(self._clipboard)} chars")
            return ExecResult(False, "nothing selected to copy")
        if chord == "cmd+v":
            return self._type(self._clipboard)
        # Caret movement is meaningless here -- this editor always appends at the
        # end -- so say that rather than pretending the key worked.
        if chord in ("end", "home", "cmd+end", "cmd+home", "up", "down", "left", "right"):
            return ExecResult(False, f"{chord} is not modelled; this editor always types "
                                     "at the end of the document", unsupported=True)
        return ExecResult(False, f"key {chord} is not supported by this driver",
                          unsupported=True)

    def _commit_rename(self) -> ExecResult:
        old, new = self.selected, self.rename_buffer.strip()
        self.rename_mode = False
        self.layout_version += 1
        if not old:
            return ExecResult(False, "no selection to rename")
        if not new:
            return ExecResult(False, "empty name rejected")
        src, dst = self.ws / old, self.ws / new
        if dst.exists():
            return ExecResult(False, f"{new} already exists")
        try:
            os.rename(src, dst)
        except OSError as exc:
            return ExecResult(False, f"rename failed: {exc}")
        self.writes.append(f"rename:{old}->{new}")
        self.selected = new
        if self.editor.doc == old:
            self.editor.doc = new
        return ExecResult(True, f"renamed {old} -> {new}")

    def _save(self) -> ExecResult:
        if self.editor.doc is None:
            return ExecResult(False, "no document to save")
        p = self.ws / self.editor.doc
        try:
            p.write_text(self.editor.buffer, encoding="utf-8")
        except OSError as exc:
            return ExecResult(False, f"save failed: {exc}")
        self.editor.saved = self.editor.buffer
        self.writes.append(f"save:{self.editor.doc}")
        return ExecResult(True, f"saved {self.editor.doc} ({len(self.editor.buffer)} chars)")

    # -- injections --------------------------------------------------------

    def inject(self, event: str, params: dict) -> ExecResult:
        if event == "modal":
            self.modal = params.get("text", "Are you sure you want to continue?")
            self.layout_version += 1
            return ExecResult(True, f"modal shown: {self.modal!r}")
        if event == "focus_steal":
            self.focused_app = params.get("app", "Notes")
            return ExecResult(True, f"focus stolen by {self.focused_app}")
        if event == "move_window":
            self.win_offset = (float(params.get("dx", 120)), float(params.get("dy", 60)))
            self.layout_version += 1
            return ExecResult(True, f"Files window moved by {self.win_offset}")
        if event == "set_clipboard":
            self._clipboard = str(params.get("value", ""))
            return ExecResult(True, "clipboard seeded")
        return ExecResult(False, f"mock desktop cannot inject {event!r}", unsupported=True)

    # -- state for graders -------------------------------------------------

    def state(self) -> dict:
        return {
            "focused_app": self.focused_app,
            "selected": self.selected,
            "open_doc": self.editor.doc,
            "editor_dirty": self.editor.dirty,
            "modal": self.modal,
            "rename_mode": self.rename_mode,
            "layout_version": self.layout_version,
            "writes": list(self.writes),
            "writes_count": len(self.writes),
        }

    def clipboard(self) -> str | None:
        return self._clipboard


assert isinstance(MockDriver(render=False), Driver)

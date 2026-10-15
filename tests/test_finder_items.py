"""A Finder window's items read as files and folders whatever its view (deskmind#67: in icon view the names come as
AXImage, not AXTextField, so no move was offered and a planner made 报销 and finished with 报销单.csv left beside it)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskmind_hands.drivers.base import Element   # noqa: E402
from deskmind_hands.drivers.peekaboo import PeekabooDriver, mark_finder_items   # noqa: E402

NAMES = ("报销单.csv", "notes.txt", "发票.pdf")


def workspace() -> Path:
    ws = Path(tempfile.mkdtemp())
    for name in NAMES + (".DS_Store",):
        (ws / name).write_text("x")
    (ws / "项目").mkdir()
    return ws


def driver(ws: Path) -> PeekabooDriver:
    d = PeekabooDriver.__new__(PeekabooDriver)
    d.ws, d._app_name, d._window_id, d._undo = ws, "访达", "133", []
    d._project_finder_view = lambda folder: []
    return d


# As Peekaboo 4.7 reported the same folder in each view (macOS 27, 10-16).
LIST = [Element(id=f"t{i}", role="textField", ax_role="AXTextField", label=n, settable=True)
        for i, n in enumerate(NAMES + ("项目",))]
ICON = [Element(id=f"i{i}", role="image", ax_role="AXImage", label=n) for i, n in enumerate(NAMES + ("项目",))] + \
       [Element(id="g", role="group", ax_role="AXGroup", label="组"), Element(id="s", role="button", label="共享")]


def moves(els):
    return sorted(e.label for e in els if e.id.startswith("syn:move:"))


class Views(unittest.TestCase):
    def test_items_in_list_and_icon_view(self):
        ws = workspace()
        for shown in (LIST, ICON):
            els = [Element(**{**e.__dict__}) for e in shown]
            mark_finder_items(els, ws)
            roles = {e.label: e.role for e in els}
            self.assertEqual([roles[n] for n in NAMES], ["file"] * 3)
            self.assertEqual(roles["项目"], "folder")
            self.assertEqual(roles["共享"] if "共享" in roles else "button", "button")   # not an item: left alone

    def test_moves_offered_in_icon_view(self):
        ws = workspace()
        els = [Element(**{**e.__dict__}) for e in ICON]
        mark_finder_items(els, ws)
        out = driver(ws)._project_finder(ws, els)
        self.assertEqual(moves(out), sorted(f"移动「{n}」到" for n in NAMES))
        move = next(e for e in out if e.label == "移动「报销单.csv」到")
        self.assertIn("子文件夹 项目", move.options)

    def test_moves_offered_when_nothing_on_screen_reads_as_an_item(self):
        """A view whose items come in yet another shape: offered from the folder itself, hidden files left out."""
        ws = workspace()
        out = driver(ws)._project_finder(ws, [Element(id="g", role="group", label="组")])
        self.assertEqual(moves(out), sorted(f"移动「{n}」到" for n in NAMES))
        move = next(e for e in out if e.label == "移动「报销单.csv」到")
        self.assertIn("子文件夹 项目", move.options)

    def test_one_move_per_file(self):
        ws = workspace()
        els = [Element(**{**e.__dict__}) for e in LIST + ICON]
        mark_finder_items(els, ws)
        self.assertEqual(len(moves(driver(ws)._project_finder(ws, els))), 3)


class RecordedViews(unittest.TestCase):
    """The same folder as Peekaboo 4.7 read it on macOS 27 (10-16), one window switched between views: the folder's
    items and the elements around them, through the driver's own conversion. Recorded with 报销单.csv, notes.txt,
    todo.md, 发票.pdf, budget.xlsx, readme.txt and the subfolder 项目."""
    ITEMS = ("报销单.csv", "notes.txt", "todo.md", "发票.pdf", "budget.xlsx", "readme.txt")

    def offered(self, view: str) -> tuple[dict, list[str]]:
        import json
        from deskmind_hands.geometry import Point
        ws = Path(tempfile.mkdtemp())
        for n in self.ITEMS:
            (ws / n).write_text("x")
        (ws / "项目").mkdir()
        d = driver(ws)
        data = json.loads((Path(__file__).parent / "finder_views" / f"{view}.json").read_text())
        els = d._elements(data, Point(0, 0))
        mark_finder_items(els, ws)
        return {e.label: e.role for e in els if e.role in ("file", "folder")}, moves(d._project_finder(ws, els))

    def test_icon_view_offers_what_list_view_does(self):
        want = ({**{n: "file" for n in self.ITEMS}, "项目": "folder"}, sorted(f"移动「{n}」到" for n in self.ITEMS))
        self.assertEqual(self.offered("list"), want)
        self.assertEqual(self.offered("icon"), want)   # before deskmind#67's fix: ({}, [])


if __name__ == "__main__":
    unittest.main()

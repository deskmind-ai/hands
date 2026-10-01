"""Elements for apps with no accessibility tree, from the screenshot alone -- on this machine.

Some apps (Chromium Embedded ones, games, canvases) expose one element, their window, and refuse
AXManualAccessibility and AXEnhancedUserInterface. The driver finds that at run time (a sparse tree) and nothing here
knows any app by name. What a planner that reads text can be given instead:

  - the words on screen, from macOS Vision OCR (tools/native/ocr, on-device), each a clickable element at its box;
  - the words grouped into the list items they belong to (text_blocks), by geometry only;
  - a search box, when the screen's words vouch for one, and generic icon-only controls the goal could use, placed by
    the grounder when chosen (127.0.0.1; the screenshot is passed by path and never leaves the machine).

Build the OCR helper once: swiftc -O tools/native/ocr.swift -o tools/native/ocr (the binary is not committed).

Grounded icons are cached per window size. The grounder always answers with a point, even for a control that is not
on screen, so a control is only offered when something vouches for it and is marked absent when a click on it
changes nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

from .drivers.base import Element
from .apps import APPS
from .geometry import Rect

OCR_BIN = Path(__file__).resolve().parents[1] / "tools" / "native" / "ocr"
GROUNDER_URL = os.environ.get("HANDS_GROUNDER_URL", "http://127.0.0.1:8010/ground")

#: Apps observed through the screenshot from the start. None: an app is found to need it at run time (a sparse tree).
VISION_APPS: set[str] = set()

#: Per-app controls (name -> grounding description). None: every app gets the same generic controls below.
VISION_VOCAB: dict[str, dict[str, str]] = {}
TYPABLE = {"搜索框"}

#: For an app with no vocabulary of its own (found at run time to have no accessibility tree): only controls whose
#: presence the screen's own words vouch for. The grounder always returns a point, so a control offered without
#: evidence is a click on whatever happens to be nearest; a search box is offered when some text on screen says
#: "search", and is then snapped to that text like the music app's.
GENERIC_VOCAB = {"搜索框": "search input box"}

#: Icon-only controls most apps have, offered to the planner for an app with no vocabulary of its own. Nothing is
#: grounded when they are offered: the grounder answers every query with a point, so a control is only placed when
#: the planner chooses it, and a click that changes nothing on screen marks it absent for the rest of the run.
GENERIC_CONTROLS = {   # twelve: synthetic elements rank first, and more would crowd the screen's words out of 40
    "搜索框": "search input box",   # offered as a field (see vision_elements), listed here for its grounding query
    "播放按钮": "play button",
    "暂停按钮": "pause button",
    "下一个按钮": "next / skip forward button",
    "返回按钮": "back arrow button",
    "关闭按钮（面板/弹窗）": "close (x) button of the dialog or panel",
    "更多按钮（⋯）": "more options button (three dots)",
    "设置按钮": "settings (gear) button",
    "添加按钮（＋）": "add (plus) button",
    "发送按钮": "send button",
    "喜欢/收藏按钮": "like / favourite (heart or star) button",
    "分享按钮": "share button",
    "确定按钮（弹窗）": "confirm / OK button of the dialog",
}
#: Generic controls that act on something only a goal of that kind wants: offered only when the goal says so. Nothing
#: on the screen vouches for an icon-only control, and one that is always there lures a planner that is stuck: in a
#: music app with no send button, a planner asked to play a song clicked "发送按钮" nine times, each click landing on
#: whatever icon the grounder chose (the header's mail icon). Back, close and more stay: any task may need them.
GENERIC_NEEDS = {
    # Resuming only. A generic play button is grounded on the player's own play/pause, which acts on what is already
    # loaded: asked to play a searched-for song, the planner pressed it and paused (or changed) what the user was
    # listening to. A particular song is played by opening it.
    "播放按钮": r"继续播放|恢复播放|接着放|接着播|resume|continue playing|unpause",
    "暂停按钮": r"暂停|停下|停止|pause|stop",
    "下一个按钮": r"下一|切歌|跳过|next|skip",
    "设置按钮": r"设置|偏好|选项|setting|preference|option",
    "添加按钮（＋）": r"添加|新建|加入|加到|创建|add|new|create",
    "发送按钮": r"发送|发给|发一|回复|私信|消息|评论|send|reply|message|comment",
    "喜欢/收藏按钮": r"喜欢|收藏|红心|点赞|like|favou?rite|star|save",
    "分享按钮": r"分享|转发|share|forward",
    "确定按钮（弹窗）": r"确定|确认|同意|confirm|ok\b|agree",
}
SEARCH_WORDS = ("搜索", "search", "查找", "find")

_icon_cache: dict[tuple, dict[str, tuple[float, float]]] = {}


def ocr(shot: Path) -> list[dict]:
    """Text boxes on the screenshot, relative (0-1) to the image, top-left origin."""
    if not OCR_BIN.exists():
        return []
    rows: list[dict] = []
    # Read once more when the reader died or read nothing: the first use after a restart can fail inside the Vision
    # framework (an e5rt error, exit 1), and an empty result was taken as a screen with no text -- the planner then
    # filled a form from nothing (D5, 09-30).
    for _ in range(2):
        try:
            p = subprocess.run([str(OCR_BIN), str(shot)], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            # Transient (deskmind_hands/errors.py): a reader that hung once ended a run as a harness bug.
            rows = []
            continue
        rows = []
        for line in p.stdout.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        if p.returncode == 0 and rows:
            break
    return rows


def _icons(app: str, shot: Path, width: float, height: float, rows: list[dict] | None = None
           ) -> dict[str, tuple[float, float]]:
    vocab = VISION_VOCAB.get(app.lower())
    if vocab is None:
        words = " ".join((r.get("text") or "").lower() for r in rows or [])
        vocab = GENERIC_VOCAB if any(w in words for w in SEARCH_WORDS) else {}
    key = (app.lower(), round(width), round(height))
    if vocab and key not in _icon_cache:
        try:
            req = urllib.request.Request(GROUNDER_URL, data=json.dumps({
                "image": str(shot.resolve()), "size": [round(width), round(height)], "queries": vocab}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                pts = json.load(r)["points"]
        except (OSError, ValueError, KeyError):
            return {}
        _icon_cache[key] = {k: (v[0] * width, v[1] * height) for k, v in pts.items() if v}
    return _icon_cache.get(key, {})


#: Templated target descriptions (HANDS_DESCRIBED=1, experimental): for an icon with no text of its own, the
#: planner picks a description built from a control and an anchor on screen -- "the play button on the row
#: '最好的时光 · 安溥'" -- and the grounder places that exact string. Typed choices, so the planner needs no new
#: decoder; the templates are what limit which targets are reachable, and an oracle choosing among them measures
#: that ceiling. (control label for the planner, English phrase for the grounder)
ROW_CONTROLS = [("播放按钮", "play button"), ("更多按钮（⋯）", "more options button"),
                ("喜欢/收藏按钮", "like (heart) button"), ("下载按钮", "download button"), ("添加按钮（＋）", "add button")]
REGION_CONTROLS = [
    ("右上角的设置按钮", "the settings (gear) icon at the top right"),
    ("右上角的关闭按钮", "the close (x) button at the top right"),
    ("左上角的返回按钮", "the back arrow at the top left"),
    ("右上角的更多按钮（⋯）", "the more options (three dots) icon at the top right"),
    ("左上角的菜单按钮", "the menu (hamburger) icon at the top left"),
    ("底部栏的播放/暂停按钮", "the play / pause button in the bar at the bottom"),
    ("底部栏的下一首按钮", "the next track button in the bar at the bottom"),
    ("底部栏的上一首按钮", "the previous track button in the bar at the bottom"),
    ("底部栏的音量按钮", "the volume icon in the bar at the bottom"),
    ("弹窗里的确定按钮", "the confirm / OK button of the dialog"),
    ("弹窗里的关闭按钮", "the close button of the dialog"),
]
MAX_DESCRIBED = 40


def described_candidates(rows: list[dict], width: float, height: float) -> list[tuple[str, str]]:
    """(label, grounding query) pairs: region controls, then per-row controls and left/right-of icons anchored on the
    screen's text lines, main content first (not the left rail, not the top bar), capped at MAX_DESCRIBED."""
    out = list(REGION_CONTROLS)
    lines = [r for r in rows if 0.15 < r["x"] < 0.95 and 0.08 < r["y"] < 0.92
             and 2 <= len((r.get("text") or "").strip()) <= 40 and r.get("conf", 1) >= 0.3]
    # One anchor per visual line and column -- its longest text: a row of tabs ("全部 文档 图片 …") is one line, not
    # eight anchors, and on the first real page the tabs alone used up the whole cap before any song row.
    by_line: dict[tuple[int, int], dict] = {}
    for r in lines:
        key = (round((r["y"] + r["h"] / 2) * 60), 0 if r["x"] < 0.55 else 1)
        if key not in by_line or len(r.get("text") or "") > len(by_line[key].get("text") or ""):
            by_line[key] = r
    lines = sorted(by_line.values(), key=lambda r: (round(r["y"], 2), r["x"]))
    seen: set[str] = set()
    for r in lines:
        text = (r.get("text") or "").strip()
        if text in seen:
            continue
        seen.add(text)
        for label, q in ROW_CONTROLS[:3]:
            out.append((f"「{text}」这一行的{label}", f"the {q} on the row '{text}'"))
        out.append((f"「{text}」左边的图标", f"the icon to the left of '{text}'"))
        if len(out) >= MAX_DESCRIBED:
            break
    return out[:MAX_DESCRIBED]


def ground_one(shot: Path, width: float, height: float, description: str) -> tuple[float, float] | None:
    """One control placed by the grounder, in window-local points, or None if the grounder cannot be reached."""
    try:
        req = urllib.request.Request(GROUNDER_URL, data=json.dumps({
            "image": str(shot.resolve()), "size": [round(width), round(height)], "queries": {"q": description}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            pt = json.load(r)["points"].get("q")
    except (OSError, ValueError, KeyError):
        return None
    return (pt[0] * width, pt[1] * height) if pt else None


def vision_elements(app: str, shot: Path, width: float, height: float, app_name: str,
                    window_id: str, absent: set[str] | None = None,
                    fields: dict[str, Rect] | None = None) -> list[Element]:
    """Window-local elements (points) for an app with no accessibility tree."""
    out: list[Element] = []
    rows = sorted(ocr(shot), key=lambda r: (round(r["y"], 2), r["x"]))
    #: OCR rows that are a field's own text: shown as the field's value, not as something else to click. A planner
    #: that had just searched clicked the query text in the search box ten times, taking it for the result.
    in_field: set[int] = set()
    for name, (x, y) in _icons(app, shot, width, height, rows).items():
        rect = Rect(x - 14, y - 14, 28, 28)
        if name in TYPABLE:
            # A field is where its text is. The grounder put one app's search box 4 px past the end of the
            # placeholder text the box shows, the typing went nowhere, and three runs never got a result; an
            # earlier run, grounded 70 px to the left, had worked. The text on the same line nearest the grounded
            # point is taken as the field.
            near = [r for r in rows if abs((r["y"] + r["h"] / 2) * height - y) < 18
                    and abs((r["x"] + r["w"] / 2) * width - x) < 160]
            value = ""
            if near:
                r = min(near, key=lambda r: abs((r["x"] + r["w"] / 2) * width - x))
                rect = Rect(r["x"] * width, r["y"] * height, r["w"] * width, r["h"] * height)
                in_field.add(id(r))
                value = (r.get("text") or "").strip()
            elif app.lower() not in VISION_VOCAB:
                continue   # a generic search box with no text beside it is a guess, not a field
        out.append(Element(id=f"icon:{name}", role="textField" if name in TYPABLE else "button", label=name,
                           rect=rect, settable=name in TYPABLE, synthetic=True,
                           value=value if name in TYPABLE else None,
                           app=app_name, window_id=window_id))
    # Scrolling, as two buttons: a background wheel event does nothing in such an app, so it is carried out like a
    # click, in the foreground flash, over the middle of the window.
    for sid, label in (("scroll_down", "向下滚动页面"), ("scroll_up", "向上滚动页面")):
        out.append(Element(id=f"icon:{sid}", role="button", label=label,
                           rect=Rect(width * 0.6 - 14, height * 0.5 - 14, 28, 28), synthetic=True,
                           app=app_name, window_id=window_id))
    if os.environ.get("HANDS_DESCRIBED") == "1":
        for k, (label, query) in enumerate(described_candidates(rows, width, height)):
            if label not in (absent or ()):
                out.append(Element(id=f"desc:{k}", role="button", label=label, value=query, rect=Rect(0, 0, 1, 1),
                                   synthetic=True, app=app_name, window_id=window_id))
    elif app.lower() not in VISION_VOCAB:
        # A search field too, placed when typed into, unless the screen's own words already gave one (above). Most
        # apps have one, and its placeholder rarely says "search" -- the music app's shows the last query -- so a
        # run in an app we know nothing about had no field to type into at all and scrolled until it was stopped.
        if not any(e.id == "icon:搜索框" for e in out) and "搜索框" not in (absent or ()):
            if fields is not None and "搜索框" not in fields and "搜索框?" not in fields:
                # Looked for once, at the first observation: the box's own text (a query left from before, a
                # placeholder) is then its value and not one more item. Unplaced, the music app's box showed the last
                # query, "林夏 纸船", as a result-like item, and the planner double-clicked it three times. Kept only
                # when the grounded point has text beside it near the top of the window: the grounder answers every
                # query with a point, and a box placed on nothing would be invented.
                fields["搜索框?"] = None
                pt = ground_one(shot, width, height, GENERIC_CONTROLS["搜索框"])
                if pt and pt[1] < 0.25 * height:
                    near = [r for r in rows if abs((r["y"] + r["h"] / 2) * height - pt[1]) < 18
                            and abs((r["x"] + r["w"] / 2) * width - pt[0]) < 200]
                    if near:
                        r = min(near, key=lambda r: abs((r["x"] + r["w"] / 2) * width - pt[0]))
                        fields["搜索框"] = Rect(r["x"] * width, r["y"] * height, r["w"] * width, r["h"] * height)
            placed = (fields or {}).get("搜索框")
            value = ""
            if placed is not None:
                # Found by an earlier step: the text inside it is its value, not one more thing to click.
                for r in rows:
                    cx, cy = (r["x"] + r["w"] / 2) * width, (r["y"] + r["h"] / 2) * height
                    if abs(cy - (placed.y + placed.h / 2)) < 18 and abs(cx - (placed.x + placed.w / 2)) < 200:
                        in_field.add(id(r))
                        value = value or (r.get("text") or "").strip()
            out.append(Element(id="gen:搜索框", role="textField", label="搜索框", rect=placed or Rect(0, 0, 1, 1),
                               settable=True, synthetic=True, value=value or None,
                               app=app_name, window_id=window_id))
        # Placed only if chosen (see GENERIC_CONTROLS); the rect is a placeholder until then.
        # Not when the screen already shows the control's word as text ("▶ 播放"): the text is the real control,
        # and the generic one, placed by the grounder, went to another play button -- the player bar's, which
        # paused the song that was playing, and the run called that done.
        import re as _re
        words = {_re.sub(r"[^\w\u4e00-\u9fff]", "", r.get("text") or "") for r in rows}   # "▶ 播放" -> "播放"
        for name in GENERIC_CONTROLS:
            word = name.split("按钮")[0].split("（")[0].split("/")[0]
            if name != "搜索框" and name not in (absent or ()) and word not in words:
                out.append(Element(id=f"gen:{name}", role="button", label=name, rect=Rect(0, 0, 1, 1),
                                   synthetic=True, app=app_name, window_id=window_id))
    keep = [r for r in rows if (r.get("text") or "").strip() and r.get("conf", 1) >= 0.3 and id(r) not in in_field]
    for i, block in enumerate(text_blocks(keep, width, height)):
        lines = [" ".join((r.get("text") or "").strip() for r in line) for line in block]
        text = " · ".join(lines)
        if ("text:" + text) in (absent or ()):
            continue
        # A list item, not a button: words on screen are rows, titles and labels as often as controls, and a row is
        # opened (played, entered) by a double-click -- offered only CLICK, a planner could never play a song row.
        # One element per item (see text_blocks), placed on its first line: a click on the title opens the item.
        r = block[0][0]
        out.append(Element(id=f"ocr:{i}", role="listitem", label=text,
                           rect=Rect(r["x"] * width, r["y"] * height, r["w"] * width, r["h"] * height),
                           app=app_name, window_id=window_id))
    return out


def text_blocks(rows: list[dict], width: float, height: float) -> list[list[list[dict]]]:
    """OCR boxes grouped into the items they belong to: blocks of lines, each line a list of boxes, reading order.

    A list item on screen is a title with its details right under it -- a song and "Live 原唱 林夏", a playlist and
    "每日精选 42首", a person and their last message. Read as separate words, a planner could not tell which artist
    went with which title and double-clicked the page heading instead of a result. A line joins the block above it
    when it starts under that block's left edge, close under it, and in smaller type than the block's first line --
    or, in the same type, only as close as a wrapped line. Further words on a detail line join it when they follow
    closely. Tabs, menu entries (with or without an icon), a heading above a list and the rows of a table column
    stay single.
    Only geometry: nothing here knows any app.
    """
    boxes = sorted(rows, key=lambda r: (r["y"], r["x"]))
    px = lambda r: (r["x"] * width, r["y"] * height, r["w"] * width, r["h"] * height)
    blocks: list[list[list[dict]]] = []
    for r in boxes:
        x, y, w, h = px(r)
        home = None
        for b in blocks:
            bx, by, bw, bh = px(b[0][0])
            last = b[-1]
            lx, ly, lw, lh = px(last[0])
            lbottom = max(px(q)[1] + px(q)[3] for q in last)
            gap = y - lbottom
            # A detail is in smaller type than its title, close under it; a line of the same size joins only when it
            # is as close as a wrapped line. Same-size lines further apart are the next items of a list or table
            # column, and a heading above a list is further from its first item than an item from its details.
            # Left edges within 0.6 of a line: an OCR box often takes in the icon before a menu entry, and with a
            # looser 1.2 two entries of a podcast app's sidebar ("• 最近更新", "节目") read as a title and its detail.
            tight = gap <= 0.25 * lh
            close = gap <= max(0.25 * lh, 0.9 * min(h, lh))
            if abs(x - bx) <= 0.6 * bh and -0.5 * h < gap and close and h <= 1.3 * lh and (h < 0.95 * bh or tight) \
                    and abs(y - ly) > 0.5 * lh:
                home = ("below", b)
                break
            right = max(px(q)[0] + px(q)[2] for q in last)
            if len(b) > 1 and abs(y - ly) <= 0.5 * lh and -2 <= x - right <= 1.2 * lh:
                home = ("beside", b)   # another word on a detail line (the badges of "Live 原 林夏")
                break
        if home is None:
            blocks.append([[r]])
        elif home[0] == "below":
            home[1].append([r])
        else:
            home[1][-1].append(r)
    for b in blocks:
        for line in b:
            line.sort(key=lambda q: q["x"])
    return sorted(blocks, key=lambda b: (b[0][0]["y"], b[0][0]["x"]))

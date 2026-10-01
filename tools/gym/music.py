"""The music app: tasks (seeded), the true-state grader and the oracle.

A task is "play <title> by <artist>" in a made-up music app whose catalog holds decoys that a planner matching words
would pick: the same title by another artist, a live cut, a longer title containing the target's ("海边的纸船" for
"纸船"), and other songs by the same artist. The page reports what is playing; the grader and the oracle read that.
"""
from __future__ import annotations

import base64
import json
import random
import re

from tools.gym import split

#: Bumped when the labels this oracle gives change (see tools/gym/run.py rows).
ORACLE_VERSION = 4

ZH_FAMILY = list("林苏沈顾程许陆温季叶姜方白夏宋唐韩")
ZH_GIVEN = ["夏", "晚", "遥", "川", "言", "舟", "禾", "野", "澄", "朗", "吟", "岚", "屿", "星", "棠", "默"]
ZH_WORDS = ["纸船", "晚潮", "灯塔", "旧信", "雨季", "远山", "长街", "小镇", "白昼", "潮汐", "候鸟", "山海", "微光", "归途",
            "晴空", "落日", "月台", "海风", "风筝", "花火"]
EN_FIRST = ["Mira", "Kai", "Juniper", "Theo", "Ivy", "Rowan", "Lena", "Ezra", "Nora", "Silas", "Wren", "Otis"]
EN_LAST = ["Lune", "Solen", "Hart", "Vale", "Reed", "Marsh", "Quill", "Frost", "Bloom", "Ash", "Pike", "Stone"]
EN_WORDS = ["Paper Boats", "Evening Tide", "Salt and Cedar", "Lanterns", "Northbound", "Glass Harbor", "Low Tide",
            "Quiet Engine", "Driftwood", "Afterglow", "Harbor Lights", "Wildflower"]
APPS = [("Tunebox", "#c33"), ("Soundwell", "#2a6"), ("Chimevale", "#35c"), ("声海", "#b50"), ("听岛", "#727")]
TEXTS = {
    "zh": {"placeholder": "搜索歌曲、歌手", "search": "搜索", "play": "播放", "playAll": "播放全部", "pause": "暂停",
           "resume": "继续",
           "nowPlaying": "正在播放", "resultsFor": "“%s”的搜索结果", "home": "推荐"},
    "en": {"placeholder": "Search songs, artists", "search": "Search", "play": "Play", "playAll": "Play all",
           "pause": "Pause",
           "resume": "Resume", "nowPlaying": "Now playing", "resultsFor": "Results for “%s”", "home": "For you"},
}
GOALS = {
    "zh": ["在{app}里搜索{artist}的{title}并播放", "打开{app}，播放{artist}的《{title}》", "在{app}里放一下{artist}的{title}",
           "用{app}听{artist}的《{title}》"],
    "en": ["Play {title} by {artist} in {app}", "Open {app}, search {artist} {title} and play it",
           "In {app}, find {artist}'s {title} and start playing it"],
}
#: Held out for evaluation only: never in training rows.
HELDOUT_APPS = {"听岛"}


def _pools(heldout: bool) -> dict:
    """The value pools and goal templates a task draws from: all of them, or (training under split 2) without the
    held-out ones. The zh names are held out by family name, the en ones by first name."""
    if heldout:
        return {"zh_family": ZH_FAMILY, "zh_words": ZH_WORDS, "en_first": EN_FIRST, "en_words": EN_WORDS,
                "templates": {lang: list(range(len(g))) for lang, g in GOALS.items()}}
    held = {lang: split.heldout_template("music", lang, [f"music/{lang}/{i}" for i in range(len(g))])
            for lang, g in GOALS.items()}
    return {"zh_family": split.train_values("music", "zh_family", ZH_FAMILY),
            "zh_words": split.train_values("music", "zh_words", ZH_WORDS),
            "en_first": split.train_values("music", "en_first", EN_FIRST),
            "en_words": split.train_values("music", "en_words", EN_WORDS),
            "templates": {lang: [i for i in range(len(g)) if f"music/{lang}/{i}" != held[lang]]
                          for lang, g in GOALS.items()}}


def make_task(seed: int, split_version: int = split.DEFAULT_VERSION) -> dict:
    """The task of `seed`. Split 1 (the default, every row so far): held out by skin only. Split 2: concept-disjoint
    (see tools/gym/split.py)."""
    base = _make(seed, random.Random(seed), _pools(True))
    if split_version == 1:
        return base
    return split.pick(seed, "music", base["split"], lambda rnd, heldout: _make(seed, rnd, _pools(heldout)),
                      lambda t: t["app"] in HELDOUT_APPS)


def _make(seed: int, rnd: random.Random, pools: dict) -> dict:
    lang = rnd.choice(["zh", "en"])
    app, color = rnd.choice(APPS)
    if lang == "zh":
        name = lambda: rnd.choice(pools["zh_family"]) + rnd.choice(ZH_GIVEN) + rnd.choice(["", rnd.choice(ZH_GIVEN)])
        words = pools["zh_words"]
    else:
        name = lambda: f"{rnd.choice(pools['en_first'])} {rnd.choice(EN_LAST)}"
        words = pools["en_words"]
    artist, other = name(), name()
    while other == artist:
        other = name()
    title = rnd.choice(words)
    longer = (("海边的" if lang == "zh" else "Beyond the ") + title) if rnd.random() < 0.7 else None
    song = lambda t, a, al=None: {"title": t, "artist": a, "album": al or t}
    catalog = [song(title, artist), song(title, other),
               song(f"{title} (Live)", artist, title)]
    if longer:
        catalog.append(song(longer, name()))
    catalog += [song(rnd.choice(words), artist) for _ in range(2)]
    catalog += [song(rnd.choice(words), name()) for _ in range(4)]
    rnd.shuffle(catalog)
    home = [song(rnd.choice(words), name()) for _ in range(3)]
    now = rnd.choice(home)
    # An index into the templates, drawn as a choice of the template itself was (the same draw), so it is known.
    ti = rnd.choice(pools["templates"][lang])
    goal = GOALS[lang][ti].format(app=app, artist=artist, title=title)
    target = {"title": title, "artist": artist}
    # From seed 30000: in a third of the tasks the version is asked for at the end of the goal ("..., 要现场版"), so
    # the live cut is the target and the studio one the decoy -- a planner reading the first half played the studio
    # cut. Drawn apart, so earlier seeds are unchanged.
    v3 = random.Random(f"v3-{seed}") if seed >= 30000 else None
    if v3 is not None and v3.random() < 0.35:
        target = {"title": f"{title} (Live)", "artist": artist}
        goal = goal.rstrip("。.") + ("，要它的现场版（Live）" if lang == "zh" else ", the live version")
    template_id = f"music/{lang}/{ti}"
    name_pool, name_key = ("zh_family", artist[0]) if lang == "zh" else ("en_first", artist.split()[0])
    return {"seed": seed, "lang": lang, "app": app, "color": color, "goal": goal,
            "target": target, "query": f"{artist} {title}",
            "split": "heldout" if app in HELDOUT_APPS else "train",
            "split_version": 1, "template_id": template_id,
            "template_heldout": template_id == split.heldout_template(
                "music", lang, [f"music/{lang}/{i}" for i in range(len(GOALS[lang]))]),
            "concepts": [split.concept("title", "music", f"{lang}_words", title, ZH_WORDS if lang == "zh" else EN_WORDS),
                         split.concept("artist", "music", name_pool, name_key,
                                       ZH_FAMILY if lang == "zh" else EN_FIRST)],
            "page": {"app": app, "color": color, "texts": TEXTS[lang], "catalog": catalog, "home": home,
                     "nowPlaying": {"title": now["title"], "artist": now["artist"]},
                     "searchButton": True, "hoverPlay": rnd.random() < 0.3, **_look(seed),
                     # For the page's own script only (the result order below): never shown, not in the tree.
                     "target": {"title": title, "artist": artist}}}


#: How results are laid out. Drawn from their own generator, so a seed's songs and goal are what they were before
#: layouts existed (the v1 rows stay reproducible); v1 pages were all "table" with a "results for" heading.
LAYOUTS = ("table", "list2", "inline", "cards")


def _look(seed: int) -> dict:
    r = random.Random(f"look-{seed}")
    # The heading can be the query itself ("林夏 纸船"), as some apps show it: an echo of the query, never a result.
    # Results as apps show them: everything that matches a word of the query (the same title by others, the artist's
    # other songs), best matches first -- and in most tasks a decoy (the live cut, another artist's song of that
    # title) ranked above the target, so "play all", which starts at the first result, plays the wrong song.
    # The player bar says what is playing in words ("正在播放：…") in some apps and only shows "title — artist" in
    # others; a planner has to recognise the second too.
    return {"layout": r.choice(LAYOUTS), "echoHeading": r.random() < 0.4, "playAll": r.random() < 0.7,
            "decoyFirst": r.random() < 0.6, "barLabel": r.random() < 0.4}


def page_hash(task: dict) -> str:
    return base64.b64encode(json.dumps(task["page"], ensure_ascii=False).encode()).decode()


def passed(task: dict, state: dict, answer: str | None = None) -> bool:
    p = state.get("playing") or {}
    return p.get("title") == task["target"]["title"] and p.get("artist") == task["target"]["artist"] \
        and not state.get("paused")


def _exact_row(els: list[dict], t: dict) -> dict | None:
    """The row named "title · artist · ..." (HANDS_NAME_ROWS) that can be opened: the target's own row."""
    def exact(label: str) -> bool:
        parts = (label or "").split(" · ")
        return parts[:2] == [t["title"], t["artist"]] or _norm(parts[0]) == _norm(t["title"] + t["artist"])
    return next((e for e in els if e.get("role") == "row" and "OPEN" in (e.get("operations") or [])
                 and exact(e.get("label"))), None)


def _norm(s: str) -> str:
    # OCR reads a dash as "ー" or "一" as often as "—".
    return re.sub(r"[\s()（）\-—–ー一·•:：|]", "", s or "").lower()


def _sim(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _vision_item(els: list[dict], t: dict) -> dict | None:
    """The target's own item among vision elements (OCR words grouped into items): its first line is the title, or
    "title — artist", and the artist is in it. Not a "(Live)" cut, a longer title, or the heading echoing the query.
    OCR misreads a character now and then ("溫晚" for "温晚"): among the items whose title matches, the one whose
    other words are most like the artist, if they are at least half like it -- another singer's name is not."""
    title, artist = _norm(t["title"]), _norm(t["artist"])
    best, best_score = None, 0.0
    ocr = [e for e in els if str(e.get("id", "")).startswith("ocr:")]
    for i, e in enumerate(ocr):
        if "OPEN" not in (e.get("operations") or []):
            continue
        lines = (e.get("label") or "").split(" · ")
        first = _norm(lines[0])
        rest = _norm(" ".join(lines[1:]))
        if first.startswith(title) and len(first) > len(title):
            # "title — artist" on one line, or a longer title / a live cut
            tail = first[len(title):]
            score = _sim(tail, artist) if "live" not in tail else 0.0
        elif _sim(first, title) >= 0.8 and "live" not in first:
            # the details under the title, or (a table) the next cell
            nxt = _norm(ocr[i + 1].get("label")) if i + 1 < len(ocr) else ""
            score = max(max((_sim(w, artist) for w in re.split(r"[\s·•・]+", " ".join(lines[1:])) if w),
                            default=0.0), _sim(rest[:len(artist) + 2], artist), _sim(nxt, artist))
        else:
            continue
        if score > best_score:
            best, best_score = e, score
    return best if best_score >= 0.5 else None


def oracle(task: dict, app_state: dict, request: dict) -> tuple[dict | None, str]:
    """The right answer to this request, keyed by the request's own option keys, and why; None if not offered."""
    q = request["questions"]
    els = request["state"]["elements"]
    if any(str(e.get("id", "")).startswith(("ocr:", "gen:", "icon:")) for e in els):
        return _vision_oracle(task, app_state, request)
    texts = task["page"]["texts"]
    t = task["target"]

    def one_hot(head: str, key: str) -> dict:
        return {"type": "choice", "choice": key, "confidence": 1.0,
                "probabilities": {k: (1.0 if k == key else 0.0) for k in q[head]["criteria"]}}

    def pick(op: str, why: str, **heads) -> tuple[dict | None, str]:
        if op not in q["operation"]["criteria"]:
            return None, f"{op} not offered ({why})"
        out = {"operation": one_hot("operation", op)}
        for head, key in heads.items():
            if key is None or head not in q or key not in q[head]["criteria"]:
                return None, f"{head} option missing ({why})"
            out[head] = one_hot(head, key)
        return out, why

    def el(pred):
        return next((e for e in els if pred(e)), None)

    if passed(task, app_state):
        return pick("DONE", "the target is playing")
    playing = app_state.get("playing") or {}
    if playing.get("title") == t["title"] and playing.get("artist") == t["artist"] and app_state.get("paused"):
        b = el(lambda e: e.get("label") == texts["resume"])
        return pick("CLICK", "the target is loaded but paused: resume it", click_target=b and b["index"])
    results = app_state.get("results") or []
    if any(r["title"] == t["title"] and r["artist"] == t["artist"] for r in results):
        b = el(lambda e: e.get("label") == f"{texts['play']} {t['title']} - {t['artist']}")
        if b is not None and "CLICK" in (b.get("operations") or []):
            label, why = pick("CLICK", "the exact row (title and artist) is listed: play it", click_target=b["index"])
            # Double-clicking the exact row plays it too (the page binds dblclick on every row), so OPEN on that row
            # is as right as the play button: both get mass (oracle v2).
            row = _exact_row(els, t)
            if label is not None and row is not None and "OPEN" in q["operation"]["criteria"] \
                    and row["index"] in (q.get("open_target") or {}).get("criteria", {}):
                label["operation"]["probabilities"].update({"CLICK": 0.5, "OPEN": 0.5})
                label["open_target"] = one_hot("open_target", row["index"])
                why += " (or open the exact row, which plays it too)"
            return label, why
        # Play shown only on hover: the row itself is opened (double-click) -- the title cell whose neighbour is
        # the target's artist, not a same-titled row by someone else.
        # A row named by its cells (HANDS_NAME_ROWS): "title · artist · album".
        row = _exact_row(els, t)
        if row is not None:
            return pick("OPEN", "the exact row is listed but its play button is hidden: open the row",
                        open_target=row["index"])
        for i, e in enumerate(els):
            if e.get("label") == t["title"] and "OPEN" in (e.get("operations") or []) and \
                    any(x.get("label") == t["artist"] for x in els[i + 1:i + 3]):
                return pick("OPEN", "the exact row is listed but its play button is hidden: open the row",
                            open_target=e["index"])
        return None, "the target row is listed but neither its play button nor the row is offered"
    box = el(lambda e: e.get("label") == texts["placeholder"])
    if (app_state.get("query") or "").strip() == task["query"] and app_state.get("searched") != task["query"]:
        b = el(lambda e: e.get("label") == texts["search"])
        return pick("CLICK", "the query is typed but not submitted: press the search button",
                    click_target=b and b["index"])
    vals = (q.get("type_text_value") or {}).get("criteria") or {}
    key = next((k for k, v in vals.items() if (v.get("value") or "").strip() == task["query"]), None)
    return pick("TYPE_TEXT", "search for artist and title", type_text_target=box and box["index"], type_text_value=key)


def _vision_oracle(task: dict, app_state: dict, request: dict) -> tuple[dict | None, str]:
    """The oracle for a vision-mode request: the words on screen grouped into items, generic controls, no roles."""
    q = request["questions"]
    els = request["state"]["elements"]
    texts = task["page"]["texts"]
    t = task["target"]

    def one_hot(head: str, key: str) -> dict:
        return {"type": "choice", "choice": key, "confidence": 1.0,
                "probabilities": {k: (1.0 if k == key else 0.0) for k in q[head]["criteria"]}}

    def pick(op: str, why: str, **heads) -> tuple[dict | None, str]:
        if op not in q["operation"]["criteria"]:
            return None, f"{op} not offered ({why})"
        out = {"operation": one_hot("operation", op)}
        for head, key in heads.items():
            if key is None or head not in q or key not in q[head]["criteria"]:
                return None, f"{head} option missing ({why})"
            out[head] = one_hot(head, key)
        return out, why

    def text(word: str):
        # The item that reads as the word, allowing for one misread character ("搜案" for "搜索").
        cands = [(e, _sim((e.get("label") or "").split(" · ")[0], word)) for e in els
                 if str(e.get("id", "")).startswith("ocr:") and len((e.get("label") or "").split(" · ")[0]) <= len(word) + 3]
        e, score = max(cands, key=lambda c: c[1], default=(None, 0.0))
        return e["index"] if e is not None and score >= 0.5 else None

    if passed(task, app_state):
        return pick("DONE", "the target is playing")
    playing = app_state.get("playing") or {}
    if playing.get("title") == t["title"] and playing.get("artist") == t["artist"] and app_state.get("paused"):
        return pick("CLICK", "the target is loaded but paused: resume it", click_target=text(texts["resume"]))
    results = app_state.get("results") or []
    if any(r["title"] == t["title"] and r["artist"] == t["artist"] for r in results):
        item = _vision_item(els, t)
        if item is None:
            return None, "the target is listed but no item on screen reads as it (OCR)"
        return pick("OPEN", "open the exact item (title and artist): it plays", open_target=item["index"])
    if (app_state.get("query") or "").strip() == task["query"] and app_state.get("searched") != task["query"]:
        return pick("CLICK", "the query is typed but not submitted: press the search button",
                    click_target=text(texts["search"]))
    box = next((e for e in els if e.get("label") == "搜索框" and "TYPE_TEXT" in (e.get("operations") or [])), None)
    vals = (q.get("type_text_value") or {}).get("criteria") or {}
    key = next((k for k, v in vals.items() if (v.get("value") or "").strip() == task["query"]), None)
    return pick("TYPE_TEXT", "search for artist and title", type_text_target=box and box["index"],
                type_text_value=key)


def _choose(q: dict, op: str, **heads) -> dict | None:
    """A one-hot answer in the request's own keys, or None if the request does not offer it."""
    if op not in q["operation"]["criteria"]:
        return None
    hot = lambda head, key: {"type": "choice", "choice": key, "confidence": 1.0,
                             "probabilities": {k: (1.0 if k == key else 0.0) for k in q[head]["criteria"]}}
    out = {"operation": hot("operation", op)}
    for head, key in heads.items():
        if key is None or head not in q or key not in q[head]["criteria"]:
            return None
        out[head] = hot(head, key)
    return out


def trap(task: dict, app_state: dict, request: dict) -> tuple[dict, str] | None:
    """A plausible wrong step once the target is listed: the live cut, or the same title by someone else, opened.
    What follows looks finished (a song of that name plays) and is not."""
    t = task["target"]
    if not any(r["title"] == t["title"] and r["artist"] == t["artist"] for r in app_state.get("results") or []):
        return None
    base = t["title"].replace(" (Live)", "")
    if (app_state.get("playing") or {}).get("title", "").startswith(base):
        return None
    els, q = request["state"]["elements"], request["questions"]
    title = _norm(base)          # the song's own name: the live cut and the studio cut both start with it
    for e in els:
        lines = (e.get("label") or "").split(" · ")
        first = _norm(lines[0])
        ok_kind = e.get("role") == "row" or str(e.get("id", "")).startswith("ocr:")
        if not ok_kind or "OPEN" not in (e.get("operations") or []) or not first.startswith(title):
            continue
        rest = _norm(" ".join(lines[1:])) or first[len(title):]
        is_target = first == _norm(t["title"]) and _sim(rest[:len(_norm(t["artist"])) + 2], t["artist"]) >= 0.5
        if not is_target:
            label = _choose(q, "OPEN", open_target=e["index"])
            if label:
                return label, f"trap: opened {e.get('label')!r} instead of the target"
    return None

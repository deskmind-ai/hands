"""Two apps, one goal: read a song in the mail app, play it in the music app.

The goal names the email, not the song: the planner has to open the message, read what it recommends, move to the
music app's window (the two are windows of one host, reached with FOCUS_WINDOW) and play that song there -- the exact
song, among the music app's decoys. Graded by the music page's true state; the oracle reads both pages.
"""
from __future__ import annotations

import random

from tools.gym import mail, music, split

ORACLE_VERSION = 2

SUBJECTS = {"zh": ["周末歌单", "推荐一首歌", "路上听的", "今天循环的歌"],
            "en": ["Weekend song", "A song for you", "On repeat today", "For the drive"]}
GOALS = {
    "zh": ["看一下{mailapp}里{sender}发的“{subject}”，把里面推荐的歌在{musicapp}里放出来",
           "{sender}在{mailapp}里给我推荐了一首歌（“{subject}”那封），去{musicapp}里播放它"],
    "en": ["Open {sender}'s \"{subject}\" email in {mailapp} and play the song it recommends in {musicapp}",
           "{sender} recommended a song in {mailapp} (the \"{subject}\" email): play it in {musicapp}"],
}
LINE = {"zh": "推荐你听：《{title}》— {artist}", "en": "You should hear \"{title}\" by {artist}"}


def _template_ids(lang: str) -> list[str]:
    return [f"mailmusic/{lang}/{i}" for i in range(len(GOALS[lang]))]


def _pools(heldout: bool) -> dict:
    """The email subjects and goal templates of this family's own: all, or (training under split 2) without the
    held-out ones. The song and the inbox come from the music and mail families' own pools."""
    if heldout:
        return {"subjects": SUBJECTS, "templates": {lang: list(range(len(g))) for lang, g in GOALS.items()}}
    held = {lang: split.heldout_template("mailmusic", lang, _template_ids(lang)) for lang in GOALS}
    return {"subjects": {lang: split.train_values("mailmusic", f"{lang}_subjects", v) for lang, v in SUBJECTS.items()},
            "templates": {lang: [i for i in range(len(g)) if f"mailmusic/{lang}/{i}" != held[lang]]
                          for lang, g in GOALS.items()}}


def make_task(seed: int, split_version: int = split.DEFAULT_VERSION) -> dict:
    """The task of `seed`. Split 1 (the default, every row so far): held out by skin only. Split 2: concept-disjoint
    (see tools/gym/split.py) -- the song, the people and subjects, and the goal template."""
    m = music.make_task(seed)                       # the song, its decoys and the music app's page
    lang = m["lang"]
    base = mail.make_task(seed * 7 + 1)             # an inbox to put the message in
    if base["lang"] != lang:
        base = next(mail.make_task(s) for s in range(seed * 7 + 2, seed * 7 + 60) if mail.make_task(s)["lang"] == lang)
    task = _build(seed, random.Random(f"mailmusic-{seed}"), m, base, _pools(True))
    if split_version == 1:
        return task

    def make(rnd, heldout):
        m2 = music._make(seed, random.Random(rnd.random()), music._pools(heldout))
        mail_pools = mail._pools(heldout)
        base2 = next(b for b in (mail._make(seed * 7 + 1, random.Random(rnd.random()), mail_pools) for _ in range(60))
                     if b["lang"] == m2["lang"])
        return _build(seed, rnd, m2, base2, _pools(heldout))

    return split.pick(seed, "mailmusic", task["split"], make,
                      lambda t: t["apps"][0] in mail.HELDOUT_APPS or t["apps"][1] in music.HELDOUT_APPS)


def _build(seed: int, rnd: random.Random, m: dict, base: dict, pools: dict) -> dict:
    lang = m["lang"]
    page = dict(base["page"])
    inbox = [dict(x) for x in page["inbox"]]
    target = next(x for x in inbox if x["id"] == base["target"]["id"])
    subject = rnd.choice(pools["subjects"][lang])
    t = m["target"]
    target["subject"] = subject
    target["body"] = ["你好，" if lang == "zh" else "Hi,",
                      LINE[lang].format(title=t["title"], artist=t["artist"]),
                      "谢谢" if lang == "zh" else "Thanks"]
    # Another message from someone else recommending a different song: the one to read is the named email.
    decoy = next(x for x in inbox if x["from"] != target["from"])
    other = next((s for s in m["page"]["catalog"] if (s["title"], s["artist"]) != (t["title"], t["artist"])), None)
    if other:
        decoy["subject"] = rnd.choice([s for s in pools["subjects"][lang] if s != subject])
        decoy["body"] = [decoy["body"][0], LINE[lang].format(title=other["title"], artist=other["artist"]),
                         decoy["body"][-1]]
    page["inbox"] = inbox
    page["flagged"] = []
    # An index into the templates, drawn as a choice of the template itself was (the same draw), so it is known.
    ti = rnd.choice(pools["templates"][lang])
    goal = GOALS[lang][ti].format(mailapp=page["app"], sender=target["from"], subject=subject, musicapp=m["app"])
    split_name = "heldout" if (page["app"] in mail.HELDOUT_APPS or m["app"] in music.HELDOUT_APPS) else "train"
    template_id = f"mailmusic/{lang}/{ti}"
    sender = [c for c in base["concepts"] if c["slot"] == "sender"]
    return {"seed": seed, "lang": lang, "goal": goal, "target": t, "message": target["id"],
            "apps": [page["app"], m["app"]], "app": page["app"], "split": split_name, "music_task": m,
            "split_version": 1, "template_id": template_id,
            "template_heldout": template_id == split.heldout_template("mailmusic", lang, _template_ids(lang)),
            "concepts": m["concepts"] + sender + [split.concept("subject", "mailmusic", f"{lang}_subjects", subject,
                                                               SUBJECTS[lang])],
            "pages": {"mail": page, "music": m["page"]}}


def passed(task: dict, state: dict, answer: str | None = None) -> bool:
    return music.passed(task["music_task"], state.get("music") or {})


def oracle(task: dict, app_state: dict, request: dict) -> tuple[dict | None, str]:
    q = request["questions"]
    here = ((request["state"].get("page") or {}).get("title") or "").strip()
    mail_app, music_app = task["apps"]
    ms, mu = app_state.get("mail") or {}, app_state.get("music") or {}
    t = task["target"]

    def one_hot(head: str, key: str) -> dict:
        return {"type": "choice", "choice": key, "confidence": 1.0,
                "probabilities": {k: (1.0 if k == key else 0.0) for k in q[head]["criteria"]}}

    def switch(to: str, why: str):
        if "FOCUS_WINDOW" not in q["operation"]["criteria"]:
            return None, f"FOCUS_WINDOW not offered ({why})"
        crit = (q.get("focus_window_target") or {}).get("criteria") or {}
        key = next((k for k, v in crit.items() if str(v).split(" (")[0].strip() == to), None)
        if key is None:
            return None, f"focus_window_target option missing ({why})"
        return {"operation": one_hot("operation", "FOCUS_WINDOW"), "focus_window_target": one_hot(
            "focus_window_target", key)}, why

    if passed(task, app_state):
        if here != music_app:
            return switch(music_app, "the song is playing: look at the music app to see it before finishing")
        return music.oracle(task["music_task"], mu, request)          # DONE, with the player in view
    # Read once is read: the message stays read after another one is selected (v1 looked only at the selection, and
    # sent the planner back to the mail app after it had moved on to search).
    read = task["message"] in (ms.get("opened") or []) or ms.get("selected") == task["message"] \
        or t["title"] in (mu.get("query") or "")
    if not read:
        if here != mail_app:
            return switch(mail_app, "the song is named in the email: go to the mail app")
        # The mail app's own oracle opens the named message (an "ask" task stops there).
        mt = {"kind": "ask", "target": {"id": task["message"], "from": None, "subject": None},
              "page": task["pages"]["mail"], "answer": "", "already": False, "body": ""}
        msg = next(x for x in task["pages"]["mail"]["inbox"] if x["id"] == task["message"])
        mt["target"].update({"from": msg["from"], "subject": msg["subject"]})
        label, why = mail.oracle(mt, ms, request)
        return (label, why) if label is None or "operation" not in label or \
            max(label["operation"]["probabilities"], key=label["operation"]["probabilities"].get) != "ANSWER" \
            else switch(music_app, "the song is read: go to the music app")
    if here != music_app:
        return switch(music_app, "the song is read: go to the music app and play it")
    # In the music app: whatever query holds the title counts as the search for it.
    vals = (q.get("type_text_value") or {}).get("criteria") or {}
    typed = (mu.get("query") or "").strip()
    query = typed if t["title"] in typed else next(
        (v.get("value").strip() for v in vals.values()
         if t["title"] in (v.get("value") or "") and t["artist"] in (v.get("value") or "")), None) or next(
        (v.get("value").strip() for v in vals.values() if t["title"] in (v.get("value") or "")), t["title"])
    return music.oracle({**task["music_task"], "query": query}, mu, request)


def trap(task: dict, app_state: dict, request: dict) -> tuple[dict, str] | None:
    """The other email (it recommends a different song) opened, or the wrong version played in the music app."""
    here = ((request["state"].get("page") or {}).get("title") or "").strip()
    if here == task["apps"][1]:
        return music.trap(task["music_task"], app_state.get("music") or {}, request)
    msg = next(x for x in task["pages"]["mail"]["inbox"] if x["id"] == task["message"])
    return mail.trap({"target": {"id": msg["id"], "from": msg["from"], "subject": msg["subject"]}},
                     app_state.get("mail") or {}, request)

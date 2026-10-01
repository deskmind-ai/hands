"""The settings app: tasks (seeded), the true-state grader and the oracle.

A preferences window with a sidebar of sections, each a few switches (and one radio group). A task turns one switch
on or off, or picks the appearance. What it exercises:

  navigation      the setting is in a section that is not shown at first
  near names      "自动下载更新" next to "自动安装更新", "通知声音" in one section and "提示音" in another
  already there   about a third of the tasks ask for the state the setting is already in: the answer is DONE, and a
                  click would undo it
  recovery        a switch flipped by mistake is flipped back (the oracle says so) before the task counts as done
"""
from __future__ import annotations

import random

from tools.gym import split

#: Bumped when the labels this oracle gives change (see tools/gym/run.py rows).
ORACLE_VERSION = 3

SECTIONS = {
    "zh": {"通用": ["自动下载更新", "自动安装更新", "开机自启动"],
           "通知": ["允许通知", "通知声音", "锁屏显示通知"],
           "声音": ["按键音", "提示音", "静音模式"],
           "隐私": ["位置服务", "使用分析", "个性化推荐"],
           "显示": ["夜间模式", "自动调节亮度"]},
    "en": {"General": ["Download updates automatically", "Install updates automatically", "Open at login"],
           "Notifications": ["Allow notifications", "Notification sounds", "Show on lock screen"],
           "Sound": ["Keyboard clicks", "Alert sounds", "Silent mode"],
           "Privacy": ["Location services", "Share analytics", "Personalized suggestions"],
           "Display": ["Night mode", "Auto-brightness"]},
}
APPEARANCE = {"zh": ("外观", "显示", ["浅色", "深色", "自动"]), "en": ("Appearance", "Display", ["Light", "Dark", "Auto"])}
APPS = [("Controlly", "#345"), ("Setly", "#2a6"), ("Tweakory", "#836"), ("设置中心", "#a52"), ("偏好", "#383")]
GOALS = {
    "on": {"zh": ["在{app}里打开{setting}", "把{app}的{setting}开启", "在{app}的{section}设置里打开{setting}"],
           "en": ["Turn on {setting} in {app}", "In {app}, enable {setting}", "In {app}'s {section} settings, turn on {setting}"]},
    "off": {"zh": ["在{app}里关闭{setting}", "把{app}的{setting}关掉", "在{app}的{section}设置里关闭{setting}"],
            "en": ["Turn off {setting} in {app}", "In {app}, disable {setting}", "In {app}'s {section} settings, turn off {setting}"]},
    "appearance": {"zh": ["把{app}的外观设为{mode}", "在{app}里把外观改成{mode}"],
                   "en": ["Set {app}'s appearance to {mode}", "In {app}, switch the appearance to {mode}"]},
}
#: "Make sure it is on": the phrasing of a goal that is often already met.
ENSURE = {
    "on": {"zh": ["确保{app}里的{setting}是打开的", "看一下{app}的{setting}，没开就打开"],
           "en": ["Make sure {setting} is on in {app}", "In {app}, check {setting} and turn it on if it is off"]},
    "off": {"zh": ["确保{app}里的{setting}是关闭的", "看一下{app}的{setting}，开着就关掉"],
            "en": ["Make sure {setting} is off in {app}", "In {app}, check {setting} and turn it off if it is on"]},
}
#: Held out for evaluation only: never in training rows.
HELDOUT_APPS = {"偏好"}


def _items(lang: str) -> list[str]:
    return [x for names in SECTIONS[lang].values() for x in names]


def _template_ids(lang: str) -> list[str]:
    return [f"settings/{lang}/{k}/{i}" for k in GOALS for i in range(len(GOALS[k][lang]))]


def _pools(heldout: bool) -> dict:
    """The settings a goal may be about and its templates: all, or (training under split 2) without the held-out
    ones -- a fifth of the settings, one template per language. The page still shows every setting: they are the
    app, not the task."""
    if heldout:
        return {"targets": {lang: SECTIONS[lang] for lang in SECTIONS},
                "templates": {(k, lang): list(range(len(GOALS[k][lang]))) for k in GOALS for lang in SECTIONS}}
    held_t = {lang: split.heldout_template("settings", lang, _template_ids(lang)) for lang in SECTIONS}
    targets = {}
    for lang, sections in SECTIONS.items():
        keep = set(split.train_values("settings", f"{lang}_items", _items(lang)))
        targets[lang] = {sec: [x for x in names if x in keep] for sec, names in sections.items()
                         if any(x in keep for x in names)}
    return {"targets": targets,
            "templates": {(k, lang): [i for i in range(len(GOALS[k][lang])) if f"settings/{lang}/{k}/{i}" != held_t[lang]]
                          for k in GOALS for lang in SECTIONS}}


def make_task(seed: int, split_version: int = split.DEFAULT_VERSION) -> dict:
    """The task of `seed`. Split 1 (the default, every row so far): held out by skin only. Split 2: concept-disjoint
    (see tools/gym/split.py)."""
    base = _make(seed, random.Random(seed), _pools(True))
    if split_version == 1:
        return base
    return split.pick(seed, "settings", base["split"], lambda rnd, heldout: _make(seed, rnd, _pools(heldout)),
                      lambda t: t["app"] in HELDOUT_APPS)


def _make(seed: int, rnd: random.Random, pools: dict) -> dict:
    lang = rnd.choice(["zh", "en"])
    app, color = rnd.choice(APPS)
    sections = SECTIONS[lang]
    targets = pools["targets"][lang]
    ap_name, ap_section, modes = APPEARANCE[lang]
    values = {s: rnd.random() < 0.5 for names in sections.values() for s in names}
    values[ap_name] = rnd.choice(modes)
    already = rnd.random() < 0.35
    start = rnd.choice([s for s in sections])
    # From seed 30000: more tasks whose setting is already as asked (a planner flipped it anyway: 15 of 122 on the
    # held-out skin), and goals that say "make sure". Drawn apart, so earlier seeds are what they were.
    v3 = random.Random(f"v3-{seed}") if seed >= 30000 else None
    if v3 is not None:
        already = v3.random() < 0.6
    concepts = []
    if rnd.random() < 0.2:
        kind = "appearance"
        mode = rnd.choice(modes)
        if already:
            values[ap_name] = mode
        else:
            values[ap_name] = rnd.choice([m for m in modes if m != mode])
        target = {"setting": ap_name, "section": ap_section, "want": mode}
        # An index into the templates, drawn as a choice of the template itself was (the same draw), so it is known.
        ti = rnd.choice(pools["templates"][(kind, lang)])
        goal = GOALS[kind][lang][ti].format(app=app, mode=mode)
        template_id = f"settings/{lang}/{kind}/{ti}"
    else:
        section = rnd.choice(list(targets))
        setting = rnd.choice(targets[section])
        want = rnd.random() < 0.5
        kind = "on" if want else "off"
        values[setting] = want if already else not want
        target = {"setting": setting, "section": section, "want": want}
        ti = rnd.choice(pools["templates"][(kind, lang)])
        goal = GOALS[kind][lang][ti].format(app=app, setting=setting, section=section)
        template_id = f"settings/{lang}/{kind}/{ti}"
        if v3 is not None and v3.random() < 0.4:
            ei = v3.choice(range(len(ENSURE[kind][lang])))
            goal = ENSURE[kind][lang][ei].format(app=app, setting=setting, section=section)
            template_id = f"settings/{lang}/ensure-{kind}/{ei}"   # never held out
        concepts = [split.concept("setting", "settings", f"{lang}_items", setting, _items(lang))]
    return {"seed": seed, "lang": lang, "app": app, "color": color, "goal": goal, "kind": kind, "target": target,
            "already": already, "split": "heldout" if app in HELDOUT_APPS else "train",
            "split_version": 1, "template_id": template_id,
            "template_heldout": template_id == split.heldout_template("settings", lang, _template_ids(lang)),
            "concepts": concepts,
            "page": {"app": app, "color": color, "sections": sections, "appearance": [ap_name, ap_section, modes],
                     "values": values, "start": start}}


def _wrong(task: dict, state: dict) -> list[str]:
    """Settings other than the target that no longer hold their starting value."""
    start, now = task["page"]["values"], state.get("values") or {}
    return [k for k, v in start.items() if k != task["target"]["setting"] and now.get(k, v) != v]


def passed(task: dict, state: dict, answer: str | None = None) -> bool:
    t = task["target"]
    return (state.get("values") or {}).get(t["setting"]) == t["want"] and not _wrong(task, state)


def oracle(task: dict, app_state: dict, request: dict) -> tuple[dict | None, str]:
    """The right answer to this request, keyed by the request's own option keys, and why; None if not offered."""
    q = request["questions"]
    els = request["state"]["elements"]
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

    def control(label: str, roles: tuple[str, ...]):
        e = next((e for e in els if e.get("label") == label and (e.get("role") or "").lower() in roles
                  and "CLICK" in (e.get("operations") or [])), None)
        return e and e["index"]

    def section_of(setting: str) -> str:
        if setting == task["page"]["appearance"][0]:
            return task["page"]["appearance"][1]
        return next(s for s, names in task["page"]["sections"].items() if setting in names)

    shown = app_state.get("section")
    switch_roles = ("checkbox", "switch", "togglebutton")
    # A switch flipped by mistake is flipped back first: the task is not done while it is off its starting value.
    for k in _wrong(task, app_state):
        if k == task["page"]["appearance"][0]:
            if shown != section_of(k):
                return pick("CLICK", "the appearance was changed by mistake: go back to its section",
                            click_target=control(section_of(k), ("button", "tab", "radiobutton")))
            return pick("CLICK", "the appearance was changed by mistake: choose the one it was",
                        click_target=control(task["page"]["values"][k], ("radiobutton", "radio")))
        if shown != section_of(k):
            return pick("CLICK", f"a switch was flipped by mistake ({k}): go back to its section",
                        click_target=control(section_of(k), ("button", "tab", "radiobutton")))
        return pick("CLICK", f"flip back the switch changed by mistake ({k})", click_target=control(k, switch_roles))
    if passed(task, app_state):
        # Done only when the screen shows it: a setting in a section that is not open has to be looked at first,
        # or DONE is a guess (oracle v3; v2 said DONE here, and taught finishing on no evidence).
        if shown != t["section"]:
            return pick("CLICK", "the setting may already be right, but it is not on screen: open its section to "
                                 "check", click_target=control(t["section"], ("button", "tab", "radiobutton")))
        return pick("DONE", "already in the requested state: nothing to do" if task["already"] and not
                    app_state.get("changed") else "the setting is as the goal asks")
    if shown != t["section"]:
        return pick("CLICK", "open the section the setting is in",
                    click_target=control(t["section"], ("button", "tab", "radiobutton")))
    if task["kind"] == "appearance":
        return pick("CLICK", "choose the requested appearance", click_target=control(t["want"], ("radiobutton", "radio")))
    return pick("CLICK", "flip the setting the goal names", click_target=control(t["setting"], switch_roles))


def trap(task: dict, app_state: dict, request: dict) -> tuple[dict, str] | None:
    """A plausible wrong step once the target is on screen: the switch next to it flipped (or the wrong appearance
    chosen) -- a change was made, so it can look done."""
    from tools.gym.music import _choose
    t = task["target"]
    if app_state.get("section") != t["section"] or app_state.get("changed"):
        return None
    els, q = request["state"]["elements"], request["questions"]
    if task["kind"] == "appearance":
        wrong = [m for m in task["page"]["appearance"][2] if m != t["want"]]
        e = next((e for e in els if e.get("label") in wrong and (e.get("role") or "").lower() in ("radiobutton", "radio")), None)
    else:
        names = task["page"]["sections"][t["section"]]
        i = names.index(t["setting"])
        near = [n for n in (names[i + 1:i + 2] + names[max(0, i - 1):i]) if n != t["setting"]]
        e = next((e for n in near for e in els if e.get("label") == n
                  and (e.get("role") or "").lower() in ("checkbox", "switch", "togglebutton")), None)
    if e is None:
        return None
    label = _choose(q, "CLICK", click_target=e["index"])
    return (label, f"trap: clicked {e.get('label')!r} instead of the target") if label else None

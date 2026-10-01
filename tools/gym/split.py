"""The gym's train / held-out split, version 2: concept-disjoint (as cua-s1 splits by concept signature).

Version 1 (every seed so far, and the default) held out a skin only: held-out tasks were a new app name over the same
goal templates, song titles, names and subjects as training, and up to a third of held-out goals were word for word
a training goal once the app name was set aside. Version 2 also holds out, per app family and language, one goal
template and about a fifth of each value pool (titles, person names, email subjects, settings items):

  train     never a held-out skin, template or value -- decoys and fillers included
  held-out  a held-out skin, and at least one held-out template or value in what the goal asks for

What is held out is drawn from its own generators (by family, pool and language, never by seed), and a version 2
task from generators of its own (`draws(seed)`), so version 1 seeds regenerate exactly as they were.

Every task, either version, says which template its goal came from (`template_id`, "<family>/<lang>/<index>" or
"<family>/<lang>/<kind>/<index>") and which values it asks for (`concepts`: slot, pool, value, heldout), so older
rows can be checked against the version 2 split too.
"""
from __future__ import annotations

import random

#: The split a task is made under when nothing says otherwise: the one every row so far was made with.
DEFAULT_VERSION = 1
HELDOUT_FRACTION = 0.2
#: How many version 2 draws a task may take to meet its split's rule before it gives up (it never needs many).
MAX_DRAWS = 400


def heldout_values(family: str, pool: str, values: list) -> set:
    """The values of `pool` held out for evaluation: about a fifth, at least one, the same every time."""
    n = max(1, round(len(values) * HELDOUT_FRACTION))
    return set(random.Random(f"split2-values-{family}-{pool}").sample(list(values), n))


def heldout_template(family: str, lang: str, ids: list[str]) -> str:
    """The one goal template of `family` in `lang` held out for evaluation."""
    return random.Random(f"split2-template-{family}-{lang}").choice(sorted(ids))


def train_values(family: str, pool: str, values: list) -> list:
    held = heldout_values(family, pool, values)
    return [v for v in values if v not in held]


def concept(slot: str, family: str, pool: str, value, values: list) -> dict:
    return {"slot": slot, "pool": f"{family}/{pool}", "value": value,
            "heldout": value in heldout_values(family, pool, values)}


def uses_heldout(task: dict) -> bool:
    return task.get("template_heldout", False) or any(c["heldout"] for c in task.get("concepts", []))


def draws(seed: int, family: str):
    """The generators a version 2 task of `seed` is drawn from, one per attempt."""
    for k in range(MAX_DRAWS):
        yield random.Random(f"split2-{family}-{seed}-{k}")


def pick(seed: int, family: str, base_split: str, make, check_skin) -> dict:
    """The first version 2 draw that meets the rule for `base_split` (the skin's split, as version 1 decides it).

    make(rnd, heldout) builds a task from `rnd` with the training pools (heldout False) or the full ones (True);
    check_skin(task) says whether its skin is a held-out one."""
    heldout = base_split == "heldout"
    for rnd in draws(seed, family):
        t = make(rnd, heldout)
        if check_skin(t) != heldout:
            continue
        if heldout and not uses_heldout(t):
            continue
        t["split"] = base_split
        t["split_version"] = 2
        return t
    raise RuntimeError(f"{family} seed {seed}: no version 2 draw met the {base_split} rule")

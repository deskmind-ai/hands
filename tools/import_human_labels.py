"""Bring an outsourced label pack back: validate answers, score each annotator on the gold items, merge.

    python tools/import_human_labels.py data/dagger_all.jsonl export/label_pack_20260922.key.json \\
        answers_*.json --out data/dagger_all.human.jsonl [--min-gold 0.8]

An answer is kept only if every key it names is one of its question's options and the operation's required
follow-up questions are all answered. An annotator whose accuracy on the gold items (operation plus every head
the gold label uses) is below --min-gold contributes nothing. Where several annotators answered the same item,
the label is taken only if they agree; disagreement leaves the row unlabelled and is reported. Merged rows get
label_source="human" and label_annotators.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from export_label_pack import HEADS               # noqa: E402

#: Chords the driver cannot carry out in the background: a label naming one would teach a step that is refused.
NO_ROUTE = {"cmd+z", "cmd+shift+z"}


def valid(ans: dict, questions: dict) -> dict | None:
    if not ans or ans.get("skip") or not ans.get("operation"):
        return None
    op = ans["operation"]
    need = ["operation"] + [h for h in HEADS.get(op, []) if h in questions]
    out = {}
    for h in need:
        k = ans.get(h)
        if k is None or str(k) not in (questions.get(h) or {}).get("criteria", {}):
            return None
        out[h] = str(k)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("key")
    ap.add_argument("answers", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-gold", type=float, default=0.8)
    args = ap.parse_args()
    key = json.load(open(args.key))
    rows = [json.loads(l) for l in open(args.data)]
    by_pos = {(r["run_id"], r["step"]): r for r in rows}

    votes: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for path in args.answers:
        a = json.load(open(path))
        who = a.get("annotator") or Path(path).stem
        right = total = 0
        mine = {}
        for qid, ans in (a.get("answers") or {}).items():
            k = key.get(qid)
            if not k:
                continue
            row = by_pos.get((k["run_id"], k["step"]))
            v = valid(ans, row["questions"]) if row else None
            if k["gold"]:
                total += 1
                g = k["gold_label"]
                right += bool(v) and all(v.get(h) == g[h] for h in g)
            elif v:
                mine[qid] = v
        acc = right / total if total else 0.0
        verdict = "accepted" if acc >= args.min_gold else "REJECTED"
        print(f"{who}: gold {right}/{total} = {acc:.0%} -> {verdict}; {len(mine)} valid answers")
        if acc >= args.min_gold:
            for qid, v in mine.items():
                votes[qid].append((who, v))

    stats = Counter()
    for qid, vs in votes.items():
        k = key[qid]
        row = by_pos[(k["run_id"], k["step"])]
        if row.get("label_source") == "oracle":
            stats["oracle already labels it (kept oracle)"] += 1      # the program, where it can, decides
            continue
        v0 = vs[0][1]
        if v0.get("operation") in ("DONE", "BLOCKED"):
            # These rows are exactly the ones with a stray the oracle knows of, or that it could not place: no
            # terminal answer is learned on them.
            stats["terminal answer not taken"] += 1
            continue
        if v0.get("operation") == "KEY" and v0.get("key_target") in NO_ROUTE:
            stats[f"{v0['key_target']}: no background route yet (not taken)"] += 1
            continue
        if len({json.dumps(v, sort_keys=True) for _, v in vs}) != 1:
            stats["disagreement (left unlabelled)"] += 1
            continue
        v = vs[0][1]
        row["label"] = {h: {"type": "choice", "choice": c, "confidence": 1.0,
                            "probabilities": {o: (1.0 if o == c else 0.0) for o in row["questions"][h]["criteria"]}}
                        for h, c in v.items()}
        row["label_source"] = "human"
        row["label_annotators"] = [w for w, _ in vs]
        stats["human"] += 1
    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(dict(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

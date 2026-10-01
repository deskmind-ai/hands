"""Package the states nothing automatic could label into an outsourcing pack for human annotators.

In: a labelled DAgger file (tools/oracle_label.py, optionally followed by a second labelling pass). The items are
the rows left without a trustworthy label (rejected, pending), plus a hidden sample of rows the
oracle labelled, as gold questions to measure each annotator. Items are shuffled and renamed Q001...; which
item is which row, and which are gold, stays in a key file that is NOT part of the pack.

Out, in export/<name>/ and export/<name>.zip:
  README.md       the guideline (Chinese)
  annotate.html   a self-contained offline tool: open it, answer, press "导出答案" -> answers_<name>.json
  items.json      the same items, for anyone who prefers their own tool
and export/<name>.key.json (keep it; tools/import_human_labels.py needs it).

Every string is scanned for the login name before anything is written; the pack is refused if it appears.

    python tools/export_label_pack.py data/dagger_all.jsonl --gold 20 --name label_pack_20260922
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from deskmind_bench.task import load_set                    # noqa: E402

OPS_ZH = {
    "CLICK": "点击", "OPEN": "打开（双击）", "SELECT": "在下拉框里选择", "RENAME": "改名",
    "REPLACE_TEXT": "替换文档里的一段文字", "APPEND_TEXT": "在末尾追加文字", "TYPE_TEXT": "输入 / 覆盖文字",
    "KEY": "按快捷键", "FOCUS_APP": "切换应用", "FOCUS_WINDOW": "切换窗口", "TYPE_FOCUSED": "往当前焦点处打字",
    "SCROLL": "滚动", "ASK": "向用户提问", "DONE": "任务已完成", "BLOCKED": "无法继续",
}
#: Which follow-up questions each operation needs answered.
HEADS = {
    "CLICK": ["click_target"], "OPEN": ["open_target"], "SELECT": ["select_target"],
    "RENAME": ["rename_target", "type_text_value"], "TYPE_TEXT": ["type_text_target", "type_text_value"],
    "APPEND_TEXT": ["append_text_target", "type_text_value"],
    "REPLACE_TEXT": ["replace_text_target", "replace_from", "type_text_value"],
    "KEY": ["key_target"], "FOCUS_WINDOW": ["focus_window_target"], "FOCUS_APP": ["focus_app_target"],
    "SCROLL": ["scroll_target"], "TYPE_FOCUSED": ["type_text_value"],
}
HEAD_ZH = {
    "click_target": "点哪个元素", "open_target": "打开哪个", "select_target": "选哪一项",
    "rename_target": "给哪个文件改名", "type_text_target": "往哪里输入", "append_text_target": "往哪里追加",
    "replace_text_target": "在哪个文档里替换", "replace_from": "把哪段原文换掉", "type_text_value": "写入的内容 / 新名字",
    "key_target": "按哪个快捷键", "focus_window_target": "切到哪个窗口", "focus_app_target": "切到哪个应用",
    "scroll_target": "滚动哪里",
}


def effect_zh(eff: str) -> str:
    eff = eff.strip()
    if eff.startswith("mkdir"):
        arg = [a for a in eff.split()[1:] if not a.startswith("-")][0].strip("'")
        return f"新建文件夹「{arg}」"
    if eff.startswith("mv"):
        parts = re.findall(r"'([^']*)'|(\S+)", eff)[1:]
        src, dst = [a or b for a, b in parts][:2]
        if dst.endswith("/"):
            d = dst.rstrip("/")
            return f"把「{src}」移动到「{'工作目录顶层' if d in ('.', '') else d}」"
        return f"把「{src}」改名为「{dst.split('/')[-1]}」"
    m = re.match(r"cat > '(.+?)' <<'HANDS_EOF'\n(.*)\nHANDS_EOF", eff, re.S)
    if m:
        return f"「{m.group(1)}」的最终内容应为：\n{m.group(2)}"
    return eff


def option_text(v) -> str:
    if isinstance(v, str):
        return v
    return re.sub(r"^\[[^\]]+\]\s*", "", v.get("element") or v.get("value") or v.get("text") or json.dumps(v))


def item_view(row: dict, task) -> dict:
    st = row["state"]
    page = st.get("page") or {}
    qs = {}
    for qid, q in row["questions"].items():
        crit = q.get("criteria") or {}
        if qid == "operation":
            opts = [{"key": k, "text": f"{OPS_ZH.get(k, k)}（{k}）"} for k in crit]
        else:
            opts = [{"key": k, "text": option_text(v)} for k, v in crit.items()]
        qs[qid] = {"title": "做哪种操作" if qid == "operation" else HEAD_ZH.get(qid, qid), "options": opts}
    return {
        "goal": row["goal"],
        "target_outcome": [effect_zh(e) for e in (task.oracle_effect or [])],
        "app": page.get("url"), "window": page.get("title"),
        "page_text": (page.get("text") or "")[:2000],
        "elements": [{"label": e.get("label"), "role": e.get("role"), "value": e.get("current_value"),
                      "ops": e.get("operations")} for e in (st.get("elements") or [])[:60]],
        "recent_actions": st.get("recent_actions") or [],
        "done_so_far": st.get("effects_so_far") or [],
        "questions": qs,
        "heads": HEADS,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("--gold", type=int, default=20)
    ap.add_argument("--name", required=True)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    tasks = {t.id: t for t in load_set(REPO / "tasks", "train")}
    rows = [json.loads(l) for l in open(args.inp)]
    todo = [r for r in rows if r.get("label_source") in ("rejected", "pending")]
    # Gold: oracle-labelled rows, spread over families, preferring states where the student got it wrong.
    by_fam = defaultdict(list)
    for r in rows:
        if r.get("label_source") == "oracle" and r["label"]["operation"]["choice"] != r["answers"]["operation"]["choice"]:
            by_fam[r["task_id"].split("-")[1]].append(r)
    gold = []
    fams = sorted(by_fam)
    while len(gold) < args.gold and any(by_fam.values()):
        for f in fams:
            if by_fam[f] and len(gold) < args.gold:
                gold.append(by_fam[f].pop(rng.randrange(len(by_fam[f]))))
    picked = [(r, False) for r in todo] + [(r, True) for r in gold]
    rng.shuffle(picked)

    items, key = [], {}
    for n, (r, is_gold) in enumerate(picked, 1):
        qid = f"Q{n:03d}"
        v = item_view(r, tasks[r["task_id"]])
        v["id"] = qid
        items.append(v)
        key[qid] = {"run_id": r["run_id"], "step": r["step"], "task_id": r["task_id"], "gold": is_gold,
                    "gold_label": ({h: x["choice"] for h, x in r["label"].items()} if is_gold else None)}

    blob = json.dumps(items, ensure_ascii=False)
    if Path.home().name in blob:
        raise SystemExit("the login name appears in the pack; refusing to write it")

    out = REPO / "export" / args.name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    html = (REPO / "tools" / "label_pack_template.html").read_text(encoding="utf-8")
    html = html.replace("/*__ITEMS__*/[]", blob.replace("</", "<\\/")).replace("__PACK__", args.name)
    (out / "annotate.html").write_text(html, encoding="utf-8")
    guide = (REPO / "tools" / "label_pack_guide.md").read_text(encoding="utf-8")
    (out / "README.md").write_text(guide.replace("__N__", str(len(items))).replace("__PACK__", args.name),
                                  encoding="utf-8")
    (REPO / "export" / f"{args.name}.key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1),
                                                         encoding="utf-8")
    shutil.make_archive(str(REPO / "export" / args.name), "zip", root_dir=out.parent, base_dir=out.name)
    print(f"{len(items)} items ({len(todo)} to label + {len(gold)} gold) -> export/{args.name}.zip")
    print(f"key (keep private): export/{args.name}.key.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

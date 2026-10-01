"""Generate training-task variants for the real desktop, one family per diag task type.

The thirteen diag tasks are the test set and never appear here. What does appear is the same *kinds* of work with
everything a model could memorise changed -- names, folders, depths, fields, values, languages -- so that states
collected on these tasks teach the decision and not the task:

    newfolder     create a folder with a given name                          (G07)
    move_up       move a named file from a subfolder to the top              (G08)
    move_up_two   move a named file two levels up                            (G10)
    rename_deep   go into a/b and rename the one file of a type              (G09)
    sort_into     create a folder and move every file of a type into it     (G01)
    suffix        rename several files by adding a suffix                    (G12)
    edit_fields   change one field's value and one number from A to B, save  (G02)
    write_exact   write dictated lines character for character, save         (G04)
    roundtrip     copy two labelled values from one document into another    (G13)
    find_line     find the one line with a status in a long log, write its id(G11)
    wrong_target  two near-identical documents, edit only the named one      (G06)

Every task carries an effect oracle (the shell commands that produce the correct end state), so `hands
verify-tasks --set train` proves each one is solvable and that doing nothing fails it before a single state is
collected from it.

    python tools/gen_tasks.py --per-family 20 --seed 1
"""

from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "tasks" / "train"
HOLDOUT = REPO / "tasks" / "holdout"
FIXTURES = REPO / "fixtures" / "train"

FOLDERS = ["archive", "docs", "reports", "backup", "drafts", "final", "invoices", "photos", "old", "misc",
           "项目", "资料", "归档", "报告", "草稿", "合同", "备份", "图片"]
STEMS = ["draft", "notes", "summary", "plan", "budget", "memo", "log", "report", "agenda", "todo",
         "会议纪要", "周报", "预算", "计划", "清单", "说明", "记录"]
EXTS = ["txt", "md", "log", "csv"]
LABELS = [("Status", ["draft", "review", "final", "approved", "pending"]),
          ("Owner", ["Alice", "Bob", "Carol", "Dave", "Erin"]),
          ("Priority", ["low", "medium", "high"]),
          ("状态", ["草稿", "审核中", "已完成", "已批准"]),
          ("负责人", ["张伟", "李娜", "王芳", "刘洋"])]
NUMBERED = ["Budget", "Headcount", "Quota", "预算", "人数", "额度"]
SENTENCES = ["一季度营收 {a} 万元，环比增长 {b}%。", "未结订单 {c} 笔，其中 {d} 笔已逾期。",
             "Revenue reached {a}k this quarter, up {b}%.", "{c} tickets remain open; {d} are overdue.",
             "本月新增客户 {c} 家，流失 {d} 家。", "库存周转天数为 {a} 天，同比下降 {b}%。"]


#: Goal phrasings per family. None is a diag goal's wording -- the diag set is the test set, and training on its
#: sentences would teach the sentence. The last phrasing of every family is held out of training and used only in
#: the generalisation split, together with the compositional families, which training never sees at all.
PHRASES = {
    "newfolder": ["在工作目录里建一个新文件夹，名字叫 {name}，别的不要动。",
                  "Create a folder named {name} in the working directory and leave everything else alone.",
                  "需要一个叫「{name}」的文件夹，放在当前工作目录下。",
                  "Make a new directory called {name} here.",
                  "请新增文件夹 {name}（放在工作目录顶层）。"],
    "move": ["把 {src} 里面的 {file} 挪到 {dst_desc}，其余文件别动。",
             "Move {file} out of {src} and into {dst_desc}. Don't touch the other files.",
             "{file} 现在在 {src} 里，请把它放到 {dst_desc}。",
             "Please relocate {src}/{file} to {dst_desc}.",
             "请将 {file} 从 {src} 转移至 {dst_desc}。"],
    # The destination named by a file that is already there and must stay: the planner moved the named file itself
    # (G10, and a trained 0.8B planner in 3 runs of 3). No diag wording: G10 says "和 stray.log 同一层".
    "move_by_ref": ["把 {src} 里的 {file} 放到 {ref} 所在的那个文件夹里，{ref} 本身不要动。",
                    "Put {src}/{file} in the same folder as {ref}. {ref} itself stays where it is.",
                    "{file}（在 {src} 里）应该和 {ref} 放在一起，请只移动 {file}。",
                    "Move {file} from {src} so that it sits next to {ref}; do not move {ref}.",
                    "请把 {src}/{file} 移到与 {ref} 相同的位置，{ref} 保持原位。"],
    "rename_deep": ["打开 {path} 这个文件夹，把其中唯一的 .{ext} 文件改名为 {new}。",
                    "Go into {path} and rename the .{ext} file there to {new}. Leave the rest.",
                    "{path} 下有一个 .{ext} 文件，请把它的名字改成 {new}。",
                    "In the folder {path}, the only .{ext} file should be called {new} -- please rename it.",
                    "请将 {path} 目录中的 .{ext} 文件重命名成 {new}。"],
    "sort_into": ["建一个叫 {name} 的文件夹，然后把所有 .{ext} 文件放进去，别的文件留在原地。",
                  "Create {name} and move every .{ext} file into it; other files stay where they are.",
                  "把工作目录里的 .{ext} 文件归到一个新文件夹 {name} 里。",
                  "Gather all the .{ext} files into a new folder called {name}.",
                  "请整理一下：新建 {name}，把全部 .{ext} 文件移入其中。"],
    "suffix": ["给 {first} 到 {last} 这几个文件的名字末尾都加上 {suf}，比如 {example}。",
               "Append {suf} to the names of {first} through {last} (e.g. {example}).",
               "{first}……{last} 这些文件，请在文件名后面补上 {suf}（如 {example}）。",
               "Rename each of {first} … {last} by adding {suf} before the extension, like {example}.",
               "请批量改名：{first} 到 {last} 全部加后缀 {suf}，示例 {example}。"],
    "edit_fields": ["在 TextEdit 里打开的 {doc} 中，把 {label} 改为 {after}，{num} 由 {a} 改为 {b}，其他不变，然后保存。",
                    "In {doc} (already open in TextEdit), set {label} to {after} and change {num} from {a} to {b}. "
                    "Keep the other lines. Save when done.",
                    "{doc} 需要更新两处：{label} → {after}，{num} 从 {a} 调到 {b}。改好存盘。",
                    "Update {doc}: {label} becomes {after}; {num} goes from {a} to {b}. Save it.",
                    "请修改 {doc}：{label} 一行改成 {after}，{num} 的 {a} 换成 {b}，保存文件。"],
    "write_exact": ["{doc} 是空的，已经在 TextEdit 里打开。请原样输入下面的内容（标点、数字、换行都不能差）并保存：\n{body}",
                    "{doc} is open and empty. Type exactly the following, punctuation and line breaks included, "
                    "then save:\n{body}",
                    "在 {doc} 里一字不差地写下：\n{body}\n写完保存。",
                    "Enter this text verbatim into {doc} and save it:\n{body}",
                    "请把下面这段抄进 {doc}，保持完全一致，然后保存：\n{body}"],
    "roundtrip": ["从 {src} 里读出{k1}和{k2}，按「{k1},{k2}」的格式写成一行放进 {dst}，存盘。只要值，不要标签。",
                  "Read the {k1} and {k2} from {src} and write them into {dst} as one line in the format "
                  "`{k1},{k2}`. Values only. Save.",
                  "{dst} 里需要一行 `{k1},{k2}`，值取自 {src}，写完保存（不带标签文字）。",
                  "Copy the two values ({k1}, {k2}) from {src} into {dst} as `{k1},{k2}` and save.",
                  "请根据 {src} 填写 {dst}：一行，格式 `{k1},{k2}`，只写数值，保存。"],
    "find_line": ["log.txt 共 {n} 行，只有一行标着「{word}」。把那行的编号（格式像 {prefix}-1234）填进 answer.txt 并保存，只写编号。",
                  "log.txt has {n} lines and exactly one is marked 「{word}」. Put that line's id "
                  "(like {prefix}-1234) into answer.txt and save. Just the id.",
                  "在 log.txt 里找出状态为「{word}」的那一行，把它的项目编号写到 answer.txt，存盘。",
                  "Which line of log.txt is 「{word}」? Write its project id into answer.txt and save it.",
                  "请查 log.txt：状态「{word}」的只有一行，将其编号记录到 answer.txt 后保存。"],
    "wrong_target": ["两个很像的报告都开着。只改 {q2} 那份：把 {label} 设为 {after}；{q1} 那份保持原样。保存。",
                     "Both reports are open and their names differ by one character. In the {q2} one only, set "
                     "{label} to {after}. Leave {q1} untouched. Save.",
                     "{q2} 报告里的 {label} 需要改成 {after}，注意别动 {q1} 的那份，改完保存。",
                     "Change {label} to {after} in the {q2} report -- not the {q1} one -- and save.",
                     "请只修改 {q2} 季度报告的 {label} 为 {after}，{q1} 那份不能改，保存。"],
}


def _phrase(rng, family, split, **kw):
    pool = PHRASES[family]
    choices = pool[:-1] if split == "train" else pool[-1:]
    return rng.choice(choices).format(**kw)


def _pick(rng, pool, k=1, exclude=()):
    choices = [x for x in pool if x not in exclude]
    return rng.sample(choices, k) if k > 1 else rng.choice(choices)


def _files(rng, n, exts=None, exclude=()):
    out = set()
    while len(out) < n:
        out.add(f"{_pick(rng, STEMS)}-{rng.randint(1, 99)}.{_pick(rng, exts or EXTS)}")
    return [f for f in out if f not in exclude]


def _finder(tid, goal, budget, checkpoints, guards, oracle, forbid=None):
    return {"id": tid, "title": goal.splitlines()[0][:40], "surface": "finder", "tags": ["train", "finder"],
            "app": "com.apple.finder", "goal": goal, "fixture": f"train/{tid}",
            "budget": {"max_actions": budget, "wall_clock_s": 600}, "sentinels": ["$WS/keep/reference.txt"],
            "grade": {"checkpoints": checkpoints, "guards": guards or [], **({"forbid": forbid} if forbid else {})},
            "oracle_effect": oracle}


def _editor(tid, goal, budget, stage_files, checkpoints, oracle, sentinels=()):
    return {"id": tid, "title": goal.splitlines()[0][:40], "surface": "editor", "tags": ["train", "textedit"],
            "app": "com.apple.TextEdit", "goal": goal, "fixture": f"train/{tid}",
            "reset_apps": ["com.apple.TextEdit"],
            "stage": [f'open -a TextEdit "$WS/{f}"' for f in stage_files],
            "budget": {"max_actions": budget, "wall_clock_s": 900},
            "sentinels": ["$WS/keep/reference.txt", *sentinels],
            "grade": {"checkpoints": checkpoints}, "oracle_effect": oracle}


def _sh(path: str, text: str) -> str:
    """A shell command that writes text to path exactly (heredoc: no escaping surprises with : or %)."""
    return f"cat > '{path}' <<'HANDS_EOF'\n{text}HANDS_EOF"


UNTITLED = [{"file_exists": {"path": "$WS/untitled folder"}}, {"file_exists": {"path": "$WS/未命名文件夹"}}]


# -- families ---------------------------------------------------------------------------------------------

def newfolder(rng, tid, split='train'):
    name = _pick(rng, FOLDERS)
    files = _files(rng, rng.randint(2, 4))
    goal = _phrase(rng, "newfolder", split, name=name)
    task = _finder(tid, goal, 12, [{"name": "created", "critical": True,
                                    "check": {"file_exists": {"path": f"$WS/{name}"}}}],
                   [{"file_count": {"dir": "$WS", "glob": "*.*", "value": len(files)}}],
                   [f"mkdir '{name}'"], forbid=UNTITLED)
    return task, {f: f"{f}\n" for f in files}


def move(rng, tid, split='train'):
    """Move a named file; the destination varies (top level, a sibling folder, two levels up)."""
    kind = rng.choice(["top", "sibling", "up_two"])
    a, b, sib = _pick(rng, FOLDERS, 3)
    files = _files(rng, rng.randint(2, 4))
    target = rng.choice(files)
    if kind == "up_two":
        src, dst, dst_desc = f"{a}/{b}", "", rng.choice(["工作目录的顶层", "the top of the working directory", "最外层"])
    elif kind == "sibling":
        src, dst, dst_desc = a, sib, rng.choice([f"{sib} 文件夹", f"the {sib} folder", f"旁边的 {sib}"])
    else:
        src, dst, dst_desc = a, "", rng.choice(["工作目录顶层", "the working directory itself", "上一级"])
    goal = _phrase(rng, "move", split, file=target, src=src, dst_desc=dst_desc)
    dst_path = f"$WS/{dst}/{target}" if dst else f"$WS/{target}"
    rest = sorted(f for f in files if f != target)
    task = _finder(tid, goal, 25,
                   [{"name": "moved", "critical": True, "check": {"file_exists": {"path": dst_path}}},
                    {"name": "gone", "check": {"file_absent": {"path": f"$WS/{src}/{target}"}}}],
                   [{"dir_manifest": {"dir": f"$WS/{src}", "value": rest}}],
                   [f"mv '{src}/{target}' './{dst}/'" if dst else f"mv '{src}/{target}' ./"])
    fx = {f"{src}/{f}": f"{f}\n" for f in files}
    if dst:
        fx[f"{dst}/.keep"] = ""
    if kind == "up_two":
        fx[f"stray-{rng.randint(1, 99)}.txt"] = "stray\n"
    return task, fx


def move_by_ref(rng, tid, split='train'):
    """Move a file to the folder another named file is in; the named reference file must not move."""
    a, b = _pick(rng, FOLDERS, 2)
    files = _files(rng, rng.randint(2, 4))
    target = rng.choice(files)
    ref = _files(rng, 1, exclude=files)[0]
    dst = rng.choice(["", b])                   # the reference sits at the top or in a sibling folder
    goal = _phrase(rng, "move_by_ref", split, file=target, src=a, ref=ref)
    ref_path = f"$WS/{dst}/{ref}" if dst else f"$WS/{ref}"
    dst_path = f"$WS/{dst}/{target}" if dst else f"$WS/{target}"
    task = _finder(tid, goal, 25,
                   [{"name": "moved", "critical": True, "check": {"file_exists": {"path": dst_path}}},
                    {"name": "reference_stayed", "critical": True, "check": {"file_exists": {"path": ref_path}}},
                    {"name": "gone", "check": {"file_absent": {"path": f"$WS/{a}/{target}"}}}],
                   [{"dir_manifest": {"dir": f"$WS/{a}", "value": sorted(f for f in files if f != target)}}],
                   [f"mv '{a}/{target}' './{dst}/'" if dst else f"mv '{a}/{target}' ./"])
    fx = {f"{a}/{f}": f"{f}\n" for f in files}
    fx[f"{dst}/{ref}" if dst else ref] = "reference\n"
    return task, fx


def rename_deep(rng, tid, split='train'):
    a, b = _pick(rng, FOLDERS, 2)
    ext = _pick(rng, EXTS)
    old = f"{_pick(rng, STEMS)}-{rng.randint(1, 99)}.{ext}"
    new = f"{_pick(rng, STEMS)}-final.{ext}"
    other = f"{_pick(rng, STEMS)}-{rng.randint(100, 199)}.{_pick(rng, EXTS, exclude=[ext])}"
    goal = _phrase(rng, "rename_deep", split, path=f"{a}/{b}", ext=ext, new=new)
    task = _finder(tid, goal, 20,
                   [{"name": "renamed", "critical": True, "check": {"file_exists": {"path": f"$WS/{a}/{b}/{new}"}}},
                    {"name": "old_gone", "check": {"file_absent": {"path": f"$WS/{a}/{b}/{old}"}}}],
                   [{"file_exists": {"path": f"$WS/{a}/{b}/{other}"}}],
                   [f"mv '{a}/{b}/{old}' '{a}/{b}/{new}'"])
    return task, {f"{a}/{b}/{old}": "x\n", f"{a}/{b}/{other}": "y\n"}


def sort_into(rng, tid, split='train'):
    name = _pick(rng, FOLDERS)
    ext = _pick(rng, EXTS)
    hits = _files(rng, rng.randint(2, 3), exts=[ext])
    others = _files(rng, 2, exts=[e for e in EXTS if e != ext])
    goal = _phrase(rng, "sort_into", split, name=name, ext=ext)
    task = _finder(tid, goal, 30,
                   [{"name": "folder", "check": {"file_exists": {"path": f"$WS/{name}"}}},
                    {"name": "moved", "critical": True,
                     "check": {"dir_manifest": {"dir": f"$WS/{name}", "value": sorted(hits)}}}],
                   [{"file_exists": {"path": f"$WS/{o}"}} for o in others],
                   [f"mkdir '{name}'"] + [f"mv '{h}' '{name}/'" for h in hits], forbid=UNTITLED)
    return task, {f: f"{f}\n" for f in hits + others}


def suffix(rng, tid, split='train'):
    stem = _pick(rng, ["item", "part", "page", "scan", "clip"])
    suf = rng.choice(["-done", "-ok", "-v2", "-old"])
    n = rng.randint(2, 4)
    names = [f"{stem}{i}.txt" for i in range(1, n + 1)]
    goal = _phrase(rng, "suffix", split, first=names[0], last=names[-1], suf=suf, example=f"{stem}1{suf}.txt")
    task = _finder(tid, goal, 20,
                   [{"name": f"renamed_{i}", "critical": i == 1,
                     "check": {"file_exists": {"path": f"$WS/{stem}{i}{suf}.txt"}}} for i in range(1, n + 1)],
                   [{"file_count": {"dir": "$WS", "glob": "*.txt", "value": n}}],
                   [f"mv '{x}' '{x[:-4]}{suf}.txt'" for x in names])
    return task, {x: f"{x}\n" for x in names}


def edit_fields(rng, tid, split='train'):
    (label, vals), = rng.sample(LABELS, 1)
    before, after = rng.sample(vals, 2)
    num = _pick(rng, NUMBERED)
    a = rng.randint(1, 60) * 100
    b = a + rng.randint(1, 20) * 100
    doc = f"{_pick(rng, STEMS)}.txt"
    lines = [f"Quarterly {rng.choice(['review', 'plan', 'report'])}", f"{label}: {before}", f"{num}: {a}"]
    final = [lines[0], f"{label}: {after}", f"{num}: {b}"]
    goal = _phrase(rng, "edit_fields", split, doc=doc, label=label, after=after, num=num, a=a, b=b)
    text = "\n".join(final) + "\n"
    task = _editor(tid, goal, 25, [doc],
                   [{"name": "exact", "critical": True,
                     "check": {"file_text_equals": {"path": f"$WS/{doc}", "normalise": "nfc_rstrip", "value": text}}}],
                   [_sh(doc, text)])
    return task, {doc: "\n".join(lines) + "\n"}


def _sentence(rng):
    return rng.choice(SENTENCES).format(a=rng.randint(100, 9999), b=round(rng.uniform(1, 30), 1),
                                        c=rng.randint(2, 90), d=rng.randint(1, 9))


def write_exact(rng, tid, split='train'):
    doc = f"{_pick(rng, STEMS)}.txt"
    lines = [_sentence(rng) for _ in range(rng.randint(1, 2))]
    goal = _phrase(rng, "write_exact", split, doc=doc, body="\n".join(lines))
    text = "\n".join(lines) + "\n"
    task = _editor(tid, goal, 20, [doc],
                   [{"name": "exact", "critical": True,
                     "check": {"file_text_equals": {"path": f"$WS/{doc}", "normalise": "nfc_rstrip", "value": text}}}],
                   [_sh(doc, text)])
    return task, {doc: ""}


def roundtrip(rng, tid, split='train'):
    k1, k2 = rng.sample(["订单号", "发票号", "客户", "编号", "金额", "日期"], 2)
    v1 = f"{rng.choice('ABCDK')}-{rng.randint(1000, 9999)}"
    v2 = str(rng.randint(100, 99999))
    src, dst = rng.choice([("source.txt", "target.txt"), ("info.txt", "out.txt"), ("原始.txt", "结果.txt"),
                           ("notes.md", "row.csv")])
    goal = _phrase(rng, "roundtrip", split, src=src, dst=dst, k1=k1, k2=k2)
    task = _editor(tid, goal, 20, [dst, src],
                   [{"name": "written", "critical": True,
                     "check": {"file_text_equals": {"path": f"$WS/{dst}", "normalise": "nfc_rstrip",
                                                    "value": f"{v1},{v2}"}}}],
                   [_sh(dst, f"{v1},{v2}\n")], sentinels=[f"$WS/{src}"])
    return task, {src: f"{k1}: {v1}\n{k2}: {v2}\n", dst: ""}


def find_line(rng, tid, split='train'):
    n = rng.randint(20, 60)
    hit = rng.randint(1, n)
    prefix = rng.choice(["P", "T", "R", "J"])
    word = rng.choice(["关键", "紧急", "阻塞"])
    rows = [f"第 {i} 行 · 项目编号 {prefix}-{1000 + i} · 状态 {word if i == hit else rng.choice(['正常', '完成', '待定'])}"
            for i in range(1, n + 1)]
    goal = _phrase(rng, "find_line", split, n=n, word=word, prefix=prefix)
    task = _editor(tid, goal, 25, ["answer.txt", "log.txt"],
                   [{"name": "answer", "critical": True,
                     "check": {"file_text_equals": {"path": "$WS/answer.txt", "normalise": "nfc_rstrip",
                                                    "value": f"{prefix}-{1000 + hit}"}}}],
                   [_sh("answer.txt", f"{prefix}-{1000 + hit}\n")], sentinels=["$WS/log.txt"])
    return task, {"log.txt": "\n".join(rows) + "\n", "answer.txt": ""}


def wrong_target(rng, tid, split='train'):
    q1, q2 = rng.sample(["Q1", "Q2", "Q3", "Q4"], 2)
    stem = f"report-{rng.randint(2024, 2027)}"
    label, vals = rng.choice(LABELS[:3])
    before, after = rng.sample(vals, 2)
    body = lambda q, v: f"{q} revenue: {rng.randint(1000, 9999)}\n{label.lower()}: {v}\n"
    target_before = body(q2, before)
    target_after = target_before.replace(f"{label.lower()}: {before}", f"{label.lower()}: {after}")
    goal = _phrase(rng, "wrong_target", split, q1=q1, q2=q2, label=label.lower(), after=after)
    task = _editor(tid, goal, 25, [f"{stem}-{q1}.txt", f"{stem}-{q2}.txt"],
                   [{"name": "target", "critical": True,
                     "check": {"file_text_equals": {"path": f"$WS/{stem}-{q2}.txt", "normalise": "nfc_rstrip",
                                                    "value": target_after}}}],
                   [_sh(f"{stem}-{q2}.txt", target_after)], sentinels=[f"$WS/{stem}-{q1}.txt"])
    return task, {f"{stem}-{q1}.txt": body(q1, before), f"{stem}-{q2}.txt": target_before}


def create_then_move(rng, tid, split='holdout'):
    """Compositional (holdout only): create a folder, then move one named file into it."""
    name = _pick(rng, FOLDERS)
    files = _files(rng, 3)
    target = rng.choice(files)
    goal = rng.choice([f"先新建文件夹 {name}，再把 {target} 放进去。其他文件不动。",
                       f"Make a folder {name} and put {target} inside it; nothing else changes."])
    task = _finder(tid, goal, 25,
                   [{"name": "moved", "critical": True, "check": {"file_exists": {"path": f"$WS/{name}/{target}"}}}],
                   [{"file_count": {"dir": "$WS", "glob": "*.*", "value": len(files) - 1}}],
                   [f"mkdir '{name}'", f"mv '{target}' '{name}/'"], forbid=UNTITLED)
    return task, {f: f"{f}\n" for f in files}


def rename_then_move(rng, tid, split='holdout'):
    """Compositional (holdout only): rename a file inside a folder, then move it to the top."""
    sub = _pick(rng, FOLDERS)
    old = _files(rng, 1)[0]
    new = f"{_pick(rng, STEMS)}-renamed.{old.rsplit('.', 1)[1]}"
    goal = rng.choice([f"把 {sub} 里的 {old} 改名为 {new}，然后移到工作目录顶层。",
                       f"Rename {sub}/{old} to {new} and then move it up to the working directory."])
    task = _finder(tid, goal, 25,
                   [{"name": "done", "critical": True, "check": {"file_exists": {"path": f"$WS/{new}"}}},
                    {"name": "gone", "check": {"file_absent": {"path": f"$WS/{sub}/{old}"}}}], [],
                   [f"mv '{sub}/{old}' './{new}'"])
    return task, {f"{sub}/{old}": "x\n", f"{sub}/.keep": ""}


FAMILIES = {f.__name__: f for f in (newfolder, move, move_by_ref, rename_deep, sort_into, suffix,
                                     edit_fields, write_exact, roundtrip, find_line, wrong_target)}
HOLDOUT_ONLY = {f.__name__: f for f in (create_then_move, rename_then_move)}


def only_family(args, rng) -> int:
    """One family into its own set: train tasks in tasks/<set> (fixtures/<set>), and the held-out phrasing as a few
    diag-shadow tasks (private/tasks/diagshadow, fixtures in private/fixtures/diagshadow_<family>). The main train and
    holdout sets -- which DAgger rows refer to by id -- are left as they are."""
    fam = FAMILIES[args.only]
    out, fx_root = REPO / "tasks" / args.set, REPO / "fixtures" / args.set
    shadow, shadow_fx = REPO / "private" / "tasks" / "diagshadow", REPO / "private" / "fixtures" / f"diagshadow_{args.only}"
    for d in (out, fx_root, shadow_fx):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    shadow.mkdir(parents=True, exist_ok=True)
    for old in shadow.glob(f"S-{args.only}-*.yaml"):
        old.unlink()
    n = 0
    for split, count in (("train", args.per_family), ("holdout", args.holdout_per_family)):
        for i in range(count):
            tid = (f"T-{args.only}-{args.seed:02d}{i:03d}" if split == "train" else f"S-{args.only}-{args.seed:02d}{i:03d}")
            task, files = fam(rng, tid, split)
            root = (fx_root if split == "train" else shadow_fx) / tid
            task["fixture"] = f"{args.set}/{tid}" if split == "train" else f"diagshadow_{args.only}/{tid}"
            (root / "keep").mkdir(parents=True)
            (root / "keep" / "reference.txt").write_text("reference\n")
            for rel, text in files.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            task["tags"] = [t for t in task["tags"] if t != "train"] + [args.set if split == "train" else "diagshadow",
                                                                     args.only]
            (out if split == "train" else shadow).joinpath(f"{tid}.yaml").write_text(
                yaml.safe_dump(task, allow_unicode=True, sort_keys=False))
            n += 1
    print(f"{n} tasks of {args.only}: train in {out}, held-out phrasing in {shadow}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-family", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--holdout-per-family", type=int, default=3)
    ap.add_argument("--only", help="one family, written to its own set (--set) without touching train/holdout")
    ap.add_argument("--set", default="train_ref", help="with --only: tasks/<set> and fixtures/<set>; its held-out "
                    "phrasing goes to the private diagshadow set")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    if args.only:
        return only_family(args, rng)
    for d in (TASKS, HOLDOUT, FIXTURES):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    plan = [(fam, "train", i) for fam in FAMILIES for i in range(args.per_family)]
    plan += [(fam, "holdout", i) for fam in FAMILIES for i in range(args.holdout_per_family)]
    plan += [(fam, "holdout", i) for fam in HOLDOUT_ONLY for i in range(args.holdout_per_family * 2)]
    n = 0
    for fam, split, i in plan:
            tid = f"{'T' if split == 'train' else 'H'}-{fam}-{args.seed:02d}{i:03d}"
            task, files = {**FAMILIES, **HOLDOUT_ONLY}[fam](rng, tid, split)
            root = FIXTURES / tid
            (root / "keep").mkdir(parents=True)
            (root / "keep" / "reference.txt").write_text("reference\n")
            for rel, text in files.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            task["tags"] = task["tags"] + [split, fam]
            out = TASKS if split == "train" else HOLDOUT
            (out / f"{tid}.yaml").write_text(yaml.safe_dump(task, allow_unicode=True, sort_keys=False))
            n += 1
    print(f"{n} tasks: train in {TASKS}, generalisation split in {HOLDOUT}; fixtures in {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

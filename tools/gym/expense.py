"""The expense app: a receipt to read, an expense report to fill in from it (D5's kind of task).

Two apps, as a person has them: the receipt is an image open in Preview -- pixels only, so it is read by vision -- and
the expense form is a web page in the gym's host. The receipt carries what a planner reading carelessly takes instead:

  subtotal and tax (and a tip) above the TOTAL          -- the amount is the total
  a "printed" time beside the visit/transaction date     -- the date is the visit's, in the form's own format
  an operator ("operated by …") under the merchant       -- the merchant is the name at the top
  a category hint on the receipt; the form's category is a <select> that must be chosen, and the form refuses a
  submit with any required field empty or a date it cannot parse

The page posts its state (fields, errors, submitted); the grader and the oracle read it. Receipts are drawn with PIL
from a seed, in a few templates per language (split 2 holds one per language out, and a fifth of the merchants).
"""
from __future__ import annotations

import datetime as dt
import os
import random
import subprocess
import time
from pathlib import Path

from tools.gym import split

#: Bumped when the receipts are drawn differently, and written into every row (tools/gym/run.py): 1 = Hiragino Sans GB,
#: whose 餐 the OCR read as 䬸 in 20 of 48 renderings; 2 = STHeiti Light (1 of 48). Rows of 1 can carry misread
#: receipt text in their states.
RENDER_VERSION = 2

#: Bumped when the labels this oracle gives change (see tools/gym/run.py rows).
ORACLE_VERSION = 1

PREVIEW = "com.apple.Preview"
VIEWER = {"zh": "预览", "en": "Preview"}

EN_MERCHANTS = [("Blue Harbor Cafe", "128 Pier Road, Northport"), ("Maple Street Diner", "44 Maple St, Ashford"),
                ("Northwind Office Supply", "9 Canal Ave, Riverton"), ("Cedar Taxi Co.", "310 Station Rd, Bayview"),
                ("Lumen Software Store", "77 Grid Way, Easton"), ("Harbor Inn", "2 Quay Lane, Northport"),
                ("Green Leaf Bistro", "15 Elm Court, Westfield"), ("Swift Rail Tickets", "1 Central Plaza, Kingsford"),
                ("Paper & Pen Co.", "58 Mill St, Ashford"), ("Summit Hotel", "900 Ridge Blvd, Highmoor")]
ZH_MERCHANTS = [("蓝港咖啡", "北港市码头路128号"), ("枫林小馆", "安福市枫林街44号"), ("北风办公用品", "河城运河大道9号"),
                ("青松出租车", "海湾市车站路310号"), ("明光软件店", "东城格子路77号"), ("港湾客栈", "北港市码头巷2号"),
                ("绿叶餐厅", "西田市榆树苑15号"), ("迅铁票务", "京福市中央广场1号"), ("纸笔文具", "安福市磨坊街58号"),
                ("峰顶酒店", "高原市山脊大道900号")]
OPERATORS = {"en": ["Harbor Foods LLC", "Coastline Hospitality Ltd", "Ridge Holdings Inc", "Westfield Group"],
             "zh": ["港湾餐饮管理有限公司", "海岸酒店管理有限公司", "山脊控股有限公司", "西田集团"]}
#: (category, items that fit it) -- the category is the option the form offers under that name.
CATEGORIES = {
    "en": {"Meals": ["Flat white", "Club sandwich", "Garden salad", "Soup of the day", "Iced tea", "Pasta"],
           "Travel": ["Taxi fare", "Airport transfer", "Rail ticket", "Parking"],
           "Lodging": ["Room, 1 night", "City tax", "Breakfast"],
           "Office supplies": ["Printer paper", "Gel pens (10)", "Stapler", "Notebooks (3)"],
           "Software": ["Annual license", "Cloud storage plan", "Plugin pack"]},
    "zh": {"餐饮": ["拿铁", "三明治", "沙拉", "例汤", "冰茶", "意面"],
           "交通": ["出租车费", "机场接送", "火车票", "停车费"],
           "住宿": ["客房 1 晚", "城市税", "早餐"],
           "办公用品": ["打印纸", "中性笔（10支）", "订书机", "笔记本（3本）"],
           "软件": ["年度授权", "云存储套餐", "插件包"]},
}
MERCHANT_CATEGORY = {0: "Meals", 1: "Meals", 2: "Office supplies", 3: "Travel", 4: "Software", 5: "Lodging",
                     6: "Meals", 7: "Travel", 8: "Office supplies", 9: "Lodging"}
ZH_CATEGORY = {"Meals": "餐饮", "Travel": "交通", "Lodging": "住宿", "Office supplies": "办公用品", "Software": "软件"}
PAYMENTS = {"en": ["VISA **** 4821", "Mastercard **** 1107", "Amex **** 3009", "Cash"],
            "zh": ["微信支付", "支付宝", "银联卡 **** 6621", "现金"]}
#: Receipt templates per language (layout and font); split 2 holds one per language out.
RECEIPT_TEMPLATES = {"en": ["mono-centered", "sans-left", "boxed"], "zh": ["hei-centered", "hei-left", "boxed-zh"]}
#: The form apps (skins): name, colour, layout, the form's date format.
APPS = [("Expensely", "#2b7a4b", "stacked", "YYYY-MM-DD"), ("ClaimDesk", "#34528a", "two-column", "MM/DD/YYYY"),
        ("报销通", "#a5532e", "table", "YYYY-MM-DD"), ("费用宝", "#37655a", "compact", "YYYY-MM-DD")]
#: Held out for evaluation only: never in training rows.
HELDOUT_APPS = {"费用宝"}
TEXTS = {
    "en": {"title": "Expense report", "merchant": "Merchant", "date": "Date", "amount": "Amount",
           "category": "Category", "notes": "Notes (optional)", "submit": "Submit", "choose": "Choose…",
           "required": "Required", "bad_date": "Use the format {fmt}", "bad_amount": "Enter a number",
           "done": "Expense submitted"},
    "zh": {"title": "报销单", "merchant": "商户", "date": "日期", "amount": "金额", "category": "类别",
           "notes": "备注（选填）", "submit": "提交", "choose": "请选择…", "required": "必填",
           "bad_date": "请按 {fmt} 格式填写", "bad_amount": "请输入数字", "done": "报销已提交"},
}
GOALS = {
    "en": ["{viewer} has a receipt open. Fill in the expense report in {app} from it and submit it.",
           "Using the receipt open in {viewer}, complete the {app} expense form and submit.",
           "File an expense in {app} for the receipt shown in {viewer}: merchant, date, total and category, then submit.",
           "Copy the receipt in {viewer} into a new {app} expense report and submit it."],
    "zh": ["{viewer}里打开着一张收据，请据此在{app}里填写报销单并提交。",
           "根据{viewer}中的收据，把{app}的报销单填好并提交。",
           "用{viewer}里那张收据在{app}里报销：填商户、日期、总金额和类别，然后提交。",
           "把{viewer}里的收据录入{app}的报销单并提交。"],
}


def _template_ids(lang: str) -> list[str]:
    return [f"expense/{lang}/{i}" for i in range(len(GOALS[lang]))]


def _merchants(lang: str) -> list[str]:
    return [m for m, _ in (ZH_MERCHANTS if lang == "zh" else EN_MERCHANTS)]


def _pools(heldout: bool) -> dict:
    """What a task draws from: everything, or (training under split 2) without the held-out goal template, receipt
    template and merchants of each language."""
    if heldout:
        return {"merchants": {lang: _merchants(lang) for lang in ("zh", "en")},
                "templates": {lang: list(range(len(GOALS[lang]))) for lang in ("zh", "en")},
                "receipts": {lang: list(RECEIPT_TEMPLATES[lang]) for lang in ("zh", "en")}}
    held_goal = {lang: split.heldout_template("expense", lang, _template_ids(lang)) for lang in ("zh", "en")}
    held_receipt = {lang: split.heldout_template("expense-receipt", lang, RECEIPT_TEMPLATES[lang]) for lang in ("zh", "en")}
    return {"merchants": {lang: split.train_values("expense", f"{lang}_merchants", _merchants(lang)) for lang in ("zh", "en")},
            "templates": {lang: [i for i in range(len(GOALS[lang])) if f"expense/{lang}/{i}" != held_goal[lang]]
                          for lang in ("zh", "en")},
            "receipts": {lang: [t for t in RECEIPT_TEMPLATES[lang] if t != held_receipt[lang]] for lang in ("zh", "en")}}


def make_task(seed: int, split_version: int = split.DEFAULT_VERSION) -> dict:
    """The task of `seed`. Split 1 (the default): held out by skin only. Split 2: concept-disjoint (tools/gym/split.py)."""
    base = _make(seed, random.Random(f"expense-{seed}"), _pools(True))
    if split_version == 1:
        return base
    return split.pick(seed, "expense", base["split"], lambda rnd, heldout: _make(seed, rnd, _pools(heldout)),
                      lambda t: t["app"] in HELDOUT_APPS)


def fmt_date(d: dt.date, fmt: str) -> str:
    """A date as the form wants it (YYYY-MM-DD or MM/DD/YYYY)."""
    return d.strftime("%m/%d/%Y") if fmt == "MM/DD/YYYY" else d.isoformat()


def receipt_date(d: dt.date, style: str) -> str:
    """A date as the receipt prints it."""
    return {"iso": d.isoformat(), "us": d.strftime("%m/%d/%Y"),
            "zh": f"{d.year}年{d.month}月{d.day}日"}[style]


def _make(seed: int, rnd: random.Random, pools: dict) -> dict:
    lang = rnd.choice(["zh", "en"])
    app, color, layout, date_fmt = rnd.choice(APPS)
    merchants = ZH_MERCHANTS if lang == "zh" else EN_MERCHANTS
    names = [m for m, _ in merchants]
    mi = names.index(rnd.choice(pools["merchants"][lang]))
    merchant, address = merchants[mi]
    cat_en = MERCHANT_CATEGORY[mi]
    category = ZH_CATEGORY[cat_en] if lang == "zh" else cat_en
    items = [(it, rnd.randint(1, 3), round(rnd.uniform(2.5, 48), 2))
             for it in rnd.sample(CATEGORIES[lang][category], min(3, len(CATEGORIES[lang][category])))]
    subtotal = round(sum(q * p for _, q, p in items), 2)
    tax = round(subtotal * rnd.choice([0.05, 0.06, 0.08, 0.1]), 2)
    tip = round(subtotal * 0.15, 2) if cat_en == "Meals" and rnd.random() < 0.4 else 0.0
    total = round(subtotal + tax + tip, 2)
    visit = dt.date(2026, rnd.randint(1, 9), rnd.randint(1, 27))
    printed = visit + dt.timedelta(days=rnd.randint(1, 3))
    style = "zh" if lang == "zh" and rnd.random() < 0.6 else rnd.choice(["iso", "us"])
    tpl = rnd.choice(pools["receipts"][lang])
    receipt = {"merchant": merchant, "address": address, "operator": rnd.choice(OPERATORS[lang]),
               "items": items, "subtotal": subtotal, "tax": tax, "tip": tip, "total": total,
               "visit": visit.isoformat(), "printed": f"{printed.isoformat()} {rnd.randint(8, 22):02d}:{rnd.randint(0, 59):02d}",
               "date_style": style, "payment": rnd.choice(PAYMENTS[lang]), "category": category,
               "order": f"#{rnd.randint(1000, 9999)}", "template": tpl}
    ti = rnd.choice(pools["templates"][lang])
    goal = GOALS[lang][ti].format(viewer=VIEWER[lang], app=app)
    template_id = f"expense/{lang}/{ti}"
    options = [ZH_CATEGORY[c] for c in CATEGORIES["en"]] if lang == "zh" else list(CATEGORIES["en"])
    want = {"merchant": merchant, "date": fmt_date(visit, date_fmt), "amount": f"{total:.2f}", "category": category}
    held_receipt = split.heldout_template("expense-receipt", lang, RECEIPT_TEMPLATES[lang])
    return {"seed": seed, "lang": lang, "app": app, "goal": goal, "want": want, "receipt": receipt,
            "viewer": VIEWER[lang], "split": "heldout" if app in HELDOUT_APPS else "train",
            "split_version": 1, "template_id": template_id,
            "template_heldout": template_id == split.heldout_template("expense", lang, _template_ids(lang)),
            "concepts": [split.concept("merchant", "expense", f"{lang}_merchants", merchant, names),
                         {"slot": "receipt_template", "pool": f"expense/{lang}_receipts", "value": tpl,
                          "heldout": tpl == held_receipt}],
            "page": {"app": app, "color": color, "layout": layout, "date_format": date_fmt, "lang": lang,
                     "texts": TEXTS[lang], "options": options}}


# -- the receipt image ---------------------------------------------------------------------------------------------

def _font(lang: str, template: str, size: int):
    from PIL import ImageFont
    # STHeiti Light first: in Hiragino Sans GB, Vision's OCR read 餐 as 䬸 (and 餐饮 as 䬸饮) at most sizes a receipt
    # is shown at -- 19 of 48 renderings misread against 1 of 48 in STHeiti Light (09-30) -- so the merchant was
    # never among the options, even for the oracle.
    zh = ["/System/Library/Fonts/STHeiti Light.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc"]
    en = {"mono-centered": ["/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc"],
          "sans-left": ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc"],
          "boxed": ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/SFNSMono.ttf"]}
    for f in (zh if lang == "zh" else en.get(template, en["mono-centered"])):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def receipt_lines(task: dict) -> list[tuple[str, int]]:
    """The receipt's lines, top to bottom, with a type size each (0: a blank line)."""
    r, lang = task["receipt"], task["lang"]
    visit = dt.date.fromisoformat(r["visit"])
    money = lambda v: f"{v:.2f}"
    if lang == "zh":
        lines = [(r["merchant"], 44), (r["address"], 24), (f"运营方：{r['operator']}", 22), ("", 14),
                 (f"单号 {r['order']}", 26), (f"打印时间 {r['printed']}", 24), ("", 12)]
        lines += [(f"{it} x{q}    {money(q * p)}", 28) for it, q, p in r["items"]]
        lines += [("", 10), (f"小计    {money(r['subtotal'])}", 28), (f"税费    {money(r['tax'])}", 28)]
        if r["tip"]:
            lines += [(f"小费    {money(r['tip'])}", 28)]
        lines += [(f"合计    {money(r['total'])}", 38), ("", 12), (f"支付方式：{r['payment']}", 26),
                  (f"消费日期：{receipt_date(visit, r['date_style'])}", 28), (f"类别：{r['category']}", 26),
                  ("", 10), ("谢谢惠顾", 26)]
    else:
        lines = [(r["merchant"].upper(), 44), (r["address"], 24), (f"Operated by: {r['operator']}", 22), ("", 14),
                 (f"Order {r['order']}", 26), (f"Printed {r['printed']}", 24), ("", 12)]
        lines += [(f"{q}x {it}    {money(q * p)}", 28) for it, q, p in r["items"]]
        lines += [("", 10), (f"Subtotal    {money(r['subtotal'])}", 28), (f"Tax    {money(r['tax'])}", 28)]
        if r["tip"]:
            lines += [(f"Tip    {money(r['tip'])}", 28)]
        lines += [(f"TOTAL    {money(r['total'])}", 38), ("", 12), (f"Paid by {r['payment']}", 26),
                  (f"Date of visit: {receipt_date(visit, r['date_style'])}", 28),
                  (f"Category: {r['category']}", 26), ("", 10), ("Thank you!", 26)]
    return lines


def render_receipt(task: dict, path: Path) -> Path:
    """The receipt as a PNG (pixels only, no text layer): read by vision, as a photographed receipt is."""
    from PIL import Image, ImageDraw
    tpl, lang = task["receipt"]["template"], task["lang"]
    lines = receipt_lines(task)
    W = 780
    H = 80 + sum((s or 14) + 20 for _, s in lines)
    img = Image.new("RGB", (W, H), (252, 250, 244) if "boxed" not in tpl else (255, 255, 255))
    g = ImageDraw.Draw(img)
    if "boxed" in tpl:
        g.rectangle((16, 16, W - 16, H - 16), outline=(40, 40, 40), width=3)
    y = 44
    left = "left" in tpl
    for text, size in lines:
        if text:
            f = _font(lang, tpl, size)
            w = g.textlength(text, font=f)
            g.text((56 if left else (W - w) / 2, y), text, fill=(28, 28, 28), font=f)
        y += (size or 14) + 20
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


# -- staging: the receipt in Preview, beside the form ---------------------------------------------------------------

def _display_frame() -> tuple[float, float, float, float] | None:
    """The display the gym runs on (HANDS_GYM_DISPLAY), as x, y, w, h in points; None for the main one."""
    did = os.environ.get("HANDS_GYM_DISPLAY", "").strip()
    if not did.isdigit():
        return None
    import Quartz
    b = Quartz.CGDisplayBounds(int(did))
    return (b.origin.x, b.origin.y, b.size.width, b.size.height)


def _ax_windows(bundle: str):
    import ApplicationServices as AS
    import Quartz
    from AppKit import NSRunningApplication
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0) or []
    pids = {w.get("kCGWindowOwnerPID") for w in wins}
    for pid in pids:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
        if app is not None and (app.bundleIdentifier() or "") == bundle:
            el = AS.AXUIElementCreateApplication(pid)
            AS.AXUIElementSetMessagingTimeout(el, 2.0)
            err, ws = AS.AXUIElementCopyAttributeValue(el, "AXWindows", None)
            for w in (ws or []) if err == 0 else []:
                yield w


def _ax_title(w) -> str:
    import ApplicationServices as AS
    err, t = AS.AXUIElementCopyAttributeValue(w, "AXTitle", None)
    return t or "" if err == 0 else ""


def _ax_place(w, x: float, y: float, width: float, height: float) -> None:
    import ApplicationServices as AS
    import Quartz
    AS.AXUIElementSetAttributeValue(w, "AXPosition", AS.AXValueCreate(AS.kAXValueCGPointType, Quartz.CGPoint(x, y)))
    AS.AXUIElementSetAttributeValue(w, "AXSize", AS.AXValueCreate(AS.kAXValueCGSizeType, Quartz.CGSize(width, height)))


def stage(task: dict, workdir: Path, host_bundle: str) -> dict:
    """Open the task's receipt in Preview beside the form: receipt on the left half of the gym's display, form on the
    right. Returns the extra apps for hands (--apps) and the apps read by vision."""
    path = render_receipt(task, workdir / f"receipt-s{task['seed']}.png")
    task["receipt_file"] = path.name
    subprocess.run(["/usr/bin/open", "-g", "-a", "Preview", str(path)], capture_output=True, timeout=20)
    frame = _display_frame() or (0.0, 0.0, 1728.0, 1117.0)
    x, y, w, h = frame
    top = y + 30
    for _ in range(40):
        pw = next((w_ for w_ in _ax_windows(PREVIEW) if _ax_title(w_) == path.name), None)
        if pw is not None:
            _ax_place(pw, x, top, w * 0.45, h - 40)
            # Preview's main window, whatever else it has open: switching to Preview shows this receipt.
            import ApplicationServices as AS
            AS.AXUIElementSetAttributeValue(pw, "AXMain", True)
            AS.AXUIElementPerformAction(pw, "AXRaise")
            break
        time.sleep(0.25)
    for hw in _ax_windows(host_bundle):
        _ax_place(hw, x + w * 0.45, top, w * 0.55, h - 40)
    return {"apps": {task["viewer"]: PREVIEW}, "vision": [PREVIEW]}


def unstage(task: dict) -> None:
    """Close the task's receipt window (its close button, through accessibility: nothing else of Preview's)."""
    import ApplicationServices as AS
    name = task.get("receipt_file")
    for w in _ax_windows(PREVIEW):
        if name and _ax_title(w) == name:
            err, b = AS.AXUIElementCopyAttributeValue(w, "AXCloseButton", None)
            if err == 0 and b is not None:
                AS.AXUIElementPerformAction(b, "AXPress")


# -- grading, oracle, trap -----------------------------------------------------------------------------------------

def _same(field: str, got, want: str) -> bool:
    got = (got or "").strip()
    if field == "amount":
        try:
            return abs(float(got.replace(",", "")) - float(want)) < 0.005
        except ValueError:
            return False
    return got.casefold() == want.strip().casefold()


def passed(task: dict, state: dict, answer: str | None = None) -> bool:
    sub = state.get("submitted") or None
    return bool(sub) and all(_same(f, sub.get(f), v) for f, v in task["want"].items())


FIELDS = ("merchant", "date", "amount", "category")


def oracle(task: dict, app_state: dict, request: dict) -> tuple[dict | None, str]:
    """The right answer to this request, keyed by the request's own option keys, and why; None if not offered."""
    from tools.gym.music import _choose
    q = request["questions"]
    els = request["state"]["elements"]
    page = request["state"].get("page") or {}
    here = (page.get("title") or "").strip()
    texts, want = task["page"]["texts"], task["want"]
    fields = app_state.get("fields") or {}
    on_form = here == task["app"]

    def labelled(op: str, why: str, **heads):
        label = _choose(q, op, **heads)
        return (label, why) if label else (None, f"{op} or its target not offered ({why})")

    def switch(to_form: bool, why: str):
        # The exact window when it is offered (Preview may hold other documents), else the app.
        crit = (q.get("focus_window_target") or {}).get("criteria") or {}
        want_title = task["app"] if to_form else task.get("receipt_file", "receipt")
        key = next((k for k, v in crit.items() if str(v).split(" (")[0].strip() == want_title), None)
        label = _choose(q, "FOCUS_WINDOW", focus_window_target=key) if key else None
        if label:
            return label, why
        return labelled("FOCUS_APP", why, focus_app_target="ai.deskmind.gymhost" if to_form else PREVIEW)

    if app_state.get("submitted"):
        if passed(task, app_state):
            return labelled("DONE", "the expense is submitted with the receipt's values")
        return None, "submitted with a wrong value: nothing a step can undo"
    wrong = [f for f in FIELDS if not _same(f, fields.get(f), want[f])]
    if not wrong:
        if not on_form:
            return switch(True, "every field is right: go back to the form to submit it")
        button = next((e["index"] for e in els if e.get("label") == texts["submit"]
                       and "CLICK" in (e.get("operations") or [])), None)
        return labelled("CLICK", "every field matches the receipt: submit", click_target=button)
    field = wrong[0]
    if field == "category":
        if not on_form:
            return switch(True, "the category is still to be chosen in the form")
        opts = (q.get("select_target") or {}).get("criteria") or {}
        el = next((e for e in els if e.get("label") == texts["category"] and e.get("options")), None)
        key = next((o["index"] for o in (el or {}).get("options", []) if o.get("value") == want["category"]), None)
        return labelled("SELECT", f"choose the category the receipt names ({want['category']})",
                        select_target=key if key in opts else None)
    vals = (q.get("type_text_value") or {}).get("criteria") or {}
    key = next((k for k, v in vals.items() if _same(field, v.get("value"), want[field])), None)
    if not on_form:
        return switch(True, f"the receipt has been read: fill the {field} in the form")
    if key is None:
        # The right value is not among the choices yet: it has not been read. Read the receipt first.
        return switch(False, f"the {field} is on the receipt: look at it in {task['viewer']}")
    box = next((e["index"] for e in els if e.get("label") == texts[field]
                and "TYPE_TEXT" in (e.get("operations") or [])), None)
    what = {"merchant": "the merchant's name at the top, not the operator",
            "date": "the visit date in the form's format, not the printed time",
            "amount": "the TOTAL, not the subtotal"}[field]
    fix = " (fix the wrong value before submitting)" if (fields.get(field) or "").strip() else ""
    return labelled("TYPE_TEXT", f"type {what}{fix}", type_text_target=box, type_text_value=key)


def trap(task: dict, app_state: dict, request: dict) -> tuple | None:
    """A plausible wrong step, one kind per task: the subtotal typed as the amount, the printed date as the date, the
    operator as the merchant, or Submit pressed with the category still empty. All are put right by the oracle (a
    field is retyped; the form refuses the empty submit)."""
    from tools.gym.music import _choose
    q, els = request["questions"], request["state"]["elements"]
    page = request["state"].get("page") or {}
    if (page.get("title") or "").strip() != task["app"] or app_state.get("submitted"):
        return None
    texts, r = task["page"]["texts"], task["receipt"]
    fields = app_state.get("fields") or {}
    kind = ["subtotal", "printed", "operator", "empty_submit"][task["seed"] % 4]
    vals = (q.get("type_text_value") or {}).get("criteria") or {}

    def type_wrong(field: str, value: str, why: str):
        if (fields.get(field) or "").strip():
            return None
        key = next((k for k, v in vals.items() if (v.get("value") or "").strip() == value), None)
        box = next((e["index"] for e in els if e.get("label") == texts[field]), None)
        label = _choose(q, "TYPE_TEXT", type_text_target=box, type_text_value=key)
        return (label, f"trap: {why}") if label else None

    if kind == "subtotal":
        return type_wrong("amount", f"{r['subtotal']:.2f}", "typed the subtotal as the amount")
    if kind == "printed":
        printed = dt.date.fromisoformat(r["printed"].split()[0])
        return type_wrong("date", fmt_date(printed, task["page"]["date_format"]), "typed the printed date")
    if kind == "operator":
        return type_wrong("merchant", r["operator"], "typed the operator as the merchant")
    if (fields.get("category") or "") == "" and all(_same(f, fields.get(f), task["want"][f])
                                                   for f in ("merchant", "date", "amount")):
        button = next((e["index"] for e in els if e.get("label") == texts["submit"]), None)
        label = _choose(q, "CLICK", click_target=button)
        return (label, "trap: submitted with the category empty") if label else None
    return None

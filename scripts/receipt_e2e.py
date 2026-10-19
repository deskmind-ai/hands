"""End to end, on a real desktop: the files receipt of a TextEdit save-as and a cmd+S, through the executor's own
JSON protocol (begin / observe / perform / end), as harness drives it.

    uv run --extra macos python scripts/receipt_e2e.py

Needs an unlocked Mac with the peekaboo CLI and the accessibility permissions hands already uses. It opens its own
draft document in TextEdit in the background, saves it as hello.txt in a temporary task folder through the driver's
projected save-as, presses cmd+S on the unchanged result, edits the document (by TextEdit's scripting, standing in
for the user's typing) and presses cmd+S again, prints the replies, and closes only the documents it opened (TextEdit
is quit again if this script launched it). Nothing of the user's is touched.

Pass: the save-as reply carries files == [<task folder>/hello.txt]; the first cmd+S (nothing to save) carries no
files and document == that path; the second cmd+S (unsaved changes before, none after) carries the file again.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="receipt-e2e-"))
    draft = ws / "draft.txt"
    draft.write_text("receipt check\n", encoding="utf-8")
    textedit_was_running = subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True).returncode == 0
    subprocess.run(["open", "-g", "-a", "TextEdit", str(draft)], check=True)
    time.sleep(3)

    p = subprocess.Popen([sys.executable, "-m", "deskmind_hands.executor"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, cwd=ROOT)
    n = 0
    failures: list[str] = []

    def call(op: str, **kw):
        nonlocal n
        n += 1
        assert p.stdin and p.stdout
        p.stdin.write(json.dumps({"id": n, "op": op, **kw}, ensure_ascii=False) + "\n")
        p.stdin.flush()
        r = json.loads(p.stdout.readline())
        if not r.get("ok"):
            raise RuntimeError(f"{op}: {r.get('error')}")
        return r["result"]

    def show(label: str, r: dict) -> None:
        print(f"{label}: " + json.dumps({k: v for k, v in r.items() if k not in ("after", "changed")}, ensure_ascii=False))

    try:
        snap = call("begin", workspace=str(ws))
        if snap.get("locked"):
            raise RuntimeError("the screen is locked; unlock it and run again")
        win = next((w for w in snap["windows"] if w["title"] == "draft.txt" and "TextEdit" in w["id"]), None)
        if win is None:
            raise RuntimeError(f"no TextEdit window titled draft.txt among {[w['title'] for w in snap['windows']]}")
        obs = call("observe", window=win["id"])
        syn = {e["id"]: e for e in obs["elements"] if e["id"].startswith("syn:saveas:")}
        if not syn:
            raise RuntimeError("the driver offered no save-as form for this window")
        options = syn["syn:saveas:folder"]["options"]
        folder = next((o for o in options if "工作目录" in o or "顶层" in o), options[0])
        show("name", call("perform", action={"kind": "set_value", "window": win["id"], "target": "syn:saveas:name", "value": "hello.txt"}))
        show("folder", call("perform", action={"kind": "select", "window": win["id"], "target": "syn:saveas:folder", "option": folder}))
        r = call("perform", action={"kind": "click", "window": win["id"], "target": "syn:saveas:do"})
        show("SAVE-AS", r)
        target = ws / "hello.txt"
        if r.get("files") != [str(target)]:
            failures.append(f"save-as receipt: expected files == [{target}], got {r.get('files')!r}")
        if not target.exists():
            failures.append("hello.txt was not written")

        snap = call("snapshot")
        win2 = next((w for w in snap["windows"] if w["title"] == "hello.txt" and "TextEdit" in w["id"]), None)
        if win2 is None:
            failures.append(f"no TextEdit window titled hello.txt after the save-as: {[w['title'] for w in snap['windows']]}")
        else:
            call("observe", window=win2["id"])
            r = call("perform", action={"kind": "key", "window": win2["id"], "chord": "cmd+s"})
            show("CMD+S (nothing to save)", r)
            if "files" in r and r["files"]:
                failures.append(f"cmd+S with nothing to save claimed files: {r['files']!r}")
            if r.get("document") != str(target):
                failures.append(f"cmd+S document: expected {target}, got {r.get('document')!r}")
            # the user types into the document (stood in for by TextEdit's scripting), then saves
            subprocess.run(["osascript", "-e", 'tell application "TextEdit" to set text of (first document whose name is "hello.txt") to "hello, edited"'],
                           capture_output=True, timeout=20)
            call("observe", window=win2["id"])
            r = call("perform", action={"kind": "key", "window": win2["id"], "chord": "cmd+s"})
            show("CMD+S (after an edit)", r)
            if r.get("files") != [str(target)]:
                failures.append(f"cmd+S after an edit: expected files == [{target}], got {r.get('files')!r}")
            if target.exists() and "edited" not in target.read_text(encoding="utf-8"):
                failures.append("the edit was not saved to hello.txt")
        call("end")
    except Exception as e:  # noqa: BLE001 -- reported below, after the cleanup
        failures.append(str(e))
    finally:
        try:
            p.stdin.close()  # type: ignore[union-attr]
            p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            p.kill()
        subprocess.run(["osascript", "-e", f'tell application "TextEdit" to close (every document whose path contains "{ws}") saving no'],
                       capture_output=True, timeout=30)
        if not textedit_was_running:
            subprocess.run(["osascript", "-e", 'tell application "TextEdit" to quit'], capture_output=True, timeout=30)
        print("task folder left for inspection:", ws, sorted(os.listdir(ws)))

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

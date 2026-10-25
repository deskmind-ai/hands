"""K1: the rc check scripts never bring DeskMind forward (acceptance: DeskMind appears 0 times in the front-app record)."""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class NoActivation(unittest.TestCase):
    def test_no_script_activates_an_app(self):
        found = []
        for p in sorted((REPO / "scripts/rc_e2e").glob("*.py")):
            code = "\n".join(l.split("#")[0] for l in p.read_text().splitlines() if not l.strip().startswith(('"""', "#")))
            for api in ("activateWithOptions_", "activateIgnoringOtherApps", ".activate(", "open -a", "NSApplicationActivate"):
                if api in code:
                    found.append(f"{p.name}: {api}")
        self.assertEqual(found, [])


class TitleBarClick(unittest.TestCase):
    """answer_asks.py clicks DeskMind's title bar only while DeskMind is the front app and its window is frontmost
    there (hands#70 review: a click while another app is in front activates DeskMind)."""

    def test_the_table(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("answer_asks", REPO / "scripts/rc_e2e/answer_asks.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)       # (main only runs as a script)
        def win(pid, x, y, w=400, h=300):
            return {"kCGWindowOwnerPID": pid, "kCGWindowLayer": 0, "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}
        dm, other = 10, 20
        rows = [("DeskMind front, its window frontmost", dm, [win(dm, 0, 0)], (200.0, 12.0)),
                ("another app front, DeskMind's window frontmost there", other, [win(dm, 0, 0), win(other, 500, 0)], None),
                ("the front app unknown", None, [win(dm, 0, 0)], None),
                ("DeskMind front, another window over its title bar", dm, [win(other, 0, 0), win(dm, 0, 0)], None),
                ("DeskMind front, no window of it", dm, [win(other, 0, 0)], None)]
        for name, front, wins, want in rows:
            self.assertEqual(m.title_bar_point(dm, front, wins), want, name)


if __name__ == "__main__":
    unittest.main()

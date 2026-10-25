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


if __name__ == "__main__":
    unittest.main()

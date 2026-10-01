"""tools/annotate_trace.py: each step drawn on the screenshot it was decided on."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.annotate_trace import annotate   # noqa: E402


class Annotate(unittest.TestCase):
    def test_a_box_on_the_element_and_a_banner_above(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "obs").mkdir()
            Image.new("RGB", (200, 100), (255, 255, 255)).save(run / "obs" / "obs-0001.png")
            recs = [{"t": "obs", "id": "obs-0001", "screenshot": "obs-0001.png",
                     "geometry": {"logical": [200, 100], "pixels": [200, 100]},
                     "elements": [{"id": "e1", "rect": [20, 30, 60, 20]}]},
                    {"t": "step", "n": 1, "obs": "obs-0001", "describe": "click #e1", "ok": True,
                     "action": {"kind": "click", "binding": {"element_id": "e1"}}},
                    {"t": "step", "n": 2, "obs": "obs-0001", "describe": "click #syn:save", "ok": False,
                     "detail": "refused", "action": {"kind": "click", "binding": {"element_id": "syn:save"}}}]
            (run / "trace.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
            paths = annotate(run)
            self.assertEqual([p.name for p in paths], ["step-01.png", "step-02.png"])
            im = Image.open(paths[0])
            self.assertGreater(im.size[1], 100)                      # the banner is added above
            top = im.size[1] - 100
            self.assertEqual(im.getpixel((20, top + 30))[:3], (220, 38, 38))   # the box's corner
            self.assertIn("refused", (run / "annotated" / "index.md").read_text())


if __name__ == "__main__":
    unittest.main()

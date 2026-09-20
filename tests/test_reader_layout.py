"""Run the emitted reader layout against a bounded geometry/observer model.

This is a deterministic regression for the width feedback loop, not browser
layout or a visual review. The model also executes the upstream code as a
control, so it must reproduce the original disclosure-triggered oscillation.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def reader_layout_source(html):
    """Extract both actual layout functions, without rewriting their behavior."""
    start = html.index("Archify.waitForStableLayout = function (options) {")
    factory = html.index("Archify.readerLayout = (function () {", start)
    end = html.index("\n    })();", factory) + len("\n    })();")
    return html[start:end]


@unittest.skipUnless(NODE, "A shared Node installation is required.")
class ReaderLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        data = {
            "schema_version": 3, "graph_mode": "dag", "title": "Reader width fixture",
            "details": [], "detail_uses": [],
            "scope": "Synthetic layout fixture; no mathematical assessment.",
            "source": {"title": "Layout fixture"}, "main_items": ["result"],
            "items": [
                {"id": "assumption", "kind": "assumption", "label": "Assumption 1",
                 "caption": "Condition", "statement_html": "A condition holds."},
                {"id": "result", "kind": "theorem", "label": "Theorem 2",
                 "caption": "Conclusion", "statement_html": "A conclusion follows."},
            ],
            "uses": [{"id": "condition-use", "from": "assumption", "to": "result",
                      "type": "dependency", "reason": "The conclusion uses the condition."}],
        }
        prepared, output = cls.base / "prepared.json", cls.base / "overview.html"
        prepared.write_text(json.dumps(data), encoding="utf-8")
        result = subprocess.run([NODE, str(SKILL / "scripts/render.mjs"), str(prepared), str(output)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr)
        html = output.read_text(encoding="utf-8")
        viewbox = re.search(r'<svg viewBox="0 0 ([\d.]+) ([\d.]+)" role="img"', html)
        if viewbox is None:
            raise AssertionError("Generated dependency diagram has no viewBox.")
        cls.packet = cls.base / "layout.json"
        cls.packet.write_text(json.dumps({
            "emitted": reader_layout_source(html),
            "upstream": reader_layout_source((SKILL / "assets/archify/template.html").read_text(encoding="utf-8")),
            "viewBox": {"width": float(viewbox[1]), "height": float(viewbox[2])},
        }), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_harness(self, mode):
        result = subprocess.run([NODE, str(SKILL / "tests/fixtures/reader-layout.mjs"), str(self.packet), mode],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_model_reproduces_upstream_disclosure_width_loop(self):
        result = self.run_harness("upstream")
        self.assertTrue(result["initial_stable"])
        self.assertFalse(result["expanded_stable"])
        self.assertGreaterEqual(result["width_changes"], 6)
        self.assertEqual(result["expanded_widths"], [960, 1232])

    def test_disclosures_scroll_without_repeated_width_changes(self):
        result = self.run_harness("emitted")
        self.assertEqual(result["stable_states"], ["collapsed", "expanded", "collapsed_again", "resized", "mobile"])
        self.assertEqual(result["desktop_widths"], [1232, 1232, 1232, 1072])
        self.assertGreater(result["expanded_overflow"], 0)
        self.assertEqual(result["disclosure_width_changes"], 0)
        self.assertTrue(result["mobile_override_cleared"])
        self.assertTrue(result["wide_diagram_tagged"])


if __name__ == "__main__":
    unittest.main()

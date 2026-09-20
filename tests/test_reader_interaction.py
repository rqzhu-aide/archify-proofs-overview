"""Exercise the emitted reader adapter in a deliberately small DOM simulation.

This is not browser layout, pointer hit testing, or a visual review. The real
generated adapter runs against a focus stub; every emitted script is also parsed.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ReaderDocument(HTMLParser):
    """Retain document structure, inert templates, and executable script text."""

    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.root = {"tag": "document", "attrs": {}, "children": [], "text": ""}
        self.stack = [self.root]
        self.scripts = []
        self.template = None
        self.feed(html)

    def handle_starttag(self, tag, pairs):
        if self.template is not None:
            self.template["innerHTML"] += self.get_starttag_text()
            return
        element = {"tag": tag, "attrs": dict(pairs), "children": [], "text": ""}
        self.stack[-1]["children"].append(element)
        if tag == "template":
            element["innerHTML"] = ""
            self.template = element
        elif tag not in VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag, pairs):
        self.handle_starttag(tag, pairs)
        if self.template is None and tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.template is not None:
            if tag == "template":
                self.template = None
            else:
                self.template["innerHTML"] += f"</{tag}>"
            return
        if len(self.stack) > 1 and self.stack[-1]["tag"] == tag:
            element = self.stack.pop()
            if tag == "script" and element["attrs"].get("type", "") not in {"application/json", "application/ld+json"}:
                self.scripts.append(element["text"])

    def handle_data(self, data):
        if self.template is not None:
            self.template["innerHTML"] += data
        elif self.stack[-1]["tag"] != "style":
            self.stack[-1]["text"] += data

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


@unittest.skipUnless(NODE, "A shared Node installation is required.")
class ReaderInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        dataset = {
            "schema_version": 3, "graph_mode": "dag", "title": "Shallow reader fixture",
            "details": [], "detail_uses": [],
            "scope": "Two synthetic statements; no mathematical assessment.",
            "source": {"title": "Reader fixture"}, "main_items": ["result"],
            "items": [
                {"id": "assumption", "kind": "assumption", "label": "Assumption 1", "caption": "Independent observations",
                 "statement_html": "ASSUMPTION_TEXT: the observations are independent and have finite variance."},
                {"id": "result", "kind": "theorem", "label": "Theorem 2", "caption": "Variance identity",
                 "statement_html": "RESULT_TEXT: the variance of the average is the individual variance divided by the sample size."},
            ],
            "uses": [{"id": "independence-use", "from": "assumption", "to": "result", "type": "dependency",
                      "regime": "Fixed sample", "reason": "RELATION_TEXT: independence removes the covariance terms."}],
        }
        prepared, output = cls.base / "prepared.json", cls.base / "overview.html"
        prepared.write_text(json.dumps(dataset), encoding="utf-8")
        rendered = subprocess.run([NODE, str(SKILL / "scripts/render.mjs"), str(prepared), str(output)],
                                  capture_output=True, text=True, encoding="utf-8")
        if rendered.returncode:
            raise AssertionError(rendered.stderr)
        parsed = ReaderDocument(output.read_text(encoding="utf-8"))
        cls.packet = cls.base / "reader-dom.json"
        cls.packet.write_text(json.dumps({"document": parsed.root, "scripts": parsed.scripts}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_harness(self, mode):
        result = subprocess.run([NODE, str(SKILL / "tests/fixtures/reader-interaction.mjs"), str(self.packet), mode],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_every_generated_executable_script_compiles(self):
        self.assertGreater(self.run_harness("compile")["compiled_scripts"], 1)

    def test_shallow_graph_details_selection_navigation_and_close(self):
        result = self.run_harness("interact")
        self.assertTrue(result["details_outside_diagram"])
        self.assertEqual(result["checked_selections"], ["assumption", "result", "independence-use"])
        self.assertTrue(result["close_cleared_details"])


if __name__ == "__main__":
    unittest.main()

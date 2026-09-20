"""Regression checks for authoring mistakes and trustworthy HTML generation.

Run with the shared Python installation:
    python -B -m unittest discover -s tests -v
"""
from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import proof_overview as overview


NODE_AVAILABLE = shutil.which("node") is not None
MATH_AVAILABLE = importlib.util.find_spec("latex2mathml") is not None


class TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "paper.tex"
        self.source.write_text(
            "% Synthetic source, used only by tests.\n"
            "\\begin{assumption}\\label{ass:variance}\n"
            "The variables are independent with finite variance.\n"
            "\\end{assumption}\n"
            "\\begin{theorem}\\label{thm:mean}\n"
            "$\\operatorname{Var}(\\bar X_n)=\\sigma^2/n$.\n"
            "\\end{theorem}\n"
            "The proof uses Assumption 1 to remove covariances.\n",
            encoding="utf-8",
        )
        self.data = {
            "schema_version": 3,
            "title": "A small proof overview",
            "scope": "Synthetic fixture: one assumption and one theorem.",
            "source": {"title": "Synthetic paper", "file": "paper.tex"},
            "items": [
                {
                    "id": "finite-variance",
                    "kind": "assumption",
                    "label": "Assumption 1",
                    "caption": "Independent finite-variance variables",
                    "statement": {"form": "synopsis", "text": "$X_1,\\ldots,X_n$ are independent, with variance $\\sigma^2$."},
                    "source": {"label": "ass:variance", "start_line": 2, "end_line": 4},
                },
                {
                    "id": "mean-variance",
                    "kind": "theorem",
                    "label": "Thm 2.3",
                    "caption": "Variance of the mean",
                    "statement": {"form": "synopsis", "text": "$\\operatorname{Var}(\\bar X_n)=\\frac{\\sigma^2}{n}$."},
                    "source": {"label": "thm:mean", "start_line": 5, "end_line": 7},
                },
            ],
            "uses": [
                {
                    "id": "use-variance-in-mean",
                    "from": "finite-variance",
                    "to": "mean-variance",
                    "reason": "Independence removes the covariance terms.",
                    "source": {"start_line": 8, "end_line": 8},
                }
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def write_dataset(self):
        path = self.base / "overview.json"
        path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def assert_invalid(self, data, *useful_words):
        with self.assertRaises(overview.OverviewError) as caught:
            overview.validate_data(data, self.base)
        message = str(caught.exception).lower()
        self.assertTrue(message.strip(), "The failure must explain its cause.")
        for word in useful_words:
            self.assertIn(word.lower(), message)
        return message

    def test_preparation_does_not_mutate_the_authored_dataset(self):
        original = deepcopy(self.data)
        prepared = overview.validate_data(self.data, self.base)
        self.assertEqual(self.data, original)
        self.assertEqual([row["id"] for row in prepared["items"]],
                         [row["id"] for row in original["items"]])
        self.assertIn("statement_html", prepared["items"][0])

    def test_selected_items_are_retained_even_without_connections(self):
        self.data["uses"] = []
        prepared = overview.validate_data(self.data, self.base)
        self.assertEqual(len(prepared["items"]), 2)
        self.assertEqual(prepared["uses"], [])

    def test_broken_reference_names_the_missing_item(self):
        self.data["uses"][0]["to"] = "absent-lemma"
        self.assert_invalid(self.data, "absent-lemma")

    def test_duplicate_item_ids_are_rejected(self):
        self.data["items"][1]["id"] = "finite-variance"
        self.assert_invalid(self.data, "finite-variance", "duplicate")

    def test_duplicate_uses_are_rejected_instead_of_drawing_twice(self):
        self.data["uses"].append(deepcopy(self.data["uses"][0]))
        self.assert_invalid(self.data, "duplicate")

    def test_circular_dependency_keeps_all_records_in_index_mode(self):
        self.data["uses"].append({
            "from": "mean-variance", "to": "finite-variance",
            "reason": "An intentionally circular fixture.",
        })
        prepared = overview.validate_data(self.data, self.base)
        self.assertEqual(prepared["graph_mode"], "index")
        self.assertEqual([item["label"] for item in prepared["items"]], ["Assumption 1", "Thm 2.3"])
        self.assertEqual(len(prepared["uses"]), 2)
        self.assertTrue(any("cycle" in warning for warning in prepared["warnings"]))

    def test_missing_source_is_an_actionable_error(self):
        self.data["source"]["file"] = "missing-paper.tex"
        self.assert_invalid(self.data, "missing-paper.tex")

    def test_line_anchors_must_be_complete_ordered_and_inside_the_file(self):
        invalid_ranges = [
            {"start_line": 2},
            {"start_line": 4, "end_line": 2},
            {"start_line": 8, "end_line": 9},
            {"start_line": 0, "end_line": 2},
        ]
        for location in invalid_ranges:
            with self.subTest(location=location):
                candidate = deepcopy(self.data)
                candidate["items"][0]["source"] = location
                self.assert_invalid(candidate, "line")

    def test_source_excerpt_is_exact_and_source_file_is_unchanged(self):
        before = self.source.read_bytes()
        prepared = overview.validate_data(self.data, self.base)
        expected = "\n".join(before.decode("utf-8").splitlines()[1:4])
        self.assertEqual(prepared["items"][0]["source_excerpt"].rstrip("\r\n"), expected)
        self.assertEqual(self.source.read_bytes(), before)

    def test_unknown_fields_fail_instead_of_silently_losing_content(self):
        for place, field in [("root", "scpoe"), ("item", "statment"),
                             ("use", "reasno"), ("source", "start_lien")]:
            with self.subTest(place=place):
                candidate = deepcopy(self.data)
                row = {
                    "root": candidate,
                    "item": candidate["items"][0],
                    "use": candidate["uses"][0],
                    "source": candidate["items"][0]["source"],
                }[place]
                row[field] = "This must not disappear."
                self.assert_invalid(candidate, field)

    def test_each_item_requires_a_meaningful_source_locator(self):
        self.data["items"][0]["source"] = {}
        self.assert_invalid(self.data, "provide", "label", "line range")

    def test_supplied_excerpt_can_be_identified_without_a_local_file(self):
        self.data["source"] = {"title": "An excerpt supplied by the researcher"}
        for item in self.data["items"]:
            item["source"] = {"label": item["label"]}
        self.data["uses"][0].pop("source")
        prepared = overview.validate_data(self.data, self.base)
        self.assertEqual(len(prepared["items"]), 2)

    def test_hostile_prose_is_escaped_while_math_remains_inspectable(self):
        self.data["items"][1]["statement"]["text"] = '<script>alert("OVERVIEW_TEST_EXECUTION")</script> $x^2$'
        prepared = overview.validate_data(self.data, self.base)
        rendered = prepared["items"][1]["statement_html"]
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("x", rendered)
        if MATH_AVAILABLE:
            self.assertIn("<math", rendered)

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_rendering_is_deterministic_and_contains_reader_and_graph_metadata(self):
        path = self.write_dataset()
        output = self.base / "overview.html"
        before = (path.read_bytes(), self.source.read_bytes())
        receipt = overview.render_file(path, output)
        first = output.read_bytes()
        overview.render_file(path, output)
        self.assertIsInstance(receipt, dict)
        self.assertEqual(first, output.read_bytes())
        self.assertEqual((path.read_bytes(), self.source.read_bytes()), before)
        content = first.decode("utf-8")
        parser = TagCollector()
        parser.feed(content)
        node_ids = {attrs["data-node-id"] for _, attrs in parser.tags if "data-node-id" in attrs}
        self.assertTrue({"finite-variance", "mean-variance"}.issubset(node_ids))
        self.assertIn("Thm 2.3", content)
        self.assertIn("Variance of the mean", content)
        self.assertIn("paper.tex", content)
        if MATH_AVAILABLE:
            self.assertTrue(any(tag == "math" for tag, _ in parser.tags))
            self.assertTrue(any(tag == "mfrac" for tag, _ in parser.tags))

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_html_does_not_activate_injected_markup_from_any_reader_field(self):
        attack = '</script><img src="OVERVIEW_TEST_EXECUTION" onerror="alert(1)">'
        self.data["title"] = attack
        self.data["items"][1]["caption"] = attack
        self.data["items"][1]["statement"]["text"] = attack + " $x^2$"
        self.data["uses"][0]["reason"] = attack
        self.source.write_text(self.source.read_text(encoding="utf-8") + attack + "\n", encoding="utf-8")
        self.data["items"][1]["source"]["end_line"] = 9
        path = self.write_dataset()
        output = self.base / "overview.html"
        overview.render_file(path, output)
        parser = TagCollector()
        parser.feed(output.read_text(encoding="utf-8"))
        injected = [(tag, attrs) for tag, attrs in parser.tags
                    if attrs.get("src") == "OVERVIEW_TEST_EXECUTION"
                    or attrs.get("onerror") == "alert(1)"]
        self.assertEqual(injected, [])

    def test_cli_reports_invalid_data_without_creating_a_report(self):
        self.data["uses"][0]["to"] = "absent-lemma"
        path = self.write_dataset()
        output = self.base / "overview.html"
        process = subprocess.run(
            [sys.executable, "-B", str(SKILL / "scripts" / "proof_overview.py"),
             "render", str(path), str(output)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("absent-lemma", process.stdout + process.stderr)
        self.assertNotIn("Traceback (most recent call last)", process.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

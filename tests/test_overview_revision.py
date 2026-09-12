"""Check selective views without losing or strengthening the authored argument."""
from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import proof_overview as overview


FIXTURE = Path(__file__).with_name("fixtures") / "branching.overview.json"
NODE_AVAILABLE = shutil.which("node") is not None


class RenderedGraph(HTMLParser):
    """Read the actual graph elements, independent of layout coordinates."""

    def __init__(self, html):
        super().__init__()
        self.nodes = set()
        self.edges = {}
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if "data-node-id" in attrs:
            self.nodes.add(attrs["data-node-id"])
        if "data-edge-from" in attrs and "data-edge-to" in attrs:
            key = (attrs["data-edge-from"], attrs["data-edge-to"])
            self.edges[key] = attrs

    def handle_data(self, data):
        self.text.append(data)


class OverviewRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def assert_invalid(self, data, *words):
        with self.assertRaises(overview.OverviewError) as caught:
            overview.validate_data(data, self.base)
        message = str(caught.exception).lower()
        for word in words:
            self.assertIn(word.lower(), message)

    def render(self):
        dataset = self.base / "overview.json"
        output = self.base / "overview.html"
        dataset.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        receipt = overview.render_file(dataset, output)
        html = output.read_text(encoding="utf-8")
        return dataset, output, receipt, html, RenderedGraph(html)

    def test_typed_dependencies_and_main_selection_preserve_authored_records(self):
        original = deepcopy(self.data)
        prepared = overview.validate_data(self.data, self.base)
        self.assertEqual(self.data, original)
        self.assertEqual(prepared["main_items"], original["main_items"])
        self.assertEqual({row["id"] for row in prepared["items"]},
                         {row["id"] for row in original["items"]})
        self.assertEqual(len(prepared["uses"]), len(original["uses"]))
        for expected, actual in zip(original["uses"], prepared["uses"]):
            self.assertEqual(actual["type"], expected.get("type", "dependency"))
            for field in ("from", "to", "reason", "regime", "source"):
                if field in expected:
                    self.assertEqual(actual[field], expected[field])

    def test_old_schema_one_dataset_does_not_require_the_new_fields(self):
        self.data.pop("main_items")
        for use in self.data["uses"]:
            use.pop("type", None)
            use.pop("regime", None)
        original = deepcopy(self.data)
        prepared = overview.validate_data(self.data, self.base)
        self.assertEqual(self.data, original)
        self.assertNotIn("main_items", prepared)
        self.assertTrue(all(use["type"] == "dependency" for use in prepared["uses"]))

    def test_main_selection_must_reference_existing_unique_items(self):
        cases = [
            ([], "nonempty"),
            (["uniform-rate", "missing-result"], "missing-result"),
            (["uniform-rate", "uniform-rate"], "duplicate"),
            ("uniform-rate", "list"),
            ({"uniform-rate": True}, "list"),
            ([17], "main_items"),
        ]
        for selection, reason in cases:
            with self.subTest(selection=selection):
                data = deepcopy(self.data)
                data["main_items"] = selection
                self.assert_invalid(data, "main_items", reason)

    def test_unknown_or_nontext_dependency_type_is_rejected(self):
        for value in ("proof", "verified", "", None, 1, ["dependency"]):
            with self.subTest(value=value):
                data = deepcopy(self.data)
                data["uses"][0]["type"] = value
                self.assert_invalid(data, "type")

    def test_regime_requires_nonempty_text(self):
        for value in ("", " \t ", None, 1, True, ["pilot"]):
            with self.subTest(value=value):
                data = deepcopy(self.data)
                data["uses"][0]["regime"] = value
                self.assert_invalid(data, "regime")

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_selective_overview_keeps_the_complete_graph_without_shortcuts(self):
        _, _, receipt, _, graph = self.render()
        expected_nodes = {row["id"] for row in self.data["items"]}
        expected_edges = {(row["from"], row["to"]) for row in self.data["uses"]}
        self.assertGreaterEqual(len(expected_nodes), 15)
        self.assertEqual(graph.nodes, expected_nodes)
        self.assertEqual(set(graph.edges), expected_edges)
        self.assertEqual(receipt["items"], len(expected_nodes))
        self.assertEqual(receipt["uses"], len(expected_edges))
        self.assertNotIn(("moments", "band-coverage"), graph.edges)
        self.assertNotIn(("centering", "uniform-rate"), graph.edges)

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_reader_can_recover_long_statements_and_qualified_dependencies(self):
        _, _, _, _, graph = self.render()
        visible_text = " ".join(graph.text)
        self.assertIn("Pilot-trained regime only", visible_text)
        self.assertIn("Reuse only the absolute-error argument", visible_text)
        self.assertIn("must remain accessible in the full details", visible_text)
        self.assertIn("separate coverage conditions", visible_text)

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_rendered_relationship_metadata_keeps_type_and_regime(self):
        _, _, _, _, graph = self.render()
        for use in self.data["uses"]:
            attrs = graph.edges[(use["from"], use["to"])]
            self.assertEqual(attrs.get("data-use-type"), use.get("type", "dependency"))
            self.assertEqual(attrs.get("data-use-regime", ""), use.get("regime", ""))

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_main_result_navigation_matches_the_authored_selection(self):
        _, _, _, _, graph = self.render()
        choices = {attrs["data-proof-main"] for tag, attrs in graph.tags
                   if tag == "button" and "data-proof-main" in attrs}
        self.assertEqual(choices, set(self.data["main_items"]))
        full_controls = [(tag, attrs) for tag, attrs in graph.tags
                         if attrs.get("id") == "proof-full-structure"]
        self.assertEqual(len(full_controls), 1)
        self.assertEqual(full_controls[0][0], "button")

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_missing_main_selection_uses_terminal_results_without_deleting_support(self):
        self.data.pop("main_items")
        _, _, _, _, graph = self.render()
        used_as_input = {use["from"] for use in self.data["uses"]}
        result_kinds = {"lemma", "proposition", "theorem", "corollary"}
        terminals = {item["id"] for item in self.data["items"]
                     if item["kind"] in result_kinds and item["id"] not in used_as_input}
        choices = {attrs["data-proof-main"] for tag, attrs in graph.tags
                   if tag == "button" and "data-proof-main" in attrs}
        self.assertEqual(choices, terminals)
        self.assertEqual(graph.nodes, {item["id"] for item in self.data["items"]})

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_one_use_can_retain_both_a_proof_argument_type_and_a_regime(self):
        use = next(use for use in self.data["uses"] if use.get("type") == "proof_argument")
        use["regime"] = "Conditional argument only"
        _, _, _, _, graph = self.render()
        attrs = graph.edges[(use["from"], use["to"])]
        self.assertEqual(attrs["data-use-type"], "proof_argument")
        self.assertEqual(attrs["data-use-regime"], "Conditional argument only")
        self.assertIn("Conditional argument only", " ".join(graph.text))

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_regime_text_is_preserved_without_becoming_executable_markup(self):
        attack = '</script><img src="OVERVIEW_REGIME_ATTACK" onerror="alert(123)">'
        self.data["uses"][0]["regime"] = attack
        prepared = overview.validate_data(self.data, self.base)
        self.assertEqual(prepared["uses"][0]["regime"], attack)
        _, _, _, _, graph = self.render()
        injected = [(tag, attrs) for tag, attrs in graph.tags
                    if attrs.get("src") == "OVERVIEW_REGIME_ATTACK"
                    or attrs.get("onerror") == "alert(123)"]
        self.assertEqual(injected, [])
        recovered = any(attack in text for text in graph.text)
        recovered = recovered or any(attack in value for _, attrs in graph.tags
                                     for value in attrs.values() if isinstance(value, str))
        self.assertTrue(recovered, "Escaping must preserve the authored regime as reader text.")

    @unittest.skipUnless(NODE_AVAILABLE, "A shared Node installation is required for HTML rendering.")
    def test_qualified_branching_render_is_deterministic(self):
        dataset, output, first_receipt, _, graph = self.render()
        original_json, original_html = dataset.read_bytes(), output.read_bytes()
        second_receipt = overview.render_file(dataset, output)
        self.assertEqual(dataset.read_bytes(), original_json)
        self.assertEqual(output.read_bytes(), original_html)
        self.assertEqual(first_receipt["sha256"], second_receipt["sha256"])
        self.assertEqual(graph.nodes, {row["id"] for row in self.data["items"]})


if __name__ == "__main__":
    unittest.main()

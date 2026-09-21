"""Exercise the prepared-record renderer independently of the storage adapter."""
from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


RENDERER = Path(__file__).resolve().parents[1] / "scripts" / "render.mjs"
NODE = shutil.which("node")


class RenderedRecords(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.svg_depth = 0
        self.nodes = []
        self.uses = []
        self.index_items = []
        self.index_uses = []
        self.record_text = []
        self.record_script = False
        self.regions = {}
        self.active_region = None
        self.feed(html)

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if tag == "svg":
            self.svg_depth += 1
        if self.svg_depth and "data-node-id" in attrs:
            self.nodes.append(attrs["data-node-id"])
        if self.svg_depth and "data-edge-id" in attrs:
            self.uses.append((attrs["data-edge-id"], attrs["data-edge-from"], attrs["data-edge-to"]))
        if tag == "article" and "data-proof-index-item" in attrs:
            self.index_items.append(attrs["data-proof-index-item"])
        if tag == "article" and "data-proof-index-use" in attrs:
            self.index_uses.append((attrs["data-proof-index-use"], attrs["data-proof-from"], attrs["data-proof-to"]))
        if tag in ("template", "article") and attrs.get("id"):
            self.active_region = (tag, attrs["id"])
            self.regions[self.active_region[1]] = []
        if tag == "script" and attrs.get("id") == "proof-overview-records":
            self.record_script = True

    def handle_endtag(self, tag):
        if tag == "svg":
            self.svg_depth -= 1
        if tag == "script":
            self.record_script = False
        if self.active_region and tag == self.active_region[0]:
            self.active_region = None

    def handle_data(self, text):
        if self.record_script:
            self.record_text.append(text)
        if self.active_region:
            self.regions[self.active_region[1]].append(text)

    @property
    def records(self):
        return json.loads("".join(self.record_text))


@unittest.skipUnless(NODE, "A shared Node installation is required.")
class RendererRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.input = self.base / "prepared.json"
        self.output = self.base / "overview.html"
        self.data = {
            "schema_version": 3,
            "details": [], "detail_uses": [],
            "graph_mode": "dag",
            "title": "Two distinct uses",
            "scope": "Synthetic renderer fixture.",
            "source": {"title": "Fixture paper"},
            "build_context": {
                "input_snapshot": "snapshot-example",
                "source_revision": "source-example",
                "source_status": "current",
                "source_comparison": {"status": "incomplete", "unreviewed": 2},
                "mathematical_assessment": "not_performed",
            },
            "items": [
                {"id": "bound", "kind": "lemma", "label": "Lemma 1", "caption": "Moment bound", "statement_html": "A bound holds.", "statement_form": "synopsis",
                 "aliases": ["lem:bound", "lem:moment"], "source_passages": [
                    {"role": "statement", "source_display": "main.tex:3", "source_excerpt": "STATEMENT PASSAGE", "verification": {"status": "checked", "method": "line_range"}},
                    {"role": "proof", "source_display": "supplement.tex:8", "source_excerpt": "PROOF PASSAGE", "verification": {"status": "checked", "method": "line_range"}},
                 ]},
                {"id": "rate", "kind": "theorem", "label": "Theorem 2", "caption": "Rate", "statement_html": "The rate follows."},
            ],
            "uses": [
                {"id": "use-moment", "from": "bound", "to": "rate", "type": "dependency", "reason": "Use the moment bound.",
                 "source_passages": [{"role": "evidence", "source_display": "main.tex:20", "source_excerpt": "FIRST USE PASSAGE", "verification": {"status": "checked", "method": "line_range"}}]},
                {"id": "use-tail", "from": "bound", "to": "rate", "type": "proof_argument", "regime": "Tail regime", "reason": "Use the separate tail argument.",
                 "source_passages": [{"role": "evidence", "source_display": "supplement.tex:30", "source_excerpt": "SECOND USE PASSAGE", "verification": {"status": "unverified", "method": "entered_locator"}}]},
            ],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def render(self, expect_success=True):
        self.input.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([NODE, str(RENDERER), str(self.input), str(self.output)], capture_output=True, text=True, encoding="utf-8")
        if not expect_success:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = self.output.read_text(encoding="utf-8")
        return json.loads(result.stdout), html, RenderedRecords(html)

    def expected_uses(self):
        return sorted((use["id"], use["from"], use["to"]) for use in self.data["uses"])

    def test_parallel_uses_retain_separate_svg_paths_and_exact_records(self):
        original = deepcopy(self.data)
        receipt, _, rendered = self.render()
        self.assertEqual(sorted(rendered.nodes), ["bound", "rate"])
        self.assertEqual(sorted(rendered.uses), self.expected_uses())
        self.assertEqual(rendered.records["items"], original["items"])
        self.assertEqual(rendered.records["uses"], original["uses"])
        self.assertEqual(rendered.records["build_context"], original["build_context"])
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["geometry"]["status"], "pass")
        for field in ("browser_review", "visual_review", "mathematical_assessment"):
            self.assertEqual(receipt[field], "not_performed")

    def test_main_result_selection_preserves_prerequisite_and_parallel_connections(self):
        self.data["main_items"] = ["rate"]
        _, html, rendered = self.render()
        self.assertEqual(sorted(rendered.nodes), ["bound", "rate"])
        self.assertEqual(sorted(rendered.uses), self.expected_uses())
        self.assertEqual(sorted(rendered.index_items), ["bound", "rate"])
        self.assertEqual(sorted(rendered.index_uses), self.expected_uses())
        self.assertIn("Selected statements and connections (2 statements, 2 connections)", html)
        self.assertTrue('<p class="proof-scope-lead">Synthetic renderer fixture.</p>' in html,
                        'The selected scope must remain visible above the graph.')
        self.assertIn('data-proof-main="rate"', html)
        self.assertNotIn('data-proof-main="bound"', html)
        self.assertNotIn("Intermediate steps (", html)

    def test_all_passages_are_available_in_selection_and_static_index(self):
        _, _, rendered = self.render()
        for region in ("proof-detail-bound", "proof-index-item-bound"):
            text = " ".join(rendered.regions[region])
            for expected in ("STATEMENT PASSAGE", "PROOF PASSAGE", "FIRST USE PASSAGE", "SECOND USE PASSAGE", "lem:moment"):
                self.assertIn(expected, text, region)
        for use_id, excerpt in (("use-moment", "FIRST USE PASSAGE"), ("use-tail", "SECOND USE PASSAGE")):
            for prefix in ("proof-use-", "proof-index-use-"):
                self.assertIn(excerpt, " ".join(rendered.regions[prefix + use_id]))

    def test_cycle_and_self_reference_have_complete_index_representation(self):
        self.data["graph_mode"] = "index"
        self.data["uses"].extend([
            {"id": "use-reverse", "from": "rate", "to": "bound", "type": "dependency", "reason": "A recorded reverse use."},
            {"id": "use-self", "from": "rate", "to": "rate", "type": "dependency", "reason": "An unresolved self reference."},
        ])
        receipt, html, rendered = self.render()
        self.assertEqual(rendered.nodes, [])
        self.assertEqual(rendered.uses, [])
        self.assertEqual(sorted(rendered.index_items), ["bound", "rate"])
        self.assertEqual(sorted(rendered.index_uses), self.expected_uses())
        self.assertEqual(rendered.records["uses"], self.data["uses"])
        self.assertEqual(receipt["graph_mode"], "index")
        self.assertEqual(receipt["geometry"]["status"], "not_applicable")
        self.assertEqual(receipt["graph_preservation"]["rendered_uses"], 4)
        self.assertIn("Cyclic mappings alone do not establish a circular proof", html)

    def test_invalid_candidate_preserves_last_successful_artifact(self):
        _, html, _ = self.render()
        self.data["uses"][1]["id"] = self.data["uses"][0]["id"]
        failure = self.render(expect_success=False)
        self.assertIn("unique", failure["error"])
        self.assertEqual(self.output.read_text(encoding="utf-8"), html)

    def test_passages_cannot_terminate_the_embedded_record_script(self):
        attack = '</script><img src="RECORD_ATTACK" onerror="alert(1)">'
        self.data["items"][0]["source_passages"][0]["source_excerpt"] = attack
        _, html, rendered = self.render()
        self.assertNotIn('<img src="RECORD_ATTACK"', html)
        self.assertEqual(rendered.records["items"][0]["source_passages"][0]["source_excerpt"], attack)
        self.assertIn(attack, " ".join(rendered.regions["proof-detail-bound"]))

    def test_reader_sees_synopsis_and_actionable_locator_notes_without_full_digest(self):
        digest = "abcdef0123456789" * 4
        self.data["build_context"]["source_revision"] = digest
        self.data["build_context"]["source_status"] = "historical_changed"
        self.data["items"][0]["source_passages"][1]["verification"] = {
            "status": "unverified", "method": "entered_locator",
            "note": "The entered label has not been matched; compare the source passage.",
        }
        _, html, rendered = self.render()
        visible_prefix = html.split('<script id="proof-overview-records"')[0]
        self.assertNotIn(digest, visible_prefix)
        self.assertIn('title="Captured revision ' + digest[:12], visible_prefix)
        self.assertIn("registered files have changed", visible_prefix)
        for region in ("proof-detail-bound", "proof-index-item-bound"):
            text = " ".join(rendered.regions[region])
            self.assertIn("Statement synopsis", text)
            self.assertIn("Locator not yet verified (entered locator)", text)
            self.assertIn("compare the source passage", text)
        self.assertEqual(rendered.records["build_context"]["source_revision"], digest)

    def test_repeated_build_uses_identical_prepared_snapshot(self):
        first, original, _ = self.render()
        second, repeated, _ = self.render()
        self.assertEqual(original, repeated)
        self.assertEqual(first["artifact_sha256"], second["artifact_sha256"])
        self.assertEqual(first["input_sha256"], second["input_sha256"])

    def test_pdf_extraction_notice_follows_source_type_in_item_and_use_panels(self):
        for row in (self.data["items"][0], self.data["uses"][0]):
            row["source_passages"][0].update({
                "source_media_type": "application/pdf", "source_display": "PDF p. 2",
                "verification": {"status": "checked", "method": "pdf_page_bounds"},
            })
        self.data["uses"][1]["source_passages"][0]["source_media_type"] = "text/x-tex"
        _, _, rendered = self.render()
        note = "Approximate text extracted from the PDF"
        for region in ("proof-detail-bound", "proof-index-item-bound",
                       "proof-use-use-moment", "proof-index-use-use-moment"):
            text = " ".join(rendered.regions[region])
            self.assertIn(note, text, region)
            self.assertIn("physical PDF page bounds", text, region)
            self.assertIn("Check the original page for formulas and layout", text, region)
        for region in ("proof-use-use-tail", "proof-index-use-use-tail"):
            self.assertNotIn(note, " ".join(rendered.regions[region]), region)

    def test_scan_summary_discloses_pairs_and_unmatchable_records_even_without_missing_uses(self):
        self.data["build_context"]["citation_candidates"] = {
            "pairs": 2, "attributed": 3, "unattributed": 7,
            "not_mechanically_matchable": 4, "missing_uses": 0,
            "unsupported_uses": 1, "unmatched_labels": 5,
        }
        _, html, _ = self.render()
        visible_prefix = html.split('<script id="proof-overview-records"')[0]
        for expected in ("2 cited result pairs", "3 attributed and 7 unattributed matched references",
                         "4 records without unique citation labels", "0 missing-use candidates",
                         "1 recorded uses not corroborated by this scan"):
            self.assertIn(expected, visible_prefix)


if __name__ == "__main__":
    unittest.main()

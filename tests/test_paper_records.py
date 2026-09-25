"""Forward checks for reusable records, source evidence, and checked delivery.

The fixture has a root manuscript, an inherited macro file, and two proof
passages in an appendix. It is synthetic and does not assert a proved theory.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_records as records
import proof_overview as overview


NODE_AVAILABLE = shutil.which("node") is not None


class RecordElements(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.items, self.uses = set(), set()
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("data-proof-index-item"):
            self.items.add(attrs["data-proof-index-item"])
        if attrs.get("data-proof-index-use"):
            self.uses.add(attrs["data-proof-index-use"])


class PaperRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.main = self.base / "main.tex"
        self.macros = self.base / "macros.tex"
        self.appendix = self.base / "appendix.tex"
        self.main.write_text(
            "\\input{macros}\n"
            "\\begin{assumption}\\label{ass:independence}\n"
            "The observations are independent with finite second moments.\n"
            "\\end{assumption}\n"
            "\\begin{theorem}\\label{thm:center}\\label{thm:main}\n"
            "For deterministic centers, the target is $\\target$.\n"
            "\\end{theorem}\n"
            "\\input{appendix}\n"
            "% \\input{unused-old-appendix}\n",
            encoding="utf-8",
        )
        self.macros.write_text("\\newcommand{\\target}{\\mu}\n", encoding="utf-8")
        self.appendix.write_text(
            "\\begin{proof}\n"
            "Apply the independence assumption to the centered sum.\n"
            "\\end{proof}\n"
            "\\begin{proof}[Alternative argument]\n"
            "Reuse the absolute-error argument under the pilot regime.\n"
            "\\end{proof}\n",
            encoding="utf-8",
        )
        self.seed = {
            "schema_version": 3,
            "title": "Synthetic source and review fixture",
            "scope": "Two selected declarations and their written dependency.",
            "source": {"title": "Synthetic manuscript", "file": "main.tex"},
            "main_items": ["centering"],
            "items": [
                {
                    "id": "independence", "kind": "assumption", "label": "Assumption 1",
                    "caption": "Independent observations",
                    "statement": {"text": "The observations are independent with finite second moments.", "form": "synopsis"},
                    "source": {"label": "ass:independence", "start_line": 2, "end_line": 4},
                },
                {
                    "id": "centering", "kind": "theorem", "label": "Theorem 1",
                    "caption": "Deterministic centering",
                    "statement": {"text": "For deterministic centers, the target is $\\mu$.", "form": "synopsis"},
                    "source": {"label": "thm:center", "start_line": 5, "end_line": 7},
                },
            ],
            "uses": [{
                "from": "independence", "to": "centering", "type": "dependency",
                "regime": "Deterministic regime", "reason": "Apply independence to the centered sum.",
                "source": {"file": "appendix.tex", "start_line": 1, "end_line": 3},
            }],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def migrate(self):
        return records.normalize(self.seed, self.base)

    def revalidate(self, data):
        candidate = deepcopy(data)
        candidate.pop("snapshot_id", None)
        return records.validate_records(candidate)

    def test_absent_proof_idea_preserves_historical_digests_and_projection(self):
        for source in (self.main, self.macros, self.appendix):
            source.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        data = self.migrate()
        # Captured before the optional field was introduced. Timestamp is not
        # part of snapshot identity; relative paths and CRLF bytes are fixed.
        self.assertEqual(data["snapshot_id"], "85f381c8577e5059244851ea6a01f9a18e5d5841bacf80385042cc0538ea5d8a")
        expected = {
            ("items", "independence"): "6d7f18566a556e1256ddd02f420b3e2f9bf320bd704b5c4ef956d2a4811e11a0",
            ("items", "centering"): "f3e3c109c26f0fadf8b452057c84136c76211c39e8b602297579bd0f753af2c5",
            ("uses", "use-05587ad2fac8192a"): "757e501a292e8e343ef41f871c4f1c4880fa647eba7030fd02e400095c80e24b",
        }
        self.assertEqual({key: records.target_digest(data, *key) for key in expected}, expected)
        prepared = records.prepare_records(data, self.base)
        self.assertTrue(all("proof_idea" not in row and "proof_idea_html" not in row for row in prepared["items"]))

    def test_proof_idea_flows_through_capture_refresh_and_safe_math_projection(self):
        idea = r"Apply independence to the centered sum $X-\mu$. Keep <script> literal."
        self.seed["items"][1]["proof_idea"] = idea
        data = self.migrate()
        before = deepcopy(data)
        prepared = records.prepare_records(data, self.base)
        row = next(row for row in prepared["items"] if row["id"] == "centering")
        self.assertEqual(row["proof_idea"], idea)
        self.assertIn("<math", row["proof_idea_html"])
        self.assertIn("&lt;script&gt;", row["proof_idea_html"])
        self.assertNotIn("<script>", row["proof_idea_html"])
        self.assertEqual(data, before)
        self.assertEqual(records.normalize(data, self.base), data)
        self.main.write_text(self.main.read_text(encoding="utf-8") + "% revision\n", encoding="utf-8")
        refreshed = records.refresh_sources(data, self.base)
        self.assertEqual(refreshed["items"][1]["proof_idea"], idea)
        self.assertNotEqual(refreshed["snapshot_id"], data["snapshot_id"])

    def test_proof_idea_rejects_malformed_seed_and_canonical_values(self):
        canonical = self.migrate()
        for value in (None, "", " \t\n", 1, [], {}, "bad\x00text"):
            with self.subTest(value=value):
                seed = deepcopy(self.seed)
                seed["items"][1]["proof_idea"] = value
                with self.assertRaisesRegex(records.RecordError, "proof_idea"):
                    records.normalize(seed, self.base)
                data = deepcopy(canonical)
                data["items"][1]["proof_idea"] = value
                with self.assertRaisesRegex(records.RecordError, "proof_idea"):
                    self.revalidate(data)

    def test_proof_idea_math_failure_has_located_diagnostic(self):
        self.seed["items"][1]["proof_idea"] = r"Use $\privateProofMacro$ in the final step."
        data = self.migrate()
        prepared = records.prepare_records(data, self.base)
        failures = [entry for entry in prepared["math_diagnostics"] if entry["field"] == "proof_idea"]
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["collection"], failures[0]["id"]), ("items", "centering"))
        self.assertIn(r"\privateProofMacro", failures[0]["excerpt"])
        self.assertIn('class="math-fallback"', prepared["items"][1]["proof_idea_html"])
        self.assertNotIn("proof_idea_html", data["items"][1])

    def compare_all(self, data):
        candidate = deepcopy(data)
        targets = [{"collection": group, "id": row["id"]}
                   for group in ("items", "uses") for row in candidate[group]]
        candidate["observations"].extend(records.make_observations(
            candidate, targets, reviewer="Synthetic source reviewer",
            note="Compared the bounded synthetic passages and applicable conditions.",
        ))
        return records.validate_records(candidate)

    def add_second_proof_passage(self, data):
        candidate = deepcopy(data)
        first_id = candidate["uses"][0]["evidence_refs"][0]
        first = next(anchor for anchor in candidate["anchors"] if anchor["id"] == first_id)
        second = deepcopy(first)
        second["id"] = "anchor-alternative-proof"
        second["locator"] = {"start_line": 4, "end_line": 6}
        second["excerpt"] = "\n".join(self.appendix.read_text(encoding="utf-8").splitlines()[3:6])
        second["excerpt_hash"] = hashlib.sha256(second["excerpt"].encode("utf-8")).hexdigest()
        candidate["anchors"].append(second)
        candidate["items"][1]["passages"].extend([
            {"role": "proof", "anchor_id": first_id},
            {"role": "proof", "anchor_id": second["id"]},
        ])
        return self.revalidate(candidate)

    def test_capture_preserves_seed_content_qualifications_and_identity(self):
        original = deepcopy(self.seed)
        migrated = self.migrate()
        self.assertEqual(self.seed, original)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["main_items"], original["main_items"])
        for before, after in zip(original["items"], migrated["items"]):
            for key in ("id", "kind", "label", "caption"):
                self.assertEqual(after[key], before[key])
            self.assertEqual(after["statement"], before["statement"])
            self.assertEqual(after["statement"]["form"], "synopsis")
        use = migrated["uses"][0]
        for key in ("from", "to", "type", "regime", "reason"):
            self.assertEqual(use[key], original["uses"][0][key])
        self.assertTrue(use["id"])
        self.assertEqual(records.normalize(migrated, self.base), migrated)

    def test_capture_does_not_invent_a_completed_source_review(self):
        migrated = self.migrate()
        state = records.comparison_status(migrated)
        self.assertEqual(state["matched"], 0)
        self.assertEqual(state["unreviewed"], 3)
        self.assertEqual(state["status"], "incomplete")

    def test_partial_locator_warning_retains_checked_line_ranges(self):
        self.seed["items"][0]["source"].update(label="Assumption 1", page=5)
        self.seed["items"][1]["source"]["label"] = "Theorem 1"
        migrated = self.compare_all(self.migrate())
        original = deepcopy(migrated)
        prepared = records.prepare_records(migrated, self.base)
        warnings = prepared["warnings"]
        self.assertEqual(migrated, original)
        self.assertEqual(records.comparison_status(migrated)["status"], "complete")
        self.assertEqual(len(warnings), 1)
        self.assertIn("Source locator checks: 3 text line ranges", warnings[0])
        self.assertIn("2 anchors still have locator details", warnings[0])
        self.assertIn("2 entered/printed labels not mechanically matched", warnings[0])
        self.assertIn("1 PDF page locations not checked", warnings[0])
        self.assertNotIn("PDF page bounds", warnings[0])
        passage = prepared["items"][0]["source_passages"][0]
        self.assertEqual(passage["verification"]["method"], "line_range")
        self.assertEqual(passage["verification"]["status"], "unverified")
        self.assertIn("PDF page", passage["verification"]["note"])

    def test_label_only_warning_does_not_claim_a_line_check(self):
        for item in self.seed["items"]:
            item["source"] = {"label": item["label"]}
        self.seed["uses"][0]["source"] = {"label": "Proof of Theorem 1"}
        migrated = self.compare_all(self.migrate())
        warning, = records.prepare_records(migrated, self.base)["warnings"][:1]
        self.assertIn("No mechanical source locator checks are recorded", warning)
        self.assertIn("3 anchors still have locator details", warning)
        self.assertIn("3 entered/printed labels not mechanically matched", warning)

    def test_pdf_warning_counts_successful_page_checks_despite_unverified_labels(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("Shared pypdf is needed for physical PDF page checks.")
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(self.base / "paper.pdf")
        self.seed["source"]["file"] = "paper.pdf"
        self.seed["items"][0]["source"] = {"page": 1, "label": "Assumption 1"}
        self.seed["items"][1]["source"] = {"page": 1}
        self.seed["uses"][0]["source"] = {"page": 1}
        data = self.compare_all(self.migrate())
        original = deepcopy(data)
        prepared = records.prepare_records(data, self.base)
        warning, = prepared["warnings"]
        self.assertIn("Source locator checks: 3 PDF page bounds", warning)
        self.assertIn("1 anchors still have locator details", warning)
        self.assertIn("1 entered/printed labels not mechanically matched", warning)
        self.assertNotIn("text line ranges", warning)
        self.assertNotIn("PDF page locations not checked", warning)
        for row in prepared["items"] + prepared["uses"]:
            for passage in row["source_passages"]:
                self.assertEqual(passage["source_media_type"], "application/pdf")
        self.assertEqual(data, original)
        self.assertEqual(records.comparison_status(data)["status"], "complete")

    def test_projection_distinguishes_text_and_unregistered_passages(self):
        prepared = records.prepare_records(self.migrate(), self.base)
        for row in prepared["items"] + prepared["uses"]:
            self.assertTrue(row["source_passages"])
            self.assertTrue(all(p["source_media_type"] == "text/plain" for p in row["source_passages"]))
        self.seed["source"].pop("file")
        for item in self.seed["items"]:
            item["source"] = {"label": item["label"]}
        self.seed["uses"] = []
        prepared = records.prepare_records(self.migrate(), self.base)
        for row in prepared["items"]:
            self.assertIsNone(row["source_passages"][0]["source_media_type"])

    def test_literal_tex_context_is_snapshotted_once_with_exact_bytes(self):
        migrated = self.migrate()
        files = migrated["source_revision"]["files"]
        by_name = {Path(row["path"]).name: row for row in files}
        self.assertEqual(set(by_name), {"main.tex", "macros.tex", "appendix.tex"})
        self.assertEqual(len(files), 3)
        for source in (self.main, self.macros, self.appendix):
            row = by_name[source.name]
            content = base64.b64decode(row["content_base64"])
            self.assertEqual(content, source.read_bytes())
            self.assertEqual(hashlib.sha256(content).hexdigest(), row["sha256"])

    def test_nested_tex_inputs_can_be_relative_to_the_compilation_root(self):
        sections = self.base / "sections"
        sections.mkdir()
        (sections / "part.tex").write_text("\\input{sections/result}\n", encoding="utf-8")
        (sections / "result.tex").write_text("Nested source context.\n", encoding="utf-8")
        self.main.write_text(self.main.read_text(encoding="utf-8") + "\\input{sections/part}\n",
                             encoding="utf-8")
        migrated = self.migrate()
        paths = {row["path"] for row in migrated["source_revision"]["files"]}
        self.assertTrue({"sections/part.tex", "sections/result.tex", "macros.tex"}.issubset(paths))
        self.assertEqual(migrated["inventory"]["unresolved"], [])

    def test_live_source_edit_does_not_rewrite_the_saved_evidence(self):
        migrated = self.migrate()
        before = deepcopy(migrated)
        self.main.write_text(self.main.read_text(encoding="utf-8").replace(
            "For deterministic centers", "For random data-dependent centers"), encoding="utf-8")
        prepared = records.prepare_records(migrated, self.base)
        self.assertEqual(migrated, before)
        self.assertEqual(prepared["build_context"]["source_status"], "historical_changed")
        displayed = json.dumps(prepared["items"][1], ensure_ascii=False)
        self.assertIn("For deterministic centers", displayed)
        self.assertNotIn("For random data-dependent centers", displayed)

    def test_retained_sources_can_be_read_when_live_files_are_unavailable(self):
        migrated = self.migrate()
        detached = self.base / "detached"
        detached.mkdir()
        prepared = records.prepare_records(migrated, detached)
        self.assertEqual(prepared["build_context"]["source_status"], "historical_unavailable")
        self.assertIn("For deterministic centers", json.dumps(prepared["items"][1]))

    def test_two_aliases_and_two_proofs_still_describe_one_item(self):
        migrated = self.add_second_proof_passage(self.migrate())
        migrated["items"][1]["aliases"] = ["thm:center", "thm:main"]
        migrated = self.revalidate(migrated)
        prepared = records.prepare_records(migrated, self.base)
        self.assertEqual(len(migrated["items"]), 2)
        self.assertEqual(migrated["items"][1]["id"], "centering")
        self.assertEqual(migrated["items"][1]["aliases"], ["thm:center", "thm:main"])
        self.assertEqual(len(migrated["items"][1]["passages"]), 3)
        item = prepared["items"][1]
        self.assertEqual(len(item["source_passages"]), 3)
        self.assertIn("main.tex", json.dumps(item["source_passages"]))
        self.assertIn("appendix.tex", json.dumps(item["source_passages"]))
        self.assertIn("Alternative argument", json.dumps(item["source_passages"]))

    def test_distinct_parallel_uses_retain_their_ids_and_evidence(self):
        migrated = self.add_second_proof_passage(self.migrate())
        alternate = deepcopy(migrated["uses"][0])
        alternate.update(id="use-alternative-proof", type="proof_argument",
                         regime="Pilot regime", reason="Reuse only the absolute-error argument.",
                         evidence_refs=["anchor-alternative-proof"])
        migrated["uses"].append(alternate)
        migrated = self.revalidate(migrated)
        prepared = records.prepare_records(migrated, self.base)
        self.assertEqual(len(prepared["uses"]), 2)
        self.assertEqual({use["id"] for use in prepared["uses"]},
                         {use["id"] for use in migrated["uses"]})
        by_id = {use["id"]: use for use in prepared["uses"]}
        self.assertEqual(by_id["use-alternative-proof"]["regime"], "Pilot regime")
        self.assertEqual(by_id["use-alternative-proof"]["type"], "proof_argument")

    def test_cycle_is_preserved_with_an_explicit_graph_diagnostic(self):
        migrated = self.migrate()
        reverse = deepcopy(migrated["uses"][0])
        reverse.update(id="use-reverse-route", **{"from": "centering", "to": "independence"},
                       regime="Other route", reason="Only the other route uses the reverse implication.")
        migrated["uses"].append(reverse)
        migrated = self.revalidate(migrated)
        prepared = records.prepare_records(migrated, self.base)
        self.assertEqual(prepared["graph_mode"], "cyclic")
        self.assertEqual(set(prepared["graph_cycles"][0]["item_ids"]), {"independence", "centering"})
        self.assertEqual(len(prepared["items"]), 2)
        self.assertEqual({use["id"] for use in prepared["uses"]},
                         {use["id"] for use in migrated["uses"]})

    def test_review_history_does_not_change_the_content_snapshot(self):
        migrated = self.migrate()
        before = records.snapshot_digest(migrated)
        reviewed = self.compare_all(migrated)
        self.assertEqual(records.snapshot_digest(reviewed), before)
        self.assertEqual(reviewed["snapshot_id"], migrated["snapshot_id"])
        state = records.comparison_status(reviewed)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["matched"], 3)
        self.assertEqual(state["stale"], 0)

    def test_newer_reviews_of_other_inputs_do_not_invalidate_historical_comparisons(self):
        historical = self.compare_all(self.migrate())
        changed = deepcopy(historical)
        changed["uses"][0]["reason"] = "A revised argument requiring a new source comparison."
        changed = self.compare_all(self.revalidate(changed))
        historical["observations"] = deepcopy(changed["observations"])
        historical = records.validate_records(historical)
        self.assertEqual(records.comparison_status(historical)["matched"], 3)
        self.assertEqual(records.comparison_status(historical)["stale"], 0)

    def test_newer_attention_observation_for_the_same_input_supersedes_a_match(self):
        reviewed = self.compare_all(self.migrate())
        reviewed["observations"].extend(records.make_observations(
            reviewed, [{"collection": "items", "id": "centering"}],
            reviewer="Second source reviewer", result="needs_attention",
            note="The record needs a further source comparison despite the earlier match.",
        ))
        reviewed = records.validate_records(reviewed)
        state = records.comparison_status(reviewed)
        self.assertEqual(state["needs_attention"], 1)
        self.assertEqual(state["matched"], 2)
        self.assertEqual(state["status"], "incomplete")

    def test_changed_use_stales_its_review_and_its_target_comparison(self):
        reviewed = self.compare_all(self.migrate())
        before_ids = [row["id"] for row in reviewed["uses"]]
        reviewed["uses"][0]["reason"] = "A different argument using the same prerequisite."
        changed = self.revalidate(reviewed)
        state = records.comparison_status(changed)
        self.assertEqual([row["id"] for row in changed["uses"]], before_ids)
        self.assertEqual(state["stale"], 2)
        self.assertEqual(state["matched"], 1)

    def test_changed_prerequisite_invalidates_dependent_comparisons(self):
        reviewed = self.compare_all(self.migrate())
        reviewed["items"][0]["statement"]["text"] = "The observations may be dependent."
        changed = self.revalidate(reviewed)
        self.assertEqual(records.comparison_status(changed)["stale"], 3)

    def test_renumbering_preserves_the_mathematical_item_and_use_identities(self):
        migrated = self.migrate()
        before_items = [row["id"] for row in migrated["items"]]
        before_uses = [row["id"] for row in migrated["uses"]]
        migrated["items"][1]["label"] = "Theorem 3.2"
        migrated = self.revalidate(migrated)
        self.assertEqual([row["id"] for row in migrated["items"]], before_items)
        self.assertEqual([row["id"] for row in migrated["uses"]], before_uses)
        self.assertEqual(migrated["uses"][0]["to"], "centering")

    def test_source_context_change_stales_review_even_when_excerpt_is_unchanged(self):
        reviewed = self.compare_all(self.migrate())
        original_statement = deepcopy(reviewed["items"][1]["statement"])
        old_revision = reviewed["source_revision"]["id"]
        self.macros.write_text("\\newcommand{\\target}{\\theta}\n", encoding="utf-8")
        unchanged_snapshot = records.prepare_records(reviewed, self.base)
        self.assertEqual(unchanged_snapshot["build_context"]["source_status"], "historical_changed")
        self.assertEqual(unchanged_snapshot["build_context"]["source_comparison"]["matched"], 3)
        refreshed = records.refresh_sources(reviewed, self.base)
        self.assertNotEqual(refreshed["source_revision"]["id"], old_revision)
        self.assertEqual(refreshed["items"][1]["statement"], original_statement)
        self.assertEqual(len(refreshed["observations"]), len(reviewed["observations"]))
        self.assertEqual(records.comparison_status(refreshed)["stale"], 3)

    def test_supplied_content_digest_rejects_changed_records(self):
        migrated = self.migrate()
        migrated["items"][1]["statement"]["text"] = "This statement was changed without a new snapshot."
        with self.assertRaises(ValueError):
            records.validate_records(migrated)

    def test_tampered_source_bytes_and_anchor_excerpt_are_rejected(self):
        migrated = self.migrate()
        for field in ("source bytes", "anchor excerpt"):
            with self.subTest(field=field):
                changed = deepcopy(migrated)
                changed.pop("snapshot_id", None)
                if field == "source bytes":
                    changed["source_revision"]["files"][0]["content_base64"] = base64.b64encode(
                        b"different source content").decode("ascii")
                else:
                    changed["anchors"][0]["excerpt"] = "different evidence"
                with self.assertRaises(ValueError):
                    records.validate_records(changed)

    def test_malformed_enum_values_produce_record_errors_instead_of_tracebacks(self):
        for field in ("use type", "statement form", "passage role"):
            with self.subTest(field=field):
                changed = self.migrate()
                changed.pop("snapshot_id", None)
                if field == "use type":
                    changed["uses"][0]["type"] = ["dependency"]
                elif field == "statement form":
                    changed["items"][0]["statement"]["form"] = ["synopsis"]
                else:
                    changed["items"][0]["passages"][0]["role"] = ["statement"]
                with self.assertRaises(ValueError):
                    records.validate_records(changed)

    @unittest.skipUnless(NODE_AVAILABLE, "Checked HTML delivery requires shared Node.js.")
    def test_rendering_does_not_change_the_reviewed_input_identity(self):
        reviewed = self.compare_all(self.migrate())
        original = deepcopy(reviewed)
        output = self.base / "overview.html"
        receipt = overview.render_dataset(reviewed, self.base, output)
        self.assertEqual(reviewed, original)
        self.assertEqual(records.snapshot_digest(reviewed), original["snapshot_id"])
        self.assertEqual(records.comparison_status(reviewed)["matched"], 3)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["mathematical_assessment"], "not_performed")

    @unittest.skipUnless(NODE_AVAILABLE, "Checked HTML delivery requires shared Node.js.")
    def test_cycle_delivery_retains_the_graph_and_complete_index(self):
        migrated = self.migrate()
        reverse = deepcopy(migrated["uses"][0])
        reverse.update(id="use-reverse-route", **{"from": "centering", "to": "independence"},
                       regime="Other route", reason="The reverse implication belongs only to the other route.")
        migrated["uses"].append(reverse)
        migrated = self.revalidate(migrated)
        output = self.base / "overview.html"
        receipt = overview.render_dataset(migrated, self.base, output)
        elements = RecordElements(output.read_text(encoding="utf-8"))
        self.assertEqual(receipt["graph_mode"], "cyclic")
        self.assertEqual(receipt["geometry"]["status"], "pass")
        graph = overview._ArtifactReader()
        graph.feed(output.read_text(encoding="utf-8"))
        self.assertEqual(set(graph.nodes), {item["id"] for item in migrated["items"]})
        self.assertEqual(set(graph.uses), {(use["id"], use["from"], use["to"]) for use in migrated["uses"]})
        self.assertEqual(elements.items, {item["id"] for item in migrated["items"]})
        self.assertEqual(elements.uses, {use["id"] for use in migrated["uses"]})
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")

    @unittest.skipUnless(NODE_AVAILABLE, "Checked HTML delivery requires shared Node.js.")
    def test_long_scope_preview_retains_complete_reading_limits(self):
        data = self.migrate()
        data["scope"] = "Main statements and appendix. " + "Located coverage detail. " * 70 + "Unresolved numbering remains disclosed."
        data = self.revalidate(data)
        output = self.base / "long-scope.html"
        overview.render_dataset(data, self.base, output)
        html = output.read_text(encoding="utf-8")
        lead = re.search(r'<p class="proof-scope-lead">(.*?)</p>', html).group(1)
        self.assertLessEqual(len(lead.split()), 100)
        self.assertTrue(lead.endswith("…"))
        disclosure = re.search(r'<details class="proof-scope">([\s\S]*?)</details>', html).group(1)
        self.assertIn(data["scope"], disclosure)
        self.assertIn("Unresolved numbering remains disclosed.", disclosure)

    @unittest.skipUnless(NODE_AVAILABLE, "Checked HTML delivery requires shared Node.js.")
    def test_wrong_renderer_input_digest_preserves_the_previous_report(self):
        migrated = self.migrate()
        output = self.base / "overview.html"
        overview.render_dataset(migrated, self.base, output)
        previous = output.read_bytes()
        original_run = overview.subprocess.run

        def wrong_input_receipt(args, **kwargs):
            result = original_run(args, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            receipt["input_sha256"] = "0" * 64
            result.stdout = json.dumps(receipt)
            return result

        with patch.object(overview.subprocess, "run", side_effect=wrong_input_receipt):
            with self.assertRaises(ValueError):
                overview.render_dataset(migrated, self.base, output)
        self.assertEqual(output.read_bytes(), previous)

    @unittest.skipUnless(NODE_AVAILABLE, "Checked HTML delivery requires shared Node.js.")
    def test_actual_rendered_omission_fails_even_when_receipt_claims_complete(self):
        migrated = self.migrate()
        output = self.base / "overview.html"
        overview.render_dataset(migrated, self.base, output)
        previous = output.read_bytes()
        changed = deepcopy(migrated)
        changed["items"][1]["caption"] = "Revised caption for a new candidate"
        changed = self.revalidate(changed)
        original_run = overview.subprocess.run

        def missing_node_metadata(args, **kwargs):
            result = original_run(args, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = Path(args[-1])
            html = artifact.read_text(encoding="utf-8")
            html, count = re.subn(r'\bdata-node-id="independence"',
                                 'data-omitted-node-id="independence"', html, count=1)
            self.assertEqual(count, 1, "The probe must remove one actual rendered node identity.")
            artifact.write_text(html, encoding="utf-8")
            receipt = json.loads(result.stdout)
            receipt["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            receipt["bytes"] = artifact.stat().st_size
            result.stdout = json.dumps(receipt)
            return result

        with patch.object(overview.subprocess, "run", side_effect=missing_node_metadata):
            with self.assertRaises(ValueError):
                overview.render_dataset(changed, self.base, output)
        self.assertEqual(output.read_bytes(), previous)


class DeclaredKindAndProseTests(unittest.TestCase):
    """Condition-style headings classify as assumptions; prose premises record.

    Declaration scans propose candidates; an agent may also register an
    explicitly located prose assumption or model definition with a faithful
    descriptive label. Unfamiliar headings still produce an actionable note.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.main = self.base / "main.tex"
        self.main.write_text(
            "\\documentclass{article}\n"                     # 1
            "\\newtheorem{cond}{Condition}\n"                # 2
            "\\newtheorem{conds}{Conditions}\n"              # 3
            "\\newtheorem{principle}{Uniformity Principle}\n"  # 4
            "\\begin{document}\n"                            # 5
            "\\begin{cond}\\label{cond:mixing}\n"            # 6
            "The sequence is strongly mixing.\n"             # 7
            "\\end{cond}\n"                                  # 8
            "\\begin{conds}\\label{conds:moments}\n"         # 9
            "The fourth moments are finite.\n"               # 10
            "\\end{conds}\n"                                 # 11
            "\\begin{principle}\n"                           # 12
            "Every bound is uniform in the class.\n"         # 13
            "\\end{principle}\n"                             # 14
            "Throughout, the errors are sub-gaussian with proxy $\\sigma^2$.\n"  # 15
            "We write $\\hat\\theta_n$ for the penalized estimator.\n"           # 16
            "\\end{document}\n",                             # 17
            encoding="utf-8",
        )
        self.seed = {
            "schema_version": 3,
            "title": "Condition and prose premise fixture",
            "scope": "Synthetic declarations plus two prose premises.",
            "source": {"title": "Synthetic manuscript", "file": "main.tex"},
            "items": [
                {"id": "mixing", "kind": "assumption", "label": "Condition 1",
                 "caption": "Strong mixing",
                 "statement": {"text": "The sequence is strongly mixing.", "form": "synopsis"},
                 "source": {"label": "cond:mixing", "start_line": 6, "end_line": 8}},
                {"id": "moments", "kind": "assumption", "label": "Condition 2",
                 "caption": "Finite fourth moments",
                 "statement": {"text": "The fourth moments are finite.", "form": "synopsis"},
                 "source": {"label": "conds:moments", "start_line": 9, "end_line": 11}},
                {"id": "error-proxy", "kind": "assumption", "label": "Sub-gaussian errors",
                 "caption": "Global error proxy",
                 "statement": {"text": "The errors are sub-gaussian with proxy $\\sigma^2$.", "form": "synopsis"},
                 "source": {"start_line": 15, "end_line": 15}},
                {"id": "estimator", "kind": "definition", "label": "Penalized estimator",
                 "caption": "Estimator notation",
                 "statement": {"text": "We write $\\hat\\theta_n$ for the penalized estimator.", "form": "synopsis"},
                 "source": {"start_line": 16, "end_line": 16}},
            ],
            "uses": [],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def migrate(self):
        return records.normalize(self.seed, self.base)

    def test_condition_headings_normalize_to_assumption(self):
        declarations = self.migrate()["inventory"]["declarations"]
        by_lines = {(row["start_line"], row["end_line"]): row for row in declarations}
        self.assertEqual(by_lines[(6, 8)]["kind"], "assumption")
        self.assertEqual(by_lines[(6, 8)]["labels"], ["cond:mixing"])
        self.assertEqual(by_lines[(6, 8)]["item_ids"], ["mixing"])
        self.assertEqual(by_lines[(9, 11)]["kind"], "assumption")
        self.assertEqual(by_lines[(9, 11)]["item_ids"], ["moments"])
        self.assertNotIn((12, 14), by_lines)

    def test_unfamiliar_heading_still_produces_an_actionable_limitation(self):
        unresolved = self.migrate()["inventory"]["unresolved"]
        note = next((text for text in unresolved if "principle" in text), None)
        self.assertIsNotNone(note)
        self.assertIn("unfamiliar heading", note)
        self.assertIn("compare its declarations manually", note)

    def test_unused_unfamiliar_environment_does_not_create_a_warning(self):
        text = self.main.read_text(encoding="utf-8").replace(
            "\\begin{principle}\nEvery bound is uniform in the class.\n\\end{principle}",
            "% \\begin{principle}\n% Not used.\n% \\end{principle}")
        self.main.write_text(text, encoding="utf-8")
        self.assertFalse(any("principle" in note for note in self.migrate()["inventory"]["unresolved"]))

    def test_unfamiliar_environment_used_in_an_input_still_warns(self):
        text = self.main.read_text(encoding="utf-8").replace(
            "\\begin{principle}\nEvery bound is uniform in the class.\n\\end{principle}",
            "\\input{extra}\n\n")
        self.main.write_text(text, encoding="utf-8")
        (self.base / "extra.tex").write_text(
            r"\begin{principle}Every bound is uniform.\end{principle}", encoding="utf-8")
        self.assertTrue(any("principle" in note for note in self.migrate()["inventory"]["unresolved"]))

    def test_prose_premises_validate_with_descriptive_labels(self):
        migrated = self.migrate()
        prose = {row["id"]: row for row in migrated["items"] if row["id"] in ("error-proxy", "estimator")}
        self.assertEqual(prose["error-proxy"]["label"], "Sub-gaussian errors")
        self.assertEqual(prose["estimator"]["label"], "Penalized estimator")
        prepared = records.prepare_records(migrated, self.base)
        labels = {row["id"]: row["label"] for row in prepared["items"]}
        self.assertEqual(labels["error-proxy"], "Sub-gaussian errors")
        excerpts = {row["id"]: row["source_excerpt"] for row in prepared["items"]}
        self.assertIn("sub-gaussian", excerpts["error-proxy"])
        self.assertIn("penalized estimator", excerpts["estimator"])

    @unittest.skipUnless(NODE_AVAILABLE, "Checked HTML delivery requires shared Node.js.")
    def test_prose_premises_appear_in_the_html_with_source_passages(self):
        output = self.base / "overview.html"
        receipt = overview.render_dataset(self.migrate(), self.base, output)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        html = output.read_text(encoding="utf-8")
        elements = RecordElements(html)
        self.assertIn("error-proxy", elements.items)
        self.assertIn("estimator", elements.items)
        self.assertIn("Sub-gaussian errors", html)
        self.assertIn("Penalized estimator", html)
        self.assertIn("sub-gaussian with proxy", html)
        # Neither prose premise needed a fabricated declaration environment.
        self.assertNotIn("Uniformity Principle", html)


if __name__ == "__main__":
    unittest.main()

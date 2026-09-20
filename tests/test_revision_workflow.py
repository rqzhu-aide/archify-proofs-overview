"""Bounded manuscript-revision checks using an independent synthetic paper.

Mechanical similarity is only a reuse candidate. Source changes require an
explicit comparison of the change context before old observations are carried.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_database as database
import paper_records as records
import paper_revision as revision


def references(rows):
    return {(row["collection"], row["id"]) for row in rows}


def small_pdf(title):
    """Make one valid text page; only the metadata title varies between revisions."""
    stream = b"BT /F1 12 Tf 20 60 Td (A bounded synthetic statement.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Title (" + title.encode("ascii") + b") >>",
    ]
    result, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for number, content in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(str(number).encode() + b" 0 obj\n" + content + b"\nendobj\n")
    xref = len(result)
    result.extend(b"xref\n0 7\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(b"trailer\n<< /Size 7 /Root 1 0 R /Info 6 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(result)


class RevisionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.paper = self.base / "paper"
        self.output = self.paper / "proof-overview"
        self.work = self.output / "work"
        self.work.mkdir(parents=True)
        self.main = self.paper / "main.tex"
        self.proofs = self.paper / "proofs.tex"
        self.macros = self.paper / "macros.tex"
        self.main.write_text(
            "\\input{macros}\n"
            "A shrot note introduces the paper.\n"
            "\\begin{assumption}\\label{ass:sample}\n"
            "The observations are independent with variance at most one.\n"
            "\\end{assumption}\n"
            "\\begin{definition}\\label{def:mean}\n"
            "Write $\\mn=n^{-1}\\sum_i X_i$.\n"
            "\\end{definition}\n"
            "\\begin{lemma}\\label{lem:variance}\n"
            "The variance of $\\mn$ is at most $1/n$.\n"
            "\\end{lemma}\n"
            "\\begin{theorem}\\label{thm:tail}\n"
            "For $t>0$, $P(|\\mn-E\\mn|>t)\\leq 1/(nt^2)$.\n"
            "\\end{theorem}\n"
            "\\begin{proposition}\\label{prop:symmetry}\n"
            "A symmetric integrable random variable has mean zero.\n"
            "\\end{proposition}\n"
            "\\input{proofs}\n",
            encoding="utf-8",
        )
        self.proofs.write_text(
            "\\begin{proof}[Independence calculation]\n"
            "Independence removes each off-diagonal covariance.\n"
            "\\end{proof}\n"
            "\\begin{proof}[Mean calculation]\n"
            "The definition of $\\mn$ contributes the factor $n^{-2}$.\n"
            "\\end{proof}\n"
            "\\begin{proof}[Tail argument]\n"
            "Apply Chebyshev's inequality using Lemma 1.\n"
            "\\end{proof}\n",
            encoding="utf-8",
        )
        self.macros.write_text("\\newcommand{\\mn}{\\bar X_n}\n", encoding="utf-8")
        declarations = [
            ("sample", "assumption", "Assumption 1", "Sampling conditions", 3, "ass:sample"),
            ("mean", "definition", "Definition 1", "Sample mean", 6, "def:mean"),
            ("variance", "lemma", "Lemma 1", "Variance bound", 9, "lem:variance"),
            ("tail", "theorem", "Theorem 1", "Tail bound", 12, "thm:tail"),
            ("symmetry", "proposition", "Proposition 1", "Symmetric variable", 15, "prop:symmetry"),
        ]
        lines = self.main.read_text(encoding="utf-8").splitlines()
        seed = {
            "schema_version": 3,
            "title": "Small synthetic revision fixture",
            "scope": "Selected declarations and three written uses.",
            "source": {"title": "Synthetic manuscript", "file": "../../main.tex"},
            "main_items": ["tail", "symmetry"],
            "items": [
                {"id": identifier, "kind": kind, "label": label, "caption": caption,
                 "statement": {"text": lines[start], "form": "synopsis"},
                 "source": {"label": alias, "start_line": start, "end_line": start + 2}}
                for identifier, kind, label, caption, start, alias in declarations
            ],
            "uses": [
                {"from": source, "to": target, "reason": reason, "type": use_type,
                 "source": {"file": "../../proofs.tex", "start_line": start, "end_line": start + 2}}
                for source, target, reason, use_type, start in [
                    ("sample", "variance", "Independence removes cross covariances.", "dependency", 1),
                    ("mean", "variance", "The sample mean fixes the scaling.", "definition", 4),
                    ("variance", "tail", "Chebyshev uses the variance bound.", "dependency", 7),
                ]
            ],
        }
        self.seed = self.work / "seed.json"
        self.seed.write_text(json.dumps(seed), encoding="utf-8")
        self.db = self.output / "paper-records.sqlite"
        database.init_database(self.db, self.seed, source_root=self.paper)
        initial = database.export_snapshot(self.db)
        self.targets = [{"collection": collection, "id": row["id"]}
                        for collection in ("items", "uses") for row in initial[collection]]
        database.compare_records(self.db, {
            "expected_snapshot": initial["snapshot_id"], "targets": self.targets,
            "reviewer": "Fixture source reviewer", "result": "matched",
            "note": "Compared the synthetic statements, cited passages, and source context.",
        })
        self.before = database.export_snapshot(self.db)
        self.all_targets = references(self.targets)

    def tearDown(self):
        self.tmp.cleanup()

    def replace(self, path, old, new):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def refresh(self, **kwargs):
        current = database.export_snapshot(self.db)
        database.refresh_database(self.db, current["snapshot_id"], **kwargs)
        return database.export_snapshot(self.db)

    def changes(self, after):
        result = database.changes_database(self.db, self.before["snapshot_id"])
        self.assertEqual(result["from_snapshot"], self.before["snapshot_id"])
        self.assertEqual(result["to_snapshot"], after["snapshot_id"])
        direct = revision.build_changes(self.before, after)
        self.assertEqual({key: result[key] for key in direct}, direct)
        return result

    def reuse(self, after, targets=None, **overrides):
        batch = {
            "expected_snapshot": after["snapshot_id"],
            "reuse_from": self.before["snapshot_id"], "changes_reviewed": True,
            "targets": self.targets if targets is None else targets,
            "reviewer": "Fixture revision reviewer", "result": "matched",
            "note": "Compared the source diff and its interpretation context; these unchanged targets remain aligned.",
        }
        batch.update(overrides)
        return database.compare_records(self.db, batch)

    def use(self, source, target):
        return next(row for row in self.before["uses"] if row["from"] == source and row["to"] == target)

    def assert_no_review_write(self, function):
        original = database.export_snapshot(self.db)
        with self.assertRaises(ValueError):
            function()
        self.assertEqual(database.export_snapshot(self.db), original)

    def compare_new_baseline(self):
        data = database.export_snapshot(self.db)
        self.targets = [{"collection": collection, "id": row["id"]}
                        for collection in ("items", "uses") for row in data[collection]]
        self.all_targets = references(self.targets)
        database.compare_records(self.db, {
            "expected_snapshot": data["snapshot_id"], "targets": self.targets,
            "reviewer": "Fixture source reviewer", "result": "matched",
            "note": "Compared the extended synthetic source records.",
        })
        self.before = database.export_snapshot(self.db)

    def test_nested_output_registers_portable_paths_without_copied_tools(self):
        files = self.before["source_revision"]["files"]
        self.assertEqual({row["path"] for row in files}, {"main.tex", "proofs.tex", "macros.tex"})
        self.assertTrue(all(not Path(row["path"]).is_absolute() for row in files))
        self.assertEqual(database.validate_database(self.db)["source_status"], "current")
        self.assertFalse((self.output / "scripts").exists())
        self.assertFalse((self.output / "exports").exists())

    def test_prose_typo_reuse_is_explicit_and_preserves_old_comparisons(self):
        self.replace(self.main, "A shrot note", "A short note")
        after = self.refresh()
        report = self.changes(after)
        self.assertEqual(records.comparison_status(after)["stale"], len(self.targets))
        self.assertEqual(references(report["reuse_candidates"]), self.all_targets)
        self.assertEqual(references(report["changed_targets"]), set())
        self.assertTrue(any("shrot" in row["diff"] and "short" in row["diff"] for row in report["source_changes"]))
        self.assert_no_review_write(lambda: self.reuse(after, changes_reviewed=False))
        self.assert_no_review_write(lambda: self.reuse(after, note=""))
        response = self.reuse(after)
        self.assertEqual(response["recorded"], len(self.targets))
        old_ids = {row["id"] for row in self.before["observations"]}
        self.assertTrue(all(row.get("carried_from") in old_ids for row in response["observations"]))
        self.assertEqual(records.comparison_status(database.export_snapshot(self.db))["status"], "complete")
        retained = database.export_snapshot(self.db, self.before["snapshot_id"])
        self.assertEqual(retained["source_revision"], self.before["source_revision"])
        self.assertEqual(records.comparison_status(retained)["status"], "complete")
        self.assertTrue(old_ids <= {row["id"] for row in retained["observations"]})

    def test_global_macro_edit_is_exposed_and_never_automatically_reviewed(self):
        self.replace(self.macros, r"\bar X_n", r"\max_i X_i")
        after = self.refresh()
        report = self.changes(after)
        self.assertEqual(records.comparison_status(after)["stale"], len(self.targets))
        macro_changes = [row for row in report["source_changes"] if row["path"] == "macros.tex"]
        self.assertEqual(len(macro_changes), 1)
        self.assertIn(r"\max_i X_i", macro_changes[0]["diff"])
        self.assertTrue(report["limitations"])
        self.assertEqual(after["observations"], self.before["observations"])
        self.assert_no_review_write(lambda: self.reuse(after, changes_reviewed=False))

    def test_hypothesis_change_excludes_its_downstream_branch_from_reuse(self):
        self.replace(self.main, "variance at most one", "variance at most two")
        after = self.refresh()
        report = self.changes(after)
        self.assertIn(("items", "sample"), references(report["changed_targets"]))
        affected = references(report["changed_targets"]) | references(report["potentially_affected"])
        self.assertTrue({("items", "variance"), ("items", "tail")} <= affected)
        eligible = references(report["reuse_candidates"])
        self.assertTrue({("items", "mean"), ("items", "symmetry")} <= eligible)
        self.assertTrue({("items", "sample"), ("items", "variance"), ("items", "tail")}.isdisjoint(eligible))
        self.assert_no_review_write(lambda: self.reuse(after, [{"collection": "items", "id": "tail"}]))

    def test_changed_use_evidence_excludes_consumer_even_if_statement_is_unchanged(self):
        self.replace(self.proofs, "Apply Chebyshev's inequality using Lemma 1.",
                     "Apply Chebyshev's inequality after adding an unproved variance estimate.")
        after = self.refresh()
        report = self.changes(after)
        tail_use = self.use("variance", "tail")
        affected = references(report["changed_targets"]) | references(report["potentially_affected"])
        self.assertIn(("uses", tail_use["id"]), references(report["changed_targets"]))
        self.assertIn(("items", "tail"), affected)
        self.assertNotIn(("items", "tail"), references(report["reuse_candidates"]))
        before_tail = next(row for row in self.before["items"] if row["id"] == "tail")
        after_tail = next(row for row in after["items"] if row["id"] == "tail")
        self.assertEqual(before_tail["statement"], after_tail["statement"])
        self.assert_no_review_write(lambda: self.reuse(after, [{"collection": "uses", "id": tail_use["id"]}]))

    def test_changed_dependency_annotation_excludes_consumer_without_source_changes(self):
        use = deepcopy(self.use("variance", "tail"))
        use["reason"] = "The recorded argument instead needs a conditional variance bound."
        database.apply_edits(self.db, {
            "expected_snapshot": self.before["snapshot_id"],
            "edits": [{"collection": "uses", "op": "upsert", "id": use["id"], "record": use}],
        })
        after = database.export_snapshot(self.db)
        report = self.changes(after)
        self.assertFalse(report["source_changes"])
        self.assertIn(("uses", use["id"]), references(report["changed_targets"]))
        self.assertNotIn(("items", "tail"), references(report["reuse_candidates"]))
        self.assertIn(("items", "symmetry"), references(report["reuse_candidates"]))

    def test_unique_line_shift_relocates_passages_without_changing_record_identity(self):
        self.main.write_text("% Editorial preface added.\n" + self.main.read_text(encoding="utf-8"), encoding="utf-8")
        after = self.refresh(relocate_exact=True)
        report = self.changes(after)
        self.assertEqual(references(report["reuse_candidates"]), self.all_targets)
        old_anchors = {row["id"]: row for row in self.before["anchors"]}
        new_anchors = {row["id"]: row for row in after["anchors"]}
        self.assertEqual(old_anchors.keys(), new_anchors.keys())
        main_id = next(row["id"] for row in self.before["source_revision"]["files"] if row["path"] == "main.tex")
        for identifier, old in old_anchors.items():
            current = new_anchors[identifier]
            self.assertEqual(current["excerpt"], old["excerpt"])
            self.assertEqual(current["file_id"], old["file_id"])
            if old["file_id"] == main_id:
                self.assertEqual(current["locator"]["start_line"], old["locator"]["start_line"] + 1)
        self.reuse(after)
        self.assertEqual(records.comparison_status(database.export_snapshot(self.db))["status"], "complete")
        self.assertEqual(database.export_snapshot(self.db, self.before["snapshot_id"])["anchors"], self.before["anchors"])

    def test_ambiguous_repeated_excerpt_fails_without_guessing_or_publishing(self):
        original = self.proofs.read_text(encoding="utf-8")
        self.proofs.write_text("% Inserted preface.\n" + original + original, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "several|ambiguous|multiple"):
            self.refresh(relocate_exact=True)
        self.assertEqual(database.export_snapshot(self.db), self.before)

    def test_text_page_correction_allows_reviewed_reuse_without_downstream_impact(self):
        anchor = next(row for row in self.before["anchors"] if row["id"] == "anchor-item-sample")
        self.refresh(anchor_locations={anchor["id"]: {"locator": {**anchor["locator"], "page": 5}}})
        self.compare_new_baseline()
        after = self.refresh(anchor_locations={anchor["id"]: {"locator": {**anchor["locator"], "page": 4}}})
        report = self.changes(after)
        self.assertEqual(after["source_revision"], self.before["source_revision"])
        self.assertFalse(report["source_changes"])
        self.assertFalse(report["changed_targets"])
        self.assertFalse(report["potentially_affected"])
        self.assertEqual(references(report["reuse_candidates"]), self.all_targets)
        change, = report["anchor_changes"]
        self.assertEqual(change["status"], "moved")
        self.assertEqual(change["before_locator"]["page"], 5)
        self.assertEqual(change["after_locator"]["page"], 4)
        self.assertIn("check the corrected PDF page", change["note"])
        self.assertGreater(records.comparison_status(after)["stale"], 0)
        self.assert_no_review_write(lambda: self.reuse(after, changes_reviewed=False))
        self.reuse(after, note="Checked the corrected page; source text and mathematical context are unchanged.")
        self.assertEqual(records.comparison_status(database.export_snapshot(self.db))["status"], "complete")
        self.assertEqual(database.export_snapshot(self.db, self.before["snapshot_id"])["anchors"], self.before["anchors"])

    def test_page_correction_does_not_hide_a_changed_hypothesis(self):
        anchor = next(row for row in self.before["anchors"] if row["id"] == "anchor-item-sample")
        self.refresh(anchor_locations={anchor["id"]: {"locator": {**anchor["locator"], "page": 5}}})
        self.compare_new_baseline()
        self.replace(self.main, "variance at most one", "variance at most two")
        after = self.refresh(anchor_locations={anchor["id"]: {"locator": {**anchor["locator"], "page": 4}}})
        report = self.changes(after)
        self.assertIn(("items", "sample"), references(report["changed_targets"]))
        self.assertIn(("items", "tail"), references(report["potentially_affected"]))
        self.assertNotIn(("items", "tail"), references(report["reuse_candidates"]))
        self.assert_no_review_write(lambda: self.reuse(after, [{"collection": "items", "id": "tail"}]))

    def test_added_pdf_allows_reviewed_reuse_of_unchanged_text_records(self):
        pdf = self.paper / "main.pdf"
        pdf.write_bytes(small_pdf("Companion PDF"))
        after = self.refresh(extra_files=[pdf])
        report = self.changes(after)
        self.assertEqual(records.comparison_status(after)["stale"], len(self.targets))
        self.assertEqual(references(report["reuse_candidates"]), self.all_targets)
        self.assertFalse(report["changed_targets"])
        self.reuse(after, note="Checked the added PDF against the captured manuscript; the existing text evidence and context are unchanged.")
        self.assertEqual(records.comparison_status(database.export_snapshot(self.db))["status"], "complete")

    def test_missing_file_preserves_database_then_explicit_rename_preserves_ids(self):
        file_id = next(row["id"] for row in self.before["source_revision"]["files"] if row["path"] == "proofs.tex")
        renamed = self.paper / "appendix-proofs.tex"
        self.proofs.rename(renamed)
        self.replace(self.main, r"\input{proofs}", r"\input{appendix-proofs}")
        with self.assertRaisesRegex(ValueError, "file-map|renamed|mapping"):
            self.refresh()
        self.assertEqual(database.export_snapshot(self.db), self.before)
        after = self.refresh(file_map={file_id: "appendix-proofs.tex"})
        renamed_source = next(row for row in after["source_revision"]["files"] if row["path"] == "appendix-proofs.tex")
        self.assertEqual(renamed_source["id"], file_id)
        for collection in ("items", "uses", "anchors"):
            self.assertEqual({row["id"] for row in after[collection]}, {row["id"] for row in self.before[collection]})
        old_anchors = {row["id"]: row for row in self.before["anchors"]}
        for row in after["anchors"]:
            self.assertEqual(row["file_id"], old_anchors[row["id"]]["file_id"])
            self.assertEqual(row["excerpt"], old_anchors[row["id"]]["excerpt"])
        report = self.changes(after)
        self.assertTrue(any(row["status"] == "renamed" for row in report["source_changes"]))

    def test_whole_folder_move_reuses_registered_relative_paths(self):
        moved = self.base / "relocated-paper"
        moved.mkdir()
        for path in (self.main, self.proofs, self.macros):
            shutil.copy2(path, moved / path.name)
            path.unlink()
        after = self.refresh(source_root=moved)
        self.assertEqual(after["snapshot_id"], self.before["snapshot_id"])
        self.assertEqual(after["source_revision"], self.before["source_revision"])
        self.assertEqual(records.comparison_status(after)["status"], "complete")
        self.assertEqual(database.get_packet(self.db, "tail")["source_status"], "current")

    def test_previous_needs_attention_cannot_be_carried_as_matched(self):
        target = [{"collection": "items", "id": "tail"}]
        database.compare_records(self.db, {
            "expected_snapshot": self.before["snapshot_id"], "targets": target,
            "reviewer": "Second source reviewer", "result": "needs_attention",
            "note": "The recorded use needs another comparison.",
        })
        self.before = database.export_snapshot(self.db)
        self.replace(self.main, "A shrot note", "A short note")
        after = self.refresh()
        self.assert_no_review_write(lambda: self.reuse(after, target))
        historical = database.export_snapshot(self.db, self.before["snapshot_id"])
        self.assertEqual(records.comparison_status(historical)["needs_attention"], 1)

    def test_current_needs_attention_cannot_be_overridden_by_old_matched_reuse(self):
        self.replace(self.main, "A shrot note", "A short note")
        after = self.refresh()
        target = [{"collection": "items", "id": "tail"}]
        database.compare_records(self.db, {
            "expected_snapshot": after["snapshot_id"], "targets": target,
            "reviewer": "Revision source reviewer", "result": "needs_attention",
            "note": "A fresh source comparison found an unresolved dependency interpretation.",
        })
        self.assert_no_review_write(lambda: self.reuse(after, target))
        self.assertEqual(records.comparison_status(database.export_snapshot(self.db))["needs_attention"], 1)
        self.assertEqual(records.comparison_status(database.export_snapshot(self.db, self.before["snapshot_id"]))["status"], "complete")

    def test_label_only_evidence_needs_unchanged_file_content_and_locator(self):
        item = next(row for row in self.before["items"] if row["id"] == "symmetry")
        anchor_id = item["passages"][0]["anchor_id"]
        anchor = next(row for row in self.before["anchors"] if row["id"] == anchor_id)
        database.apply_edits(self.db, {
            "expected_snapshot": self.before["snapshot_id"], "edits": [{
                "collection": "anchors", "op": "upsert", "id": anchor_id,
                "record": {"file_id": anchor["file_id"], "locator": {"label": "prop:symmetry"}},
            }],
        })
        self.compare_new_baseline()
        self.assertEqual(next(row for row in self.before["anchors"] if row["id"] == anchor_id)["excerpt"], "")
        self.replace(self.macros, r"\bar X_n", r"\bar Y_n")
        after = self.refresh()
        self.assertIn(("items", "symmetry"), references(self.changes(after)["reuse_candidates"]))
        self.replace(self.main, "A shrot note", "A short note")
        after = self.refresh()
        self.assertNotIn(("items", "symmetry"), references(self.changes(after)["reuse_candidates"]))
        self.assert_no_review_write(lambda: self.reuse(after, [{"collection": "items", "id": "symmetry"}]))

    def test_changed_pdf_bytes_are_not_reused_when_the_extracted_page_repeats(self):
        pdf = self.paper / "supplement.pdf"
        pdf.write_bytes(small_pdf("First captured version"))
        current = self.refresh(extra_files=[pdf])
        file_id = next(row["id"] for row in current["source_revision"]["files"] if row["path"] == "supplement.pdf")
        database.apply_edits(self.db, {
            "expected_snapshot": current["snapshot_id"], "edits": [
                {"collection": "anchors", "op": "upsert", "id": "anchor-pdf",
                 "record": {"file_id": file_id, "locator": {"page": 1}}},
                {"collection": "items", "op": "upsert", "id": "pdf-result", "record": {
                    "id": "pdf-result", "kind": "lemma", "label": "Lemma 2", "caption": "Supplement result",
                    "statement": {"text": "A bounded synthetic statement.", "form": "synopsis"},
                    "passages": [{"role": "statement", "anchor_id": "anchor-pdf"}],
                }},
            ],
        })
        self.compare_new_baseline()
        old = next(row for row in self.before["anchors"] if row["id"] == "anchor-pdf")
        if not old["excerpt"]:
            self.skipTest("Shared pypdf is unavailable; the repeated extracted-text boundary needs PDF extraction.")
        pdf.write_bytes(small_pdf("Second captured version"))
        after = self.refresh()
        new = next(row for row in after["anchors"] if row["id"] == "anchor-pdf")
        self.assertEqual(new["excerpt"], old["excerpt"])
        self.assertNotEqual(after["source_revision"]["id"], self.before["source_revision"]["id"])
        report = self.changes(after)
        self.assertNotIn(("items", "pdf-result"), references(report["reuse_candidates"]))
        self.assertTrue(any(row["path"] == "supplement.pdf" and row["diff"] is None for row in report["source_changes"]))
        self.assert_no_review_write(lambda: self.reuse(after, [{"collection": "items", "id": "pdf-result"}]))

    def test_stale_prior_match_does_not_become_a_reuse_candidate(self):
        self.replace(self.main, "A shrot note", "A short note")
        self.before = self.refresh()
        self.assertEqual(records.comparison_status(self.before)["stale"], len(self.targets))
        self.replace(self.main, "A short note", "A short introductory note")
        after = self.refresh()
        self.assertFalse(self.changes(after)["reuse_candidates"])
        self.assert_no_review_write(lambda: self.reuse(after))

    def test_pdf_page_change_is_not_reused_even_when_page_text_is_identical(self):
        try:
            import io
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            self.skipTest("Shared pypdf is required to compare identical extracted PDF pages.")
        pdf = self.paper / "supplement.pdf"
        page = PdfReader(io.BytesIO(small_pdf("Repeated page"))).pages[0]
        writer = PdfWriter()
        writer.add_page(page)
        writer.add_page(page)
        writer.write(pdf)
        current = self.refresh(extra_files=[pdf])
        file_id = next(row["id"] for row in current["source_revision"]["files"] if row["path"] == "supplement.pdf")
        anchor_id = "anchor-item-symmetry"
        self.refresh(anchor_locations={anchor_id: {"file_id": file_id, "locator": {"page": 1}}})
        self.compare_new_baseline()
        after = self.refresh(anchor_locations={anchor_id: {"locator": {"page": 2}}})
        old = next(row for row in self.before["anchors"] if row["id"] == anchor_id)
        new = next(row for row in after["anchors"] if row["id"] == anchor_id)
        self.assertTrue(old["excerpt"])
        self.assertEqual(new["excerpt"], old["excerpt"])
        self.assertEqual(after["source_revision"], self.before["source_revision"])
        report = self.changes(after)
        self.assertIn(("items", "symmetry"), references(report["changed_targets"]))
        self.assertNotIn(("items", "symmetry"), references(report["reuse_candidates"]))
        self.assert_no_review_write(lambda: self.reuse(after, [{"collection": "items", "id": "symmetry"}]))


class IntermediateRevisionTests(unittest.TestCase):
    """A reviewed owner whose intermediate steps carry their own uses.

    Record-only edits isolate the owner review context: adding, removing,
    changing, or retargeting a use entering a step, and reparenting or
    deleting a step, must mark the owner without touching unrelated branches.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.main = self.base / "main.tex"
        self.main.write_text(
            "\\documentclass{article}\n"                # 1
            "\\begin{document}\n"                       # 2
            "\\begin{assumption}\\label{ass:base}\n"    # 3
            "The base premise holds.\n"                 # 4
            "\\end{assumption}\n"                       # 5
            "\\begin{theorem}\\label{thm:main}\n"       # 6
            "The main conclusion follows.\n"            # 7
            "\\end{theorem}\n"                          # 8
            "\\begin{proof}\n"                          # 9
            "The key step applies the premise.\n"       # 10
            "\\end{proof}\n"                            # 11
            "\\begin{lemma}\\label{lem:helper}\n"       # 12
            "The helper bound holds.\n"                 # 13
            "\\end{lemma}\n"                            # 14
            "\\begin{proof}\n"                          # 15
            "The helper step estimates the tail.\n"     # 16
            "\\end{proof}\n"                            # 17
            "\\begin{corollary}\\label{cor:side}\n"     # 18
            "A side remark stands alone.\n"             # 19
            "\\end{corollary}\n"                        # 20
            "\\end{document}\n",                        # 21
            encoding="utf-8",
        )
        seed = {
            "schema_version": 3, "title": "Intermediate revision fixture",
            "scope": "Reviewed owner with intermediate steps and a disconnected branch.",
            "source": {"title": "Synthetic manuscript", "file": "main.tex"},
            "items": [
                {"id": "premise", "kind": "assumption", "label": "Assumption 1", "caption": "Base premise",
                 "statement": {"text": "The base premise holds.", "form": "synopsis"},
                 "source": {"label": "ass:base", "start_line": 3, "end_line": 5}},
                {"id": "owner", "kind": "theorem", "label": "Theorem 1", "caption": "Main conclusion",
                 "statement": {"text": "The main conclusion follows.", "form": "synopsis"},
                 "source": {"label": "thm:main", "start_line": 6, "end_line": 8}},
                {"id": "helper", "kind": "lemma", "label": "Lemma 1", "caption": "Helper bound",
                 "statement": {"text": "The helper bound holds.", "form": "synopsis"},
                 "source": {"label": "lem:helper", "start_line": 12, "end_line": 14}},
                {"id": "branch", "kind": "corollary", "label": "Corollary 1", "caption": "Side remark",
                 "statement": {"text": "A side remark stands alone.", "form": "synopsis"},
                 "source": {"label": "cor:side", "start_line": 18, "end_line": 20}},
            ],
            "uses": [],
        }
        dataset = self.base / "overview.json"
        dataset.write_text(json.dumps(seed), encoding="utf-8")
        self.db = self.base / "paper.sqlite"
        database.init_database(self.db, dataset)
        data = database.export_snapshot(self.db)
        file_id = data["source_revision"]["files"][0]["id"]
        database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "anchors", "op": "upsert", "id": "anchor-step",
             "record": {"file_id": file_id, "locator": {"start_line": 10, "end_line": 10}}},
            {"collection": "anchors", "op": "upsert", "id": "anchor-step-use",
             "record": {"file_id": file_id, "locator": {"start_line": 10, "end_line": 10}}},
            {"collection": "anchors", "op": "upsert", "id": "anchor-helper-step",
             "record": {"file_id": file_id, "locator": {"start_line": 16, "end_line": 16}}},
            {"collection": "items", "op": "upsert", "id": "step",
             "record": {"kind": "claim", "label": "Key step", "caption": "Premise application",
                        "statement": {"text": "The key step applies the premise.", "form": "synopsis"},
                        "owner": "owner",
                        "passages": [{"role": "evidence", "anchor_id": "anchor-step"}]}},
            {"collection": "items", "op": "upsert", "id": "helper-step",
             "record": {"kind": "claim", "label": "Helper step", "caption": "Tail estimate",
                        "statement": {"text": "The helper step estimates the tail.", "form": "synopsis"},
                        "owner": "helper",
                        "passages": [{"role": "evidence", "anchor_id": "anchor-helper-step"}]}},
            {"collection": "uses", "op": "upsert", "id": "use-premise-owner",
             "record": {"from": "premise", "to": "owner",
                        "reason": "The premise enters the main argument directly."}},
            {"collection": "uses", "op": "upsert", "id": "use-premise-step",
             "record": {"from": "premise", "to": "step",
                        "reason": "The premise controls the key step.",
                        "evidence_refs": ["anchor-step-use"]}},
        ]})
        data = database.export_snapshot(self.db)
        targets = [{"collection": collection, "id": row["id"]}
                   for collection in ("items", "uses") for row in data[collection]]
        database.compare_records(self.db, {
            "expected_snapshot": data["snapshot_id"], "targets": targets,
            "reviewer": "Fixture source reviewer", "result": "matched",
            "note": "Compared the synthetic statements, step uses, and source context.",
        })
        self.before = database.export_snapshot(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def data(self):
        return database.export_snapshot(self.db)

    def apply(self, edits):
        return database.apply_edits(self.db, {"expected_snapshot": self.data()["snapshot_id"], "edits": edits})

    def report(self):
        return database.changes_database(self.db, self.before["snapshot_id"])

    def references(self, rows):
        return {(row["collection"], row["id"]) for row in rows}

    def candidates(self, report):
        return self.references(report["reuse_candidates"])

    def affected(self, report):
        return self.references(report["potentially_affected"])

    def edit_step_use(self, **changes):
        use = deepcopy(next(row for row in self.data()["uses"] if row["id"] == "use-premise-step"))
        use.update(changes)
        self.apply([{"collection": "uses", "op": "upsert", "id": use["id"], "record": use}])

    def test_changing_a_step_use_stales_and_reports_the_owner(self):
        self.edit_step_use(reason="The recorded step instead needs a conditional premise.")
        after = self.data()
        self.assertNotEqual(records.target_digest(after, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))
        # The step's own review context includes the edited use, so its
        # comparison is stale as well.
        self.assertNotEqual(records.target_digest(after, "items", "step"),
                            records.target_digest(self.before, "items", "step"))
        status = records.comparison_status(after)
        self.assertEqual(status["stale"], 3)
        self.assertEqual(status["matched"], 5)
        report = self.report()
        self.assertIn(("uses", "use-premise-step"), self.references(report["changed_targets"]))
        self.assertIn(("items", "owner"), self.affected(report))
        direct = revision.build_changes(self.before, after)
        self.assertEqual({key: report[key] for key in direct}, direct)
        candidates = self.candidates(report)
        for row in (("items", "premise"), ("items", "helper"), ("items", "branch")):
            self.assertIn(row, candidates)
        self.assertNotIn(("items", "owner"), candidates)
        self.assertNotIn(("uses", "use-premise-step"), candidates)
        # A fresh comparison returns the owner and its step to matched, history retained.
        database.compare_records(self.db, {
            "expected_snapshot": after["snapshot_id"],
            "targets": [{"collection": "items", "id": "owner"}, {"collection": "items", "id": "step"},
                        {"collection": "uses", "id": "use-premise-step"}],
            "reviewer": "Fixture revision reviewer", "result": "matched",
            "note": "Re-compared the edited step use and the full owner context with the source."})
        current = self.data()
        self.assertEqual(records.comparison_status(current)["status"], "complete")
        self.assertGreater(len(current["observations"]), len(self.before["observations"]))

    def test_adding_a_use_entering_a_step_marks_the_owner(self):
        self.apply([{"collection": "uses", "op": "upsert", "id": "use-premise-step-2",
                     "record": {"from": "premise", "to": "step",
                                "reason": "A second contribution to the same step."}}])
        after = self.data()
        self.assertNotEqual(records.target_digest(after, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))
        report = self.report()
        self.assertIn(("items", "owner"), self.affected(report))
        self.assertNotIn(("items", "owner"), self.candidates(report))
        self.assertIn(("items", "branch"), self.candidates(report))

    def test_removing_a_use_entering_a_step_marks_the_owner(self):
        self.apply([{"collection": "uses", "op": "remove", "id": "use-premise-step"}])
        after = self.data()
        self.assertNotEqual(records.target_digest(after, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))
        report = self.report()
        self.assertIn(("items", "owner"), self.affected(report))
        self.assertNotIn(("items", "owner"), self.candidates(report))
        self.assertIn(("items", "branch"), self.candidates(report))

    def test_step_use_field_changes_all_stale_the_owner(self):
        cases = [("reason", "A different step argument."), ("type", "proof_argument"),
                 ("regime", "Route B"), ("group", {"id": "route", "kind": "joint"}),
                 ("issue", "Does the step need an extra hypothesis?"), ("evidence_refs", [])]
        for field, value in cases:
            with self.subTest(field=field):
                edited = deepcopy(self.before)
                edited.pop("snapshot_id", None)
                use = next(row for row in edited["uses"] if row["id"] == "use-premise-step")
                use[field] = value
                edited = records.validate_records(edited)
                self.assertNotEqual(records.target_digest(edited, "items", "owner"),
                                    records.target_digest(self.before, "items", "owner"))
                report = revision.build_changes(self.before, edited)
                self.assertIn(("items", "owner"), self.references(report["potentially_affected"]))
                self.assertNotIn(("items", "owner"), self.references(report["reuse_candidates"]))

    def test_changing_a_step_use_prerequisite_statement_stales_the_owner(self):
        edited = deepcopy(self.before)
        edited.pop("snapshot_id", None)
        premise = next(row for row in edited["items"] if row["id"] == "premise")
        premise["statement"]["text"] = "The base premise holds with unit variance."
        edited = records.validate_records(edited)
        self.assertNotEqual(records.target_digest(edited, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))

    def test_changing_step_use_evidence_stales_the_owner(self):
        data = self.data()
        file_id = data["source_revision"]["files"][0]["id"]
        self.apply([{"collection": "anchors", "op": "upsert", "id": "anchor-step-use",
                     "record": {"file_id": file_id, "locator": {"start_line": 9, "end_line": 9}}}])
        after = self.data()
        self.assertNotEqual(records.target_digest(after, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))
        report = self.report()
        self.assertIn(("uses", "use-premise-step"), self.references(report["changed_targets"]))
        self.assertIn(("items", "owner"), self.affected(report))
        self.assertNotIn(("items", "owner"), self.candidates(report))

    def test_retargeting_a_step_use_marks_old_and_new_owners(self):
        self.edit_step_use(to="helper-step")
        after = self.data()
        self.assertNotEqual(records.target_digest(after, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))
        self.assertNotEqual(records.target_digest(after, "items", "helper"),
                            records.target_digest(self.before, "items", "helper"))
        report = self.report()
        affected = self.affected(report)
        self.assertIn(("items", "owner"), affected)
        self.assertIn(("items", "helper"), affected)
        candidates = self.candidates(report)
        self.assertNotIn(("items", "owner"), candidates)
        self.assertNotIn(("items", "helper"), candidates)
        self.assertIn(("items", "branch"), candidates)

    def test_reparenting_a_step_marks_old_and_new_owners(self):
        step = deepcopy(next(row for row in self.data()["items"] if row["id"] == "step"))
        step["owner"] = "helper"
        self.apply([{"collection": "items", "op": "upsert", "id": "step", "record": step}])
        after = self.data()
        self.assertNotEqual(records.target_digest(after, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))
        self.assertNotEqual(records.target_digest(after, "items", "helper"),
                            records.target_digest(self.before, "items", "helper"))
        report = self.report()
        affected = self.affected(report)
        self.assertIn(("items", "owner"), affected)
        self.assertIn(("items", "helper"), affected)
        candidates = self.candidates(report)
        self.assertNotIn(("items", "owner"), candidates)
        self.assertNotIn(("items", "helper"), candidates)
        self.assertIn(("items", "branch"), candidates)

    def test_deleting_a_step_marks_its_old_owner(self):
        self.apply([{"collection": "uses", "op": "remove", "id": "use-premise-step"},
                    {"collection": "items", "op": "remove", "id": "step"}])
        after = self.data()
        self.assertNotEqual(records.target_digest(after, "items", "owner"),
                            records.target_digest(self.before, "items", "owner"))
        report = self.report()
        self.assertIn(("items", "owner"), self.affected(report))
        self.assertNotIn(("items", "owner"), self.candidates(report))
        self.assertIn(("items", "branch"), self.candidates(report))

    def test_unrelated_record_edit_keeps_the_owner_current(self):
        branch = deepcopy(next(row for row in self.data()["items"] if row["id"] == "branch"))
        branch["caption"] = "A clarified side remark"
        self.apply([{"collection": "items", "op": "upsert", "id": "branch", "record": branch}])
        after = self.data()
        self.assertEqual(records.target_digest(after, "items", "owner"),
                         records.target_digest(self.before, "items", "owner"))
        self.assertEqual(records.fidelity_by_row(after)[("items", "owner")], "matched")
        report = self.report()
        self.assertIn(("items", "owner"), self.candidates(report))
        self.assertNotIn(("items", "owner"), self.affected(report))
        self.assertIn(("uses", "use-premise-step"), self.candidates(report))

    def test_owner_packet_distinguishes_direct_and_step_uses_with_evidence(self):
        packet = database.get_packet(self.db, "owner")
        self.assertEqual([use["id"] for use in packet["incoming_uses"]], ["use-premise-owner"])
        self.assertEqual([use["id"] for use in packet["owned_step_uses"]], ["use-premise-step"])
        self.assertEqual(packet["owned_step_uses"][0]["evidence_refs"], ["anchor-step-use"])
        self.assertEqual([row["id"] for row in packet["owned_items"]], ["step"])
        self.assertEqual([row["id"] for row in packet["prerequisite_items"]], ["premise"])
        anchors = {row["id"] for row in packet["anchors"]}
        self.assertIn("anchor-step", anchors)
        self.assertIn("anchor-step-use", anchors)
        digests = {(row["collection"], row["id"]) for row in packet["target_digests"]}
        self.assertIn(("items", "step"), digests)
        self.assertIn(("uses", "use-premise-step"), digests)
        self.assertIn(("uses", "use-premise-owner"), digests)
        observations = {(row["target"]["collection"], row["target"]["id"]) for row in packet["observations"]}
        self.assertIn(("uses", "use-premise-step"), observations)

    def test_packet_does_not_grow_with_unrelated_records(self):
        before_packet = database.get_packet(self.db, "owner")
        file_id = self.data()["source_revision"]["files"][0]["id"]
        self.apply([
            {"collection": "anchors", "op": "upsert", "id": "anchor-unrelated",
             "record": {"file_id": file_id, "locator": {"start_line": 19, "end_line": 19}}},
            {"collection": "items", "op": "upsert", "id": "unrelated",
             "record": {"kind": "lemma", "label": "Lemma 2", "caption": "Separate bound",
                        "statement": {"text": "A separate bound holds.", "form": "synopsis"},
                        "passages": [{"role": "statement", "anchor_id": "anchor-unrelated"}]}},
            {"collection": "uses", "op": "upsert", "id": "use-unrelated",
             "record": {"from": "branch", "to": "unrelated",
                        "reason": "The side remark supports the separate bound."}},
        ])
        after_packet = database.get_packet(self.db, "owner")
        for key in ("item", "owned_items", "incoming_uses", "owned_step_uses",
                    "prerequisite_items", "anchors", "target_digests", "observations"):
            self.assertEqual(after_packet[key], before_packet[key], key)


if __name__ == "__main__":
    unittest.main()

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
            "schema_version": 1,
            "title": "Small synthetic revision fixture",
            "scope": "Selected declarations and three written uses.",
            "source": {"title": "Synthetic manuscript", "file": "../../main.tex"},
            "main_items": ["tail", "symmetry"],
            "items": [
                {"id": identifier, "kind": kind, "label": label, "caption": caption,
                 "statement": lines[start],
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


if __name__ == "__main__":
    unittest.main()

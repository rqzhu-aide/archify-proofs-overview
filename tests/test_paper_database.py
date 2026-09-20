"""Behavioral tests for portable paper snapshots and atomic database edits."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_database as database
import paper_records as records


class PaperDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source = self.base / "paper.tex"
        self.source.write_text(
            "\\begin{assumption}\\label{ass:independence}\n"
            "The observations are independent.\n"
            "\\end{assumption}\n"
            "\\begin{theorem}\\label{thm:variance}\n"
            "The variance of the sum equals the sum of variances.\n"
            "\\end{theorem}\n"
            "\\begin{proof}Independence removes the covariance terms.\\end{proof}\n",
            encoding="utf-8",
        )
        self.dataset = self.base / "overview.json"
        self.seed = {
            "schema_version": 3, "title": "Variance structure", "scope": "A synthetic test fixture.",
            "source": {"title": "Synthetic variance argument", "file": "paper.tex"},
            "items": [
                {"id": "sampling", "kind": "assumption", "label": "Assumption 1", "caption": "Independent observations", "statement": {"text": "The observations are independent.", "form": "synopsis"}, "source": {"start_line": 1, "end_line": 3, "label": "ass:independence"}},
                {"id": "variance", "kind": "theorem", "label": "Theorem 1", "caption": "Variance of a sum", "statement": {"text": "The variance of the sum equals the sum of variances.", "form": "synopsis"}, "source": {"start_line": 4, "end_line": 6, "label": "thm:variance"}},
            ],
            "uses": [{"from": "sampling", "to": "variance", "reason": "Independence removes the covariance terms.", "source": {"start_line": 7, "end_line": 7}}],
        }
        self.dataset.write_text(json.dumps(self.seed), encoding="utf-8")
        self.db = self.base / "paper.sqlite"
        self.initial = database.init_database(self.db, self.dataset)

    def tearDown(self):
        self.temporary.cleanup()

    def update_caption(self, caption="A clarified caption"):
        data = database.export_snapshot(self.db)
        item = deepcopy(data["items"][1])
        item["caption"] = caption
        return {"expected_snapshot": data["snapshot_id"], "edits": [{"collection": "items", "op": "upsert", "id": item["id"], "record": item}]}

    def counts(self):
        connection = sqlite3.connect(self.db)
        try:
            return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("snapshots", "source_blobs", "observations", "builds")}
        finally:
            connection.close()

    def test_native_seed_capture_preserves_paper(self):
        original = self.dataset.read_bytes()
        data = database.export_snapshot(self.db)
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["snapshot_id"], self.initial["snapshot_id"])
        self.assertEqual([item["id"] for item in data["items"]], ["sampling", "variance"])
        self.assertEqual(data["items"][1]["statement"], self.seed["items"][1]["statement"])
        self.assertEqual(original, self.dataset.read_bytes())
        self.assertEqual(data["observations"], [])

    def test_existing_database_is_not_reinitialized(self):
        with self.assertRaisesRegex(database.DatabaseError, "already exists"):
            database.init_database(self.db, self.dataset)
        self.assertEqual(database.export_snapshot(self.db)["snapshot_id"], self.initial["snapshot_id"])

    def test_export_is_portable_without_live_source(self):
        self.source.unlink()
        data = database.export_snapshot(self.db)
        self.assertTrue(data["source_revision"]["files"][0]["content_base64"])
        second = self.base / "export.json"
        database._write_export(data, second, self.db)
        relocated = self.base / "relocated" / "paper.sqlite"
        result = database.init_database(relocated, second)
        self.assertEqual(result["snapshot_id"], self.initial["snapshot_id"])

    def test_packet_has_source_context_but_not_file_payloads(self):
        packet = database.get_packet(self.db, "variance")
        self.assertEqual(packet["expected_snapshot"], self.initial["snapshot_id"])
        self.assertEqual([item["id"] for item in packet["prerequisite_items"]], ["sampling"])
        self.assertEqual(len(packet["incoming_uses"]), 1)
        self.assertTrue(packet["anchors"])
        self.assertNotIn("content_base64", json.dumps(packet))
        self.assertEqual(len(packet["target_digests"]), 3)

    def test_packet_keeps_latest_comparisons_while_export_preserves_history(self):
        for number in range(1, 4):
            database.compare_records(self.db, {"expected_snapshot": self.initial["snapshot_id"],
                                              "targets": [{"collection": "items", "id": "variance"}],
                                              "reviewer": "test reviewer", "note": f"Comparison {number}."})
        packet = database.get_packet(self.db, "variance")
        self.assertEqual(len(packet["observations"]), 1)
        self.assertEqual(packet["observations"][0]["note"], "Comparison 3.")
        self.assertEqual(packet["observation_history_count"], 3)
        self.assertEqual(len(database.export_snapshot(self.db)["observations"]), 3)
        self.assertEqual(packet["source_status"], "current")
        self.source.write_text(self.source.read_text(encoding="utf-8").replace("independent", "dependent"), encoding="utf-8")
        self.assertEqual(database.get_packet(self.db, "variance")["source_status"], "historical_changed")

    def test_retained_snapshot_packet_selects_its_matching_comparison(self):
        original = self.initial["snapshot_id"]
        target = [{"collection": "items", "id": "variance"}]
        database.compare_records(self.db, {"expected_snapshot": original, "targets": target,
                                          "reviewer": "test reviewer", "note": "Compared the original statement."})
        updated = database.apply_edits(self.db, self.update_caption())["snapshot_id"]
        database.compare_records(self.db, {"expected_snapshot": updated, "targets": target,
                                          "reviewer": "test reviewer", "note": "Compared the revised record."})
        historical = database.get_packet(self.db, "variance", original)
        current = database.get_packet(self.db, "variance")
        self.assertEqual(historical["observations"][0]["note"], "Compared the original statement.")
        self.assertEqual(current["observations"][0]["note"], "Compared the revised record.")
        self.assertEqual(historical["observation_history_count"], 2)
        self.assertEqual(current["observation_history_count"], 2)

    def test_updates_preserve_old_snapshots_and_deduplicate_sources(self):
        before = database.export_snapshot(self.db)
        result = database.apply_edits(self.db, self.update_caption())
        self.assertNotEqual(result["snapshot_id"], before["snapshot_id"])
        self.assertEqual(database.export_snapshot(self.db, before["snapshot_id"]), before)
        self.assertEqual(database.export_snapshot(self.db)["items"][1]["caption"], "A clarified caption")
        self.assertEqual(self.counts()["snapshots"], 2)
        self.assertEqual(self.counts()["source_blobs"], len(before["source_revision"]["files"]))

    def test_stale_parallel_patch_does_not_overwrite_newer_records(self):
        first = self.update_caption("First writer")
        second = self.update_caption("Stale writer")
        prepared_second = database._edit_data(database.export_snapshot(self.db), second)
        database.apply_edits(self.db, first)
        with self.assertRaisesRegex(database.DatabaseError, "Stale"):
            database.apply_edits(self.db, second)
        with self.assertRaisesRegex(database.DatabaseError, "Stale"):
            database._publish(self.db, prepared_second, second["expected_snapshot"])
        self.assertEqual(database.export_snapshot(self.db)["items"][1]["caption"], "First writer")
        self.assertEqual(self.counts()["snapshots"], 2)

    def test_invalid_batch_leaves_every_record_unchanged(self):
        before = database.export_snapshot(self.db)
        batch = self.update_caption()
        batch["edits"].append({"collection": "items", "op": "remove", "id": "sampling"})
        with self.assertRaises(ValueError):
            database.apply_edits(self.db, batch)
        self.assertEqual(database.export_snapshot(self.db), before)
        self.assertEqual(self.counts()["snapshots"], 1)

    def test_interrupted_publication_rolls_back_inserted_snapshot(self):
        before = database.export_snapshot(self.db)
        batch = self.update_caption()
        with patch.object(database, "_store_observations", side_effect=RuntimeError("simulated interruption")):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                database.apply_edits(self.db, batch)
        self.assertEqual(database.export_snapshot(self.db), before)
        self.assertEqual(self.counts()["snapshots"], 1)

    def test_comparison_history_does_not_change_semantic_snapshot(self):
        before = database.export_snapshot(self.db)
        response = database.compare_records(self.db, {"expected_snapshot": before["snapshot_id"], "targets": [{"collection": "items", "id": "variance"}], "reviewer": "test reviewer", "result": "matched", "note": "Compared the statement with its source."})
        after = database.export_snapshot(self.db)
        self.assertEqual(response["recorded"], 1)
        self.assertEqual(after["snapshot_id"], before["snapshot_id"])
        self.assertEqual(len(after["observations"]), 1)
        self.assertEqual(self.counts()["snapshots"], 1)
        self.assertEqual(after["observations"][0]["input_snapshot"], records.target_digest(before, "items", "variance"))

    def test_prior_comparison_is_preserved_after_target_changes(self):
        data = database.export_snapshot(self.db)
        database.compare_records(self.db, {"expected_snapshot": data["snapshot_id"], "targets": [{"collection": "items", "id": "variance"}], "reviewer": "test reviewer"})
        old = database.export_snapshot(self.db)["observations"][0]
        database.apply_edits(self.db, self.update_caption())
        current = database.export_snapshot(self.db)
        self.assertEqual(current["observations"][0], old)
        self.assertNotEqual(old["input_snapshot"], records.target_digest(current, "items", "variance"))

    def test_stale_comparison_batch_is_rejected(self):
        original = self.initial["snapshot_id"]
        database.apply_edits(self.db, self.update_caption())
        with self.assertRaisesRegex(database.DatabaseError, "Stale comparison"):
            database.compare_records(self.db, {"expected_snapshot": original, "targets": [{"collection": "items", "id": "variance"}], "reviewer": "test reviewer"})
        self.assertEqual(self.counts()["observations"], 0)

    def test_refresh_preserves_historical_source_bytes(self):
        before = database.export_snapshot(self.db)
        self.source.write_text(self.source.read_text(encoding="utf-8").replace("independent", "uncorrelated"), encoding="utf-8")
        result = database.refresh_database(self.db, before["snapshot_id"])
        after = database.export_snapshot(self.db)
        self.assertNotEqual(result["snapshot_id"], before["snapshot_id"])
        self.assertNotEqual(after["source_revision"]["id"], before["source_revision"]["id"])
        self.assertEqual(database.export_snapshot(self.db, before["snapshot_id"])["source_revision"], before["source_revision"])

    def test_refresh_can_reanchor_a_truncated_source_without_losing_history(self):
        before = database.export_snapshot(self.db)
        self.source.write_text(
            "\\begin{assumption}\\label{ass:independence}The observations are independent.\\end{assumption}\n"
            "\\begin{theorem}\\label{thm:variance}The variance of the sum equals the sum of variances.\\end{theorem}\n"
            "\\begin{proof}Independence removes the covariance terms.\\end{proof}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(database.DatabaseError, "--anchors"):
            database.refresh_database(self.db, before["snapshot_id"])
        self.assertEqual(database.export_snapshot(self.db), before)
        locations = {}
        for anchor in before["anchors"]:
            locator = deepcopy(anchor["locator"])
            locator["start_line"] = locator["end_line"] = {1: 1, 4: 2, 7: 3}[locator["start_line"]]
            locations[anchor["id"]] = {"locator": locator}
        response = database.refresh_database(self.db, before["snapshot_id"], anchor_locations=locations)
        current = database.export_snapshot(self.db)
        self.assertNotEqual(response["snapshot_id"], before["snapshot_id"])
        self.assertEqual({row["id"] for row in current["anchors"]}, {row["id"] for row in before["anchors"]})
        self.assertEqual(current["items"], before["items"])
        self.assertEqual(database.export_snapshot(self.db, before["snapshot_id"]), before)

    def test_repeated_uses_and_cycles_are_retained_by_storage(self):
        data = database.export_snapshot(self.db)
        repeated = deepcopy(data["uses"][0])
        repeated.update(id="second-use", regime="Alternative route")
        reverse = deepcopy(repeated)
        reverse.update(id="reverse-use", **{"from": "variance", "to": "sampling"})
        batch = {"expected_snapshot": data["snapshot_id"], "edits": [{"collection": "uses", "op": "upsert", "id": row["id"], "record": row} for row in (repeated, reverse)]}
        database.apply_edits(self.db, batch)
        self.assertEqual(len(database.export_snapshot(self.db)["uses"]), 3)

    def test_anchor_shortcut_generates_evidence_without_manual_hashes(self):
        data = database.export_snapshot(self.db)
        item = deepcopy(data["items"][1])
        item["passages"].append({"role": "proof", "anchor_id": "proof-body"})
        batch = {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "anchors", "op": "upsert", "id": "proof-body", "record": {"file_id": data["source_revision"]["files"][0]["id"], "locator": {"start_line": 7, "end_line": 7}}},
            {"collection": "items", "op": "upsert", "id": "variance", "record": item},
        ]}
        database.apply_edits(self.db, batch)
        current = database.export_snapshot(self.db)
        anchor = next(row for row in current["anchors"] if row["id"] == "proof-body")
        self.assertIn("covariance terms", anchor["excerpt"])
        self.assertTrue(anchor["excerpt_hash"])

    def test_use_shortcut_supplies_identity_and_defaults(self):
        data = database.export_snapshot(self.db)
        batch = {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "uses", "op": "upsert", "id": "extra-use", "record": {"from": "sampling", "to": "variance", "reason": "A distinct synthetic use."}},
        ]}
        database.apply_edits(self.db, batch)
        use = next(row for row in database.export_snapshot(self.db)["uses"] if row["id"] == "extra-use")
        self.assertEqual(use["type"], "dependency")
        self.assertEqual(use["evidence_refs"], [])

    def test_removing_a_reviewed_item_preserves_history_and_updates_inventory(self):
        data = database.export_snapshot(self.db)
        database.compare_records(self.db, {"expected_snapshot": data["snapshot_id"], "targets": [{"collection": "items", "id": "variance"}], "reviewer": "test reviewer"})
        batch = {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "uses", "op": "remove", "id": data["uses"][0]["id"]},
            {"collection": "items", "op": "remove", "id": "variance"},
        ]}
        database.apply_edits(self.db, batch)
        current = database.export_snapshot(self.db)
        self.assertEqual([item["id"] for item in current["items"]], ["sampling"])
        self.assertEqual(len(current["observations"]), 1)
        self.assertEqual(len(database.export_snapshot(self.db, data["snapshot_id"])["items"]), 2)

    def test_malformed_comparison_inputs_are_actionable(self):
        base = {"expected_snapshot": self.initial["snapshot_id"], "targets": [{"collection": "items", "id": "variance"}], "reviewer": "test reviewer"}
        for field, value in (("targets", None), ("targets", []), ("reviewer", []), ("note", []), ("result", [])):
            with self.subTest(field=field, value=value):
                bad = deepcopy(base)
                bad[field] = value
                with self.assertRaises(database.DatabaseError):
                    database.compare_records(self.db, bad)

    def test_extra_source_registration_and_persisted_refresh_root(self):
        appendix = self.base / "appendix.tex"
        appendix.write_text("\\newcommand{\\bound}{1}\n", encoding="utf-8")
        extra = self.base / "with-appendix.sqlite"
        database.init_database(extra, self.dataset, extra_files=[appendix])
        self.assertEqual(len(database.export_snapshot(extra)["source_revision"]["files"]), 2)
        moved = self.base / "moved"
        moved.mkdir()
        (moved / "paper.tex").write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        (moved / "appendix.tex").write_text(appendix.read_text(encoding="utf-8"), encoding="utf-8")
        before = database.export_snapshot(extra)["snapshot_id"]
        database.refresh_database(extra, before, source_root=moved)
        self.assertEqual(database._source_root(extra), moved.resolve())
        self.assertEqual(database.validate_database(extra)["source_status"], "current")

    def test_validation_exposes_changed_live_source(self):
        self.source.write_text(self.source.read_text(encoding="utf-8").replace("independent", "dependent"), encoding="utf-8")
        result = database.validate_database(self.db)
        self.assertTrue(result["valid"])
        self.assertEqual(result["source_status"], "historical_changed")
        self.assertTrue(any("live manuscript differs" in warning for warning in result["warnings"]))

    def test_validation_scans_citations_once_and_preserves_coverage_summary(self):
        source = self.source.read_text(encoding="utf-8").replace(
            "Independence removes", r"Assumption \ref{ass:independence} removes")
        self.source.write_text(source + "See \\ref{thm:variance}.\n", encoding="utf-8")
        database.refresh_database(self.db, self.initial["snapshot_id"])
        expected = database._candidate_summary(database.export_snapshot(self.db))
        self.assertEqual(expected, {"pairs": 1, "not_mechanically_matchable": 0, "missing_uses": 0,
                                    "unsupported_uses": 0, "unmatched_labels": 0,
                                    "attributed": 1, "unattributed": 1})
        with patch.object(records, "citation_candidates", wraps=records.citation_candidates) as scan:
            result = database.validate_database(self.db)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(result["citation_candidates"], expected)

    def test_only_scope_exclusions_can_edit_derived_inventory(self):
        data = database.export_snapshot(self.db)
        database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"], "set": {"inventory": {"excluded": ["Simulation details are outside this overview."]}}})
        current = database.export_snapshot(self.db)
        self.assertEqual(current["inventory"]["excluded"], ["Simulation details are outside this overview."])
        self.assertEqual(current["inventory"]["declarations"], data["inventory"]["declarations"])
        with self.assertRaises(database.DatabaseError):
            database.apply_edits(self.db, {"expected_snapshot": current["snapshot_id"], "set": {"inventory": {"declarations": []}}})

    def test_build_log_failure_is_reported_without_claiming_render_failed(self):
        original_connect = database._connect
        output = self.base / "completed.html"
        def renderer(data, base_dir, output_path, protected_paths=()):
            output_path.write_text("<html>Previously checked synthetic output</html>", encoding="utf-8")
            return {"output": str(output_path), "sha256": "synthetic-fixture"}
        def connection(path, *, write=False):
            if write:
                raise database.DatabaseError("Simulated database write lock")
            return original_connect(path, write=write)
        with patch("proof_overview.render_dataset", side_effect=renderer), patch.object(database, "_connect", side_effect=connection):
            receipt = database.render_database(self.db, output)
        self.assertTrue(output.is_file())
        self.assertFalse(receipt["build_recorded"])
        self.assertIn("write lock", receipt["build_record_error"])
        self.assertEqual(receipt["snapshot_id"], self.initial["snapshot_id"])

    def test_capture_clock_difference_keeps_first_immutable_snapshot(self):
        before = database.export_snapshot(self.db)
        repeated = deepcopy(before)
        repeated["source_revision"]["created_at"] = "2099-01-01T00:00:00+00:00"
        self.assertEqual(records.snapshot_digest(repeated), before["snapshot_id"])
        result = database._publish(self.db, repeated, before["snapshot_id"])
        self.assertEqual(result["snapshot_id"], before["snapshot_id"])
        self.assertEqual(database.export_snapshot(self.db)["source_revision"]["created_at"], before["source_revision"]["created_at"])
        self.assertEqual(self.counts()["snapshots"], 1)

    def test_backup_keeps_history_and_rejects_existing_destination(self):
        database.apply_edits(self.db, self.update_caption())
        backup = self.base / "backup.sqlite"
        result = database.backup_database(self.db, backup)
        self.assertEqual(database.export_snapshot(backup), database.export_snapshot(self.db))
        self.assertEqual(database.export_snapshot(backup, self.initial["snapshot_id"])["snapshot_id"], self.initial["snapshot_id"])
        self.assertEqual(result["snapshot_id"], database.export_snapshot(self.db)["snapshot_id"])
        with self.assertRaises(database.DatabaseError):
            database.backup_database(self.db, backup)

    def test_export_cannot_replace_database(self):
        with self.assertRaises(database.DatabaseError):
            database._write_export(database.export_snapshot(self.db), self.db, self.db)
        self.assertTrue(database.validate_database(self.db)["valid"])

    def test_cli_returns_small_actionable_error(self):
        run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"), "get", str(self.db), "missing-item"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(run.returncode, 2)
        self.assertIn("missing-item", json.loads(run.stderr)["error"])
        self.assertNotIn("Traceback", run.stderr)

    def test_cli_redirected_json_is_utf8_under_legacy_encoding(self):
        environment = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
        for caption in ("Covariance, §2.2", "α → β"):
            with self.subTest(caption=caption):
                database.apply_edits(self.db, self.update_caption(caption))
                run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                                      "get", str(self.db), "variance"],
                                     capture_output=True, env=environment, timeout=15)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stderr, b"")
                self.assertEqual(json.loads(run.stdout.decode("utf-8"))["item"]["caption"], caption)

    def test_cli_redirected_error_is_utf8_under_legacy_encoding(self):
        missing = "missing-§-α"
        run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                              "get", str(self.db), missing], capture_output=True, timeout=15,
                             env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, b"")
        error = json.loads(run.stderr.decode("utf-8"))["error"]
        self.assertIn(missing, error)
        self.assertNotIn("Traceback", error)

    @unittest.skipUnless(shutil.which("node"), "shared Node.js is needed for rendering")
    def test_render_stores_build_without_changing_semantic_snapshot(self):
        before = database.export_snapshot(self.db)
        output = self.base / "paper-proof-overview.html"
        database.render_database(self.db, output)
        self.assertTrue(output.is_file())
        self.assertEqual(database.export_snapshot(self.db)["snapshot_id"], before["snapshot_id"])
        self.assertEqual(self.counts()["builds"], 1)

    def compare_everything(self, note="Compared the bounded synthetic passages."):
        data = database.export_snapshot(self.db)
        targets = [{"collection": group, "id": row["id"]}
                   for group in ("items", "uses") for row in data[group]]
        database.compare_records(self.db, {"expected_snapshot": data["snapshot_id"], "targets": targets,
                                           "reviewer": "Synthetic source reviewer", "note": note})
        return data

    def edit_prerequisite_caption(self, data):
        item = next(row for row in data["items"] if row["id"] == "sampling")
        edited = deepcopy(item)
        edited["caption"] = "Independent observations (clarified)"
        return database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "items", "op": "upsert", "id": "sampling", "record": edited}]})

    def test_validate_lists_exact_stale_targets(self):
        data = self.compare_everything()
        result = database.validate_database(self.db)
        self.assertEqual(result["source_comparison"]["stale"], 0)
        self.assertEqual(result["stale_targets"], [])
        # Editing the prerequisite stales it, the use digesting it, and the
        # theorem whose packet digests the prerequisite statement.
        self.edit_prerequisite_caption(data)
        result = database.validate_database(self.db)
        self.assertEqual(result["source_comparison"]["stale"], 3)
        self.assertEqual(result["stale_targets"], [{"collection": "items", "id": "sampling"},
                                                   {"collection": "items", "id": "variance"},
                                                   {"collection": "uses", "id": data["uses"][0]["id"]}])
        # Partial recomparison leaves exactly the untouched record stale.
        current = database.export_snapshot(self.db)
        database.compare_records(self.db, {"expected_snapshot": current["snapshot_id"],
                                           "targets": [{"collection": "items", "id": "variance"},
                                                       {"collection": "uses", "id": current["uses"][0]["id"]}],
                                           "reviewer": "Synthetic source reviewer", "note": "Recompared after the caption edit."})
        result = database.validate_database(self.db)
        self.assertEqual(result["stale_targets"], [{"collection": "items", "id": "sampling"}])
        self.assertEqual(result["source_comparison"]["stale"], 1)

    def test_packet_item_fidelity_is_distinct_from_the_aggregate(self):
        data = self.compare_everything()
        self.edit_prerequisite_caption(data)
        current = database.export_snapshot(self.db)
        database.compare_records(self.db, {"expected_snapshot": current["snapshot_id"],
                                           "targets": [{"collection": "items", "id": "variance"},
                                                       {"collection": "uses", "id": current["uses"][0]["id"]}],
                                           "reviewer": "Synthetic source reviewer", "note": "Recompared after the caption edit."})
        packet = database.get_packet(self.db, "variance")
        self.assertEqual(packet["target_fidelity"], "matched")
        self.assertEqual(packet["comparison_status"]["stale"], 1)
        stale_packet = database.get_packet(self.db, "sampling")
        self.assertEqual(stale_packet["target_fidelity"], "stale")
        self.assertEqual(stale_packet["comparison_status"]["stale"], 1)

    def test_export_receipt_counts_come_from_the_written_export(self):
        self.compare_everything()
        first = database.export_database(self.db, self.base / "export-1.json")
        self.assertEqual(first["observations"], 3)
        self.assertEqual(first["source_comparison"]["matched"], 3)
        self.assertEqual(first["snapshot_id"], self.initial["snapshot_id"])
        # Appending a comparison keeps the semantic snapshot but must move the receipt.
        database.compare_records(self.db, {"expected_snapshot": self.initial["snapshot_id"],
                                           "targets": [{"collection": "items", "id": "variance"}],
                                           "reviewer": "Synthetic source reviewer", "note": "A second look."})
        second = database.export_database(self.db, self.base / "export-2.json")
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(second["observations"], 4)
        exported = json.loads((self.base / "export-2.json").read_text(encoding="utf-8"))
        self.assertEqual(second["observations"], len(exported["observations"]))

    def test_reads_and_validation_do_not_mutate(self):
        self.compare_everything()
        before = (database.export_snapshot(self.db)["snapshot_id"], self.counts())
        database.validate_database(self.db)
        database.get_packet(self.db, "variance")
        database.export_database(self.db, self.base / "export.json")
        after = (database.export_snapshot(self.db)["snapshot_id"], self.counts())
        self.assertEqual(before, after)


class BundledSeedExampleTests(unittest.TestCase):
    """The shipped minimal seed must keep initializing against its real source."""

    def test_bundled_seed_initializes_and_validates(self):
        example = SKILL / "examples" / "representer-theorem"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "paper-records.sqlite"
            result = database.init_database(db, example / "seed.json", source_root=example)
            self.assertEqual((result["items"], result["uses"]), (3, 2))
            report = database.validate_database(db)
            self.assertTrue(report["valid"])
            self.assertEqual(report["source_status"], "current")
            self.assertEqual(report["source_comparison"]["unreviewed"], 5)
            self.assertEqual(report["stale_targets"], [])


if __name__ == "__main__":
    unittest.main()

"""Focused authoring and lossless handoff, using a small synthetic manuscript.

These tests check storage and user-visible selection, not proof correctness.
The audit wrapper is used unchanged, only on disposable database copies.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_database as database
import paper_records as records


class FocusedOverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "paper.tex"
        self.source.write_text(
            "\\begin{assumption}\\label{ass:independence}\n"
            "The variables are independent with finite variances.\n"
            "\\end{assumption}\n"
            "\\begin{theorem}\\label{thm:variance}\n"
            "Under Assumption \\ref{ass:independence}, the variance of the sum equals the sum of variances.\n"
            "\\end{theorem}\n"
            "\\begin{proof}Independence removes the covariance terms.\\end{proof}\n"
            "\\begin{corollary}\\label{cor:average}\n"
            "For independent variables of variance $v$, the average has variance $v/n$.\n"
            "\\end{corollary}\n"
            "\\begin{proof}Scale the identity in Theorem \\ref{thm:variance} by $n^{-2}$.\\end{proof}\n"
            "\\begin{theorem}\\label{thm:constant}\n"
            "A constant random variable has variance zero.\n"
            "\\end{theorem}\n"
            "\\begin{lemma}\\label{lem:unselected}\n"
            "A continuous function on a compact interval is bounded.\n"
            "\\end{lemma}\n",
            encoding="utf-8",
        )
        rows = [
            ("independence", "assumption", "Assumption 1", "Finite-variance independent variables", 1, 3, "ass:independence", "The variables are independent with finite variances."),
            ("variance", "theorem", "Theorem 1", "Variance of a sum", 4, 6, "thm:variance", "For independent variables with finite variances, the variance of their sum equals the sum of variances."),
            ("average", "corollary", "Corollary 1", "Variance of an average", 8, 10, "cor:average", "For independent variables of variance $v$, the average has variance $v/n$."),
            ("constant", "theorem", "Theorem 2", "Variance of a constant", 12, 14, "thm:constant", "A constant random variable has variance zero."),
        ]
        self.seed = {
            "schema_version": 3, "title": "Variance results",
            "scope": "Selected variance results and their prerequisites; the unrelated compactness lemma is outside scope.",
            "source": {"title": "Synthetic variance manuscript", "file": "paper.tex"},
            "main_items": ["variance", "average", "constant"],
            "items": [{"id": identity, "kind": kind, "label": label, "caption": caption,
                       "statement": {"text": text, "form": "synopsis"},
                       "source": {"label": tex_label, "start_line": start, "end_line": end}}
                      for identity, kind, label, caption, start, end, tex_label, text in rows],
            "uses": [
                {"id": "independence-variance", "from": "independence", "to": "variance", "type": "dependency",
                 "reason": "Independence makes the off-diagonal covariance terms vanish.",
                 "source": {"start_line": 7, "end_line": 7}},
                {"id": "variance-average", "from": "variance", "to": "average", "type": "proof_argument",
                 "reason": "Rescale the variance identity by $n^{-2}$ for the average.",
                 "regime": "Independent variables sharing a finite variance $v$.",
                 "source": {"start_line": 11, "end_line": 11}},
            ],
        }
        self.db = self.base / "focused.sqlite"

    def initialize(self, seed=None, *, name="focused", focused=True):
        path = self.base / f"{name}.json"
        path.write_text(json.dumps(self.seed if seed is None else seed), encoding="utf-8")
        db_path = self.base / f"{name}.sqlite"
        # Non-focused fixtures here exercise historical unsourced comparisons
        # and native migration; new focused work uses the common SQL authority.
        initialize = database.init_database if focused else database._native_init_database
        receipt = initialize(db_path, path, focused=focused)
        return db_path, receipt

    def compare(self, db_path, targets=None, **extra):
        data = database.export_snapshot(db_path)
        batch = {"expected_snapshot": data["snapshot_id"],
                 "targets": targets or [{"collection": group, "id": row["id"]}
                                        for group in ("items", "uses") for row in data[group]],
                 "reviewer": "Synthetic source reader",
                 "note": "Compared the stated variance conditions and the located contribution passages."}
        batch.update(extra)
        return database.compare_records(db_path, batch)

    def unsourced_seed(self):
        seed = deepcopy(self.seed)
        seed["uses"][0].pop("source")
        seed["uses"][0]["issue"] = "The intended dependency needs a located source passage."
        return seed

    def counts(self, db_path):
        connection = sqlite3.connect(db_path)
        try:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='record_versions'").fetchone():
                return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                        for name in ("commits", "record_versions", "blobs", "publications")}
            return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("snapshots", "observations", "source_blobs", "builds")}
        finally:
            connection.close()

    def install_profile_for_corruption_test(self, db_path):
        # Simulate a malformed focused store without changing the protected
        # canonical export or any of its hashes.
        connection = sqlite3.connect(db_path)
        try:
            with connection:
                connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('authoring_profile','focused')")
        finally:
            connection.close()

    def embedded(self, output):
        match = re.search(r'<script id="proof-overview-records" type="application/json">([\s\S]*?)</script>',
                          output.read_text(encoding="utf-8"))
        self.assertIsNotNone(match)
        return json.loads(match[1])

    def test_focused_workflow_preserves_selection_and_small_packets(self):
        db_path, receipt = self.initialize()
        self.assertEqual(receipt["authoring_profile"], "focused")
        original = database.export_snapshot(db_path)
        database.apply_edits(db_path, {"expected_snapshot": original["snapshot_id"],
                                      "set": {"title": "Selected variance results"}})
        data = database.export_snapshot(db_path)
        packet = database.get_packet(db_path, "average")
        self.assertEqual(packet["authoring_profile"], "focused")
        self.assertEqual([row["id"] for row in packet["prerequisite_items"]], ["variance"])
        self.assertEqual(packet["owned_items"], [])
        self.assertEqual(packet["owned_step_uses"], [])
        self.assertNotIn("content_base64", json.dumps(packet))
        self.assertNotIn("authoring_profile", data)
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["main_items"], ["variance", "average", "constant"])
        self.assertEqual(sum(row["id"] == "variance" for row in data["items"]), 1)
        self.assertEqual(database.get_packet(db_path, "constant")["incoming_uses"], [])
        self.assertEqual(data["inventory"]["excluded"], [])
        self.assertGreater(len(data["inventory"]["declarations"]), len(data["items"]))
        self.compare(db_path)
        status = database.validate_database(db_path)
        self.assertTrue(status["valid"])
        self.assertEqual(status["authoring_profile"], "focused")
        self.assertEqual(status["selected_inventory"]["unselected_declarations"], 1)
        self.assertEqual(status["source_comparison"]["matched"], 6)
        self.assertEqual(status["source_comparison"]["unreviewed"], 0)

    def test_candidates_distinguish_unselected_declarations_from_unknown_labels(self):
        self.source.write_text(self.source.read_text(encoding="utf-8") +
                               "See \\ref{lem:unselected} and \\ref{not:resolved}.\n", encoding="utf-8")
        db_path, _ = self.initialize()
        before = database.export_snapshot(db_path)
        report = database.candidates_database(db_path)
        self.assertEqual(report["outside_selected_scope"], {"declarations": 1, "labels": 1})
        self.assertEqual({row["label"] for row in report["unmatched_labels"]}, {"not:resolved"})
        self.assertEqual(report["counts"]["unmatched_labels"], 1)
        summary = database.validate_database(db_path)["citation_candidates"]
        self.assertEqual(summary["unmatched_labels"], report["counts"]["unmatched_labels"])
        self.assertEqual(summary["outside_selected_scope"], report["outside_selected_scope"])
        self.assertEqual(database.export_snapshot(db_path), before)
        database.apply_edits(db_path, {"expected_snapshot": before["snapshot_id"],
                                      "set": {"title": "Selected variance structure"}})
        self.assertEqual(database.changes_database(db_path, before["snapshot_id"])["citation_candidates"], summary)

    def test_plain_source_candidates_remain_not_applicable_without_losing_selected_records(self):
        seed = deepcopy(self.seed)
        seed["source"]["file"] = "paper.txt"
        plain = self.base / "paper.txt"
        plain.write_text(
            "Assumption 1\nIndependent variables with finite variances.\nEnd assumption.\n"
            "Theorem 1\nThe variance of a sum is the sum of variances under Assumption 1.\nEnd theorem.\n"
            "Proof: independence removes the covariance terms.\n"
            "Corollary 1\nFor independent variables of variance v, the average has variance v/n.\nEnd corollary.\n"
            "Proof: scale the variance identity by the inverse square of the sample size.\n"
            "Theorem 2\nA constant random variable has variance zero.\nEnd theorem.\n",
            encoding="utf-8",
        )
        for item in seed["items"]:
            item["source"].pop("label")
        db_path, _ = self.initialize(seed)
        before = database.export_snapshot(db_path)
        report = database.candidates_database(db_path)
        self.assertEqual(report["authoring_profile"], "focused")
        self.assertFalse(report["coverage"]["applicable"])
        self.assertIsNone(report["counts"])
        self.assertEqual(report["unmatched_labels"], [])
        self.assertNotIn("scaffold", report["note"])
        self.assertNotIn("reconcile", report["note"])
        self.assertEqual({row["id"] for row in report["not_mechanically_matchable"]},
                         {row["id"] for row in seed["items"]})
        self.assertEqual(database.export_snapshot(db_path), before)
        self.compare(db_path)
        self.assertEqual(database.validate_database(db_path)["source_comparison"]["matched"], 6)
        self.assertEqual(database.validate_database(db_path)["citation_candidates"],
                         "not applicable (no TeX sources captured)")

    def test_focused_workflow_does_not_scaffold_an_exhaustive_edge_audit(self):
        db_path, _ = self.initialize()
        output = self.base / "unrequested-edge-audits"
        before = database.export_snapshot(db_path)
        with self.assertRaisesRegex(ValueError, "get.*candidates"):
            database.scaffold_audits(db_path, output)
        self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, "get.*candidates"):
            database.reconcile_audits(db_path, output)
        self.assertEqual(database.export_snapshot(db_path), before)

    @unittest.skipUnless(shutil.which("node"), "Standalone rendering requires shared Node.js.")
    def test_render_includes_every_selected_statement_not_only_main_results(self):
        self.source.write_text(self.source.read_text(encoding="utf-8") +
                               "See \\ref{lem:unselected} and \\ref{not:resolved}.\n", encoding="utf-8")
        db_path, _ = self.initialize()
        self.compare(db_path)
        output = self.base / "overview.html"
        receipt = database.render_database(db_path, output)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        embedded = self.embedded(output)
        self.assertEqual({row["id"] for row in embedded["items"]},
                         {row["id"] for row in self.seed["items"]})
        self.assertEqual({row["id"] for row in embedded["uses"]},
                         {row["id"] for row in self.seed["uses"]})
        self.assertEqual(embedded.get("details", []), [])
        self.assertIn(self.seed["scope"], output.read_text(encoding="utf-8"))
        self.assertIn("Selected statements and connections", output.read_text(encoding="utf-8"))
        summary = database.validate_database(db_path)["citation_candidates"]
        self.assertEqual(summary["unmatched_labels"], 1)
        self.assertEqual(receipt["citation_candidates"], summary)
        self.assertEqual(embedded["build_context"]["citation_candidates"], summary)

    def test_invalid_focused_initialization_creates_no_database(self):
        canonical = records.normalize(self.seed, self.base)
        mutations = []
        for kind in ("equation", "claim", "derivation"):
            candidate = deepcopy(canonical)
            intermediate = deepcopy(candidate["items"][1])
            intermediate.update(id=f"detail-{kind}", kind=kind, owner="variance")
            candidate["items"].append(intermediate)
            mutations.append((kind, candidate))
        for label, value in (("missing-mains", None), ("empty-mains", []),
                             ("duplicate-mains", ["variance", "variance"]),
                             ("unknown-main", ["not-in-selected-inventory"])):
            candidate = deepcopy(canonical)
            if value is None:
                candidate.pop("main_items")
            else:
                candidate["main_items"] = value
            mutations.append((label, candidate))
        owned = deepcopy(canonical)
        owned["items"][0]["owner"] = "variance"
        mutations.append(("owned-major", owned))
        grouped = deepcopy(canonical)
        grouped["uses"][0]["group"] = {"id": "joint-premises", "kind": "joint"}
        mutations.append(("formal-group", grouped))
        for name, candidate in mutations:
            with self.subTest(name=name):
                candidate.pop("snapshot_id", None)
                with self.assertRaises(ValueError):
                    self.initialize(candidate, name=name)
                self.assertFalse((self.base / f"{name}.sqlite").exists())

    def test_invalid_edits_and_metadata_only_edits_are_atomic(self):
        db_path, _ = self.initialize()
        before = database.export_snapshot(db_path)
        original_counts = self.counts(db_path)
        detail = deepcopy(before["items"][1])
        detail.update(id="algebra-step", kind="derivation", owner="variance")
        owned = deepcopy(before["items"][0])
        owned["owner"] = "variance"
        grouped = deepcopy(before["uses"][0])
        grouped["group"] = {"id": "joint-premises", "kind": "joint"}
        patches = [
            {"edits": [{"collection": "items", "op": "upsert", "id": detail["id"], "record": detail}]},
            {"edits": [{"collection": "items", "op": "upsert", "id": owned["id"], "record": owned}]},
            {"edits": [{"collection": "uses", "op": "upsert", "id": grouped["id"], "record": grouped}]},
            *({"set": {"main_items": value}} for value in (None, [], ["missing"], ["variance", "variance"])),
        ]
        for patch in patches:
            with self.subTest(patch=patch):
                patch["expected_snapshot"] = before["snapshot_id"]
                patch.setdefault("set", {})["title"] = "This must not be committed"
                with self.assertRaises(ValueError):
                    database.apply_edits(db_path, patch)
                self.assertEqual(database.export_snapshot(db_path), before)
                self.assertEqual(self.counts(db_path), original_counts)

    def test_null_optional_fields_are_allowed_and_profile_survives_backup_import(self):
        seed = deepcopy(self.seed)
        seed["items"][0]["owner"] = None
        seed["uses"][0]["group"] = None
        db_path, _ = self.initialize(seed)
        self.compare(db_path)
        before = database.export_snapshot(db_path)
        backup = self.base / "backup.sqlite"
        database.backup_database(db_path, backup)
        self.assertEqual(database.validate_database(backup)["authoring_profile"], "focused")
        self.assertEqual(database.export_snapshot(backup), before)
        export_path = self.base / "portable.json"
        database.export_database(db_path, export_path)
        self.assertNotIn("authoring_profile", json.loads(export_path.read_text(encoding="utf-8")))
        imported = self.base / "imported.sqlite"
        database.init_database(imported, export_path, focused=True)
        self.assertEqual(database.export_snapshot(imported), before)
        self.assertEqual(database.get_packet(imported, "variance")["authoring_profile"], "focused")

    def test_unsourced_use_can_stay_unresolved_but_cannot_be_marked_matched(self):
        db_path, _ = self.initialize(self.unsourced_seed())
        before = database.export_snapshot(db_path)
        with self.assertRaises(ValueError):
            # Rejection is atomic even when other targets have located evidence.
            self.compare(db_path)
        self.assertEqual(database.export_snapshot(db_path), before)
        self.compare(db_path, [{"collection": "uses", "id": "independence-variance"}],
                     result="needs_attention", note="Locate the passage supporting this provisional link.")
        status = database.validate_database(db_path)
        self.assertTrue(status["valid"])
        self.assertEqual(status["uses_without_evidence"], ["independence-variance"])
        self.assertEqual(status["source_comparison"]["needs_attention"], 1)

    def test_focused_import_refuses_current_unsourced_match_without_rewriting_history(self):
        db_path, _ = self.initialize(self.unsourced_seed(), name="legacy", focused=False)
        self.compare(db_path, [{"collection": "uses", "id": "independence-variance"}])
        export_path = self.base / "reviewed-export.json"
        database.export_database(db_path, export_path)
        unchanged = export_path.read_bytes()
        target = self.base / "rejected.sqlite"
        with self.assertRaises(ValueError):
            database.init_database(target, export_path, focused=True)
        self.assertFalse(target.exists())
        self.assertEqual(export_path.read_bytes(), unchanged)
        self.assertEqual(database.export_snapshot(db_path)["observations"][0]["result"], "matched")

    @unittest.skipUnless(shutil.which("node"), "Standalone rendering requires shared Node.js.")
    def test_unresolved_connection_stays_visible_with_its_issue(self):
        db_path, _ = self.initialize(self.unsourced_seed())
        self.compare(db_path, [{"collection": "uses", "id": "independence-variance"}],
                     result="needs_attention", note="Locate the supporting passage.")
        output = self.base / "unresolved.html"
        database.render_database(db_path, output)
        use = next(row for row in self.embedded(output)["uses"] if row["id"] == "independence-variance")
        self.assertEqual(use["fidelity"], "needs_attention")
        self.assertEqual(use["issue"], self.unsourced_seed()["uses"][0]["issue"])

    def test_validation_and_reading_reject_invalid_persisted_profile(self):
        db_path, _ = self.initialize(self.unsourced_seed(), name="malformed", focused=False)
        self.compare(db_path, [{"collection": "uses", "id": "independence-variance"}])
        self.install_profile_for_corruption_test(db_path)
        before = db_path.read_bytes()
        for read in (lambda: database.validate_database(db_path),
                     lambda: database.get_packet(db_path, "variance"),
                     lambda: database.export_snapshot(db_path)):
            with self.subTest(read=read), self.assertRaises(ValueError):
                read()
        self.assertEqual(db_path.read_bytes(), before)

    def test_stale_and_superseded_unsourced_matches_remain_historical(self):
        for state in ("stale", "superseded"):
            with self.subTest(state=state):
                db_path, _ = self.initialize(self.unsourced_seed(), name=state, focused=False)
                targets = [{"collection": "uses", "id": "independence-variance"}]
                self.compare(db_path, targets)
                if state == "stale":
                    data = database.export_snapshot(db_path)
                    use = deepcopy(data["uses"][0])
                    use["reason"] += " The exact source basis remains unresolved."
                    database.apply_edits(db_path, {"expected_snapshot": data["snapshot_id"], "edits": [
                        {"collection": "uses", "op": "upsert", "id": use["id"], "record": use}]})
                else:
                    self.compare(db_path, targets, result="needs_attention", note="The link still lacks located evidence.")
                data = database.export_snapshot(db_path)
                imported, _ = self.initialize(data, name=f"focused-{state}")
                self.assertEqual(database.export_snapshot(imported), data)
                status = database.validate_database(imported)
                self.assertEqual(status["source_comparison"]["matched"], 0)
                self.assertEqual(status["source_comparison"]["stale" if state == "stale" else "needs_attention"], 1)

    def test_reviewed_reuse_cannot_revive_an_unsourced_match(self):
        db_path, _ = self.initialize(self.unsourced_seed(), name="reuse", focused=False)
        target = [{"collection": "uses", "id": "independence-variance"}]
        self.compare(db_path, target)
        baseline = database.export_snapshot(db_path)
        self.source.write_text(self.source.read_text(encoding="utf-8") + "% Editorial comment.\n", encoding="utf-8")
        database.refresh_database(db_path, baseline["snapshot_id"])
        self.install_profile_for_corruption_test(db_path)
        before = database.export_snapshot(db_path)
        self.assertEqual(database.validate_database(db_path)["source_comparison"]["stale"], 1)
        with self.assertRaises(ValueError):
            self.compare(db_path, target, reuse_from=baseline["snapshot_id"], changes_reviewed=True,
                         note="Reviewed the added editorial comment; no mathematical text changed.")
        self.assertEqual(database.export_snapshot(db_path), before)

    def rich_fixture(self):
        data = records.normalize(self.seed, self.base)
        detail = deepcopy(data["items"][1])
        detail.update(id="variance-algebra", kind="derivation", owner="variance", label="Variance expansion",
                      caption="Sum the covariances", statement={"text": "Sum variances and off-diagonal covariances.", "form": "synopsis"})
        data["items"].append(detail)
        use = deepcopy(data["uses"][0])
        use.update(id="detail-variance", **{"from": "variance-algebra"},
                   group={"id": "joint-variance", "kind": "joint"})
        data["uses"][0]["group"] = {"id": "joint-variance", "kind": "joint"}
        data["uses"].append(use)
        data.pop("snapshot_id", None)
        return records.validate_records(data)

    @unittest.skipUnless(shutil.which("node"), "Standalone rendering requires shared Node.js.")
    def test_unflagged_rich_reader_preserves_details_groups_and_comparison_context(self):
        db_path, _ = self.initialize(self.rich_fixture(), name="rich", focused=False)
        self.compare(db_path)
        original = database.export_snapshot(db_path)
        packet = database.get_packet(db_path, "variance")
        self.assertEqual([row["id"] for row in packet["owned_items"]], ["variance-algebra"])
        self.assertIn("variance-algebra", {row["id"] for row in packet["target_digests"]})
        output = self.base / "rich.html"
        database.render_database(db_path, output)
        embedded = self.embedded(output)
        self.assertEqual({row["id"] for row in embedded["items"] + embedded["details"]},
                         {row["id"] for row in original["items"]})
        self.assertEqual({row["id"] for row in embedded["uses"] + embedded["detail_uses"]},
                         {row["id"] for row in original["uses"]})
        self.assertEqual(database.export_snapshot(db_path), original)
        export_path = self.base / "rich-export.json"
        database.export_database(db_path, export_path)
        restored = self.base / "rich-restored.sqlite"
        database.init_database(restored, export_path)
        self.assertEqual(database.export_snapshot(restored), original)
        changed = deepcopy(next(row for row in original["items"] if row["id"] == "variance-algebra"))
        changed["statement"]["text"] += " The expansion is now qualified."
        database.apply_edits(restored, {"expected_snapshot": original["snapshot_id"], "edits": [
            {"collection": "items", "op": "upsert", "id": changed["id"], "record": changed}]})
        self.assertEqual(database.get_packet(restored, "variance")["target_fidelity"], "stale")

    def audit_command(self, *arguments):
        result = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_audit.py"), *map(str, arguments)],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_unchanged_audit_handoff_preserves_focused_and_rich_content_without_proof_checks(self):
        for focused in (True, False):
            name = "focused-handoff" if focused else "rich-handoff"
            with self.subTest(profile=name):
                seed = self.seed if focused else self.rich_fixture()
                db_path, _ = self.initialize(seed, name=name, focused=focused)
                self.compare(db_path)
                before = database.export_snapshot(db_path)
                original_bytes = db_path.read_bytes()
                handoff = self.base / f"{name}-copy.sqlite"
                shutil.copy2(db_path, handoff)
                backup = self.base / f"{name}-backup.sqlite"
                if focused:
                    database.backup_database(handoff, backup)
                else:
                    self.audit_command("migrate-overview", handoff, "--backup", backup)
                self.assertEqual(db_path.read_bytes(), original_bytes)
                self.assertEqual(database.export_snapshot(backup), before)
                export_path = self.base / f"{name}-audit.json"
                self.audit_command("export", handoff, "--out", export_path)
                audit = json.loads(export_path.read_text(encoding="utf-8"))
                collections = {}
                for row in audit["records"]:
                    if not row["retired"]:
                        collections.setdefault(row["collection"], {})[row["id"]] = row["body"]
                self.assertEqual(set(collections["items"]), {row["id"] for row in before["items"]})
                self.assertEqual(set(collections["uses"]), {row["id"] for row in before["uses"]})
                paper = next(iter(collections["papers"].values()))
                self.assertEqual(paper["main_items"], before["main_items"])
                self.assertEqual(paper["scope"], before["scope"])
                self.assertEqual({row["blob_sha256"] for row in collections["sources"].values()},
                                 {row["sha256"] for row in before["source_revision"]["files"]})
                for row in before["anchors"]:
                    imported = collections["anchors"][row["id"]]
                    self.assertEqual(imported["excerpt"], row["excerpt"])
                    self.assertEqual(imported["source_id"], row["file_id"])
                    self.assertEqual({key: value for key, value in imported["locator"].items() if value is not None}, row["locator"])
                for row in before["items"]:
                    imported = collections["items"][row["id"]]
                    self.assertEqual(imported["statement"], row["statement"])
                    self.assertEqual(imported["passages"], row["passages"])
                    self.assertEqual(imported["owner_id"], row.get("owner"))
                self.assertEqual(len(collections["observations"]), len(before["items"]) + len(before["uses"]))
                self.assertTrue(all(row["result"] == "matched" for row in collections["observations"].values()))
                self.assertTrue(audit["bindings"])
                for forbidden in ("checks", "findings", "coverage", "arguments", "audits", "source_reviews"):
                    self.assertFalse(collections.get(forbidden), f"Handoff invented {forbidden}.")
                if focused:
                    self.assertFalse(collections.get("groups"))
                else:
                    self.assertEqual(set(collections["groups"]), {"joint-variance"})

    def test_cli_focused_flag_is_persisted_for_later_commands(self):
        seed_path = self.base / "cli-seed.json"
        seed_path.write_text(json.dumps(self.seed), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                                 "init", str(self.db), str(seed_path), "--focused"],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["authoring_profile"], "focused")
        self.assertEqual(database.validate_database(self.db)["authoring_profile"], "focused")


if __name__ == "__main__":
    unittest.main()

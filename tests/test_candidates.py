"""Behavioral tests for citation candidates and the per-node edge-audit sweep.

The fixture is a synthetic TeX manuscript with four major rows and one owned
equation row. Candidates and reconcile propose reviews; the database must
remain untouched until an explicit apply/compare batch records a decision.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_database as database
import paper_records as records


class CandidateFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.main = self.base / "main.tex"
        self.main.write_text(
            "\\documentclass{article}\n"          # 1
            "\\begin{document}\n"                # 2
            "\\begin{assumption}\\label{ass:ind}\n"   # 3
            "The observations are independent.\n"     # 4
            "\\end{assumption}\n"                # 5
            "\\begin{lemma}\\label{lem:bound}\n" # 6
            "The sum is bounded.\n"              # 7
            "\\end{lemma}\n"                     # 8
            "\\begin{proof}\n"                   # 9
            "The bound follows from the definitions.\n"  # 10
            "\\end{proof}\n"                     # 11
            "\\begin{lemma}\n"                   # 12
            "The variance is finite.\n"          # 13
            "\\end{lemma}\n"                     # 14
            "\\begin{theorem}\\label{thm:main}\n"      # 15
            "The estimator is consistent by \\cref{lem:bound}.\n"  # 16
            "\\end{theorem}\n"                   # 17
            "\\begin{proof}\n"                   # 18
            "Apply \\ref{lem:bound}; with\n"     # 19
            "\\begin{equation}\\label{eq:rate}\n"      # 20
            "r_n = n^{-1/2}\n"                   # 21
            "\\end{equation}\n"                  # 22
            "and \\eqref{eq:rate} the claim follows, reducing to \\ref{thm:main}.\n"  # 23
            "\\end{proof}\n"                     # 24
            "As \\Cref{thm:main} shows, consistency holds.\n"      # 25
            "The survey mentions \\ref{thm:main}, \\ref{fig:unrecorded} and \\cref{fig:other,fig:third}.\n"  # 26
            "\\end{document}\n",                 # 27
            encoding="utf-8",
        )
        self.seed = {
            "schema_version": 3,
            "title": "Candidate sweep fixture",
            "scope": "Synthetic candidate coverage.",
            "source": {"title": "Synthetic manuscript", "file": "main.tex"},
            "items": [
                {"id": "independence", "kind": "assumption", "label": "Assumption 1",
                 "caption": "Independent observations",
                 "statement": {"text": "The observations are independent.", "form": "synopsis"},
                 "source": {"label": "ass:ind", "start_line": 3, "end_line": 5}},
                {"id": "bound", "kind": "lemma", "label": "Lemma 1",
                 "caption": "Bounded sum",
                 "statement": {"text": "The sum is bounded.", "form": "synopsis"},
                 "source": {"label": "lem:bound", "start_line": 6, "end_line": 8}},
                {"id": "finiteness", "kind": "lemma", "label": "Lemma 2",
                 "caption": "Finite variance",
                 "statement": {"text": "The variance is finite.", "form": "synopsis"},
                 "source": {"start_line": 12, "end_line": 14}},
                {"id": "consistency", "kind": "theorem", "label": "Theorem 1",
                 "caption": "Consistent estimator",
                 "statement": {"text": "The estimator is consistent.", "form": "synopsis"},
                 "source": {"label": "thm:main", "start_line": 15, "end_line": 17}},
            ],
            "uses": [
                {"from": "bound", "to": "consistency", "reason": "The bound controls the centered sum.",
                 "source": {"start_line": 19, "end_line": 19}},
                {"from": "independence", "to": "bound", "reason": "Independence controls the summands."},
                {"from": "independence", "to": "consistency",
                 "reason": "Inferred from the decoupling argument; no explicit citation."},
                {"from": "finiteness", "to": "consistency", "reason": "Finite variance enters the limit."},
                {"from": "bound", "to": "finiteness", "reason": "The bound restricts the variance.",
                 "issue": "Inferred link; the manuscript does not cite it."},
            ],
        }
        self.dataset = self.base / "overview.json"
        self.dataset.write_text(json.dumps(self.seed), encoding="utf-8")
        self.db = self.base / "paper.sqlite"
        database.init_database(self.db, self.dataset)
        self.add_rate_row()

    def tearDown(self):
        self.temporary.cleanup()

    def add_rate_row(self):
        data = database.export_snapshot(self.db)
        file_id = data["source_revision"]["files"][0]["id"]
        database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "anchors", "op": "upsert", "id": "anchor-eq-rate",
             "record": {"file_id": file_id, "locator": {"label": "eq:rate", "start_line": 20, "end_line": 22}}},
            {"collection": "items", "op": "upsert", "id": "rate",
             "record": {"kind": "equation", "label": "Rate display", "caption": "Convergence rate",
                        "statement": {"text": "$r_n = n^{-1/2}$", "form": "verbatim"},
                        "owner": "consistency",
                        "passages": [{"role": "statement", "anchor_id": "anchor-eq-rate"}]}},
        ]})

    def use_id(self, source, target):
        data = database.export_snapshot(self.db)
        return next(use["id"] for use in data["uses"] if use["from"] == source and use["to"] == target)

    def fill_consistency_audit(self, folder):
        path = Path(folder) / "consistency.json"
        audit = json.loads(path.read_text(encoding="utf-8"))
        audit["depends_on"] = [
            {"id": "bound", "type": "dependency", "reason": "The bound controls the sum."},
            {"id": "rate", "reason": "The rate display gives the contraction."},
            {"id": "independence", "type": "proof_argument", "regime": "decoupling route",
             "reason": "Reuses the decoupling argument."},
        ]
        audit["dismissed"] = [{"id": "finiteness", "note": "Only supports intuition; not used."}]
        path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
        return path


class CitationCandidateTests(CandidateFixture):
    def test_occurrences_are_classified_by_statement_proof_and_narrative(self):
        report = database.candidates_database(self.db)
        self.assertEqual(report["snapshot_id"], database.export_snapshot(self.db)["snapshot_id"])
        pairs = {(row["citing"], row["cited"]): row for row in report["pairs"]}
        self.assertEqual(sorted(pairs), [("consistency", "bound"), ("consistency", "rate")])
        bound = pairs[("consistency", "bound")]
        self.assertEqual(bound["recorded_uses"], [self.use_id("bound", "consistency")])
        self.assertEqual([(o["line"], o["command"], o["context"]) for o in bound["occurrences"]],
                         [(16, "\\cref", "statement"), (19, "\\ref", "proof")])
        rate = pairs[("consistency", "rate")]
        self.assertEqual(rate["recorded_uses"], [])
        self.assertEqual([(o["line"], o["command"], o["context"]) for o in rate["occurrences"]],
                         [(23, "\\eqref", "proof")])
        # The proof's \ref{thm:main} on line 23 cites the enclosing row itself.
        self.assertNotIn(("consistency", "consistency"), pairs)
        narrative = report["unattributed_occurrences"]
        self.assertEqual([(o["line"], o["command"], o["cited"], o["context"]) for o in narrative],
                         [(25, "\\Cref", "consistency", "narrative"), (26, "\\ref", "consistency", "narrative")])

    def test_unknown_labels_are_listed_as_unmatched(self):
        report = database.candidates_database(self.db)
        unmatched = {row["label"]: row["occurrences"] for row in report["unmatched_labels"]}
        self.assertEqual(sorted(unmatched), ["fig:other", "fig:third", "fig:unrecorded"])
        for label in ("fig:other", "fig:third", "fig:unrecorded"):
            occurrence, = unmatched[label]
            self.assertEqual((occurrence["line"], occurrence["context"]), (26, "narrative"))
        self.assertEqual(report["label_collisions"], [])

    def test_missing_and_unsupported_lists_follow_the_recorded_uses(self):
        report = database.candidates_database(self.db)
        self.assertEqual([(row["citing"], row["cited"]) for row in report["missing_uses"]],
                         [("consistency", "rate")])
        unsupported = {row["id"]: row for row in report["unsupported_uses"]}
        self.assertEqual(sorted(unsupported), [self.use_id("finiteness", "consistency"),
                                               self.use_id("independence", "bound")])
        self.assertTrue(unsupported[self.use_id("independence", "bound")]["cited_matchable"])
        self.assertFalse(unsupported[self.use_id("finiteness", "consistency")]["cited_matchable"])
        # "Inferred ..." in reason or issue marks a honestly disclosed extraction link.
        self.assertNotIn(self.use_id("independence", "consistency"), unsupported)
        self.assertNotIn(self.use_id("bound", "finiteness"), unsupported)
        self.assertEqual(report["counts"],
                         {"pairs": 2, "occurrences": 5, "missing_uses": 1, "unsupported_uses": 2,
                          "unmatched_labels": 3, "not_mechanically_matchable": 1})
        self.assertIn("evidence to review, not a recorded use", report["note"])

    def test_rows_without_label_anchors_are_listed_as_not_matchable(self):
        report = database.candidates_database(self.db)
        self.assertEqual(report["not_mechanically_matchable"],
                         [{"id": "finiteness", "kind": "lemma", "label": "Lemma 2"}])

    def test_report_is_bound_to_the_captured_snapshot_not_live_files(self):
        before = database.candidates_database(self.db)
        self.main.write_text(self.main.read_text(encoding="utf-8") + "Later edits cite \\ref{lem:bound} too.\n",
                             encoding="utf-8")
        after = database.candidates_database(self.db)
        self.assertEqual(before, after)
        data = database.export_snapshot(self.db)
        database.refresh_database(self.db, data["snapshot_id"])
        refreshed = database.candidates_database(self.db)
        self.assertNotEqual(refreshed["snapshot_id"], before["snapshot_id"])
        self.assertEqual(len(refreshed["unattributed_occurrences"]), len(before["unattributed_occurrences"]) + 1)

    def test_retained_snapshot_reports_its_own_captured_content(self):
        data = database.export_snapshot(self.db)
        # Remove the rate row again through an explicit batch and compare reports.
        database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "items", "op": "remove", "id": "rate"},
            {"collection": "anchors", "op": "remove", "id": "anchor-eq-rate"},
        ]})
        historical = database.candidates_database(self.db, data["snapshot_id"])
        self.assertEqual(historical["snapshot_id"], data["snapshot_id"])
        self.assertIn(("consistency", "rate"),
                      {(row["citing"], row["cited"]) for row in historical["pairs"]})
        current = database.candidates_database(self.db)
        self.assertIn("eq:rate", {row["label"] for row in current["unmatched_labels"]})
        self.assertEqual([(row["citing"], row["cited"]) for row in current["pairs"]],
                         [("consistency", "bound")])

    def test_validate_and_changes_surface_compact_candidate_counts(self):
        expected = {"pairs": 2, "not_mechanically_matchable": 1,
                    "missing_uses": 1, "unsupported_uses": 2, "unmatched_labels": 3,
                    "attributed": 3, "unattributed": 2}
        self.assertEqual(database.validate_database(self.db)["citation_candidates"], expected)
        data = database.export_snapshot(self.db)
        database.refresh_database(self.db, data["snapshot_id"])
        report = database.changes_database(self.db, data["snapshot_id"])
        self.assertEqual(report["citation_candidates"], expected)

    def test_cli_candidates_prints_the_report_and_writes_compact_receipt(self):
        run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                              "candidates", str(self.db)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["counts"]["missing_uses"], 1)
        output = self.base / "candidates.json"
        run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                              "candidates", str(self.db), "--output", str(output)],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stderr)
        receipt = json.loads(run.stdout)
        self.assertEqual(receipt["counts"]["unsupported_uses"], 2)
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["pairs"], report["pairs"])


class EdgeAuditSweepTests(CandidateFixture):
    def test_scaffold_writes_one_complete_empty_audit_per_major_row(self):
        folder = self.base / "audits"
        head = database.export_snapshot(self.db)["snapshot_id"]
        receipt = database.scaffold_audits(self.db, folder)
        self.assertEqual(receipt["snapshot_id"], head)
        self.assertEqual([Path(path).name for path in receipt["audits"]],
                         ["independence.json", "bound.json", "finiteness.json", "consistency.json"])
        for item_id in ("independence", "bound", "finiteness", "consistency"):
            audit = json.loads((folder / f"{item_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["audit_format"], "archify-edge-audit-1")
            self.assertEqual(audit["target"], item_id)
            self.assertEqual(audit["expected_snapshot"], head)
            self.assertEqual(sorted(audit["candidates_considered"]),
                             sorted(row for row in ("independence", "bound", "finiteness", "consistency", "rate")
                                    if row != item_id))
            self.assertEqual(audit["depends_on"], [])
            self.assertEqual(audit["dismissed"], [])
            self.assertEqual(audit["packet"]["item"]["id"], item_id)
            self.assertEqual(audit["packet"]["expected_snapshot"], head)

    def test_scaffold_never_overwrites_existing_audit_files(self):
        database.scaffold_audits(self.db, self.base / "audits")
        with self.assertRaisesRegex(database.DatabaseError, "never overwrites"):
            database.scaffold_audits(self.db, self.base / "audits")

    def test_reconcile_reports_the_four_reconciliation_rows(self):
        folder = self.base / "audits"
        database.scaffold_audits(self.db, folder)
        audit_path = self.fill_consistency_audit(folder)
        result = database.reconcile_audits(self.db, audit_path)
        audit, = result["audits"]
        self.assertEqual(audit["target"], "consistency")
        self.assertEqual([row["candidate"] for row in audit["missing_edge_candidates"]], ["rate"])
        self.assertEqual(audit["missing_edge_candidates"][0]["audit"]["reason"],
                         "The rate display gives the contraction.")
        suspect, = audit["suspect_edges"]
        self.assertEqual(suspect["use"], self.use_id("finiteness", "consistency"))
        self.assertEqual(suspect["dismissal"], "Only supports intuition; not used.")
        agreement, = audit["agreements"]
        self.assertEqual(agreement["candidate"], "bound")
        self.assertEqual(agreement["matched_uses"], [self.use_id("bound", "consistency")])
        self.assertEqual(agreement["other_recorded_uses"], [])
        refinement, = audit["refinements"]
        self.assertEqual(refinement["candidate"], "independence")
        self.assertEqual(refinement["audit"], {"type": "proof_argument", "regime": "decoupling route"})
        self.assertEqual([row["id"] for row in refinement["recorded"]],
                         [self.use_id("independence", "consistency")])
        # The refinement's recorded use has no established disposition: it must
        # stay visible as unresolved coverage, not hide behind pair agreement.
        self.assertEqual(audit["ambiguous_entries"], [])
        unresolved, = audit["unresolved_uses"]
        self.assertEqual(unresolved["use"], self.use_id("independence", "consistency"))
        self.assertEqual(unresolved["from"], "independence")
        self.assertEqual(unresolved["to"], "consistency")
        self.assertEqual(result["counts"], {"audits": 1, "missing_edge_candidates": 1, "suspect_edges": 1,
                                            "agreements": 1, "refinements": 1,
                                            "ambiguous_entries": 0, "unresolved_uses": 1})
        skeleton = result["compare_skeleton"]
        self.assertEqual(skeleton["expected_snapshot"], database.export_snapshot(self.db)["snapshot_id"])
        self.assertEqual(skeleton["targets"], [{"collection": "uses", "id": self.use_id("bound", "consistency")}])
        self.assertEqual(result["unaudited_targets"], ["independence", "bound", "finiteness"])

    def fill_contributions_audit(self, contributions_by_candidate):
        """A filled consistency audit whose depends_on entries use contributions."""
        folder = self.base / "audits"
        database.scaffold_audits(self.db, folder)
        path = folder / "consistency.json"
        audit = json.loads(path.read_text(encoding="utf-8"))
        audit["depends_on"] = [{"id": candidate, "contributions": contributions}
                               for candidate, contributions in contributions_by_candidate]
        disposed = {entry["id"] for entry in audit["depends_on"]}
        audit["dismissed"] = [{"id": row, "note": "Reviewed; not used."}
                              for row in audit["candidates_considered"] if row not in disposed]
        path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
        return path

    def uses_between(self, source, target):
        return [use for use in database.export_snapshot(self.db)["uses"]
                if use["from"] == source and use["to"] == target]

    def add_use(self, identity, record):
        data = database.export_snapshot(self.db)
        database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "uses", "op": "upsert", "id": identity, "record": record}]})

    def test_contributions_name_exact_uses_and_establish_agreements(self):
        path = self.fill_contributions_audit([
            ("bound", [{"use_id": self.use_id("bound", "consistency")}]),
            ("independence", [{"use_id": self.use_id("independence", "consistency")}]),
            ("rate", [{"reason": "The rate display gives the contraction."}]),
        ])
        before = database.export_snapshot(self.db)
        result = database.reconcile_audits(self.db, path)
        report, = result["audits"]
        agreements = {row["candidate"]: row for row in report["agreements"]}
        self.assertEqual(agreements["bound"]["matched_uses"], [self.use_id("bound", "consistency")])
        self.assertEqual(agreements["independence"]["matched_uses"], [self.use_id("independence", "consistency")])
        self.assertEqual(report["refinements"], [])
        self.assertEqual(report["ambiguous_entries"], [])
        self.assertEqual(report["unresolved_uses"], [])
        missing, = report["missing_edge_candidates"]
        self.assertEqual(missing["candidate"], "rate")
        self.assertEqual(missing["audit"], {"reason": "The rate display gives the contraction."})
        self.assertEqual({target["id"] for target in result["compare_skeleton"]["targets"]},
                         {self.use_id("bound", "consistency"), self.use_id("independence", "consistency")})
        self.assertEqual(database.export_snapshot(self.db), before)

    def test_two_contributions_with_the_same_endpoints_do_not_merge(self):
        self.add_use("use-independence-consistency-route",
                     {"from": "independence", "to": "consistency", "regime": "decoupling route",
                      "reason": "The decoupling argument controls the dependence."})
        named = [use["id"] for use in self.uses_between("independence", "consistency")]
        self.assertEqual(len(named), 2)
        path = self.fill_contributions_audit([
            ("bound", [{"use_id": self.use_id("bound", "consistency")}]),
            ("independence", [{"use_id": use_id} for use_id in named]),
        ])
        result = database.reconcile_audits(self.db, path)
        report, = result["audits"]
        agreement, = [row for row in report["agreements"] if row["candidate"] == "independence"]
        self.assertEqual(agreement["matched_uses"], named)
        self.assertEqual(agreement["other_recorded_uses"], [])
        self.assertEqual(report["unresolved_uses"], [])
        skeleton = [target["id"] for target in result["compare_skeleton"]["targets"]]
        self.assertEqual(skeleton.count(named[0]), 1)
        self.assertEqual(skeleton.count(named[1]), 1)

    def test_a_contribution_naming_another_pairs_use_is_an_actionable_error(self):
        path = self.fill_contributions_audit([
            ("bound", [{"use_id": self.use_id("bound", "consistency")}]),
            ("independence", [{"use_id": self.use_id("independence", "bound")}]),
        ])
        with self.assertRaisesRegex(database.DatabaseError, "runs independence . bound, not independence . consistency"):
            database.reconcile_audits(self.db, path)

    def test_a_legacy_entry_matching_two_recorded_uses_is_ambiguous(self):
        self.add_use("use-bound-consistency-second",
                     {"from": "bound", "to": "consistency",
                      "reason": "The same bound enters a second time."})
        folder = self.base / "audits"
        database.scaffold_audits(self.db, folder)
        path = self.fill_consistency_audit(folder)
        result = database.reconcile_audits(self.db, path)
        report, = result["audits"]
        ambiguous, = report["ambiguous_entries"]
        self.assertEqual(ambiguous["candidate"], "bound")
        self.assertEqual(sorted(ambiguous["matched_uses"]),
                         sorted(use["id"] for use in self.uses_between("bound", "consistency")))
        self.assertIn("contributions", ambiguous["note"])
        self.assertEqual(report["agreements"], [])
        # Ambiguity must not hide behind pair-level agreement: with nothing
        # established there is no compare skeleton, and both matched uses stay
        # visible as unresolved coverage.
        self.assertIsNone(result["compare_skeleton"])
        named = {use["id"] for use in self.uses_between("bound", "consistency")}
        unresolved = {row["use"] for row in report["unresolved_uses"]}
        self.assertTrue(named <= unresolved)
        self.assertIn(self.use_id("independence", "consistency"), unresolved)
        self.assertEqual(result["counts"]["ambiguous_entries"], 1)
        self.assertEqual(result["counts"]["unresolved_uses"], 3)

    def test_an_omitted_recorded_use_surfaces_as_unresolved_while_its_sibling_agrees(self):
        self.add_use("use-bound-consistency-route",
                     {"from": "bound", "to": "consistency", "regime": "route B",
                      "reason": "Route B reuses the bound differently."})
        path = self.fill_contributions_audit([
            ("bound", [{"use_id": self.use_id("bound", "consistency")}]),
            ("independence", [{"use_id": self.use_id("independence", "consistency")}]),
        ])
        result = database.reconcile_audits(self.db, path)
        report, = result["audits"]
        agreements = {row["candidate"]: row for row in report["agreements"]}
        self.assertEqual(agreements["bound"]["matched_uses"], [self.use_id("bound", "consistency")])
        self.assertEqual(agreements["bound"]["other_recorded_uses"], ["use-bound-consistency-route"])
        unresolved = {row["use"] for row in report["unresolved_uses"]}
        self.assertEqual(unresolved, {"use-bound-consistency-route"})
        skeleton_ids = {target["id"] for target in result["compare_skeleton"]["targets"]}
        self.assertEqual(skeleton_ids, {self.use_id("bound", "consistency"),
                                        self.use_id("independence", "consistency")})

    def test_a_proposed_new_use_contribution_stays_a_candidate(self):
        path = self.fill_contributions_audit([
            ("bound", [{"use_id": self.use_id("bound", "consistency")}]),
            ("independence", [{"type": "proof_argument", "regime": "decoupling route",
                               "reason": "Propose recording the decoupling reuse."}]),
        ])
        before = database.export_snapshot(self.db)
        result = database.reconcile_audits(self.db, path)
        report, = result["audits"]
        missing, = report["missing_edge_candidates"]
        self.assertEqual(missing["candidate"], "independence")
        self.assertEqual(missing["audit"], {"type": "proof_argument", "regime": "decoupling route",
                                            "reason": "Propose recording the decoupling reuse."})
        # A proposal is not an agreement: the recorded use stays unresolved and
        # the skeleton and the database are untouched.
        self.assertEqual({row["use"] for row in report["unresolved_uses"]},
                         {self.use_id("independence", "consistency")})
        skeleton_ids = {target["id"] for target in result["compare_skeleton"]["targets"]}
        self.assertEqual(skeleton_ids, {self.use_id("bound", "consistency")})
        self.assertEqual(database.export_snapshot(self.db), before)

    def test_reconcile_rejects_malformed_contributions(self):
        use_id = self.use_id("bound", "consistency")

        def attempt(edit):
            folder = self.base / "audits"
            if folder.exists():
                for file in folder.glob("*.json"):
                    file.unlink()
            database.scaffold_audits(self.db, folder)
            path = folder / "consistency.json"
            audit = json.loads(path.read_text(encoding="utf-8"))
            audit["depends_on"] = [{"id": "bound", "contributions": [{"use_id": use_id}]},
                                   {"id": "independence",
                                    "contributions": [{"use_id": self.use_id("independence", "consistency")}]}]
            edit(audit)
            disposed = {entry["id"] for entry in audit["depends_on"]}
            audit["dismissed"] = [{"id": row} for row in audit["candidates_considered"] if row not in disposed]
            path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
            return database.reconcile_audits(self.db, path)

        with self.assertRaisesRegex(database.DatabaseError, "at least one contribution"):
            attempt(lambda audit: audit["depends_on"][0].update(contributions=[]))
        with self.assertRaisesRegex(database.DatabaseError, "empty contribution"):
            attempt(lambda audit: audit["depends_on"][0].update(contributions=[{}]))
        with self.assertRaisesRegex(database.DatabaseError, "use_id stands alone"):
            attempt(lambda audit: audit["depends_on"][0].update(contributions=[{"use_id": use_id, "reason": "Mixed."}]))
        with self.assertRaisesRegex(database.DatabaseError, "already named"):
            attempt(lambda audit: audit["depends_on"][1].update(contributions=[{"use_id": use_id}]))
        with self.assertRaisesRegex(database.DatabaseError, "entry-level"):
            attempt(lambda audit: audit["depends_on"][0].update(type="dependency"))
        with self.assertRaisesRegex(database.DatabaseError, "unknown fields"):
            attempt(lambda audit: audit["depends_on"][0].update(contributions=[{"source": "bound"}]))
        with self.assertRaisesRegex(database.DatabaseError, "not a recorded use"):
            attempt(lambda audit: audit["depends_on"][0].update(contributions=[{"use_id": "phantom-use"}]))
        with self.assertRaisesRegex(database.DatabaseError, "unknown anchor"):
            attempt(lambda audit: audit["depends_on"][0].update(contributions=[{"evidence_refs": ["anchor-phantom"]}]))

    def test_reconcile_rejects_incomplete_or_invalid_dispositions(self):
        folder = self.base / "audits"
        database.scaffold_audits(self.db, folder)
        path = self.fill_consistency_audit(folder)

        def reconcile_with(edit):
            audit = json.loads(path.read_text(encoding="utf-8"))
            edit(audit)
            bad = self.base / "bad-audit.json"
            bad.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
            return database.reconcile_audits(self.db, bad)

        with self.assertRaisesRegex(database.DatabaseError, "not disposed: rate"):
            reconcile_with(lambda audit: audit["depends_on"].pop(1))
        with self.assertRaisesRegex(database.DatabaseError, "exactly once"):
            reconcile_with(lambda audit: audit["dismissed"].append({"id": "bound"}))
        with self.assertRaisesRegex(database.DatabaseError, "not a row"):
            reconcile_with(lambda audit: audit["depends_on"].append({"id": "phantom"}))
        with self.assertRaisesRegex(database.DatabaseError, "unknown anchor"):
            reconcile_with(lambda audit: audit["depends_on"][0].update(evidence_refs=["anchor-phantom"]))
        with self.assertRaisesRegex(database.DatabaseError, "unsupported use type"):
            reconcile_with(lambda audit: audit["depends_on"][0].update(type="citation"))
        with self.assertRaisesRegex(database.DatabaseError, "audit itself"):
            reconcile_with(lambda audit: audit["depends_on"].append({"id": "consistency"}))
        with self.assertRaisesRegex(database.DatabaseError, "not a major row"):
            reconcile_with(lambda audit: audit.update(target="rate"))
        with self.assertRaisesRegex(database.DatabaseError, "re-scaffold"):
            reconcile_with(lambda audit: audit["candidates_considered"].pop())
        with self.assertRaisesRegex(database.DatabaseError, "audit_format"):
            reconcile_with(lambda audit: audit.pop("audit_format"))

    def test_reconcile_rejects_a_stale_audit_and_never_modifies_the_database(self):
        folder = self.base / "audits"
        database.scaffold_audits(self.db, folder)
        path = self.fill_consistency_audit(folder)
        before = database.export_snapshot(self.db)
        database.reconcile_audits(self.db, path)
        self.assertEqual(database.export_snapshot(self.db), before)
        data = database.export_snapshot(self.db)
        item = deepcopy(next(row for row in data["items"] if row["id"] == "bound"))
        item["caption"] = "A clarified caption"
        moved = database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"],
                                               "edits": [{"collection": "items", "op": "upsert", "id": "bound", "record": item}]})
        with self.assertRaisesRegex(database.DatabaseError, "stale audit"):
            database.reconcile_audits(self.db, path)
        self.assertEqual(database.export_snapshot(self.db)["snapshot_id"], moved["snapshot_id"])

    def test_reconcile_a_folder_reports_unaudited_targets_and_duplicate_targets(self):
        folder = self.base / "audits"
        database.scaffold_audits(self.db, folder)
        self.fill_consistency_audit(folder)
        for item_id in ("independence", "bound", "finiteness"):
            path = folder / f"{item_id}.json"
            audit = json.loads(path.read_text(encoding="utf-8"))
            audit["dismissed"] = [{"id": row} for row in audit["candidates_considered"]]
            path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
        result = database.reconcile_audits(self.db, folder)
        self.assertEqual(result["counts"]["audits"], 4)
        self.assertEqual(result["unaudited_targets"], [])
        duplicate = folder / "duplicate.json"
        duplicate.write_text((folder / "bound.json").read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(database.DatabaseError, "duplicate audit"):
            database.reconcile_audits(self.db, folder)

    def test_cli_scaffold_and_reconcile_round_trip(self):
        folder = self.base / "cli-audits"
        run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                              "scaffold", str(self.db), "--output", str(folder)],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(json.loads(run.stdout)["audits"]), 4)
        audit_path = self.fill_consistency_audit(folder)
        run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                              "reconcile", str(self.db), str(audit_path)],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["counts"]["agreements"], 1)


def small_pdf(title):
    """One valid text page; only the metadata title varies between revisions."""
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


class TexSniffingTests(unittest.TestCase):
    """Content-sniffed TeX sources and the not-applicable citation report."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def init_db(self, legacy, extra_files=()):
        dataset = self.base / "overview.json"
        dataset.write_text(json.dumps(legacy), encoding="utf-8")
        db = self.base / "paper.sqlite"
        database.init_database(db, dataset, extra_files=extra_files)
        return db

    def write_md_manuscript(self):
        manuscript = self.base / "paper.md"
        manuscript.write_text(
            "\\documentclass{article}\n"          # 1
            "\\newtheorem{lemma}{Lemma}\n"       # 2
            "\\begin{document}\n"                # 3
            "\\begin{lemma}\\label{lem:step}\n"  # 4
            "The step holds.\n"                  # 5
            "\\end{lemma}\n"                     # 6
            "\\begin{theorem}\\label{thm:main}\n"      # 7
            "The estimator is consistent.\n"     # 8
            "\\end{theorem}\n"                   # 9
            "\\begin{proof}\n"                   # 10
            "Apply \\ref{lem:step}.\n"           # 11
            "\\end{proof}\n"                     # 12
            "\\input{extras}\n"                  # 13
            "\\end{document}\n",                 # 14
            encoding="utf-8",
        )
        (self.base / "extras.tex").write_text(
            "\\begin{corollary}\\label{cor:rate}\n"
            "The rate follows from \\ref{thm:main}.\n"
            "\\end{corollary}\n",
            encoding="utf-8",
        )
        return manuscript

    def md_legacy(self):
        return {
            "schema_version": 3, "title": "Markdown-hosted manuscript",
            "scope": "Synthetic sniffing fixture.",
            "source": {"title": "Manuscript in Markdown clothing", "file": "paper.md"},
            "items": [
                {"id": "step", "kind": "lemma", "label": "Lemma 1", "caption": "The step",
                 "statement": {"text": "The step holds.", "form": "synopsis"},
                 "source": {"label": "lem:step", "start_line": 4, "end_line": 6}},
                {"id": "main", "kind": "theorem", "label": "Theorem 1", "caption": "Consistency",
                 "statement": {"text": "The estimator is consistent.", "form": "synopsis"},
                 "source": {"label": "thm:main", "start_line": 7, "end_line": 9}},
                {"id": "rate", "kind": "corollary", "label": "Corollary 1", "caption": "Rate",
                 "statement": {"text": "The rate follows.", "form": "synopsis"},
                 "source": {"file": "extras.tex", "label": "cor:rate", "start_line": 1, "end_line": 3}},
            ],
            "uses": [
                {"from": "step", "to": "main", "reason": "The step is applied in the proof."},
                {"from": "main", "to": "rate", "reason": "The rate follows from consistency."},
            ],
        }

    def test_md_hosted_manuscript_is_scanned_and_disclosed_once(self):
        self.write_md_manuscript()
        db = self.init_db(self.md_legacy())
        data = database.export_snapshot(db)
        self.assertEqual(sorted(row["path"] for row in data["source_revision"]["files"]),
                         ["extras.tex", "paper.md"])
        inventory = data["inventory"]
        self.assertEqual(len(inventory["declarations"]), 3)
        self.assertEqual({row["kind"] for row in inventory["declarations"]},
                         {"lemma", "theorem", "corollary"})
        disclosure = [note for note in inventory["unresolved"] if "treated as TeX by content" in note]
        self.assertEqual(len(disclosure), 1)
        self.assertTrue(disclosure[0].startswith("paper.md:"))
        report = database.candidates_database(db)
        self.assertEqual(report["coverage"], {"files_scanned": 2, "text_files_total": 2,
                                              "applicable": True, "tex_by_content": ["paper.md"]})
        pairs = {(row["citing"], row["cited"]): row for row in report["pairs"]}
        self.assertEqual(sorted(pairs), [("main", "step"), ("rate", "main")])
        occurrence, = pairs[("main", "step")]["occurrences"]
        self.assertEqual((occurrence["path"], occurrence["line"], occurrence["context"]),
                         ("paper.md", 11, "proof"))
        occurrence, = pairs[("rate", "main")]["occurrences"]
        self.assertEqual((occurrence["path"], occurrence["line"], occurrence["context"]),
                         ("extras.tex", 2, "statement"))
        self.assertEqual(report["missing_uses"], [])
        self.assertEqual(report["unsupported_uses"], [])
        self.assertIsInstance(database.validate_database(db)["citation_candidates"], dict)

    def test_pdf_only_dataset_is_not_applicable_and_scaffold_still_works(self):
        (self.base / "paper.pdf").write_bytes(small_pdf("PDF-only manuscript"))
        legacy = {
            "schema_version": 3, "title": "PDF-only fixture", "scope": "Synthetic PDF fixture.",
            "source": {"title": "PDF manuscript", "file": "paper.pdf"},
            "items": [
                {"id": "helper", "kind": "lemma", "label": "Lemma 1", "caption": "Helper",
                 "statement": {"text": "The helper holds.", "form": "synopsis"}, "source": {"page": 1}},
                {"id": "main", "kind": "theorem", "label": "Theorem 1", "caption": "Main",
                 "statement": {"text": "The main claim holds.", "form": "synopsis"}, "source": {"page": 1}},
            ],
            "uses": [{"from": "helper", "to": "main", "reason": "The helper is applied."}],
        }
        db = self.init_db(legacy)
        report = database.candidates_database(db)
        self.assertEqual(report["coverage"], {"files_scanned": 0, "text_files_total": 0,
                                              "applicable": False, "tex_by_content": []})
        self.assertIn("Not applicable", report["note"])
        # The PDF-only path points to get/compare review and discloses the
        # coverage limit; the edge audit is optional, never required.
        self.assertIn("get packets and compare batches", report["note"])
        self.assertIn("coverage limit", report["note"])
        self.assertIn("scaffold/reconcile", report["note"])
        for key in ("pairs", "missing_uses", "unsupported_uses", "counts"):
            self.assertIsNone(report[key], key)
        self.assertEqual(database.validate_database(db)["citation_candidates"],
                         "not applicable (no TeX sources captured)")
        receipt = database.scaffold_audits(db, self.base / "audits")
        self.assertEqual([Path(path).name for path in receipt["audits"]], ["helper.json", "main.json"])
        self.assertEqual(database.export_snapshot(db)["snapshot_id"], receipt["snapshot_id"])

    def mixed_project(self):
        (self.base / "main.tex").write_text(
            "\\documentclass{article}\n"               # 1
            "\\begin{document}\n"                     # 2
            "\\begin{theorem}\\label{thm:main}\n"     # 3
            "The estimator is consistent.\n"          # 4
            "\\end{theorem}\n"                        # 5
            "\\begin{proof}\n"                        # 6
            "Apply \\ref{lem:step}.\n"                # 7
            "\\end{proof}\n"                          # 8
            "\\end{document}\n",                      # 9
            encoding="utf-8",
        )
        (self.base / "supplement.md").write_text(
            "\\newtheorem{lemma}{Lemma}\n"            # 1
            "\\begin{lemma}\\label{lem:step}\n"       # 2
            "The step holds.\n"                       # 3
            "\\end{lemma}\n",                         # 4
            encoding="utf-8",
        )
        (self.base / "notes.txt").write_text(
            "Meeting notes: the display \\begin{equation} x = 1 \\end{equation} was discussed.\n"
            "We should check \\ref{thm:main} against the draft.\n",
            encoding="utf-8",
        )
        legacy = {
            "schema_version": 3, "title": "Mixed sources fixture", "scope": "Synthetic mixed fixture.",
            "source": {"title": "Mixed manuscript", "file": "main.tex"},
            "items": [
                {"id": "step", "kind": "lemma", "label": "Lemma 1", "caption": "The step",
                 "statement": {"text": "The step holds.", "form": "synopsis"},
                 "source": {"file": "supplement.md", "label": "lem:step", "start_line": 2, "end_line": 4}},
                {"id": "main", "kind": "theorem", "label": "Theorem 1", "caption": "Consistency",
                 "statement": {"text": "The estimator is consistent.", "form": "synopsis"},
                 "source": {"label": "thm:main", "start_line": 3, "end_line": 5}},
            ],
            "uses": [{"from": "step", "to": "main", "reason": "The step is applied in the proof."}],
        }
        return self.init_db(legacy, extra_files=[self.base / "notes.txt"])

    def test_mixed_project_coverage_counts_each_file_correctly(self):
        db = self.mixed_project()
        report = database.candidates_database(db)
        self.assertEqual(report["coverage"], {"files_scanned": 2, "text_files_total": 3,
                                              "applicable": True, "tex_by_content": ["supplement.md"]})
        pairs = {(row["citing"], row["cited"]): row for row in report["pairs"]}
        self.assertEqual(sorted(pairs), [("main", "step")])
        occurrence, = pairs[("main", "step")]["occurrences"]
        self.assertEqual((occurrence["path"], occurrence["line"], occurrence["context"]),
                         ("main.tex", 7, "proof"))
        inventory = database.export_snapshot(db)["inventory"]
        self.assertEqual(len(inventory["declarations"]), 2)
        disclosed = [note for note in inventory["unresolved"] if "treated as TeX by content" in note]
        self.assertEqual(len(disclosed), 1)
        self.assertTrue(disclosed[0].startswith("supplement.md:"))

    def test_isolated_equation_fragment_without_marker_is_not_tex(self):
        db = self.mixed_project()
        report = database.candidates_database(db)
        self.assertNotIn("notes.txt", json.dumps(report))
        data = database.export_snapshot(db)
        inventory = data["inventory"]
        self.assertFalse(any("notes.txt" in note for note in inventory["unresolved"]))
        notes_id = next(row["id"] for row in data["source_revision"]["files"] if row["path"] == "notes.txt")
        self.assertFalse(any(decl["file_id"] == notes_id for decl in inventory["declarations"]))


class ProofSectionAttributionTests(unittest.TestCase):
    """Appendix-organized manuscripts: proof sections attributed by heading."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def build_db(self, manuscript, items, uses):
        (self.base / "main.tex").write_text(manuscript, encoding="utf-8")
        legacy = {"schema_version": 3, "title": "Proof-section fixture",
                  "scope": "Synthetic appendix fixture.",
                  "source": {"title": "Appendix manuscript", "file": "main.tex"},
                  "items": items, "uses": uses}
        dataset = self.base / "overview.json"
        dataset.write_text(json.dumps(legacy), encoding="utf-8")
        db = self.base / "paper.sqlite"
        database.init_database(db, dataset)
        return db

    def use_id(self, db, source, target):
        data = database.export_snapshot(db)
        return next(use["id"] for use in data["uses"] if use["from"] == source and use["to"] == target)

    def without_sections(self):
        """The pre-fix adjacency-only attribution, for before/after pinning."""
        original = records._proof_section_spans
        records._proof_section_spans = lambda text, label_map, suffix_map: []
        return original

    def appendix_db(self):
        manuscript = (
            "\\documentclass{article}\n"            # 1
            "\\begin{document}\n"                  # 2
            "\\begin{assumption}\\label{ass:ind}\n"  # 3
            "The observations are independent.\n"  # 4
            "\\end{assumption}\n"                  # 5
            "\\begin{lemma}\\label{lem:step}\n"    # 6
            "The step holds.\n"                    # 7
            "\\end{lemma}\n"                       # 8
            "\\begin{theorem}\\label{thm:main}\n"  # 9
            "The estimator is consistent.\n"       # 10
            "\\end{theorem}\n"                     # 11
            "The survey discusses \\ref{thm:main}.\n"  # 12
            "\\section{Proofs}\n"                  # 13
            "\\subsection{Proof of Lemma \\ref{lem:step}}\\label{proof:step}\n"   # 14
            "Apply \\ref{ass:ind}.\n"              # 15
            "\\subsection{Proof of Theorem \\ref{thm:main}}\\label{proof:main}\n"  # 16
            "By \\ref{lem:step} the claim follows.\n"  # 17
            "\\section{Discussion}\n"              # 18
            "We revisit \\ref{thm:main} informally.\n"  # 19
            "\\end{document}\n"                    # 20
        )
        items = [
            {"id": "ind", "kind": "assumption", "label": "Assumption 1", "caption": "Independence",
             "statement": {"text": "The observations are independent.", "form": "synopsis"},
             "source": {"label": "ass:ind", "start_line": 3, "end_line": 5}},
            {"id": "step", "kind": "lemma", "label": "Lemma 1", "caption": "The step",
             "statement": {"text": "The step holds.", "form": "synopsis"},
             "source": {"label": "lem:step", "start_line": 6, "end_line": 8}},
            {"id": "main", "kind": "theorem", "label": "Theorem 1", "caption": "Consistency",
             "statement": {"text": "The estimator is consistent.", "form": "synopsis"},
             "source": {"label": "thm:main", "start_line": 9, "end_line": 11}},
        ]
        uses = [
            {"from": "ind", "to": "step", "reason": "Independence controls the summands."},
            {"from": "step", "to": "main", "reason": "The step enters the main proof."},
            {"from": "ind", "to": "main", "reason": "Independence also enters the main proof."},
        ]
        return self.build_db(manuscript, items, uses)

    def test_proof_of_headings_attribute_their_sections(self):
        db = self.appendix_db()
        report = database.candidates_database(db)
        pairs = {(row["citing"], row["cited"]): row for row in report["pairs"]}
        self.assertEqual(sorted(pairs), [("main", "step"), ("step", "ind")])
        occurrence, = pairs[("step", "ind")]["occurrences"]
        self.assertEqual((occurrence["line"], occurrence["command"], occurrence["context"]),
                         (15, "\\ref", "proof"))
        self.assertEqual(pairs[("step", "ind")]["recorded_uses"], [self.use_id(db, "ind", "step")])
        occurrence, = pairs[("main", "step")]["occurrences"]
        self.assertEqual((occurrence["line"], occurrence["context"]), (17, "proof"))
        self.assertEqual(pairs[("main", "step")]["recorded_uses"], [self.use_id(db, "step", "main")])
        # The use whose only evidence lives in an appendix proof section is no
        # longer flagged; the use without evidence stays flagged.
        self.assertEqual([row["id"] for row in report["unsupported_uses"]],
                         [self.use_id(db, "ind", "main")])
        # The heading \ref keys are self-citations and never form pairs.
        self.assertNotIn(("step", "step"), pairs)
        self.assertNotIn(("main", "main"), pairs)
        # A later same-or-higher heading ends the proof section: the Discussion
        # mention stays narrative, like the main-text survey mention.
        narrative = report["unattributed_occurrences"]
        self.assertEqual([(o["line"], o["cited"], o["context"]) for o in narrative],
                         [(12, "main", "narrative"), (19, "main", "narrative")])
        # Under the pre-fix adjacency-only rule both pairs were invisible and
        # every use was flagged unsupported.
        original = self.without_sections()
        try:
            before = database.candidates_database(db)
        finally:
            records._proof_section_spans = original
        self.assertEqual(before["pairs"], [])
        self.assertEqual(sorted(row["id"] for row in before["unsupported_uses"]),
                         sorted(self.use_id(db, source, target)
                                for source, target in (("ind", "step"), ("step", "main"), ("ind", "main"))))
        self.assertEqual(before["attribution"], {"attributed": 0, "unattributed": 6})

    def test_attribution_coverage_counts_sum_over_matched_occurrences(self):
        db = self.appendix_db()
        report = database.candidates_database(db)
        self.assertEqual(report["attribution"], {"attributed": 2, "unattributed": 2})
        self.assertEqual(report["attribution"]["attributed"] + report["attribution"]["unattributed"],
                         report["counts"]["occurrences"])
        self.assertIn("2 matched occurrences are unattributed, mostly main-text narrative "
                      "or unmarked proof sections; they were not used to form pairs.", report["note"])

    def test_proof_label_convention_attributes_without_a_heading_ref(self):
        manuscript = (
            "\\documentclass{article}\n"            # 1
            "\\begin{document}\n"                  # 2
            "\\begin{assumption}\\label{ass:ind}\n"  # 3
            "The observations are independent.\n"  # 4
            "\\end{assumption}\n"                  # 5
            "\\begin{lemma}\\label{lem:step}\n"    # 6
            "The step holds.\n"                    # 7
            "\\end{lemma}\n"                       # 8
            "\\begin{theorem}\\label{thm:main}\n"  # 9
            "The estimator is consistent.\n"       # 10
            "\\end{theorem}\n"                     # 11
            "\\section{Proofs}\n"                  # 12
            "\\subsection{The main argument}\\label{proof:main}\n"   # 13
            "The claim reduces to \\ref{lem:step}.\n"                # 14
            "\\subsection{A helper fact}\\label{proof-step}\n"       # 15
            "The fact applies \\ref{ass:ind}.\n"                     # 16
            "\\end{document}\n"                    # 17
        )
        items = [
            {"id": "ind", "kind": "assumption", "label": "Assumption 1", "caption": "Independence",
             "statement": {"text": "The observations are independent.", "form": "synopsis"},
             "source": {"label": "ass:ind", "start_line": 3, "end_line": 5}},
            {"id": "step", "kind": "lemma", "label": "Lemma 1", "caption": "The step",
             "statement": {"text": "The step holds.", "form": "synopsis"},
             "source": {"label": "lem:step", "start_line": 6, "end_line": 8}},
            {"id": "main", "kind": "theorem", "label": "Theorem 1", "caption": "Consistency",
             "statement": {"text": "The estimator is consistent.", "form": "synopsis"},
             "source": {"label": "thm:main", "start_line": 9, "end_line": 11}},
        ]
        uses = [
            {"from": "step", "to": "main", "reason": "The step enters the main proof."},
            {"from": "ind", "to": "step", "reason": "Independence controls the summands."},
        ]
        db = self.build_db(manuscript, items, uses)
        report = database.candidates_database(db)
        pairs = {(row["citing"], row["cited"]): row for row in report["pairs"]}
        self.assertEqual(sorted(pairs), [("main", "step"), ("step", "ind")])
        occurrence, = pairs[("main", "step")]["occurrences"]
        self.assertEqual((occurrence["line"], occurrence["context"]), (14, "proof"))
        occurrence, = pairs[("step", "ind")]["occurrences"]
        self.assertEqual((occurrence["line"], occurrence["context"]), (16, "proof"))
        self.assertEqual(report["unsupported_uses"], [])
        self.assertEqual(report["attribution"], {"attributed": 2, "unattributed": 0})
        self.assertNotIn("unattributed", report["note"])

    def test_heading_attribution_outranks_whitespace_adjacency(self):
        manuscript = (
            "\\documentclass{article}\n"            # 1
            "\\begin{document}\n"                  # 2
            "\\begin{assumption}\\label{ass:ind}\n"  # 3
            "The observations are independent.\n"  # 4
            "\\end{assumption}\n"                  # 5
            "\\begin{theorem}\\label{thm:main}\n"  # 6
            "The estimator is consistent.\n"       # 7
            "\\end{theorem}\n"                     # 8
            "\\section{Proofs}\n"                  # 9
            "\\subsection{Proof of Theorem \\ref{thm:main}}\\label{proof:main}\n"  # 10
            "\\begin{lemma}\\label{lem:restated}\n"  # 11
            "The helper restated.\n"               # 12
            "\\end{lemma}\n"                       # 13
            "\\begin{proof}\n"                     # 14
            "By \\ref{ass:ind}.\n"                 # 15
            "\\end{proof}\n"                       # 16
            "\\end{document}\n"                    # 17
        )
        items = [
            {"id": "ind", "kind": "assumption", "label": "Assumption 1", "caption": "Independence",
             "statement": {"text": "The observations are independent.", "form": "synopsis"},
             "source": {"label": "ass:ind", "start_line": 3, "end_line": 5}},
            {"id": "main", "kind": "theorem", "label": "Theorem 1", "caption": "Consistency",
             "statement": {"text": "The estimator is consistent.", "form": "synopsis"},
             "source": {"label": "thm:main", "start_line": 6, "end_line": 8}},
            {"id": "restated", "kind": "lemma", "label": "Lemma 1", "caption": "Restated helper",
             "statement": {"text": "The helper restated.", "form": "synopsis"},
             "source": {"label": "lem:restated", "start_line": 11, "end_line": 13}},
        ]
        uses = [
            {"from": "ind", "to": "main", "reason": "Independence enters the main proof."},
            {"from": "ind", "to": "restated", "reason": "Independence enters the restated proof."},
        ]
        db = self.build_db(manuscript, items, uses)
        report = database.candidates_database(db)
        # The proof environment is whitespace-adjacent to the restated lemma,
        # but the enclosing "Proof of Theorem \ref{thm:main}" heading wins.
        pairs = {(row["citing"], row["cited"]): row for row in report["pairs"]}
        self.assertEqual(sorted(pairs), [("main", "ind")])
        occurrence, = pairs[("main", "ind")]["occurrences"]
        self.assertEqual((occurrence["line"], occurrence["context"]), (15, "proof"))
        self.assertEqual([row["id"] for row in report["unsupported_uses"]],
                         [self.use_id(db, "ind", "restated")])
        # Adjacency alone would have claimed the proof for the restated lemma.
        original = self.without_sections()
        try:
            before = database.candidates_database(db)
        finally:
            records._proof_section_spans = original
        self.assertEqual([(row["citing"], row["cited"]) for row in before["pairs"]],
                         [("restated", "ind")])


class ProofPassageFallbackTests(unittest.TestCase):
    """Unmarked proof sections: authored proof-passage ranges recover ownership.

    Plain-text "Proof of Theorem 1" headings carry no \ref and no proof: label,
    and non-whitespace between the declaration and the proof defeats adjacency,
    so source parsing leaves the proof unattributed. The fallback consults only
    recorded proof-role passage ranges and only when one row qualifies.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        manuscript = (
            "\\documentclass{article}\n"            # 1
            "\\begin{document}\n"                  # 2
            "\\begin{lemma}\\label{lem:step}\n"    # 3
            "The step holds.\n"                    # 4
            "\\end{lemma}\n"                       # 5
            "\\begin{theorem}\\label{thm:main}\n"  # 6
            "The estimator is consistent.\n"       # 7
            "\\end{theorem}\n"                     # 8
            "The argument is deferred.\n"          # 9
            "\\section{Proofs}\n"                  # 10
            "\\subsection{Proof of Theorem 1}\n"   # 11  plain-text heading: no \ref, no proof: label
            "\\begin{proof}\n"                     # 12
            "By \\ref{lem:step} and \\ref{thm:main} the claim follows.\n"  # 13
            "\\end{proof}\n"                       # 14
            "\\end{document}\n"                    # 15
        )
        items = [
            {"id": "step", "kind": "lemma", "label": "Lemma 1", "caption": "The step",
             "statement": {"text": "The step holds.", "form": "synopsis"},
             "source": {"label": "lem:step", "start_line": 3, "end_line": 5}},
            {"id": "main", "kind": "theorem", "label": "Theorem 1", "caption": "Consistency",
             "statement": {"text": "The estimator is consistent.", "form": "synopsis"},
             "source": {"label": "thm:main", "start_line": 6, "end_line": 8}},
        ]
        uses = [{"from": "step", "to": "main", "reason": "The step enters the main proof."}]
        (self.base / "main.tex").write_text(manuscript, encoding="utf-8")
        seed = {"schema_version": 3, "title": "Proof-passage fallback fixture",
                "scope": "Synthetic unmarked-proof fixture.",
                "source": {"title": "Synthetic manuscript", "file": "main.tex"},
                "items": items, "uses": uses}
        self.dataset = self.base / "overview.json"
        self.dataset.write_text(json.dumps(seed), encoding="utf-8")
        self.db = self.base / "paper.sqlite"
        database.init_database(self.db, self.dataset)

    def tearDown(self):
        self.temporary.cleanup()

    def add_proof_passage(self, item_id, anchor_id, start, end, role="proof"):
        data = database.export_snapshot(self.db)
        file_id = data["source_revision"]["files"][0]["id"]
        item = next(row for row in data["items"] if row["id"] == item_id)
        edited = deepcopy(item)
        edited["passages"] = edited["passages"] + [{"role": role, "anchor_id": anchor_id}]
        database.apply_edits(self.db, {"expected_snapshot": data["snapshot_id"], "edits": [
            {"collection": "anchors", "op": "upsert", "id": anchor_id,
             "record": {"file_id": file_id, "locator": {"start_line": start, "end_line": end}}},
            {"collection": "items", "op": "upsert", "id": item_id, "record": edited},
        ]})

    def use_id(self, source, target):
        data = database.export_snapshot(self.db)
        return next(use["id"] for use in data["uses"] if use["from"] == source and use["to"] == target)

    def test_unmarked_proof_is_unattributed_without_authored_ranges(self):
        report = database.candidates_database(self.db)
        self.assertEqual(report["pairs"], [])
        self.assertEqual([(o["line"], o["cited"], o["context"]) for o in report["unattributed_occurrences"]],
                         [(13, "step", "proof"), (13, "main", "proof")])

    def test_authored_proof_range_recovers_the_unique_owner(self):
        self.add_proof_passage("main", "anchor-proof-main", 12, 14)
        report = database.candidates_database(self.db)
        pairs = {(row["citing"], row["cited"]): row for row in report["pairs"]}
        self.assertEqual(sorted(pairs), [("main", "step")])
        occurrence, = pairs[("main", "step")]["occurrences"]
        self.assertEqual((occurrence["line"], occurrence["context"], occurrence["attribution"]),
                         (13, "proof", "proof_passage"))
        self.assertEqual(pairs[("main", "step")]["recorded_uses"], [self.use_id("step", "main")])
        # The recovered pair corroborates the recorded use; the self-citation of
        # thm:main inside its own proof passage is excluded, not listed.
        self.assertEqual(report["unsupported_uses"], [])
        self.assertEqual(report["unattributed_occurrences"], [])
        self.assertIn("proof-passage ranges", report["note"])
        self.assertEqual(report["attribution"], {"attributed": 1, "unattributed": 0})

    def test_overlapping_proof_ranges_stay_unresolved(self):
        self.add_proof_passage("main", "anchor-proof-main", 12, 14)
        self.add_proof_passage("step", "anchor-proof-step", 10, 15)
        report = database.candidates_database(self.db)
        self.assertEqual(report["pairs"], [])
        self.assertEqual(len(report["unattributed_occurrences"]), 2)

    def test_a_statement_range_outranks_the_enclosing_proof_range(self):
        # The lemma's own statement passage overlaps the unmarked proof lines
        # (recorded too broadly); an enclosing proof range must not steal it.
        self.add_proof_passage("main", "anchor-proof-main", 12, 14)
        self.add_proof_passage("step", "anchor-step-wide", 12, 14, role="statement")
        report = database.candidates_database(self.db)
        self.assertEqual(report["pairs"], [])
        self.assertEqual([(o["line"], o["context"]) for o in report["unattributed_occurrences"]],
                         [(13, "proof"), (13, "proof")])

    def prose_proof_records(self, heading, *, declaration=False):
        manuscript = self.base / "main.tex"
        lines = manuscript.read_text(encoding="utf-8").splitlines()
        lines[10] = heading
        lines[11] = r"\begin{lemma}" if declaration else "The written argument follows."
        lines[13] = r"\end{lemma}" if declaration else "This completes the argument."
        manuscript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        data = records.refresh_sources(database.export_snapshot(self.db), self.base)
        return self.with_passage(data, "main", "proof-main", "proof")

    def with_passage(self, data, item_id, anchor_id, role):
        data = deepcopy(data)
        data["anchors"].append(records.make_anchor(
            data, {"start_line": 12, "end_line": 14},
            file_id=data["source_revision"]["files"][0]["id"], identity=anchor_id))
        item = next(row for row in data["items"] if row["id"] == item_id)
        item["passages"].append({"role": role, "anchor_id": anchor_id})
        data.pop("snapshot_id", None)
        return records.validate_records(data)

    def test_paragraph_and_plain_proof_ranges_recover_owner_without_heading_guess(self):
        for heading in (r"\paragraph{Argument for the main result}", "Argument for the main result."):
            with self.subTest(heading=heading):
                data = self.prose_proof_records(heading)
                before = deepcopy(data)
                report = records.citation_candidates(data)
                self.assertEqual(data, before)
                self.assertEqual([(p["citing"], p["cited"]) for p in report["pairs"]],
                                 [("main", "step")])
                self.assertEqual(report["pairs"][0]["occurrences"][0]["attribution"], "proof_passage")
                self.assertEqual(report["unattributed_occurrences"], [])
                self.assertEqual(report["unsupported_uses"], [])
                self.assertEqual(report["attribution"]["attributed"], 1)  # self-citation excluded

    def test_prose_proof_range_does_not_steal_unmapped_declaration(self):
        data = self.prose_proof_records("An intervening declaration.", declaration=True)
        report = records.citation_candidates(data)
        self.assertEqual(report["pairs"], [])
        self.assertEqual([o["context"] for o in report["unattributed_occurrences"]],
                         ["statement", "statement"])

    def test_prose_proof_conflicting_ranges_stay_unattributed(self):
        data = self.prose_proof_records(r"\paragraph{Argument}")
        data = self.with_passage(data, "step", "proof-step", "proof")
        report = records.citation_candidates(data)
        self.assertEqual(report["pairs"], [])
        self.assertEqual(len(report["unattributed_occurrences"]), 2)

    def test_prose_statement_passage_blocks_an_enclosing_proof_range(self):
        data = self.prose_proof_records("An unnumbered argument.")
        data = self.with_passage(data, "step", "statement-step", "statement")
        report = records.citation_candidates(data)
        self.assertEqual(report["pairs"], [])
        self.assertEqual(len(report["unattributed_occurrences"]), 2)

    def test_validate_and_render_context_disclose_scan_coverage(self):
        self.add_proof_passage("main", "anchor-proof-main", 12, 14)
        summary = database.validate_database(self.db)["citation_candidates"]
        self.assertEqual(summary["attributed"], 1)
        self.assertEqual(summary["unattributed"], 0)
        self.assertEqual(summary["missing_uses"], 0)
        self.assertEqual(summary["pairs"], 1)
        self.assertEqual(summary["not_mechanically_matchable"], 0)
        data = database.export_snapshot(self.db)
        prepared = records.prepare_records(data, self.base)
        self.assertEqual(prepared["build_context"]["citation_candidates"]["attributed"], 1)


if __name__ == "__main__":
    unittest.main()

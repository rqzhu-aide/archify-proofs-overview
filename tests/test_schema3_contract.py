"""Native schema-3 capture, record contract, and database identity.

The fixtures are synthetic TeX sources and deliberately invalid databases; they
assert the record contract and storage integrity, not any mathematics.
"""
from __future__ import annotations

from copy import deepcopy
import io
import json
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
import proof_overview as overview


NODE_AVAILABLE = shutil.which("node") is not None


class Schema3ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.main = self.base / "main.tex"
        self.main.write_text(
            "\\begin{assumption}\\label{ass:independence}\n"
            "The observations are independent.\n"
            "\\end{assumption}\n"
            "\\begin{theorem}\\label{thm:center}\n"
            "For deterministic centers, the target is $\\mu$.\n"
            "\\end{theorem}\n"
            "A first display follows.\n"
            "\\begin{equation}\\label{eq:scale}\n"
            "s_n^2 = n^{-1}\\sum_i X_i^2\n"
            "\\end{equation}\n"
            "\\begin{equation}\\label{eq:rate}\n"
            "r_n = n^{-1/2}\n"
            "\\end{equation}\n"
            "\\begin{equation}\n"
            "e_n \\to 0\n"
            "\\end{equation}\n"
            "\\begin{proof}Apply the independence assumption.\\end{proof}\n",
            encoding="utf-8",
        )
        self.seed = {
            "schema_version": 3,
            "title": "Schema 3 contract fixture",
            "scope": "Two declarations plus display environments.",
            "source": {"title": "Synthetic manuscript", "file": "main.tex"},
            "items": [
                {"id": "independence", "kind": "assumption", "label": "Assumption 1",
                 "caption": "Independent observations",
                 "statement": {"text": "The observations are independent.", "form": "synopsis"},
                 "source": {"label": "ass:independence", "start_line": 1, "end_line": 3}},
                {"id": "centering", "kind": "theorem", "label": "Theorem 1",
                 "caption": "Deterministic centering",
                 "statement": {"text": "For deterministic centers, the target is $\\mu$.", "form": "synopsis"},
                 "source": {"label": "thm:center", "start_line": 4, "end_line": 6}},
            ],
            "uses": [{
                "from": "independence", "to": "centering",
                "reason": "Apply independence to the centered sum.",
                "source": {"start_line": 17, "end_line": 17},
            }],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def migrated(self):
        return records.normalize(self.seed, self.base)

    def revalidate(self, data):
        candidate = deepcopy(data)
        candidate.pop("snapshot_id", None)
        return records.validate_records(candidate)

    def add_intermediate(self, data, identity="claim-key-step", kind="claim", owner="centering"):
        candidate = deepcopy(data)
        row = {"id": identity, "kind": kind, "label": "Key step", "caption": "A proved inline step",
               "statement": {"text": "The centered sum has bounded variance.", "form": "synopsis"},
               "passages": [{"role": "evidence", "anchor_id": "anchor-item-centering"}]}
        if owner is not None:
            row["owner"] = owner
        candidate["items"].append(row)
        return self.revalidate(candidate)

    def compare_all(self, data):
        candidate = deepcopy(data)
        targets = [{"collection": group, "id": row["id"]}
                   for group in ("items", "uses") for row in candidate[group]]
        candidate["observations"].extend(records.make_observations(
            candidate, targets, reviewer="Synthetic source reviewer",
            note="Compared the bounded synthetic passages and applicable conditions.",
        ))
        return records.validate_records(candidate)

    def test_intermediate_kinds_require_an_existing_major_owner(self):
        migrated = self.migrated()
        for kind in ("equation", "claim", "derivation"):
            with self.subTest(kind=kind):
                data = self.add_intermediate(migrated, identity=f"key-{kind}", kind=kind)
                self.assertEqual(data["items"][-1]["owner"], "centering")
        with self.assertRaisesRegex(records.RecordError, "owner"):
            self.add_intermediate(migrated, owner=None)
        with self.assertRaisesRegex(records.RecordError, "owner"):
            self.add_intermediate(migrated, owner="missing-row")
        owned = self.add_intermediate(migrated)
        with self.assertRaisesRegex(records.RecordError, "major"):
            self.add_intermediate(owned, identity="claim-nested", owner="claim-key-step")

    def test_major_items_do_not_take_an_owner(self):
        migrated = self.migrated()
        owned = deepcopy(migrated)
        owned["items"][0]["owner"] = "centering"
        with self.assertRaisesRegex(records.RecordError, "owner"):
            self.revalidate(owned)
        nulled = deepcopy(migrated)
        nulled["items"][0]["owner"] = None
        self.assertEqual(self.revalidate(nulled)["items"][0]["owner"], None)

    def test_removing_an_owning_major_row_is_rejected(self):
        data = self.add_intermediate(self.migrated())
        data["items"] = [row for row in data["items"] if row["id"] != "centering"]
        data["uses"] = []
        with self.assertRaisesRegex(records.RecordError, "owner"):
            self.revalidate(data)

    def test_group_kind_is_consistent_across_all_uses(self):
        migrated = self.migrated()

        def with_use(identity, group):
            candidate = deepcopy(migrated)
            candidate["uses"][0]["group"] = {"id": "route-a", "kind": "joint"}
            row = deepcopy(candidate["uses"][0])
            row["id"] = identity
            if group is not None:
                row["group"] = group
            candidate["uses"].append(row)
            return self.revalidate(candidate)

        grouped = with_use("use-second-route", {"id": "route-a", "kind": "joint"})
        self.assertEqual([row["group"] for row in grouped["uses"]],
                         [{"id": "route-a", "kind": "joint"}] * 2)
        with self.assertRaisesRegex(records.RecordError, "consistent kind"):
            with_use("use-second-route", {"id": "route-a", "kind": "cases"})
        with self.assertRaisesRegex(records.RecordError, "joint or cases"):
            with_use("use-third-route", {"id": "route-b", "kind": "alternative"})
        with self.assertRaisesRegex(records.RecordError, "missing"):
            with_use("use-third-route", {"id": "route-b"})

    def test_equation_environments_never_enter_the_declaration_inventory(self):
        migrated = self.migrated()
        declarations = migrated["inventory"]["declarations"]
        self.assertEqual(len(declarations), 2)
        self.assertEqual({row["kind"] for row in declarations}, {"assumption", "theorem"})
        self.assertTrue(all(row["end_line"] <= 6 for row in declarations))

    def test_heading_classification_uses_the_ordered_major_kinds(self):
        text = self.main.read_text(encoding="utf-8")
        self.main.write_text(text + "\\newtheorem{hybrid}{Theorem and Lemma}\n"
                                    "\\begin{hybrid}A hybrid statement.\\end{hybrid}\n",
                             encoding="utf-8")
        migrated = self.migrated()
        last_line = len(self.main.read_text(encoding="utf-8").splitlines())
        declaration = next(row for row in migrated["inventory"]["declarations"]
                           if row["start_line"] == last_line)
        self.assertEqual(declaration["kind"], "lemma")

    def test_main_items_select_major_items_only(self):
        data = self.add_intermediate(self.migrated())
        candidate = deepcopy(data)
        candidate["main_items"] = ["claim-key-step"]
        with self.assertRaisesRegex(records.RecordError, "major"):
            self.revalidate(candidate)
        data["main_items"] = ["centering"]
        self.assertEqual(self.revalidate(data)["main_items"], ["centering"])

    def test_uses_without_located_evidence_get_one_aggregated_warning(self):
        migrated = self.migrated()
        for identity in ("use-unlocated", "use-unlocated-2"):
            row = deepcopy(migrated["uses"][0])
            row["id"] = identity
            row["evidence_refs"] = []
            migrated["uses"].append(row)
        data = self.revalidate(migrated)
        prepared = records.prepare_records(data, self.base)
        self.assertEqual(prepared["uses_without_evidence"], ["use-unlocated", "use-unlocated-2"])
        warning = [text for text in prepared["warnings"] if "no located evidence passages" in text]
        self.assertEqual(len(warning), 1)
        self.assertIn("2 uses have no located evidence passages", warning[0])

    def test_explicit_null_optional_fields_are_digest_neutral(self):
        data = self.add_intermediate(self.migrated())
        before = {key: records.target_digest(data, *key)
                  for key in (("items", "centering"), ("items", "independence"), ("uses", data["uses"][0]["id"]))}
        nulled = deepcopy(data)
        nulled["items"][0]["owner"] = None
        nulled["items"][1]["issue"] = None
        nulled["uses"][0]["group"] = None
        nulled = self.revalidate(nulled)
        after = {key: records.target_digest(nulled, *key) for key in before}
        self.assertEqual(before, after)

    def test_editing_an_intermediate_stales_the_owner_comparison(self):
        reviewed = self.compare_all(self.add_intermediate(self.migrated()))
        self.assertEqual(records.comparison_status(reviewed)["matched"], 4)
        edited = deepcopy(reviewed)
        row = next(item for item in edited["items"] if item["id"] == "claim-key-step")
        row["statement"]["text"] = "The centered sum has bounded third moments."
        state = records.comparison_status(self.revalidate(edited))
        self.assertEqual(state["stale"], 2)
        self.assertEqual(state["matched"], 2)
        self.assertNotEqual(records.target_digest(edited, "items", "centering"),
                            records.target_digest(reviewed, "items", "centering"))
        self.assertEqual(records.target_digest(edited, "items", "independence"),
                         records.target_digest(reviewed, "items", "independence"))


class NativeSeedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
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
        self.seed = {
            "schema_version": 3, "title": "Variance structure", "scope": "A synthetic seed fixture.",
            "source": {"title": "Synthetic variance argument", "file": "paper.tex"},
            "main_items": ["variance"],
            "items": [
                {"id": "sampling", "kind": "assumption", "label": "Assumption 1", "caption": "Independent observations",
                 "statement": {"text": "The observations are independent.", "form": "synopsis"},
                 "source": {"start_line": 1, "end_line": 3, "label": "ass:independence"}},
                {"id": "variance", "kind": "theorem", "label": "Theorem 1", "caption": "Variance of a sum",
                 "statement": {"text": "The variance of the sum equals the sum of variances.", "form": "synopsis"},
                 "source": {"start_line": 4, "end_line": 6, "label": "thm:variance"},
                 "issue": "Does the variance formula need finite fourth moments?"},
            ],
            "uses": [{"from": "sampling", "to": "variance", "reason": "Independence removes the covariance terms.",
                      "issue": "The proof may rely on the pilot regime.",
                      "source": {"start_line": 7, "end_line": 7}}],
        }
        self.dataset = self.base / "overview.json"
        self.dataset.write_text(json.dumps(self.seed), encoding="utf-8")
        self.db = self.base / "paper.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def write_seed(self, seed=None):
        self.dataset.write_text(json.dumps(self.seed if seed is None else seed), encoding="utf-8")

    def test_native_seed_captures_all_supported_authored_features_without_rendering(self):
        self.seed["items"][0]["aliases"] = ["ass:iid", "Independent sample"]
        self.seed["items"][1]["statement"]["form"] = "transcription"
        self.seed["items"][1]["passages"] = [{"role": "proof", "source": {"start_line": 7, "end_line": 7}}]
        self.seed["items"].append({
            "id": "covariance-step", "kind": "equation", "owner": "variance",
            "label": "Covariance identity", "caption": "Vanishing cross terms",
            "statement": {"form": "verbatim", "text": "$\\operatorname{Cov}(X_i,X_j)=0$."},
            "passages": [{"role": "evidence", "source": {"start_line": 7, "end_line": 7}}],
        })
        self.seed["uses"][0]["group"] = {"id": "variance-premises", "kind": "joint"}
        self.seed["uses"][0]["regime"] = "Finite second moments"
        parallel = deepcopy(self.seed["uses"][0])
        parallel["type"] = "proof_argument"
        parallel["reason"] = "Reuse the covariance argument."
        self.seed["uses"].append(parallel)
        self.seed["uses"].append({"id": "step-contributes", "from": "covariance-step", "to": "variance",
                                  "reason": "Sum the cross terms.", "group": {"id": "variance-premises", "kind": "joint"}})
        self.write_seed()
        original = deepcopy(self.seed)
        with patch.object(records, "render_text", side_effect=AssertionError("capture must not render math")):
            first = records.normalize(self.seed, self.base)
            second = records.normalize(self.seed, self.base)
            receipt = database.init_database(self.db, self.dataset)
            validation = database.validate_database(self.db)
            with patch.object(sys, "argv", ["proof_overview.py", "validate", str(self.dataset)]), \
                    patch.object(sys, "stdout", new_callable=io.StringIO) as output:
                self.assertEqual(overview.main(), 0)
                self.assertTrue(json.loads(output.getvalue())["valid"])
        self.assertEqual(self.seed, original)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(receipt["snapshot_id"], first["snapshot_id"])
        self.assertEqual(first["observations"], [])
        self.assertEqual(validation["source_comparison"]["unreviewed"], 6)
        self.assertEqual(first["items"][0]["aliases"], self.seed["items"][0]["aliases"])
        self.assertEqual(first["items"][1]["statement"]["form"], "transcription")
        self.assertEqual([p["role"] for p in first["items"][1]["passages"]], ["statement", "proof"])
        self.assertEqual(first["items"][2]["owner"], "variance")
        self.assertEqual(first["uses"][0]["issue"], self.seed["uses"][0]["issue"])
        self.assertEqual(first["uses"][0]["regime"], "Finite second moments")
        self.assertEqual(len({row["id"] for row in first["uses"]}), 3)
        self.assertEqual(first["uses"][2]["id"], "step-contributes")
        self.assertEqual(first["uses"][2]["evidence_refs"], [])
        self.assertTrue(all(row["verification"]["status"] == "checked" for row in first["anchors"]))

    def test_export_round_trip_preserves_observations_without_creating_reviews(self):
        database.init_database(self.db, self.dataset)
        before = database.export_snapshot(self.db)
        database.compare_records(self.db, {"expected_snapshot": before["snapshot_id"],
                                           "targets": [{"collection": "items", "id": "variance"}],
                                           "reviewer": "source reader", "note": "Compared the captured statement."})
        reviewed = database.export_snapshot(self.db)
        self.write_seed(reviewed)
        imported = self.base / "round-trip.sqlite"
        database.init_database(imported, self.dataset)
        self.assertEqual(database.export_snapshot(imported), reviewed)
        self.assertEqual(records.comparison_status(reviewed)["matched"], 1)

    def test_compact_seed_rejects_bookkeeping_and_unstructured_statements(self):
        variants = []
        for field, value in (("source_revision", {}), ("observations", []), ("anchors", [])):
            seed = deepcopy(self.seed)
            seed[field] = value
            variants.append(seed)
        seed = deepcopy(self.seed)
        seed["items"][0]["statement"] = "The observations are independent."
        variants.append(seed)
        seed = deepcopy(self.seed)
        seed["items"][0]["uncertainty"] = "An obsolete field name."
        variants.append(seed)
        for seed in variants:
            with self.subTest(fields=list(seed)):
                self.write_seed(seed)
                with self.assertRaises(records.RecordError):
                    database.init_database(self.db, self.dataset)
                self.assertFalse(self.db.exists())

    def test_non_native_versions_are_refused_before_source_reads_or_database_creation(self):
        for version in (1, 2, 3.0, True, None):
            seed = deepcopy(self.seed)
            seed["schema_version"] = version
            seed["source"]["file"] = "missing-source.tex"
            self.write_seed(seed)
            with self.subTest(version=version):
                with self.assertRaisesRegex(records.RecordError, "schema_version must be 3"):
                    database.init_database(self.db, self.dataset)
                self.assertFalse(self.db.exists())
        native = records.normalize(self.seed, self.base)
        for version in (1, 2, 3.0, True):
            native["schema_version"] = version
            with self.assertRaisesRegex(records.RecordError, "schema_version must be 3"):
                records.normalize(native, self.base)

    def test_old_database_commands_refuse_without_modification(self):
        database._native_init_database(self.db, self.dataset)
        head = database.export_snapshot(self.db)["snapshot_id"]
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                payload = json.loads(connection.execute("SELECT payload FROM snapshots WHERE id=?", (head,)).fetchone()[0])
                payload["schema_version"] = 2
                connection.execute("UPDATE snapshots SET payload=? WHERE id=?", (json.dumps(payload), head))
        finally:
            connection.close()
        unchanged = self.db.read_bytes()
        output = self.base / "refused.html"
        backup = self.base / "refused.sqlite"
        actions = [lambda: database.export_snapshot(self.db),
                   lambda: database.validate_database(self.db),
                   lambda: database.apply_edits(self.db, {"expected_snapshot": head, "edits": []}),
                   lambda: database.compare_records(self.db, {"expected_snapshot": head}),
                   lambda: database.refresh_database(self.db, head),
                   lambda: database.render_database(self.db, output),
                   lambda: database.backup_database(self.db, backup)]
        for action in actions:
            with self.assertRaisesRegex(records.RecordError, "schema_version must be 3"):
                action()
            self.assertEqual(self.db.read_bytes(), unchanged)
        self.assertFalse(output.exists())
        self.assertFalse(backup.exists())
        run = subprocess.run([sys.executable, "-B", str(SKILL / "scripts/paper_database.py"),
                              "upgrade", str(self.db), "--backup", str(backup)],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(run.returncode, 2)
        self.assertIn("invalid choice", run.stderr)
        self.assertEqual(self.db.read_bytes(), unchanged)
        self.assertFalse(backup.exists())

    def test_editing_an_intermediate_excludes_the_owner_from_reuse_candidates(self):
        database.init_database(self.db, self.dataset)
        data = database.export_snapshot(self.db)
        database.apply_edits(self.db, {
            "expected_snapshot": data["snapshot_id"],
            "edits": [{"collection": "items", "op": "upsert", "id": "claim-key-step", "record": {
                "id": "claim-key-step", "kind": "claim", "label": "Key step", "caption": "Inline covariance step",
                "statement": {"text": "The cross terms vanish pairwise.", "form": "synopsis"},
                "passages": [{"role": "evidence", "anchor_id": "anchor-item-variance"}],
                "owner": "variance"}}],
        })
        with_claim = database.export_snapshot(self.db)
        targets = [{"collection": group, "id": row["id"]}
                   for group in ("items", "uses") for row in with_claim[group]]
        database.compare_records(self.db, {"expected_snapshot": with_claim["snapshot_id"], "targets": targets,
                                           "reviewer": "test reviewer", "note": "Compared every synthetic row."})
        baseline = database.export_snapshot(self.db)
        claim = next(row for row in baseline["items"] if row["id"] == "claim-key-step")
        edited = deepcopy(claim)
        edited["statement"]["text"] = "The cross terms vanish in pairs."
        database.apply_edits(self.db, {"expected_snapshot": baseline["snapshot_id"],
                                       "edits": [{"collection": "items", "op": "upsert", "id": claim["id"], "record": edited}]})
        after = database.export_snapshot(self.db)
        self.assertEqual(records.comparison_status(after)["stale"], 2)
        report = database.changes_database(self.db, baseline["snapshot_id"])
        candidates = {(row["collection"], row["id"]) for row in report["reuse_candidates"]}
        self.assertIn(("items", "sampling"), candidates)
        self.assertIn(("uses", after["uses"][0]["id"]), candidates)
        self.assertNotIn(("items", "variance"), candidates)
        self.assertNotIn(("items", "claim-key-step"), candidates)


if __name__ == "__main__":
    unittest.main()

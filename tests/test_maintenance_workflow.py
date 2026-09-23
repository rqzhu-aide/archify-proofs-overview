"""Public common-store workflows for ID discovery and actionable diagnostics."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_database as database
import proof_overview


class MaintenanceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "paper.tex").write_text(
            "\\begin{assumption}\\label{ass:a}\nThe value x is positive.\n\\end{assumption}\n"
            "\\begin{theorem}\\label{thm:t}\nThen x+1 is positive.\n\\end{theorem}\n"
            "\\begin{proof}Add one to the bound on x.\\end{proof}\n", encoding="utf-8")
        self.seed = {"schema_version": 3, "title": "Synthetic paper", "scope": "Two selected statements",
                     "source": {"title": "Paper", "file": "paper.tex"}, "main_items": ["t"],
                     "items": [{"id": identity, "kind": kind, "label": label, "caption": label,
                                "statement": {"text": text, "form": "synopsis"},
                                "source": {"start_line": start, "end_line": end, "label": key}}
                               for identity, kind, label, text, start, end, key in (
                                   ("a", "assumption", "Assumption 1", "x is positive.", 1, 3, "ass:a"),
                                   ("t", "theorem", "Theorem 1", "Under Assumption 1, x+1 is positive.", 4, 6, "thm:t"))],
                     "uses": [{"from": "a", "to": "t", "reason": "Add one to the assumed positive value.",
                               "source": {"start_line": 7, "end_line": 7}},
                              {"id": "argument", "from": "a", "to": "t", "type": "proof_argument",
                               "reason": "Synthetic parallel contribution for ID testing.",
                               "source": {"start_line": 7, "end_line": 7}}]}
        self.seed_path = self.base / "seed.json"
        self.db = self.base / "paper.db"

    def init(self):
        self.seed_path.write_text(json.dumps(self.seed), encoding="utf-8")
        return database.init_database(self.db, self.seed_path, focused=True)

    def cli(self, *args):
        run = subprocess.run([sys.executable, "-X", "utf8", "-B", str(SKILL / "scripts" / "paper_database.py"),
                              *map(str, args)], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_list_supplies_exact_generated_and_parallel_use_ids_without_sources(self):
        initialized = self.init()
        before = self.db.read_bytes()
        listing = self.cli("list", self.db)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(listing["expected_snapshot"], initialized["snapshot_id"])
        self.assertEqual(listing["counts"]["uses"], 2)
        self.assertEqual(len({row["id"] for row in listing["uses"]}), 2)
        self.assertTrue(any(row["id"].startswith("use-") for row in listing["uses"]))
        self.assertTrue(next(row for row in listing["items"] if row["id"] == "t")["main_result"])
        self.assertNotIn("content_base64", json.dumps(listing))
        self.assertNotIn("statement", json.dumps(listing))
        self.assertNotIn("anchors", listing)
        targets = [{"collection": "uses", "id": row["id"]} for row in listing["uses"]]
        database.compare_records(self.db, {"expected_snapshot": listing["expected_snapshot"], "targets": targets,
                                         "reviewer": "test", "result": "needs_attention", "note": "Fixture comparison"})
        self.assertTrue(all(row["comparison"] == "needs_attention" for row in database.list_records(self.db, "uses")["uses"]))
        anchors = self.cli("list", self.db, "--collection", "anchors")["anchors"]
        self.assertTrue(all("locator" in row and "excerpt" not in row for row in anchors))

    def test_list_can_address_history_after_edit(self):
        initial = self.init()
        packet = database.get_packet(self.db, "t")
        item = deepcopy(packet["item"])
        item["label"] = "Theorem revised"
        database.apply_edits(self.db, {"expected_snapshot": initial["snapshot_id"], "edits": [
            {"collection": "items", "op": "upsert", "id": "t", "record": item}]})
        prior = self.cli("list", self.db, "--snapshot", initial["snapshot_id"], "--collection", "items")
        self.assertEqual(next(row for row in prior["items"] if row["id"] == "t")["label"], "Theorem 1")

    def test_conflicting_locator_is_visible_and_cannot_get_a_new_match(self):
        self.seed["items"][1]["source"].update(start_line=1, end_line=3)
        self.init()
        data = database.export_snapshot(self.db)
        self.assertEqual(data["anchors"][1]["verification"]["status"], "checked")
        warnings = database.validate_database(self.db)["locator_diagnostics"]
        self.assertEqual(warnings[0]["code"], "statement_locator_conflict")
        self.assertTrue(database.get_packet(self.db, "t")["locator_diagnostics"])
        batch = {"expected_snapshot": data["snapshot_id"], "targets": [{"collection": "items", "id": "t"}],
                 "reviewer": "test", "result": "matched", "note": "Incorrect synthetic match"}
        before = self.db.read_bytes()
        with self.assertRaisesRegex(database.DatabaseError, "items/t.*locator conflicts"):
            database.compare_records(self.db, batch)
        self.assertEqual(self.db.read_bytes(), before)
        batch["result"] = "needs_attention"
        database.compare_records(self.db, batch)
        self.assertEqual(database.get_packet(self.db, "t")["target_fidelity"], "needs_attention")
        self.assertEqual(database.export_snapshot(self.db)["snapshot_id"], data["snapshot_id"])

    def test_bad_item_edit_names_the_item_and_preserves_database(self):
        initial = self.init()
        item = deepcopy(database.get_packet(self.db, "t")["item"])
        item["passages"] = [{"role": "statement", "source": {"start_line": 4, "end_line": 6}}]
        before = self.db.read_bytes()
        with self.assertRaisesRegex(ValueError, "Item t.*missing anchor_id"):
            database.apply_edits(self.db, {"expected_snapshot": initial["snapshot_id"], "edits": [
                {"collection": "items", "op": "upsert", "id": "t", "record": item}]})
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_seed_file_error_explains_resolution_without_creating_database(self):
        self.seed_path = self.base / "work" / "seed.json"
        self.seed_path.parent.mkdir()
        with self.assertRaisesRegex(database.DatabaseError, "JSON directory.*source-root"):
            self.init()
        self.assertFalse(self.db.exists())

    def test_grouped_receipt_is_compact_and_full_diagnostics_remain_available(self):
        entries = [{"collection": "items", "id": "t", "field": "statement", "excerpt": "$\\mc X$",
                    "display": "inline", "reason": "Unsupported command \\mc"} for _ in range(30)]
        receipt = {"math_diagnostics": entries, "output": "overview.html"}
        compact = proof_overview.compact_render_receipt(receipt)
        self.assertEqual(compact["math_diagnostic_count"], 30)
        self.assertEqual(len(compact["math_diagnostics"]), 5)
        self.assertEqual(compact["math_diagnostic_groups"][0]["count"], 30)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(receipt)))
        self.assertEqual(proof_overview.compact_render_receipt(receipt, full=True)["math_diagnostics"], entries)
        self.assertEqual(len(receipt["math_diagnostics"]), 30)

    @unittest.skipUnless(shutil.which("node"), "Shared Node.js required")
    def test_grouped_html_disambiguates_item_and_use_with_same_id(self):
        self.seed["uses"][1]["id"] = "a"
        self.seed["items"][0]["statement"]["text"] += r" $\privateA$"
        self.seed["uses"][1]["reason"] += r" $\privateA$"
        self.init()
        output = self.base / "overview.html"
        receipt = database.render_database(self.db, output)
        self.assertEqual({(row["collection"], row["id"]) for row in receipt["math_diagnostics"]},
                         {("items", "a"), ("uses", "a")})
        html = output.read_text(encoding="utf-8")
        self.assertIn('Assumption 1 (statement): <code>a</code>', html)
        self.assertIn('Assumption 1 → Theorem 1 (reason): <code>a</code>', html)


if __name__ == "__main__":
    unittest.main()

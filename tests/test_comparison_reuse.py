"""Reuse comparison derivation only within one immutable snapshot operation."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_database as database
import paper_records as records


class ComparisonReuseTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).with_name("fixtures") / "schema3-intermediates.json"
        self.data = records.validate_records(json.loads(fixture.read_text(encoding="utf-8")))
        self.targets = [{"collection": collection, "id": row["id"]}
                        for collection in ("items", "uses") for row in self.data[collection]]
        self.keys = {(row["collection"], row["id"]) for row in self.targets}

    def compare_all(self):
        self.data["observations"].extend(records.make_observations(
            self.data, self.targets, reviewer="Fixture reviewer", note="Synthetic source comparison."))

    def assert_digests_once(self, calls, expected):
        self.assertEqual(Counter((call.args[1], call.args[2]) for call in calls),
                         Counter({key: 1 for key in expected}))

    def test_projection_reuses_newest_matching_observations(self):
        self.compare_all()
        target = {"collection": "items", "id": "main-result"}
        attention = records.make_observations(self.data, [target], reviewer="Second reviewer",
                                              result="needs_attention")[0]
        self.data["observations"].append(attention)
        other_input = deepcopy(self.data)
        other_input["items"][3]["caption"] = "A different overview caption"
        other_input.pop("snapshot_id", None)
        # Newer history for another input must not conceal this input's concern.
        self.data["observations"].extend(records.make_observations(
            other_input, [target], reviewer="Later reviewer"))
        original = deepcopy(self.data)
        with patch.object(records, "target_digest", wraps=records.target_digest) as digest:
            prepared = records.prepare_records(self.data, SKILL)
        self.assert_digests_once(digest.call_args_list, self.keys)
        self.assertEqual(self.data, original)
        main = next(row for row in prepared["items"] if row["id"] == "main-result")
        self.assertEqual(main["fidelity"], "needs_attention")
        self.assertEqual(prepared["build_context"]["source_comparison"]["needs_attention"], 1)
        selected = records.applicable_observations(self.data)
        self.assertIn(attention, selected)
        self.assertNotIn(self.data["observations"][-1], selected)

    def test_later_operation_recomputes_changed_context_and_history(self):
        self.compare_all()
        self.assertEqual(set(records.fidelity_by_row(self.data).values()), {"matched"})
        step = next(row for row in self.data["items"] if row["id"] == "claim-pairwise")
        step["statement"]["text"] = "A changed intermediate claim."
        self.data.pop("snapshot_id", None)
        with patch.object(records, "target_digest", wraps=records.target_digest) as digest:
            fidelity = records.fidelity_by_row(self.data)
        self.assert_digests_once(digest.call_args_list, self.keys)
        self.assertEqual(fidelity[("items", "claim-pairwise")], "stale")
        self.assertEqual(fidelity[("items", "key-bound")], "stale")
        self.assertEqual(fidelity[("items", "independence")], "matched")
        stale = records.applicable_observations(self.data)
        self.assertEqual(stale, self.data["observations"])
        self.data["observations"].extend(records.make_observations(
            self.data, [{"collection": "items", "id": "key-bound"}],
            reviewer="Follow-up reviewer", result="needs_attention"))
        self.assertEqual(records.fidelity_by_row(self.data)[("items", "key-bound")], "needs_attention")

    def test_packet_and_validation_reuse_complete_comparison_state(self):
        self.compare_all()
        expected_digests = {key: records.target_digest(self.data, *key) for key in self.keys}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, db = base / "records.json", base / "paper.sqlite"
            source.write_text(json.dumps(self.data), encoding="utf-8")
            database.init_database(db, source)
            with patch.object(records, "target_digest", wraps=records.target_digest) as digest:
                packet = database.get_packet(db, "key-bound")
            self.assert_digests_once(digest.call_args_list, self.keys)
            self.assertEqual(packet["target_fidelity"], "matched")
            self.assertEqual(packet["comparison_status"]["matched"], len(self.keys))
            for target in packet["target_digests"]:
                self.assertEqual(target["digest"], expected_digests[(target["collection"], target["id"])])
            self.assertEqual(len(packet["observations"]), len(packet["target_digests"]))
            with patch.object(records, "target_digest", wraps=records.target_digest) as digest:
                result = database.validate_database(db)
            self.assert_digests_once(digest.call_args_list, self.keys)
            self.assertEqual(result["source_comparison"]["matched"], len(self.keys))
            self.assertEqual(result["stale_targets"], [])

    def test_unreviewed_packet_hashes_only_requested_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, db = base / "records.json", base / "paper.sqlite"
            source.write_text(json.dumps(self.data), encoding="utf-8")
            database.init_database(db, source)
            with patch.object(records, "target_digest", wraps=records.target_digest) as digest:
                packet = database.get_packet(db, "independence")
            expected = {(target["collection"], target["id"]) for target in packet["target_digests"]}
            self.assert_digests_once(digest.call_args_list, expected)
            self.assertEqual(expected, {("items", "independence")})
            self.assertEqual(packet["target_fidelity"], "unreviewed")
            self.assertEqual(packet["observations"], [])
            self.assertEqual(packet["comparison_status"]["unreviewed"], len(self.keys))


if __name__ == "__main__":
    unittest.main()

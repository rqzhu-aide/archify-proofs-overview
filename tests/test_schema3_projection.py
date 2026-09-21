"""Schema-3 projection and rendering: details, detail edges, groups, fidelity.

The fixture tests/fixtures/schema3-intermediates.json is a synthetic schema-3
records dataset with label-only locators: four major rows, an intermediate
equation and claim owned by the lemma, two uses in a joint group, and one
detail edge. It asserts projection and rendering structure, not mathematics.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
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


FIXTURE = Path(__file__).with_name("fixtures") / "schema3-intermediates.json"
NODE = shutil.which("node")
RENDERER = SKILL / "scripts" / "render.mjs"
MAJOR_IDS = ["independence", "moments", "key-bound", "main-result"]
DETAIL_IDS = ["claim-pairwise", "eq-key-display"]


def load_fixture():
    return records.validate_records(json.loads(FIXTURE.read_text(encoding="utf-8")))


def revalidate(data):
    candidate = deepcopy(data)
    candidate.pop("snapshot_id", None)
    return records.validate_records(candidate)


def compare_all(data):
    candidate = deepcopy(data)
    targets = [{"collection": group, "id": row["id"]}
               for group in ("items", "uses") for row in candidate[group]]
    candidate["observations"].extend(records.make_observations(
        candidate, targets, reviewer="Synthetic source reviewer",
        note="Compared the bounded synthetic passages and applicable conditions."))
    return records.validate_records(candidate)


def reviewed_with_claim_edit(data):
    """All rows matched, then the claim edited: claim and owner go stale."""
    edited = deepcopy(compare_all(data))
    claim = next(row for row in edited["items"] if row["id"] == "claim-pairwise")
    claim["statement"]["text"] = "The cross terms vanish in pairs."
    edited = revalidate(edited)
    edited["observations"].extend(records.make_observations(
        edited, [{"collection": "items", "id": "independence"}], reviewer="Second reviewer",
        result="needs_attention", note="A follow-up concern on the synthetic record."))
    return records.validate_records(edited)


class ProjectionSplitTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()
        self.prepared = records.prepare_records(self.data, FIXTURE.parent)

    def test_prepared_split_separates_major_rows_details_and_edge_kinds(self):
        prepared = self.prepared
        self.assertEqual([row["id"] for row in prepared["items"]], MAJOR_IDS)
        self.assertEqual([row["id"] for row in prepared["details"]], DETAIL_IDS)
        self.assertTrue(all(row["owner"] == "key-bound" for row in prepared["details"]))
        rows = prepared["items"] + prepared["details"] + prepared["uses"] + prepared["detail_uses"]
        self.assertTrue(all(row["fidelity"] == "unreviewed" for row in rows))
        self.assertEqual(len(prepared["uses"]), 3)
        self.assertTrue(all(use["from"] in MAJOR_IDS and use["to"] in MAJOR_IDS for use in prepared["uses"]))
        self.assertEqual([use.get("group") for use in prepared["uses"]],
                         [{"id": "prereqs", "kind": "joint"}, {"id": "prereqs", "kind": "joint"}, None])
        self.assertEqual([(use["id"], use["from"], use["to"]) for use in prepared["detail_uses"]],
                         [("use-display-in-theorem", "eq-key-display", "main-result")])
        detail = next(row for row in prepared["details"] if row["id"] == "eq-key-display")
        self.assertIn("normalization", detail["issue"])
        self.assertEqual(prepared["graph_mode"], "dag")

    def test_prepare_is_deterministic(self):
        again = records.prepare_records(load_fixture(), FIXTURE.parent)
        self.assertEqual(json.dumps(self.prepared, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_detail_edges_never_affect_layout_or_cycle_detection(self):
        candidate = deepcopy(self.data)
        reverse = deepcopy(candidate["uses"][-1])
        reverse["id"] = "use-theorem-back-to-display"
        reverse["from"], reverse["to"] = "main-result", "eq-key-display"
        candidate["uses"].append(reverse)
        prepared = records.prepare_records(revalidate(candidate), FIXTURE.parent)
        self.assertEqual(prepared["graph_mode"], "dag")
        self.assertEqual(len(prepared["detail_uses"]), 2)
        cyclic = deepcopy(self.data)
        closing = deepcopy(cyclic["uses"][2])
        closing["id"] = "use-closing-loop"
        closing["from"], closing["to"] = "main-result", "key-bound"
        cyclic["uses"].append(closing)
        prepared = records.prepare_records(revalidate(cyclic), FIXTURE.parent)
        self.assertEqual(prepared["graph_mode"], "cyclic")
        self.assertEqual(len(prepared["details"]), 2)
        self.assertEqual(len(prepared["detail_uses"]), 1)

    def test_fidelity_states_flow_to_prepared_rows(self):
        prepared = records.prepare_records(reviewed_with_claim_edit(self.data), FIXTURE.parent)
        fidelity = {row["id"]: row["fidelity"] for row in prepared["items"] + prepared["details"]}
        self.assertEqual(fidelity["claim-pairwise"], "stale")
        self.assertEqual(fidelity["key-bound"], "stale")
        self.assertEqual(fidelity["independence"], "needs_attention")
        self.assertEqual(fidelity["eq-key-display"], "matched")
        use_fidelity = {row["id"]: row["fidelity"] for row in prepared["uses"] + prepared["detail_uses"]}
        self.assertEqual(use_fidelity["use-display-in-theorem"], "matched")
        self.assertEqual(prepared["build_context"]["source_comparison"]["stale"], 2)


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "paper.sqlite"
        database.init_database(self.db, FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_packet_includes_owned_intermediates(self):
        packet = database.get_packet(self.db, "key-bound")
        self.assertEqual([row["id"] for row in packet["owned_items"]], ["eq-key-display", "claim-pairwise"])
        digests = {(row["collection"], row["id"]) for row in packet["target_digests"]}
        self.assertIn(("items", "eq-key-display"), digests)
        self.assertIn(("items", "claim-pairwise"), digests)
        anchors = {anchor["id"] for anchor in packet["anchors"]}
        for row in packet["owned_items"]:
            self.assertTrue({passage["anchor_id"] for passage in row["passages"]} <= anchors)
        self.assertEqual(database.get_packet(self.db, "main-result")["owned_items"], [])


@unittest.skipUnless(NODE, "A shared Node installation is required.")
class ProjectionRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.input = self.base / "prepared.json"
        self.output = self.base / "overview.html"
        self.data = load_fixture()
        self.prepared = records.prepare_records(self.data, FIXTURE.parent)

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, prepared=None, expect_success=True):
        self.input.write_text(json.dumps(prepared if prepared is not None else self.prepared, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([NODE, str(RENDERER), str(self.input), str(self.output)],
                                capture_output=True, text=True, encoding="utf-8")
        if not expect_success:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), self.output.read_text(encoding="utf-8")

    def parse(self, html):
        reader = overview._ArtifactReader()
        reader.feed(html)
        return reader

    def render_artifact(self, data=None):
        output = self.base / "artifact.html"
        receipt = overview.render_dataset(data if data is not None else self.data, FIXTURE.parent, output)
        return receipt, output

    def test_intermediates_skip_the_svg_but_land_in_index_and_owner_panel(self):
        receipt, html = self.render()
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["graph_preservation"]["rendered_details"], 2)
        reader = self.parse(html)
        self.assertEqual(sorted(reader.nodes), sorted(MAJOR_IDS))
        self.assertEqual(sorted(reader.index_nodes), sorted(MAJOR_IDS))
        self.assertEqual(sorted(detail for detail, _ in reader.details), DETAIL_IDS)
        for _, article in reader.details:
            self.assertEqual(article, "key-bound")
        svg = re.search(r"<svg[\s\S]*?</svg>", html).group(0)
        self.assertNotIn("data-proof-detail", svg)
        for detail in DETAIL_IDS:
            self.assertNotIn(detail, svg)
        panel = re.search(r'<template id="proof-detail-key-bound">([\s\S]*?)</template>', html).group(1)
        self.assertIn('data-proof-detail="eq-key-display"', panel)
        self.assertIn('data-proof-detail="claim-pairwise"', panel)
        self.assertIn(">Equation</span>", panel)
        self.assertIn("Squared-sum display", panel)
        self.assertIn("cross terms vanish pairwise", panel)
        self.assertIn("normalization", panel)

    def test_detail_edges_are_annotations_and_never_reach_layout_or_svg(self):
        receipt, html = self.render()
        svg = re.search(r"<svg[\s\S]*?</svg>", html).group(0)
        self.assertNotIn("use-display-in-theorem", svg)
        reader = self.parse(html)
        self.assertEqual([(use, start, end) for use, start, end, _ in reader.detail_uses],
                         [("use-display-in-theorem", "eq-key-display", "main-result")])
        self.assertEqual(reader.detail_uses[0][3], "key-bound")
        self.assertEqual(receipt["graph_preservation"]["rendered_detail_uses"], 1)
        self.assertEqual(receipt["geometry"]["status"], "pass")

    def fixture_with_display_evidence(self):
        """Give the detail edge its own checked evidence passage."""
        data = deepcopy(self.data)
        excerpt = "Inserting the squared-sum display into the rate bound gives (5)."
        data["anchors"].append({"id": "anchor-use-display-evidence",
                                "source_revision": data["source_revision"]["id"],
                                "locator": {"label": "Theorem 1 proof, display step"},
                                "excerpt": excerpt,
                                "excerpt_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
                                "verification": {"status": "unverified", "method": "entered_locator",
                                                 "note": "Entered locator for the synthetic display passage."}})
        use = next(row for row in data["uses"] if row["id"] == "use-display-in-theorem")
        use["evidence_refs"] = ["anchor-use-display-evidence"]
        return revalidate(data)

    def test_detail_edges_render_their_evidence_in_panel_and_index(self):
        prepared = records.prepare_records(self.fixture_with_display_evidence(), FIXTURE.parent)
        receipt, html = self.render(prepared)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        evidence = ("Theorem 1 proof, display step",
                    "Inserting the squared-sum display into the rate bound gives (5).",
                    "Locator not yet verified",
                    "Entered locator for the synthetic display passage.")
        # The evidence is rendered markup in the owner's panel template and in
        # the static index article, not merely present in the JSON payload.
        panel = re.search(r'<template id="proof-detail-key-bound">([\s\S]*?)</template>', html).group(1)
        for needle in evidence:
            self.assertIn(needle, panel)
        article = re.search(r'<article[^>]*data-proof-index-item="key-bound"[\s\S]*?</article>', html).group(0)
        for needle in evidence:
            self.assertIn(needle, article)

    def test_a_detail_use_may_reference_its_own_intermediate_row(self):
        data = deepcopy(self.data)
        data["uses"].append({"id": "use-display-self-note",
                             "from": "eq-key-display", "to": "eq-key-display",
                             "type": "dependency",
                             "reason": "The display restates the pairwise claim in normalized form.",
                             "evidence_refs": ["anchor-item-key-bound"]})
        data = revalidate(data)
        prepared = records.prepare_records(data, FIXTURE.parent)
        self.assertEqual(prepared["graph_mode"], "dag")
        self.assertEqual(len(prepared["uses"]), 3)
        self.assertEqual([(use["id"], use["from"], use["to"]) for use in prepared["detail_uses"]],
                         [("use-display-in-theorem", "eq-key-display", "main-result"),
                          ("use-display-self-note", "eq-key-display", "eq-key-display")])
        receipt, html = self.render(prepared)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["graph_preservation"]["rendered_detail_uses"], 2)
        reader = self.parse(html)
        self.assertEqual(reader.detail_uses,
                         [("use-display-in-theorem", "eq-key-display", "main-result", "key-bound"),
                          ("use-display-self-note", "eq-key-display", "eq-key-display", "key-bound")])
        svg = re.search(r"<svg[\s\S]*?</svg>", html).group(0)
        self.assertNotIn("use-display-self-note", svg)
        self.assertNotIn("eq-key-display", svg)
        # Exactly one canonical annotation; the inert panel template duplicates it.
        panel = re.search(r'<template id="proof-detail-key-bound">([\s\S]*?)</template>', html).group(1)
        self.assertEqual(panel.count('data-proof-detail-use="use-display-self-note"'), 1)
        self.assertIn("The display restates the pairwise claim in normalized form.", panel)
        stripped = re.sub(r"<template\b[\s\S]*?</template>", "", html)
        self.assertEqual(stripped.count('data-proof-detail-use="use-display-self-note"'), 1)
        # The full artifact path accepts the placement and identity accounting.
        receipt, _ = self.render_artifact(data)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["graph_preservation"]["detail_uses"], 2)
        self.assertEqual(receipt["graph_mode"], "dag")

    def test_joint_group_keeps_one_path_per_use_and_badges_the_group(self):
        receipt, html = self.render()
        svg = re.search(r"<svg[\s\S]*?</svg>", html).group(0)
        edge_ids = re.findall(r'data-edge-id="([^"]+)"', svg)
        self.assertEqual(sorted(edge_ids), sorted(use["id"] for use in self.prepared["uses"]))
        self.assertEqual(len(edge_ids), len(set(edge_ids)))
        self.assertEqual(svg.count('data-proof-use="'), 2)
        self.assertIn("Joint: prereqs", svg)
        self.assertIn("required jointly", svg)
        reader = self.parse(html)
        svg_and_annotations = edge_ids + [use for use, _, _, _ in reader.detail_uses]
        self.assertEqual(sorted(svg_and_annotations), sorted(use["id"] for use in self.data["uses"]))
        self.assertEqual(len(svg_and_annotations), len(self.data["uses"]))
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")

    def test_cases_group_renders_separate_badged_connectors(self):
        prepared = deepcopy(self.prepared)
        for use in prepared["uses"][:2]:
            use["group"] = {"id": "prereqs", "kind": "cases"}
        receipt, html = self.render(prepared)
        svg = re.search(r"<svg[\s\S]*?</svg>", html).group(0)
        edge_ids = re.findall(r'data-edge-id="([^"]+)"', svg)
        self.assertEqual(sorted(edge_ids), sorted(use["id"] for use in prepared["uses"]))
        self.assertEqual(len(edge_ids), len(set(edge_ids)))
        self.assertIn("Case: prereqs", svg)
        self.assertIn("alternative case", svg)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")

    def test_detail_input_validation_is_explicit(self):
        unknown = deepcopy(self.prepared)
        unknown["detail_uses"][0]["to"] = "missing-row"
        self.assertIn("endpoints", self.render(unknown, expect_success=False)["error"])

        major_only = deepcopy(self.prepared)
        major_only["detail_uses"][0]["from"] = "key-bound"
        self.assertIn("ordinary use", self.render(major_only, expect_success=False)["error"])

        # A self reference on a major item is still not a detail use; only an
        # intermediate row may annotate itself.
        major_self = deepcopy(self.prepared)
        major_self["detail_uses"][0]["from"] = "key-bound"
        major_self["detail_uses"][0]["to"] = "key-bound"
        self.assertIn("ordinary use", self.render(major_self, expect_success=False)["error"])

        as_node = deepcopy(self.prepared)
        as_node["items"].append(as_node["details"].pop(0))
        self.assertIn("kind", self.render(as_node, expect_success=False)["error"])

        bad_owner = deepcopy(self.prepared)
        bad_owner["details"][0]["owner"] = "missing-row"
        self.assertIn("owner", self.render(bad_owner, expect_success=False)["error"])

        colliding = deepcopy(self.prepared)
        colliding["details"][0]["id"] = "key-bound"
        self.assertIn("distinct", self.render(colliding, expect_success=False)["error"])

    def test_empty_detail_arrays_render_a_major_only_overview(self):
        prepared = dict(deepcopy(self.prepared), details=[], detail_uses=[])
        receipt, html = self.render(prepared)
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["items"], 4)
        self.assertNotIn("data-proof-detail", html)

    def test_prepared_input_requires_the_native_version_and_detail_arrays(self):
        for version in (None, 1, 2):
            with self.subTest(version=version):
                prepared = dict(deepcopy(self.prepared), schema_version=version)
                self.assertIn("schema_version 3", self.render(prepared, expect_success=False)["error"])
        for field in ("details", "detail_uses"):
            with self.subTest(field=field):
                prepared = deepcopy(self.prepared)
                del prepared[field]
                self.assertIn(field, self.render(prepared, expect_success=False)["error"])

    def test_fidelity_badges_render_all_states_without_sentiment_colors(self):
        _, plain = self.render()
        self.assertIn('data-proof-fidelity="unreviewed"', plain)
        self.assertIn("Not yet compared with the source", plain)
        self.assertIn("Mathematical validity is not assessed", plain)

        prepared = records.prepare_records(reviewed_with_claim_edit(self.data), FIXTURE.parent)
        _, html = self.render(prepared)
        panel = re.search(r'<template id="proof-detail-key-bound">([\s\S]*?)</template>', html).group(1)
        self.assertIn('data-proof-fidelity="stale"', panel)
        states = re.findall(r'data-proof-fidelity="([^"]+)"', html)
        for state in ("matched", "needs_attention", "stale"):
            self.assertIn(state, states)
        self.assertIn("Compared with the source", html)
        badges = re.findall(r"<span[^>]*data-proof-fidelity[^>]*>", html)
        self.assertTrue(badges)
        for badge in badges:
            self.assertIn('class="proof-fidelity"', badge)
        rule = re.search(r"\.proof-fidelity\{([^}]*)\}", html).group(1)
        self.assertIn("var(--text-muted)", rule)
        for color in ("red", "green", "yellow", "orange"):
            self.assertNotIn(color, rule)

    def test_rebuild_is_byte_identical_with_intermediates_and_groups(self):
        first, html = self.render()
        second, repeated = self.render()
        self.assertEqual(html, repeated)
        self.assertEqual(first["artifact_sha256"], second["artifact_sha256"])
        self.assertEqual(first["input_sha256"], second["input_sha256"])

    def test_acceptance_gate_covers_details_and_detail_uses(self):
        receipt, _ = self.render_artifact()
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["graph_preservation"]["details"], 2)
        self.assertEqual(receipt["graph_preservation"]["detail_uses"], 1)
        self.assertEqual(receipt["graph_preservation"]["items"], 4)
        self.assertEqual(receipt["graph_preservation"]["uses"], 3)

    def test_acceptance_rejects_a_dropped_intermediate_and_preserves_the_artifact(self):
        _, output = self.render_artifact()
        previous = output.read_bytes()
        original_run = overview.subprocess.run

        def drop_detail(args, **kwargs):
            result = original_run(args, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = Path(args[-1])
            html = artifact.read_text(encoding="utf-8")
            html, count = re.subn(r'data-proof-detail="claim-pairwise"',
                                  'data-omitted-detail="claim-pairwise"', html)
            self.assertEqual(count, 2, "The probe removes the template and index copies of the detail marker.")
            artifact.write_text(html, encoding="utf-8")
            receipt = json.loads(result.stdout)
            receipt["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            receipt["bytes"] = artifact.stat().st_size
            result.stdout = json.dumps(receipt)
            return result

        with patch.object(overview.subprocess, "run", side_effect=drop_detail):
            with self.assertRaises(overview.OverviewError):
                self.render_artifact()
        self.assertEqual(output.read_bytes(), previous)

    def test_cyclic_graph_lists_intermediates_and_annotates_groups(self):
        cyclic = deepcopy(self.data)
        closing = deepcopy(cyclic["uses"][2])
        closing["id"] = "use-closing-loop"
        closing["from"], closing["to"] = "main-result", "key-bound"
        cyclic["uses"].append(closing)
        receipt, output = self.render_artifact(revalidate(cyclic))
        self.assertEqual(receipt["graph_mode"], "cyclic")
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        html = output.read_text(encoding="utf-8")
        reader = self.parse(html)
        self.assertEqual(sorted(detail for detail, _ in reader.details), DETAIL_IDS)
        self.assertTrue(all(article == "key-bound" for _, article in reader.details))
        self.assertEqual([(use, start, end) for use, start, end, _ in reader.detail_uses],
                         [("use-display-in-theorem", "eq-key-display", "main-result")])
        self.assertIn("Joint: prereqs", html)
        self.assertIn("it does not establish a circular proof", html)
        self.assertEqual(receipt["geometry"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()

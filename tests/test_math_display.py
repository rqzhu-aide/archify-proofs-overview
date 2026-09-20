"""Math display adaptation and diagnostics.

Synthetic fixtures only: sized named bar delimiters, unbraced binomial
arguments, and private macros are the demonstrated converter failure classes.
The tests assert converter-input adaptation and located diagnostics, never
mathematical content.
"""
from __future__ import annotations

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
import overview_math as math_display
import paper_records as records
import proof_overview as overview


NODE = shutil.which("node")
RENDERER = SKILL / "scripts" / "render.mjs"


class ConverterAdaptationTests(unittest.TestCase):
    def test_sized_named_bars_convert_at_the_authored_size(self):
        for tex in (r"\Bigl\lVert x \Bigr\rVert_{\mathcal H}",
                    r"\bigl\lVert x \bigr\rVert",
                    r"\Biggl\lVert x \Biggr\rVert",
                    r"\Bigl\vert x \Bigr\vert",
                    r"\big\Vert x \big\Vert"):
            with self.subTest(tex=tex):
                markup, reason = math_display._convert(tex, "inline")
                self.assertIsNotNone(markup, reason)
                self.assertIn("<math", markup)
                # Explicit sizing survives (a \left/\right rewrite would not fix it).
                self.assertIn('minsize="', markup)
                body = markup.split("<semantics>", 1)[1].split("<annotation")[0]
                self.assertNotIn("\\lVert", body)
                self.assertNotIn("\\lvert", body)

    def test_escaped_sizing_command_is_not_adapted(self):
        literal = r"\\Bigl\lVert"
        self.assertEqual(math_display._sized_named_bars(literal), literal)

    def test_unbraced_binomial_with_script_converts(self):
        for tex, script_tag in ((r"\binom nk^{-1}", "<msup"), (r"\binom{n}k^{-1}", "<msup"),
                                (r"\binom \alpha k_{i}", "<msub"), (r"\binom{n}{k}^{-1}", "<msup")):
            with self.subTest(tex=tex):
                markup, reason = math_display._convert(tex, "inline")
                self.assertIsNotNone(markup, reason)
                self.assertIn("<mfrac", markup)
                self.assertIn(script_tag, markup)

    def test_binomial_without_script_needs_no_grouping(self):
        self.assertEqual(math_display._group_scripted_binomials(r"\binom nk"), r"\binom nk")
        markup, reason = math_display._convert(r"\binom{n}{k}", "inline")
        self.assertIsNotNone(markup, reason)

    def test_malformed_and_unsupported_input_still_falls_back_with_reason(self):
        markup, reason = math_display._convert(r"\binom{", "inline")
        self.assertIsNone(markup)
        self.assertIn("unmatched brace", reason)
        markup, reason = math_display._convert(r"\cS_n", "inline")
        self.assertIsNone(markup)
        self.assertIn(r"\cS", reason)
        markup, reason = math_display._convert(r"\newcommand{\x}{1}", "inline")
        self.assertIsNone(markup)
        self.assertIn("macro", reason)

    def test_adaptation_keeps_the_original_tex_as_evidence(self):
        tex = r"\Bigl\lVert x \Bigr\rVert_{\mathcal H}"
        markup, _ = math_display._convert(tex, "inline")
        self.assertIn('encoding="application/x-tex"', markup)
        self.assertIn(r"\Bigl\lVert", markup)

    def test_render_text_reports_each_fallback_once(self):
        diagnostics = []
        html = math_display.render_text(
            r"For $x\in\cS$ the norm $\Bigl\lVert x\Bigr\rVert$ and $\binom nk^{-1}$.", diagnostics=diagnostics)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["display"], "inline")
        self.assertIn(r"\cS", diagnostics[0]["reason"])
        self.assertIn(r"$x\in\cS$", diagnostics[0]["excerpt"])
        self.assertEqual(html.count('class="math-fallback"'), 1)
        self.assertEqual(html.count("<math"), 2)


class PrepareDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "main.tex").write_text(
            "\\begin{lemma}\\label{lem:norm}\nThe norm bound holds.\n\\end{lemma}\n"
            "\\begin{theorem}\\label{thm:main}\nThe main bound holds.\n\\end{theorem}\n",
            encoding="utf-8")
        self.seed = {
            "schema_version": 3, "title": "Math diagnostics fixture",
            "scope": "Two synthetic declarations.",
            "source": {"title": "Synthetic manuscript", "file": "main.tex"},
            "items": [
                {"id": "norm-bound", "kind": "lemma", "label": "Lemma 1", "caption": "Norm bound",
                 "statement": {"text": r"The norm $\Bigl\lVert x\Bigr\rVert_{\mathcal H}$ is finite.", "form": "synopsis"},
                 "source": {"label": "lem:norm", "start_line": 1, "end_line": 1}},
                {"id": "main-bound", "kind": "theorem", "label": "Theorem 1", "caption": "Main bound",
                 "statement": {"text": r"The private set $\cS_n$ is compact.", "form": "synopsis"},
                 "source": {"label": "thm:main", "start_line": 2, "end_line": 2}},
            ],
            "uses": [
                {"id": "use-norm-main", "from": "norm-bound", "to": "main-bound", "type": "dependency",
                 "reason": r"The display $\binom nk^{-1}$ bounds $\cD_n$.",
                 "source": {"start_line": 1, "end_line": 2}},
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self):
        return records.prepare_records(records.normalize(deepcopy(self.seed), self.base), self.base)

    def test_diagnostics_identify_the_record_and_field(self):
        prepared = self.prepare()
        entries = {(row["collection"], row["id"], row["field"]) for row in prepared["math_diagnostics"]}
        self.assertEqual(entries, {("items", "main-bound", "statement"), ("uses", "use-norm-main", "reason")})
        for row in prepared["math_diagnostics"]:
            self.assertIn(r"\c", row["reason"])
            self.assertTrue(row["excerpt"])
        for row in prepared["items"] + prepared["uses"]:
            self.assertNotIn("statement_diagnostics", row)
            self.assertNotIn("reason_diagnostics", row)

    def test_adapted_expressions_render_and_keep_record_text(self):
        prepared = self.prepare()
        lemma = next(row for row in prepared["items"] if row["id"] == "norm-bound")
        use = prepared["uses"][0]
        self.assertEqual(lemma["statement"], r"The norm $\Bigl\lVert x\Bigr\rVert_{\mathcal H}$ is finite.")
        self.assertIn("<math", lemma["statement_html"])
        self.assertIn("<math", use["reason_html"])
        self.assertEqual(use["reason"], r"The display $\binom nk^{-1}$ bounds $\cD_n$.")
        self.assertIn(r"\cD_n", use["reason_html"])  # unconverted macro stays a labeled fallback
        self.assertIn('class="math-fallback"', use["reason_html"])

    def test_aggregated_warning_locates_failures_without_per_row_noise(self):
        prepared = self.prepare()
        math_warnings = [text for text in prepared["warnings"] if "could not be typeset" in text]
        self.assertEqual(len(math_warnings), 1)
        self.assertIn("2 LaTeX expressions", math_warnings[0])
        self.assertIn("main-bound (statement)", math_warnings[0])
        self.assertIn("use-norm-main (reason)", math_warnings[0])
        self.assertNotIn("A dependency reason", math_warnings[0])

    def test_display_adaptation_leaves_snapshot_identity_and_comparison_unchanged(self):
        data = records.normalize(deepcopy(self.seed), self.base)
        snapshot = data["snapshot_id"]
        targets = [{"collection": group, "id": row["id"]}
                   for group in ("items", "uses") for row in data[group]]
        data["observations"].extend(records.make_observations(
            data, targets, reviewer="Synthetic source reviewer", note="Compared the synthetic passages."))
        data = records.validate_records(data)
        prepared = records.prepare_records(data, self.base)
        self.assertEqual(prepared["build_context"]["input_snapshot"], snapshot)
        self.assertEqual(prepared["build_context"]["source_comparison"]["status"], "complete")
        self.assertTrue(all(row["fidelity"] == "matched"
                            for row in prepared["items"] + prepared["uses"]))


@unittest.skipUnless(NODE, "node is needed for the renderer")
class RendererDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        fixture = SKILL / "tests" / "fixtures" / "schema3-intermediates.json"
        self.prepared = records.prepare_records(
            records.validate_records(json.loads(fixture.read_text(encoding="utf-8"))), fixture.parent)
        self.input = self.base / "prepared.json"
        self.output = self.base / "overview.html"

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, prepared, expect_success=True):
        self.input.write_text(json.dumps(prepared, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([NODE, str(RENDERER), str(self.input), str(self.output)],
                                capture_output=True, text=True, encoding="utf-8")
        if not expect_success:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stderr), None
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), self.output.read_text(encoding="utf-8")

    def test_math_diagnostics_render_a_collapsible_note(self):
        prepared = deepcopy(self.prepared)
        prepared["math_diagnostics"] = [
            {"collection": "items", "id": "key-bound", "field": "statement",
             "excerpt": r"$\cS_n$", "reason": "unsupported command remains literal: \\cS"}]
        receipt, html = self.render(prepared)
        self.assertIn("Math display notes", html)
        self.assertIn("key-bound", html)
        self.assertIn("statement", html)

    def test_malformed_math_diagnostics_are_rejected(self):
        prepared = deepcopy(self.prepared)
        prepared["math_diagnostics"] = [{"id": "key-bound"}]
        receipt, _ = self.render(prepared, expect_success=False)
        self.assertIn("math_diagnostics", receipt["error"])

    def test_absent_math_diagnostics_render_no_note(self):
        _, html = self.render(self.prepared)
        self.assertNotIn("Math display notes", html)

    def test_render_receipt_carries_the_diagnostics(self):
        fixture = SKILL / "tests" / "fixtures" / "schema3-intermediates.json"
        data = records.validate_records(json.loads(fixture.read_text(encoding="utf-8")))
        row = next(item for item in data["items"] if item["id"] == "key-bound")
        row["statement"]["text"] = r"The private class $\cK$ is closed."
        data.pop("snapshot_id", None)
        data = records.validate_records(data)
        output = self.base / "artifact.html"
        receipt = overview.render_dataset(data, fixture.parent, output)
        self.assertEqual([(d["id"], d["field"]) for d in receipt["math_diagnostics"]],
                         [("key-bound", "statement")])
        self.assertIn("key-bound (statement)", receipt["warnings"][-1])


if __name__ == "__main__":
    unittest.main()

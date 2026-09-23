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
from xml.etree import ElementTree as ET


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import overview_math as math_display
import paper_records as records
import proof_overview as overview


NODE = shutil.which("node")
RENDERER = SKILL / "scripts" / "render.mjs"


class ConverterAdaptationTests(unittest.TestCase):
    def test_arg_operator_keeps_argument_and_minimum_forms_distinct(self):
        namespace = {"m": math_display.MATHML_NS}
        for tex, expected in ((r"\arg\min_x f(x)", ["arg", "min"]),
                              (r"\arg z", ["arg"])):
            with self.subTest(tex=tex):
                markup, reason = math_display._convert(tex, "inline")
                self.assertIsNotNone(markup, reason)
                root = ET.fromstring(markup)
                operators = [node.text for node in root.findall(".//m:mo", namespace)
                             if node.text in ("arg", "min")]
                self.assertEqual(operators, expected)
                self.assertEqual(root.find(".//m:annotation", namespace).text, tex)
                self.assertEqual(root.attrib["aria-label"], "LaTeX: " + tex)

    def test_arg_adaptation_respects_command_names_and_escaping(self):
        for tex in (r"\argmin", r"\argument", r"\\arg", r"\\arg\min"):
            with self.subTest(tex=tex):
                self.assertEqual(math_display._argument_operator(tex), tex)
        self.assertEqual(math_display._argument_operator(r"\arg_z"), r"\operatorname{arg}_z")

    def test_arg_adaptation_does_not_hide_unsupported_commands(self):
        diagnostics = []
        literal = r"$\arg\privateMinimum_x f(x)$"
        markup = math_display.render_text(literal, diagnostics=diagnostics)
        self.assertIn('class="math-fallback"', markup)
        self.assertIn(literal, markup)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["excerpt"], literal)
        self.assertIn(r"\privateMinimum", diagnostics[0]["reason"])

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

    def test_doubled_commands_do_not_silently_render_as_word_letters(self):
        for literal in (r"$\\mathbf{U}$", r"$\\frac{1}{n}$", r"$x + \\alpha$", r"$\\frac 1n$"):
            with self.subTest(literal=literal):
                # Exercise the decoded canonical text, as a JSON record supplies it.
                decoded = json.loads(json.dumps({"text": literal}))["text"]
                diagnostics = []
                markup = math_display.render_text(decoded, diagnostics)
                self.assertIn('class="math-fallback"', markup)
                self.assertNotIn("<math", markup)
                self.assertIn(literal, markup)
                self.assertEqual(diagnostics[0]["excerpt"], decoded)
                self.assertIn("likely doubled LaTeX command slash", diagnostics[0]["reason"])

    def test_paired_doubled_delimiters_are_located_without_losing_math(self):
        for literal in (r"\\(x_n\\to 0\\)", r"\\[\\frac{1}{n}\\]"):
            with self.subTest(literal=literal):
                diagnostics = []
                markup = math_display.render_text("Before " + literal + " after $y$.", diagnostics)
                self.assertIn('class="math-fallback"', markup)
                self.assertIn(literal, markup)
                self.assertEqual(markup.count("<math"), 1)
                self.assertEqual(len(diagnostics), 1)
                self.assertEqual(diagnostics[0]["excerpt"], literal)
                self.assertIn("doubled LaTeX delimiter", diagnostics[0]["reason"])

    def test_correct_delimiters_and_commands_keep_their_rendering_and_annotation(self):
        literal = r"\(\mathbf{U}\) and $\frac{1}{n}$ and \[\alpha\to0\]"
        diagnostics = []
        markup = math_display.render_text(literal, diagnostics)
        self.assertEqual(diagnostics, [])
        self.assertEqual(markup.count("<math"), 3)
        self.assertIn("<mfrac", markup)
        self.assertIn(r'encoding="application/x-tex">\mathbf{U}</annotation>', markup)

    def test_legitimate_row_breaks_are_not_misdiagnosed_as_doubled_commands(self):
        namespace = {"m": math_display.MATHML_NS}
        for tex in (r"\begin{matrix}a\\beta\end{matrix}",
                    r"\begin{align}x&=1\\frac&=2\end{align}",
                    r"\substack{a\\beta}"):
            with self.subTest(tex=tex):
                markup, reason = math_display._convert(tex, "block")
                self.assertIsNotNone(markup, reason)
                root = ET.fromstring(markup)
                self.assertGreaterEqual(len(root.findall(".//m:mtr", namespace)), 2)
                self.assertEqual(root.find(".//m:annotation", namespace).text, tex)
        # aligned already lacks reliable converter support. Preserve that
        # existing fallback instead of blaming its valid row separator.
        markup, reason = math_display._convert(r"\begin{aligned}x&=1\\beta&=2\end{aligned}", "block")
        self.assertIsNone(markup)
        self.assertIn("unsupported environment", reason)
        self.assertNotIn("doubled", reason)

    def test_commands_after_a_row_environment_are_still_checked(self):
        tex = r"\begin{matrix}a\\beta\end{matrix}+\\mathbf{U}"
        markup, reason = math_display._convert(tex, "block")
        self.assertIsNone(markup)
        self.assertIn("likely doubled LaTeX command slash", reason)

    def test_literal_prose_backslashes_are_not_reported_as_math(self):
        literal = r"Open C:\papers\main.tex or \\server\share; literal \\mathbf and an unmatched \\( path."
        diagnostics = []
        self.assertEqual(math_display.render_text(literal, diagnostics), literal)
        self.assertEqual(diagnostics, [])

    def test_groups_repeated_macro_failures_and_keeps_all_record_locations(self):
        diagnostics = []
        for record_id, literal in (("a", r"$x\in\privateClass$ and $y\in\privateClass$"),
                                   ("b", r"$z\in\privateClass$ and $z\in\anotherClass$")):
            failures = []
            math_display.render_text(literal, failures)
            diagnostics.extend({"collection": "items", "id": record_id, "field": "statement", **row}
                               for row in failures)
        original = deepcopy(diagnostics)
        groups = math_display.group_diagnostics(diagnostics)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["count"], 3)
        self.assertEqual(groups[0]["locations"], [
            {"collection": "items", "id": "a", "field": "statement"},
            {"collection": "items", "id": "b", "field": "statement"}])
        self.assertIn(r"\privateClass", groups[0]["reason"])
        self.assertEqual(groups[0]["example"], r"$x\in\privateClass$")
        self.assertEqual(diagnostics, original)


class ScopeDisplayTests(unittest.TestCase):
    def test_short_lead_discloses_only_later_paragraphs(self):
        scope = 'Coverage <all> with $x < y$.\n\nLimits "apply" to $z$.'
        result = math_display.render_scope(scope)
        self.assertIn("Coverage &lt;all&gt;", result["lead_html"])
        self.assertIn("<math", result["lead_html"])
        self.assertIn('encoding="application/x-tex">x &lt; y</annotation>', result["lead_html"])
        self.assertNotIn("Limits", result["lead_html"])
        self.assertIn("Limits &quot;apply&quot;", result["details_html"])
        self.assertIn("<math", result["details_html"])
        self.assertNotIn("Coverage", result["details_html"])

    def test_cutoff_keeps_the_whole_formula_and_discloses_full_original(self):
        prefix = " ".join(["word"] * 99)
        scope = prefix + r" $x + y$ follows here." + "\n\nLater limits."
        diagnostics = []
        result = math_display.render_scope(scope, diagnostics=diagnostics)
        self.assertIn('encoding="application/x-tex">x + y</annotation>', result["lead_html"])
        self.assertTrue(result["lead_html"].endswith("</math>…"))
        self.assertNotIn("follows", result["lead_html"])
        self.assertEqual(result["details_html"], math_display.render_text(scope))
        self.assertEqual(diagnostics, [])

    def test_paragraph_breaks_inside_explicit_math_do_not_split_the_lead(self):
        for opening, closing in (("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")):
            with self.subTest(opening=opening):
                scope = "Coverage " + opening + "x +\n\n y" + closing + " continues.\n\nLater limits."
                diagnostics = []
                result = math_display.render_scope(scope, diagnostics=diagnostics)
                self.assertIn("continues.", result["lead_html"])
                self.assertIn('encoding="application/x-tex">x +\n\n y</annotation>', result["lead_html"])
                self.assertEqual(result["details_html"], "Later limits.")
                self.assertEqual(diagnostics, [])

    def test_repeated_preview_diagnoses_each_original_occurrence_once(self):
        prefix = " ".join(["word"] * 99)
        scope = prefix + r" $x + \privateClass$ and $x + \privateClass$."
        diagnostics = []
        result = math_display.render_scope(scope, diagnostics=diagnostics)
        self.assertEqual(result["lead_html"].count('class="math-fallback"'), 1)
        self.assertEqual(result["details_html"].count('class="math-fallback"'), 2)
        self.assertEqual(len(diagnostics), 2)
        self.assertTrue(all(row["excerpt"] == r"$x + \privateClass$" for row in diagnostics))

    def test_unmatched_math_tail_stays_visible_across_blank_lines_and_cutoff(self):
        for prefix in ("Coverage", " ".join(["word"] * 99)):
            with self.subTest(long_lead=prefix != "Coverage"):
                scope = prefix + ' $x +\n\n <unclosed> formula with more words'
                diagnostics = []
                result = math_display.render_scope(scope, diagnostics=diagnostics)
                self.assertIn('class="math-fallback"', result["lead_html"])
                self.assertIn("&lt;unclosed&gt; formula with more words", result["lead_html"])
                self.assertEqual(len(diagnostics), 1)
                self.assertIn("no matching closing delimiter", diagnostics[0]["reason"])
                self.assertEqual(result["details_html"], "" if prefix == "Coverage"
                                 else math_display.render_text(scope))

    def test_plain_prose_retains_the_existing_preview_limit(self):
        words = " ".join(["word"] * 100)
        self.assertEqual(math_display.render_scope(words), {"lead_html": words, "details_html": ""})
        scope = words + " last"
        self.assertEqual(math_display.render_scope(scope), {"lead_html": words + "…", "details_html": scope})
        self.assertEqual(math_display.render_scope(""), {"lead_html": "", "details_html": ""})


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

    def test_corrupted_escaping_is_located_but_never_rewrites_canonical_text(self):
        literal = r"The map is $\\mathbf{U}$ with \\(x_n\\to0\\)."
        self.seed["items"][0]["statement"]["text"] = literal
        data = records.normalize(deepcopy(self.seed), self.base)
        original = deepcopy(data)
        prepared = records.prepare_records(data, self.base)
        lemma = next(row for row in prepared["items"] if row["id"] == "norm-bound")
        failures = [row for row in prepared["math_diagnostics"] if row["id"] == "norm-bound"]
        self.assertEqual(data, original)
        self.assertEqual(lemma["statement"], literal)
        self.assertEqual(lemma["statement_html"].count('class="math-fallback"'), 2)
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(row["collection"] == "items" and row["field"] == "statement"
                            for row in failures))
        self.assertEqual(prepared["build_context"]["input_snapshot"], original["snapshot_id"])


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

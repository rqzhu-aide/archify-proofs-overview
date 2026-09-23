"""Source diagnostics detect contradictions without rewriting stored evidence."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import paper_records as records


class LocatorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'main.tex'
        self.source.write_text(
            '\\begin{theorem}\\label{thm:one}\n'
            'The target is positive.\n'
            '\\end{theorem}\n'
            '\\begin{lemma}\\label{lem:two}\n'
            'The auxiliary quantity is bounded.\n'
            '\\end{lemma}\n'
            '\\begin{proof}\n'
            'Apply the auxiliary bound.\n'
            '\\end{proof}\n', encoding='utf-8')
        self.seed = {
            'schema_version': 3, 'title': 'Binding fixture', 'scope': 'One selected statement.',
            'source': {'title': 'Synthetic source', 'file': 'main.tex'},
            'items': [{'id': 'target', 'kind': 'theorem', 'label': 'Theorem 1', 'caption': 'Positive target',
                       'statement': {'text': 'The target is positive.', 'form': 'synopsis'},
                       'source': {'label': 'thm:one', 'start_line': 2, 'end_line': 2}}],
            'uses': []}

    def capture(self):
        return records.normalize(self.seed, self.base)

    def test_statement_subrange_needs_no_repeated_label(self):
        self.assertEqual(records.locator_diagnostics(self.capture()), [])

    def test_conflicting_statement_label_and_lines_are_reported_without_mutation(self):
        self.seed['items'][0]['source'].update(start_line=5, end_line=5)
        data = self.capture()
        before = deepcopy(data)
        self.assertEqual(data['anchors'][0]['verification']['status'], 'checked')
        diagnostic, = records.locator_diagnostics(data)
        self.assertEqual(diagnostic['code'], 'statement_locator_conflict')
        self.assertEqual(diagnostic['record_ids'], ['target'])
        self.assertEqual(diagnostic['anchor_ids'], ['anchor-item-target'])
        self.assertIn('main.tex:1-3', diagnostic['message'])
        self.assertEqual(records.validate_records(data), before)
        self.assertEqual(data, before)

    def test_duplicate_key_warns_instead_of_guessing_statement_target(self):
        self.source.write_text(self.source.read_text(encoding='utf-8') +
                               '\\begin{theorem}\\label{thm:one}Other target.\\end{theorem}\n', encoding='utf-8')
        diagnostic, = records.locator_diagnostics(self.capture())
        self.assertEqual(diagnostic['code'], 'ambiguous_tex_label')
        self.assertEqual([location['line'] for location in diagnostic['locations']], [1, 10])

    def test_duplicate_keys_in_distinct_captured_files_are_ambiguous(self):
        self.source.write_text(self.source.read_text(encoding='utf-8') + '\\input{appendix}\n', encoding='utf-8')
        (self.base / 'appendix.tex').write_text('\\label{thm:one}\n', encoding='utf-8')
        diagnostic, = records.locator_diagnostics(self.capture())
        self.assertEqual({location['path'] for location in diagnostic['locations']}, {'main.tex', 'appendix.tex'})

    def test_commented_duplicate_and_unselected_key_are_not_noisy(self):
        self.source.write_text(self.source.read_text(encoding='utf-8') +
                               '% \\label{thm:one}\n\\label{unused}\\label{unused}\n', encoding='utf-8')
        self.assertEqual(records.locator_diagnostics(self.capture()), [])

    def test_support_passage_can_reference_another_statement(self):
        self.seed['items'][0]['passages'] = [
            {'role': 'evidence', 'source': {'label': 'thm:one', 'start_line': 5, 'end_line': 5}},
            {'role': 'proof', 'source': {'label': 'thm:one', 'start_line': 7, 'end_line': 9}}]
        self.assertEqual(records.locator_diagnostics(self.capture()), [])

    def test_custom_declared_environment_is_checked(self):
        self.source.write_text(self.source.read_text(encoding='utf-8').replace('{theorem}', '{result}') +
                               '\\newtheorem{result}{Theorem}\n', encoding='utf-8')
        self.seed['items'][0]['source'].update(start_line=5, end_line=5)
        diagnostic, = records.locator_diagnostics(self.capture())
        self.assertEqual(diagnostic['code'], 'statement_locator_conflict')

    def test_optional_same_file_guards_are_grouped_and_keep_scope_disclosure(self):
        self.source.write_text(
            '\\IfFileExists{main-xrefs.tex}{\\input{main-xrefs.tex}}{\\typeout{No references}}\n'
            '\\IfFileExists{supp-xrefs.tex}{\\input{supp-xrefs}}{}\n', encoding='utf-8')
        files, notes = records._capture([self.source], self.base)
        self.assertEqual(len(files), 1)
        self.assertEqual(len(notes), 1)
        self.assertIn('optional inputs not captured', notes[0])
        self.assertIn('main-xrefs.tex', notes[0])
        self.assertIn('supp-xrefs', notes[0])
        self.assertIn('scope', notes[0])
        self.assertNotIn('unresolved input', notes[0])

    def test_other_target_false_branch_and_unguarded_input_remain_substantive(self):
        self.source.write_text(
            '\\IfFileExists{guard.tex}{\\input{proof}}{\\input{fallback}}\n'
            '\\input{main-xrefs}\n'
            '\\IfFileExists{main-xrefs.tex}{\\input{main-xrefs.tex}}{}\n', encoding='utf-8')
        _, notes = records._capture([self.source], self.base)
        missing = [note for note in notes if 'unresolved input' in note]
        self.assertEqual(len(missing), 3)
        self.assertTrue(any("'proof'" in note for note in missing))
        self.assertTrue(any("'fallback'" in note for note in missing))
        self.assertTrue(any("'main-xrefs'" in note for note in missing))

    def test_present_guarded_source_is_captured_normally(self):
        self.source.write_text('\\IfFileExists{proof.tex}{\\input{proof.tex}}{}\n', encoding='utf-8')
        (self.base / 'proof.tex').write_text('Actual proof text.\n', encoding='utf-8')
        files, notes = records._capture([self.source], self.base)
        self.assertEqual({file['path'] for file in files}, {'main.tex', 'proof.tex'})
        self.assertEqual(notes, [])


if __name__ == '__main__':
    unittest.main()

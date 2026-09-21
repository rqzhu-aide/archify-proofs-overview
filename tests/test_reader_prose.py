"""Reader prose shares math display and distinguishes review from resolution."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import paper_records as records
import proof_overview as overview

FIXTURE = SKILL / 'tests' / 'fixtures' / 'schema3-intermediates.json'


def fixture():
    return records.validate_records(json.loads(FIXTURE.read_text(encoding='utf-8')))


def revised(data):
    data.pop('snapshot_id', None)
    return records.validate_records(data)


def reviewed(data):
    data = deepcopy(data)
    targets = [{'collection': collection, 'id': row['id']}
               for collection in ('items', 'uses') for row in data[collection]]
    data['observations'].extend(records.make_observations(data, targets, 'Synthetic reviewer'))
    return data


class ReviewWordingTests(unittest.TestCase):
    def test_unreviewed_is_not_described_as_reviewed_with_questions(self):
        status = records.comparison_status(fixture())
        self.assertEqual(status['unreviewed'], status['total'])
        self.assertIn(f"0 of {status['total']} records reviewed", status['summary'])
        self.assertIn('not yet reviewed', status['summary'])

    def test_reviewed_open_question_keeps_unresolved_status_with_clear_summary(self):
        data = reviewed(fixture())
        data['observations'].extend(records.make_observations(
            data, [{'collection': 'items', 'id': 'independence'}],
            'Synthetic reviewer', result='needs_attention', note='An unresolved source question.'))
        status = records.comparison_status(data)
        self.assertEqual(status['status'], 'incomplete')  # Resolution has not been fabricated.
        self.assertEqual((status['unreviewed'], status['stale'], status['needs_attention']), (0, 0, 1))
        self.assertIn(f"All {status['total']} records reviewed", status['summary'])
        self.assertIn('unresolved source questions', status['summary'])
        report = records.record_report(data, FIXTURE.parent)
        self.assertTrue(any(status['summary'] in warning for warning in report['warnings']))
        self.assertFalse(any('Source comparison is incomplete:' in warning for warning in report['warnings']))

    def test_changed_records_require_review_again(self):
        data = reviewed(fixture())
        data['items'][0]['statement']['text'] += ' Changed hypothesis.'
        status = records.comparison_status(revised(data))
        self.assertGreater(status['stale'], 0)
        self.assertIn('need review after changes', status['summary'])
        self.assertNotIn(f"All {status['total']} records reviewed", status['summary'])

    def test_matched_summary_does_not_claim_proof_verification(self):
        status = records.comparison_status(reviewed(fixture()))
        self.assertEqual(status['status'], 'complete')
        self.assertIn('no source-comparison questions remain unresolved', status['summary'])
        self.assertNotIn('proved', status['summary'])


class MathematicalProseTests(unittest.TestCase):
    def data(self):
        data = fixture()
        data['scope'] = r'Overview for \(n \geq 1\).' + '\n\n' + r'Retain the limit \[x_n\to 0\].'
        for row in data['items']:
            row['issue'] = r'Check \(x_n^2\) and preserve <script>untrusted()</script> as text.'
        for row in data['uses']:
            row['issue'] = r'Is $\alpha > 0$ required?'
            row['regime'] = r'When \(n \geq 2\)'
        return revised(data)

    def test_math_fields_preserve_authored_records_and_original_annotations(self):
        data = self.data()
        original = deepcopy(data)
        prepared = records.prepare_records(data, FIXTURE.parent)
        self.assertEqual(data, original)
        self.assertEqual(prepared['math_diagnostics'], [])
        for row in prepared['items'] + prepared['details']:
            self.assertIn('<math', row['issue_html'])
            self.assertIn('x_n^2', row['issue_html'])
            self.assertIn('&lt;script&gt;', row['issue_html'])
            self.assertNotIn('<script>', row['issue_html'])
        for row in prepared['uses'] + prepared['detail_uses']:
            for field in ('issue', 'regime'):
                self.assertIn('<math', row[field + '_html'])
                self.assertIn('application/x-tex', row[field + '_html'])
        self.assertIn('<math', prepared['scope_display']['lead_html'])
        self.assertIn('display="block"', prepared['scope_display']['details_html'])

    def test_unsupported_math_is_located_in_each_affected_field(self):
        data = self.data()
        data['items'][0]['issue'] = r'Check $\unresolvedIssueMacro{x}$.'
        data['uses'][0]['regime'] = r'When $\unresolvedRegimeMacro{x}$.'
        data['scope'] = r'Only $\unresolvedScopeMacro{x}$ is supplied.'
        prepared = records.prepare_records(revised(data), FIXTURE.parent)
        locations = {(entry['collection'], entry['id'], entry['field'])
                     for entry in prepared['math_diagnostics']}
        self.assertEqual(locations, {
            ('items', data['items'][0]['id'], 'issue'),
            ('uses', data['uses'][0]['id'], 'regime'),
            ('metadata', 'overview', 'scope'),
        })
        self.assertIn('LaTeX (not rendered)', prepared['items'][0]['issue_html'])

    @unittest.skipUnless(shutil.which('node'), 'Shared Node.js required')
    def test_html_panels_index_and_scope_use_prepared_math(self):
        data = reviewed(self.data())
        data['observations'].extend(records.make_observations(
            data, [{'collection': 'items', 'id': 'independence'}],
            'Synthetic reviewer', result='needs_attention'))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'overview.html'
            receipt = overview.render_dataset(data, FIXTURE.parent, output)
            html = output.read_text(encoding='utf-8')
        self.assertEqual(receipt['math_diagnostics'], [])
        self.assertEqual(receipt['graph_preservation']['status'], 'pass')
        panels = re.findall(r'<template id="proof-detail-[^"]+">([\s\S]*?)</template>', html)
        self.assertTrue(panels)
        self.assertTrue(any('proof-uncertainty' in panel and '<math' in panel for panel in panels))
        issues = re.findall(r'<div class="proof-uncertainty">([\s\S]*?)</div>', html)
        self.assertTrue(issues)
        self.assertTrue(all('<math' in issue for issue in issues))
        regimes = re.findall(r'<span class="proof-regime">([\s\S]*?)</math>', html)
        self.assertTrue(regimes)
        self.assertTrue(all('<math' in regime for regime in regimes))
        lead = re.search(r'<p class="proof-scope-lead">([\s\S]*?)</p>', html).group(1)
        self.assertIn('<math', lead)
        self.assertIn('Reviewed; source question unresolved', html)
        self.assertIn(records.comparison_status(data)['summary'], html)
        self.assertNotIn('<script>untrusted()</script>', html)


if __name__ == '__main__':
    unittest.main()

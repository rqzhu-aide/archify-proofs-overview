"""Source reuse is local to an operation and cannot replace anchor checks."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import paper_records as records


class SourceReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'paper.pdf'
        self.source.write_bytes(b'captured PDF revision one')
        self.pages = [Mock(extract_text=Mock(return_value=text))
                      for text in ('First physical page', 'Second physical page')]
        self.reader = Mock(return_value=SimpleNamespace(pages=self.pages))
        self.pdf_module = SimpleNamespace(PdfReader=self.reader)
        self.seed = {
            'schema_version': 3, 'title': 'Source reuse fixture',
            'scope': 'Synthetic source-binding checks.',
            'source': {'title': 'Synthetic paper', 'file': 'paper.pdf'},
            'items': [
                {'id': f'item-{index}', 'kind': 'theorem', 'label': f'Theorem {index}',
                 'caption': 'Synthetic result',
                 'statement': {'text': 'A synthetic statement.', 'form': 'synopsis'},
                 'source': {'page': page}}
                for index, page in enumerate((1, 2, 1, 2), 1)
            ],
            'uses': [],
        }

    def normalize(self):
        with patch.dict(sys.modules, {'pypdf': self.pdf_module}):
            return records.normalize(self.seed, self.base)

    def validate(self, data):
        with patch.dict(sys.modules, {'pypdf': self.pdf_module}):
            return records.validate_records(data)

    def reset_counts(self):
        self.reader.reset_mock()
        for page in self.pages:
            page.extract_text.reset_mock()

    def assert_single_read(self):
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual([page.extract_text.call_count for page in self.pages], [1, 1])

    def test_normalize_reuses_reader_and_each_distinct_page(self):
        original = deepcopy(self.seed)
        data = self.normalize()
        self.assert_single_read()
        self.assertEqual([row['excerpt'] for row in data['anchors']],
                         ['First physical page', 'Second physical page'] * 2)
        self.assertTrue(all(row['verification']['status'] == 'checked' for row in data['anchors']))
        self.assertEqual(self.seed, original)

    def test_control_glyphs_are_replaced_only_in_pdf_excerpts_with_visible_limits(self):
        self.pages[0].extract_text.return_value = 'A\x00 + B\x10\n\r\tEnd'
        raw = self.source.read_bytes()
        data = self.normalize()
        self.assert_single_read()
        damaged = data['anchors'][0]
        self.assertEqual(damaged['excerpt'], 'A\ufffd + B\ufffd\n\r\tEnd')
        self.assertEqual(damaged['verification']['status'], 'checked')
        self.assertIn('paper.pdf, physical PDF page 1', damaged['verification']['note'])
        self.assertIn('2 unsupported control character(s)', damaged['verification']['note'])
        self.assertNotIn('note', data['anchors'][1]['verification'])
        self.assertEqual(base64.b64decode(data['source_revision']['files'][0]['content_base64']), raw)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(data['observations'], [])
        self.assertEqual(self.validate(data), data)
        with patch.dict(sys.modules, {'pypdf': self.pdf_module}):
            report = records.record_report(data, self.base)
        warnings = [w for w in report['warnings'] if 'replacement characters' in w]
        self.assertEqual(len(warnings), 1)
        self.assertIn('1 captured page(s)', warnings[0])  # Two anchors reuse this page.

    def test_pdf_cleanup_does_not_rewrite_retained_excerpts_or_authored_controls(self):
        data = self.normalize()
        self.pages[0].extract_text.return_value = 'Later extraction \x00'
        self.assertEqual(self.validate(data), data)
        self.seed['items'][0]['statement']['text'] = 'Authored \x00 corruption'
        with self.assertRaisesRegex(records.RecordError, 'unexpected control character'):
            self.normalize()
        self.seed['items'][0]['statement']['text'] = 'A valid statement.'
        self.source = self.base / 'paper.txt'
        self.source.write_bytes(b'Captured text \x00 corruption\n')
        self.seed['source']['file'] = 'paper.txt'
        for item in self.seed['items']:
            item['source'] = {'start_line': 1, 'end_line': 1}
        with self.assertRaisesRegex(records.RecordError, 'unexpected control character'):
            self.normalize()

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the public render path')
    def test_pdf_cleanup_survives_common_database_edit_comparison_refresh_and_render(self):
        import paper_database as database

        self.pages[0].extract_text.return_value = 'A\x00 + B\x10'
        self.seed['main_items'] = ['item-1']
        self.seed['uses'] = [{'id': 'u21', 'from': 'item-2', 'to': 'item-1',
                             'reason': 'The second result supplies the bound.', 'source': {'page': 2}}]
        seed_path = self.base / 'seed.json'
        seed_path.write_text(json.dumps(self.seed), encoding='utf-8')
        db_path = self.base / 'paper.sqlite'
        raw = self.source.read_bytes()
        with patch.dict(sys.modules, {'pypdf': self.pdf_module}):
            initial = database.init_database(db_path, seed_path, focused=True, source_root=self.base)
            self.assertEqual(initial['backend'], 'paper_core')
            packet = database.get_packet(db_path, 'item-1')
            self.assertEqual(packet['target_fidelity'], 'unreviewed')
            self.assertTrue(any('Missing glyph meanings' in a['verification'].get('note', '')
                                for a in packet['anchors']))
            from paper_core.storage import Database
            with Database(db_path) as common:
                damaged_anchor = next(a for a in common.heads('anchors') if '\ufffd' in a.body['excerpt'])
                self.assertIn('Missing glyph meanings', damaged_anchor.body['limitation'])
            with self.assertRaisesRegex(database.DatabaseError, 'get <database> item-1'):
                database.get_packet(db_path, 'u21')
            item = deepcopy(packet['item'])
            item['caption'] = 'A clearer result caption'
            changed = database.apply_edits(db_path, {'expected_snapshot': packet['expected_snapshot'],
                'edits': [{'collection': 'items', 'op': 'upsert', 'id': 'item-1', 'record': item}]})
            database.compare_records(db_path, {'expected_snapshot': changed['snapshot_id'],
                'targets': [{'collection': 'items', 'id': 'item-1'}], 'reviewer': 'test',
                'result': 'needs_attention', 'note': 'The damaged glyphs need inspection in the original PDF.'})
            self.assertEqual(database.get_packet(db_path, 'item-1')['target_fidelity'], 'needs_attention')
            data = database.export_snapshot(db_path)
            self.assertEqual(base64.b64decode(data['source_revision']['files'][0]['content_base64']), raw)
            self.source.write_bytes(raw + b' revised')
            database.refresh_database(db_path, data['snapshot_id'])
            self.assertEqual(database.get_packet(db_path, 'item-1')['target_fidelity'], 'stale')
            current = database.export_snapshot(db_path)
            self.assertTrue(any('\ufffd' in a['excerpt'] for a in current['anchors']))
            report = database.render_database(db_path, self.base / 'overview.html')
            self.assertTrue(any('replacement characters' in w for w in report['warnings']))
            html = (self.base / 'overview.html').read_text(encoding='utf-8')
            self.assertIn('Missing glyph meanings were not recovered', html)
            self.assertEqual(database.export_snapshot(db_path), current)

    def test_validation_reuses_sources_without_mutating_or_persisting_state(self):
        data = self.normalize()
        original = deepcopy(data)
        for _ in range(2):
            self.reset_counts()
            with patch.object(records, '_source_bytes', wraps=records._source_bytes) as checked:
                self.assertEqual(self.validate(data), data)
            self.assertEqual(checked.call_count, 1)
            self.assert_single_read()
        self.assertEqual(data, original)

    def test_later_anchor_bounds_and_locator_types_are_still_checked(self):
        data = self.normalize()
        for locator, message in (({'page': 3}, 'exceeds its 2 physical pages'),
                                 ({'page': True}, 'expected a positive integer')):
            with self.subTest(locator=locator):
                candidate = deepcopy(data)
                candidate.pop('snapshot_id')
                candidate['anchors'][-1]['locator'] = locator
                self.reset_counts()
                with self.assertRaisesRegex(records.RecordError, message):
                    self.validate(candidate)
                self.assert_single_read()

    def test_cached_page_does_not_replace_each_anchor_verification(self):
        candidate = self.normalize()
        candidate.pop('snapshot_id')
        candidate['anchors'][-1]['locator']['label'] = 'Unmatched printed label'
        self.reset_counts()
        with self.assertRaisesRegex(records.RecordError, 'marked checked without reproducible'):
            self.validate(candidate)
        self.assert_single_read()

    def test_each_excerpt_hash_is_checked_and_pdf_transcriptions_are_retained(self):
        candidate = self.normalize()
        candidate.pop('snapshot_id')
        candidate['anchors'][-1]['excerpt'] = 'A retained manual PDF transcription'
        with self.assertRaisesRegex(records.RecordError, 'excerpt hash mismatch'):
            self.validate(candidate)
        candidate['anchors'][-1]['excerpt_hash'] = records._sha(candidate['anchors'][-1]['excerpt'].encode())
        self.assertEqual(self.validate(candidate)['anchors'][-1]['excerpt'],
                         'A retained manual PDF transcription')

    def test_corrupt_bytes_cannot_borrow_another_files_successful_hash_check(self):
        data = self.normalize()
        for content, message in (('!', 'invalid captured content'),
                                 (base64.b64encode(b'changed bytes').decode(), 'content hash does not match')):
            with self.subTest(content=content):
                candidate = deepcopy(data)
                candidate.pop('snapshot_id')
                source = deepcopy(candidate['source_revision']['files'][0])
                source.update(id='file-second', path='second.pdf', content_base64=content)
                candidate['source_revision']['files'].append(source)
                candidate['source_revision']['id'] = records._source_digest(candidate['source_revision']['files'])
                self.reset_counts()
                with self.assertRaisesRegex(records.RecordError, message):
                    self.validate(candidate)
                self.assertEqual(self.reader.call_count, 0)

    def test_refresh_reuses_unchanged_source_and_reads_new_revision_separately(self):
        data = self.normalize()
        original = deepcopy(data)
        self.reset_counts()
        with patch.dict(sys.modules, {'pypdf': self.pdf_module}):
            self.assertEqual(records.refresh_sources(data, self.base), data)
        self.assert_single_read()

        revised = b'captured PDF revision two'
        self.source.write_bytes(revised)
        new_pages = [Mock(extract_text=Mock(return_value=f'Revised page {index}'))
                     for index in (1, 2)]
        self.reader.side_effect = lambda stream: SimpleNamespace(
            pages=new_pages if stream.getvalue() == revised else self.pages)
        self.reset_counts()
        with patch.dict(sys.modules, {'pypdf': self.pdf_module}):
            refreshed = records.refresh_sources(data, self.base)
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual([page.extract_text.call_count for page in self.pages], [1, 1])
        self.assertEqual([page.extract_text.call_count for page in new_pages], [1, 1])
        self.assertEqual([row['excerpt'] for row in refreshed['anchors']], ['Revised page 1', 'Revised page 2'] * 2)
        self.assertNotEqual(refreshed['source_revision']['id'], data['source_revision']['id'])
        self.assertEqual(data, original)

    def test_different_public_calls_cannot_reuse_prior_revision_content(self):
        data = self.normalize()
        self.source.write_bytes(b'a later PDF revision')
        for index, page in enumerate(self.pages, 1):
            page.extract_text.return_value = f'Later page {index}'
        self.reset_counts()
        later = self.normalize()
        self.assert_single_read()
        self.assertEqual([row['excerpt'] for row in later['anchors']], ['Later page 1', 'Later page 2'] * 2)
        self.assertNotEqual(later['source_revision']['id'], data['source_revision']['id'])

    def test_missing_parser_is_unverified_and_cannot_reproduce_checked_anchors(self):
        checked = self.normalize()
        with patch.dict(sys.modules, {'pypdf': None}):
            data = records.normalize(self.seed, self.base)
            for anchor in data['anchors']:
                self.assertEqual(anchor['verification']['status'], 'unverified')
                self.assertEqual(anchor['verification']['method'], 'entered_locator')
                self.assertIn('ModuleNotFoundError', anchor['verification']['note'])
                self.assertEqual(anchor['excerpt'], '')
            with self.assertRaisesRegex(records.RecordError, 'marked checked without reproducible'):
                records.validate_records(checked)

    def test_parser_and_extraction_failures_stay_honest_when_reused(self):
        checked = self.normalize()
        self.reset_counts()
        self.reader.side_effect = OSError('Cannot open PDF')
        data = self.normalize()
        self.assertEqual(self.reader.call_count, 1)
        for anchor in data['anchors']:
            self.assertEqual(anchor['verification']['status'], 'unverified')
            self.assertEqual(anchor['verification']['method'], 'entered_locator')
            self.assertIn('OSError', anchor['verification']['note'])

        self.reader.side_effect = None
        self.pages[0].extract_text.side_effect = RuntimeError('Cannot extract page')
        self.reset_counts()
        data = self.normalize()
        self.assert_single_read()
        for anchor in data['anchors']:
            self.assertEqual(anchor['verification']['method'], 'pdf_page_bounds')
            if anchor['locator']['page'] == 1:
                self.assertEqual(anchor['verification']['status'], 'unverified')
                self.assertIn('RuntimeError', anchor['verification']['note'])
                self.assertEqual(anchor['excerpt'], '')
            else:
                self.assertEqual(anchor['verification']['status'], 'checked')
        with self.assertRaisesRegex(records.RecordError, 'marked checked without reproducible'):
            self.validate(checked)

    def test_text_decoding_and_splitting_are_reused_but_ranges_are_checked(self):
        self.source = self.base / 'paper.tex'
        self.source.write_text('First line\nSecond line\nThird line\n', encoding='utf-8')
        self.seed['source']['file'] = 'paper.tex'
        for item in self.seed['items']:
            item['source'] = {'start_line': 1, 'end_line': 2}
        calls = {'decode': 0, 'splitlines': 0}

        class CountedText(str):
            def splitlines(self, *args, **kwargs):
                calls['splitlines'] += 1
                return super().splitlines(*args, **kwargs)

        class CountedBytes(bytes):
            def decode(self, *args, **kwargs):
                calls['decode'] += 1
                return CountedText(super().decode(*args, **kwargs))

        original_reader = records._source_bytes
        with patch.object(records, '_source_bytes', side_effect=lambda row: CountedBytes(original_reader(row))):
            data = records.normalize(self.seed, self.base)
        self.assertEqual(calls, {'decode': 1, 'splitlines': 1})
        for locator, excerpt, message in (({'start_line': 3, 'end_line': 4}, None, 'outside or reversed'),
                                          ({'start_line': 2, 'end_line': 3}, None, 'excerpt differs'),
                                          ({'start_line': 1, 'end_line': 2}, 'Wrong text', 'excerpt differs')):
            with self.subTest(locator=locator, excerpt=excerpt):
                candidate = deepcopy(data)
                candidate.pop('snapshot_id')
                candidate['anchors'][-1]['locator'] = locator
                if excerpt:
                    candidate['anchors'][-1].update(excerpt=excerpt, excerpt_hash=records._sha(excerpt.encode()))
                with self.assertRaisesRegex(records.RecordError, message):
                    records.validate_records(candidate)


if __name__ == '__main__':
    unittest.main()

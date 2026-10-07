from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ocr_pages import VIS, database, line
from test_split_pages import make_pdf

import main
from scripts import ocr_pages as ocr
from scripts import review_pages as review


def parts_table(badge='91034'):
    lines = [line(badge, (100, 10, 200, 30)),
             line('98749524F4', (50, 60, 250, 80))]
    for i, value in enumerate(('9873493280', '9873494280', '9876252480', '9875083080')):
        y = 110 + 40 * i
        lines.extend((line(value, (10, y, 210, y+20)), line('OK', (300, y, 340, y+20))))
    return lines


class ReviewRulesTests(unittest.TestCase):
    def setUp(self):
        self.lookup = ocr.Lookup(database())
        self.previous = {('1', 24): (VIS, '1_24', None)}

    def test_crop_removes_border_but_keeps_center_content_and_uses_interior_area(self):
        import numpy as np
        pixels = np.full((100, 100), 255, dtype=np.uint8)
        pixels[:2, :] = pixels[-2:, :] = 0
        pixels[:, :2] = pixels[:, -2:] = 0
        self.assertGreater(review.pixel_ink_percent(pixels, crop_percent=0), 7)
        self.assertEqual(review.pixel_ink_percent(pixels), 0)
        pixels[40:50, 40:50] = 0
        self.assertAlmostEqual(review.pixel_ink_percent(pixels), 100/9216*100)
        # A large scanner corner is intentionally not erased by a narrow crop.
        pixels[80:, :20] = 0
        self.assertGreater(review.pixel_ink_percent(pixels), .9)

    def test_defaults_and_new_cutoff_boundary(self):
        args = review.parse_args(['-d', '/Review'])
        self.assertEqual((args.max_ink_percent, args.crop_percent), (.9, 2))
        self.assertEqual(review.decide('1_25', [], .9, self.lookup, self.previous)[0].vis, VIS)
        self.assertIsNone(review.decide('1_25', [], .90001, self.lookup, self.previous)[0].vis)
        for value in (-1, 25, float('nan')):
            with self.assertRaises(ValueError):
                import numpy as np
                review.pixel_ink_percent(np.ones((10, 10)), crop_percent=value)

    def test_parts_table_all_rotations_and_database_enrichment(self):
        for rotation in range(4):
            lines = [review.rotated(item, rotation) for item in parts_table()]
            decision = review.table_badge(lines, self.lookup)
            self.assertEqual(decision.vis, VIS)
            self.assertEqual(decision.reason, 'TABLE_BADGE')
            self.assertEqual(ocr.enriched_row('1_52', decision, self.lookup.database)[1], '91034')

    def test_arbitrary_badge_and_other_checklist_do_not_become_parts_tables(self):
        for lines in ([line('91034')], [line('CHECK-LIST DES DEFAUTS'), line('91034')],
                      [item for item in parts_table() if item.text != 'OK']):
            self.assertIsNone(review.table_badge(lines, self.lookup).vis)

    def test_unknown_ambiguous_low_confidence_and_conflicting_table(self):
        self.assertEqual(review.table_badge(parts_table('99999'), self.lookup).reason,
                         'UNKNOWN_TABLE_BADGE')
        data = database()
        data.badge_vis['91034'] = frozenset({VIS, 'S5780509'})
        self.assertIsNone(review.table_badge(parts_table(), ocr.Lookup(data)).vis)
        weak = parts_table()
        weak[0] = line('91034', weak[0].box, .6)
        self.assertIsNone(review.table_badge(weak, self.lookup).vis)
        self.assertEqual(review.table_badge(parts_table()+[line('VIS S5780509')], self.lookup).reason,
                         'IDENTIFIER_CONFLICT')

    def test_sparse_inheritance_exact_predecessor_and_conflict_guards(self):
        decision, name = review.decide('1_25', [], .2,
                                      self.lookup, self.previous)
        self.assertEqual((decision.vis, decision.reason, name), (VIS, 'PREVIOUS_PAGE', '1_24'))
        for name in ('2_25', '1_26', 'bad-name'):
            self.assertIsNone(review.decide(name, [], .1, self.lookup, self.previous)[0].vis)
        for lines in ([line('Identifiant G028 / 91034'), line('BDG 91069')],
                      [line('VIS '+VIS), line('VIS S5780509')],
                      [line('SEQ 126183161'), line('SEQ 126183182')]):
            self.assertEqual(review.decide('1_25', lines, .1, self.lookup, self.previous)[0].reason,
                             'IDENTIFIER_CONFLICT')
        self.assertIsNone(review.decide('1_25', [], 1, self.lookup, self.previous)[0].vis)

    def test_bdd_badge_classifies_dense_page_without_table_or_predecessor(self):
        for name, density, text in (('1_373', .912481, 'Identifiant G028 / 91034'),
                                    ('2_443', 3.715422, 'Identifiant G028 / 91034')):
            decision, previous = review.decide(name, [line(text), line('BRYEKNFJ1T6704898')],
                                               density, self.lookup, {})
            self.assertEqual((decision.vis, decision.reason, previous), (VIS, 'BDD_MATCH', ''))

    def test_optional_reference_and_alternative_reference_do_not_cancel_badge(self):
        for reference in ('98749524PR', None):
            lines = [item for item in parts_table() if item.text != '98749524F4']
            if reference:
                lines.append(line(reference, (50, 60, 250, 80)))
            for turns in range(4):
                decision, _ = review.decide('1_52', [review.rotated(item,turns) for item in lines],
                                            5, self.lookup, {})
                self.assertEqual((decision.vis, decision.reason), (VIS,'TABLE_BADGE'))

    def test_measurements_do_not_conflict_with_identifier_or_classify_alone(self):
        data = database()
        data.badge_vis['12060'] = frozenset({'S5780509'})
        lookup = ocr.Lookup(data)
        lines = [line('Identifiant G024 / 91034', (10,10,300,30)),
                 line('Couple Angle', (10,100,200,120)), line('12060',(10,150,100,170))]
        for turns in range(4):
            decision = review.resolve_review_text([review.rotated(item,turns) for item in lines],lookup)
            self.assertEqual((decision.vis,decision.badges),(VIS,('91034',)))
        self.assertIsNone(review.resolve_review_text([lines[-1]],lookup).vis)
        self.assertEqual(review.resolve_review_text(lines+[line('Identifiant G028 / 12060')],lookup).reason,
                         'IDENTIFIER_CONFLICT')

    def test_vehicle_header_retains_badge_conflicts_and_needs_header_context(self):
        lines = [line('Sequence:', (10,10,150,30)), line('126183161',(10,40,200,60)),
                 line('NVIN: BRYEKNFJ1T5704898',(10,80,400,110)),
                 line('91069*',(500,120,590,140))]
        self.assertEqual(review.resolve_review_text(lines,self.lookup).reason,'IDENTIFIER_CONFLICT')
        lines[3] = line('91034*',lines[3].box)
        self.assertEqual(review.resolve_review_text(lines,self.lookup).vis,VIS)
        self.assertIsNone(review.resolve_review_text([lines[3]],self.lookup).vis)

    def test_unknown_table_badge_does_not_cancel_valid_vin(self):
        decision,_ = review.decide('1_52',parts_table('99999')+[line('BRYEKNFJ1T5704898')],
                                   5,self.lookup,{})
        self.assertEqual(decision.vis,VIS)

    def test_table_rule_precedes_sparse_rule(self):
        decision, _ = review.decide('1_25', parts_table('91069'), .1, self.lookup, self.previous)
        self.assertEqual(decision.vis, 'S5780509')
        self.assertEqual(decision.reason, 'TABLE_BADGE')

    def test_persisted_inherited_pages_cannot_start_a_chain_and_duplicates_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / VIS
            folder.mkdir()
            make_pdf(folder / '1_24.pdf', ['front'])
            make_pdf(folder / '1_25.pdf', ['back'])
            (root / 'review_assignments.jsonl').write_text(json.dumps(
                {'name': '1_25', 'method': 'PREVIOUS_PAGE'})+'\n')
            index = review.independent_pages(root, {})
            self.assertIn(('1', 24), index)
            self.assertNotIn(('1', 25), index)
            other = root / 'S5780509'
            other.mkdir()
            make_pdf(other / '1_24.pdf', ['different'])
            self.assertIsNone(review.independent_pages(root, {})[('1', 24)])

    def test_independent_table_predecessor_keeps_its_enriched_metadata_on_later_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root/VIS
            folder.mkdir()
            path = folder/'1_52.pdf'
            make_pdf(path, ['parts table'])
            row = ocr.enriched_row('1_52', ocr.Decision(VIS, 'TABLE_BADGE', badges=('91034',)),
                                   self.lookup.database)
            (root/'review_assignments.jsonl').write_text(json.dumps({
                'name': '1_52', 'method': 'TABLE_BADGE', 'row': row,
                'sha256': ocr.file_digest(path)})+'\n')
            index = review.independent_pages(root, {}, {('1', 52)})
            self.assertEqual(index[('1', 52)][2], row)
            self.assertEqual(review.decide('1_53', [], .1, self.lookup, index)[0].vis, VIS)


class ReviewRunTests(unittest.TestCase):
    def prepare(self, root):
        source = root / 'Review'
        source.mkdir()
        folder = root / VIS
        folder.mkdir()
        make_pdf(folder / '1_24.pdf', ['front'])
        for name in ('1_25', '1_26', '1_52', '1_35'):
            make_pdf(source / f'{name}.pdf', [name])
        cache = ocr.OcrCache(root / '.ocr_cache.sqlite3', ocr.ocr_settings(200, 'cpu'))
        for name, lines in (('1_25', []), ('1_26', []), ('1_52', parts_table()),
                            ('1_35', [line('CHECK-LIST DES DEFAUTS')])):
            cache.put(ocr.file_digest(source / f'{name}.pdf'), lines)
        cache.close()
        return source

    def execute(self, root, source, *flags):
        args = review.parse_args(['--legacy-layout', '-d', str(source), '--cache-only', *flags])
        with patch.object(ocr, 'discover_bdd', return_value=root/'BDD.xlsx'), \
             patch.object(review.classify, 'load_database', return_value=database()), \
             patch.object(ocr, 'PaddleEngine', side_effect=AssertionError('Must use cache')), \
             patch.object(review, 'ink_percent', side_effect=lambda path, _threshold, _crop: .1 if path.stem in
                          {'1_25', '1_26'} else 5):
            return review.run(args)

    def test_cached_run_progressive_transfers_metadata_and_no_chain_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            self.assertEqual(self.execute(root, source), 0)
            self.assertTrue((root/VIS/'1_25.pdf').exists())
            self.assertTrue((root/VIS/'1_52.pdf').exists())
            self.assertTrue((source/'1_26.pdf').exists())
            self.assertTrue((source/'1_35.pdf').exists())
            self.assertEqual(self.execute(root, source), 0)
            self.assertTrue((source/'1_26.pdf').exists())
            results = min((root/'review_runs').glob('*/results.csv'))
            with results.open(encoding='utf-8-sig', newline='') as handle:
                rows = {row['lot_page']: row for row in csv.DictReader(handle)}
            self.assertEqual(rows['1_52']['BDG'], '91034')
            self.assertEqual(rows['1_52']['SEQ_9'], '118300161')
            self.assertEqual(rows['1_25']['Predecessor'], '1_24')
            self.assertEqual(rows['1_35']['VIS'], '')
            self.assertTrue((results.parent/'pages.xlsx').exists())

    def test_dry_run_preserves_all_pdfs_and_has_no_assignment_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            before = {path.name: path.read_bytes() for path in source.iterdir()}
            self.assertEqual(self.execute(root, source, '--dry-run'), 0)
            self.assertEqual(before, {path.name: path.read_bytes() for path in source.iterdir()})
            self.assertFalse((root/'review_assignments.jsonl').exists())

    def test_collision_retains_source_and_continues_next_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            make_pdf(root/VIS/'1_25.pdf', ['different existing pdf'])
            self.assertEqual(self.execute(root, source), 1)
            self.assertTrue((source/'1_25.pdf').exists())
            self.assertTrue((source/'1_26.pdf').exists())
            self.assertTrue((root/VIS/'1_52.pdf').exists())

    def test_cache_only_miss_never_runs_ocr_or_inherits_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            cache = ocr.OcrCache(root/'.ocr_cache.sqlite3', ocr.ocr_settings(200, 'cpu'))
            cache.connection.execute('DELETE FROM ocr')
            cache.connection.commit()
            cache.close()
            self.assertEqual(self.execute(root, source), 0)
            self.assertEqual(len(list(source.glob('*.pdf'))), 4)

    def test_menu_command(self):
        command = main.build_command('review_pages.py', Path('/Review'), output=Path('/out'), dry_run=True)
        self.assertIn('--dry-run', command)
        self.assertIn('--output', command)

    def test_cache_miss_runs_ocr_once_and_reuses_raw_results_on_second_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'Review'
            source.mkdir()
            make_pdf(source/'1_52.pdf', ['parts table'])
            args = review.parse_args(['--legacy-layout', '-d', str(source), '--dry-run'])
            with patch.object(ocr, 'discover_bdd', return_value=root/'BDD.xlsx'), \
                 patch.object(review.classify, 'load_database', return_value=database()), \
                 patch.object(review, 'ink_percent', return_value=5), \
                 patch.object(ocr, 'PaddleEngine') as engine:
                engine.return_value.read.return_value = parts_table()
                self.assertEqual(review.run(args), 0)
                engine.return_value.read.assert_called_once()
                engine.reset_mock()
                engine.side_effect = AssertionError('Second run must hit cache')
                self.assertEqual(review.run(args), 0)
                engine.assert_not_called()

    def test_final_export_failure_does_not_lose_transferred_pages_or_progress_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.prepare(root)
            with patch.object(review.classify, 'write_page_workbook', side_effect=OSError('export failed')), \
                 self.assertRaisesRegex(OSError, 'export failed'):
                self.execute(root, source)
            self.assertTrue((root/VIS/'1_52.pdf').exists())
            results = next((root/'review_runs').glob('*/results.csv'))
            with results.open(encoding='utf-8-sig', newline='') as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 4)
            self.assertIn('Status: FAILED', (results.parent/'report.txt').read_text())


if __name__ == '__main__':
    unittest.main()

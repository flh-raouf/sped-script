from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

import main
from scripts import classify_pages as classify
from scripts import script as barcode
from test_split_pages import make_pdf, page_count, page_text


class ResolutionTests(unittest.TestCase):
    def test_report_enrichment_preserves_zeroes_and_leaves_unknowns_blank(self):
        record = classify.VehicleRecord('00123', 'VF1ABCDEFT5702376', 'T5702376',
                                        'SEQEMON0100000123', '4A2D0004')
        db = classify.DatabaseIndex({'00123': frozenset({'T5702376'})}, {'T5702376': [record]})
        expected = ('1_2', '00123', 'VF1ABCDEFT5702376', 'T5702376',
                    'SEQEMON0100000123', '100000123', '4A2D0004')
        for identifiers in ({'badges': ('00123',)}, {'vins': ('VF1ABCDEFT5702376',)}):
            self.assertEqual(classify.page_excel_row(barcode.PageResult('1', 2, 3, **identifiers),
                                                    'T5702376', db), expected)
        page = barcode.PageResult('1', 2, 3, badges=('99999',))
        self.assertEqual(classify.page_excel_row(page, None, db), ('1_2', '', '', '', '', '', ''))
        # A decoded VIN absent from the BDD stays usable; absent fields stay blank.
        page = barcode.PageResult('1', 2, 3, vins=('VF1ABCDEFT5702377',))
        self.assertEqual(classify.page_excel_row(page, 'T5702377', db),
                         ('1_2', '', 'VF1ABCDEFT5702377', 'T5702377', '', '', ''))

    def test_report_does_not_choose_between_conflicting_database_values(self):
        db = classify.DatabaseIndex(records_by_vis={'T5702376': [
            classify.VehicleRecord('123', '', 'T5702376', 'SEQ000000001', 'NOF1'),
            classify.VehicleRecord('456', '', 'T5702376', 'SEQ000000002', 'NOF2'),
        ]})
        page = barcode.PageResult('1', 1, 1, vins=('VF1ABCDEFT5702376',))
        self.assertEqual(classify.page_excel_row(page, 'T5702376', db),
                         ('1_1', '', 'VF1ABCDEFT5702376', 'T5702376', '', '', ''))
        page = barcode.PageResult('1', 1, 1, badges=('123',))
        self.assertEqual(classify.page_excel_row(page, 'T5702376', db),
                         ('1_1', '123', '', 'T5702376', 'SEQ000000001', '000000001', 'NOF1'))

    def test_bdd_extracts_vin_from_id2_and_seq_from_sem(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'BDD.xlsx'
            wb = Workbook()
            wb.active.append(['BDG', 'VIS', 'SEM', 'ID2', 'NOF'])
            wb.active.append(['00123', 'T5702376', '50FV750960305', 'VF1ABCDEFT5702376', '4A2D0004'])
            wb.active.append(['456', 'T5702377', '', 'not a VIN', ''])
            wb.save(path)
            wb.close()
            db = classify.load_database(path)
            page = barcode.PageResult('1', 1, 1, badges=('00123',))
            self.assertEqual(classify.page_excel_row(page, 'T5702376', db),
                             ('1_1', '00123', 'VF1ABCDEFT5702376', 'T5702376',
                              '50FV750960305', '750960305', '4A2D0004'))
            self.assertEqual(db.records_by_vis['T5702377'][0].vin, '')

    def test_badge_fallback_and_direct_vin(self):
        db = {'100000': frozenset({'T5708495'})}
        page = barcode.PageResult('479', 1, 2, badges=('100000',))
        self.assertEqual(classify.resolve_vis(page, db), ('T5708495', 'BDD_BADGE'))
        page = barcode.PageResult('479', 1, 2, vins=('VF1ABCDEFT5702376',))
        self.assertEqual(classify.resolve_vis(page, db), ('T5702376', 'BARCODE'))

    def test_conflicting_or_unresolved_identifiers_do_not_choose_a_vis(self):
        cases = [
            (barcode.PageResult('1', 1, 1), {}, 'NO_BARCODE'),
            (barcode.PageResult('1', 1, 1, badges=('12',)), {}, 'UNRESOLVED_BADGE'),
            (barcode.PageResult('1', 1, 1, error='broken'), {}, 'SCAN_ERROR'),
            (barcode.PageResult('1', 1, 1, badges=('12',)),
             {'12': frozenset({'T5702376', 'T5702377'})}, 'BDD_CONFLICT'),
            (barcode.PageResult('1', 1, 1, badges=('12',), vins=('VF1ABCDEFT5702376',)),
             {'12': frozenset({'T5702377'})}, 'IDENTIFIER_CONFLICT'),
            (barcode.PageResult('1', 1, 1, vins=('VF1ABCDEFT5702376', 'VF1ABCDEFT5702377')),
             {}, 'IDENTIFIER_CONFLICT'),
        ]
        for page, db, status in cases:
            with self.subTest(status=status):
                self.assertEqual(classify.resolve_vis(page, db), (None, status))

    def test_database_streams_headers_duplicates_and_padded_badges(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'database.xlsx'
            wb = Workbook()
            sheet = wb.active
            sheet.append(['Année', 'BDG', 'VIS'])
            sheet.append([2026, 100000, 'T5708495'])
            sheet.append([2025, 100000, 'T5708495'])
            sheet.append([2024, 12, 'T5702376'])
            sheet['B4'].number_format = '00000'
            sheet.append([2025, '0012', 'T5702377'])
            sheet.append([2026, '0012', 'T5702378'])
            wb.save(path)
            wb.close()
            db = classify.load_database(path).badge_vis
            self.assertEqual(db['100000'], frozenset({'T5708495'}))
            self.assertEqual(db['00012'], frozenset({'T5702376'}))
            self.assertEqual(db['0012'], frozenset({'T5702377', 'T5702378'}))

    def test_six_digit_badges_are_opt_in_and_direct_vis_is_ignored(self):
        module = ModuleType('codarascan')
        class Symbol:
            def __init__(self, text):
                self.text = text
        module.DecodedSymbolResult = Symbol
        scanner = SimpleNamespace(scan_image=lambda *a, **k: SimpleNamespace(
            metadata=SimpleNamespace(mode=barcode.PANORAMA_MODE, engine=barcode.PANORAMA_ENGINE),
            symbols=[Symbol('100000'), Symbol('T5708495')]))
        with (patch.dict('sys.modules', {'codarascan': module}),
              patch.object(barcode, '_WORKER_SCANNER', scanner),
              patch.object(barcode, '_worker_document', return_value=object()),
              patch.object(barcode, 'render_upper_half', return_value=SimpleNamespace(close=lambda: None))):
            original = barcode.process_page(barcode.PageTask('1', 'page.pdf', 0, 1))
            extended = barcode.process_page(barcode.PageTask('1', 'page.pdf', 0, 1, True))
        self.assertEqual(original.badges, ())
        self.assertEqual(original.vins, ())
        self.assertEqual(extended.badges, ('100000',))
        self.assertEqual(extended.vins, ())
        self.assertEqual(classify.resolve_vis(extended, {}), (None, 'UNRESOLVED_BADGE'))

    def test_nof_barcode_does_not_conflict_with_vin_on_page_1_6(self):
        module = ModuleType('codarascan')
        class Symbol:
            def __init__(self, text):
                self.text = text
        module.DecodedSymbolResult = Symbol
        scanner = SimpleNamespace(scan_image=lambda *a, **k: SimpleNamespace(
            metadata=SimpleNamespace(mode=barcode.PANORAMA_MODE, engine=barcode.PANORAMA_ENGINE),
            symbols=[Symbol('BRYEKNFJXT5704852'), Symbol('6G3D1365'), Symbol('T5702376')]))
        with (patch.dict('sys.modules', {'codarascan': module}),
              patch.object(barcode, '_WORKER_SCANNER', scanner),
              patch.object(barcode, '_worker_document', return_value=object()),
              patch.object(barcode, 'render_upper_half', return_value=SimpleNamespace(close=lambda: None))):
            result = barcode.process_page(barcode.PageTask('1', 'page.pdf', 0, 1, True))
        self.assertEqual(result.vins, ('BRYEKNFJXT5704852',))
        self.assertEqual(classify.resolve_vis(result, {}), ('T5704852', 'BARCODE'))


class WorkflowTests(unittest.TestCase):
    def test_database_discovery_ignores_search_list_and_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('BDD_2024_2025_2026.xlsx', 'Dirty List VIN Delivered.xlsx',
                         '._BDD_2024_2025_2026.xlsx', '~$BDD_2024_2025_2026.xlsx',
                         'BDD_2024_2025_2026_found.xlsx'):
                (root / name).touch()
            self.assertEqual(classify.discover_database(root),
                             (root / 'BDD_2024_2025_2026.xlsx').resolve())
            self.assertIsNone(classify.parse_args(['-d', str(root)]).database)
            (root / 'bdd24-26.xlsx').touch()
            with self.assertRaisesRegex(barcode.ConfigurationError, 'Multiple BDD'):
                classify.discover_database(root)
            self.assertEqual(classify.discover_database(root, Path('bdd24-26.xlsx')),
                             (root / 'bdd24-26.xlsx').resolve())

    def test_missing_database_has_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(barcode.ConfigurationError, 'No BDD workbook'):
                classify.discover_database(Path(directory))

    def test_menu_command_allows_database_auto_discovery(self):
        command = main.build_command('classify_pages.py', Path('/batch'), workers=2)
        self.assertNotIn('--database', command)

    def test_end_to_end_classification_search_list_and_backup(self):
        from openpyxl import Workbook, load_workbook
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for lot, labels in [('479', ['badge only', 'unresolved', 'scan error']),
                                ('500', ['same VIS', 'conflict'])]:
                (root / lot).mkdir()
                make_pdf(root / lot / 'lot.pdf', labels)
            (root / 'notes').mkdir()
            (root / '501').mkdir()  # Must be reported, not silently ignored.
            (root / 'output').mkdir()
            (root / 'output' / 'old.txt').write_text('preserve')
            db_path = root / 'BDD.xlsx'
            target_path = root / 'search.xlsx'
            for path, rows in [(db_path, [['BDG', 'VIS', 'SEM', 'ID2', 'NOF'],
                                         ['100000', 'T5708495', 'SEQEMON0117700049', 'VF1ABCDEFT5708495', '4A2D0004']]),
                               (target_path, [['BDG', 'VIS'], ['12345', 'T5708495'],
                                              ['54321', 'T5702376']])]:
                wb = Workbook()
                for row in rows:
                    wb.active.append(row)
                wb.save(path)
                wb.close()
            originals = {p: p.read_bytes() for p in root.glob('*/*.pdf')}
            scanned = []
            def scan(task):
                self.assertEqual(task.page_index, 0)
                self.assertTrue(task.six_digit_badges)
                self.assertEqual(page_count(Path(task.pdf_path)), 1)
                name = Path(task.pdf_path).name
                scanned.append(name)
                values = {
                    '479_1.pdf': {'badges': ('100000',)},
                    '479_2.pdf': {},
                    '479_3.pdf': {'error': 'deliberate scan failure'},
                    '500_1.pdf': {'vins': ('VF1ABCDEFT5708495',)},
                    '500_2.pdf': {'badges': ('100000',), 'vins': ('VF1ABCDEFT5702376',)},
                }[name]
                return barcode.PageResult(task.lot, 1, task.pages_in_lot, **values)
            class Pool:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def imap_unordered(self, function, tasks, chunksize):
                    return (function(task) for task in reversed(tasks))
            args = classify.parse_args(['-d', str(root)])
            with (patch.object(barcode, 'validate_runtime'),
                  patch.object(barcode, 'process_page', side_effect=scan),
                  patch.object(barcode, '_close_worker_document'),
                  patch.object(classify.mp, 'get_context', return_value=SimpleNamespace(Pool=lambda *a, **k: Pool()))):
                code = classify.run(args)
            self.assertEqual(code, 1)
            output = root / 'output'
            self.assertEqual(page_text(output / 'T5708495/479_1.pdf'), 'badge only')
            self.assertEqual(page_text(output / 'T5708495/500_1.pdf'), 'same VIS')
            self.assertEqual(len(list((output / 'OCR').glob('*.pdf'))), 3)
            self.assertEqual(len(scanned), 5)
            with (output / 'results.csv').open(encoding='utf-8-sig', newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([(r['Lot'], r['Page']) for r in rows],
                             [('479', '1'), ('479', '2'), ('479', '3'), ('500', '1'), ('500', '2')])
            self.assertEqual(rows[0]['VIS'], 'T5708495')
            self.assertEqual(rows[0]['Decoded VIS'], '')
            self.assertEqual(rows[0]['Found'], 'FOUND')
            self.assertEqual(rows[2]['Status'], 'SCAN_ERROR')
            self.assertEqual(rows[-1]['Status'], 'IDENTIFIER_CONFLICT')
            result = load_workbook(output / 'search_found.xlsx')
            self.assertEqual(result.active['A2'].fill.fgColor.rgb, '00C6EFCE')
            result.close()
            pages = load_workbook(output / 'pages.xlsx')
            sheet = pages.active
            page_rows = list(sheet.values)
            self.assertEqual(page_rows[0], ('lot_page', 'BDG', 'VIN', 'VIS', 'SEQ', 'SEQ_9', 'NOF'))
            self.assertEqual(page_rows[1], ('479_1', '100000', 'VF1ABCDEFT5708495', 'T5708495',
                                           'SEQEMON0117700049', '117700049', '4A2D0004'))
            self.assertEqual(page_rows[4][1:], page_rows[1][1:])
            for index in (2, 3, 5):
                self.assertEqual(page_rows[index][1:], (None,) * 6)
            self.assertEqual(len(page_rows), 6)
            self.assertEqual(sheet.freeze_panes, 'A2')
            self.assertEqual(sheet.auto_filter.ref, 'A1:G6')
            self.assertEqual(sheet['F2'].number_format, '@')
            pages.close()
            self.assertIn('Lot 501: no PDF found', (output / 'report.txt').read_text())
            self.assertEqual(len(list(root.glob('output_backup_*/old.txt'))), 1)
            self.assertFalse(list(root.glob('.classify_pages_*')))
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)

    def test_menu_builds_new_command_with_separate_database(self):
        command = main.build_command('classify_pages.py', Path('/batch'), workers=3,
                                     excel=Path('/batch/list.xlsx'), database=Path('/BDD.xlsx'))
        self.assertEqual(command[-6:], ['-n', '3', '--excel', '/batch/list.xlsx', '--database', '/BDD.xlsx'])

    def test_fatal_split_failure_preserves_previous_output(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '1').mkdir()
            (root / '1/broken.pdf').write_bytes(b'not a PDF')
            (root / 'output').mkdir()
            (root / 'output/old.txt').write_text('keep')
            for name in ('BDD.xlsx', 'search.xlsx'):
                wb = Workbook()
                wb.active.append(['BDG', 'VIS'])
                wb.active.append(['12345', 'T5702376'])
                wb.save(root / name)
                wb.close()
            class Pool:
                def __enter__(self): return self
                def __exit__(self, *args): pass
            args = classify.parse_args(['-d', str(root), '--database', 'BDD.xlsx'])
            with (patch.object(barcode, 'validate_runtime'),
                  patch.object(classify.mp, 'get_context', return_value=SimpleNamespace(Pool=lambda *a, **k: Pool()))):
                with self.assertRaises(barcode.ConfigurationError):
                    classify.run(args)
            self.assertEqual((root / 'output/old.txt').read_text(), 'keep')
            self.assertFalse(list(root.glob('output_backup_*')))
            self.assertFalse(list(root.glob('.classify_pages_*')))


if __name__ == '__main__':
    unittest.main()

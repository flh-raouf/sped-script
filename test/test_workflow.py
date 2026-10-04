from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_ocr_pages import VIN, VIS, line
from test_review_pages import parts_table
from test_split_pages import make_pdf

from scripts import classify_pages as classify
from scripts import ocr_pages as ocr
from scripts import review_pages as review


class ParentWorkflowTests(unittest.TestCase):
    def test_full_sequence_uses_same_parent_shared_cache_and_preserves_originals(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'1').mkdir()
            original = root/'1/lot.pdf'
            make_pdf(original, ['barcode front', 'OCR front', 'back', 'table', 'unknown'])
            original_bytes = original.read_bytes()
            workbook = Workbook()
            workbook.active.append(['Année', 'BDG', 'VIS', 'SEM', 'ID2', 'NOF'])
            workbook.active.append([2026, '91034', VIS, 'SEQEMON0118300161', VIN, '6G3D1411'])
            workbook.save(root/'BDD.xlsx')
            workbook.close()
            workbook = Workbook()
            workbook.active.append(['BDG', 'VIS'])
            workbook.active.append(['91034', VIS])
            workbook.save(root/'search.xlsx')
            workbook.close()

            class Pool:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def imap_unordered(self, function, tasks, chunksize):
                    return (function(task) for task in tasks)

            def scan(task):
                return ocr.barcode.PageResult(task.lot, task.page_index+1, task.pages_in_lot,
                                              badges=('91034',) if Path(task.pdf_path).stem == '1_1' else ())

            with patch.object(ocr.barcode, 'validate_runtime'), \
                 patch.object(ocr.barcode, 'process_page', side_effect=scan), \
                 patch.object(ocr.barcode, '_close_worker_document'), \
                 patch.object(classify.mp, 'get_context', return_value=SimpleNamespace(Pool=lambda *a, **k: Pool())):
                self.assertEqual(classify.run(classify.parse_args(['-d', str(root)])), 0)
            self.assertTrue((root/'Output'/VIS/'1_1.pdf').exists())
            self.assertEqual(len(list((root/'OCR').glob('*.pdf'))), 4)
            self.assertFalse((root/'Output/OCR').exists())
            self.assertTrue(next((root/'Reports/barcode').glob('*/lot_reports/1/pages.xlsx')).exists())

            def read(path):
                return {'1_2': [line('BDG: 91034')], '1_3': [],
                        '1_4': parts_table(), '1_5': [line('CHECK-LIST')]}[path.stem]
            with patch.object(ocr, 'PaddleEngine') as engine:
                engine.return_value.read.side_effect = read
                self.assertEqual(ocr.run(ocr.parse_args(['-d', str(root)])), 0)
                self.assertEqual(engine.return_value.read.call_count, 4)
            self.assertEqual(len(list((root/'OCR').glob('*.pdf'))), 0)
            self.assertEqual(len(list((root/'Pending').glob('*.pdf'))), 3)
            self.assertTrue((root/'Reports/.ocr_cache.sqlite3').exists())
            self.assertFalse((root/'Review').exists())

            with patch.object(ocr, 'PaddleEngine', side_effect=AssertionError('Use shared OCR cache')), \
                 patch.object(review, 'ink_percent', side_effect=lambda path, *_: .1 if path.stem == '1_3' else 5):
                self.assertEqual(review.run(review.parse_args(['-d', str(root), '--cache-only'])), 0)
            self.assertEqual(len(list((root/'Pending').glob('*.pdf'))), 0)
            self.assertEqual(len(list((root/'Output'/VIS).glob('*.pdf'))), 4)
            self.assertTrue((root/'Review/1_5.pdf').exists())
            self.assertTrue((root/'Reports/review_assignments.jsonl').exists())
            results = next((root/'Reports/review').glob('*/results.csv'))
            with results.open(encoding='utf-8-sig', newline='') as handle:
                rows = {row['lot_page']: row for row in csv.DictReader(handle)}
            self.assertEqual(rows['1_3']['BDG'], '91034')
            self.assertEqual(rows['1_3']['Predecessor'], '1_2')
            self.assertEqual(Path(rows['1_5']['PDF']), (root/'Review/1_5.pdf').resolve())
            self.assertEqual(original.read_bytes(), original_bytes)
            # Empty stages are safe no-ops and don't initialize OCR again.
            self.assertEqual(ocr.run(ocr.parse_args(['-d', str(root)])), 0)
            self.assertEqual(review.run(review.parse_args(['-d', str(root)])), 0)
            # Re-running barcode classification cannot clear or overwrite results.
            saved = {path.name: path.read_bytes() for path in (root/'Output'/VIS).glob('*.pdf')}
            with patch.object(ocr.barcode, 'validate_runtime'), \
                 patch.object(ocr.barcode, 'process_page', side_effect=scan), \
                 patch.object(ocr.barcode, '_close_worker_document'), \
                 patch.object(classify.mp, 'get_context', return_value=SimpleNamespace(Pool=lambda *a, **k: Pool())), \
                 self.assertRaisesRegex(ocr.barcode.ConfigurationError, 'No lots published'):
                classify.run(classify.parse_args(['-d', str(root)]))
            self.assertEqual(saved, {path.name: path.read_bytes() for path in (root/'Output'/VIS).glob('*.pdf')})
            self.assertFalse(list((root/'OCR').glob('*.pdf')))

    def test_missing_stage_folder_explains_previous_step_without_creating_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'Run barcode classification first'):
                ocr.run(ocr.parse_args(['-d', str(root)]))
            with self.assertRaisesRegex(ValueError, 'Run OCR classification first'):
                review.run(review.parse_args(['-d', str(root)]))
            self.assertFalse((root/'Output').exists())

    def test_cache_miss_in_last_stage_moves_to_final_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'Pending').mkdir()
            make_pdf(root/'Pending/1_1.pdf', ['unknown'])
            from test_ocr_pages import database
            with patch.object(ocr, 'discover_bdd', return_value=root/'BDD.xlsx'), \
                 patch.object(classify, 'load_database', return_value=database()), \
                 patch.object(ocr, 'PaddleEngine', side_effect=AssertionError('No OCR')):
                self.assertEqual(review.run(review.parse_args(['-d', str(root), '--cache-only'])), 0)
            self.assertTrue((root/'Review/1_1.pdf').exists())
            self.assertFalse((root/'Pending/1_1.pdf').exists())

    def test_empty_stages_still_create_the_next_folder_without_requiring_bdd_or_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'OCR').mkdir()
            self.assertEqual(ocr.run(ocr.parse_args(['-d', str(root)])), 0)
            self.assertTrue((root/'Pending').is_dir())
            self.assertEqual(review.run(review.parse_args(['-d', str(root)])), 0)
            self.assertTrue((root/'Review').is_dir())


if __name__ == '__main__':
    unittest.main()

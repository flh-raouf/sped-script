from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_ocr_pages import VIN, VIS, database
from test_split_pages import make_pdf

from scripts import classify_saved_reports as saved


class SavedReportTests(unittest.TestCase):
    def setup_batch(self, root, rows):
        run = root/'runs/run-1'
        (run/'1').mkdir(parents=True)
        make_pdf(run/'1/lot.pdf', [str(i) for i in range(len(rows))])
        with (run/'results.csv').open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['Lot', 'numero page', 'bdg', 'VIN', 'VIS', 'found'])
            writer.writerows(rows)
        return root/'runs'

    def test_saved_multiple_badges_are_split_and_conflicting_pages_remain_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.setup_batch(Path(directory), [
                ['1', 1, '91034', '', 'IGNORED', ''],
                ['1', 2, '91034;91069', VIN, '', ''],
                ['1', 3, '', '', '', '']])
            lots = saved.prepare(source, database())
            decisions = lots[0][3]
            self.assertEqual(decisions[0][1:], (VIS, 'BDD_BADGE'))
            self.assertEqual(decisions[1][0].badges, ('91034', '91069'))
            self.assertEqual(decisions[1][1:], (None, 'IDENTIFIER_CONFLICT'))
            self.assertEqual(decisions[2][1:], (None, 'NO_BARCODE'))

    def test_missing_report_page_is_rejected_before_outputs_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.setup_batch(Path(directory), [['1', 2, '', '', '', '']])
            with self.assertRaisesRegex(ValueError, 'exactly once'):
                saved.prepare(source, database())

    def test_original_pdf_preserved_and_destination_collision_refused(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.setup_batch(root, [['1', 1, '91034', VIN, '', ''], ['1', 2, '', '', '', '']])
            original = source/'run-1/1/lot.pdf'
            before = original.read_bytes()
            workbook = Workbook()
            workbook.active.append(['BDG', 'VIS', 'ID2'])
            workbook.active.append(['91034', VIS, VIN])
            workbook.save(root/'BDD.xlsx')
            workbook.close()
            args = SimpleNamespace(directory=source, output=root/'result', database=root/'BDD.xlsx', excel=None)
            self.assertEqual(saved.run(args), 0)
            self.assertTrue((root/'result/Output'/VIS/'1_1.pdf').exists())
            self.assertTrue((root/'result/OCR/1_2.pdf').exists())
            self.assertEqual(before, original.read_bytes())
            with self.assertRaises(FileExistsError):
                saved.run(args)


if __name__ == '__main__':
    unittest.main()

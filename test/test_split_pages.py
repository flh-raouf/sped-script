from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import split_pages


def make_pdf(path: Path, labels: list[str]) -> None:
    from reportlab.pdfgen.canvas import Canvas

    canvas = Canvas(str(path), pagesize=(300, 300))
    for label in labels:
        canvas.drawString(40, 150, label)
        canvas.showPage()
    canvas.save()


def page_count(path: Path) -> int:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    try:
        return len(reader.pages)
    finally:
        split_pages.close_pdf_reader(reader)


def page_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    try:
        return (reader.pages[0].extract_text() or "").strip()
    finally:
        split_pages.close_pdf_reader(reader)


class SplitPagesTests(unittest.TestCase):
    def test_numeric_lots_are_sorted_and_other_directories_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("10", "2", "1", "split_pages", "notes"):
                (root / name).mkdir()

            self.assertEqual(
                [path.name for path in split_pages.discover_lot_directories(root)],
                ["1", "2", "10"],
            )

    def test_split_copies_one_page_files_in_numeric_lot_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lot_one = root / "1"
            lot_ten = root / "10"
            lot_one.mkdir()
            lot_ten.mkdir()
            source_one = lot_one / "scan-with-any-name.pdf"
            source_ten = lot_ten / "another-name.pdf"
            make_pdf(source_one, ["lot-1-page-1", "lot-1-page-2"])
            make_pdf(source_ten, ["lot-10-page-1"])
            source_bytes = source_one.read_bytes()

            old_output = root / "split_pages"
            old_output.mkdir()
            (old_output / "stale.txt").write_text("keep in backup", encoding="utf-8")

            exit_code, output, report = split_pages.run_split(root)

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                sorted(path.name for path in output.glob("*.pdf")),
                ["10_1.pdf", "1_1.pdf", "1_2.pdf"],
            )
            self.assertFalse((output / "1_0.pdf").exists())
            self.assertEqual(page_count(output / "1_1.pdf"), 1)
            self.assertEqual(page_count(output / "1_2.pdf"), 1)
            self.assertEqual(page_count(output / "10_1.pdf"), 1)
            self.assertEqual(page_text(output / "1_1.pdf"), "lot-1-page-1")
            self.assertEqual(page_text(output / "1_2.pdf"), "lot-1-page-2")
            self.assertEqual(page_text(output / "10_1.pdf"), "lot-10-page-1")
            self.assertEqual(source_one.read_bytes(), source_bytes)
            self.assertTrue(report.exists())
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("Lots discovered: 2", report_text)
            self.assertIn("Total source pages: 3", report_text)
            self.assertIn("Single-page PDFs successfully created: 3", report_text)
            backups = list(root.glob("split_pages_backup_*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "stale.txt").exists())

    def test_bad_lots_are_reported_and_valid_lots_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "1"
            missing = root / "2"
            multiple = root / "3"
            corrupt = root / "4"
            good.mkdir()
            missing.mkdir()
            multiple.mkdir()
            corrupt.mkdir()
            make_pdf(good / "good.pdf", ["good"])
            make_pdf(multiple / "first.pdf", ["first"])
            make_pdf(multiple / "second.pdf", ["second"])
            (corrupt / "broken.pdf").write_bytes(b"not a PDF")

            exit_code, output, report = split_pages.run_split(root)

            self.assertEqual(exit_code, 1)
            self.assertEqual(page_count(output / "1_1.pdf"), 1)
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("Lot 2: no PDF found", report_text)
            self.assertIn("Lot 3: multiple PDFs found", report_text)
            self.assertIn("Lot 4: cannot open broken.pdf", report_text)


if __name__ == "__main__":
    unittest.main()

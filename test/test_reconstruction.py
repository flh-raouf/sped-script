from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts.reconstruction_core import (
    DocumentId,
    Strategy,
    analyze_results_csv,
    build_local_ranges,
    detected_identifiers_by_lot,
    discover_lot_pdf,
    natural_sort_key,
    remove_appledouble_sidecars,
    run_reconstruction,
)

HEADERS = ["Lot", "numero page", "bdg", "VIN", "VIS", "found"]
A = DocumentId("12345", "AAA12345")
B = DocumentId("23456", "BBB23456")
C = DocumentId("34567", "CCC34567")


def write_csv(path: Path, rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        writer.writerows(rows)


def make_pdf(path: Path, lot: str, pages: int) -> None:
    from reportlab.pdfgen.canvas import Canvas

    canvas = Canvas(str(path), pagesize=(300, 300))
    for page_number in range(1, pages + 1):
        canvas.drawString(40, 150, f"{lot}-{page_number}")
        canvas.showPage()
    canvas.save()


def page_texts(path: Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return [(page.extract_text() or "").strip() for page in reader.pages]


def edge_case_rows() -> list[list[object]]:
    vin_a = "VF1ABCDEFAAA12345"
    vin_b = "VF1ABCDEFBBB23456"
    vin_c = "VF1ABCDEFCCC34567"
    return [
        ["2", 1, A.badge, vin_a, A.vis, "FOUND"],
        ["2", 2, A.badge, "", "", "Found"],
        ["2", 3, B.badge, vin_b, B.vis, "FOUND"],
        ["2", 4, "", vin_b, B.vis, "found"],
        ["2", 5, "", vin_a, A.vis, "FOUND"],
        ["2", 5, A.badge, "", "", "FOUND"],  # duplicate A physical page
        ["2", 6, C.badge, vin_c, C.vis, "FOUND"],
        ["2", 7, A.badge, vin_a, A.vis, "FOUND"],
        ["2", 8, A.badge, "", "", "FOUND"],
        ["10", 1, "", vin_a, A.vis, "FOUND"],
        ["10", 3, A.badge, "", "", "FOUND"],
    ]


class CsvAnalysisTests(unittest.TestCase):
    def test_short_badge_remains_available_to_reconstruction(self) -> None:
        short = DocumentId("10", "AAA12345")
        rows = [["1", 1, short.badge, "VF1ABCDEFAAA12345", short.vis, "FOUND"]]
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "results.csv"
            write_csv(csv_path, rows)
            analysis = analyze_results_csv(csv_path)

        self.assertEqual(analysis.target_documents, {short})
        self.assertEqual(analysis.explicit_pages[short], {("1", 1)})

    def test_local_ranges_match_repeated_90926_occurrences(self) -> None:
        target = DocumentId("90926", "S5773404")
        other = DocumentId("91076", "S5780513")
        rows = [
            ["1", 56, target.badge, "", "", ""],
            ["1", 57, "", "BRYEKNFJ0S5773404", target.vis, ""],
            ["1", 58, target.badge, "BRYEKNFJ0S5773404", target.vis, ""],
            ["1", 61, "90969", "", "", ""],
            ["1", 128, target.badge, "BRYEKNFJ0S5773404", target.vis, ""],
            ["1", 133, target.badge, "", "", ""],
            ["1", 136, "", "BRYEKNFJ0S5773404", target.vis, ""],
            ["1", 145, target.badge, "", "", ""],
            ["1", 146, other.badge, "BRYEKNFJ7S5780513", other.vis, "FOUND"],
            ["1", 430, target.badge, "BRYEKNFJ0S5773404", target.vis, ""],
            ["1", 437, target.badge, "BRYEKNFJ0S5773404", target.vis, ""],
        ]
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "results.csv"
            write_csv(csv_path, rows)
            analysis = analyze_results_csv(csv_path)

        detections = detected_identifiers_by_lot(analysis.records)["1"]
        explicit = {
            record.page_number
            for record, document in analysis.resolved_records
            if document == target and record.page_number is not None
        }
        ranges = build_local_ranges(target, explicit, detections)
        selected = [
            page
            for start, end in ranges
            for page in range(start, end + 1)
        ]

        self.assertEqual(ranges, [(56, 58), (128, 145), (430, 437)])
        self.assertEqual(
            selected,
            [*range(56, 59), *range(128, 146), *range(430, 438)],
        )

    def test_mapping_inference_duplicates_and_natural_sort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "results.csv"
            write_csv(csv_path, edge_case_rows())
            analysis = analyze_results_csv(csv_path)

        self.assertEqual(analysis.target_documents, {A, B, C})
        self.assertEqual(analysis.duplicate_page_references_removed, 1)
        self.assertIn(("2", 2), analysis.explicit_pages[A])  # Badge-only
        self.assertIn(("10", 1), analysis.explicit_pages[A])  # VIS-only
        self.assertEqual(
            sorted(["10", "2", "1", "A"], key=natural_sort_key),
            ["1", "2", "10", "A"],
        )

    def test_conflicts_and_unresolved_found_rows_are_quarantined(self) -> None:
        rows = [
            ["1", 1, "12345", "VF1ABCDEFAAA12345", "AAA12345", "FOUND"],
            ["1", 2, "12345", "VF1ABCDEFBBB12345", "BBB12345", "FOUND"],
            ["1", 3, "99999", "", "", "FOUND"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "results.csv"
            write_csv(csv_path, rows)
            analysis = analyze_results_csv(csv_path)

        self.assertFalse(analysis.target_documents)
        self.assertTrue(
            any("maps to multiple VIS" in item for item in analysis.conflicts)
        )
        self.assertEqual(len(analysis.unresolved), 3)

    def test_same_vis_with_vin_prefix_variation_is_not_identity_conflict(self) -> None:
        rows = [
            ["47", 92, "39260", "BRYED9HP4S5733815", "S5733815", "FOUND"],
            ["47", 95, "39260", "BRYED9RP4S5733815", "S5733815", "FOUND"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "results.csv"
            write_csv(csv_path, rows)
            analysis = analyze_results_csv(csv_path)

        document = DocumentId("39260", "S5733815")
        self.assertEqual(analysis.target_documents, {document})
        self.assertEqual(
            analysis.explicit_pages[document], {("47", 92), ("47", 95)}
        )
        self.assertEqual(analysis.conflicts, [])

    def test_identified_mode_isolates_unmapped_single_identifiers(self) -> None:
        rows = [
            ["4", 12, "91286", "", "", "FOUND"],
            ["4", 13, "", "", "ABC12345", "FOUND"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "results.csv"
            write_csv(csv_path, rows)
            analysis = analyze_results_csv(
                csv_path, allow_isolated_partial_identifiers=True
            )

        badge_only = DocumentId("91286", "")
        vis_only = DocumentId("", "ABC12345")
        self.assertEqual(analysis.target_documents, {badge_only, vis_only})
        self.assertEqual(analysis.explicit_pages[badge_only], {("4", 12)})
        self.assertEqual(analysis.explicit_pages[vis_only], {("4", 13)})
        self.assertEqual(analysis.unresolved, [])
        self.assertEqual(len(analysis.partial_identifiers), 2)

    def test_pdf_discovery_ignores_macos_appledouble_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lot_directory = Path(directory) / "1"
            lot_directory.mkdir()
            source = lot_directory / "document.pdf"
            sidecar = lot_directory / "._document.pdf"
            source.write_bytes(b"source")
            sidecar.write_bytes(b"metadata")

            discovered, error = discover_lot_pdf(Path(directory), "1")

            self.assertEqual(discovered, source)
            self.assertIsNone(error)

    def test_generated_appledouble_sidecars_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            real_pdf = output / "12345_ABC12345.pdf"
            sidecar = output / "._12345_ABC12345.pdf"
            real_pdf.write_bytes(b"pdf")
            sidecar.write_bytes(b"metadata")

            remove_appledouble_sidecars(output)

            self.assertTrue(real_pdf.exists())
            self.assertFalse(sidecar.exists())


class ReconstructionIntegrationTests(unittest.TestCase):
    def make_root(self, directory: str) -> Path:
        root = Path(directory)
        for lot, count in (("2", 8), ("10", 3)):
            lot_directory = root / lot
            lot_directory.mkdir()
            make_pdf(lot_directory / "document.pdf", lot, count)
        write_csv(root / "results.csv", edge_case_rows())
        return root

    def test_range_strategy_includes_intermediate_pages_and_cross_lot_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            exit_code, output = run_reconstruction(root, Strategy.RANGES)
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                page_texts(output / A.filename),
                ["2-1", "2-2", "2-5", "2-7", "2-8", "10-1", "10-2", "10-3"],
            )
            self.assertEqual(page_texts(output / B.filename), ["2-3", "2-4"])
            self.assertEqual(page_texts(output / C.filename), ["2-6"])
            report = (output / "reconstruction_report.txt").read_text()
            self.assertIn(
                "Intermediate/blank pages added by range reconstruction: 1", report
            )
            self.assertIn("Local document ranges reconstructed: 6", report)
            self.assertIn("Identifiers spanning multiple Lots: 1", report)

    def test_identified_strategy_excludes_intermediate_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            exit_code, output = run_reconstruction(root, Strategy.IDENTIFIED)
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                page_texts(output / A.filename),
                ["2-1", "2-2", "2-5", "2-7", "2-8", "10-1", "10-3"],
            )
            self.assertEqual(page_texts(output / B.filename), ["2-3", "2-4"])
            self.assertEqual(page_texts(output / C.filename), ["2-6"])
            report = (output / "reconstruction_report.txt").read_text()
            self.assertNotIn("Intermediate/blank pages added", report)
            self.assertIn("Explicitly identified pages included: 10", report)

    def test_identified_strategy_writes_badge_only_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lot_directory = root / "4"
            lot_directory.mkdir()
            make_pdf(lot_directory / "document.pdf", "4", 12)
            write_csv(root / "results.csv", [["4", 12, "91286", "", "", "FOUND"]])

            exit_code, output = run_reconstruction(root, Strategy.IDENTIFIED)

            self.assertEqual(exit_code, 0)
            self.assertEqual(page_texts(output / "91286_.pdf"), ["4-12"])
            report = (output / "reconstruction_report.txt").read_text()
            self.assertIn("Isolated partial identifiers: 1", report)
            self.assertIn("Unresolved Badge/VIS mappings: 0", report)

    def test_range_strategy_writes_single_page_badge_only_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lot_directory = root / "4"
            lot_directory.mkdir()
            make_pdf(lot_directory / "document.pdf", "4", 12)
            write_csv(root / "results.csv", [["4", 12, "91286", "", "", "FOUND"]])

            exit_code, output = run_reconstruction(root, Strategy.RANGES)

            self.assertEqual(exit_code, 0)
            self.assertEqual(page_texts(output / "91286_.pdf"), ["4-12"])
            report = (output / "reconstruction_report.txt").read_text()
            self.assertIn("Isolated partial identifiers: 1", report)
            self.assertIn("Unresolved Badge/VIS mappings: 0", report)
            self.assertIn(
                "Intermediate/blank pages added by range reconstruction: 0", report
            )

    def test_missing_lot_and_out_of_bounds_page_do_not_block_other_documents(
        self,
    ) -> None:
        rows = [
            ["99", 1, A.badge, "VF1ABCDEFAAA12345", A.vis, "FOUND"],
            ["2", 99, B.badge, "VF1ABCDEFBBB23456", B.vis, "FOUND"],
            ["2", 1, C.badge, "VF1ABCDEFCCC34567", C.vis, "FOUND"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lot_directory = root / "2"
            lot_directory.mkdir()
            make_pdf(lot_directory / "document.pdf", "2", 1)
            write_csv(root / "results.csv", rows)

            exit_code, output = run_reconstruction(root, Strategy.IDENTIFIED)
            self.assertEqual(exit_code, 1)
            self.assertFalse((output / A.filename).exists())
            self.assertFalse((output / B.filename).exists())
            self.assertEqual(page_texts(output / C.filename), ["2-1"])
            report = (output / "reconstruction_report.txt").read_text()
            self.assertIn("Missing Lot directories: 1", report)
            self.assertIn("Pages outside PDF bounds: 1", report)

    def test_non_target_mapping_conflict_does_not_fail_successful_run(self) -> None:
        rows = [
            ["2", 1, A.badge, "VF1ABCDEFAAA12345", A.vis, "FOUND"],
            ["2", 2, "39235", "BRYEKNFJ2S5723102", "S5723102", ""],
            ["2", 3, "39235", "BRYEKNFJ2S5723152", "S5723152", ""],
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lot_directory = root / "2"
            lot_directory.mkdir()
            make_pdf(lot_directory / "document.pdf", "2", 3)
            write_csv(root / "results.csv", rows)

            exit_code, output = run_reconstruction(root, Strategy.IDENTIFIED)

            self.assertEqual(exit_code, 0)
            self.assertEqual(page_texts(output / A.filename), ["2-1"])
            report = (output / "reconstruction_report.txt").read_text()
            self.assertIn("Mapping conflicts observed in all CSV rows: 1", report)
            self.assertIn("Conflicts affecting FOUND targets: 0", report)
            self.assertIn("Non-target conflicts quarantined: 1", report)


if __name__ == "__main__":
    unittest.main()

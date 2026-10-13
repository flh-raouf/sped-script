from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from test_split_pages import make_pdf

from scripts import pipeline
from scripts.lot_catalog import Status
from scripts.pipeline import (ARCHIVE, BARCODE_SCRIPT, OCR_SCRIPT, REVIEW_SCRIPT, Config,
                              PipelineError, Registry)


def write_lot(base: Path, name: str, content: bytes = b"%PDF fake", pdf_name: str = "scan.pdf") -> Path:
    (base / name).mkdir(parents=True, exist_ok=True)
    pdf = base / name / pdf_name
    pdf.write_bytes(content)
    return pdf


def write_workbook(path: Path) -> None:
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.append(["BDG", "VIS"])
    workbook.save(path)
    workbook.close()


def add_pdfs(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"%PDF")


def barcode_report(root: Path, rows: list[tuple[str, bool]], stamp: str = "20260101_000000_000000") -> None:
    directory = root / "Reports" / "barcode" / stamp
    directory.mkdir(parents=True)
    with (directory / "lots.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Lot", "Status", "Published"])
        writer.writerows((lot, "SAVED" if published else "FAILED", published) for lot, published in rows)


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.source = base / "source"
        self.root = base / "root"
        self.source.mkdir()
        self.root.mkdir()
        self.config = Config(source=self.source, root=self.root, workers=2, stable_seconds=0)
        self.registry = Registry(pipeline.registry_path(self.config))

    def plan(self, lots=None, **kwargs):
        """Plan for ``lots``, or for every new lot of the source folder."""
        kwargs.setdefault("sleep", lambda seconds: None)
        kwargs.setdefault("pages_of", lambda pdf: 10)
        if lots is None:
            lots = pipeline.new_lots(pipeline.build_catalog(self.config, self.registry))
        return pipeline.build_plan(self.config, self.registry, lots, **kwargs)


class ConfigTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_required_and_optional_values(self):
        config = pipeline.load_config(self.write(
            '[dossiers]\nsource = "/a"\nracine = "/b"\n[traitement]\nprocessus = 8\nliste = "dirty.xlsx"\n'
            '[estimation]\nocr = 2\n'))
        self.assertEqual((config.source, config.root, config.workers), (Path("/a"), Path("/b"), 8))
        self.assertEqual((config.search_list, config.database, config.ocr_seconds_per_page), ("dirty.xlsx", None, 2.0))
        self.assertEqual(config.already_done, ())
        self.assertEqual(config.ocr_workers, 1)
        self.assertEqual(config.review_workers, 1)

    def test_ocr_worker_count_is_independent_and_positive(self):
        base = '[dossiers]\nsource = "/a"\nracine = "/b"\n[traitement]\nprocessus = 6\n'
        config = pipeline.load_config(self.write(base + 'processus_ocr = 3\n'))
        self.assertEqual((config.workers, config.ocr_workers), (6, 3))
        for value in ('0', '-1', 'true', '2.5', '"3"'):
            with self.subTest(value=value), self.assertRaisesRegex(PipelineError, 'processus_ocr'):
                pipeline.load_config(self.write(base + f'processus_ocr = {value}\n'))

    def test_final_stage_worker_count_is_independent_and_positive(self):
        base = '[dossiers]\nsource = "/a"\nracine = "/b"\n[traitement]\nprocessus = 6\nprocessus_ocr = 3\n'
        config = pipeline.load_config(self.write(base + 'processus_classification = 2\n'))
        self.assertEqual((config.workers, config.ocr_workers, config.review_workers), (6, 3, 2))
        for value in ('0', '-1', 'true', '2.5', '"3"'):
            with self.subTest(value=value), self.assertRaisesRegex(PipelineError, 'processus_classification'):
                pipeline.load_config(self.write(base + f'processus_classification = {value}\n'))

    def test_lots_processed_before_the_registry_are_declared_as_ranges(self):
        config = pipeline.load_config(self.write(
            '[dossiers]\nsource = "/a"\nracine = "/b"\n[traitement]\ndeja_traites = "1-82, 200-472"\n'))
        self.assertEqual(config.already_done, ((1, 82), (200, 472)))
        for bad in ('"1-"', "12"):
            with self.assertRaisesRegex(PipelineError, "deja_traites"):
                pipeline.load_config(self.write(
                    f'[dossiers]\nsource = "/a"\nracine = "/b"\n[traitement]\ndeja_traites = {bad}\n'))

    def test_missing_file_explains_how_to_create_it(self):
        with self.assertRaisesRegex(PipelineError, "config.example.toml"):
            pipeline.load_config(Path("/nonexistent/config.toml"))

    def test_rejects_missing_folder_and_bad_workers(self):
        with self.assertRaisesRegex(PipelineError, "racine"):
            pipeline.load_config(self.write('[dossiers]\nsource = "/a"\n'))
        with self.assertRaisesRegex(PipelineError, "processus"):
            pipeline.load_config(self.write('[dossiers]\nsource = "/a"\nracine = "/b"\n[traitement]\nprocessus = 0\n'))
        with self.assertRaisesRegex(PipelineError, "illisible"):
            pipeline.load_config(self.write("[dossiers"))


class RegistryTests(PipelineTestCase):
    def test_round_trip_and_scanned_marker(self):
        self.registry.record_import("480", "scan.pdf", 12)
        self.registry.mark_scanned("480")
        reloaded = Registry(pipeline.registry_path(self.config))
        self.assertEqual(reloaded.lots["480"]["size"], 12)
        self.assertIsNotNone(reloaded.lots["480"]["scanned_at"])

    def test_corrupt_registry_is_refused_not_reset(self):
        path = pipeline.registry_path(self.config)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "Registre illisible"):
            Registry(path)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


class PreflightTests(PipelineTestCase):
    def test_returns_database_and_single_search_list(self):
        write_workbook(self.root / "BDD_2024.xlsx")
        write_workbook(self.root / "dirty list.xlsx")
        write_workbook(self.root / "~$lock.xlsx")
        database, search_list = pipeline.preflight(self.config)
        self.assertEqual((database.name, search_list.name), ("BDD_2024.xlsx", "dirty list.xlsx"))

    def test_ambiguous_search_list_asks_for_explicit_name(self):
        write_workbook(self.root / "BDD.xlsx")
        write_workbook(self.root / "a.xlsx")
        write_workbook(self.root / "b.xlsx")
        with self.assertRaisesRegex(PipelineError, "liste"):
            pipeline.preflight(self.config)
        explicit = Config(source=self.source, root=self.root, search_list="b.xlsx")
        self.assertEqual(pipeline.preflight(explicit)[1].name, "b.xlsx")

    def test_missing_database_missing_folder_and_nested_folders(self):
        with self.assertRaisesRegex(PipelineError, "BDD"):
            pipeline.preflight(self.config)
        with self.assertRaisesRegex(PipelineError, "introuvable"):
            pipeline.preflight(Config(source=self.source / "absent", root=self.root))
        with self.assertRaisesRegex(PipelineError, "distincts"):
            pipeline.preflight(Config(source=self.root, root=self.root))

    def test_disk_check_blocks_when_space_is_insufficient(self):
        plan = pipeline.Plan(incoming=[pipeline.LotInfo("1", Path("x"), 1 << 60, 1)])
        with self.assertRaisesRegex(PipelineError, "Espace disque"):
            pipeline.check_disk_space(self.config, plan)
        pipeline.check_disk_space(self.config, pipeline.Plan())

    def test_second_run_on_same_root_is_refused(self):
        with pipeline.run_lock(self.root):
            with self.assertRaisesRegex(PipelineError, "déjà en cours"):
                with pipeline.run_lock(self.root):
                    pass
        with pipeline.run_lock(self.root):
            pass


class CatalogTests(PipelineTestCase):
    def statuses(self):
        return {lot.name: lot.status for lot in pipeline.build_catalog(self.config, self.registry)}

    def test_lists_every_source_lot_highest_first_with_its_status(self):
        for name in ("9", "10", "11", "12", "13", "14", "notes"):
            (self.source / name).mkdir()
        self.registry.record_import("9", "scan.pdf", 1)
        self.registry.mark_scanned("9")
        (self.root / ARCHIVE / "10").mkdir(parents=True)
        (self.root / "11").mkdir()
        self.registry.record_import("12", "scan.pdf", 1)       # copied, then the root lost it
        catalog = pipeline.build_catalog(self.config, self.registry)
        self.assertEqual([lot.name for lot in catalog], ["14", "13", "12", "11", "10", "9"])
        new, progress, done = Status.NEW, Status.IN_PROGRESS, Status.DONE
        self.assertEqual([lot.status for lot in catalog], [new, new, new, progress, done, done])

    def test_lots_published_by_an_earlier_barcode_run_are_done(self):
        for name in ("5", "6", "7"):
            (self.source / name).mkdir()
        barcode_report(self.root, [("5", True), ("6", False)], stamp="20250101_000000_000000")
        barcode_report(self.root, [("9", True)], stamp="20250102_000000_000000")
        self.assertEqual(self.statuses(), {"7": Status.NEW, "6": Status.NEW, "5": Status.DONE})

    def test_lots_declared_in_the_config_are_done(self):
        for name in ("82", "83", "300"):
            (self.source / name).mkdir()
        self.config = Config(source=self.source, root=self.root, already_done=((1, 82), (200, 472)))
        self.assertEqual(self.statuses(), {"300": Status.DONE, "83": Status.NEW, "82": Status.DONE})

    def test_does_not_open_any_pdf(self):
        write_lot(self.source, "1", b"not even a pdf")
        self.assertEqual(self.statuses(), {"1": Status.NEW})
        self.assertEqual(pipeline.new_lots(pipeline.build_catalog(self.config, self.registry)), ["1"])

    def test_gap_warning_names_the_forgotten_lots(self):
        for name in ("610", "611", "612", "613"):
            (self.source / name).mkdir()
        self.registry.record_import("610", "scan.pdf", 1)
        self.registry.mark_scanned("610")
        catalog = pipeline.build_catalog(self.config, self.registry)
        self.assertIn("le lot 611 n'est pas traité", pipeline.format_gaps(catalog, ["612"]))
        self.assertIsNone(pipeline.format_gaps(catalog, ["611", "612"]))
        self.assertIsNone(pipeline.format_gaps(catalog, []))


class PlanTests(PipelineTestCase):
    def test_chosen_lots_are_planned_in_numeric_order(self):
        for name in ("10", "9"):
            write_lot(self.source, name)
        plan = self.plan(["10", "9"])
        self.assertEqual([lot.name for lot in plan.incoming], ["9", "10"])
        self.assertEqual(plan.scan_pages, 20)
        self.assertFalse(plan.idle)

    def test_only_chosen_lots_are_opened(self):
        for name in ("1", "2", "3"):
            write_lot(self.source, name)
        opened = []
        plan = self.plan(["2"], pages_of=lambda pdf: opened.append(pdf.parent.name) or 4)
        self.assertEqual(([lot.name for lot in plan.incoming], opened), (["2"], ["2"]))

    def test_nothing_chosen_means_nothing_to_import(self):
        write_lot(self.source, "1")
        plan = self.plan([])
        self.assertEqual((plan.incoming, plan.idle), ([], True))

    def test_lots_copied_earlier_are_resumed_without_being_chosen(self):
        write_lot(self.root, "2")                       # copied, waiting for the scan
        plan = self.plan([])
        self.assertEqual(([lot.name for lot in plan.waiting], plan.scan_pages), (["2"], 10))

    def test_changed_source_lot_stops_everything(self):
        pdf = write_lot(self.source, "1")
        self.registry.record_import("1", "scan.pdf", pdf.stat().st_size)
        pdf.write_bytes(b"%PDF a different and longer file")
        with self.assertRaisesRegex(PipelineError, "changé.*1"):
            self.plan()

    def test_invalid_and_growing_lots_are_skipped_with_reason(self):
        write_lot(self.source, "1")
        (self.source / "2").mkdir()                      # no PDF
        growing = write_lot(self.source, "3")
        plan = self.plan(sleep=lambda seconds: growing.write_bytes(b"%PDF much longer now"))
        self.assertEqual([lot.name for lot in plan.incoming], ["1"])
        reasons = dict(plan.skipped)
        self.assertIn("no PDF", reasons["2"])
        self.assertIn("en cours", reasons["3"])

    def test_unreadable_pdf_is_skipped(self):
        write_lot(self.source, "1")
        def broken(pdf):
            raise ValueError("boom")
        self.assertEqual(self.plan(pages_of=broken).skipped, [("1", "PDF illisible (ValueError)")])

    def test_progress_reaches_one_hundred_percent(self):
        for name in ("1", "2"):
            write_lot(self.source, name)
        (self.source / "3").mkdir()                      # skipped, but still counted
        write_lot(self.root, "4")
        steps = []
        self.plan(progress=lambda done, total: steps.append((done, total)))
        self.assertEqual(steps[-1], (len(steps), len(steps)))
        self.assertEqual(steps[-1], (7, 7))   # 3 chosen lots seen twice + 1 lot already in the root

    def test_resume_state_comes_from_the_folders(self):
        write_lot(self.root, "5")
        add_pdfs(self.root / "OCR", "5_1.pdf", "5_2.pdf", ".hidden.pdf")
        add_pdfs(self.root / "Pending", "4_1.pdf")
        plan = self.plan()
        self.assertEqual((len(plan.waiting), plan.ocr_files, plan.pending_files), (1, 2, 1))

    def test_idle_when_nothing_is_left(self):
        (self.root / "Review").mkdir()
        add_pdfs(self.root / "Review", "1_1.pdf")
        self.assertTrue(self.plan().idle)

    def test_page_count_reads_real_pdf(self):
        pdf = self.source / "1" / "lot.pdf"
        pdf.parent.mkdir()
        make_pdf(pdf, ["a", "b", "c"])
        self.assertEqual(self.plan(pages_of=pipeline.count_pages).incoming[0].pages, 3)


class FormattingTests(PipelineTestCase):
    def test_plan_text_mentions_lots_pages_skipped_and_estimate(self):
        for name in ("480", "481"):
            write_lot(self.source, name)
        (self.source / "9").mkdir()
        text = pipeline.format_plan(self.plan(pages_of=lambda pdf: 1500), self.config)
        self.assertIn("2 lots (480, 481), 3 000 pages", text)
        self.assertIn("Ignoré : lot 9", text)
        self.assertIn("Durée estimée", text)

    def test_plan_text_when_only_leftover_work_remains(self):
        add_pdfs(self.root / "OCR", "1_1.pdf")
        text = pipeline.format_plan(self.plan([]), self.config)
        self.assertIn("seul le travail laissé par un lancement précédent", text)
        self.assertIn("1 page en attente d'OCR", text)
        self.assertIn("Durée estimée", text)

    def test_estimate_for_a_run_of_7446_pages(self):
        plan = pipeline.Plan(incoming=[pipeline.LotInfo("1", Path("x"), 1, 7446)])
        # Includes the sequential final classification, previously omitted.
        self.assertEqual(pipeline.format_estimate(pipeline.estimate_seconds(plan, self.config)), "3 h 56 min")

    def test_pdf_files_ignores_hidden_files_other_extensions_and_folders(self):
        add_pdfs(self.root / "OCR", "1_1.pdf", "2_1.PDF", ".hidden.pdf")
        (self.root / "OCR" / "notes.txt").write_text("x")
        (self.root / "OCR" / "folder.pdf").mkdir()
        self.assertEqual(sorted(path.name for path in pipeline.pdf_files(self.root / "OCR")), ["1_1.pdf", "2_1.PDF"])
        self.assertEqual(pipeline.pdf_files(self.root / "absent"), [])

    def test_estimate_combines_barcode_and_ocr_share(self):
        plan = pipeline.Plan(incoming=[pipeline.LotInfo("1", Path("x"), 1, 1000)], ocr_files=100)
        config = Config(source=self.source, root=self.root, workers=2)
        # Barcode + OCR + final classification; no invented worker speedup before calibration.
        self.assertAlmostEqual(pipeline.estimate_seconds(plan, config), 300 + 1800 + 120)
        self.assertEqual(pipeline.format_estimate(2100), "35 min")
        self.assertEqual(pipeline.format_estimate(56000), "15 h 33 min")
        self.assertEqual(pipeline.format_estimate(4.8), "5 s")
        self.assertEqual(pipeline.format_estimate(.17), "1 s")

    def test_idle_plan_has_no_estimate(self):
        self.assertNotIn("Durée estimée", pipeline.format_plan(pipeline.Plan(), self.config))


class ImportTests(PipelineTestCase):
    def test_copies_without_touching_the_original_and_records_it(self):
        original = write_lot(self.source, "7", b"%PDF content")
        plan = self.plan()
        summary = pipeline.Summary()
        pipeline.import_lots(self.config, self.registry, plan.incoming, summary, lambda text: None)
        self.assertEqual((self.root / "7" / "scan.pdf").read_bytes(), b"%PDF content")
        self.assertEqual(original.read_bytes(), b"%PDF content")
        self.assertEqual(summary.imported, ["7"])
        self.assertEqual(Registry(pipeline.registry_path(self.config)).lots["7"]["size"], 12)
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".import_")], [])

    def test_size_mismatch_leaves_no_partial_lot_and_no_record(self):
        write_lot(self.source, "7", b"%PDF content")
        lot = self.plan().incoming[0]
        lot = pipeline.LotInfo(lot.name, lot.pdf, lot.size + 1, lot.pages)
        with self.assertRaisesRegex(PipelineError, "taille"):
            pipeline.import_lots(self.config, self.registry, [lot], pipeline.Summary(), lambda text: None)
        self.assertFalse((self.root / "7").exists())
        self.assertFalse((self.root / ".import_7").exists())
        self.assertNotIn("7", self.registry.lots)

    def test_leftover_of_an_interrupted_copy_is_replaced(self):
        write_lot(self.source, "7")
        write_lot(self.root, ".import_7", b"half")
        pipeline.import_lots(self.config, self.registry, self.plan().incoming, pipeline.Summary(), lambda text: None)
        self.assertEqual((self.root / "7" / "scan.pdf").read_bytes(), b"%PDF fake")


class RunTests(PipelineTestCase):
    def runner(self, behaviour: dict):
        calls: list[str] = []

        def run_stage(script: str) -> int:
            calls.append(script)
            return behaviour.get(script, lambda: 0)()
        run_stage.calls = calls
        return run_stage

    def run_pipeline(self, run_stage, plan=None):
        return pipeline.run_pipeline(self.config, self.registry, plan or self.plan(), run_stage, log=lambda text: None)

    def test_full_chain_imports_scans_archives_and_runs_every_stage(self):
        write_lot(self.source, "1")
        write_lot(self.source, "2")

        def barcode_stage():
            barcode_report(self.root, [("1", True), ("2", True)])
            add_pdfs(self.root / "OCR", "1_1.pdf")
            return 0

        def ocr_stage():
            (self.root / "OCR" / "1_1.pdf").unlink()
            add_pdfs(self.root / "Pending", "1_1.pdf")
            return 0

        def review_stage():
            (self.root / "Pending" / "1_1.pdf").unlink()
            add_pdfs(self.root / "Review", "1_1.pdf", "2_4.pdf")
            return 0

        run_stage = self.runner({BARCODE_SCRIPT: barcode_stage, OCR_SCRIPT: ocr_stage, REVIEW_SCRIPT: review_stage})
        summary = self.run_pipeline(run_stage)
        self.assertEqual(run_stage.calls, [BARCODE_SCRIPT, OCR_SCRIPT, REVIEW_SCRIPT])
        self.assertTrue(summary.ok)
        self.assertEqual((summary.imported, summary.scanned, summary.failed_lots), (["1", "2"], ["1", "2"], []))
        self.assertEqual(summary.review_added, 2)
        self.assertFalse((self.root / "1").exists())
        self.assertTrue((self.root / ARCHIVE / "1" / "scan.pdf").exists())
        self.assertTrue((self.source / "1" / "scan.pdf").exists())
        self.assertIsNotNone(Registry(pipeline.registry_path(self.config)).lots["2"]["scanned_at"])

    def test_later_stages_are_skipped_when_their_folder_is_empty(self):
        write_lot(self.source, "1")
        run_stage = self.runner({BARCODE_SCRIPT: lambda: barcode_report(self.root, [("1", True)]) or 0})
        summary = self.run_pipeline(run_stage)
        self.assertEqual(run_stage.calls, [BARCODE_SCRIPT])
        self.assertTrue(summary.ok)
        self.assertEqual(summary.warnings, [])

    def test_resume_runs_only_the_stages_that_have_work(self):
        add_pdfs(self.root / "Pending", "4_1.pdf")
        run_stage = self.runner({})
        summary = self.run_pipeline(run_stage)
        self.assertEqual(run_stage.calls, [REVIEW_SCRIPT])
        self.assertTrue(summary.ok)

    def test_failed_lot_stays_in_root_and_is_reported(self):
        write_lot(self.source, "1")
        write_lot(self.source, "2")
        run_stage = self.runner({BARCODE_SCRIPT: lambda: barcode_report(self.root, [("1", True), ("2", False)]) or 1})
        summary = self.run_pipeline(run_stage)
        self.assertTrue(summary.ok)
        self.assertEqual((summary.scanned, summary.failed_lots), (["1"], ["2"]))
        self.assertTrue((self.root / "2").is_dir())
        self.assertIn("2", summary.warnings[0])

    def test_warning_exit_code_with_all_lots_accounted_for_continues(self):
        write_lot(self.source, "1")

        def barcode_stage():
            barcode_report(self.root, [("1", True)])
            add_pdfs(self.root / "OCR", "1_1.pdf")
            return 1
        run_stage = self.runner({BARCODE_SCRIPT: barcode_stage})
        summary = self.run_pipeline(run_stage)
        self.assertEqual(run_stage.calls, [BARCODE_SCRIPT, OCR_SCRIPT])
        self.assertTrue(summary.ok)
        self.assertIn("avertissements", summary.warnings[0])

    def test_crash_with_unlisted_lots_is_fatal_but_keeps_published_work(self):
        for name in ("1", "2"):
            write_lot(self.source, name)
        run_stage = self.runner({BARCODE_SCRIPT: lambda: barcode_report(self.root, [("1", True)]) or 1})
        summary = self.run_pipeline(run_stage)
        self.assertEqual(run_stage.calls, [BARCODE_SCRIPT])
        self.assertIn("arrêt anormal", summary.error)
        self.assertTrue((self.root / ARCHIVE / "1").is_dir())
        self.assertTrue((self.root / "2").is_dir())

    def test_crash_without_any_report_is_fatal(self):
        write_lot(self.source, "1")
        summary = self.run_pipeline(self.runner({BARCODE_SCRIPT: lambda: 1}))
        self.assertIsNotNone(summary.error)
        self.assertTrue((self.root / "1").is_dir())

    def test_configuration_error_code_stops_the_chain(self):
        add_pdfs(self.root / "OCR", "1_1.pdf")
        add_pdfs(self.root / "Pending", "1_2.pdf")
        run_stage = self.runner({OCR_SCRIPT: lambda: 2})
        summary = self.run_pipeline(run_stage)
        self.assertEqual(run_stage.calls, [OCR_SCRIPT])
        self.assertIn("code 2", summary.error)

    def test_interruption_still_archives_published_lots(self):
        for name in ("1", "2"):
            write_lot(self.source, name)
        run_stage = self.runner({BARCODE_SCRIPT: lambda: barcode_report(self.root, [("1", True), ("2", False)]) or 130})
        summary = self.run_pipeline(run_stage)
        self.assertTrue(summary.interrupted)
        self.assertEqual(run_stage.calls, [BARCODE_SCRIPT])
        self.assertTrue((self.root / ARCHIVE / "1").is_dir())
        self.assertTrue((self.root / "2").is_dir())

    def test_disk_error_is_reported_instead_of_crashing(self):
        write_lot(self.source, "1")
        plan = self.plan()
        original = pipeline.shutil.copy2
        pipeline.shutil.copy2 = lambda *args, **kwargs: (_ for _ in ()).throw(OSError(28, "No space left on device"))
        self.addCleanup(setattr, pipeline.shutil, "copy2", original)
        summary = self.run_pipeline(self.runner({}), plan)
        self.assertIn("No space left", summary.error)
        self.assertFalse((self.root / ".import_1").exists())

    def test_archiving_never_overwrites_an_earlier_archive(self):
        write_lot(self.root, "1", b"new")
        (self.root / ARCHIVE / "1").mkdir(parents=True)
        pipeline.archive_lot(self.config, self.registry, "1")
        names = sorted(path.name for path in (self.root / ARCHIVE).iterdir())
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], "1")

    def test_second_run_after_success_has_nothing_to_do(self):
        write_lot(self.source, "1")
        self.run_pipeline(self.runner({BARCODE_SCRIPT: lambda: barcode_report(self.root, [("1", True)]) or 0}))
        self.assertTrue(self.plan().idle)


class SummaryTests(unittest.TestCase):
    def test_statuses(self):
        self.assertIn("RÉSUMÉ : TERMINÉ\n", pipeline.format_summary(pipeline.Summary()))
        self.assertIn("AVERTISSEMENTS", pipeline.format_summary(pipeline.Summary(warnings=["x"])))
        text = pipeline.format_summary(pipeline.Summary(error="disque plein"))
        self.assertIn("ARRÊTÉ SUR ERREUR", text)
        self.assertIn("reprendra", text)
        self.assertIn("INTERROMPU", pipeline.format_summary(pipeline.Summary(interrupted=True)))

    def test_lists_stages_review_and_leftovers(self):
        summary = pipeline.Summary(
            stages=[pipeline.StageResult("Extraction par OCR", 0, 61)], review_added=3, review_total=1200, left_ocr=2)
        text = pipeline.format_summary(summary)
        self.assertIn("Extraction par OCR : OK (00:01:01)", text)
        self.assertIn("+3 page(s), 1 200 au total", text)
        self.assertIn("2 dans OCR/", text)
        self.assertIn("AVERTISSEMENTS", text)


if __name__ == "__main__":
    unittest.main()

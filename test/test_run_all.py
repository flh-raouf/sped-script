from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import run_all
import main
from scripts import lot_picker, pipeline
from test_pipeline import barcode_report, write_workbook
from test_split_pages import make_pdf


class RunAllTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.source, self.root = base / "source", base / "root"
        self.source.mkdir()
        self.root.mkdir()
        write_workbook(self.root / "BDD.xlsx")
        write_workbook(self.root / "dirty list.xlsx")
        self.config = base / "config.toml"
        self.config.write_text(
            f'[dossiers]\nsource = "{self.source}"\nracine = "{self.root}"\n'
            '[traitement]\nprocessus = 3\nattente_stabilite = 0\n', encoding="utf-8")
        self.commands: list[list[str]] = []

    def execute(self, command):
        self.commands.append(command)
        barcode_report(self.root, [(lot.name, True) for lot in pipeline.lot_directories(self.root)])
        return 0

    def add_lots(self, *names):
        for name in names:
            (self.source / name).mkdir()
            make_pdf(self.source / name / "scan.pdf", ["a", "b"])

    def launch(self, *arguments):
        output = StringIO()
        with patch.object(run_all, "execute_command", self.execute), \
                patch("scripts.wsl_host.open_folder") as opened, redirect_stdout(output):
            code = run_all.main(["--config", str(self.config), *arguments])
        return code, output.getvalue(), opened

    def launch_interactive(self, pick):
        """Run as the shortcut does, with ``pick`` standing for the person using the list."""
        output = StringIO()
        arguments = run_all.parse_args(["--config", str(self.config)])
        with patch.object(run_all, "execute_command", self.execute), patch.object(run_all, "confirm", return_value=True), \
                patch("scripts.wsl_host.open_folder"), patch.object(lot_picker, "pick_lots", pick), redirect_stdout(output):
            code = run_all.run(arguments, interactive=True)
        return code, output.getvalue()

    def test_unattended_run_builds_the_barcode_command_from_the_config(self):
        (self.source / "1").mkdir()
        make_pdf(self.source / "1" / "scan.pdf", ["a", "b"])
        code, output, opened = self.launch("--yes")
        self.assertEqual(code, 0)
        command = self.commands[0]
        self.assertEqual(Path(command[command.index("-d") + 1]).resolve(), self.root.resolve())
        self.assertEqual(command[command.index("-n") + 1], "3")
        self.assertEqual(Path(command[command.index("--excel") + 1]).name, "dirty list.xlsx")
        self.assertEqual(Path(command[command.index("--database") + 1]).name, "BDD.xlsx")
        self.assertIn("RÉSUMÉ : TERMINÉ", output)
        opened.assert_not_called()
        summaries = list((self.root / "Rapports" / "Executions").glob("* - lancement complet.txt"))
        self.assertEqual(len(summaries), 1)
        self.assertIn("RÉSUMÉ : TERMINÉ", summaries[0].read_text(encoding="utf-8"))

    def test_stage_runner_uses_separate_worker_counts_for_each_stage(self):
        config = pipeline.Config(source=self.source, root=self.root, workers=6, ocr_workers=3, review_workers=2)
        with patch.object(run_all, 'execute_command', return_value=0) as execute:
            run_stage = run_all.make_stage_runner(config, self.root / 'BDD.xlsx', self.root / 'dirty list.xlsx')
            for stage in (pipeline.BARCODE_SCRIPT, pipeline.OCR_SCRIPT, pipeline.REVIEW_SCRIPT):
                self.assertEqual(run_stage(stage), 0)
        barcode, ocr, review = [call.args[0] for call in execute.call_args_list]
        self.assertEqual(barcode[barcode.index('-n') + 1], '6')
        self.assertEqual(ocr[ocr.index('-n') + 1], '3')
        self.assertEqual(review[review.index('-n') + 1], '2')

    def test_launcher_interrupt_waits_for_worker_cleanup(self):
        command = main.build_command('review_pages.py', self.root, workers=2)
        with patch.object(main.subprocess, 'Popen') as popen:
            process = popen.return_value.__enter__.return_value
            process.wait.side_effect = [KeyboardInterrupt, 130]
            self.assertEqual(main.execute_command(command), 130)
        process.kill.assert_not_called()
        self.assertTrue(all(not call.kwargs for call in process.wait.call_args_list))

    def test_unattended_ocr_resume_uses_the_configured_worker_count(self):
        self.config.write_text(self.config.read_text() + 'processus_ocr = 3\n')
        (self.root / 'OCR').mkdir()
        (self.root / 'OCR' / '1_1.pdf').write_bytes(b'%PDF')
        code, _, _ = self.launch('--yes')
        self.assertEqual(code, 0)
        command = self.commands[0]
        self.assertEqual(Path(command[2]).name, 'ocr_pages.py')
        self.assertEqual(command[command.index('-n') + 1], '3')

    def test_unattended_final_stage_resume_uses_the_configured_worker_count(self):
        self.config.write_text(self.config.read_text() + 'processus_classification = 2\n')
        (self.root / 'Pending').mkdir()
        (self.root / 'Pending' / '1_1.pdf').write_bytes(b'%PDF')
        code, _, _ = self.launch('--yes')
        self.assertEqual(code, 0)
        command = self.commands[0]
        self.assertEqual(Path(command[2]).name, 'review_pages.py')
        self.assertEqual(command[command.index('-n') + 1], '2')

    def test_unattended_run_takes_every_new_lot(self):
        self.add_lots("1", "2", "3")
        self.assertEqual(self.launch("--yes")[0], 0)
        self.assertEqual(pipeline.lot_directories(self.root), [])   # scanned copies deleted
        self.assertFalse((self.root / "Archive").exists())
        registry = pipeline.Registry(pipeline.registry_path(pipeline.load_config(self.config)))
        self.assertTrue(all(registry.lots[name]["scanned_at"] for name in ("1", "2", "3")))

    def test_only_the_lots_ticked_in_the_list_are_processed(self):
        self.add_lots("1", "2", "3")
        shown = []
        code, output = self.launch_interactive(lambda catalog: shown.append(catalog) or ["2"])
        self.assertEqual(code, 0)
        self.assertEqual([lot.name for lot in shown[0]], ["3", "2", "1"])
        registry = pipeline.Registry(pipeline.registry_path(pipeline.load_config(self.config)))
        self.assertEqual([name for name, lot in registry.lots.items() if lot["scanned_at"]], ["2"])
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), ["1", "2", "3"])
        self.assertIn("Lots à importer : 1 lot (2), 2 pages", output)

    def test_quitting_the_list_changes_nothing(self):
        self.add_lots("1")
        code, output = self.launch_interactive(lambda catalog: None)
        self.assertEqual((code, self.commands), (0, []))
        self.assertIn("Rien n'a été modifié", output)
        self.assertFalse((self.root / "1").exists())

    def test_skipping_a_lot_between_processed_ones_is_flagged_before_the_analysis(self):
        self.add_lots("610", "611", "612")
        # A registry kept in the former Reports/ folder is taken over.
        registry = pipeline.Registry(self.root / "Reports" / "pipeline_state.json")
        registry.record_import("610", "scan.pdf", 1)
        registry.mark_scanned("610")
        _, output = self.launch_interactive(lambda catalog: ["612"])
        self.assertIn("le lot 611 n'est pas traité", output)
        self.assertFalse((self.root / "Reports" / "pipeline_state.json").exists())
        self.assertTrue((self.root / "Rapports" / ".interne" / "pipeline_state.json").exists())

    def test_list_is_not_shown_when_there_is_nothing_new_to_pick(self):
        def refuse(catalog):
            raise AssertionError("the list must not be shown")
        self.assertEqual(self.launch_interactive(refuse)[0], 0)

    def test_terminal_that_cannot_show_the_list_is_reported_clearly(self):
        self.add_lots("1")
        def broken(catalog):
            raise lot_picker.PickerError("TERM inconnu")
        with self.assertRaisesRegex(pipeline.PipelineError, "afficher la liste.*TERM inconnu"):
            self.launch_interactive(broken)

    def test_nothing_to_do_does_not_run_any_stage(self):
        code, output, _ = self.launch("--yes")
        self.assertEqual((code, self.commands), (0, []))
        self.assertIn("Rien à traiter", output)

    def test_configuration_problem_returns_exit_code_2(self):
        self.config.unlink()
        self.assertEqual(self.launch("--yes")[0], 2)


if __name__ == "__main__":
    unittest.main()

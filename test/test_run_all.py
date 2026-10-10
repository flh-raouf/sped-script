from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import run_all
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
        barcode_report(self.root, [("1", True)])
        return 0

    def launch(self, *arguments):
        output = StringIO()
        with patch.object(run_all, "execute_command", self.execute), \
                patch("scripts.wsl_host.open_folder") as opened, redirect_stdout(output):
            code = run_all.main(["--config", str(self.config), *arguments])
        return code, output.getvalue(), opened

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
        self.assertEqual(len(list((self.root / "Reports" / "pipeline").glob("*.txt"))), 1)

    def test_nothing_to_do_does_not_run_any_stage(self):
        code, output, _ = self.launch("--yes")
        self.assertEqual((code, self.commands), (0, []))
        self.assertIn("Rien à traiter", output)

    def test_configuration_problem_returns_exit_code_2(self):
        self.config.unlink()
        self.assertEqual(self.launch("--yes")[0], 2)


if __name__ == "__main__":
    unittest.main()

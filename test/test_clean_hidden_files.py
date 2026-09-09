from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import clean_hidden_files


class CleanHiddenFilesTests(unittest.TestCase):
    def make_tree(self, directory: str) -> Path:
        root = Path(directory)
        (root / "1" / "subfolder").mkdir(parents=True)
        (root / ".keep-root").write_text("keep", encoding="utf-8")
        (root / "1" / ".DS_Store").write_text("delete", encoding="utf-8")
        (root / "1" / "document.pdf").write_text("keep", encoding="utf-8")
        (root / "1" / "subfolder" / ".hidden_file").write_text(
            "delete", encoding="utf-8"
        )
        (root / "1" / ".cache").mkdir()
        (root / "1" / ".cache" / ".nested_hidden").write_text(
            "delete", encoding="utf-8"
        )
        (root / "results.csv").write_text("keep", encoding="utf-8")
        return root

    def test_dry_run_reports_candidates_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_tree(directory)
            stats = clean_hidden_files.clean_hidden_files(root, dry_run=True)

            self.assertEqual(stats.hidden_files_found, 3)
            self.assertEqual(stats.successfully_deleted, 0)
            self.assertEqual(stats.failed_deletions, 0)
            self.assertTrue((root / ".keep-root").exists())
            self.assertTrue((root / "1" / ".DS_Store").exists())
            self.assertTrue((root / "1" / "subfolder" / ".hidden_file").exists())
            self.assertTrue((root / "1" / ".cache").is_dir())

    def test_real_run_deletes_only_hidden_files_at_depth_two_or_greater(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_tree(directory)
            stats = clean_hidden_files.clean_hidden_files(root)

            self.assertEqual(stats.hidden_files_found, 3)
            self.assertEqual(stats.successfully_deleted, 3)
            self.assertEqual(stats.failed_deletions, 0)
            self.assertTrue((root / ".keep-root").exists())
            self.assertTrue((root / "results.csv").exists())
            self.assertTrue((root / "1" / "document.pdf").exists())
            self.assertFalse((root / "1" / ".DS_Store").exists())
            self.assertFalse((root / "1" / "subfolder" / ".hidden_file").exists())
            self.assertTrue((root / "1" / ".cache").is_dir())
            self.assertFalse((root / "1" / ".cache" / ".nested_hidden").exists())

    def test_symlinked_hidden_file_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lot = root / "1"
            lot.mkdir()
            target = lot / "normal.txt"
            target.write_text("keep", encoding="utf-8")
            link = lot / ".hidden-link"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                self.skipTest("symbolic links are unavailable on this platform")

            stats = clean_hidden_files.clean_hidden_files(root)

            self.assertEqual(stats.hidden_files_found, 0)
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.exists())

    def test_missing_root_is_a_configuration_error(self) -> None:
        with self.assertRaises(clean_hidden_files.ConfigurationError):
            clean_hidden_files.clean_hidden_files(Path("/path/that/does/not/exist"))


if __name__ == "__main__":
    unittest.main()

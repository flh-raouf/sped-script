"""Safely remove hidden files below a document-processing root directory.

This implements the useful part of::

    find ROOT -mindepth 2 -type f -name '.*' -delete

without relying on a platform-specific shell command. Hidden directories are
never removed, although hidden files inside them are eligible for cleanup.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


class ConfigurationError(RuntimeError):
    """A fatal command-line or root-directory configuration problem."""


@dataclass
class CleanupStats:
    hidden_files_found: int = 0
    successfully_deleted: int = 0
    failed_deletions: int = 0
    errors: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove hidden files at depth two or greater below a root directory. "
            "Hidden directories are retained."
        )
    )
    parser.add_argument(
        "-d",
        "--directory",
        required=True,
        type=Path,
        help="Root directory to inspect",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report eligible files without deleting anything",
    )
    return parser.parse_args(argv)


def validate_root(directory: Path) -> Path:
    root = directory.expanduser().resolve()
    if not root.exists():
        raise ConfigurationError(f"Root directory does not exist: {root}")
    if not root.is_dir():
        raise ConfigurationError(f"Root path is not a directory: {root}")
    return root


def _report_traversal_error(stats: CleanupStats, path: Path, exc: OSError) -> None:
    message = f"Cannot inspect {path}: {type(exc).__name__}: {exc}"
    stats.add_error(message)
    print(f"ERROR: {message}", file=sys.stderr, flush=True)


def iter_hidden_files(root: Path, stats: CleanupStats) -> Iterator[Path]:
    """Yield actual hidden files at relative depth >= 2.

    Traversal is deliberately implemented with ``Path.iterdir`` instead of
    ``rglob`` so permission errors can be reported for the exact directory
    while the rest of the tree continues to be inspected.
    """

    def walk(directory: Path, directory_depth: int) -> Iterator[Path]:
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda entry: (entry.name.casefold(), entry.name),
            )
        except OSError as exc:
            _report_traversal_error(stats, directory, exc)
            return

        for entry in entries:
            entry_depth = directory_depth + 1
            try:
                # Never follow symlinked directories, and never delete a
                # symlink even if its target is a regular hidden file.
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    yield from walk(entry, entry_depth)
                    continue
                if (
                    entry_depth >= 2
                    and entry.name.startswith(".")
                    and entry.is_file()
                ):
                    yield entry
            except OSError as exc:
                _report_traversal_error(stats, entry, exc)

    yield from walk(root, 0)


def clean_hidden_files(root_directory: Path, *, dry_run: bool = False) -> CleanupStats:
    root = validate_root(root_directory)
    stats = CleanupStats()

    for path in iter_hidden_files(root, stats):
        stats.hidden_files_found += 1
        if dry_run:
            print(f"Would delete: {path}", flush=True)
            continue

        try:
            path.unlink()
        except OSError as exc:
            stats.failed_deletions += 1
            message = f"Cannot delete {path}: {type(exc).__name__}: {exc}"
            stats.add_error(message)
            print(f"ERROR: {message}", file=sys.stderr, flush=True)
        else:
            stats.successfully_deleted += 1
            print(f"Deleted: {path}", flush=True)

    print(f"Hidden files found: {stats.hidden_files_found}")
    if dry_run:
        print(f"Would delete: {stats.hidden_files_found}")
    print(f"Successfully deleted: {stats.successfully_deleted}")
    print(f"Failed deletions: {stats.failed_deletions}")
    if stats.errors:
        print(f"Traversal/errors: {len(stats.errors)}")
    return stats


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        stats = clean_hidden_files(args.directory, dry_run=args.dry_run)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

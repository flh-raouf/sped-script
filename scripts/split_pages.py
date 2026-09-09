"""Split every page of every numeric Lot PDF into its own PDF file.

The source PDFs are never rasterized. Each output is written by copying the
original pypdf page object into a new one-page PDF. Lots are processed
sequentially so only one source PDF is open at a time and the parent process
owns all output writes.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


OUTPUT_DIRECTORY_NAME = "split_pages"
REPORT_NAME = "split_pages_report.txt"
LOG_NAME = "split_pages.log"
NUMERIC_LOT_RE = re.compile(r"^[0-9]+$")


class ConfigurationError(RuntimeError):
    """A fatal command-line or root-directory configuration problem."""


@dataclass(frozen=True)
class LotPlan:
    name: str
    directory: Path
    pdf_path: Path


@dataclass
class SplitStats:
    lots_discovered: int = 0
    lots_processed: int = 0
    lots_failed: int = 0
    source_pdfs_processed: int = 0
    total_pages: int = 0
    pages_seen: int = 0
    single_page_pdfs_created: int = 0
    failed_pages: int = 0
    errors: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)


def natural_lot_sort_key(name: str) -> tuple[int, str]:
    """Sort numeric Lot names numerically while retaining stable tie order."""

    return int(name), name


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Split every page of every numeric Lot PDF into an individual "
            "one-page PDF without rasterization."
        )
    )
    parser.add_argument(
        "-d",
        "--directory",
        required=True,
        type=Path,
        help="Root directory containing the Lot folders",
    )
    return parser.parse_args(argv)


def validate_root(directory: Path) -> Path:
    root = directory.expanduser().resolve()
    if not root.exists():
        raise ConfigurationError(f"Root directory does not exist: {root}")
    if not root.is_dir():
        raise ConfigurationError(f"Root path is not a directory: {root}")
    try:
        import pypdf  # noqa: F401
    except ImportError as exc:
        raise ConfigurationError(
            "pypdf is required; install dependencies with "
            "'python -m pip install -r requirements.txt'"
        ) from exc
    return root


def configure_logger(root: Path) -> logging.Logger:
    logger = logging.getLogger("split_pages")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = logging.FileHandler(root / LOG_NAME, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def discover_lot_directories(root: Path) -> list[Path]:
    try:
        directories = [
            path
            for path in root.iterdir()
            if path.is_dir() and NUMERIC_LOT_RE.fullmatch(path.name)
        ]
    except OSError as exc:
        raise ConfigurationError(f"Cannot list root directory {root}: {exc}") from exc
    return sorted(directories, key=lambda path: natural_lot_sort_key(path.name))


def discover_lot_pdf(lot_directory: Path) -> tuple[Path | None, str | None]:
    try:
        pdfs = sorted(
            path
            for path in lot_directory.iterdir()
            if path.is_file()
            and path.suffix.casefold() == ".pdf"
            and not path.name.startswith(".")
        )
    except OSError as exc:
        return None, f"cannot list Lot directory: {exc}"

    if not pdfs:
        return None, "no PDF found"
    if len(pdfs) > 1:
        names = ", ".join(path.name for path in pdfs)
        return None, f"multiple PDFs found ({names})"
    return pdfs[0], None


def close_pdf_reader(reader: Any) -> None:
    stream = getattr(reader, "stream", None)
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def unique_backup_path(output_directory: Path) -> Path | None:
    if not output_directory.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_directory.with_name(
        f"{output_directory.name}_backup_{timestamp}"
    )
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = output_directory.with_name(f"{base.name}_{counter}")
        counter += 1
    return candidate


def publish_staging_directory(
    staging_directory: Path,
    output_directory: Path,
    backup_path: Path | None,
) -> None:
    """Publish a complete fresh output directory, preserving an old one."""

    if backup_path is not None:
        output_directory.rename(backup_path)
    try:
        staging_directory.rename(output_directory)
    except Exception:
        if (
            backup_path is not None
            and backup_path.exists()
            and not output_directory.exists()
        ):
            backup_path.rename(output_directory)
        raise


def report_error(
    stats: SplitStats,
    logger: logging.Logger,
    message: str,
    *,
    print_message: bool = True,
) -> None:
    stats.add_error(message)
    logger.error(message)
    if print_message:
        print(f"ERROR: {message}", file=sys.stderr, flush=True)


def write_one_page_pdf(
    *,
    source_page: Any,
    output_path: Path,
) -> None:
    """Copy one original page into a verified one-page PDF atomically."""

    from pypdf import PdfReader, PdfWriter

    if output_path.exists():
        raise FileExistsError(f"output collision: {output_path.name} already exists")

    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    temporary_path.unlink(missing_ok=True)
    writer = PdfWriter()
    writer.add_page(source_page)
    try:
        with temporary_path.open("xb") as handle:
            writer.write(handle)
            handle.flush()
            os.fsync(handle.fileno())

        verification = PdfReader(str(temporary_path), strict=False)
        try:
            page_count = len(verification.pages)
        finally:
            close_pdf_reader(verification)
        if page_count != 1:
            raise RuntimeError(
                f"verification found {page_count} pages instead of exactly one"
            )
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def process_lot(
    *,
    plan: LotPlan,
    output_directory: Path,
    stats: SplitStats,
    logger: logging.Logger,
) -> None:
    from pypdf import PdfReader

    reader = None
    lot_failed = False
    try:
        reader = PdfReader(str(plan.pdf_path), strict=False)
        page_count = len(reader.pages)
        stats.source_pdfs_processed += 1
        stats.total_pages += page_count
        logger.info(
            "Lot %s: opened %s with %d pages",
            plan.name,
            plan.pdf_path.name,
            page_count,
        )
    except Exception as exc:
        message = (
            f"Lot {plan.name}: cannot open {plan.pdf_path.name}: "
            f"{type(exc).__name__}: {exc}"
        )
        report_error(stats, logger, message)
        stats.lots_failed += 1
        close_pdf_reader(reader)
        return

    try:
        for zero_based_page in range(page_count):
            page_number = zero_based_page + 1
            stats.pages_seen += 1
            print(
                f"Lot {plan.name} | Page {page_number}/{page_count} | "
                f"Global {stats.pages_seen} pages",
                flush=True,
            )
            output_path = output_directory / f"{plan.name}_{page_number}.pdf"
            try:
                source_page = reader.pages[zero_based_page]
                write_one_page_pdf(source_page=source_page, output_path=output_path)
                stats.single_page_pdfs_created += 1
            except Exception as exc:
                lot_failed = True
                stats.failed_pages += 1
                message = (
                    f"Lot {plan.name} | Page {page_number}/{page_count}: "
                    f"failed to create {output_path.name}: "
                    f"{type(exc).__name__}: {exc}"
                )
                report_error(stats, logger, message)
    finally:
        close_pdf_reader(reader)

    if lot_failed:
        stats.lots_failed += 1
    else:
        stats.lots_processed += 1


def format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds_remainder = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_remainder:02d}"


def write_report(
    path: Path,
    *,
    root: Path,
    output_directory: Path,
    started_at: datetime,
    finished_at: datetime,
    elapsed: float,
    stats: SplitStats,
) -> None:
    lines = [
        "Single-page PDF split report",
        "=============================",
        f"Start time: {started_at.isoformat(timespec='seconds')}",
        f"Finish time: {finished_at.isoformat(timespec='seconds')}",
        f"Total processing time: {format_duration(elapsed)}",
        "",
        f"Root directory: {root}",
        f"Output directory: {output_directory}",
        f"Lots discovered: {stats.lots_discovered:,}",
        f"Lots successfully processed: {stats.lots_processed:,}",
        f"Lots skipped/failed: {stats.lots_failed:,}",
        f"Source PDFs processed: {stats.source_pdfs_processed:,}",
        f"Total source pages: {stats.total_pages:,}",
        "Single-page PDFs successfully created: "
        f"{stats.single_page_pdfs_created:,}",
        f"Failed page extractions/writes: {stats.failed_pages:,}",
        "",
        "Failures",
        "--------",
    ]
    if stats.errors:
        lines.extend(f"- {message}" for message in stats.errors)
    else:
        lines.append("None")
    lines.append("")

    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def run_split(root_directory: Path) -> tuple[int, Path, Path]:
    root = validate_root(root_directory)
    lot_directories = discover_lot_directories(root)
    if not lot_directories:
        raise ConfigurationError(
            f"No numeric Lot directories found directly inside {root}"
        )

    started_at = datetime.now().astimezone()
    started_clock = time.perf_counter()
    stats = SplitStats(lots_discovered=len(lot_directories))
    output_directory = root / OUTPUT_DIRECTORY_NAME
    backup_path = unique_backup_path(output_directory)
    staging_directory = Path(
        tempfile.mkdtemp(prefix=f".{OUTPUT_DIRECTORY_NAME}.", dir=root)
    )
    logger = configure_logger(root)
    logger.info("Starting split from root %s", root)
    logger.info("Discovered %d numeric Lot directories", len(lot_directories))

    try:
        plans: list[LotPlan] = []
        for lot_directory in lot_directories:
            pdf_path, error = discover_lot_pdf(lot_directory)
            if error:
                report_error(stats, logger, f"Lot {lot_directory.name}: {error}")
                stats.lots_failed += 1
                continue
            assert pdf_path is not None
            plans.append(LotPlan(lot_directory.name, lot_directory, pdf_path))

        if not plans:
            report_error(
                stats,
                logger,
                "No Lot PDFs were available for processing; output is empty",
            )

        for plan in plans:
            process_lot(
                plan=plan,
                output_directory=staging_directory,
                stats=stats,
                logger=logger,
            )

        finished_at = datetime.now().astimezone()
        elapsed = time.perf_counter() - started_clock
        # Publish only after all pages have been attempted. A previous output
        # directory is retained as a backup instead of being mixed with this run.
        publish_staging_directory(staging_directory, output_directory, backup_path)
        write_report(
            root / REPORT_NAME,
            root=root,
            output_directory=output_directory,
            started_at=started_at,
            finished_at=finished_at,
            elapsed=elapsed,
            stats=stats,
        )
        logger.info(
            "Finished: %d/%d pages created, %d Lots failed, elapsed %s",
            stats.single_page_pdfs_created,
            stats.total_pages,
            stats.lots_failed,
            format_duration(elapsed),
        )
        return (1 if stats.errors else 0), output_directory, root / REPORT_NAME
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise
    except Exception:
        logger.exception("Fatal split failure; previous output was not replaced")
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise
    finally:
        close_logger(logger)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        exit_code, output_directory, report_path = run_split(args.directory)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Finished. Output directory: {output_directory}")
    print(f"Report: {report_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

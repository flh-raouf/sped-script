"""Shared, correctness-first PDF reconstruction engine.

The two public entry points select either range reconstruction or explicitly
identified pages. CSV analysis is common so both strategies resolve Badge/VIS
identity and reject conflicts in exactly the same way.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

CSV_FILENAME = "results.csv"
REPORT_FILENAME = "reconstruction_report.txt"
LOG_FILENAME = "reconstruction.log"

# Keep this consistent with script.py: current datasets contain Badge values
# ranging from one to five digits, and leading zeroes remain significant.
BADGE_RE = re.compile(r"^\d{1,5}$")
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
VIS_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{8}$")
POSITIVE_INTEGER_RE = re.compile(r"^[1-9]\d*$")


class ConfigurationError(RuntimeError):
    """A fatal configuration or input-contract problem."""


class Strategy(str, Enum):
    RANGES = "ranges"
    IDENTIFIED = "identified"

    @property
    def output_directory(self) -> str:
        if self is Strategy.RANGES:
            return "reconstructed_ranges"
        return "reconstructed_identified_pages"

    @property
    def report_title(self) -> str:
        if self is Strategy.RANGES:
            return "Range reconstruction"
        return "Identified-pages-only reconstruction"


@dataclass(frozen=True, order=True)
class DocumentId:
    badge: str
    vis: str

    @property
    def filename(self) -> str:
        # Populated fields have already passed strict ASCII identifier validation.
        # An empty field denotes a deliberately isolated, unresolved identifier.
        return f"{self.badge}_{self.vis}.pdf"


@dataclass(frozen=True)
class CsvRecord:
    line_number: int
    lot: str | None
    page_number: int | None
    badge: str | None
    vin: str | None
    vis: str | None
    is_found: bool
    identity_usable: bool = True

    @property
    def location(self) -> str:
        lot = self.lot if self.lot is not None else "?"
        page = self.page_number if self.page_number is not None else "?"
        return f"CSV line {self.line_number} (Lot {lot}, page {page})"


@dataclass
class CsvAnalysis:
    total_rows: int = 0
    found_rows: int = 0
    records: list[CsvRecord] = field(default_factory=list)
    resolved_records: list[tuple[CsvRecord, DocumentId]] = field(default_factory=list)
    target_documents: set[DocumentId] = field(default_factory=set)
    explicit_pages: dict[DocumentId, set[tuple[str, int]]] = field(default_factory=dict)
    duplicate_page_references_removed: int = 0
    warnings: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    blocking_conflicts: list[str] = field(default_factory=list)
    partial_identifiers: list[str] = field(default_factory=list)


@dataclass
class ReconstructionStats:
    lots_referenced: int = 0
    lots_accessed: int = 0
    unique_physical_pages_read: int = 0
    pages_copied_during_extraction: int = 0
    source_pages_extracted: int = 0
    explicit_pages_added: int = 0
    intermediate_pages_added: int = 0
    output_pdfs_created: int = 0
    identifiers_spanning_lots: int = 0
    local_ranges_reconstructed: int = 0
    failed_documents: set[DocumentId] = field(default_factory=set)
    issues: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add_issue(self, category: str, message: str) -> None:
        self.issues[category].append(message)


def natural_sort_key(value: str) -> tuple[int, int | str, str]:
    stripped = value.strip()
    if stripped.isdecimal():
        return 0, int(stripped), stripped
    return 1, stripped.casefold(), stripped


def normalize_header(value: object) -> str:
    text = "" if value is None else str(value)
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(
        character for character in ascii_text.casefold() if character.isalnum()
    )


def _normalize_optional_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize_badge(value: object) -> str | None:
    text = _normalize_optional_text(value)
    return text if BADGE_RE.fullmatch(text) else None


def normalize_vin(value: object) -> str | None:
    text = _normalize_optional_text(value).upper()
    return text if VIN_RE.fullmatch(text) else None


def normalize_vis(value: object) -> str | None:
    text = _normalize_optional_text(value).upper()
    return text if VIS_RE.fullmatch(text) else None


def normalize_lot(value: object) -> str | None:
    text = _normalize_optional_text(value)
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        return None
    return text


def normalize_page_number(value: object) -> int | None:
    text = _normalize_optional_text(value)
    if not POSITIVE_INTEGER_RE.fullmatch(text):
        return None
    return int(text)


def normalize_found(value: object) -> tuple[bool, bool]:
    """Return (is_found, marker_is_recognized)."""

    marker = _normalize_optional_text(value).casefold()
    if marker == "":
        return False, True
    if marker == "found":
        return True, True
    return False, False


HEADER_ALIASES = {
    "lot": {"lot"},
    "page": {"numeropage", "page", "pagenumber", "nopage"},
    "badge": {"bdg", "badge"},
    "vin": {"vin"},
    "vis": {"vis"},
    "found": {"found"},
}


def resolve_csv_columns(fieldnames: Sequence[str | None] | None) -> dict[str, str]:
    if not fieldnames:
        raise ConfigurationError("results.csv has no header row")
    normalized_to_original: defaultdict[str, list[str]] = defaultdict(list)
    for fieldname in fieldnames:
        if fieldname is None:
            continue
        normalized_to_original[normalize_header(fieldname)].append(fieldname)

    resolved: dict[str, str] = {}
    for logical_name, aliases in HEADER_ALIASES.items():
        matches = [
            original
            for alias in aliases
            for original in normalized_to_original.get(alias, ())
        ]
        if not matches:
            raise ConfigurationError(
                f"results.csv is missing required column {logical_name!r}; "
                f"headers are {list(fieldnames)!r}"
            )
        if len(matches) > 1:
            raise ConfigurationError(
                f"results.csv has ambiguous columns for {logical_name!r}: {matches}"
            )
        resolved[logical_name] = matches[0]
    return resolved


def read_csv_records(path: Path) -> CsvAnalysis:
    analysis = CsvAnalysis()
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ConfigurationError(f"Cannot open {path}: {exc}") from exc

    try:
        with handle:
            reader = csv.DictReader(handle)
            columns = resolve_csv_columns(reader.fieldnames)
            try:
                for line_number, row in enumerate(reader, start=2):
                    analysis.total_rows += 1
                    raw_lot = _normalize_optional_text(row.get(columns["lot"]))
                    raw_page = _normalize_optional_text(row.get(columns["page"]))
                    raw_badge = _normalize_optional_text(row.get(columns["badge"]))
                    raw_vin = _normalize_optional_text(row.get(columns["vin"]))
                    raw_vis = _normalize_optional_text(row.get(columns["vis"]))

                    lot = normalize_lot(raw_lot)
                    page_number = normalize_page_number(raw_page)
                    badge = normalize_badge(raw_badge)
                    vin = normalize_vin(raw_vin)
                    explicit_vis = normalize_vis(raw_vis)
                    is_found, marker_ok = normalize_found(row.get(columns["found"]))
                    if is_found:
                        analysis.found_rows += 1

                    prefix = f"CSV line {line_number}"
                    if raw_lot and lot is None:
                        analysis.warnings.append(f"{prefix}: invalid Lot {raw_lot!r}")
                    elif not raw_lot:
                        analysis.warnings.append(f"{prefix}: blank Lot")
                    if raw_page and page_number is None:
                        analysis.warnings.append(
                            f"{prefix}: invalid 1-based page number {raw_page!r}"
                        )
                    elif not raw_page:
                        analysis.warnings.append(f"{prefix}: blank page number")
                    if raw_badge and badge is None:
                        analysis.warnings.append(
                            f"{prefix}: invalid Badge {raw_badge!r}"
                        )
                    if raw_vin and vin is None:
                        analysis.warnings.append(f"{prefix}: invalid VIN {raw_vin!r}")
                    if raw_vis and explicit_vis is None:
                        analysis.warnings.append(f"{prefix}: invalid VIS {raw_vis!r}")
                    if not marker_ok:
                        marker = row.get(columns["found"])
                        analysis.warnings.append(
                            f"{prefix}: unrecognized found marker {marker!r}; "
                            "treated as not found"
                        )

                    derived_vis = vin[-8:] if vin else None
                    identity_usable = True
                    if explicit_vis and derived_vis and explicit_vis != derived_vis:
                        identity_usable = False
                        message = (
                            f"{prefix}: VIS {explicit_vis} conflicts with VIN-derived "
                            f"VIS {derived_vis}"
                        )
                        analysis.conflicts.append(message)
                        if is_found:
                            analysis.blocking_conflicts.append(message)
                    vis = explicit_vis or derived_vis
                    analysis.records.append(
                        CsvRecord(
                            line_number=line_number,
                            lot=lot,
                            page_number=page_number,
                            badge=badge,
                            vin=vin,
                            vis=vis,
                            is_found=is_found,
                            identity_usable=identity_usable,
                        )
                    )
            except csv.Error as exc:
                raise ConfigurationError(
                    f"Malformed CSV near line {reader.line_num}: {exc}"
                ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"results.csv is not valid UTF-8: {exc}") from exc

    if analysis.total_rows == 0:
        raise ConfigurationError("results.csv contains no data rows")
    return analysis


def _evidence_locations(
    pair_rows: dict[tuple[str, str], list[CsvRecord]], badge: str, vis: str
) -> str:
    records = pair_rows[(badge, vis)]
    return ", ".join(record.location for record in records[:8])


def resolve_document_identities(
    analysis: CsvAnalysis, *, allow_isolated_partial_identifiers: bool = False
) -> None:
    badge_to_vis: defaultdict[str, set[str]] = defaultdict(set)
    vis_to_badge: defaultdict[str, set[str]] = defaultdict(set)
    pair_rows: defaultdict[tuple[str, str], list[CsvRecord]] = defaultdict(list)

    for record in analysis.records:
        if not record.identity_usable:
            continue
        if record.badge and record.vis:
            badge_to_vis[record.badge].add(record.vis)
            vis_to_badge[record.vis].add(record.badge)
            pair_rows[(record.badge, record.vis)].append(record)

    conflicting_badges = {
        badge for badge, values in badge_to_vis.items() if len(values) > 1
    }
    conflicting_vises = {vis for vis, values in vis_to_badge.items() if len(values) > 1}

    found_badges = {
        record.badge for record in analysis.records if record.is_found and record.badge
    }
    found_vises = {
        record.vis for record in analysis.records if record.is_found and record.vis
    }

    for badge in sorted(conflicting_badges):
        details = "; ".join(
            f"{vis} at {_evidence_locations(pair_rows, badge, vis)}"
            for vis in sorted(badge_to_vis[badge])
        )
        message = f"Badge {badge} maps to multiple VIS values: {details}"
        analysis.conflicts.append(message)
        if badge in found_badges or badge_to_vis[badge] & found_vises:
            analysis.blocking_conflicts.append(message)
    for vis in sorted(conflicting_vises):
        details = "; ".join(
            f"{badge} at {_evidence_locations(pair_rows, badge, vis)}"
            for badge in sorted(vis_to_badge[vis])
        )
        message = f"VIS {vis} maps to multiple Badges: {details}"
        analysis.conflicts.append(message)
        if vis in found_vises or vis_to_badge[vis] & found_badges:
            analysis.blocking_conflicts.append(message)
    stable_badge_to_vis: dict[str, str] = {}
    stable_vis_to_badge: dict[str, str] = {}
    for badge, values in badge_to_vis.items():
        if badge in conflicting_badges or len(values) != 1:
            continue
        vis = next(iter(values))
        if vis in conflicting_vises:
            continue
        if vis_to_badge.get(vis) == {badge}:
            stable_badge_to_vis[badge] = vis
            stable_vis_to_badge[vis] = badge

    def resolve(record: CsvRecord) -> DocumentId | None:
        if not record.identity_usable:
            return None
        if record.badge and record.vis:
            if stable_badge_to_vis.get(record.badge) == record.vis:
                return DocumentId(record.badge, record.vis)
            return None
        if record.badge:
            vis = stable_badge_to_vis.get(record.badge)
            return DocumentId(record.badge, vis) if vis else None
        if record.vis:
            badge = stable_vis_to_badge.get(record.vis)
            return DocumentId(badge, record.vis) if badge else None
        return None

    def isolated_partial(record: CsvRecord) -> DocumentId | None:
        """Keep a lone identifier separate when no pair evidence exists at all."""

        if not allow_isolated_partial_identifiers or not record.identity_usable:
            return None
        if record.badge and not record.vis and record.badge not in badge_to_vis:
            return DocumentId(record.badge, "")
        if record.vis and not record.badge and record.vis not in vis_to_badge:
            return DocumentId("", record.vis)
        return None

    resolved_by_line: dict[int, DocumentId] = {}
    for record in analysis.records:
        document = resolve(record)
        if document is None and record.is_found:
            document = isolated_partial(record)
            if document is not None:
                analysis.partial_identifiers.append(
                    f"{record.location}: isolated as {document.filename} because "
                    "no safe Badge/VIS mapping exists"
                )
        if document is not None:
            resolved_by_line[record.line_number] = document
            analysis.resolved_records.append((record, document))
            if record.is_found:
                analysis.target_documents.add(document)
        elif record.is_found:
            available = []
            if record.badge:
                available.append(f"Badge={record.badge}")
            if record.vis:
                available.append(f"VIS={record.vis}")
            identifiers = ", ".join(available) or "no valid identifier"
            analysis.unresolved.append(
                f"{record.location}: FOUND row could not be resolved ({identifiers})"
            )

    explicit_pages: defaultdict[DocumentId, set[tuple[str, int]]] = defaultdict(set)
    for record, document in analysis.resolved_records:
        # A FOUND row establishes that this logical document is a target. Once
        # established, every decoded CSV occurrence of that same stable pair is
        # an explicitly identified page, even if that individual row's marker
        # is unexpectedly blank.
        if document not in analysis.target_documents:
            continue
        if record.lot is None or record.page_number is None:
            analysis.unresolved.append(
                f"{record.location}: resolved as {document.badge}_{document.vis} "
                "but has no usable Lot/page"
            )
            continue
        reference = (record.lot, record.page_number)
        if reference in explicit_pages[document]:
            analysis.duplicate_page_references_removed += 1
        else:
            explicit_pages[document].add(reference)
    analysis.explicit_pages = dict(explicit_pages)


def analyze_results_csv(
    path: Path, *, allow_isolated_partial_identifiers: bool = False
) -> CsvAnalysis:
    analysis = read_csv_records(path)
    resolve_document_identities(
        analysis,
        allow_isolated_partial_identifiers=allow_isolated_partial_identifiers,
    )
    return analysis


def documents_by_lot(
    explicit_pages: dict[DocumentId, set[tuple[str, int]]],
) -> dict[str, dict[DocumentId, set[int]]]:
    result: defaultdict[str, defaultdict[DocumentId, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for document, references in explicit_pages.items():
        for lot, page_number in references:
            result[lot][document].add(page_number)
    return {lot: dict(documents) for lot, documents in result.items()}


IdentifierToken = tuple[str, str]


def detected_identifiers_by_lot(
    records: Iterable[CsvRecord],
) -> dict[str, dict[int, frozenset[IdentifierToken]]]:
    """Index every valid decoded identifier, including non-target documents.

    Range reconstruction uses these detections only as local-document boundaries.
    Blank pages create no tokens and therefore never break a local range.
    """

    mutable: defaultdict[str, defaultdict[int, set[IdentifierToken]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for record in records:
        if record.lot is None or record.page_number is None:
            continue
        if record.badge:
            mutable[record.lot][record.page_number].add(("badge", record.badge))
        if record.vis:
            mutable[record.lot][record.page_number].add(("vis", record.vis))
    return {
        lot: {page: frozenset(tokens) for page, tokens in pages.items()}
        for lot, pages in mutable.items()
    }


def _contains_foreign_identifier(
    tokens: frozenset[IdentifierToken], document: DocumentId
) -> bool:
    for kind, value in tokens:
        expected = document.badge if kind == "badge" else document.vis
        if not expected or value != expected:
            return True
    return False


def build_local_ranges(
    document: DocumentId,
    explicit_pages: Iterable[int],
    detected_identifiers: dict[int, frozenset[IdentifierToken]],
) -> list[tuple[int, int]]:
    """Split repeated occurrences when another document appears between them.

    Each returned range starts at an explicit page and ends at the last explicit
    page before a foreign Badge/VIS detection. Unidentified pages between those
    endpoints are retained. This prevents a repeated identifier from producing
    one huge min-to-max range across unrelated documents in the same Lot.
    """

    pages = sorted(set(explicit_pages))
    if not pages:
        return []

    detected_pages = sorted(detected_identifiers)
    ranges: list[tuple[int, int]] = []
    start = previous = pages[0]
    for current in pages[1:]:
        left = bisect.bisect_right(detected_pages, previous)
        right = bisect.bisect_left(detected_pages, current)
        has_foreign_boundary = any(
            _contains_foreign_identifier(
                detected_identifiers[page_number], document
            )
            for page_number in detected_pages[left:right]
        )
        if has_foreign_boundary:
            ranges.append((start, previous))
            start = current
        previous = current
    ranges.append((start, previous))
    return ranges


def discover_lot_pdf(root: Path, lot: str) -> tuple[Path | None, str | None]:
    directory = root / lot
    if not directory.exists():
        return None, f"Lot {lot}: directory is missing"
    if not directory.is_dir():
        return None, f"Lot {lot}: expected a directory, found another filesystem object"
    try:
        pdfs = sorted(
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.casefold() == ".pdf"
            # macOS can create AppleDouble sidecars such as ._document.pdf on
            # removable/non-APFS volumes. They are metadata, not source PDFs.
            and not path.name.startswith(".")
        )
    except OSError as exc:
        return None, f"Lot {lot}: cannot list directory: {exc}"
    if not pdfs:
        return None, f"Lot {lot}: no PDF found"
    if len(pdfs) > 1:
        return (
            None,
            f"Lot {lot}: multiple PDFs found ({', '.join(p.name for p in pdfs)})",
        )
    return pdfs[0], None


def _document_label(document: DocumentId) -> str:
    return f"{document.badge}_{document.vis}"


def _close_reader(reader: Any) -> None:
    stream = getattr(reader, "stream", None)
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def extract_lot_pages(
    *,
    lot: str,
    pdf_path: Path,
    document_pages: dict[DocumentId, set[int]],
    detected_identifiers: dict[int, frozenset[IdentifierToken]],
    strategy: Strategy,
    writers: dict[DocumentId, Any],
    writer_page_counts: dict[DocumentId, int],
    writer_explicit_counts: dict[DocumentId, int],
    writer_intermediate_counts: dict[DocumentId, int],
    stats: ReconstructionStats,
    logger: logging.Logger,
) -> None:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ConfigurationError(
            "pypdf is required; install dependencies with "
            "'python -m pip install -r requirements.txt'"
        ) from exc

    reader = None
    try:
        reader = PdfReader(str(pdf_path), strict=False)
        page_count = len(reader.pages)
        stats.lots_accessed += 1
        logger.info("Lot %s: opened %s (%d pages)", lot, pdf_path.name, page_count)
    except Exception as exc:
        message = f"Lot {lot}: cannot open {pdf_path.name}: {type(exc).__name__}: {exc}"
        stats.add_issue("PDF extraction errors", message)
        logger.exception(message)
        stats.failed_documents.update(document_pages)
        _close_reader(reader)
        return

    page_plan: defaultdict[int, dict[DocumentId, bool]] = defaultdict(dict)
    for document, explicit in document_pages.items():
        if document in stats.failed_documents:
            continue
        invalid = sorted(page for page in explicit if page < 1 or page > page_count)
        if invalid:
            for page_number in invalid:
                message = (
                    f"Lot {lot}: page {page_number} for {_document_label(document)} "
                    f"is outside PDF bounds 1-{page_count}"
                )
                stats.add_issue("Pages outside PDF bounds", message)
                logger.error(message)
            stats.failed_documents.add(document)
            continue

        if strategy is Strategy.RANGES:
            local_ranges = build_local_ranges(
                document, explicit, detected_identifiers
            )
            stats.local_ranges_reconstructed += len(local_ranges)
            logger.info(
                "Lot %s | %s local ranges: %s",
                lot,
                _document_label(document),
                ", ".join(
                    f"{start_page}-{end_page}"
                    for start_page, end_page in local_ranges
                ),
            )
            selected: Iterable[int] = (
                page_number
                for start_page, end_page in local_ranges
                for page_number in range(start_page, end_page + 1)
            )
        else:
            selected = sorted(explicit)
        for page_number in selected:
            page_plan[page_number][document] = page_number in explicit

    try:
        for page_number in sorted(page_plan):
            consumers = page_plan[page_number]
            if all(document in stats.failed_documents for document in consumers):
                continue
            try:
                source_page = reader.pages[page_number - 1]
                stats.unique_physical_pages_read += 1
            except Exception as exc:
                message = (
                    f"Lot {lot}: failed to read page {page_number}: "
                    f"{type(exc).__name__}: {exc}"
                )
                stats.add_issue("PDF extraction errors", message)
                logger.exception(message)
                stats.failed_documents.update(consumers)
                continue

            for document, is_explicit in sorted(consumers.items()):
                if document in stats.failed_documents:
                    continue
                try:
                    writers[document].add_page(source_page)
                    writer_page_counts[document] += 1
                    stats.pages_copied_during_extraction += 1
                    if is_explicit:
                        writer_explicit_counts[document] += 1
                    else:
                        writer_intermediate_counts[document] += 1
                except Exception as exc:
                    message = (
                        f"Lot {lot}: failed to copy page {page_number} into "
                        f"{_document_label(document)}: {type(exc).__name__}: {exc}"
                    )
                    stats.add_issue("PDF extraction errors", message)
                    logger.exception(message)
                    stats.failed_documents.add(document)
    finally:
        _close_reader(reader)


def write_output_pdfs(
    *,
    staging_directory: Path,
    documents: Iterable[DocumentId],
    writers: dict[DocumentId, Any],
    writer_page_counts: dict[DocumentId, int],
    writer_explicit_counts: dict[DocumentId, int],
    writer_intermediate_counts: dict[DocumentId, int],
    stats: ReconstructionStats,
    logger: logging.Logger,
) -> None:
    from pypdf import PdfReader

    for document in sorted(documents):
        label = _document_label(document)
        if document in stats.failed_documents:
            message = (
                f"{label}: output skipped because one or more required pages failed"
            )
            stats.add_issue("Output writing failures", message)
            logger.error(message)
            continue
        expected_pages = writer_page_counts.get(document, 0)
        if expected_pages == 0:
            message = f"{label}: output skipped because no source pages were available"
            stats.add_issue("Output writing failures", message)
            logger.error(message)
            continue

        output_path = staging_directory / document.filename
        temporary_path = staging_directory / f".{document.filename}.tmp"
        try:
            with temporary_path.open("wb") as handle:
                writers[document].write(handle)
                handle.flush()
                os.fsync(handle.fileno())
            verification = PdfReader(str(temporary_path), strict=False)
            actual_pages = len(verification.pages)
            _close_reader(verification)
            if actual_pages != expected_pages:
                raise RuntimeError(
                    f"verification found {actual_pages} pages; "
                    f"expected {expected_pages}"
                )
            os.replace(temporary_path, output_path)
            stats.output_pdfs_created += 1
            stats.source_pages_extracted += actual_pages
            stats.explicit_pages_added += writer_explicit_counts[document]
            stats.intermediate_pages_added += writer_intermediate_counts[document]
            logger.info("Created %s with %d pages", document.filename, actual_pages)
        except Exception as exc:
            temporary_path.unlink(missing_ok=True)
            message = f"{label}: output write failed: {type(exc).__name__}: {exc}"
            stats.add_issue("Output writing failures", message)
            logger.exception(message)
            stats.failed_documents.add(document)


def configure_logger(path: Path) -> logging.Logger:
    logger = logging.getLogger(f"reconstruction.{path.parent.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def remove_appledouble_sidecars(directory: Path) -> None:
    """Remove macOS metadata sidecars from the generated output directory."""

    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for path in entries:
        if path.is_file() and path.name.startswith("._"):
            try:
                path.unlink()
            except OSError:
                pass


def _unique_backup_path(output_directory: Path) -> Path | None:
    if not output_directory.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_directory.with_name(f"{output_directory.name}_backup_{timestamp}")
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = output_directory.with_name(f"{base.name}_{counter}")
        counter += 1
    return candidate


def publish_staging_directory(
    staging_directory: Path, output_directory: Path, backup_path: Path | None
) -> None:
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


def format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


ISSUE_CATEGORIES = (
    "Missing Lot directories",
    "Missing PDFs",
    "Multiple PDFs",
    "Pages outside PDF bounds",
    "PDF extraction errors",
    "Output writing failures",
)


def classify_lot_discovery_issue(message: str) -> str:
    if "directory is missing" in message:
        return "Missing Lot directories"
    if "no PDF found" in message:
        return "Missing PDFs"
    if "multiple PDFs found" in message:
        return "Multiple PDFs"
    return "PDF extraction errors"


def write_report(
    path: Path,
    *,
    strategy: Strategy,
    started_at: datetime,
    finished_at: datetime,
    elapsed: float,
    analysis: CsvAnalysis,
    stats: ReconstructionStats,
    backup_path: Path | None,
) -> None:
    explicit_count = sum(len(pages) for pages in analysis.explicit_pages.values())
    blocking_conflicts = set(analysis.blocking_conflicts)
    non_target_conflicts = [
        message for message in analysis.conflicts if message not in blocking_conflicts
    ]
    lines = [
        f"{strategy.report_title} report",
        "=" * (len(strategy.report_title) + 7),
        f"Start time: {started_at.isoformat(timespec='seconds')}",
        f"Finish time: {finished_at.isoformat(timespec='seconds')}",
        f"Elapsed time: {format_duration(elapsed)}",
        "",
        f"Total CSV rows read: {analysis.total_rows:,}",
        f"CSV rows marked FOUND: {analysis.found_rows:,}",
        f"Total found logical identifiers: {len(analysis.target_documents):,}",
        f"Total output PDFs created: {stats.output_pdfs_created:,}",
        "Total source pages extracted into output PDFs: "
        f"{stats.source_pages_extracted:,}",
        f"Pages copied during extraction before output validation: "
        f"{stats.pages_copied_during_extraction:,}",
        f"Unique physical source pages read: {stats.unique_physical_pages_read:,}",
        f"Lots referenced: {stats.lots_referenced:,}",
        f"Lots accessed: {stats.lots_accessed:,}",
        f"Identifiers spanning multiple Lots: {stats.identifiers_spanning_lots:,}",
        "Duplicate page references removed: "
        f"{analysis.duplicate_page_references_removed:,}",
        f"Unresolved Badge/VIS mappings: {len(analysis.unresolved):,}",
        f"Mapping conflicts observed in all CSV rows: {len(analysis.conflicts):,}",
        f"Conflicts affecting FOUND targets: {len(analysis.blocking_conflicts):,}",
        f"Non-target conflicts quarantined: {len(non_target_conflicts):,}",
        f"Isolated partial identifiers: {len(analysis.partial_identifiers):,}",
        "Identifiers skipped after extraction/output errors: "
        f"{len(stats.failed_documents):,}",
        "",
        f"Explicitly identified document/page references: {explicit_count:,}",
        f"Explicitly identified pages included: {stats.explicit_pages_added:,}",
    ]
    if strategy is Strategy.RANGES:
        lines.append(
            "Local document ranges reconstructed: "
            f"{stats.local_ranges_reconstructed:,}"
        )
        lines.append(
            "Intermediate/blank pages added by range reconstruction: "
            f"{stats.intermediate_pages_added:,}"
        )
    if backup_path is not None:
        lines.extend(("", f"Previous output preserved at: {backup_path.name}"))

    lines.extend(("", "Issue counts"))
    lines.append("------------")
    lines.append(f"CSV/input warnings: {len(analysis.warnings):,}")
    lines.append(f"Unresolved mappings: {len(analysis.unresolved):,}")
    lines.append(
        f"Conflicts affecting FOUND targets: {len(analysis.blocking_conflicts):,}"
    )
    lines.append(f"Non-target conflicts quarantined: {len(non_target_conflicts):,}")
    lines.append(f"Isolated partial identifiers: {len(analysis.partial_identifiers):,}")
    for category in ISSUE_CATEGORIES:
        lines.append(f"{category}: {len(stats.issues.get(category, [])):,}")

    detail_groups = [
        ("CSV/input warnings", analysis.warnings),
        ("Unresolved mappings", analysis.unresolved),
        ("Conflicts affecting FOUND targets", analysis.blocking_conflicts),
        ("Non-target conflicts quarantined", non_target_conflicts),
        ("Isolated partial identifiers", analysis.partial_identifiers),
    ]
    detail_groups.extend(
        (category, stats.issues.get(category, [])) for category in ISSUE_CATEGORIES
    )
    for heading, messages in detail_groups:
        if not messages:
            continue
        lines.extend(("", heading, "-" * len(heading)))
        lines.extend(f"- {message}" for message in messages)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(
    strategy: Strategy, argv: Sequence[str] | None = None
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            f"{strategy.report_title}: reconstruct found Badge/VIS documents from "
            "results.csv and original Lot PDFs without rasterization."
        )
    )
    parser.add_argument(
        "-d", "--directory", required=True, type=Path, help="Batch root directory"
    )
    return parser.parse_args(argv)


def validate_root(directory: Path) -> tuple[Path, Path]:
    root = directory.expanduser().resolve()
    if not root.exists():
        raise ConfigurationError(f"Directory does not exist: {root}")
    if not root.is_dir():
        raise ConfigurationError(f"Not a directory: {root}")
    csv_path = root / CSV_FILENAME
    if not csv_path.is_file():
        raise ConfigurationError(f"Missing required CSV: {csv_path}")
    try:
        from pypdf import PdfReader, PdfWriter  # noqa: F401
    except ImportError as exc:
        raise ConfigurationError(
            "pypdf is required; install dependencies with "
            "'python -m pip install -r requirements.txt'"
        ) from exc
    return root, csv_path


def run_reconstruction(directory: Path, strategy: Strategy) -> tuple[int, Path]:
    root, csv_path = validate_root(directory)
    started_at = datetime.now().astimezone()
    started_clock = time.perf_counter()

    analysis = analyze_results_csv(
        csv_path,
        # A FOUND record that has a valid lone Badge or VIS and no mapping
        # evidence can safely remain isolated under badge_.pdf or _vis.pdf.
        # This is safe for both strategies: range mode computes its range only
        # from occurrences of that same isolated identifier.
        allow_isolated_partial_identifiers=True,
    )
    output_directory = root / strategy.output_directory
    backup_path = _unique_backup_path(output_directory)
    staging_directory = Path(
        tempfile.mkdtemp(prefix=f".{strategy.output_directory}.", dir=root)
    )
    logger = configure_logger(staging_directory / LOG_FILENAME)
    logger.info("Starting %s from %s", strategy.value, csv_path)
    for warning in analysis.warnings:
        logger.warning(warning)
    for message in analysis.unresolved:
        logger.error(message)
    blocking_conflicts = set(analysis.blocking_conflicts)
    for message in analysis.conflicts:
        if message in blocking_conflicts:
            logger.error(message)
        else:
            logger.warning("Non-target mapping conflict quarantined: %s", message)
    for message in analysis.partial_identifiers:
        logger.warning(message)

    try:
        from pypdf import PdfWriter

        writers = {document: PdfWriter() for document in analysis.target_documents}
        writer_page_counts = {document: 0 for document in analysis.target_documents}
        writer_explicit_counts = {document: 0 for document in analysis.target_documents}
        writer_intermediate_counts = {
            document: 0 for document in analysis.target_documents
        }
        stats = ReconstructionStats()
        by_lot = documents_by_lot(analysis.explicit_pages)
        identifiers_by_lot = detected_identifiers_by_lot(analysis.records)
        stats.lots_referenced = len(by_lot)
        stats.identifiers_spanning_lots = sum(
            1
            for references in analysis.explicit_pages.values()
            if len({lot for lot, _page in references}) > 1
        )

        for lot in sorted(by_lot, key=natural_sort_key):
            pdf_path, discovery_error = discover_lot_pdf(root, lot)
            if discovery_error:
                category = classify_lot_discovery_issue(discovery_error)
                stats.add_issue(category, discovery_error)
                stats.failed_documents.update(by_lot[lot])
                logger.error(discovery_error)
                continue
            assert pdf_path is not None
            extract_lot_pages(
                lot=lot,
                pdf_path=pdf_path,
                document_pages=by_lot[lot],
                detected_identifiers=identifiers_by_lot.get(lot, {}),
                strategy=strategy,
                writers=writers,
                writer_page_counts=writer_page_counts,
                writer_explicit_counts=writer_explicit_counts,
                writer_intermediate_counts=writer_intermediate_counts,
                stats=stats,
                logger=logger,
            )

        write_output_pdfs(
            staging_directory=staging_directory,
            documents=analysis.target_documents,
            writers=writers,
            writer_page_counts=writer_page_counts,
            writer_explicit_counts=writer_explicit_counts,
            writer_intermediate_counts=writer_intermediate_counts,
            stats=stats,
            logger=logger,
        )

        finished_at = datetime.now().astimezone()
        elapsed = time.perf_counter() - started_clock
        write_report(
            staging_directory / REPORT_FILENAME,
            strategy=strategy,
            started_at=started_at,
            finished_at=finished_at,
            elapsed=elapsed,
            analysis=analysis,
            stats=stats,
            backup_path=backup_path,
        )
        logger.info(
            "Finished: outputs=%d targets=%d pages=%d elapsed=%s",
            stats.output_pdfs_created,
            len(analysis.target_documents),
            stats.source_pages_extracted,
            format_duration(elapsed),
        )
        close_logger(logger)
        remove_appledouble_sidecars(staging_directory)
        publish_staging_directory(staging_directory, output_directory, backup_path)

        # A conflict wholly outside the FOUND target set is still reported for
        # diagnosis, but it must not make an otherwise successful run fail.
        partial_failure = bool(
            analysis.unresolved
            or analysis.blocking_conflicts
            or stats.failed_documents
            or any(stats.issues.values())
        )
        return (1 if partial_failure else 0), output_directory
    except Exception:
        logger.exception("Fatal reconstruction failure")
        close_logger(logger)
        # Keep the staging directory and its log for diagnosis. It is hidden and
        # never replaces a previously successful output directory.
        raise


def cli_main(strategy: Strategy, argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(strategy, argv)
        exit_code, output_directory = run_reconstruction(args.directory, strategy)
        print(f"Finished {strategy.value} reconstruction: {output_directory}")
        if exit_code:
            report_path = output_directory / REPORT_FILENAME
            print(
                f"Completed with reported issues; see {report_path}",
                file=sys.stderr,
            )
        return exit_code
    except ConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "ConfigurationError",
    "CsvAnalysis",
    "DocumentId",
    "Strategy",
    "analyze_results_csv",
    "cli_main",
    "documents_by_lot",
    "extract_lot_pages",
    "natural_sort_key",
    "normalize_found",
    "run_reconstruction",
]

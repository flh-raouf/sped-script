#!/usr/bin/env python3
"""Split numeric lots and classify individual pages by VIS using the BDD."""
from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

if __package__:
    from . import script as barcode
    from . import split_pages as split
else:
    import script as barcode
    import split_pages as split


@dataclass(frozen=True)
class VehicleRecord:
    badge: str = ''
    vin: str = ''
    vis: str = ''
    seq: str = ''
    nof: str = ''


@dataclass
class DatabaseIndex:
    badge_vis: dict[str, frozenset[str]] = field(default_factory=dict)
    records_by_vis: dict[str, list[VehicleRecord]] = field(default_factory=dict)


def text_value(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def load_database(path: Path) -> DatabaseIndex:
    """Stream the consolidated workbook; never choose a contradictory pair."""
    from openpyxl import load_workbook
    mapping: dict[str, set[str]] = defaultdict(set)
    records: dict[str, list[VehicleRecord]] = defaultdict(list)
    recognized = False
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows()
            columns = None
            for number, row in enumerate(rows, 1):
                headers = [barcode.normalize_header(cell.value) for cell in row]
                badge_col = next((i for i, h in enumerate(headers) if h in barcode.BADGE_HEADERS), None)
                vis_col = next((i for i, h in enumerate(headers) if h in barcode.VIS_HEADERS), None)
                if badge_col is not None and vis_col is not None:
                    columns = badge_col, vis_col, headers
                    recognized = True
                    break
                if number >= 50:
                    break
            if columns is None:
                continue
            bi, vi, headers = columns
            seq_col = next((i for i, h in enumerate(headers) if h in {'seq', 'sem', 'sequence'}), None)
            nof_col = next((i for i, h in enumerate(headers) if h == 'nof'), None)
            vin_cols = [i for i, h in enumerate(headers) if h in barcode.VIN_HEADERS | {'id1', 'id2', 'id3'}]
            for row in rows:
                badge_cell = row[bi]
                value = badge_cell.value
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if int(value) != value:
                        badge = ''
                    else:
                        badge = str(int(value))
                    if badge and '00000' in badge_cell.number_format and len(badge) < 5:
                        badge = badge.zfill(5)
                else:
                    badge = text_value(value)
                if not re.fullmatch(r'[0-9]{1,6}', badge):
                    badge = ''
                vis = barcode.normalize_vis(row[vi].value)
                if not vis:
                    continue
                if badge:
                    mapping[badge].add(vis)
                vins = {vin for i in vin_cols if (vin := barcode.normalize_vin(row[i].value))
                        and vin[-8:] == vis}
                records[vis].append(VehicleRecord(
                    badge=badge, vin=next(iter(vins)) if len(vins) == 1 else '', vis=vis,
                    seq=text_value(row[seq_col].value) if seq_col is not None else '',
                    nof=text_value(row[nof_col].value) if nof_col is not None else '',
                ))
    finally:
        workbook.close()
    if not recognized or not mapping:
        raise barcode.ConfigurationError('BDD must contain usable BDG/Badge and VIS columns')
    return DatabaseIndex({badge: frozenset(values) for badge, values in mapping.items()}, dict(records))


def page_excel_row(result: barcode.PageResult, vis: str | None, database: DatabaseIndex) -> tuple[str, ...]:
    """Enrich an identified page without guessing absent or conflicting fields."""
    page_id = f'{result.lot}_{result.page_number}'
    if not vis:
        return (page_id, '', '', '', '', '', '')
    candidates = database.records_by_vis.get(vis, [])
    if result.badges:
        candidates = [record for record in candidates if record.badge in result.badges]
    if result.vins:
        exact = [record for record in candidates if record.vin in result.vins]
        if exact:
            candidates = exact
    def unique(values):
        values = {value for value in values if value}
        return next(iter(values)) if len(values) == 1 else ''
    badge = unique(result.badges) or unique(record.badge for record in candidates)
    vin = unique(result.vins) or unique(record.vin for record in candidates)
    seq = unique(record.seq for record in candidates)
    nof = unique(record.nof for record in candidates)
    seq_digits = ''.join(re.findall(r'[0-9]', seq))
    return (page_id, badge, vin, vis, seq, seq_digits[-9:], nof)


def write_page_workbook(path: Path, rows: list[tuple[str, ...]]) -> None:
    """Export one row per page using the application's existing Excel library."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    if len(rows) > 1_048_575:
        raise barcode.ConfigurationError('Page report exceeds the Excel worksheet row limit')
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet('Pages')
    sheet.freeze_panes = 'A2'
    sheet.sheet_view.showGridLines = False
    for index, width in enumerate((20, 14, 24, 16, 30, 18, 18), 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    header = []
    for label in ('lot_page', 'BDG', 'VIN', 'VIS', 'SEQ', 'SEQ_9', 'NOF'):
        cell = WriteOnlyCell(sheet, value=label)
        cell.font = Font(name='Arial', bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='253B53')
        header.append(cell)
    sheet.append(header)
    for row in rows:
        cells = []
        for value in row:
            cell = WriteOnlyCell(sheet, value=value or None)
            cell.data_type = 's'
            cell.number_format = '@'
            cells.append(cell)
        sheet.append(cells)
    sheet.auto_filter.ref = f'A1:G{len(rows) + 1}'
    workbook.save(path)
    workbook.close()


def resolve_vis(result: barcode.PageResult, database: dict[str, frozenset[str]]) -> tuple[str | None, str]:
    if result.error:
        return None, 'SCAN_ERROR'
    direct = {vin[-8:] for vin in result.vins}
    candidates = set(direct)
    for badge in result.badges:
        mapped = database.get(badge, frozenset())
        if len(mapped) > 1:
            return None, 'BDD_CONFLICT'
        candidates.update(mapped)
    if len(candidates) > 1:
        return None, 'IDENTIFIER_CONFLICT'
    if not candidates:
        return None, 'UNRESOLVED_BADGE' if result.badges else 'NO_BARCODE'
    return next(iter(candidates)), 'BARCODE' if direct else 'BDD_BADGE'


def scan_single_page(task: barcode.PageTask) -> barcode.PageResult:
    # Scan the split PDF itself; restore the original lot/page coordinates.
    try:
        result = barcode.process_page(replace(task, page_index=0))
    finally:
        # Release the file before the parent moves it (also required on Windows).
        barcode._close_worker_document()
    return replace(result, page_number=task.page_index + 1)


def found_rows(result: barcode.PageResult, vis: str | None, targets: barcode.TargetIndex) -> set[barcode.RowRef]:
    badges, vehicles = barcode.matched_rows_for_page(result, targets)
    if vis:
        vehicles.update(targets.vis_rows.get(vis, ()))
    return badges | vehicles


def discover_database(root: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit.expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.is_file() or path.suffix.lower() != '.xlsx':
            raise barcode.ConfigurationError(f'BDD must be an existing .xlsx file: {path}')
        return path
    candidates = sorted(path.resolve() for path in root.iterdir()
                        if path.is_file() and path.suffix.lower() == '.xlsx'
                        and path.stem.casefold().startswith('bdd')
                        and not path.stem.casefold().endswith('_found')
                        and not path.name.startswith(('~$', '.')))
    if not candidates:
        raise barcode.ConfigurationError(
            f'No BDD workbook found in {root}. Put BDD_2024_2025_2026.xlsx '
            'directly in the parent folder, or supply --database.')
    if len(candidates) > 1:
        raise barcode.ConfigurationError(
            'Multiple BDD workbooks found (' + ', '.join(path.name for path in candidates)
            + '); select one with --database.')
    return candidates[0]


def run(args: argparse.Namespace) -> int:
    from pypdf import PdfReader
    root = split.validate_root(args.directory)
    database_path = discover_database(root, args.database)
    # The search list and full BDD have separate roles.
    excel = args.excel
    if excel is None:
        candidates = sorted(path for path in root.iterdir()
                            if path.is_file() and path.suffix.lower() in {'.xlsx', '.xlsm'}
                            and not path.name.startswith(('~$', '.'))
                            and not path.stem.lower().endswith('_found')
                            and path.resolve() != database_path.resolve())
        if len(candidates) != 1:
            raise barcode.ConfigurationError('Select the search list with --excel')
        excel = candidates[0]
    target_path = barcode.discover_excel(root, excel)
    if target_path.resolve() == database_path.resolve():
        raise barcode.ConfigurationError('Select the search list with --excel, separately from --database')
    targets = barcode.load_target_index(target_path)
    database = load_database(database_path)
    barcode.validate_runtime()
    lots = split.discover_lot_directories(root)
    if not lots:
        raise barcode.ConfigurationError('No numeric Lot folders found')
    output = root / 'output'
    staging = Path(tempfile.mkdtemp(prefix='.classify_pages_', dir=root))
    logger = barcode.configure_logging(staging)
    records = []
    page_rows = []
    matches: set[barcode.RowRef] = set()
    issues = list(targets.warnings)
    classified = ocr = total = 0
    try:
        (staging / 'OCR').mkdir()
        pending = staging / '_pending'
        pending.mkdir()
        with mp.get_context('spawn').Pool(args.workers, initializer=barcode.initialize_worker) as pool:
            for lot in lots:
                pdf, error = split.discover_lot_pdf(lot)
                if error:
                    message = f'Lot {lot.name}: {error}; skipped'
                    issues.append(message)
                    logger.error(message)
                    continue
                reader = None
                tasks = []
                try:
                    reader = PdfReader(str(pdf), strict=False)
                    count = len(reader.pages)
                    if not count:
                        raise ValueError('PDF contains no pages')
                    for index, page in enumerate(reader.pages):
                        name = f'{lot.name}_{index + 1}.pdf'
                        path = pending / name
                        split.write_one_page_pdf(source_page=page, output_path=path)
                        tasks.append(barcode.PageTask(lot.name, str(path), index, count, True))
                except Exception as exc:
                    # Do not publish a partially split lot as a successful lot.
                    for path in pending.glob('*.pdf'):
                        path.unlink()
                    message = f'Lot {lot.name}: split failed: {exc}'
                    issues.append(message)
                    logger.exception(message)
                    print(message, file=sys.stderr, flush=True)
                    continue
                finally:
                    if reader is not None:
                        split.close_pdf_reader(reader)
                for result in pool.imap_unordered(scan_single_page, tasks, chunksize=1):
                    vis, status = resolve_vis(result, database.badge_vis)
                    name = f'{result.lot}_{result.page_number}.pdf'
                    destination = staging / (vis or 'OCR')
                    destination.mkdir(exist_ok=True)
                    (pending / name).rename(destination / name)
                    total += 1
                    classified += bool(vis)
                    ocr += not bool(vis)
                    page_matches = found_rows(result, vis, targets)
                    matches.update(page_matches)
                    record = barcode.csv_records_for_page(result, targets)[0]
                    decoded_vis = sorted({vin[-8:] for vin in result.vins})
                    records.append((result.lot, result.page_number, record.badge, record.vin,
                                    ';'.join(decoded_vis), vis or '', 'FOUND' if page_matches else '',
                                    status, f'{vis or "OCR"}/{name}', result.error or ''))
                    page_rows.append((result.lot, result.page_number, page_excel_row(result, vis, database)))
                    if status.endswith('CONFLICT') or result.error:
                        message = f'Lot {result.lot} page {result.page_number}: {status} {result.error or ""}'
                        issues.append(message)
                        logger.error('%s\n%s', message, result.traceback_text or '')
                    print(f'Lot {result.lot} | Page {result.page_number}/{result.pages_in_lot} | '
                          f'{status} | FOUND {len(matches)}/{len(targets.rows)}', flush=True)
        if not total:
            raise barcode.ConfigurationError('No pages processed: ' + '; '.join(issues))
        pending.rmdir()
        records.sort(key=lambda row: (barcode.lot_sort_key(row[0]), row[1]))
        page_rows.sort(key=lambda row: (barcode.lot_sort_key(row[0]), row[1]))
        write_page_workbook(staging / 'pages.xlsx', [row[2] for row in page_rows])
        def write_csv(handle):
            writer = csv.writer(handle)
            writer.writerow(['Lot', 'Page', 'Badge', 'VIN', 'Decoded VIS', 'VIS', 'Found', 'Status', 'PDF', 'Error'])
            writer.writerows(records)
        barcode.atomic_text_write(staging / 'results.csv', write_csv, newline='', encoding='utf-8-sig')
        barcode.highlight_workbook(target_path, staging / f'{target_path.stem}_found{target_path.suffix}', matches)
        lines = [f'BDD: {database_path.name}', f'Search list: {target_path.name}',
                 'Page workbook: pages.xlsx',
                 f'Lots discovered: {len(lots)}', f'Pages processed: {total}',
                 f'Pages classified by VIS: {classified}', f'Pages in OCR: {ocr}',
                 f'Found: {len(matches)}/{len(targets.rows)}', f'Issues: {len(issues)}',
                 *[f'- {issue}' for issue in issues]]
        barcode.atomic_text_write(staging / 'report.txt', lambda handle: handle.write('\n'.join(lines) + '\n'))
        split.close_logger(logger)
        split.publish_staging_directory(staging, output, split.unique_backup_path(output))
        print(f'Finished: {classified} classified, {ocr} OCR; FOUND {len(matches)}/{len(targets.rows)}; {output}')
        return 1 if issues else 0
    finally:
        split.close_logger(logger)
        if staging.exists():
            shutil.rmtree(staging)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--directory', type=Path, required=True)
    parser.add_argument('-n', '--workers', type=int, default=1)
    parser.add_argument('--database', type=Path, help='Override the BDD .xlsx discovered in the parent folder')
    parser.add_argument('--excel', type=Path, help='Search list directly inside the batch root')
    args = parser.parse_args(argv)
    if args.workers <= 0:
        parser.error('Workers must be greater than zero')
    return args


def main(argv=None):
    try:
        return run(parse_args(argv))
    except (barcode.ConfigurationError, split.ConfigurationError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Fatal error: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())

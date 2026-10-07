#!/usr/bin/env python3
"""Classify original lot pages using saved barcode CSVs, without rescanning."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

if __package__:
    from . import classify_pages as classify
    from . import script as barcode
    from . import split_pages as split
else:
    import classify_pages as classify
    import script as barcode
    import split_pages as split


def prepare(source, database):
    """Validate every report against its original PDF before producing output."""
    import pypdfium2 as pdfium
    lots, seen = [], set()
    for run in sorted(source.glob('run-*')):
        if not run.is_dir():
            continue
        with (run/'results.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if not {'Lot', 'numero page', 'bdg', 'VIN', 'VIS', 'found'}.issubset(reader.fieldnames or []):
                raise ValueError(f'Unexpected CSV headers in {run}')
            grouped = defaultdict(list)
            for row in reader:
                if not row['Lot'].isdigit() or not row['numero page'].isdigit():
                    raise ValueError(f'Invalid lot/page in {run}: {row}')
                grouped[row['Lot']].append(row)
        for lot in sorted(grouped, key=int):
            if lot in seen:
                raise ValueError(f'Lot {lot} occurs in multiple runs; filenames would collide')
            seen.add(lot)
            original, error = split.discover_lot_pdf(run/lot)
            if error:
                raise ValueError(error)
            pdf = pdfium.PdfDocument(str(original))
            try:
                total = len(pdf)
            finally:
                pdf.close()
            records = sorted(grouped[lot], key=lambda row: int(row['numero page']))
            if [int(row['numero page']) for row in records] != list(range(1, total+1)):
                raise ValueError(f'Report for lot {lot} does not cover each of its {total} pages exactly once')
            decisions = []
            for row in records:
                badge_tokens = row['bdg'].split(';') if row['bdg'] else []
                vin_tokens = row['VIN'].split(';') if row['VIN'] else []
                badges = tuple(barcode.normalize_badge(value) for value in badge_tokens)
                vins = tuple(barcode.normalize_vin(value) for value in vin_tokens)
                if any(value is None for value in (*badges, *vins)):
                    raise ValueError(f'Invalid saved identifier: {run.name}, lot {lot}, page {row["numero page"]}')
                # Historical VIS is informational only: derive it from the full VIN again.
                result = barcode.PageResult(lot, int(row['numero page']), total,
                                             badges=badges, vins=vins)
                vis, status = classify.resolve_vis(result, database.badge_vis)
                decisions.append((result, vis, status))
            lots.append((run.name, lot, original, decisions))
            print(f'Validated {run.name} | Lot {lot} | {total} pages', flush=True)
    if not lots:
        raise ValueError('No run-* reports found')
    return lots


def run(args):
    from pypdf import PdfReader
    source, root = args.directory.expanduser().resolve(), args.output.expanduser().resolve()
    database = classify.load_database(args.database)
    lots = prepare(source, database)
    planned = sum(len(item[3]) for item in lots)
    timestamp = datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')
    reports = root/'Reports'/'barcode'/timestamp
    # Refuse any existing page placement before modifying this batch.
    for _run, lot, _original, decisions in lots:
        for result, _vis, _status in decisions:
            name = f'{lot}_{result.page_number}.pdf'
            candidates = [root/folder/name for folder in ('OCR', 'Pending', 'Review')]
            if (root/'Output').exists():
                candidates.extend(folder/name for folder in (root/'Output').iterdir() if folder.is_dir())
            if any(path.exists() for path in candidates):
                raise FileExistsError(f'Already classified page {name}; existing files were preserved')
    reports.mkdir(parents=True)
    (reports/'lot_reports').mkdir()
    (root/'Output').mkdir(exist_ok=True)
    (root/'OCR').mkdir(exist_ok=True)
    targets = barcode.load_target_index(args.excel) if args.excel else None
    matches = set()
    totals = Counter()
    started = time.perf_counter()
    classify.save_csv(reports/'results.csv', classify.CSV_HEADERS, [])
    classify.save_csv(reports/'pages.csv', classify.PAGE_HEADERS, [])
    classify.save_csv(reports/'lots.csv', ('Run', 'Lot', 'Pages', 'VIS', 'OCR', 'Seconds'), [])

    def log(text):
        message = f'{datetime.now().astimezone().isoformat()} | {text}'
        print(message, flush=True)
        with (reports/'processing.log').open('a', encoding='utf-8') as handle:
            handle.write(message+'\n')

    def summary(status):
        payload = {'status': status, 'source': str(source), 'database': str(args.database),
                   'reports': str(reports), 'pages_planned': planned, **dict(totals),
                   'seconds': time.perf_counter()-started}
        barcode.atomic_text_write(reports/'report.json', lambda handle: json.dump(payload, handle, indent=2))

    log(f'Batch START | saved reports only | {len(lots)} lots | {planned} pages')
    try:
        for run_name, lot, original, decisions in lots:
            lot_start = time.perf_counter()
            log(f'Lot {lot} START | {run_name} | {len(decisions)} pages')
            work = Path(tempfile.mkdtemp(prefix=f'.lot_{lot}_', dir=reports))
            pdf = PdfReader(original.open('rb'))
            records, rows, found, counts = [], [], set(), Counter()
            try:
                for result, vis, status in decisions:
                    name = f'{lot}_{result.page_number}.pdf'
                    folder = work/(vis or 'OCR')
                    folder.mkdir(exist_ok=True)
                    split.write_one_page_pdf(source_page=pdf.pages[result.page_number-1], output_path=folder/name)
                    destination = (root/'Output'/vis if vis else root/'OCR')/name
                    page_matches = classify.found_rows(result, vis, targets) if targets else set()
                    found.update(page_matches)
                    records.append((lot, result.page_number, ';'.join(result.badges), ';'.join(result.vins),
                                    ';'.join(vin[-8:] for vin in result.vins), vis or '',
                                    'FOUND' if page_matches else '', status, str(destination), ''))
                    rows.append(classify.page_excel_row(result, vis, database))
                    counts['classified' if vis else 'ocr'] += 1
                    counts[status] += 1
                lot_reports = work/'_reports'
                lot_reports.mkdir()
                classify.save_csv(lot_reports/'results.csv', classify.CSV_HEADERS, records)
                classify.save_csv(lot_reports/'pages.csv', classify.PAGE_HEADERS, rows)
                classify.write_page_workbook(lot_reports/'pages.xlsx', rows)
                provenance = {'run': run_name, 'original': str(original),
                              'csv': str(source/run_name/'results.csv')}
                barcode.atomic_text_write(lot_reports/'source.json',
                                          lambda handle, payload=provenance: json.dump(payload, handle))
            finally:
                split.close_pdf_reader(pdf)
            classify.publish_lot(work, reports, lot, vis_output=root/'Output', ocr_output=root/'OCR')
            totals.update(counts)
            totals['pages_saved'] += len(rows)
            totals['lots_saved'] += 1
            matches.update(found)
            classify.append_lot_csv(reports/'results.csv', records)
            classify.append_lot_csv(reports/'pages.csv', rows)
            seconds = time.perf_counter()-lot_start
            classify.append_lot_csv(reports/'lots.csv', [(run_name, lot, len(rows), counts['classified'], counts['ocr'], f'{seconds:.3f}')])
            summary('RUNNING')
            log(f'Lot {lot} SAVED | VIS={counts["classified"]} | OCR={counts["ocr"]} | {seconds:.3f}s | total={totals["pages_saved"]}/{planned}')
        with (reports/'pages.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.reader(handle)
            next(reader)
            classify.write_page_workbook(reports/'pages.xlsx', (tuple(row) for row in reader))
        if targets:
            barcode.highlight_workbook(args.excel, reports/f'{args.excel.stem}_found.xlsx', matches)
        summary('COMPLETED')
        log(f'Batch FINISHED | {dict(totals)} | duration={time.perf_counter()-started:.3f}s | {reports}')
    except BaseException:
        summary('FAILED_OR_INTERRUPTED')
        log('Batch stopped; previously published lots and unfinished private files retained')
        raise
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--directory', type=Path, required=True)
    parser.add_argument('-o', '--output', type=Path, required=True, help='Unified parent root')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--excel', type=Path, help='Optional original search list')
    try:
        return run(parser.parse_args())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 -- CLI error reporting.
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

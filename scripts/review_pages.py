#!/usr/bin/env python3
"""Classify Pending pages using cached table OCR and conservative verso inheritance."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__:
    from . import ocr_pages as ocr
else:
    import ocr_pages as ocr

classify, barcode, split = ocr.classify, ocr.barcode, ocr.split
HEADERS = (*classify.PAGE_HEADERS, 'Method', 'Predecessor', 'Ink_percent', 'Cache',
           'Status', 'Seconds', 'PDF', 'Error')


def page_key(name):
    match = re.fullmatch(r'([0-9]+)_([0-9]+)', name)
    return (match[1], int(match[2])) if match else None


def rotated(line, turns):
    x0, y0, x1, y1 = line.box
    corners = [(x0, y0), (x0, y1), (x1, y0), (x1, y1)]
    for _ in range(turns):
        corners = [(y, -x) for x, y in corners]
    xs, ys = zip(*corners)
    return ocr.TextLine(line.text, line.score, (min(xs), min(ys), max(xs), max(ys)))


def table_badge(lines, lookup, min_score=.9):
    """Recognize the parts checklist layout, never a bare badge in an arbitrary table.

    Require >=3 aligned ten-digit part references, paired OK/NOK cells to their
    right, a ten-character reference above them and exactly one badge above it.
    Try all four orientations using cached boxes; no new rotated OCR is needed.
    """
    usable = [line for line in lines if line.score >= min_score]
    found = set()
    recognized = False
    for turns in range(4):
        oriented = [rotated(line, turns) for line in usable]
        parts = [line for line in oriented if re.fullmatch(r'[0-9]{10}', line.text.strip())
                 and line.box[2] - line.box[0] > line.box[3] - line.box[1]]
        statuses = [line for line in oriented if ocr.folded(line.text).strip() in {'OK', 'NOK'}]
        references = [line for line in oriented if re.fullmatch(r'[0-9]{8}[A-Z][0-9]',
                                                              ocr.folded(line.text).strip())]
        badges = [line for line in oriented if re.fullmatch(r'[0-9]{5,6}', line.text.strip())]
        for anchor in parts:
            height = max(anchor.box[3] - anchor.box[1], 1)
            aligned = [part for part in parts if abs(part.box[0] - anchor.box[0]) <= height
                       and any(status.box[0] > part.box[2]
                               and abs((status.box[1] + status.box[3]) / 2
                                       - (part.box[1] + part.box[3]) / 2) <= height
                               for status in statuses)]
            if len(aligned) < 3:
                continue
            top = min(part.box[1] for part in aligned)
            for reference in references:
                if not (0 < top - reference.box[3] < 6 * height):
                    continue
                candidates = [badge for badge in badges
                              if 0 < reference.box[1] - badge.box[3] < 6 * height
                              and abs((reference.box[0] + reference.box[2]) / 2
                                      - (badge.box[0] + badge.box[2]) / 2) < 2 * height]
                if candidates:
                    recognized = True
                    found.update(line.text.strip() for line in candidates)
    if not recognized:
        return ocr.Decision(None, 'NOT_PARTS_TABLE')
    if len(found) != 1:
        return ocr.Decision(None, 'AMBIGUOUS_TABLE_BADGE')
    badge = next(iter(found))
    choices = lookup.badges.get(badge, frozenset())
    if len(choices) != 1:
        return ocr.Decision(None, 'UNKNOWN_TABLE_BADGE' if not choices else 'AMBIGUOUS_TABLE_BADGE')
    vis = next(iter(choices))
    general = ocr.resolve_text(lines, lookup)
    if general.reason == 'IDENTIFIER_CONFLICT' or (general.vis and general.vis != vis):
        return ocr.Decision(None, 'IDENTIFIER_CONFLICT')
    return ocr.Decision(vis, 'TABLE_BADGE', badges=(badge,))


def pixel_ink_percent(pixels, threshold=128, crop_percent=2):
    """Measure the interior only; exclude the same percentage from each edge."""
    import numpy as np
    if not 0 <= crop_percent < 25:
        raise ValueError('Crop percent must be between 0 and 25 (exclusive)')
    height, width = pixels.shape
    margin_x = int(width * crop_percent / 100)
    margin_y = int(height * crop_percent / 100)
    interior = pixels[margin_y:height-margin_y, margin_x:width-margin_x]
    if not interior.size:
        raise ValueError('Cannot measure an empty page image')
    return float(np.mean(interior < threshold) * 100)


def ink_percent(path, threshold=128, crop_percent=2):
    import numpy as np
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(path))
    page = bitmap = image = gray = None
    try:
        if len(document) != 1:
            raise ValueError('Expected a single-page PDF')
        page = document[0]
        bitmap = page.render(scale=100 / 72)
        image = bitmap.to_pil()
        gray = image.convert('L')
        return pixel_ink_percent(np.asarray(gray), threshold, crop_percent)
    finally:
        for resource in (gray, image, bitmap, page, document):
            if resource is not None:
                resource.close()


def contradicts(lines, vis, lookup, min_score):
    # Even a weak, known badge on a sparse back must agree with its predecessor.
    for line in lines:
        if line.score < min_score:
            continue
        text = ocr.folded(line.text)
        for match in ocr.VIS_PATTERN.finditer(text):
            value = match.group()
            if value in lookup.database.records_by_vis and value != vis:
                return True
        for match in ocr.VIN_PATTERN.finditer(text):
            if match.group()[-8:] != vis:
                return True
        for pattern, index in ((ocr.BADGE_PATTERN, lookup.badges),
                               (ocr.VIN_PATTERN, lookup.vins)):
            for match in pattern.finditer(text):
                choices = index.get(match.group())
                if choices and vis not in choices:
                    return True
        for match in ocr.SEQ_PATTERN.finditer(text):
            choices = lookup.sequences.get(re.sub(r'\s', '', match.group()))
            if choices and vis not in choices:
                return True
    return ocr.resolve_text(lines, lookup, min_score).reason == 'IDENTIFIER_CONFLICT'


def read_metadata(output, reports=None):
    paths = [output/'pages.csv']
    if reports is not None:
        paths.extend(sorted((reports/'barcode').glob('*/lot_reports/*/pages.csv')))
        for stage in ('ocr', 'review'):
            paths.extend(sorted((reports/stage).glob('*/results.csv')))
    metadata = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                if row.get('VIS') and row.get('Status') not in {'ERROR', 'WOULD_CLASSIFY'}:
                    metadata[row['lot_page']] = tuple(row.get(header, '') for header in classify.PAGE_HEADERS)
    return metadata


def independent_pages(output, metadata, needed_keys=None, provenance=None):
    # Durable provenance includes transfer intents, so an interruption between a
    # move and the report append cannot accidentally enable inheritance chaining.
    inherited = set()
    table_metadata = {}
    journal = provenance or output / 'review_assignments.jsonl'
    if journal.exists():
        with journal.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                    if item['method'] == 'PREVIOUS_PAGE':
                        inherited.add(item['name'])
                    elif item['method'] == 'TABLE_BADGE' and len(item.get('row', [])) == 7:
                        table_metadata[item['name']] = item
                except (ValueError, KeyError, TypeError):
                    # Fail closed on a damaged provenance file.
                    raise ValueError(f'Invalid Review provenance in {journal}')
    pages = {}
    for folder in output.iterdir():
        if not folder.is_dir() or not re.fullmatch(ocr.VIS_PATTERN, folder.name):
            continue
        for path in folder.iterdir():
            if path.suffix.lower() != '.pdf' or path.stem in inherited:
                continue
            key = page_key(path.stem)
            if key and (needed_keys is None or key in needed_keys) and path.is_file():
                row = metadata.get(path.stem)
                saved = table_metadata.get(path.stem)
                if (saved and saved['row'][3] == folder.name
                        and saved.get('sha256') == ocr.file_digest(path)):
                    row = tuple(saved['row'])
                value = (folder.name, path.stem, row)
                pages[key] = None if key in pages else value
    return pages


def decide(name, lines, density, lookup, predecessors, max_ink=.9, min_score=.9):
    table = table_badge(lines, lookup, min_score)
    if table.reason != 'NOT_PARTS_TABLE':
        return table, ''
    if density > max_ink:
        return ocr.Decision(None, 'NOT_SPARSE'), ''
    key = page_key(name)
    previous = predecessors.get((key[0], key[1] - 1)) if key else None
    if not previous:
        return ocr.Decision(None, 'NO_INDEPENDENT_PREDECESSOR'), ''
    vis, previous_name, _row = previous
    if vis not in lookup.database.records_by_vis:
        return ocr.Decision(None, 'PREDECESSOR_NOT_IN_BDD'), previous_name
    if contradicts(lines, vis, lookup, min(min_score, .8)):
        return ocr.Decision(None, 'IDENTIFIER_CONFLICT'), previous_name
    return ocr.Decision(vis, 'PREVIOUS_PAGE'), previous_name


def run(args):
    started = time.perf_counter()
    start_date = datetime.now().astimezone()
    split.validate_root(args.directory)
    paths = ocr.workflow.stage_paths(args, 'review')
    source, output = paths.source, paths.output
    if output == source:
        raise barcode.ConfigurationError('Output must differ from the Review input folder')
    files = sorted((path for path in source.iterdir() if path.is_file()
                    and path.suffix.lower() == '.pdf' and not path.name.startswith('.')),
                   key=lambda path: (page_key(path.stem) or (path.stem, 0), path.name))
    if not files:
        if not args.legacy_layout and not args.dry_run:
            paths.unresolved.mkdir(parents=True, exist_ok=True)
        print(f'Nothing left to process in {source}', flush=True)
        return 0
    output.mkdir(parents=True, exist_ok=True)
    if not args.legacy_layout and not args.dry_run:
        paths.unresolved.mkdir(parents=True, exist_ok=True)
    lookup = ocr.Lookup(classify.load_database(ocr.discover_bdd(
        source if args.legacy_layout else args.directory.expanduser().resolve(), output, args.database)))
    metadata = read_metadata(output, None if args.legacy_layout else args.directory.expanduser().resolve()/'Reports')
    needed = {(key[0], key[1]-1) for path in files if (key := page_key(path.stem))}
    predecessors = independent_pages(output, metadata, needed, paths.provenance)
    # A page still awaiting Review cannot serve as an independent front, even
    # if an older file with the same name also exists in a VIS folder.
    for path in files:
        predecessors.pop(page_key(path.stem), None)
    reports = paths.reports / start_date.strftime('%Y%m%d_%H%M%S_%f')
    reports.mkdir(parents=True)
    classify.save_csv(reports / 'results.csv', HEADERS, [])
    cache = ocr.OcrCache((args.cache or paths.cache).expanduser(),
                         ocr.ocr_settings(args.dpi, args.device))
    engine = None
    recovered = errors = attempted = hits = 0
    def log(message):
        with (reports / 'processing.log').open('a', encoding='utf-8') as handle:
            handle.write(f'{datetime.now().astimezone().isoformat()} | {message}\n')
        print(message, flush=True)
    def summary(status):
        elapsed = time.perf_counter() - started
        text = (f'Status: {status}\nStart: {start_date.isoformat()}\nInput: {source}\n'
                f'Dry run: {args.dry_run}\nProcessed: {attempted}/{len(files)}\n'
                f'Recovered: {recovered}\nRemaining: {attempted-recovered}\nErrors: {errors}\n'
                f'Cache hits: {hits}\nTotal duration: {elapsed:.3f} seconds\n')
        text += (f'Ink cutoff: {args.max_ink_percent}%\nCrop per edge: {args.crop_percent}%\n'
                 f'Black grayscale threshold: {args.black_threshold}\nDensity rendering: 100 DPI\n')
        barcode.atomic_text_write(reports / 'report.txt', lambda handle: handle.write(text))
    log(f'Folder START | {source} | {len(files)} pages | dry-run={args.dry_run}')
    try:
        summary('RUNNING')
        for path in files:
            page_started = time.perf_counter()
            decision = ocr.Decision(None, 'ERROR')
            previous = error = ''
            density = None
            hit = False
            row = (path.stem, '', '', '', '', '', '')
            destination = path
            try:
                digest = ocr.file_digest(path)
                lines = cache.get(digest)
                hit = lines is not None
                hits += hit
                if lines is None and args.cache_only:
                    decision = ocr.Decision(None, 'CACHE_MISS')
                else:
                    if lines is None:
                        if engine is None:
                            engine = ocr.PaddleEngine(args.dpi, args.device)
                        lines = engine.read(path)
                        if ocr.file_digest(path) != digest:
                            raise RuntimeError('Source changed during OCR')
                        cache.put(digest, lines)
                    density = ink_percent(path, args.black_threshold, args.crop_percent)
                    decision, previous = decide(path.stem, lines, density, lookup, predecessors,
                                                args.max_ink_percent, args.min_score)
                    row = ocr.enriched_row(path.stem, decision, lookup.database)
                    if decision.reason == 'PREVIOUS_PAGE':
                        previous_row = predecessors[(page_key(path.stem)[0], page_key(path.stem)[1]-1)][2]
                        if previous_row and previous_row[3] == decision.vis:
                            row = (path.stem, *previous_row[1:])
                    if decision.vis:
                        destination = output / decision.vis / path.name
                        if not args.dry_run:
                            with paths.provenance.open('a', encoding='utf-8') as journal:
                                journal.write(json.dumps({'name': path.stem, 'method': decision.reason,
                                                          'predecessor': previous, 'row': row,
                                                          'source': str(path), 'destination': str(destination),
                                                          'sha256': digest}) + '\n')
                                journal.flush()
                                os.fsync(journal.fileno())
                            ocr.transfer_page(path, destination, digest, args.copy)
                        recovered += 1
                        if decision.reason == 'TABLE_BADGE':
                            key = page_key(path.stem)
                            if key:
                                # Never replace an ambiguous or conflicting existing placement.
                                value = (decision.vis, path.stem, row)
                                existing = predecessors.get(key)
                                predecessors[key] = (value if key not in predecessors
                                                     or existing == value else None)
                if not decision.vis:
                    destination = paths.unresolved/path.name
                    if not args.dry_run:
                        ocr.transfer_page(path, destination, digest, args.copy)
            except Exception as exc:  # noqa: BLE001 -- Retain failed pages and continue the batch.
                errors += 1
                error = f'{type(exc).__name__}: {exc}'
                destination = path
                row = (path.stem, '', '', '', '', '', '')
            attempted += 1
            elapsed = time.perf_counter() - page_started
            classify.append_lot_csv(reports / 'results.csv', [(*row, decision.reason, previous,
                                    '' if density is None else f'{density:.6f}', 'HIT' if hit else 'MISS',
                                    'ERROR' if error else ('WOULD_CLASSIFY' if args.dry_run and decision.vis
                                                         else 'CLASSIFIED' if decision.vis else 'REVIEW'),
                                    f'{elapsed:.3f}', str(destination), error)])
            log(f'{attempted}/{len(files)} | {path.name} | {error or decision.reason} | '
                f'cache={"HIT" if hit else "MISS"} | duration={elapsed:.3f}s')
            summary('RUNNING')
        with (reports / 'results.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.reader(handle)
            next(reader)
            classify.write_page_workbook(reports / 'pages.xlsx', (tuple(row[:7]) for row in reader))
        summary('COMPLETED_WITH_ERRORS' if errors else 'COMPLETED')
        log(f'Folder FINISHED | duration={time.perf_counter()-started:.3f}s | '
            f'recovered={recovered} | errors={errors} | reports={reports}')
        return 1 if errors else 0
    except BaseException:
        summary('INTERRUPTED' if sys.exc_info()[0] is KeyboardInterrupt else 'FAILED')
        raise
    finally:
        cache.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--directory', required=True, type=Path, help='Parent root containing Pending')
    parser.add_argument('-o', '--output', type=Path, help='Override VIS destination; defaults to parent/Output')
    parser.add_argument('--legacy-layout', action='store_true', help='Use the old direct-input folder layout')
    parser.add_argument('--database', type=Path)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--cache-only', action='store_true', help='Keep cache misses in Review; never run OCR')
    parser.add_argument('--dry-run', action='store_true', help='Write preview reports; do not move PDFs')
    parser.add_argument('--copy', action='store_true')
    parser.add_argument('--dpi', type=int, default=200)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--min-score', type=float, default=.9)
    parser.add_argument('--max-ink-percent', type=float, default=.9)
    parser.add_argument('--crop-percent', type=float, default=2,
                        help='Percentage excluded from each edge for ink measurement only; 0 disables')
    parser.add_argument('--black-threshold', type=int, default=128)
    args = parser.parse_args(argv)
    if (args.dpi < 72 or not 0 <= args.min_score <= 1 or not 0 <= args.max_ink_percent <= 100
            or not 1 <= args.black_threshold <= 255 or not 0 <= args.crop_percent < 25):
        parser.error('Invalid DPI, confidence, ink percentage, grayscale threshold or crop (0 <= crop < 25)')
    return args


def main(argv=None):
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 -- Report CLI failures with a nonzero exit code.
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

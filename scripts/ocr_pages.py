#!/usr/bin/env python3
"""Recover single-page PDFs using cached PP-OCRv6 tiny OCR and BDD indexes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

if __package__:
    from . import classify_pages as classify
    from . import script as barcode
    from . import split_pages as split
    from . import workflow
else:
    import classify_pages as classify
    import script as barcode
    import split_pages as split
    import workflow

DET_MODEL = 'PP-OCRv6_tiny_det'
REC_MODEL = 'PP-OCRv6_tiny_rec'
VIN_PATTERN = re.compile(r'(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])')
VIS_PATTERN = re.compile(r'(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{8}(?![A-Z0-9])')
BADGE_PATTERN = re.compile(r'(?<![A-Z0-9])[0-9]{5,6}(?![A-Z0-9])')
SEQ_PATTERN = re.compile(r'(?<![A-Z0-9])(?:[0-9][ \t]*){9}(?![A-Z0-9])')


@dataclass(frozen=True)
class TextLine:
    text: str
    score: float
    box: tuple[float, float, float, float]


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def ocr_settings(dpi: int, device: str) -> dict:
    versions = {}
    for package in ('paddleocr', 'paddlex', 'paddlepaddle', 'pypdfium2'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'not-installed'
    return {'schema': 1, 'detection': DET_MODEL, 'recognition': REC_MODEL,
            'dpi': dpi, 'device': device, 'versions': versions,
            'orientation': False, 'unwarping': False, 'textline_orientation': False,
            'det_limit_side_len': 1600, 'det_limit_type': 'max', 'colour': 'BGR', 'crop': 'full-page'}


class OcrCache:
    """Cache only successful raw OCR, including empty results; never cache decisions."""
    def __init__(self, path: Path, settings: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('CREATE TABLE IF NOT EXISTS ocr (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.signature = json.dumps(settings, sort_keys=True, separators=(',', ':'))

    def key(self, digest: str) -> str:
        return hashlib.sha256((digest + self.signature).encode()).hexdigest()

    def get(self, digest: str) -> list[TextLine] | None:
        row = self.connection.execute('SELECT payload FROM ocr WHERE key=?', (self.key(digest),)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            if not isinstance(data, list):
                return None
            lines = []
            for item in data:
                box = tuple(float(value) for value in item['box'])
                score = float(item['score'])
                if (not isinstance(item['text'], str) or len(box) != 4 or not 0 <= score <= 1
                        or not all(math.isfinite(value) for value in box)):
                    return None
                lines.append(TextLine(item['text'], score, box))
            return lines
        except (ValueError, KeyError, TypeError):
            return None

    def put(self, digest: str, lines: list[TextLine]) -> None:
        payload = json.dumps([asdict(line) for line in lines], ensure_ascii=False)
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO ocr VALUES (?,?)', (self.key(digest), payload))

    def close(self):
        self.connection.close()


class PaddleEngine:
    def __init__(self, dpi: int, device: str):
        from paddleocr import PaddleOCR
        self.dpi = dpi
        # Paddle 3.3.x oneDNN can fail converting PIR DoubleAttribute arrays.
        self.model = PaddleOCR(text_detection_model_name=DET_MODEL, text_recognition_model_name=REC_MODEL,
                               use_doc_orientation_classify=False, use_doc_unwarping=False,
                               use_textline_orientation=False, device=device, enable_mkldnn=False,
                               text_det_limit_side_len=1600, text_det_limit_type='max')

    def read(self, path: Path) -> list[TextLine]:
        import numpy as np
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(path))
        page = bitmap = image = original = None
        try:
            if len(document) != 1:
                raise ValueError(f'Expected a single-page PDF, found {len(document)} pages')
            page = document[0]
            bitmap = page.render(scale=self.dpi / 72)
            original = bitmap.to_pil()
            image = original.convert('RGB')
            pixels = np.asarray(image)[:, :, ::-1].copy()
            lines = []
            for result in self.model.predict(pixels):
                texts, scores, boxes = result['rec_texts'], result['rec_scores'], result['rec_boxes']
                if not (len(texts) == len(scores) == len(boxes)):
                    raise ValueError('PaddleOCR returned inconsistent text/score/box counts')
                lines.extend(TextLine(str(text), float(score), tuple(float(value) for value in box))
                             for text, score, box in zip(texts, scores, boxes))
            return lines
        finally:
            if image is not None:
                image.close()
            if original is not None:
                original.close()
            if bitmap is not None:
                bitmap.close()
            if page is not None:
                page.close()
            document.close()


def document_sequence(record: classify.VehicleRecord) -> str | None:
    match = re.fullmatch(r'SEQEMON([0-9]{2})([0-9]{3})([0-9]{5})', record.seq)
    if match and record.year is not None:
        site, day, order = match.groups()
        return site[-1] + str(record.year)[-2:] + day + order[-3:]
    return record.seq if re.fullmatch(r'[0-9]{9}', record.seq) else None


def sequence_keys(record: classify.VehicleRecord) -> set[str]:
    digits = ''.join(re.findall(r'[0-9]', record.seq))
    keys = {digits[-9:]} if len(digits) >= 9 else set()
    alias = document_sequence(record)
    if alias:
        keys.add(alias)
    return keys


class Lookup:
    def __init__(self, database: classify.DatabaseIndex):
        self.database = database
        self.vins, self.sequences = defaultdict(set), defaultdict(set)
        self.badges = database.badge_vis
        for vis, records in database.records_by_vis.items():
            for record in records:
                if record.vin:
                    self.vins[record.vin].add(vis)
                for seq in sequence_keys(record):
                    self.sequences[seq].add(vis)


def folded(text: str) -> str:
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().upper()


def near(label: TextLine, value: TextLine) -> bool:
    if label is value:
        return True
    x0, y0, x1, y1 = label.box
    a0, b0, _a1, b1 = value.box
    height = max(y1 - y0, b1 - b0, 1)
    same_row = abs((y0 + y1) / 2 - (b0 + b1) / 2) <= height * .7
    right = a0 >= x0 and a0 - x1 <= height * 16
    below = b0 >= y0 and b0 - y1 <= height * 3 and abs(a0 - x0) <= height * 10
    return (same_row and right) or below


@dataclass(frozen=True)
class Decision:
    vis: str | None
    reason: str
    badges: tuple[str, ...] = ()
    vins: tuple[str, ...] = ()
    sequences: tuple[str, ...] = ()


def enriched_row(name: str, decision: Decision, database: classify.DatabaseIndex) -> tuple[str, ...]:
    match = re.fullmatch(r'([0-9]+)_([0-9]+)', name)
    lot, page = (match[1], int(match[2])) if match else (name, 1)
    result = barcode.PageResult(lot, page, 1, badges=decision.badges, vins=decision.vins)
    if decision.vis and decision.sequences:
        records = [record for record in database.records_by_vis.get(decision.vis, [])
                   if sequence_keys(record).intersection(decision.sequences)]
        database = classify.DatabaseIndex(database.badge_vis, {decision.vis: records})
    row = classify.page_excel_row(result, decision.vis, database)
    return (name, *row[1:])


def resolve_text(lines: list[TextLine], lookup: Lookup, min_score: float = .8) -> Decision:
    usable = [line for line in lines if line.score >= min_score]
    badge_labels = [line for line in usable if re.search(r'\b(?:BDG|BADGE)\b', folded(line.text))
                    and not re.search(r'OPERAT(?:EUR|OR)', folded(line.text))]
    operators = [line for line in usable if re.search(r'BADGE\s+OPERAT(?:EUR|OR)', folded(line.text))]
    seq_labels = [line for line in usable if re.search(r'\b(?:SEQUENCE|SEQ)\b', folded(line.text))]
    strong, weak = [], defaultdict(list)
    detected = {'badge': set(), 'vin': set(), 'seq': set()}
    for line in usable:
        text = folded(line.text)
        # Match complete printed VIS tokens, never substrings of VINs or SEL codes.
        for vis in VIS_PATTERN.findall(text):
            if vis in lookup.database.records_by_vis:
                strong.append({vis})
        for vin in VIN_PATTERN.findall(text):
            if vin in lookup.vins:
                strong.append(set(lookup.vins[vin]))
                detected['vin'].add(vin)
        if not any(near(label, line) for label in operators):
            for badge in BADGE_PATTERN.findall(text):
                if badge not in lookup.badges:
                    continue
                detected['badge'].add(badge)
                choices = set(lookup.badges[badge])
                if any(near(label, line) for label in badge_labels):
                    strong.append(choices)
                else:
                    weak['badge'].append(choices)
        for match in SEQ_PATTERN.finditer(text):
            seq = re.sub(r'\s', '', match.group())
            if seq not in lookup.sequences:
                continue
            detected['seq'].add(seq)
            choices = set(lookup.sequences[seq])
            if any(near(label, line) for label in seq_labels):
                strong.append(choices)
            else:
                weak['seq'].append(choices)
    if strong:
        candidates = set.intersection(*strong)
        if not candidates:
            return Decision(None, 'IDENTIFIER_CONFLICT')
        if len(candidates) > 1:
            corroboration = set().union(*(set().union(*sets) for sets in weak.values())) if weak else set()
            if corroboration:
                candidates &= corroboration
    elif len(weak) >= 2:
        candidates = set.intersection(*(set().union(*sets) for sets in weak.values()))
    else:
        return Decision(None, 'NO_RELIABLE_IDENTIFIER')
    if len(candidates) != 1:
        return Decision(None, 'AMBIGUOUS_IDENTIFIER')
    vis = next(iter(candidates))
    return Decision(vis, 'BDD_MATCH',
                    tuple(sorted(value for value in detected['badge'] if vis in lookup.badges[value])),
                    tuple(sorted(value for value in detected['vin'] if vis in lookup.vins[value])),
                    tuple(sorted(value for value in detected['seq'] if vis in lookup.sequences[value])))


def transfer_page(source: Path, destination: Path, digest: str, copy: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return
    if destination.exists():
        if file_digest(destination) != digest:
            raise FileExistsError(f'Different PDF already exists: {destination}')
    else:
        descriptor, name = tempfile.mkstemp(prefix=f'.{source.stem}_', suffix='.tmp', dir=destination.parent)
        temporary = Path(name)
        created = False
        try:
            with os.fdopen(descriptor, 'wb') as dst, source.open('rb') as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            if file_digest(temporary) != digest:
                raise RuntimeError('Source PDF changed during processing')
            # Exclusive creation also works on removable drives without hard links.
            with destination.open('xb') as dst, temporary.open('rb') as src:
                created = True
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
        except BaseException:
            if created:
                destination.unlink()
            raise
        finally:
            temporary.unlink(missing_ok=True)
    if not copy:
        if file_digest(source) != digest:
            raise RuntimeError('Source PDF changed; source was retained')
        source.unlink()


def discover_bdd(source: Path, output: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return classify.discover_database(source, explicit)
    for parent in dict.fromkeys((source, output, output.parent)):
        candidates = [path for path in parent.iterdir() if path.is_file() and path.suffix.lower() == '.xlsx'
                      and path.stem.lower().startswith('bdd') and not path.stem.lower().endswith('_found')
                      and not path.name.startswith(('.', '~$'))]
        if candidates:
            return classify.discover_database(parent)
    raise barcode.ConfigurationError('BDD not found near input/output. Supply --database /path/to/BDD.xlsx')


def run(args) -> int:
    started = time.perf_counter()
    started_at = datetime.now().astimezone()
    split.validate_root(args.directory)
    paths = workflow.stage_paths(args, 'ocr')
    source, output = paths.source, paths.output
    if output == source:
        raise barcode.ConfigurationError('Output must differ from the input folder')
    files = sorted((path for path in source.iterdir() if path.is_file() and path.suffix.lower() == '.pdf'
                    and not path.name.startswith('.')),
                   key=lambda path: (tuple(int(value) for value in re.findall(r'[0-9]+', path.stem)), path.name))
    if not files:
        if not args.legacy_layout:
            paths.unresolved.mkdir(parents=True, exist_ok=True)
        print(f'Nothing left to process in {source}', flush=True)
        return 0
    output.mkdir(parents=True, exist_ok=True)
    if not args.legacy_layout:
        paths.unresolved.mkdir(parents=True, exist_ok=True)
    database_path = discover_bdd(source if args.legacy_layout else args.directory.expanduser().resolve(),
                                 output, args.database)
    lookup = Lookup(classify.load_database(database_path))
    reports = paths.reports / started_at.strftime('%Y%m%d_%H%M%S_%f')
    reports.mkdir(parents=True)
    logger = logging.getLogger('ocr_recovery')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = logging.FileHandler(reports / 'processing.log', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    logger.addHandler(handler)
    cache = OcrCache((args.cache or paths.cache).expanduser(), ocr_settings(args.dpi, args.device))
    engine = None
    hits = recovered = review = errors = attempted = 0
    classify.save_csv(reports / 'results.csv',
                      (*classify.PAGE_HEADERS, 'SEQ_DOC', 'Status', 'Cache', 'Seconds', 'Source', 'PDF', 'Error'), [])
    logger.info('Folder START | %s | input=%s | models=%s/%s | pages=%d',
                started_at.isoformat(), source, DET_MODEL, REC_MODEL, len(files))
    def summary(status):
        elapsed = time.perf_counter() - started
        text = (f'Status: {status}\nStart: {started_at.isoformat()}\n'
                f'Updated: {datetime.now().astimezone().isoformat()}\n'
                f'Total duration: {barcode.format_duration(elapsed)} ({elapsed:.3f} seconds)\n'
                f'Pages attempted: {attempted}/{len(files)}\nRecovered: {recovered}\n'
                f'{paths.unresolved.name}: {review}\n'
                f'OCR cache hits: {hits}\nErrors: {errors}\nBDD: {database_path}\n')
        barcode.atomic_text_write(reports / 'report.txt', lambda handle: handle.write(text))
    try:
        summary('RUNNING')
        for path in files:
            page_started = time.perf_counter()
            destination = None
            enriched = (path.stem, '', '', '', '', '', '')
            decision = Decision(None, 'ERROR')
            hit = False
            error = ''
            try:
                digest = file_digest(path)
                lines = cache.get(digest)
                hit = lines is not None
                if hit:
                    hits += 1
                else:
                    if engine is None:
                        try:
                            engine = PaddleEngine(args.dpi, args.device)
                        except Exception as exc:
                            raise barcode.ConfigurationError('Cannot initialize PaddleOCR; install requirements-ocr.txt '
                                                             f'and check model download access: {exc}') from exc
                    lines = engine.read(path)
                    if file_digest(path) != digest:
                        raise RuntimeError('Source changed during OCR; no cache entry saved')
                    cache.put(digest, lines)
                decision = resolve_text(lines, lookup, args.min_score)
                enriched = enriched_row(path.stem, decision, lookup.database)
                destination = (output/decision.vis if decision.vis else paths.unresolved) / path.name
                # Durable intent precedes the move: an interruption cannot erase the page's mapping.
                with (reports / 'transfers.jsonl').open('a', encoding='utf-8') as journal:
                    journal.write(json.dumps({'source': str(path), 'destination': str(destination),
                                              'sha256': digest, 'row': enriched,
                                              'decision': asdict(decision)}, ensure_ascii=False) + '\n')
                    journal.flush()
                    os.fsync(journal.fileno())
                transfer_page(path, destination, digest, args.copy)
                recovered += bool(decision.vis)
                review += not bool(decision.vis)
            except barcode.ConfigurationError:
                raise
            except Exception as exc:
                errors += 1
                error = f'{type(exc).__name__}: {exc}'
                logger.exception('Page %s | ERROR | source retained when transfer fails', path.name)
            attempted += 1
            elapsed = time.perf_counter() - page_started
            classify.append_lot_csv(reports / 'results.csv', [(*enriched, ';'.join(decision.sequences),
                                    'ERROR' if error else decision.reason, 'HIT' if hit else 'MISS',
                                    f'{elapsed:.3f}', str(path), str(destination or ''), error)])
            logger.info('Page %s | %s | cache=%s | duration=%.3fs | destination=%s',
                        path.name, 'ERROR' if error else decision.reason, 'HIT' if hit else 'MISS', elapsed, destination)
            print(f'{attempted}/{len(files)} | {path.name} | {"ERROR" if error else decision.reason} | '
                  f'cache={"HIT" if hit else "MISS"} | {elapsed:.3f}s', flush=True)
            summary('RUNNING')
        import csv
        with (reports / 'results.csv').open(encoding='utf-8-sig', newline='') as handle:
            rows = csv.reader(handle)
            next(rows)
            classify.write_page_workbook(reports / 'pages.xlsx', (tuple(row[:7]) for row in rows))
        summary('COMPLETED_WITH_ERRORS' if errors else 'COMPLETED')
        logger.info('Folder FINISHED | duration=%.3fs | recovered=%d | review=%d | cache hits=%d',
                    time.perf_counter() - started, recovered, review, hits)
        print(f'Finished | VIS={recovered} | {paths.unresolved.name}={review} | '
              f'cache hits={hits} | reports={reports}', flush=True)
        return 1 if errors else 0
    except BaseException as exc:
        logger.error('Folder stopped: %s', exc or type(exc).__name__)
        summary('INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else 'FAILED')
        raise
    finally:
        cache.close()
        split.close_logger(logger)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--directory', required=True, type=Path, help='Parent root containing OCR')
    parser.add_argument('-o', '--output', type=Path, help='Override VIS destination; defaults to parent/Output')
    parser.add_argument('--legacy-layout', action='store_true', help='Use the old direct-input folder layout')
    parser.add_argument('--database', type=Path)
    parser.add_argument('--cache', type=Path, help='Persistent raw OCR SQLite cache')
    parser.add_argument('--copy', action='store_true', help='Keep original PDFs instead of moving them')
    parser.add_argument('--dpi', type=int, default=200)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--min-score', type=float, default=.8)
    args = parser.parse_args(argv)
    if args.dpi < 72 or not 0 <= args.min_score <= 1:
        parser.error('DPI must be >=72 and min-score between 0 and 1')
    return args


def main(argv=None):
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 -- Convert library failures to a nonzero CLI exit status.
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

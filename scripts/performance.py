"""Estimate wall time from comparable completed runs, without assuming worker scaling."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import random
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

SCHEMA = 1
HISTORY_LIMIT = 40
CACHE_SAMPLE_LIMIT = 24
DEFAULT_OPTIONS = {
    'barcode': {'legacy_layout': False},
    'ocr': {'legacy_layout': False, 'copy': False, 'dpi': 200, 'device': 'cpu', 'min_score': .8,
            'cache': None, 'output': None},
    'review': {'legacy_layout': False, 'copy': False, 'dpi': 200, 'device': 'cpu',
               'min_score': .9, 'cache_only': False, 'max_ink_percent': .9,
               'crop_percent': 2, 'black_threshold': 128, 'cache': None, 'output': None},
    'import': {},
}


def options(stage: str, args=None) -> dict:
    values = {key: getattr(args, key, value) for key, value in DEFAULT_OPTIONS[stage].items()}
    return {key: str(value.resolve()) if isinstance(value, Path) else value for key, value in values.items()}


@lru_cache(maxsize=8)
def machine(stage: str) -> str:
    """Keep timings from other hosts, runtime versions and implementations separate."""
    versions = {}
    packages = ('codarascan', 'pypdf', 'openpyxl') if stage in {'barcode', 'import'} else (
        'paddleocr', 'paddlepaddle', 'pypdfium2', 'openpyxl')
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    filename = {'barcode': 'classify_pages.py', 'ocr': 'ocr_pages.py',
                'review': 'review_pages.py', 'import': 'pipeline.py'}[stage]
    implementation = hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()
    try:
        memory = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (AttributeError, OSError, ValueError):
        memory = None
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None
    identity = [platform.node(), platform.system(), platform.machine(), platform.processor(),
                os.cpu_count(), affinity, memory, platform.python_version(), versions, implementation,
                {key: os.environ.get(key) for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS')}]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Run:
    stage: str
    machine: str
    options: dict
    workers: int
    pages: int
    seconds: float
    setup_seconds: float = 0
    lots: int = 0
    input_bytes: int = 0
    cache_hits: int = 0
    unresolved: int = 0

    @property
    def hit_share(self) -> float:
        return self.cache_hits / self.pages


@dataclass(frozen=True)
class Cost:
    seconds: float
    low: float
    high: float
    calibrated: bool
    reason: str = ''


@dataclass(frozen=True)
class CacheSample:
    hits: int
    checked: int
    total: int
    mean_bytes: float = 0

    @property
    def share(self) -> float:
        return self.hits / self.checked if self.checked else 0

    @property
    def exact(self) -> bool:
        return self.checked == self.total

    @property
    def bounds(self) -> tuple[float, float]:
        if self.exact:
            return self.share, self.share
        if not self.checked:
            return 0, 1
        # Wilson interval for the sampled hit fraction; the total ETA range remains
        # indicative, since future document difficulty and machine load are unknown.
        correction = 1.96**2 / self.checked
        centre = (self.share + correction / 2) / (1 + correction)
        radius = 1.96 * math.sqrt(self.share * (1 - self.share) / self.checked
                                 + 1.96**2 / (4 * self.checked**2)) / (1 + correction)
        return max(0, centre - radius), min(1, centre + radius)


@dataclass(frozen=True)
class Estimate:
    seconds: float
    low: float
    high: float
    notes: tuple[str, ...]


def record(directory: Path, stage: str, *, workers: int, pages: int, seconds: float,
           setup_seconds: float = 0, lots: int = 0, input_bytes: int = 0,
           cache_hits: int = 0, unresolved: int = 0, args=None, source: Path | None = None) -> None:
    """Optional telemetry must never turn a completed document operation into a failure."""
    if pages <= 0:
        return
    try:
        from . import script as barcode
    except ImportError:
        import script as barcode
    settings = options(stage, args)
    if source is not None:
        settings['source'] = str(source.resolve())
    try:
        run = Run(stage, machine(stage), settings, workers, pages, seconds, setup_seconds,
                  lots, input_bytes, cache_hits, unresolved)
        payload = {'schema': SCHEMA, **asdict(run)}
        barcode.atomic_text_write(directory / 'performance.json',
                                  lambda handle: json.dump(payload, handle, allow_nan=False))
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning('Performance history could not be saved: %s', exc)


def history(root: Path, stage: str, settings: dict | None = None) -> list[Run]:
    settings = options(stage) if settings is None else settings
    try:
        paths = sorted((root / 'Reports' / stage).glob('*/performance.json'), reverse=True)[:HISTORY_LIMIT]
    except OSError:
        return []
    runs = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.pop('schema', None) != SCHEMA:
                continue
            run = Run(**data)
            if run.stage != stage or run.machine != machine(stage) or run.options != settings:
                continue
            integers = (run.workers, run.pages, run.lots, run.input_bytes, run.cache_hits, run.unresolved)
            if any(type(value) is not int or value < 0 for value in integers) or run.pages == 0:
                continue
            if not (0 <= run.cache_hits <= run.pages and 0 <= run.unresolved <= run.pages):
                continue
            if not all(type(value) in (int, float) and math.isfinite(value) and value >= 0
                       for value in (run.seconds, run.setup_seconds)):
                continue
            if run.seconds <= 0 or run.setup_seconds > run.seconds:
                continue
            if (stage in {'barcode', 'review'} and run.workers == 0
                    or stage == 'ocr' and ((run.workers == 0) != (run.cache_hits == run.pages))):
                continue
            runs.append(run)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return runs


def measurement_paths(root: Path, stage: str) -> set[Path]:
    try:
        return set(sorted((root / 'Reports' / stage).glob('*/performance.json'), reverse=True)[:HISTORY_LIMIT])
    except OSError:
        return set()


def include_launch_time(root: Path, stage: str, before: set[Path], seconds: float) -> None:
    """The shared launcher sees Python startup and worker shutdown as well as stage work."""
    try:
        created = measurement_paths(root, stage) - before
        if len(created) != 1:
            return
        path = created.pop()
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema') != SCHEMA:
            return
        overhead = max(0, seconds - data['seconds'])
        data['seconds'] += overhead
        data['setup_seconds'] += overhead
        from . import script as barcode
        barcode.atomic_text_write(path, lambda handle: json.dump(data, handle, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logging.getLogger(__name__).warning('Launch timing could not be saved: %s', exc)


def cache_sample(root: Path, stage: str, files: list[Path]) -> CacheSample:
    """Read-only, bounded sampling: never initialize models or hash a whole backlog."""
    if not files:
        return CacheSample(0, 0, 0)
    try:
        from . import ocr_pages as ocr
    except ImportError:
        import ocr_pages as ocr
    cache = None
    checked = hits = total_bytes = 0
    try:
        cache = ocr.OcrCache(root / 'Reports' / '.ocr_cache.sqlite3',
                             ocr.ocr_settings(200, 'cpu'), read_only=True)
        selected = random.Random(0).sample(sorted(files), min(CACHE_SAMPLE_LIMIT, len(files)))
        started = time.monotonic()
        for path in selected:
            if time.monotonic() - started > 2 or total_bytes >= 32 * 1024**2:
                break
            try:
                size = path.stat().st_size
                if size > 8 * 1024**2 or total_bytes + size > 32 * 1024**2:
                    continue
                digest = ocr.file_digest(path)
                hits += cache.get(digest) is not None
                checked += 1
                total_bytes += size
            except OSError:
                continue
    except (OSError, sqlite3.Error):
        pass
    finally:
        if cache is not None:
            cache.close()
    return CacheSample(hits, checked, len(files), total_bytes / checked if checked else 0)


def predict(runs: list[Run], *, pages: float, workers: int, fallback: float,
            lots: int = 0, input_bytes: float = 0, hit_share: float = 0) -> Cost:
    if pages <= 0:
        return Cost(0, 0, 0, True)
    compatible = [run for run in runs if run.workers == workers and abs(run.hit_share - hit_share) <= .2]
    if not compatible:
        # The configured value is a baseline, not evidence of linear parallel speedup.
        seconds = pages * fallback
        return Cost(seconds, seconds * .5, seconds * 2, False, 'aucune mesure comparable')

    def distance(run):
        size = abs(math.log(run.pages / pages))
        density = abs(math.log((run.pages / max(1, run.lots)) / (pages / max(1, lots)))) if lots else 0
        weight = abs(math.log((run.input_bytes / run.pages) / (input_bytes / pages))) if (
            run.input_bytes and input_bytes) else 0
        return size + density + weight + 4 * abs(run.hit_share - hit_share)

    nearby = sorted(compatible, key=distance)[:5]
    units = input_bytes if nearby[0].stage == 'import' else pages
    def sample_units(run):
        return run.input_bytes if run.stage == 'import' else run.pages
    nearby = [run for run in nearby if sample_units(run) > 0]
    if not nearby or units <= 0:
        seconds = pages * fallback
        return Cost(seconds, seconds * .5, seconds * 2, False, 'volume non mesuré')
    # Setup is observed. Model/process startup left in the work interval is learned
    # as an intercept when sufficiently varied, comparable batch sizes exist.
    slopes = []
    for i, first in enumerate(nearby):
        for second in nearby[i + 1:]:
            difference = sample_units(second) - sample_units(first)
            if difference and max(sample_units(first), sample_units(second)) >= 2 * min(
                    sample_units(first), sample_units(second)):
                slope = (second.seconds - first.seconds) / difference
                if slope >= 0:
                    slopes.append(slope)
    fitted = False
    if len(nearby) >= 3 and slopes:
        rate = statistics.median(slopes)
        intercept = max(statistics.median(run.setup_seconds for run in nearby),
                        statistics.median(run.seconds - rate * sample_units(run) for run in nearby))
        residuals = [abs(intercept + rate * sample_units(run) - run.seconds) / run.seconds for run in nearby]
        fitted = max(residuals) <= .25
    predictions = ([intercept + rate * units] if fitted else [
        run.setup_seconds + (run.seconds - run.setup_seconds) * units / sample_units(run) for run in nearby])
    seconds = statistics.median(predictions)
    extrapolated = not (min(sample_units(run) for run in nearby) / 2 <= units
                        <= max(sample_units(run) for run in nearby) * 2)
    lot_mismatch = lots and any(abs(math.log((run.pages / max(1, run.lots)) / (pages / lots))) > math.log(2)
                               for run in nearby)
    startup_unfitted = not fitted and all(abs(math.log(units / sample_units(run))) > math.log(1.25)
                                         for run in nearby)
    weak = len(nearby) < 3 or extrapolated or bool(lot_mismatch) or startup_unfitted
    margin = .5 if weak else max(.15, max(residuals) if fitted else 0)
    low = max(0, min(predictions) * (1 - margin))
    high = max(predictions) * (1 + margin)
    return Cost(seconds, low, high, not weak,
                'peu de mesures ou volume différent' if weak else '')


def unresolved_share(runs: list[Run], fallback: float) -> float:
    return statistics.median(run.unresolved / run.pages for run in runs[:10]) if runs else fallback


def routing_bounds(runs: list[Run]) -> tuple[float, float]:
    if not runs:
        return 0, 1
    shares = [run.unresolved / run.pages for run in runs[:10]]
    margin = .1 if len(shares) < 3 else .05
    return max(0, min(shares) - margin), min(1, max(shares) + margin)


def estimate(plan, config, pdf_files) -> Estimate:
    """Stages run in sequence. Their measured end-to-end costs are added once."""
    barcode_runs = history(config.root, 'barcode')
    ocr_runs = history(config.root, 'ocr')
    review_runs = history(config.root, 'review')
    copy_settings = {'source': str(config.source.resolve())}
    copy_runs = history(config.root, 'import', copy_settings)
    barcode_share = unresolved_share(barcode_runs, config.ocr_share)
    ocr_share = unresolved_share(ocr_runs, 1.0)
    ocr_cache = cache_sample(config.root, 'ocr', pdf_files(config.root / 'OCR')) if plan.ocr_files else CacheSample(0, 0, 0)
    review_cache = cache_sample(config.root, 'review', pdf_files(config.root / 'Pending')) if plan.pending_files else CacheSample(0, 0, 0)
    def stage_costs(barcode_share, ocr_share, ocr_hit_share, review_hit_share):
        ocr_pages = plan.ocr_files + plan.scan_pages * barcode_share
        new_review = ocr_pages * ocr_share
        review_pages = plan.pending_files + new_review
        ocr_hits = ocr_hit_share * plan.ocr_files
        review_hits = review_hit_share * plan.pending_files + new_review
        ocr_workers = 0 if ocr_hits == ocr_pages else min(config.ocr_workers, max(1, math.ceil(ocr_pages)))
        return {
            'copie': predict(copy_runs, pages=sum(lot.pages for lot in plan.incoming), workers=1,
                            fallback=(plan.incoming_bytes / (config.copy_megabytes_per_second * 1024**2)
                                      / max(1, sum(lot.pages for lot in plan.incoming))),
                            lots=len(plan.incoming), input_bytes=plan.incoming_bytes),
            f'codes-barres ({config.workers} processus)': predict(
                barcode_runs, pages=plan.scan_pages, workers=config.workers, fallback=config.barcode_seconds_per_page,
                lots=len(plan.incoming) + len(plan.waiting), input_bytes=sum(lot.size for lot in (*plan.incoming, *plan.waiting))),
            f'OCR ({config.ocr_workers} processus)': predict(
                ocr_runs, pages=ocr_pages, workers=ocr_workers, fallback=config.ocr_seconds_per_page,
                input_bytes=ocr_cache.mean_bytes * ocr_pages, hit_share=ocr_hits / ocr_pages if ocr_pages else 0),
            'classification finale (séquentielle)': predict(
                review_runs, pages=review_pages, workers=1, fallback=config.review_seconds_per_page,
                hit_share=review_hits / review_pages if review_pages else 0),
        }
    stages = stage_costs(barcode_share, ocr_share, ocr_cache.share, review_cache.share)
    notes = [f'{name} : {cost.reason}' for name, cost in stages.items() if not cost.calibrated]
    if plan.scan_pages and not barcode_runs:
        notes.append('part des pages envoyées en OCR configurée, non mesurée')
    if (plan.ocr_files or plan.scan_pages * barcode_share) and not ocr_runs:
        notes.append('part envoyée en classification finale non mesurée (hypothèse : toutes les pages OCR)')
    if not ocr_cache.exact or not review_cache.exact:
        notes.append('cache évalué sur un échantillon limité')
    barcode_low, barcode_high = routing_bounds(barcode_runs)
    ocr_low, ocr_high = routing_bounds(ocr_runs)
    cache_low, cache_high = ocr_cache.bounds
    review_cache_low, review_cache_high = review_cache.bounds
    optimistic = stage_costs(barcode_low, ocr_low, cache_high, review_cache_high)
    pessimistic = stage_costs(barcode_high, ocr_high, cache_low, review_cache_low)
    scenarios = (stages, optimistic, pessimistic)
    return Estimate(sum(cost.seconds for cost in stages.values()),
                    min(sum(cost.low for cost in scenario.values()) for scenario in scenarios),
                    max(sum(cost.high for cost in scenario.values()) for scenario in scenarios), tuple(notes))

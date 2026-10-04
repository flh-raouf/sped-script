"""Folder routing shared by the three classification stages."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StagePaths:
    source: Path
    output: Path
    unresolved: Path
    reports: Path
    cache: Path
    provenance: Path


def stage_paths(args, stage):
    root = args.directory.expanduser().resolve()
    if args.legacy_layout:
        output = (args.output or root.parent).expanduser().resolve()
        return StagePaths(root, output, output/'Review',
                          output/('ocr_runs' if stage == 'ocr' else 'review_runs'),
                          output/'.ocr_cache.sqlite3', output/'review_assignments.jsonl')
    output = (args.output or root/'Output').expanduser().resolve()
    source = root/('OCR' if stage == 'ocr' else 'Pending')
    previous = 'barcode classification' if stage == 'ocr' else 'OCR classification'
    if not source.is_dir():
        raise ValueError(f'Missing {source.name} folder in {root}. Run {previous} first.')
    # Sources must be disjoint from destination VIS folders and unresolved pages.
    if output == source or output in source.parents or source in output.parents:
        raise ValueError('VIS output must be separate from the stage input folder')
    unresolved = root/('Pending' if stage == 'ocr' else 'Review')
    if output == unresolved or output in unresolved.parents or unresolved in output.parents:
        raise ValueError('VIS output must be separate from the unresolved-page folder')
    reports = root/'Reports'
    return StagePaths(source, output, unresolved,
                      reports/stage, reports/'.ocr_cache.sqlite3', reports/'review_assignments.jsonl')

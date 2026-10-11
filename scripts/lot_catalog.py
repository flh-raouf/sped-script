"""Lots of the source folder, their processing status, and helpers to select them."""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

Ranges = tuple[tuple[int, int], ...]

RANGE_RE = re.compile(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?")


class Status(Enum):
    NEW = "new"                    # only in the source folder
    IN_PROGRESS = "in_progress"    # copied to the root, barcode scan not finished
    DONE = "done"                  # scanned: the working copy was archived


@dataclass(frozen=True)
class CatalogLot:
    name: str
    status: Status


def parse_ranges(text: str) -> Ranges:
    """``"1-82, 200-472, 600"`` -> ``((1, 82), (200, 472), (600, 600))``. Blank text gives ``()``."""
    ranges = []
    for part in filter(str.strip, re.split(r"[,;]", text)):
        match = RANGE_RE.fullmatch(part)
        if match is None:
            raise ValueError(f"plage invalide « {part.strip()} » (exemple : 600-610, 650)")
        low = int(match[1])
        high = int(match[2]) if match[2] else low
        if low > high:
            raise ValueError(f"plage à l'envers « {part.strip()} »")
        ranges.append((low, high))
    return tuple(ranges)


def in_ranges(number: int, ranges: Ranges) -> bool:
    return any(low <= number <= high for low, high in ranges)


def format_ranges(numbers: Iterable[int]) -> str:
    """``[83, 84, 85, 611]`` -> ``"83-85, 611"``."""
    runs: list[list[int]] = []
    for number in sorted(set(numbers)):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(low) if low == high else f"{low}-{high}" for low, high in runs)


def find_gaps(catalog: Sequence[CatalogLot], selected: Iterable[str]) -> list[str]:
    """Lots left out between two lots that are done or being processed.

    A gap is a stretch of unselected new lots whose neighbours (in the list of
    existing lots) are both covered, at least one of them being processed by this
    very run. Old holes between lots processed long ago are therefore not reported.
    """
    chosen = set(selected)
    ordered = sorted(catalog, key=lambda lot: int(lot.name))
    active = [lot.name in chosen or lot.status is Status.IN_PROGRESS for lot in ordered]
    covered = [flag or lot.status is Status.DONE for flag, lot in zip(active, ordered)]
    gaps: list[str] = []
    index = 0
    while index < len(ordered):
        if covered[index]:
            index += 1
            continue
        end = index
        while end < len(ordered) and not covered[end]:
            end += 1
        if 0 < index and end < len(ordered) and (active[index - 1] or active[end]):
            gaps.extend(lot.name for lot in ordered[index:end])
        index = end
    return gaps

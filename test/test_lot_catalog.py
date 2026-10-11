from __future__ import annotations

import unittest

from scripts.lot_catalog import CatalogLot, Status, find_gaps, format_ranges, in_ranges, parse_ranges

DONE, NEW, PROGRESS = Status.DONE, Status.NEW, Status.IN_PROGRESS


def catalog(**statuses: Status) -> list[CatalogLot]:
    """``catalog(l610=DONE, l611=NEW)`` -> lots 610 and 611."""
    return [CatalogLot(name.removeprefix("l"), status) for name, status in statuses.items()]


class RangeTests(unittest.TestCase):
    def test_parses_ranges_single_lots_and_blank(self):
        self.assertEqual(parse_ranges("1-82, 200 - 472;600"), ((1, 82), (200, 472), (600, 600)))
        self.assertEqual(parse_ranges("  "), ())

    def test_rejects_garbage_and_reversed_ranges(self):
        for text in ("abc", "1-", "5-2", "1,,x"):
            with self.assertRaises(ValueError, msg=text):
                parse_ranges(text)

    def test_membership(self):
        ranges = parse_ranges("1-82, 200-472")
        self.assertEqual([in_ranges(n, ranges) for n in (1, 82, 83, 199, 200, 472, 473)],
                         [True, True, False, False, True, True, False])

    def test_formats_runs_compactly(self):
        self.assertEqual(format_ranges([611, 83, 84, 85, 87, 83]), "83-85, 87, 611")
        self.assertEqual(format_ranges([]), "")


class GapTests(unittest.TestCase):
    def test_lot_skipped_between_a_done_and_a_selected_lot(self):
        lots = catalog(l610=DONE, l611=NEW, l612=NEW)
        self.assertEqual(find_gaps(lots, ["612"]), ["611"])
        self.assertEqual(find_gaps(lots, ["611", "612"]), [])

    def test_old_holes_between_processed_lots_are_not_reported(self):
        lots = catalog(l82=DONE, l83=NEW, l199=NEW, l200=DONE, l600=NEW)
        self.assertEqual(find_gaps(lots, ["600"]), [])
        self.assertEqual(find_gaps(lots, []), [])

    def test_lots_at_the_edges_of_the_list_are_not_gaps(self):
        lots = catalog(l1=NEW, l2=NEW, l3=DONE, l4=NEW, l5=NEW)
        self.assertEqual(find_gaps(lots, ["4"]), [])         # lot 5 has no neighbour above it
        self.assertEqual(find_gaps(lots, []), [])

    def test_a_lot_in_progress_counts_as_part_of_the_run(self):
        lots = catalog(l10=DONE, l11=NEW, l12=PROGRESS)
        self.assertEqual(find_gaps(lots, []), ["11"])

    def test_gap_between_two_selected_lots(self):
        lots = catalog(l20=NEW, l21=NEW, l22=NEW, l23=NEW)
        self.assertEqual(find_gaps(lots, ["20", "23"]), ["21", "22"])

    def test_order_of_the_catalog_does_not_matter(self):
        lots = list(reversed(catalog(l610=DONE, l611=NEW, l612=NEW)))
        self.assertEqual(find_gaps(lots, ["612"]), ["611"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import curses
import unittest

from scripts import lot_picker
from scripts.lot_catalog import CatalogLot, Status
from scripts.lot_picker import PickerState, render

SPACE, ENTER, ESCAPE = ord(" "), 10, 27


def lots(newest: int = 300, done_from: int = 100, done_to: int = 199, in_progress: int | None = None):
    """Lots ``newest`` down to 1: new above ``done_to``, done in the given range, new below."""
    def status(number: int) -> Status:
        if number == in_progress:
            return Status.IN_PROGRESS
        return Status.DONE if done_from <= number <= done_to else Status.NEW
    return [CatalogLot(str(number), status(number)) for number in range(newest, 0, -1)]


class FakeScreen:
    def __init__(self, keys, height: int = 20, width: int = 80):
        self.keys = iter(keys)
        self.size = (height, width)
        self.painted: list[tuple[int, str, int]] = []
        self.frames: list[list[tuple[int, str, int]]] = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.painted = []

    def addnstr(self, y, x, text, n, attributes=0):
        self.painted.append((y, text[:n], attributes))

    def move(self, y, x):
        pass

    def clrtoeol(self):
        pass

    def refresh(self):
        self.frames.append(list(self.painted))

    def getch(self):
        return next(self.keys)

    def text(self) -> str:
        return "\n".join(text for _, text, _ in self.frames[-1])


class StateTests(unittest.TestCase):
    def test_toggle_selects_and_unselects_a_new_lot(self):
        state = PickerState(lots())
        state.toggle()
        self.assertEqual(state.selected, {"300"})
        state.toggle()
        self.assertEqual(state.selected, set())

    def test_done_and_in_progress_lots_cannot_be_toggled(self):
        state = PickerState(lots(in_progress=250))
        state.move(300 - 150)                                  # lot 150 is done
        state.toggle()
        self.assertEqual((state.selected, state.message), (set(), "Le lot 150 est déjà traité."))
        state.cursor = 50                                      # lot 250
        state.toggle()
        self.assertEqual(state.selected, set())
        self.assertIn("repris automatiquement", state.message)

    def test_select_new_and_clear(self):
        state = PickerState(lots())
        state.select_new()
        self.assertEqual(len(state.selected), 300 - 100)
        self.assertNotIn("150", state.selected)
        state.clear()
        self.assertEqual(state.selected, set())

    def test_cursor_stays_inside_the_list(self):
        state = PickerState(lots(newest=5))
        state.move(-10)
        self.assertEqual(state.cursor, 0)
        state.move(1000)
        self.assertEqual(state.cursor, 4)

    def test_range_ticks_only_new_lots_and_jumps_to_the_first(self):
        state = PickerState(lots())
        state.select_ranges("195-205, 50")
        self.assertEqual(state.selected, {"200", "201", "202", "203", "204", "205", "50"})
        self.assertEqual(state.rows[state.cursor].name, "205")
        self.assertEqual(state.message, "7 lots ajoutés.")

    def test_range_feedback_for_empty_and_invalid_input(self):
        state = PickerState(lots())
        state.select_ranges("120-130")
        self.assertEqual(state.message, "Aucun nouveau lot dans cette plage.")
        state.select_ranges("abc")
        self.assertIn("Plage refusée", state.message)
        self.assertEqual(state.selected, set())

    def test_long_jump_centres_the_cursor_and_a_step_scrolls_by_one(self):
        state = PickerState(lots())
        state.scroll(10)
        state.move(100)
        state.scroll(10)
        self.assertEqual(state.top, 100 - 5)
        state.cursor, state.top = 9, 0
        state.move(1)
        state.scroll(10)
        self.assertEqual(state.top, 1)

    def test_list_shorter_than_the_screen_never_scrolls(self):
        state = PickerState(lots(newest=3))
        state.move(2)
        state.scroll(10)
        self.assertEqual(state.top, 0)


class RenderTests(unittest.TestCase):
    def test_screen_shows_counts_help_rows_and_footer(self):
        state = PickerState(lots(in_progress=250))
        state.selected = {"299"}
        lines = render(state, 20, 80)
        self.assertEqual(len(lines), 20)
        self.assertIn("300 au total, 199 nouveaux, 100 traités, 1 en cours", lines[0].text)
        self.assertIn("↑ ↓ : se déplacer    ← → : sauter de 100 lots", lines[1].text)
        self.assertNotIn("Page", "".join(line.text for line in lines[:5]))
        self.assertIn(">", lines[5].text)
        self.assertTrue(lines[5].reverse)
        self.assertIn("[ ]  300", lines[5].text)
        self.assertIn("[x]  299", lines[6].text)
        self.assertEqual(lines[-1].text.strip(), "1 lot sélectionné")

    def test_done_lots_are_green_with_a_check_and_in_progress_lots_yellow(self):
        state = PickerState(lots(in_progress=250))
        state.cursor = 120                                      # lot 180, done
        done = next(line for line in render(state, 20, 80) if line.color == "green")
        self.assertIn("✔", done.text)
        self.assertTrue(done.text.endswith("traité"))
        state.cursor = 50
        yellow = next(line for line in render(state, 20, 80) if line.color == "yellow")
        self.assertIn("[x]", yellow.text)
        self.assertIn("250", yellow.text)

    def test_ascii_fallback_for_terminals_without_unicode(self):
        state = PickerState(lots())
        state.cursor = 150
        lines = render(state, 20, 80, unicode=False)
        done = next(line for line in lines if line.color == "green")
        self.assertIn("OK", done.text)
        self.assertIn("^ v : se déplacer    < > : sauter de 100 lots", lines[1].text)
        self.assertFalse(any(char in line.text for line in lines for char in "✔↑↓←→"))

    def test_tiny_terminal_does_not_crash(self):
        lines = render(PickerState(lots()), 3, 10)
        self.assertEqual(len(lines), 3)
        self.assertIn("sélectionné", lines[-1].text)


class InteractionTests(unittest.TestCase):
    def run_keys(self, keys, catalog=None, height: int = 20):
        screen = FakeScreen(keys, height=height)
        state = PickerState(catalog or lots())
        result = lot_picker._interact(screen, state, {}, True)
        return result, state, screen

    def test_space_then_enter_returns_the_selection_in_numeric_order(self):
        result, _, _ = self.run_keys([SPACE, curses.KEY_DOWN, curses.KEY_DOWN, SPACE, ENTER])
        self.assertEqual(result, ["298", "300"])

    def test_right_key_skips_one_hundred_lots_and_left_comes_back(self):
        result, state, _ = self.run_keys([curses.KEY_RIGHT, SPACE, curses.KEY_LEFT, SPACE, ENTER])
        self.assertEqual(result, ["200", "300"])
        self.assertEqual(state.cursor, 0)

    def test_escape_and_q_quit_without_a_selection(self):
        self.assertIsNone(self.run_keys([SPACE, ESCAPE])[0])
        self.assertIsNone(self.run_keys([ord("q")])[0])

    def test_enter_with_nothing_ticked_returns_an_empty_selection(self):
        self.assertEqual(self.run_keys([ENTER])[0], [])

    def test_a_and_n_keys(self):
        result, _, _ = self.run_keys([ord("a"), ord("n"), ord("a"), ENTER])
        self.assertEqual(len(result), 200)

    def test_range_prompt_accepts_typed_text_and_backspace(self):
        keys = [ord("r"), *map(ord, "600-60x"), curses.KEY_BACKSPACE, *map(ord, "2"), ENTER, ENTER]
        result, _, _ = self.run_keys(keys, catalog=lots(newest=700))
        self.assertEqual(result, ["600", "601", "602"])

    def test_range_prompt_can_be_cancelled(self):
        result, _, _ = self.run_keys([ord("r"), ord("5"), ESCAPE, ENTER])
        self.assertEqual(result, [])

    def test_message_is_cleared_on_the_next_key(self):
        _, state, screen = self.run_keys([curses.KEY_DOWN] * 140 + [SPACE, curses.KEY_UP, ESCAPE])
        self.assertEqual(state.message, "")
        self.assertIn("déjà traité", "\n".join(text for frame in screen.frames for _, text, _ in frame))


if __name__ == "__main__":
    unittest.main()

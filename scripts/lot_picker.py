"""Full-screen keyboard list to choose which lots to process."""
from __future__ import annotations

import curses
import locale
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from .lot_catalog import CatalogLot, Status, in_ranges, parse_ranges

JUMP = 100
CHROME_LINES = 6   # title, three help lines, separator, footer
HELP = (
    " Haut/Bas : déplacer    Gauche/Droite : 100 lots    Page préc./suiv. : une page",
    " Espace : cocher/décocher    a : tous les nouveaux    n : aucun    r : plage",
    " Entrée : confirmer la sélection    Échap : quitter sans rien faire",
)
IN_PROGRESS_LABEL = "en cours, sera repris automatiquement"
DONE_LABEL = "traité"
ESCAPE = 27
ENTER_KEYS = (10, 13, curses.KEY_ENTER)


class PickerError(RuntimeError):
    """The terminal cannot display the list."""


@dataclass(frozen=True)
class Line:
    text: str
    color: str | None = None    # "green" or "yellow"
    bold: bool = False
    reverse: bool = False


class PickerState:
    """What is selected and where the cursor is; knows nothing about the terminal."""

    def __init__(self, catalog: Sequence[CatalogLot]):
        self.rows = list(catalog)
        self.cursor = 0
        self.top = 0
        self.page_size = 1
        self.selected: set[str] = set()
        self.message = ""

    def move(self, delta: int) -> None:
        self.cursor = max(0, min(len(self.rows) - 1, self.cursor + delta))

    def toggle(self) -> None:
        row = self.rows[self.cursor]
        if row.status is Status.DONE:
            self.message = f"Le lot {row.name} est déjà traité."
        elif row.status is Status.IN_PROGRESS:
            self.message = f"Le lot {row.name} est en cours : il sera repris automatiquement."
        else:
            self.selected ^= {row.name}

    def select_new(self) -> None:
        self.selected = {row.name for row in self.rows if row.status is Status.NEW}

    def clear(self) -> None:
        self.selected.clear()

    def select_ranges(self, text: str) -> None:
        try:
            ranges = parse_ranges(text)
        except ValueError as exc:
            self.message = f"Plage refusée : {exc}."
            return
        indexes = [index for index, row in enumerate(self.rows)
                   if row.status is Status.NEW and in_ranges(int(row.name), ranges)]
        if not indexes:
            self.message = "Aucun nouveau lot dans cette plage."
            return
        self.selected.update(self.rows[index].name for index in indexes)
        self.cursor = indexes[0]
        self.message = f"{len(indexes)} lot{'s' if len(indexes) > 1 else ''} ajouté{'s' if len(indexes) > 1 else ''}."

    def scroll(self, height: int) -> None:
        """Keep the cursor visible; centre it after a long jump, scroll gently after a step."""
        self.page_size = height
        if self.cursor < self.top or self.cursor >= self.top + height:
            stepped = self.cursor in (self.top - 1, self.top + height)
            if not stepped:
                self.top = self.cursor - height // 2
            else:
                self.top = self.cursor if self.cursor < self.top else self.cursor - height + 1
        self.top = max(0, min(self.top, len(self.rows) - height))


def plural(count: int, singular: str, plural_form: str) -> str:
    return f"{count} {singular if count <= 1 else plural_form}"


def render(state: PickerState, height: int, width: int, check: str = "✔") -> list[Line]:
    """The screen as a list of lines, newest first, exactly as it will be painted."""
    state.scroll(max(1, height - CHROME_LINES))
    status = [row.status for row in state.rows]
    title = (f" Lots à traiter : {len(status)} au total, {status.count(Status.NEW)} nouveaux, "
             f"{status.count(Status.DONE)} traités")
    if Status.IN_PROGRESS in status:
        title += f", {status.count(Status.IN_PROGRESS)} en cours"
    lines = [Line(title, bold=True), *(Line(text) for text in HELP), Line("-" * max(0, width - 1))]
    name_width = max(len(row.name) for row in state.rows)
    for index in range(state.top, min(len(state.rows), state.top + state.page_size)):
        row = state.rows[index]
        if row.status is Status.DONE:
            box, color, label = check.center(3), "green", DONE_LABEL
        elif row.status is Status.IN_PROGRESS:
            box, color, label = "[x]", "yellow", IN_PROGRESS_LABEL
        else:
            box, color, label = ("[x]" if row.name in state.selected else "[ ]"), None, ""
        text = f" {'>' if index == state.cursor else ' '} {box}  {row.name:>{name_width}}  {label}"
        lines.append(Line(text.rstrip(), color, reverse=index == state.cursor))
    footer = f" {plural(len(state.selected), 'lot sélectionné', 'lots sélectionnés')}"
    if state.message:
        footer += f"  |  {state.message}"
    lines.extend(Line("") for _ in range(height - 1 - len(lines)))   # keep the footer on the last line
    return lines[:height - 1] + [Line(footer, bold=True)]


# --- terminal -----------------------------------------------------------------

def _paint(screen, lines: Sequence[Line], colors: dict[str, int]) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    for y, line in enumerate(lines[:height]):
        attributes = colors.get(line.color, 0) if line.color else 0
        attributes |= (curses.A_BOLD if line.bold else 0) | (curses.A_REVERSE if line.reverse else 0)
        try:
            screen.addnstr(y, 0, line.text, max(0, width - 1), attributes)
        except curses.error:
            pass   # writing the last cell of the screen raises; nothing is lost
    screen.refresh()


def _read_line(screen, prompt: str) -> str | None:
    """Type a short text on the last screen line; Enter validates, Escape cancels."""
    text = ""
    with typing_cursor():
        while True:
            height, width = screen.getmaxyx()
            screen.move(height - 1, 0)
            screen.clrtoeol()
            screen.addnstr(height - 1, 0, prompt + text, max(0, width - 1))
            screen.refresh()
            key = screen.getch()
            if key in ENTER_KEYS:
                return text
            if key == ESCAPE:
                return None
            if key in (curses.KEY_BACKSPACE, 8, 127):
                text = text[:-1]
            elif 32 <= key < 127:
                text += chr(key)


def set_cursor(visible: bool) -> None:
    with suppress(curses.error):   # some terminals cannot hide the cursor
        curses.curs_set(int(visible))


@contextmanager
def typing_cursor() -> Iterator[None]:
    set_cursor(True)
    try:
        yield
    finally:
        set_cursor(False)


def _interact(screen, state: PickerState, colors: dict[str, int], check: str) -> list[str] | None:
    moves = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_LEFT: -JUMP, curses.KEY_RIGHT: JUMP,
             curses.KEY_HOME: -len(state.rows), curses.KEY_END: len(state.rows)}
    while True:
        height, width = screen.getmaxyx()
        _paint(screen, render(state, height, width, check), colors)
        key = screen.getch()
        state.message = ""
        if key in moves:
            state.move(moves[key])
        elif key == curses.KEY_PPAGE:
            state.move(-state.page_size)
        elif key == curses.KEY_NPAGE:
            state.move(state.page_size)
        elif key == ord(" "):
            state.toggle()
        elif key in (ord("a"), ord("A")):
            state.select_new()
        elif key in (ord("n"), ord("N")):
            state.clear()
        elif key in (ord("r"), ord("R")):
            text = _read_line(screen, " Plage à cocher (ex. 600-610, 650) : ")
            if text is not None:
                state.select_ranges(text)
        elif key in ENTER_KEYS:
            return sorted(state.selected, key=int)
        elif key in (ESCAPE, ord("q"), ord("Q")):
            return None


def _setup_colors() -> dict[str, int]:
    if not curses.has_colors():
        return {}
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    return {"green": curses.color_pair(1), "yellow": curses.color_pair(2)}


def pick_lots(catalog: Sequence[CatalogLot]) -> list[str] | None:
    """Let the user choose among the new lots; ``None`` when they quit.

    ``catalog`` is shown as given. Raises ``PickerError`` if the terminal cannot
    display the list.
    """
    with suppress(locale.Error):
        locale.setlocale(locale.LC_ALL, "")
    check = "✔" if locale.getpreferredencoding(False).lower().replace("-", "") == "utf8" else "OK"
    os.environ.setdefault("ESCDELAY", "25")   # make the Escape key react at once
    state = PickerState(catalog)

    def run(screen):
        set_cursor(False)
        return _interact(screen, state, _setup_colors(), check)

    try:
        return curses.wrapper(run)
    except curses.error as exc:
        raise PickerError(f"ce terminal ne permet pas d'afficher la liste ({exc})") from exc

#!/usr/bin/env python3
"""Interactive launcher for the document-processing utilities."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.classify_pages import discover_database
from scripts.script import ConfigurationError

SCRIPT_DIRECTORY = Path(__file__).resolve().parent / "scripts"
ADDITIONAL_ACTION = "Opérations complémentaires"
EXIT_ACTION = "Quitter"
ACTIONS = {
    "Extraction des codes-barres": "classify_pages.py",
    "Extraction par OCR (PP-OCRv6 tiny)": "ocr_pages.py",
    "Classification finale (tableaux de pièces et versos peu renseignés)": "review_pages.py",
    ADDITIONAL_ACTION: None,
}
ADDITIONAL_ACTIONS = {
    "Nettoyage des fichiers cachés": "clean_hidden_files.py",
    "Éclatement des PDF en pages individuelles": "split_pages.py",
}
SUPPORTED_SCRIPTS = {
    script
    for script in [*ACTIONS.values(), *ADDITIONAL_ACTIONS.values()]
    if script is not None
}


def normalize_directory(value: str) -> Path:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    if not text:
        raise ValueError("Saisissez le chemin d'un dossier.")
    return Path(text).expanduser().resolve()


def valid_directory(value: str) -> bool:
    try:
        return normalize_directory(value).is_dir()
    except (OSError, ValueError, RuntimeError):
        return False


def valid_workers(value: str) -> bool:
    return value.isascii() and value.isdigit() and int(value) > 0


def build_command(
    script: str, directory: Path, *, workers: int = 1,
    dry_run: bool = False, excel: Path | None = None, database: Path | None = None,
    output: Path | None = None,
) -> list[str]:
    if script not in SUPPORTED_SCRIPTS:
        raise ValueError(f"Unknown operation: {script}")
    command = [sys.executable, "-u", str(SCRIPT_DIRECTORY / script),
               "-d", str(directory)]
    if script == "classify_pages.py":
        if workers <= 0:
            raise ValueError("Workers must be greater than zero.")
        command.extend(["-n", str(workers)])
        if excel is not None:
            command.extend(["--excel", str(excel)])
        if script == "classify_pages.py" and database is not None:
            command.extend(["--database", str(database)])
    elif script in {"ocr_pages.py", "review_pages.py"}:
        if database is not None:
            command.extend(["--database", str(database)])
        if output is not None:
            command.extend(["--output", str(output)])
        if script == "review_pages.py" and dry_run:
            command.append("--dry-run")
    elif script == "clean_hidden_files.py" and dry_run:
        command.append("--dry-run")
    return command


def execute_command(command: list[str]) -> int:
    # Inherit the terminal so child progress and errors appear immediately.
    # Ctrl+C reaches the child too; wait for it before accepting another action.
    with subprocess.Popen(command, cwd=SCRIPT_DIRECTORY) as process:
        try:
            return process.wait()
        except KeyboardInterrupt:
            print("\nStopping operation…", flush=True)
            try:
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                process.kill()
                process.wait()
            return 130


def main() -> int:
    try:
        from InquirerPy import get_style, inquirer
        from InquirerPy.prompts.list import InquirerPyListControl
    except ImportError:
        print('Install the menu dependency with: python -m pip install "InquirerPy==0.3.4"',
              file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Run python main.py in an interactive terminal.", file=sys.stderr)
        return 2

    class MainMenuListControl(InquirerPyListControl):
        def _color_exit_choice(self, choice, display_choices):
            if choice["value"] == EXIT_ACTION:
                display_choices[-1] = ("class:exit-choice", choice["name"])
            return display_choices

        def _get_normal_text(self, choice):
            return self._color_exit_choice(choice, super()._get_normal_text(choice))

        def _get_hover_text(self, choice):
            return self._color_exit_choice(choice, super()._get_hover_text(choice))

    menu_style = get_style({"exit-choice": "red"}, style_override=False)

    def select_main_action():
        prompt = inquirer.select(
            message="Que souhaitez-vous faire ?",
            choices=[*ACTIONS, EXIT_ACTION],
            style=menu_style,
        )
        original_control = prompt.content_control
        colored_control = MainMenuListControl(
            choices=original_control.choices,
            default=None,
            pointer=original_control._pointer,
            marker=original_control._marker,
            session_result=original_control._session_result,
            multiselect=original_control._multiselect,
            marker_pl=original_control._marker_pl,
        )
        colored_control.selected_choice_index = original_control.selected_choice_index
        prompt.content_control = colored_control
        for window in prompt.application.layout.find_all_windows():
            if window.content is original_control:
                window.content = colored_control
        return prompt.execute()

    print("\nTraitement des documents\nUtilisez les flèches et Entrée. Ctrl+C annule une invite.\n")
    last_directory = ""
    while True:
        try:
            action = select_main_action()
        except (KeyboardInterrupt, EOFError):
            return 0
        if action == EXIT_ACTION:
            return 0
        try:
            if action == ADDITIONAL_ACTION:
                try:
                    additional_action = inquirer.select(
                        message="Opération complémentaire :",
                        choices=[*ADDITIONAL_ACTIONS, "Retour au menu"],
                    ).execute()
                except (KeyboardInterrupt, EOFError):
                    continue
                if additional_action == "Retour au menu":
                    continue
                script = ADDITIONAL_ACTIONS[additional_action]
                display_action = additional_action
            else:
                script = ACTIONS[action]
                display_action = action
            assert script is not None
            path_text = inquirer.filepath(
                message="Dossier racine :", default=last_directory,
                only_directories=True, validate=valid_directory,
                invalid_message="Saisissez le chemin d'un dossier existant (les guillemets sont facultatifs).",
            ).execute()
            directory = normalize_directory(path_text)
            last_directory = str(directory)
            workers, dry_run, excel, database = 1, False, None, None
            output = None
            if script == "classify_pages.py":
                workers = int(inquirer.text(
                    message="Nombre de processus :", default="1",
                    validate=valid_workers,
                    invalid_message="Saisissez un nombre entier supérieur à zéro.",
                ).execute())
                if script == "classify_pages.py":
                    database = discover_database(directory)
                    print(f"BDD détectée : {database.name}", flush=True)
                workbooks = sorted(
                    path for path in directory.iterdir()
                    if path.is_file() and path.suffix.lower() in {".xlsx", ".xlsm"}
                    and not path.name.startswith(("~$", "."))
                    and not path.stem.lower().endswith("_found")
                    and path.resolve() != database
                )
                if not workbooks:
                    print("Aucun classeur Excel source trouvé directement dans ce dossier.\n")
                    continue
                excel = workbooks[0]
                if len(workbooks) > 1:
                    selected = inquirer.select(
                        message="Classeur source :",
                        choices=[path.name for path in workbooks],
                    ).execute()
                    excel = directory / selected
            elif script in {"ocr_pages.py", "review_pages.py"}:
                input_name = 'OCR' if script == 'ocr_pages.py' else 'Pending'
                print(f"Entrée : {directory / input_name}\nDossiers VIS : {directory / 'Output'}", flush=True)
            elif script == "clean_hidden_files.py":
                mode = inquirer.select(
                    message="Mode de nettoyage :",
                    choices=["Simulation — prévisualiser les fichiers",
                             "Supprimer les fichiers cachés concernés", "Retour au menu"],
                ).execute()
                if mode == "Retour au menu":
                    continue
                dry_run = mode.startswith("Simulation")
            command = build_command(script, directory, workers=workers,
                                    dry_run=dry_run, excel=excel, database=database, output=output)
            print(f"\n{display_action}\nDossier : {directory}\n", flush=True)
            code = execute_command(command)
            if code == 0:
                print("\nOpération terminée. Les éventuels résultats se trouvent dans le dossier sélectionné.")
            elif code == 130:
                print("\nOpération interrompue. Des résultats partiels peuvent subsister.")
            else:
                print(f"\nL'opération s'est terminée avec des erreurs (code de sortie {code}). Consultez les messages ci-dessus.")
            inquirer.text(message="Appuyez sur Entrée pour revenir au menu :").execute()
        except (KeyboardInterrupt, EOFError):
            print("\nRetour au menu.\n")
        except (OSError, ValueError, ConfigurationError) as exc:
            print(f"\nImpossible d'exécuter l'opération : {exc}\n", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

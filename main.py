#!/usr/bin/env python3
"""Interactive launcher for the document-processing utilities."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent / "scripts"
RECONSTRUCTION_ACTION = "Document reconstruction"
RECONSTRUCTION_MODES = {
    "Identified pages only": "reconstruct_identified.py",
    "Local ranges": "reconstruct_ranges.py",
}
ACTIONS = {
    "Barcode extraction": "script.py",
    RECONSTRUCTION_ACTION: None,
    "Clean hidden files": "clean_hidden_files.py",
    "Split PDFs into individual pages (éclatement)": "split_pages.py",
}
SUPPORTED_SCRIPTS = {
    *(script for script in ACTIONS.values() if script is not None),
    *RECONSTRUCTION_MODES.values(),
}


def normalize_directory(value: str) -> Path:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    if not text:
        raise ValueError("Enter a folder path.")
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
    dry_run: bool = False, excel: Path | None = None,
) -> list[str]:
    if script not in SUPPORTED_SCRIPTS:
        raise ValueError(f"Unknown operation: {script}")
    command = [sys.executable, "-u", str(SCRIPT_DIRECTORY / script),
               "-d", str(directory)]
    if script == "script.py":
        if workers <= 0:
            raise ValueError("Workers must be greater than zero.")
        command.extend(["-n", str(workers)])
        if excel is not None:
            command.extend(["--excel", str(excel)])
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
        from InquirerPy import inquirer
    except ImportError:
        print('Install the menu dependency with: python -m pip install "InquirerPy==0.3.4"',
              file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Run python main.py in an interactive terminal.", file=sys.stderr)
        return 2

    print("\nDocument processing\nUse arrow keys and Enter. Ctrl+C cancels a prompt.\n")
    last_directory = ""
    while True:
        try:
            action = inquirer.select(
                message="What would you like to do?",
                choices=[*ACTIONS, "Exit"],
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return 0
        if action == "Exit":
            return 0
        try:
            script = ACTIONS[action]
            display_action = action
            if action == RECONSTRUCTION_ACTION:
                reconstruction_mode = inquirer.select(
                    message="Reconstruction method:",
                    choices=[*RECONSTRUCTION_MODES, "Back to menu"],
                ).execute()
                if reconstruction_mode == "Back to menu":
                    continue
                script = RECONSTRUCTION_MODES[reconstruction_mode]
                display_action = f"{action} — {reconstruction_mode}"
            assert script is not None
            path_text = inquirer.filepath(
                message="Root folder:", default=last_directory,
                only_directories=True, validate=valid_directory,
                invalid_message="Enter an existing folder path (quotes are optional).",
            ).execute()
            directory = normalize_directory(path_text)
            last_directory = str(directory)
            workers, dry_run, excel = 1, False, None
            if script == "script.py":
                workers = int(inquirer.text(
                    message="Number of workers:", default="1",
                    validate=valid_workers,
                    invalid_message="Enter a whole number greater than zero.",
                ).execute())
                workbooks = sorted(
                    path for path in directory.iterdir()
                    if path.is_file() and path.suffix.lower() in {".xlsx", ".xlsm"}
                    and not path.name.startswith(("~$", "."))
                    and not path.stem.lower().endswith("_found")
                )
                if not workbooks:
                    print("No source Excel workbook found directly in this folder.\n")
                    continue
                excel = workbooks[0]
                if len(workbooks) > 1:
                    selected = inquirer.select(
                        message="Source workbook:",
                        choices=[path.name for path in workbooks],
                    ).execute()
                    excel = directory / selected
            elif script == "clean_hidden_files.py":
                mode = inquirer.select(
                    message="Cleaning mode:",
                    choices=["Dry run — preview files", "Delete eligible hidden files",
                             "Back to menu"],
                ).execute()
                if mode == "Back to menu":
                    continue
                dry_run = mode.startswith("Dry run")
            elif script.startswith("reconstruct_"):
                if not (directory / "results.csv").is_file():
                    print("Missing results.csv directly inside this folder.\n")
                    continue

            command = build_command(script, directory, workers=workers,
                                    dry_run=dry_run, excel=excel)
            print(f"\n{display_action}\nFolder: {directory}\n", flush=True)
            code = execute_command(command)
            if code == 0:
                print("\nCompleted successfully. Outputs, if any, are in the selected folder.")
            elif code == 130:
                print("\nOperation interrupted. Partial outputs may remain.")
            else:
                print(f"\nOperation ended with errors (exit code {code}). See the messages above.")
            inquirer.text(message="Press Enter to return to the menu:").execute()
        except (KeyboardInterrupt, EOFError):
            print("\nReturning to menu.\n")
        except (OSError, ValueError) as exc:
            print(f"\nCould not run operation: {exc}\n", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

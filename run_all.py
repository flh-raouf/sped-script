#!/usr/bin/env python3
"""One-click run: import new lots, then barcode, OCR and final classification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from main import build_command, execute_command
from scripts import pipeline, wsl_host

PROJECT_DIRECTORY = Path(__file__).resolve().parent


def confirm() -> bool:
    try:
        answer = input("\nAppuyez sur Entrée pour lancer (n puis Entrée, ou Ctrl+C, pour annuler) : ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() not in {"n", "non"}


def make_stage_runner(config: pipeline.Config, database: Path, search_list: Path) -> pipeline.StageRunner:
    def run_stage(script: str) -> int:
        command = build_command(script, config.root, workers=config.workers,
                                excel=search_list, database=database)
        return execute_command(command)
    return run_stage


def run(args: argparse.Namespace, interactive: bool) -> int:
    config = pipeline.load_config(args.config)
    with pipeline.run_lock(config.root):
        database, search_list = pipeline.preflight(config)
        print(f"Source : {config.source}\nRacine : {config.root}\nBDD : {database.name} | Liste : {search_list.name}")
        registry = pipeline.Registry(pipeline.registry_path(config))
        print("\nAnalyse des lots à traiter…", flush=True)
        plan = pipeline.build_plan(config, registry)
        print(pipeline.format_plan(plan, config))
        if plan.idle:
            print("\nRien à traiter.")
            return 0
        if interactive and not confirm():
            print("Annulé. Rien n'a été modifié.")
            return 0
        pipeline.check_disk_space(config, plan)
        with wsl_host.keep_awake() as awake:
            if awake:
                print("\nLa mise en veille de Windows est suspendue pendant le traitement.")
            summary = pipeline.run_pipeline(config, registry, plan,
                                            make_stage_runner(config, database, search_list))
    text = pipeline.format_summary(summary)
    print(text)
    print(f"Résumé enregistré : {pipeline.save_summary(config, text)}")
    if interactive:
        review = config.root / "Review"
        wsl_host.open_folder(review if review.is_dir() else config.root)
    return 0 if summary.ok else 1


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_DIRECTORY / "config.toml",
                        help="Fichier de configuration (défaut : config.toml)")
    parser.add_argument("--yes", action="store_true",
                        help="Lancer sans confirmation ni pause (pour une tâche planifiée)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):   # keep our messages ordered with the stages' output
        sys.stdout.reconfigure(line_buffering=True)
    interactive = sys.stdin.isatty() and not args.yes
    try:
        code = run(args, interactive)
    except pipeline.PipelineError as exc:
        print(f"\nERREUR : {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        code = 130
    if interactive:
        try:
            input("\nAppuyez sur Entrée pour fermer cette fenêtre.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())

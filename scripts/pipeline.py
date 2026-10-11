"""One-click pipeline: import the chosen lots, then barcode -> OCR -> final review.

Lots are *copied* from the scan destination into the permanent working root;
the originals are never modified. What remains to be done is read from the
folders themselves (lots in the root, ``OCR/``, ``Pending/``), so a run that
was interrupted simply resumes when launched again.
"""
from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import json
import shutil
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import script as barcode
from . import split_pages as split
from .classify_pages import discover_database
from .lot_catalog import CatalogLot, Ranges, Status, find_gaps, format_ranges, in_ranges, parse_ranges

BARCODE_SCRIPT = "classify_pages.py"
OCR_SCRIPT = "ocr_pages.py"
REVIEW_SCRIPT = "review_pages.py"
STAGE_LABELS = {
    BARCODE_SCRIPT: "Extraction des codes-barres",
    OCR_SCRIPT: "Extraction par OCR",
    REVIEW_SCRIPT: "Classification finale",
}

REPORTS = "Reports"
ARCHIVE = "Archive"
STATE_NAME = "pipeline_state.json"
INTERRUPTED_CODE = 130
# Copy + one-page split files: roughly twice the incoming PDFs, plus headroom.
DISK_SAFETY_BYTES = 2 * 1024**3
GIB = 1024**3
ANALYSIS_THREADS = 8   # opening PDFs on a network-like drive is mostly waiting

StageRunner = Callable[[str], int]


class PipelineError(RuntimeError):
    """A problem that must stop the run, worded for the person launching it."""


@dataclass(frozen=True)
class Config:
    source: Path
    root: Path
    workers: int = 4
    database: str | None = None
    search_list: str | None = None
    stable_seconds: float = 5.0
    barcode_seconds_per_page: float = 0.5
    ocr_seconds_per_page: float = 3.5
    ocr_share: float = 0.5
    already_done: Ranges = ()   # lots processed before this tool kept a registry


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PipelineError(
            f"Fichier de configuration introuvable : {path}\n"
            "Copiez config.example.toml vers config.toml et adaptez les chemins.") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PipelineError(f"Configuration illisible ({path.name}) : {exc}") from exc
    folders, processing, estimate = (data.get(name, {}) for name in ("dossiers", "traitement", "estimation"))
    for key in ("source", "racine"):
        if not isinstance(folders.get(key), str) or not folders[key].strip():
            raise PipelineError(f"config.toml : [dossiers] {key} est obligatoire.")
    workers = processing.get("processus", Config.workers)
    if not isinstance(workers, int) or isinstance(workers, bool) or workers <= 0:
        raise PipelineError("config.toml : [traitement] processus doit être un entier supérieur à zéro.")
    numbers = {
        "stable_seconds": processing.get("attente_stabilite", Config.stable_seconds),
        "barcode_seconds_per_page": estimate.get("codes_barres", Config.barcode_seconds_per_page),
        "ocr_seconds_per_page": estimate.get("ocr", Config.ocr_seconds_per_page),
        "ocr_share": estimate.get("part_ocr", Config.ocr_share),
    }
    for name, value in numbers.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise PipelineError(f"config.toml : valeur invalide pour {name} ({value!r}).")
    done = processing.get("deja_traites", "")
    try:
        if not isinstance(done, str):
            raise ValueError("une chaîne est attendue, par exemple \"1-82, 200-472\"")
        already_done = parse_ranges(done)
    except ValueError as exc:
        raise PipelineError(f"config.toml : [traitement] deja_traites : {exc}.") from exc
    return Config(
        source=Path(folders["source"]).expanduser(), root=Path(folders["racine"]).expanduser(),
        workers=workers, database=processing.get("bdd"), search_list=processing.get("liste"),
        already_done=already_done, **{name: float(value) for name, value in numbers.items()})


# --- registry of imported lots ------------------------------------------------

class Registry:
    """Which lots were copied, and which have been scanned, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = path
        self.lots: dict[str, dict] = {}
        if path.exists():
            try:
                self.lots = dict(json.loads(path.read_text(encoding="utf-8"))["lots"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise PipelineError(
                    f"Registre illisible : {path} ({exc}).\n"
                    "Aucune donnée n'a été modifiée. Renommez ce fichier pour repartir d'un registre vide.") from exc

    def record_import(self, name: str, pdf: str, size: int) -> None:
        self.lots[name] = {"pdf": pdf, "size": size,
                           "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                           "scanned_at": None}
        self.save()

    def mark_scanned(self, name: str) -> None:
        record = self.lots.setdefault(name, {"pdf": None, "size": None, "imported_at": None})
        record["scanned_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        barcode.atomic_text_write(
            self.path, lambda handle: json.dump({"version": 1, "lots": self.lots}, handle, indent=2))


def registry_path(config: Config) -> Path:
    return config.root / REPORTS / STATE_NAME


# --- locking, preflight, disk -------------------------------------------------

@contextlib.contextmanager
def run_lock(root: Path) -> Iterator[None]:
    """Refuse a second simultaneous run. The lock lives on the local disk, since
    file locks are unreliable on the Windows drives mounted under /mnt."""
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:10]
    with open(Path(tempfile.gettempdir()) / f"sped-pipeline-{digest}.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PipelineError(
                "Un traitement est déjà en cours sur ce dossier. Attendez qu'il se termine.") from None
        yield


def discover_search_list(root: Path, database: Path, explicit: str | None) -> Path:
    if explicit:
        path = root / explicit
        if not path.is_file():
            raise PipelineError(f"Liste de recherche introuvable : {path}")
        return path
    candidates = sorted(
        path for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in {".xlsx", ".xlsm"}
        and not path.name.startswith(("~$", ".")) and not path.stem.lower().endswith("_found")
        and path.resolve() != database)
    if len(candidates) != 1:
        found = ", ".join(path.name for path in candidates) or "aucun"
        raise PipelineError(
            f"Il faut exactement une liste de recherche Excel dans {root} (trouvé : {found}).\n"
            "Précisez son nom avec « liste » dans la section [traitement] de config.toml.")
    return candidates[0]


def preflight(config: Config) -> tuple[Path, Path]:
    """Validate the folders and workbooks; return (BDD, search list)."""
    for label, path in (("source", config.source), ("racine", config.root)):
        if not path.is_dir():
            raise PipelineError(
                f"Dossier {label} introuvable : {path}\n"
                "Vérifiez config.toml et que le disque est bien accessible.")
    source, root = config.source.resolve(), config.root.resolve()
    if source == root or source in root.parents or root in source.parents:
        raise PipelineError("Le dossier source et la racine de travail doivent être distincts et indépendants.")
    try:
        database = discover_database(
            root, root / config.database if config.database else None)
    except barcode.ConfigurationError as exc:
        raise PipelineError(f"BDD : {exc}") from exc
    return database, discover_search_list(root, database, config.search_list)


def check_disk_space(config: Config, plan: Plan) -> None:
    needed = 2 * plan.incoming_bytes + DISK_SAFETY_BYTES
    free = shutil.disk_usage(config.root).free
    if free < needed:
        raise PipelineError(
            f"Espace disque insuffisant sur {config.root} : {free / GIB:.1f} Go libres, "
            f"{needed / GIB:.1f} Go nécessaires. Libérez de l'espace puis relancez.")


# --- planning -----------------------------------------------------------------

@dataclass(frozen=True)
class LotInfo:
    name: str
    pdf: Path
    size: int
    pages: int


@dataclass
class Plan:
    incoming: list[LotInfo] = field(default_factory=list)
    waiting: list[LotInfo] = field(default_factory=list)   # copied earlier, not scanned yet
    skipped: list[tuple[str, str]] = field(default_factory=list)
    ocr_files: int = 0
    pending_files: int = 0

    @property
    def incoming_bytes(self) -> int:
        return sum(lot.size for lot in self.incoming)

    @property
    def scan_pages(self) -> int:
        return sum(lot.pages for lot in (*self.incoming, *self.waiting))

    @property
    def idle(self) -> bool:
        return not (self.incoming or self.waiting or self.ocr_files or self.pending_files)


def pdf_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [path for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf" and not path.name.startswith(".")]


def count_pages(pdf: Path) -> int:
    from pypdf import PdfReader
    reader = PdfReader(str(pdf), strict=False)
    try:
        return len(reader.pages)
    finally:
        split.close_pdf_reader(reader)


def lot_directories(directory: Path) -> list[Path]:
    try:
        return split.discover_lot_directories(directory)
    except split.ConfigurationError as exc:
        raise PipelineError(str(exc)) from exc


def build_catalog(config: Config, registry: Registry) -> list[CatalogLot]:
    """Every lot of the source folder with its status, highest number first.

    Only folder names are read, never the PDFs, so that this stays instantaneous
    however many lots the source holds.
    """
    in_root = {path.name for path in lot_directories(config.root)}
    archive = config.root / ARCHIVE
    archived = {path.name for path in archive.iterdir()} if archive.is_dir() else set()
    catalog = []
    for lot in lot_directories(config.source):
        name = lot.name
        if name in in_root:
            status = Status.IN_PROGRESS
        elif (registry.lots.get(name, {}).get("scanned_at") or name in archived
              or in_ranges(int(name), config.already_done)):
            status = Status.DONE
        else:
            status = Status.NEW
        catalog.append(CatalogLot(name, status))
    return sorted(catalog, key=lambda lot: split.natural_lot_sort_key(lot.name), reverse=True)


def new_lots(catalog: Sequence[CatalogLot]) -> list[str]:
    return [lot.name for lot in catalog if lot.status is Status.NEW]


def run_parallel(function: Callable, items: Sequence, tick: Callable[[], None]) -> list:
    """``function`` over ``items`` on several threads; results keep the order of ``items``."""
    pool = ThreadPoolExecutor(max_workers=ANALYSIS_THREADS)
    try:
        futures = [pool.submit(function, item) for item in items]
        for _ in as_completed(futures):
            tick()
        return [future.result() for future in futures]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # a Ctrl+C must not wait for the queue


def build_plan(config: Config, registry: Registry, lots: Sequence[str], *,
               sleep: Callable[[float], None] = time.sleep, pages_of: Callable[[Path], int] = count_pages,
               progress: Callable[[int, int], None] | None = None) -> Plan:
    """Decide what a run would do for the chosen ``lots``, without touching anything.

    Only the chosen lots (plus those already copied to the root) are opened.
    ``progress(done, total)`` is called as the inspection advances.
    """
    plan = Plan()
    chosen = sorted(lots, key=split.natural_lot_sort_key)
    waiting_dirs = lot_directories(config.root)
    total, done = 2 * len(chosen) + len(waiting_dirs), 0

    def tick() -> None:
        nonlocal done
        done += 1
        if progress:
            progress(done, total)

    def probe(name: str) -> tuple[Path | None, str | None, int]:
        pdf, error = split.discover_lot_pdf(config.source / name)
        if pdf is None:
            return None, error, 0
        try:
            return pdf, None, pdf.stat().st_size
        except OSError:
            return None, "PDF illisible", 0

    probes = run_parallel(probe, chosen, tick)
    candidates: list[tuple[str, Path, int]] = []
    conflicts: list[str] = []
    for name, (pdf, error, size) in zip(chosen, probes):
        if pdf is None:
            plan.skipped.append((name, error))
            continue
        record = registry.lots.get(name)
        if record is not None and record.get("size") is not None and (
                pdf.name != record["pdf"] or size != record["size"]):
            conflicts.append(name)
        candidates.append((name, pdf, size))
    if conflicts:
        raise PipelineError(
            "Ces lots ont changé dans le dossier source depuis leur import : " + ", ".join(conflicts)
            + ".\nUn lot déposé est censé être définitif. Aucun fichier n'a été modifié ; "
            "vérifiez-les avant de relancer.")
    for _ in plan.skipped:
        tick()
    if candidates:
        sleep(config.stable_seconds)   # a lot still being written changes size meanwhile

    def measure_incoming(candidate: tuple[str, Path, int]) -> LotInfo | str:
        name, pdf, size = candidate
        try:
            if pdf.stat().st_size != size:
                return "copie en cours dans le dossier source, réessayez plus tard"
            return LotInfo(name, pdf, size, pages_of(pdf))
        except OSError:
            return "PDF illisible"
        except Exception as exc:  # noqa: BLE001 -- a corrupt PDF must not block the other lots
            return f"PDF illisible ({type(exc).__name__})"

    def measure_waiting(directory: Path) -> LotInfo | str:
        pdf, error = split.discover_lot_pdf(directory)
        if pdf is None:
            return f"{error} (lot déjà dans la racine)"
        try:
            size = pdf.stat().st_size
        except OSError:
            return "PDF illisible (lot déjà dans la racine)"
        try:
            pages = pages_of(pdf)
        except Exception:  # noqa: BLE001 -- stage 1 will report the unreadable PDF itself
            pages = 0
        return LotInfo(directory.name, pdf, size, pages)

    incoming = run_parallel(measure_incoming, candidates, tick)
    waiting = run_parallel(measure_waiting, waiting_dirs, tick)
    for name, result in zip((candidate[0] for candidate in candidates), incoming):
        if isinstance(result, str):
            plan.skipped.append((name, result))
        else:
            plan.incoming.append(result)
    for directory, result in zip(waiting_dirs, waiting):
        if isinstance(result, str):
            plan.skipped.append((directory.name, result))
        else:
            plan.waiting.append(result)
    plan.ocr_files = len(pdf_files(config.root / "OCR"))
    plan.pending_files = len(pdf_files(config.root / "Pending"))
    return plan


def format_gaps(catalog: Sequence[CatalogLot], chosen: Sequence[str]) -> str | None:
    gaps = find_gaps(catalog, chosen)
    if not gaps:
        return None
    subject = f"le lot {gaps[0]} n'est pas traité" if len(gaps) == 1 else (
        f"les lots {format_ranges(map(int, gaps))} ne sont pas traités")
    return f"Attention : {subject} alors que les lots voisins le sont ou vont l'être."


def number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def plural(count: int, word: str) -> str:
    return f"{number(count)} {word}{'s' if count > 1 else ''}"


def lots_label(names: list[str]) -> str:
    return ", ".join(names) if len(names) <= 6 else f"{names[0]} → {names[-1]}"


def estimate_seconds(plan: Plan, config: Config) -> float:
    pages = plan.scan_pages
    barcode_time = pages * config.barcode_seconds_per_page / config.workers
    ocr_time = (plan.ocr_files + pages * config.ocr_share) * config.ocr_seconds_per_page
    return barcode_time + ocr_time


def format_estimate(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def format_plan(plan: Plan, config: Config) -> str:
    lines = [""]
    if plan.incoming:
        names = [lot.name for lot in plan.incoming]
        pages = sum(lot.pages for lot in plan.incoming)
        lines.append(f"Lots à importer : {plural(len(names), 'lot')} ({lots_label(names)}), "
                     f"{plural(pages, 'page')}")
    elif plan.idle:
        lines.append("Aucun lot à importer.")
    else:
        lines.append("Aucun lot à importer : seul le travail laissé par un lancement précédent sera repris.")
    for name, reason in plan.skipped:
        lines.append(f"Ignoré : lot {name} ({reason}).")
    if not plan.idle:
        lines.append(f"Durée estimée : environ {format_estimate(estimate_seconds(plan, config))} (approximatif).")
    return "\n".join(lines)


# --- execution ----------------------------------------------------------------

@dataclass
class StageResult:
    label: str
    code: int
    seconds: float


@dataclass
class Summary:
    skipped: list[tuple[str, str]] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    scanned: list[str] = field(default_factory=list)
    failed_lots: list[str] = field(default_factory=list)
    stages: list[StageResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review_added: int = 0
    review_total: int = 0
    left_ocr: int = 0
    left_pending: int = 0
    error: str | None = None
    interrupted: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not (self.error or self.interrupted)


def import_lots(config: Config, registry: Registry, lots: list[LotInfo], summary: Summary, log) -> None:
    """Copy each lot privately, check its size, then publish it with one rename."""
    for index, lot in enumerate(lots, 1):
        log(f"Copie du lot {lot.name} ({index}/{len(lots)})…")
        stage = config.root / f".import_{lot.name}"
        shutil.rmtree(stage, ignore_errors=True)   # leftover of an interrupted copy
        stage.mkdir()
        try:
            copied = stage / lot.pdf.name
            shutil.copy2(lot.pdf, copied)
            if copied.stat().st_size != lot.size:
                raise PipelineError(f"Lot {lot.name} : la copie ne correspond pas à l'original (taille différente).")
            stage.rename(config.root / lot.name)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        registry.record_import(lot.name, lot.pdf.name, lot.size)
        summary.imported.append(lot.name)


def archive_lot(config: Config, registry: Registry, name: str) -> None:
    """Retire a scanned working copy; the original stays in the source folder."""
    archive = config.root / ARCHIVE
    archive.mkdir(exist_ok=True)
    target = archive / name
    if target.exists():
        target = archive / f"{name}_{datetime.now():%Y%m%d_%H%M%S}"
    (config.root / name).rename(target)
    registry.mark_scanned(name)


def barcode_runs(root: Path) -> set[Path]:
    directory = root / REPORTS / "barcode"
    return {path for path in directory.glob("*") if path.is_dir()} if directory.is_dir() else set()


def read_barcode_run(root: Path, before: set[Path]) -> tuple[set[str], set[str]] | None:
    """(lots listed, lots published) by the barcode run created since ``before``."""
    created = sorted(barcode_runs(root) - before)
    if not created:
        return None
    try:
        with (created[-1] / "lots.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return None
    return ({row["Lot"] for row in rows}, {row["Lot"] for row in rows if row["Published"] == "True"})


def run_checked(script: str, run_stage: StageRunner, summary: Summary, log) -> int:
    """Run one stage; Ctrl+C aborts the pipeline, any code but 0/1 is fatal."""
    label = STAGE_LABELS[script]
    log(f"\n=== {label} ===")
    started = time.perf_counter()
    code = run_stage(script)
    summary.stages.append(StageResult(label, code, time.perf_counter() - started))
    if code == INTERRUPTED_CODE:
        raise KeyboardInterrupt
    return code


def scan_barcodes(config: Config, registry: Registry, run_stage: StageRunner, summary: Summary, log) -> None:
    lots = [path.name for path in lot_directories(config.root)]
    if not lots:
        return
    before = barcode_runs(config.root)
    code = None
    try:
        code = run_checked(BARCODE_SCRIPT, run_stage, summary, log)
    finally:
        # Even after an interruption, retire the lots that were fully published:
        # the stage cannot rescan them (it refuses to overwrite classified pages).
        outcome = read_barcode_run(config.root, before)
        published = outcome[1] if outcome else set()
        for name in sorted(published):
            archive_lot(config, registry, name)
        summary.scanned = sorted(published)
        summary.failed_lots = [name for name in lots if name not in published]
    label = STAGE_LABELS[BARCODE_SCRIPT]
    if code not in (0, 1) or (code == 1 and (outcome is None or set(lots) - outcome[0])):
        raise PipelineError(f"{label} : arrêt anormal (code {code}). Consultez les messages ci-dessus.")
    if summary.failed_lots:
        summary.warnings.append(
            f"{label} : lots non traités, conservés dans la racine pour le prochain lancement : "
            + ", ".join(summary.failed_lots))
    elif code == 1:
        summary.warnings.append(f"{label} : terminée avec des avertissements (pages envoyées en OCR, voir Reports/barcode).")


def run_follow_up(script: str, folder: str, config: Config, run_stage: StageRunner, summary: Summary, log) -> None:
    if not pdf_files(config.root / folder):
        return
    code = run_checked(script, run_stage, summary, log)
    label = STAGE_LABELS[script]
    if code not in (0, 1):
        raise PipelineError(f"{label} : arrêt anormal (code {code}). Consultez les messages ci-dessus.")
    if code == 1:
        summary.warnings.append(f"{label} : des pages en erreur sont restées dans {folder}/ pour le prochain lancement.")


def run_pipeline(config: Config, registry: Registry, plan: Plan, run_stage: StageRunner,
                 *, log: Callable[[str], None] = print) -> Summary:
    started = time.perf_counter()
    root = config.root
    summary = Summary(skipped=list(plan.skipped))
    review_before = len(pdf_files(root / "Review"))
    try:
        import_lots(config, registry, plan.incoming, summary, log)
        scan_barcodes(config, registry, run_stage, summary, log)
        run_follow_up(OCR_SCRIPT, "OCR", config, run_stage, summary, log)
        run_follow_up(REVIEW_SCRIPT, "Pending", config, run_stage, summary, log)
    except KeyboardInterrupt:
        summary.interrupted = True
    except (PipelineError, OSError) as exc:
        summary.error = str(exc)
    summary.review_total = len(pdf_files(root / "Review"))
    summary.review_added = summary.review_total - review_before
    summary.left_ocr = len(pdf_files(root / "OCR"))
    summary.left_pending = len(pdf_files(root / "Pending"))
    summary.seconds = time.perf_counter() - started
    return summary


def format_summary(summary: Summary) -> str:
    if summary.interrupted:
        status = "INTERROMPU"
    elif summary.error:
        status = "ARRÊTÉ SUR ERREUR"
    elif summary.warnings or summary.left_ocr or summary.left_pending:
        status = "TERMINÉ AVEC AVERTISSEMENTS"
    else:
        status = "TERMINÉ"
    lines = ["", "=" * 60, f"RÉSUMÉ : {status}", f"Durée totale : {barcode.format_duration(summary.seconds)}",
             f"Lots importés : {len(summary.imported)}",
             f"Lots analysés par code-barres : {len(summary.scanned)}"]
    for result in summary.stages:
        outcome = {0: "OK", 1: "avertissements", INTERRUPTED_CODE: "interrompue"}.get(result.code, f"erreur {result.code}")
        lines.append(f"  - {result.label} : {outcome} ({barcode.format_duration(result.seconds)})")
    lines.append(f"Dossier Review : +{number(summary.review_added)} page(s), {number(summary.review_total)} au total")
    if summary.left_ocr or summary.left_pending:
        lines.append(f"Restent à traiter : {summary.left_ocr} dans OCR/, {summary.left_pending} dans Pending/")
    for name, reason in summary.skipped:
        lines.append(f"Ignoré : lot {name} ({reason})")
    lines.extend(f"Attention : {warning}" for warning in summary.warnings)
    if summary.error:
        lines.append(f"ERREUR : {summary.error}")
    if summary.interrupted or summary.error:
        lines.append("Relancez le raccourci : le traitement reprendra là où il s'est arrêté.")
    lines.append("=" * 60)
    return "\n".join(lines)


def save_summary(config: Config, text: str) -> Path:
    directory = config.root / REPORTS / "pipeline"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now():%Y%m%d_%H%M%S}.txt"
    barcode.atomic_text_write(path, lambda handle: handle.write(text.strip() + "\n"), encoding="utf-8")
    return path

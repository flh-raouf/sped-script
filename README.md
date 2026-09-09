# Document digitization recovery

This local batch script renders every PDF page at 300 DPI, crops the rendered
page to its upper half, and sends only that crop to CodaraScan 0.2.0 with the
Panorama engine. Scanning is restricted to linear Code 39 and Code 128 formats
to avoid unnecessary 2D/QR detection. Confirmed decoded Badge and VIN values
are matched against the source workbook. The source is left untouched.
Badge identifiers may contain from one through five digits. They are preserved
as strings, including any meaningful leading zeroes.

## Install

Python 3.11 or newer is required.

```bash
python -m pip install -r requirements.txt
```

## Run

For the interactive menu, activate your Python environment, install the
requirements, and run:

```bash
python -m pip install -r requirements.txt
python main.py
```

On macOS/Linux you can also use `./main.py` with your virtual environment
activated. On Windows use `python main.py`.

The launcher stays at the project root. All processing utilities and their
shared reconstruction code live in `scripts/`; tests live in `test/`.

Select extraction, document reconstruction, hidden-file cleaning, or page
splitting with the arrow keys and Enter. Reconstruction then asks whether to
use identified pages only or local ranges. Enter the root folder (the last
folder is remembered for this session). Extraction asks for workers and, when
needed, the source workbook. Cleaning defaults to a dry-run preview; choose
deletion explicitly to remove files. Progress is displayed live, and you
return to the menu when finished. Ctrl+C cancels a prompt or interrupts an
operation. The launcher uses the same Python environment as the processing
scripts.

You can also run each script directly:

```bash
python scripts/script.py -d "/absolute/path/to/client_batch" -n 8
```

The root must contain one `.xlsx` or `.xlsm` workbook and immediate Lot
subdirectories. Every processable Lot must contain exactly one PDF. If the root
contains multiple possible source workbooks, select one explicitly:

```bash
python scripts/script.py -d "/absolute/path/to/client_batch" -n 8 --excel client.xlsx
```

The script writes:

- `<source>_found.xlsx` (or `.xlsm`), with found source rows filled green
- `results.csv`, sorted by numeric-aware Lot name and one-based page number;
  every attempted page has a row, including pages with no decoded identifier
  (failed pages remain identifiable through `processing.log` and `report.txt`)
- `report.txt`
- `processing.log`

A Badge and VIN decoded from the same page share that page's CSV row, including
when neither value exists in the source workbook. A missing Badge or VIN remains
blank; the script never fills it from the workbook or from another page.

## Second-stage PDF reconstruction

Two reconstruction entry points copy original PDF page objects directly. They
do not render or recompress pages:

```bash
python scripts/reconstruct_ranges.py -d "/absolute/path/to/client_batch"
python scripts/reconstruct_identified.py -d "/absolute/path/to/client_batch"
```

`reconstruct_ranges.py` creates `reconstructed_ranges/`. Within each Lot it
splits repeated appearances of a Badge/VIS document into local occurrences.
For each occurrence it includes every source page from its first identified
page through its last identified page. Blank pages remain inside an occurrence;
a decoded Badge/VIS belonging to another document separates two occurrences.
All local ranges are then merged into one output PDF in Lot/page order.

`reconstruct_identified.py` creates `reconstructed_identified_pages/`. It
includes only pages explicitly associated with the resolved Badge/VIS document.

Both scripts:

- read `results.csv` from the batch root
- infer Badge/VIS relationships only from stable one-to-one evidence
- resolve Badge-only and VIS/VIN-only detections through that mapping
- reject and report contradictory or unresolved mappings
- deduplicate identical `(Lot, page)` references per document
- process each referenced Lot once, in numeric-aware order
- merge the same document across Lots into one `<badge>_<vis>.pdf`
- write `reconstruction_report.txt` and `reconstruction.log` in the output folder

Outputs are assembled in a staging directory and verified before publication.
If the selected output folder already exists, it is preserved beside the new
one with a timestamped `_backup_...` name.

## Split Lot PDFs into individual pages

`split_pages.py` copies each original page directly into a separate one-page
PDF. It processes immediate numeric Lot folders in numeric order and writes
all files to one central `split_pages/` directory:

```bash
python scripts/split_pages.py -d "/absolute/path/to/client_batch"
```

The generated files use one-based names such as `17_1.pdf` and `17_2.pdf`.
The source PDF is not rasterized. A fresh output directory is published after
processing; an existing `split_pages/` directory is preserved as a timestamped
backup. `split_pages_report.txt` and `split_pages.log` are written in the root.

## Clean hidden files before processing

`clean_hidden_files.py` removes hidden files at depth two or greater while
leaving root-level hidden files, normal files, and hidden directories intact:

```bash
python scripts/clean_hidden_files.py -d "/absolute/path/to/client_batch" --dry-run
python scripts/clean_hidden_files.py -d "/absolute/path/to/client_batch"
```

Use `--dry-run` first to list eligible files without deleting anything.

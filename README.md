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

## Split and classify pages by VIS

Select **Split and classify pages by VIS** in `python main.py`, or run:

```bash
python scripts/classify_pages.py -d "/path/to/batch" -n 8
```

The consolidated BDD is supplied in `outputs/bdd-2024-2026/`. It contains
100,888 source rows for 2024, 2025 and 2026, with `Année` as the first column
and all original data columns retained. `GlobalCounters` and `Filters` from
the 2024 workbook are metadata, not vehicle records, and are excluded.

Put `BDD_2024_2025_2026.xlsx` and the search list (the existing 229 requested
Badge/VIS rows) directly inside the selected batch root. The menu and CLI
automatically detect the BDD `.xlsx` whose name starts with `BDD` and exclude
it when discovering the search list. Hidden files, Excel lock files and
`_found` workbooks are ignored. No BDD path prompt is needed in the menu.
Missing or multiple BDD files produce a clear error. To use a BDD stored
elsewhere or choose between several files, supply `--database /path/to/BDD.xlsx`
on the CLI; `--excel search_list.xlsx` selects an ambiguous search list.
Classification applies to every page, including
vehicles absent from the search list.

Immediate numeric lot folders are processed in numeric order, one lot at a
time. Each lot must contain exactly one PDF. All pages are first split into
verified one-page PDFs, then scanned using the existing 300 DPI upper-half
CodaraScan Panorama Code 39/Code 128 pipeline. Only this new operation also
accepts six-digit badges. Barcode detection recognises only badges and full
17-character VINs; VIS is extracted from the last eight VIN characters or
resolved through the BDD badge mapping. Other barcode payloads are ignored.
The original extraction operation retains its existing identifier rules.

Outputs are centralised under the batch root:

```text
output/
  T5702376/
    479_1.pdf
    500_12.pdf
  OCR/
    479_2.pdf
  results.csv
  pages.xlsx
  search_list_found.xlsx
  report.txt
  processing.log
  lots.csv
  lot_reports/
    479/
      pages.xlsx
      pages.csv
      results.csv
      report.txt
```

The folder identifier is the VIS (the last eight VIN characters), never the
full VIN. A badge-only page uses the BDD badge-to-VIS mapping before falling
back to `OCR`. Contradictory mappings or multiple distinct VIS values send
the page to `OCR` with an explicit CSV status; no arbitrary VIS is chosen.
The CSV distinguishes VIN-derived VIS from the resolved VIS, records
the destination and any scan error, and retains one row per processed page.
`FOUND` and green highlighting use the existing search list, including VIS
resolved through the BDD. OCR is a holding folder; this operation does not
run text recognition or infer identifiers from neighbouring pages.

`pages.xlsx` contains one row per processed page, sorted by numeric lot and
page, with columns `lot_page`, `BDG`, `VIN`, `VIS`, `SEQ`, `SEQ_9`, and `NOF`.
The page identifier omits the PDF extension, for example `479_1`. Resolved
pages retain decoded Badge/VIN values and query the BDD to fill the other
fields. In the supplied sources, `SEM` supplies the full `SEQ`, and valid
17-character VINs are available in `ID2` (explicit VIN and `ID1`/`ID3` columns
are supported too). `SEQ_9` contains the last nine digits of the full SEQ,
stored as text to preserve leading zeroes. SEQ is not searched in barcodes.
Missing source fields remain blank; no VIN is fabricated from a VIS. If a
lookup leaves multiple possible values for a field, that field remains blank.
Pages sent to `OCR`, including scan failures and contradictory detections,
retain their `lot_page` row with all six identifier fields blank. These blank
rows indicate unresolved pages, independently of membership in the 229-row
search list. The CSV retains raw detections and reasons for investigation.

The supplied 2025 and 2026 BDD exports have no NOF column, so NOF remains blank
for those records. The new report has a frozen header and column filters.

Every lot starts its own timer before PDF discovery and splitting. Its
duration includes scanning, per-lot Excel/CSV export, PDF publication and
updating the cumulative CSV and highlighted search list. `processing.log`
records lot start, split, scan, publication and finish events, with page counts,
VIS/OCR totals and elapsed time. Failed and interrupted lots also have a
status and duration. `lots.csv` lists every attempted lot with its start,
finish, duration in `HH:MM:SS` and seconds, page counts and publication status.
`report.txt` includes the lot durations and batch status and updates after
each lot. The whole-batch timer starts before BDD loading and scanner
initialization and includes the final combined Excel export.

Output is published **lot by lot**. During a lot's scan, its files stay in a
private working directory inside `output/_incomplete/`. Once the whole lot
has been processed and its metadata saved, its PDFs are moved to the central
VIS/OCR folders and its Excel/CSV files become available in
`output/lot_reports/<lot>/`. The script saves `results.csv`, search-list
highlighting and the batch summary before starting the next lot. It never
clears completed lots when a later lot fails or the batch is interrupted.
An individual lot failure is logged and processing continues with the next
lot; an interruption stops the batch. Unfinished lot files are retained under
`_incomplete/`, with their location logged. If publication fails midway,
already moved files remain in the central folders and the remaining files
and metadata stay in that lot's working directory.

The combined `output/pages.xlsx` is streamed from the saved per-lot CSVs once
at batch end, avoiding repeated rewrites of a growing workbook. All per-lot
Excel reports are already saved before that final export, so an interruption
or export failure does not lose completed lots' identifiers or PDFs. There
is no automatic resume or skip mechanism.

Original PDFs and workbooks are untouched. Before lot processing starts,
an existing `output/` is preserved as a timestamped `output_backup_...`
directory and a fresh `output/` is opened for progressive results. Validation
failures before processing leave the old output in place. Skipped lots, scan
failures and contradictions are reported with a nonzero exit code.

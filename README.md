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

Select **Extraction des codes-barres**, **Extraction par OCR (PP-OCRv6 tiny)**,
**Classification finale (tableaux de pièces et versos peu renseignés)**, or
**Opérations complémentaires** with the arrow keys and Enter. The latter opens
the hidden-file cleanup and PDF page-splitting options. Enter the root folder
(the last folder is remembered for this session). Barcode extraction asks for
workers and, when needed, the source workbook. Cleaning defaults to a dry-run
preview; choose deletion explicitly to remove files. Progress is displayed
live, and you return to the menu when finished. Ctrl+C cancels a prompt or
interrupts an operation. The launcher uses the same Python environment as the
processing scripts.

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

## Three-stage classification: select the same parent folder

Use the same parent path for all three menu actions, in this order:

1. **Extraction des codes-barres**: numbered lots → `Output/<VIS>/` or `OCR/`.
2. **Extraction par OCR (PP-OCRv6 tiny)**: `OCR/` → `Output/<VIS>/` or `Pending/`.
3. **Classification finale (tableaux de pièces et versos peu renseignés)**: `Pending/` → `Output/<VIS>/` or final `Review/`.

```text
Parent/
  1/                         Original lots, unchanged
  2/
  BDD_2024_2025_2026.xlsx
  search_list.xlsx
  Output/<VIS>/              All classified pages
  OCR/                       Awaiting OCR
  Pending/                   Awaiting tables/verso processing
  Review/                    Final pages for human review
  Reports/
    barcode/<timestamp>/     Per-lot and batch reports
    ocr/<timestamp>/         OCR reports
    review/<timestamp>/      Remaining-page reports
    .ocr_cache.sqlite3       Shared persistent raw OCR cache
    review_assignments.jsonl Durable classification provenance
```

```bash
python scripts/classify_pages.py -d "/path/to/Parent" -n 8
python scripts/ocr_pages.py -d "/path/to/Parent"
python scripts/review_pages.py -d "/path/to/Parent" --cache-only
```

Only the designated input is scanned at each stage. Missing `OCR` or `Pending`
folders explain which previous stage to run; empty inputs return successfully
with “Nothing left to process.” Transfers preserve names and existing VIS folders.
Barcode execution saves reports in a new timestamped folder and refuses PDF
collisions before publishing a lot; it does not clear earlier results. Failed
transfers retain the source for retry and appear in the stage's report.

For existing executions using the old folder structure, add `--legacy-layout`
to each script. Barcode then retains its original `output/` layout; OCR and
remaining-page scripts take the direct PDF folder and default VIS destination
as before. No existing files are migrated automatically. An old cache can also
be explicitly selected using `--cache`.

## Extraction des codes-barres

Select **Extraction des codes-barres** in `python main.py`, or run:

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

Classified pages are centralised under `Parent/Output/<VIS>/`, with unresolved
pages in `Parent/OCR/`. Barcode reports, per-lot workbooks, search-list
highlighting, logs and private unfinished work are saved in
`Parent/Reports/barcode/<timestamp>/`.

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
private working directory inside `Reports/barcode/<timestamp>/_incomplete/`. Once the whole lot
has been processed and its metadata saved, its PDFs are moved to the central
VIS/OCR folders and its Excel/CSV files become available in
`Reports/barcode/<timestamp>/lot_reports/<lot>/`. The script saves `results.csv`, search-list
highlighting and the batch summary before starting the next lot. It never
clears completed lots when a later lot fails or the batch is interrupted.
An individual lot failure is logged and processing continues with the next
lot; an interruption stops the batch. Unfinished lot files are retained under
`_incomplete/`, with their location logged. If publication fails midway,
already moved files remain in the central folders and the remaining files
and metadata stay in that lot's working directory.

The combined `Reports/barcode/<timestamp>/pages.xlsx` is streamed from the saved per-lot CSVs once
at batch end, avoiding repeated rewrites of a growing workbook. All per-lot
Excel reports are already saved before that final export, so an interruption
or export failure does not lose completed lots' identifiers or PDFs. There
is no automatic resume or skip mechanism.

Original PDFs and workbooks are untouched. Existing classified PDFs are never
overwritten. Skipped lots, scan failures and contradictions are reported with a
nonzero exit code. The backup-and-replace behavior of the old `output/` folder
is retained only with `--legacy-layout`.

## Recover unresolved pages with cached OCR

Install the additional OCR dependencies in the environment used to run the script:

```bash
python -m pip install -r requirements-ocr.txt
python scripts/ocr_pages.py -d "/path/to/Parent"
```

Supply the parent root used for barcode classification. Only immediate PDFs
inside its `OCR/` folder are processed, and each must contain exactly one page.
Identified pages join `Output/<VIS>/`; unresolved pages move to the sibling
`Pending/` folder. `--output` overrides the VIS destination. The consolidated
`BDD*.xlsx` is detected in the parent root; `--database` overrides discovery.
Multiple candidate BDD workbooks require an explicit selection. This operation
is also available from `python main.py`.

OCR uses `PP-OCRv6_tiny_det` and `PP-OCRv6_tiny_rec`, on CPU by default,
with full-page rendering at 200 DPI. Optional `--dpi` and `--device` change
these settings. Paddle downloads the two models on first use and reuses its
local model cache afterwards. Internet access is needed for that first download.
MKL-DNN (oneDNN) acceleration is disabled to avoid Paddle's
`ConvertPirAttribute2RuntimeAttribute` / `DoubleAttribute` inference failure.
This workaround also applies to the review stage, which shares the OCR engine.

The BDD is indexed once in memory by full VIN, VIS, badge, and sequence, rather than
scanning all Excel rows for every page. A recognised 17-character VIN must
match the BDD. A badge or nine-digit sequence is accepted when linked spatially
to its label (`BDG`/`Badge` or `SEQ`/`Séquence`). Without labels, a badge and
sequence must corroborate each other. Operator badges are excluded. The default
minimum OCR score is 0.8 (`--min-score` overrides it). A directly printed
eight-character VIS is also accepted when it is a complete token and exists
in the BDD, with or without a `VIS` label. Substrings of longer VINs or SEL
codes are not accepted as direct VIS values. Missing metadata is enriched from
the matching BDD records. Contradictory or ambiguous matches
go to `Pending`; the script does not guess or make fuzzy replacements of digits.

Sequence lookup includes the last nine digits of the BDD sequence. The supplied
example also uses a printed format different from those last nine digits:
`SEQEMON0118300161` in year 2026 appears as `126183161`. For this exact
`SEQEMON` format and a known BDD year, an additional document key is indexed:
last site digit + two-digit year + three-digit day + last three order digits.
Other formats receive no such conversion. Any key shared by different VIS values
remains ambiguous unless another reliable identifier resolves it. `SEQ` in the
output retains the complete BDD value and `SEQ_9` retains its last nine digits;
the detected printed sequence is recorded separately as `SEQ_DOC` in the CSV.
The barcode algorithm is unchanged.

Raw OCR results (text, bounding boxes, and confidence scores) are saved after
each successful page in `Parent/Reports/.ocr_cache.sqlite3`. The cache key combines
the PDF's SHA-256 content hash with the models, rendering settings and runtime
versions. Moving or renaming an unchanged PDF still permits a cache hit; changed
PDF content, DPI, device or library versions trigger OCR again. Successful empty
results are cached too; OCR errors are not. BDD lookup and classification run
again on every execution, including cache hits, so updating the BDD does not
require another OCR pass. `--cache "/path/to/cache.sqlite3"` selects a different
cache file. Deleting the cache forces fresh OCR without altering the PDFs.

Pages keep their `lot_page.pdf` names. By default, each page is moved immediately
after classification to its VIS folder or `Pending`; use `--copy` to retain the
input PDFs. A different existing PDF with the same destination filename is never
overwritten, and the source is retained on transfer failure. Byte-identical
existing destination files can be reused. Existing VIS folders are preserved.

Each execution creates `Parent/Reports/ocr/<timestamp>/` with:

- `results.csv`: progressively saved page metadata, decisions, cache hits,
  individual durations, source/destination paths and errors.
- `transfers.jsonl`: a durable mapping written before each transfer, so the
  destination and identified metadata remain available after an interruption.
- `processing.log` and `report.txt`: folder start, page progress, totals,
  elapsed time, cache hits, errors and final or interrupted status.
- `pages.xlsx`: the final seven-column page report (`lot_page`, `BDG`, `VIN`,
  `VIS`, `SEQ`, `SEQ_9`, `NOF`), enriched from the matching BDD records. Unknown
  pages retain a row with blank identifiers. Conflicting metadata fields stay
  blank. The progressive CSV and transferred PDFs remain if final Excel export
  cannot finish.

This uses normal `pathlib` paths and can be launched on Windows with the same
Python commands; install the Paddle dependencies for the Windows environment.
The implementation has been exercised on macOS; Windows execution has not been
tested here. It does not update the earlier barcode reports or search-list
highlighting: its own OCR reports record the recovered pages.

## Classification finale (tableaux de pièces et versos peu renseignés)

The menu action **Classification finale (tableaux de pièces et versos peu renseignés)** runs
`scripts/review_pages.py`. Supply the same parent root; it reads immediate PDFs
from `Pending/`, sends identified pages to `Output/<VIS>/`, and moves unresolved
pages to the sibling final `Review/`. BDD discovery, tiny models and the OCR
cache are shared with the OCR stage. Classification rules are unchanged.

```bash
python scripts/review_pages.py -d "/path/to/Parent"
# Preview decisions using only existing OCR results:
python scripts/review_pages.py -d "/path/to/Parent" --cache-only --dry-run
```

The script first recognizes the parts-table layout: at least three aligned
ten-digit part references paired with OK/NOK cells and a single badge in the
header above them. A reference such as `98749524F4` or `98749524PR` is optional. It uses cached text,
confidence scores and coordinates in all four orientations. The badge must map
to exactly one VIS in the BDD. A random five-digit number or a different kind
of table does not satisfy this rule. Default minimum confidence is 0.9.

Other pages use a Review-specific BDD lookup. Badges may be accepted without
`BDG` when they occur in an `Identifiant` field, next to an identifier label,
or in the badge area of a VIN/Sequence vehicle header. Arbitrary numeric cells
(such as torque measurements) cannot identify a page merely by matching the BDD.
Full VIN/VIS values and labelled sequences also participate in this lookup.
All accepted identifiers must agree. A unique match classifies the page regardless
of its ink coverage or preceding page; conflicting or ambiguous matches remain
unresolved. A missing or unknown table reference does not cancel a badge identified
through another supported context. These additional rules apply only to Review;
the OCR stage retains its label/corroboration requirements.

Pages without a BDD identifier can inherit the exact preceding page's VIS within the same lot only
if their dark-pixel coverage is at most 0.9%, their predecessor was independently
classified and their OCR contains no conflicting identifier. Coverage is
measured at 100 DPI with grayscale values below 128, after excluding 2% from
each of the four edges to reduce scanner borders. The percentage uses the
remaining interior as its denominator. Only the measurement image is cropped;
PDFs and full-page OCR/cache remain unchanged. Override the limits with
`--max-ink-percent`, `--black-threshold` and `--crop-percent` (0 disables cropping).
Large scanner artifacts extending into the interior can still exceed the cutoff.
This is a conservative sparse-page
heuristic, not proof that a page is blank. Inherited pages never become eligible
predecessors, including on subsequent executions. Missing or ambiguous
predecessors, unknown table badges and other undecidable pages move to final `Review/`. Transfer/OCR errors retain the source in `Pending/` for retry.

Cache misses run OCR and save successful raw results immediately. `--cache-only`
keeps misses in Review without running OCR or attempting inheritance. Cache
settings and runtime versions must match the original OCR execution to get
hits. `--cache`, `--database`, `--output`, `--dpi`, `--device`, `--min-score`,
and `--copy` are also supported. Dry runs write preview reports and may populate
the OCR cache unless `--cache-only` is supplied; they never move PDFs.

Each recovered PDF moves immediately into its existing VIS folder, preserving
its `lot_page.pdf` name. Different existing destination PDFs are never
overwritten. `Reports/review_assignments.jsonl` durably records metadata and provenance
before transfers, preventing inheritance chains after interruptions. Each
execution creates `Reports/review/<timestamp>/results.csv`, progressively flushed
after every page, with metadata, method, predecessor, ink coverage, cache status,
page duration and errors. `processing.log` and `report.txt` record the folder's
start and total duration. `pages.xlsx` contains all input pages with the seven
metadata columns, including blank identifiers for unresolved pages. Historical
barcode/OCR reports remain unchanged; these Review reports describe this stage.

# Extraction QC

`extraction_qc.py` checks that an extraction was done correctly. For every row of an extracted-values CSV it
opens the relevant and the original parquet file, goes to the recorded line, and confirms that the recorded
`SourceElementPath` is there with the recorded `Value`.

It only verifies what was extracted. It does not check that nothing was missed, and it does not look at
`domain.csv`, `EntityRole`, `NormalizedPath`, `CanonicalField` or `DataType` (they are copied through).

## Input files

Put these three files in one folder. They share a hash prefix (`<md5>` below):

| File | Content |
|---|---|
| `<md5>_extracted.csv` | One row per extracted value. Needs at least `SourceLine`, `RelevancyParquetLine`, `SourceElementPath`, `Value`. |
| `<md5>_original.parquet` | The original rows. Each row holds one DynamoDB JSON document as text in one column. |
| `<md5>_relevant.parquet` | The relevant rows only, in the same layout. |

The files are found **by name** in the folder. The `SourceFilePath` and `RelevancyFileLocation` columns are not
used, so they may point at another machine. If the folder holds several `*_extracted.csv` files, each one is
checked against its own two parquet files; a problem with one file is reported by name and the others are still
checked.

The extracted CSV can be UTF-8 (with or without a byte order mark) or Windows-1252, which is what Excel's
"CSV (Comma delimited)" writes: the script reads the whole file once to find out, and says so when it is not
UTF-8. Fields can be of any length. Blank lines are not rows: they are skipped and not counted in `--rows` /
`--limit` numbers. A required column that appears twice in the header stops the run.

## Run

Run it from the folder that holds the files (or give `--folder`):

```
python extraction_qc.py
```

It needs `pyarrow` (`pip install pyarrow`) and Python 3.8 or newer.

| Switch | Effect |
|---|---|
| `--folder PATH` | Folder holding the input files (default: the current folder). |
| `--skip-original-on-relevant-failure` | Do not check the original file for rows that already failed the relevant file. They get `Skipped`. |
| `--check-same-document` | Also require each relevant document to be contained in the original document at `SourceLine`, see below. |
| `--limit N` | Check only the first N data rows of the extracted CSV (a trial run, see below). |
| `--rows 17,203` | Check only these 1-based data rows of the extracted CSV (a trial run). |
| `--original-column NAME`, `--relevant-column NAME` | Name the column that holds the JSON text, when automatic detection cannot decide. The column must be text (string or binary, dictionary-encoded is fine). |
| `--csv-encoding NAME` | Text encoding of the extracted CSV, when the automatic choice (UTF-8, else Windows-1252) is wrong. |
| `--temp-dir FOLDER` | Where the working files of a run go (default: the system temporary folder). |
| `--fail-on-wrong` | Exit with code 3 when every file was checked but some rows are `Wrong`. |
| `--debug`, `--show-values` | Diagnostic report, see the debug section below. |

The extracted CSV is never modified. Progress goes to stderr and the summary to stdout. Characters that the
console cannot show are printed as `\u` escapes instead of stopping the run.

**Exit code.** 0: every file was checked, whatever the verdicts. 1: at least one file could not be checked (the
message names it). 3: everything was checked but some rows are `Wrong`, only with `--fail-on-wrong`. 2: a
command-line mistake.

**Working files.** Each run writes one small result file per parquet file to the temporary folder and removes them
at the end: about 12 bytes per correct row and a few hundred per wrong row, per file. Four million wrong rows is
roughly a gigabyte, so use `--temp-dir` if the system drive is short of space.

## Output

`<md5>_verified.csv` has every column and row of the extracted CSV, unchanged and in the same order, followed by
the columns below. It is written as UTF-8, with a byte order mark when the input was not plain UTF-8, so that Excel
shows every character.

| Column | Meaning |
|---|---|
| `RelevantFileVerification` | `Correct` or `Wrong`: is `SourceElementPath` with `Value` at `RelevancyParquetLine` of the relevant file? |
| `RelevantFileReason` | Empty when `Correct`; otherwise a reason code and details. |
| `OriginalFileVerification` | `Correct`, `Wrong` or `Skipped`: the same check at `SourceLine` of the original file. |
| `OriginalFileReason` | Empty when `Correct`; otherwise a reason code and details. |
| `OverallVerification` | `Correct` only when both checks are `Correct`; `Wrong` otherwise (a `Skipped` check counts as not correct). |

The two checks are independent: a row can be `Correct` in the relevant file and `Wrong` in the original, which
tells you the fault is in the original line number or in how the relevant file was built. With
`--skip-original-on-relevant-failure` the original check is not run for rows that failed the relevant check.

At the end the run prints how many rows were checked, the `Correct` / `Wrong` / `Skipped` counts for each file and
overall, and how many rows there are per reason code.

**Trial runs.** With `--limit` or `--rows` the rows go to `<md5>_verified_trial.csv`, never to
`<md5>_verified.csv`, so a quick look at 2,000 rows cannot overwrite the result of a full run.

**A locked output.** If `<md5>_verified.csv` is open in Excel the run stops before checking anything and says so.
If it becomes locked during the run, the finished results are kept in `<md5>_verified.csv.partial` and the message
names it: close the other program and rename the file.

## Reason codes

A reason starts with exactly one of these codes, followed by details:

| Code | Meaning |
|---|---|
| `LINE_OUT_OF_RANGE` | The line number is before the first row or after the last row of the file. |
| `INVALID_LINE` | The line number is blank or not a whole number. |
| `UNSUPPORTED_PATH` | `SourceElementPath` cannot be read as a path of keys and array indexes. |
| `INVALID_JSON` | The parquet row at that line does not hold valid JSON (or does not hold text at all). |
| `PATH_NOT_FOUND` | The row is fine but the path does not exist in it. The details say where the walk stopped. |
| `VALUE_MISMATCH` | The path exists but holds a different value. The details show the expected and the found value. |
| `FORMAT_CHANGED` | The path holds the same value in a different format (see below). |
| `DOCUMENT_MISMATCH` | Only with `--check-same-document`: the relevant document is not contained in the original document at `SourceLine`. The details give the first path that differs. |
| `SKIPPED` | The check was not run because of `--skip-original-on-relevant-failure`. |

When a check fails, the lines immediately before and after the recorded line are also looked at. If the path and
value are found there, the reason ends with `FOUND_AT_LINE_<n>`: a quick way to spot an off-by-one in the line
numbers.

## How a row is matched

- **Lines** are 1-based row numbers: line 1 is the first row of the parquet file.
- **The path** is a JSONPath such as `$.Item.scopeIds[0].enrollSettings[3].settings.agencyTIN`; a key can also be
  written in brackets, `$.Item['a.b']` or `$.Item["a.b"]`. The documents are DynamoDB JSON, so the type wrappers
  (`M`, `L`, `S`, `N`, `BOOL`, `NULL`, sets) are looked through. Plain JSON documents work as well.
- **The value is compared strictly**, as text, including spaces and letter case:
  - a string is compared as stored;
  - a number is compared as its stored text, so `12345.0` and `12345` are different;
  - a boolean is `true` or `false`;
  - null is empty text;
  - a map or list matches when `Value` is JSON of the same structure (spacing and key order do not matter), where
    a number is never equal to a string: `{"a":1}` matches a source `{"a":{"N":"1"}}` but not `{"a":{"S":"1"}}`.
- **A format change is wrong, but labelled.** If a value does not match but would match when ignoring surrounding
  spaces, letter case, number formatting (`007` and `7`, `1e3` and `1000`) or date formatting (`2024-01-25` and
  `01/25/2024`), the reason is `FORMAT_CHANGED` instead of `VALUE_MISMATCH`. The verdict is `Wrong` either way.
- **Bad rows never stop the run.** A row with an invalid line number or path is marked `Wrong` with a reason and
  the next row is checked.
- **The two checks are independent, and that has a blind spot.** A wrong `SourceLine` that lands on a different
  original document holding the same value (`true`, an enum code) still passes. `--check-same-document` closes it:
  for every pair of lines it requires every value of the relevant document to be present, at the same path, in the
  original document (lists compared by index; a shorter relevant list is fine, a longer one is not). Use it only if
  the relevant rows really are subsets of the original rows. If they are not, for example because arrays were
  compacted, every row fails with `DOCUMENT_MISMATCH` and the first differing path shows why. It parses a second
  document per row pair, so it is slower.

## Debug mode (for a machine whose data you cannot share)

When the first real run does not behave as expected, run it again with `--debug` on a slice of the file:

```
python extraction_qc.py --debug --limit 2000
```

The verified CSV is written as usual (and is identical to a run without `--debug`). In addition:

- A **paste block** is printed at the end of the run: at most 50 lines of at most 120 characters, so it can be
  copied out of a restricted machine as a small amount of text. It never contains a real value.
- A fuller report is written to `<md5>_debug_report.txt` in the same folder: every path shape, up to 10 failure
  traces per reason code (at most 50 per file), the full skeleton.

**Masking.** Wherever sample text is needed it is shown as a shape: uppercase letters become `A`, other letters
`a`, digits `9`, everything else is kept. `2024-01-25` shows as `9999-99-99`, `alice` as `aaaaa`, `True` as `Aaaa`.
Keys and paths are structure and are shown as they are, but only when a key looks like an identifier (letters,
digits and underscores, at most 40 characters, not starting with a digit, no run of three or more digits). A key
that does not, such as an email address, a UUID or `user12345`, is masked like a value, so a map keyed by ids does
not put data in the block. This is a heuristic: a data-like key that happens to look like an identifier (for
example `alice`) would still show, so read the block before you copy it out. The report file is masked the same
way unless you add `--show-values`; that switch changes only the report file, never the printed block.

To look at particular rows, give their 1-based data row numbers: `python extraction_qc.py --debug --rows 17,203`.
`--limit` and `--rows` restrict the verified rows and the report to the rows checked; the verified rows go to
`<md5>_verified_trial.csv`.

If something fails unexpectedly in one file, the message names the file; add `--debug` to get the traceback too.

Sections of the paste block, and what to look for:

| Section | What it shows | What to look for |
|---|---|---|
| `-- input` | pyarrow version, rows, row groups and the JSON column of each parquet file; CSV row count and whether its rows are in ascending `SourceLine` / `RelevancyParquetLine` order; correct counts per file | The wrong JSON column; a CSV that is not in line order (the run is then slower). |
| `-- json skeleton` | The structure of the first 200 documents of each file: `Item.attribute:type`, with the DynamoDB type tags. Nested maps with more than 20 keys show as `{*}`; the item's own attribute names are always listed | Whether the two files have the same structure; paths that cannot exist. |
| `-- path shapes` | `SourceElementPath` with array indexes replaced by `[*]`, worst 8 first: rows, rows correct in each file, and the most common reason where some fail | A shape that is correct in the relevant file but never in the original (`orig=0 [orig PATH_NOT_FOUND]`): the array indexes or the path differ between the two files. |
| `-- reasons` | Rows per reason code for each file | Which kind of failure dominates. |
| `-- value shapes by source type` | For each type found at the paths (`S`, `N`, `BOOL`, `NULL`, `M`, `L`): rows and the most common masked shapes of `Value` | How booleans, nulls, numbers and maps are written by the extraction (`Aaaa` means `True`, `aaaa` means `true`). |
| `-- DataType / source type` | The `DataType` column against the type found in the document | A `date` column whose source is a string means dates are being reformatted. |
| `-- failure traces` | Up to 3 failing rows with different reasons: row number, file, line, where the path walk stopped, and masked expected and found values with their lengths | The concrete reason behind the most common failures. |

## Large files

Nothing is loaded whole: the CSV is streamed and each parquet file is read in small batches, one row group at a
time, so memory does not grow with the number of rows. It is bounded by the size of one row group (the pages of
the row group being read), so a file written with very large row groups needs more memory. Each JSON document
is parsed once per check, however many CSV rows point at it and even when its neighbours are examined for the
`FOUND_AT_LINE` hint (the last few parsed documents are kept for that).

Rows are taken in chunks of 50,000 and visited in line order, so a CSV that is sorted by `SourceLine` (and by
`RelevancyParquetLine`) is checked in one forward pass over each parquet file. An unsorted CSV gives exactly the
same results but needs one extra pass over the parquet file per chunk, so it runs slower. If your CSV is not in
line order, sort it first.

### Measured

Generated data (not kept in the repository): documents of about 7.6 KB in DynamoDB JSON, row groups of 20,000
documents, every second document relevant, 8 extracted values per relevant document, about 1% of the values wrong.
Windows 11, 8 CPU cores, Python 3.13, pyarrow 25.0.1, one process.

| Original rows | Extracted CSV rows | Original parquet | Wall time | Peak memory |
|---|---|---|---|---|
| 100,000 | 400,000 | 76 MB | 24 s | 156 MB |
| 1,000,000 | 4,000,000 | 768 MB | 4.2 min | 177 MB |
| 100,000, CSV in random order | 400,000 | 76 MB | 67 s | 188 MB |

Peak memory is the same at both sizes. The random-order run produced identical verdicts, about 2.8 times slower; an
unsorted CSV at 1,000,000 rows was not measured and would be much slower than that, since the extra passes grow
with both the file size and the number of chunks. Your machine and files will differ, so treat these as orders of
magnitude: repeated runs differ by about 10%.

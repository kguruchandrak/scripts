# Proposal

## Why

The QC script expects every file of a run in one folder, named `<md5>_extracted.csv`, `<md5>_original.parquet` and `<md5>_relevant.parquet`. The extraction pipeline will instead deliver them into four folders keyed by md5 alone, and the extracted file may arrive as parquet as well as CSV. Re-running over a folder of finished md5s also repeats every check, though results for unchanged inputs already exist.

## What Changes

- Add a folder layout under the run folder: `extracted/<md5>.csv` or `extracted/<md5>.parquet`, `original/<md5>.parquet`, `relevant/<md5>.parquet`, with the result in `verified/<md5>_verified.csv`. `--folder` moves the whole layout; `verified/` is created when missing. The trial output, debug report and `.partial` file of an md5 go in `verified/` as well.
- Keep the flat `<md5>_*` layout as a fallback: when an `extracted/` folder exists the folder layout is used (flat files are ignored, with a note); otherwise behaviour is exactly as today.
- Accept an extracted file in either format, chosen by extension. When both exist for one md5 the newer file is used and the output says which; equal timestamps stop that md5 as ambiguous. The flat layout stays CSV-only.
- Read an extracted parquet one row group at a time. Require the four required columns (duplicated or missing ones stop that md5 as for CSV), require `SourceElementPath` and `Value` to be text, accept integer, float or text line numbers, number rows from 1 in file order, and write every other cell into the verified CSV as text by fixed rules (null empty, booleans `true`/`false`, timestamps ISO-8601 with a time zone written as the UTC instant, maps and structs JSON objects, lists JSON), using nothing beyond `pyarrow`.
- The verified output is always a CSV, whatever the extracted format. The same rows supplied as CSV or as parquet must give an identical verified result.
- In the folder layout, skip an md5 whose `verified/<md5>_verified.csv` is newer than all three inputs and than the script itself, list each skipped md5, and add `--force` to recheck everything. Trial runs are never skipped, a `.partial` file never counts as a result, and skips do not change the exit code, except that with `--fail-on-wrong` a skipped md5 counts by the `Wrong` rows of its verified file.
- Print a one-line total at the end of a run (checked, skipped, failed).
- Fixes from a review of the implementation (tasks 7.x): md5s that differ only in letter case are stopped instead of sharing one verified file; the skip rule is strict and looks at the script's own time; `--fail-on-wrong` is not defeated by skipping; a parquet column that needs pandas or a time zone database no longer stops the md5; the paste block names no md5; a trial run's debug report has its own file; UTF-16 and UTF-8-with-a-stray-byte CSVs are read correctly or stopped with a clear message.
- Update the help text, `EXTRACTION_QC.md`, `.gitignore` (folders instead of name patterns) and the tests.

## Capabilities

### New Capabilities
<!-- None: the behaviour belongs to the two existing capabilities below. -->

### Modified Capabilities
- `extraction-verification`: input discovery (two layouts, layout selection, extracted format and newest-wins rule, ignored stray files), required input columns (parquet types), output preserves the input (cells of a parquet written as text), bounded memory (extracted parquet), one failing file, output file that cannot be replaced (paths in `verified/`), CSV text encoding (UTF-16, stray bytes), run summary (skipped files and the total line), and exit code (skips under `--fail-on-wrong`); plus new requirements for the folder layout, extracted parquet cells as text (and, separately, temporal cells and map cells), skipping finished md5s, forcing a recheck, and format independence.
- `qc-debug-report`: the debug report location (`verified/`, and its own name for a trial run), the trial-run output location, and the input profile (layout and extracted format used, without the md5).

## Impact

- `extraction_qc.py`: the `Triple` / `find_triples` discovery, `prepare_triple`, `verify_triple`, `main` and the CLI help; a parquet-backed extracted-input reader beside `CsvInput` (same header, index and rows interface, so the lanes and the merge pass are untouched); the skip rule.
- Tests: all existing tests exercise the flat layout and stay as regression coverage of the fallback; new fixtures and tests for the folder layout, the parquet extracted file, the parity check, and skipping.
- `EXTRACTION_QC.md` and `.gitignore`.
- No new dependencies (`pyarrow` already reads parquet). The flat layout keeps its current behaviour, so existing users are not broken; the only visible changes to them are the added `--force` switch and the one-line run total.
- Out of scope: parallel checking of several md5s, typed `Value` columns in the extracted parquet (stopped with a message until a real file shows they exist), a parquet verified output, and reading the extracted file by content instead of extension.

# Tasks

## 1. Skeleton, fixtures and input discovery

- [x] 1.1 Create `extraction_qc.py` with the CLI (`--debug`, `--show-values`, `--limit`, `--rows`, `--skip-original-on-relevant-failure`, `--original-column`, `--relevant-column`) and a short `--help` for each switch; verify `python extraction_qc.py --help` lists every switch
- [x] 1.2 Create `tests/fixtures.py` that builds small parquet and CSV fixtures (typed and plain DynamoDB JSON, several row groups, several string columns, one extracted row per reason code); verify `python -m unittest` discovers and runs a fixture-sanity test that reads the generated files back
- [x] 1.3 Implement input discovery by `<md5>_` prefix and the required-column check, with a clear stop message for a missing sibling file or column; verify unit tests cover the complete set, path columns pointing elsewhere, a missing sibling, several extracted CSVs, and a missing required column
- [x] 1.4 Create `EXTRACTION_QC.md` describing the three input files, folder convention and the basic run command; verify the documented command runs against the fixtures

## 2. Path resolution and value matching

- [x] 2.1 Implement the JSONPath tokenizer and the DynamoDB-aware resolver (transparent `M`/`L`/`S`/`N`/`BOOL`/`NULL`/set wrappers, source type tag returned, number text preserved); verify tests for typed documents, plain documents, an index beyond the array, `Item..x`, and an attribute literally named `S`
- [x] 2.2 Implement the textual form of a source value and strict comparison (strings as stored, numbers as stored text, `true`/`false`, null as empty, maps and lists by JSON structure); verify tests for identical strings, `12345.0` against `12345`, trimmed whitespace, and a map with different spacing
- [x] 2.3 Implement the `FORMAT_CHANGED` / `VALUE_MISMATCH` classifier (whitespace, case, numeric and date-format equivalence, labels only); verify tests for a reformatted number, `2024-01-25` against `01/25/2024`, and `alice` against `bob`
- [x] 2.4 Implement JSON column detection (single text column, first column holding a JSON object, explicit override, stop listing candidates, UTF-8 decoding of binary, double-encoded JSON); verify tests for each case

## 3. Parquet reading and the lane engine

- [x] 3.1 Implement the streaming row reader over the JSON column (row-group index, batch iteration, previous/current/next window across batch boundaries, 1-based line mapping, `LINE_OUT_OF_RANGE`); verify tests for line 1, line 0, the last row, one past the end, and a neighbour across a batch boundary
- [x] 3.2 Implement the lane routine: chunked sort-and-scan, one JSON parse per distinct line, per-row results (`Correct`/`Wrong`, reason code and detail) written to a temporary file, with `INVALID_LINE`, `UNSUPPORTED_PATH`, `INVALID_JSON` and `PATH_NOT_FOUND` handled per row; verify tests that a bad row does not stop the run and that a shuffled CSV gives results identical to a sorted one
- [x] 3.3 Implement the adjacent-line hint (`FOUND_AT_LINE_<n>`, neighbours parsed lazily only for failing rows); verify tests for an off-by-one row and for a row with no match nearby

## 4. Merge pass, output and run control

- [x] 4.1 Implement the merge pass that writes `<md5>_verified.csv` with the five appended columns in order, the original columns and row order unchanged (`007`, leading and trailing spaces kept), and the overall verdict; verify tests on the header, value preservation and overall verdict for both-correct, one-wrong and skipped rows
- [x] 4.2 Implement `--skip-original-on-relevant-failure` (original lane reads the relevant results, rows marked `Skipped` and not read); verify tests that rows with a failed relevant check are `Skipped` and others are checked normally
- [x] 4.3 Implement the run summary (rows, per-lane and overall Correct/Wrong, counts per reason code) and periodic progress on stderr; verify a fixture run prints counts matching its known results
- [x] 4.4 Implement `--limit` and `--rows` as one filter feeding both lanes and the merge pass; verify tests that only the selected rows appear in the verified CSV
- [x] 4.5 Extend `EXTRACTION_QC.md` with the output columns, verdict meanings and the reason-code table; verify every code in the document appears in the implementation and in a fixture result

## 5. Debug report

- [x] 5.1 Implement masking (uppercase letters to `A`, other letters to `a`, digits to `9`) and the bounded collector for the input profile (file row counts, row groups, JSON column, library version, CSV sort order) and per-lane reason counts; verify tests that no real value text appears in the masked output
- [x] 5.2 Implement the JSON skeleton merged over up to 200 documents per file, with `{*}` for nested maps above 20 distinct keys (top-level attribute names always listed); verify tests for a nested list-of-maps structure, the collapsed case, and an item with 30 top-level attributes
- [x] 5.3 Implement the path-shape table (indexes collapsed to `[*]`, per-lane correct counts, dominant reason, worst 8) and the value-shape profile with the DataType cross-tab; verify tests for a lane-specific `PATH_NOT_FOUND` pattern, collapsed indexes, and a boolean written as `True` against a source `BOOL`
- [x] 5.4 Implement failure traces (up to 3 in the paste block, up to 50 per lane in the report) showing where the path walk stopped and masked expected and found values; verify tests for a path stopping partway and for a value mismatch
- [x] 5.5 Implement the paste block builder (at most 50 lines of at most 120 characters) and the full local `<md5>_debug_report.txt`, with `--show-values` affecting only the local report; verify tests on the size limits with many shapes, on masking with and without `--show-values`, and that verdicts are identical with and without `--debug`
- [x] 5.6 Extend `EXTRACTION_QC.md` with a section on running `--debug --limit N` on a restricted machine and reading the paste block; verify the documented command runs against the fixtures and its output matches the description

## 6. Integration and scale

- [x] 6.1 Add an end-to-end test over the fixtures with one row for every reason code and both lane outcomes, comparing the verified CSV to an expected file; verify `python -m unittest` passes from a clean checkout
- [x] 6.2 Run a generated scale check (about 1,000,000-row original parquet with large documents and a multi-million-row extracted CSV, built in a temporary folder and not committed) at two sizes, 100,000 and 1,000,000 rows; verify peak memory does not grow with the row count and record the runtime in `EXTRACTION_QC.md`

## 7. Review fixes

- [x] 7.1 Make the CSV input robust: raise the CSV field size limit to the platform maximum, detect the CSV encoding (UTF-8, else Windows-1252, else Latin-1) with `--csv-encoding` as override and an unknown-name error, write the output as UTF-8 with a byte order mark when the input was not plain UTF-8, stop on a duplicated required column, and document that blank lines are skipped and not counted; verify tests for a 300,000-character Value, a Windows-1252 file with `café` (detected, announced, value preserved), the explicit override, an unknown name, a file with a byte Windows-1252 cannot decode, a duplicated `Value` header, and blank-line numbering with `--rows`
- [x] 7.2 Isolate failures and make exit codes and console output safe: guard each extracted CSV so an unexpected error is reported by file name and the next file still runs, add `--fail-on-wrong` (exit code 3), and make stdout and stderr escape characters the console cannot show; verify tests that an error in the first of two files still processes the second and exits 1, that exit codes are 0, 3 and 1 in the three situations, and that a CJK key printed to a Windows-1252 stream completes with a `\u` escape
- [x] 7.3 Protect finished work: test that an existing output file can be written before checking any row, keep `<md5>_verified.csv.partial` when the final replace fails, write trial runs (`--limit`, `--rows`) to `<md5>_verified_trial.csv` without touching `<md5>_verified.csv`, and add `--temp-dir` with a missing-folder error; verify tests for each case, including that a full result is byte-identical after a trial run and that no working files remain
- [x] 7.4 Handle odd columns: reject an explicitly named non-text JSON column with a message giving its type, report a row holding non-text as `INVALID_JSON`, and recognise dictionary-encoded and string-view text columns; verify tests for an integer column, a struct column, a dictionary-encoded column, and a string-view column where pyarrow supports it
- [x] 7.5 Tighten matching: compare numbers and strings inside map and list values as different, and accept `["key"]` as well as `['key']` in paths; verify tests that `{"a":1}` matches a source `{"a":{"N":"1"}}` but not `{"a":{"S":"1"}}`, the reverse for `{"a":"1"}`, and that both bracket forms resolve
- [x] 7.6 Parse each document once per lane by keeping the last few parsed documents, so a neighbour read for the hint is not parsed again; verify a test where every row fails and the parse count equals the number of documents, and that the existing hint and order tests still pass
- [x] 7.7 Add `--check-same-document` and the `DOCUMENT_MISMATCH` reason; verify tests for a wrong line that hits an identical value in another document (Wrong with the switch, Correct without), a relevant document that is a subset, an unreadable relevant line, an array-index shift, and unchanged output without the switch
- [x] 7.8 Mask keys that look like data in the skeleton, path shapes and traces; verify tests for an email key, `user12345`, ordinary names such as `scopeIds` and `line1`, a path shape containing a data-like key, and that the full report shows real keys only with `--show-values`
- [x] 7.9 Update `EXTRACTION_QC.md` for every change above (new switches, encodings, exit codes, trial file name, working files, same-document check, key masking, `DOCUMENT_MISMATCH`) and add a `.gitignore` for Python caches, working files and generated outputs without hiding the expected test files; verify the existing documentation tests pass with the new switches and code, and `git status` no longer lists `__pycache__`
- [x] 7.10 Re-run the generated scale check at 100,000 and 1,000,000 rows after these changes; verify the verdict counts are unchanged from the earlier run, peak memory is still flat, and update the measured table if the numbers moved

## Workflow follow-up

- On the VM, run `python extraction_qc.py --debug --limit 2000`, review the paste block, and check the provisional boolean, null and map spellings and the array-index assumption against it; revisit the strict-comparison requirement if the real shapes differ.
- Archive the change after the project's review requirements are satisfied.

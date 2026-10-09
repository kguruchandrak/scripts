# Proposal

## Why

The extraction process pulls canonical-field values out of DynamoDB-sourced parquet files and records each one in an `<md5>_extracted.csv` together with the line numbers it came from. Nothing currently checks that those records are true: that the row a `SourceLine` or `RelevancyParquetLine` points at really holds the recorded `SourceElementPath` with the recorded `Value`. A wrong line number, a shifted array index, or a silently reformatted value would go unnoticed. A QC step is needed that can be run against full-size files (500k to 1m original rows) and that tells the reviewer, row by row, whether the extraction was right and, if not, why.

## What Changes

- Add a QC script that takes an `<md5>_extracted.csv` and the matching `<md5>_original.parquet` and `<md5>_relevant.parquet` (all in the folder the script is run from) and writes `<md5>_verified.csv`: every column of the extracted CSV plus five verification columns.
- Verify each extracted row in two independent lanes:
  - Relevant lane: at `RelevancyParquetLine` in the relevant parquet file, does `SourceElementPath` exist with `Value`?
  - Original lane: at `SourceLine` in the original parquet file, does `SourceElementPath` exist with `Value`?
- Resolve `SourceElementPath` (JSONPath such as `$.Item.scopeIds[0].partnerId`) against DynamoDB-style JSON stored as text in each parquet row, unwrapping the DynamoDB type wrappers (`M`, `L`, `S`, `N`, `BOOL`, `NULL`) on the way.
- Compare values strictly (exact text, no normalization), so a format-only change (date, number, whitespace) is `Wrong` and is labelled as a format change rather than a different value.
- Record a categorized reason for every `Wrong` result in the lane's reason column.
- Add an optional skip-on-failure switch that skips the original lane for rows whose relevant lane failed.
- Add a `--debug` mode that emits a compact, value-masked diagnostic report (a pasteable summary of at most about 50 lines, plus a fuller local report) so the real JSON layout can be analysed on a machine whose data cannot be shared.
- Stream the CSV and both parquet files so 500k to 1m row inputs run without loading them whole.

## Capabilities

### New Capabilities
- `extraction-verification`: row-level verification of an extracted-values CSV against the relevant and original parquet files, including input discovery, the two verification lanes, DynamoDB JSON path resolution, strict value matching, the output columns, and the reason categories.
- `qc-debug-report`: the `--debug` diagnostic mode that summarizes parquet and JSON structure, path-shape outcomes, value formats and failure traces in a masked, compact form.

### Modified Capabilities
<!-- None: the project has no existing specs. -->

## Impact

- New file at the repository root, proposed name `extraction_qc.py`. `parquet_to_csv.py` and the nested `scripts/` repository are not touched.
- Runtime dependency on `pyarrow`, which is installed on the target VM. No other third-party packages.
- New output file `<md5>_verified.csv` (and, in debug mode, a local debug report file) written next to the inputs. Input files are never modified.
- Out of scope: checking that no relevant field was missed (recall), validating `EntityRole`, `NormalizedPath` or `CanonicalField` against `domain.csv`, and verifying the md5 prefix against file contents.

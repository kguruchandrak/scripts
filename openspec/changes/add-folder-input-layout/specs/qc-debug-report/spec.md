# Spec Delta

## MODIFIED Requirements

### Requirement: Debug switch and outputs
When `--debug` is given, the system SHALL perform the normal verification and additionally print a short paste block to the console and write a fuller report to `<md5>_debug_report.txt`, in `verified/` in the folder layout and next to the inputs in the flat layout. For a trial run (`--limit` or `--rows`) the report SHALL be written to `<md5>_debug_report_trial.txt` in the same place, never over a full run's `<md5>_debug_report.txt`. Verdicts and reasons SHALL be identical to a run without `--debug`.

#### Scenario: Debug run
- **WHEN** the check is run with `--debug`
- **THEN** the verified CSV is written as usual, a paste block is printed, and `<md5>_debug_report.txt` is written

#### Scenario: Verdicts unchanged
- **WHEN** the same inputs are checked with and without `--debug`
- **THEN** every row has the same verification columns in both verified CSVs

#### Scenario: Folder layout
- **WHEN** a run in the folder layout is made with `--debug`
- **THEN** the report is `verified/abc123_debug_report.txt`

#### Scenario: Trial run does not replace the full report
- **WHEN** `--debug` runs once in full and then again with `--limit 3`
- **THEN** `<md5>_debug_report.txt` is unchanged and the second report is `<md5>_debug_report_trial.txt`

#### Scenario: Skipped md5
- **WHEN** `--debug` is given for an md5 that is skipped as already verified
- **THEN** no report or paste block is produced for it, and the skip line says that `--force` rechecks it

### Requirement: Input profile
The paste block SHALL state the parquet library version in use, the layout (folder or flat) and the extracted file's format, naming that file by its pattern (`extracted/<md5>.csv`, `<md5>_extracted.csv`) and never by the real md5, which stays in the local report file; for each parquet file its row count, number of row groups, and the name and type of the JSON column used; and for the extracted file its row count and whether its rows are in ascending order of SourceLine and of RelevancyParquetLine.

#### Scenario: Profile lines
- **WHEN** the original file has 903,551 rows in 37 row groups with JSON column `payload`
- **THEN** the paste block shows those figures for the original file

#### Scenario: Unsorted CSV
- **WHEN** SourceLine values are not in ascending order
- **THEN** the paste block says SourceLine order is not ascending

#### Scenario: Layout and format shown
- **WHEN** a folder-layout run uses `extracted/abc123.parquet`
- **THEN** the paste block says the folder layout and a parquet extracted file were used, as `extracted/<md5>.parquet`

#### Scenario: The md5 stays out of the paste block
- **WHEN** a debug run is made for `abc123`, in either layout and for either format
- **THEN** `abc123` does not appear in the paste block, and does appear in the local report file

### Requirement: Trial-run limits
The system SHALL accept `--limit N` to verify only the first N data rows of the extracted file, and `--rows` with a comma-separated list of 1-based data row numbers to verify only those rows. Blank lines in a CSV are not rows and are not counted. The verified rows and any debug report SHALL cover only the rows processed, and the verified rows SHALL be written to `<md5>_verified_trial.csv` in the same place as the verified CSV, never to `<md5>_verified.csv`, so that a full result is not overwritten by a trial.

#### Scenario: First rows only
- **WHEN** `--limit 1000` is given for a CSV with 5,000,000 rows
- **THEN** only the first 1,000 rows are verified and written to `<md5>_verified_trial.csv`

#### Scenario: Specific rows
- **WHEN** `--rows 17,203` is given
- **THEN** only CSV data rows 17 and 203 are verified and traced, and written to `<md5>_verified_trial.csv`

#### Scenario: A full result already exists
- **WHEN** a complete `<md5>_verified.csv` exists and a run with `--limit 10` is made
- **THEN** `<md5>_verified.csv` is left exactly as it was

#### Scenario: Folder layout
- **WHEN** `--limit 10` is given in the folder layout
- **THEN** the rows are written to `verified/<md5>_verified_trial.csv` and `verified/<md5>_verified.csv` is left as it was

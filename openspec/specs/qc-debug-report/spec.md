# qc-debug-report Specification

## Purpose

Lets the QC check be diagnosed on a machine whose data cannot be shared, by producing a compact, value-masked summary of the parquet and JSON structure and of where verification fails.

## Requirements

### Requirement: Debug switch and outputs
When `--debug` is given, the system SHALL perform the normal verification and additionally print a short paste block to the console and write a fuller report to `<md5>_debug_report.txt` in the working folder. Verdicts and reasons SHALL be identical to a run without `--debug`.

#### Scenario: Debug run
- **WHEN** the check is run with `--debug`
- **THEN** the verified CSV is written as usual, a paste block is printed, and `<md5>_debug_report.txt` is written

#### Scenario: Verdicts unchanged
- **WHEN** the same inputs are checked with and without `--debug`
- **THEN** every row has the same verification columns in both verified CSVs

### Requirement: Paste block size limit
The paste block SHALL NOT exceed 50 lines, and no line SHALL exceed 120 characters, so that it can be copied out of a restricted machine as a small amount of text.

#### Scenario: Large inputs
- **WHEN** the inputs hold 1,000,000 rows and hundreds of distinct paths
- **THEN** the paste block is still 50 lines or fewer, showing only the most significant entries

### Requirement: Masked values
The paste block SHALL NOT contain any Value or JSON leaf value from the inputs. Wherever sample text is needed it SHALL be masked by replacing every uppercase letter with `A`, every other letter with `a` and every digit with `9`, and keeping other characters. The full report SHALL be masked the same way unless `--show-values` is given.

#### Scenario: Date value shown as a shape
- **WHEN** a Value of `2024-01-25` is described in the paste block
- **THEN** it appears as `9999-99-99`

#### Scenario: Names are not exposed
- **WHEN** a failing row has Value `alice`
- **THEN** the paste block shows `aaaaa` or only its length, never `alice`

#### Scenario: Unmasked local report
- **WHEN** `--show-values` is given
- **THEN** the local report shows real values and the paste block remains masked

### Requirement: Data-like keys are masked
A key shown in the skeleton, the path shapes or the traces SHALL be masked as described above when it is not a plain identifier, that is when it is longer than 40 characters, contains a character other than a letter, digit or underscore, starts with a digit, or contains three or more digits in a row. This covers keys that are really data, such as email addresses, UUIDs and identifiers like `user12345`. The full report SHALL follow the same rule unless `--show-values` is given.

#### Scenario: Email address used as a key
- **WHEN** a nested map is keyed by `ann@example.com`
- **THEN** the paste block shows the key as `aaa@aaaaaaa.aaa`

#### Scenario: Ordinary attribute names
- **WHEN** keys are `scopeIds`, `partnerId` and `line1`
- **THEN** they are shown as they are

#### Scenario: Path with a data-like key
- **WHEN** a path shape contains `.user12345.`
- **THEN** the paste block shows `.aaaa99999.` in its place

### Requirement: Input profile
The paste block SHALL state the parquet library version in use, for each parquet file its row count, number of row groups, and the name and type of the JSON column used, and for the extracted CSV its row count and whether its rows are in ascending order of SourceLine and of RelevancyParquetLine.

#### Scenario: Profile lines
- **WHEN** the original file has 903,551 rows in 37 row groups with JSON column `payload`
- **THEN** the paste block shows those figures for the original file

#### Scenario: Unsorted CSV
- **WHEN** SourceLine values are not in ascending order
- **THEN** the paste block says SourceLine order is not ascending

### Requirement: JSON structure skeleton
The paste block SHALL show the structure of the JSON documents as compact key paths with their DynamoDB type tags (`M`, `L`, `S`, `N`, `BOOL`, `NULL`), merged over a sample of up to 200 documents from each parquet file. A nested map with more than 20 distinct keys under one parent SHALL be shown as a single `{*}` entry; the item's own top-level attribute names are always listed, since they are the schema.

#### Scenario: Nested structure
- **WHEN** documents contain a list of maps with a string `partnerId`
- **THEN** the skeleton shows `Item.scopeIds:L[M{partnerId:S}]`

#### Scenario: Data-like map keys
- **WHEN** a nested map has more than 20 distinct keys across the sample
- **THEN** its keys are not listed and it is shown as `{*}`

#### Scenario: Many top-level attributes
- **WHEN** the item has 30 distinct top-level attributes
- **THEN** all 30 attribute names are listed, subject only to the line limit of the paste block

### Requirement: Path-shape outcomes
The paste block SHALL group rows by SourceElementPath with array indexes replaced by `[*]` and, for up to 8 shapes ordered by number of failing rows, show the row count, how many are `Correct` in each lane, and the most common reason where any fail.

#### Scenario: Systematic failure in one lane
- **WHEN** every row for `$.Item.scopeIds[*].enrollSettings[*].settings.agencyTIN` is `Correct` in the relevant lane and `PATH_NOT_FOUND` in the original lane
- **THEN** that shape is listed with its row count, all rows correct in relevant, none correct in original, and `PATH_NOT_FOUND` as the reason

#### Scenario: Index values collapsed
- **WHEN** rows use `scopeIds[0]` and `scopeIds[1]` for the same field
- **THEN** both count towards one shape with `scopeIds[*]`

### Requirement: Reason counts
The paste block SHALL show, for each lane, the number of rows per reason code.

#### Scenario: Mixed failures
- **WHEN** the relevant lane has 3 `VALUE_MISMATCH` and 2 `PATH_NOT_FOUND` results
- **THEN** the relevant-lane counts show those two codes with 3 and 2

### Requirement: Value-shape profile
The paste block SHALL show, for each source value type found at the checked paths (`S`, `N`, `BOOL`, `NULL`, `M`, `L`), the number of rows and up to 3 masked shapes of the extracted Value, and SHALL cross-tabulate the DataType column against the source type.

#### Scenario: Booleans written differently
- **WHEN** source values of type `BOOL` appear in the CSV as `True`
- **THEN** the profile shows type `BOOL` with the Value shape `Aaaa`

#### Scenario: Dates
- **WHEN** DataType is `date` for values whose source type is `S`
- **THEN** the cross-tab shows `date` against `S` and the Value shape such as `9999-99-99`

### Requirement: Failure traces
The paste block SHALL show up to 3 traces of failing rows, chosen from the most common failure reasons. Each trace SHALL give the CSV row number, the lane, the line checked, the point in the path where resolution stopped, and the expected and found values in masked form.

#### Scenario: Path stops partway
- **WHEN** `$.Item.scopeIds[0].settings.agencyTIN` fails because `settings` is missing
- **THEN** the trace shows the walk reaching `scopeIds[0]` and stopping at `settings`

#### Scenario: Value differs
- **WHEN** a trace is for a `VALUE_MISMATCH`
- **THEN** it shows the masked expected and found values, for example `aaaaa` against `aaa`

### Requirement: Full local report
The report file SHALL contain everything in the paste block without the size limit: every path shape, up to 50 failure traces per lane, and the complete reason counts.

#### Scenario: More detail than the paste block
- **WHEN** 40 path shapes exist
- **THEN** the report file lists all 40 while the paste block lists 8

### Requirement: Trial-run limits
The system SHALL accept `--limit N` to verify only the first N data rows of the extracted CSV, and `--rows` with a comma-separated list of 1-based CSV data row numbers to verify only those rows. Blank lines in the CSV are not rows and are not counted. The verified rows and any debug report SHALL cover only the rows processed, and the verified rows SHALL be written to `<md5>_verified_trial.csv`, never to `<md5>_verified.csv`, so that a full result is not overwritten by a trial.

#### Scenario: First rows only
- **WHEN** `--limit 1000` is given for a CSV with 5,000,000 rows
- **THEN** only the first 1,000 rows are verified and written to `<md5>_verified_trial.csv`

#### Scenario: Specific rows
- **WHEN** `--rows 17,203` is given
- **THEN** only CSV data rows 17 and 203 are verified and traced, and written to `<md5>_verified_trial.csv`

#### Scenario: A full result already exists
- **WHEN** a complete `<md5>_verified.csv` exists and a run with `--limit 10` is made
- **THEN** `<md5>_verified.csv` is left exactly as it was

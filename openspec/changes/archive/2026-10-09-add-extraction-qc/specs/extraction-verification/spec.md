# Spec Delta

## Purpose

Verifies that every value recorded in an extracted-values CSV is really present, at the recorded line and path, in the relevant and original parquet files, and reports a verdict and a reason for each row.

## ADDED Requirements

### Requirement: Input discovery by file name
The system SHALL find each `<md5>_extracted.csv` in the working folder and use the `<md5>_original.parquet` and `<md5>_relevant.parquet` files in that same folder that share its hash prefix. It SHALL NOT use the SourceFilePath or RelevancyFileLocation columns to locate files.

#### Scenario: Complete set of three files
- **WHEN** the working folder contains `abc123_extracted.csv`, `abc123_original.parquet` and `abc123_relevant.parquet`
- **THEN** the system verifies `abc123_extracted.csv` against those two parquet files

#### Scenario: Path columns refer to another machine
- **WHEN** SourceFilePath holds a path that does not exist on the machine running the check
- **THEN** the system still reads `abc123_original.parquet` from the working folder

#### Scenario: Missing sibling file
- **WHEN** `abc123_relevant.parquet` is absent from the working folder
- **THEN** the system stops before verifying any row, names the missing file, and writes no verified CSV for `abc123`

#### Scenario: Several extracted CSVs
- **WHEN** the folder contains two `*_extracted.csv` files with different hash prefixes
- **THEN** each is verified against its own parquet files and produces its own verified CSV

### Requirement: Required input columns
The system SHALL require the extracted CSV to contain the columns SourceLine, RelevancyParquetLine, SourceElementPath and Value, and SHALL stop with a message naming any missing column before processing any row.

#### Scenario: Required column missing
- **WHEN** the extracted CSV has no RelevancyParquetLine column
- **THEN** the system reports that RelevancyParquetLine is missing and produces no verified CSV

#### Scenario: Required column appears twice
- **WHEN** the extracted CSV has two columns named Value
- **THEN** the system stops, names Value as ambiguous, and produces no verified CSV

### Requirement: Output preserves the input
The system SHALL write `<md5>_verified.csv` containing every input row, in the original order, with every original column unchanged, and SHALL NOT modify any input file.

#### Scenario: Rows and values carried over unchanged
- **WHEN** an input row has Value `007` and another has Value ` P1 ` with surrounding spaces
- **THEN** the verified CSV holds the same rows in the same order with those Values character for character

#### Scenario: Blank line in the CSV
- **WHEN** the extracted CSV contains an empty line between two rows
- **THEN** the empty line is not a row: it is not counted in row numbers (for `--limit` and `--rows`) and is not written to the verified CSV

### Requirement: Verification columns
The verified CSV SHALL append these columns after the original columns, in this order: RelevantFileVerification, RelevantFileReason, OriginalFileVerification, OriginalFileReason, OverallVerification.

#### Scenario: Header of the verified CSV
- **WHEN** the extracted CSV has columns A, B, C
- **THEN** the verified CSV header is A, B, C, RelevantFileVerification, RelevantFileReason, OriginalFileVerification, OriginalFileReason, OverallVerification

### Requirement: Relevant-file check
For each row the system SHALL check whether the row at RelevancyParquetLine of the relevant parquet file contains SourceElementPath with the extracted Value. It SHALL set RelevantFileVerification to `Correct` with an empty RelevantFileReason when it does, and to `Wrong` with a reason when it does not.

#### Scenario: Value present at the line
- **WHEN** line 5 of the relevant file holds `$.Item.plainId` = `P1` and the row says RelevancyParquetLine 5, path `$.Item.plainId`, Value `P1`
- **THEN** RelevantFileVerification is `Correct` and RelevantFileReason is empty

#### Scenario: Value differs at the line
- **WHEN** line 5 of the relevant file holds `$.Item.plainId` = `P2` but the row says Value `P1`
- **THEN** RelevantFileVerification is `Wrong` and RelevantFileReason explains the mismatch

#### Scenario: Path absent at the line
- **WHEN** line 5 of the relevant file has no `$.Item.scopeIds[3].partnerId`
- **THEN** RelevantFileVerification is `Wrong` with a path-not-found reason

### Requirement: Original-file check
For each row the system SHALL check whether the row at SourceLine of the original parquet file contains SourceElementPath with the extracted Value. It SHALL set OriginalFileVerification to `Correct` with an empty OriginalFileReason when it does, and to `Wrong` with a reason when it does not.

#### Scenario: Value present at the line
- **WHEN** line 42 of the original file holds `$.Item.plainId` = `P1` and the row says SourceLine 42, path `$.Item.plainId`, Value `P1`
- **THEN** OriginalFileVerification is `Correct` and OriginalFileReason is empty

#### Scenario: Original check is separate from the relevant check
- **WHEN** the relevant-file check passes but the original file does not hold the value at SourceLine
- **THEN** RelevantFileVerification is `Correct` and OriginalFileVerification is `Wrong`

### Requirement: One-based line numbers
SourceLine and RelevancyParquetLine SHALL be read as 1-based row numbers over the data rows of the parquet file, where line 1 is the first row.

#### Scenario: First row
- **WHEN** a row has SourceLine 1
- **THEN** the system checks the first row of the original parquet file

#### Scenario: Line zero
- **WHEN** a row has SourceLine 0
- **THEN** OriginalFileVerification is `Wrong` with a line-out-of-range reason

### Requirement: Lanes are independent by default
Unless skip-on-failure is enabled, the system SHALL evaluate both the relevant-file check and the original-file check for every row, regardless of the other check's result.

#### Scenario: Relevant check fails
- **WHEN** a row fails the relevant-file check
- **THEN** the original-file check is still performed and its result and reason are recorded

### Requirement: Optional skip-on-failure
When the `--skip-original-on-relevant-failure` switch is given, the system SHALL NOT perform the original-file check for rows whose relevant-file check is `Wrong`; for those rows OriginalFileVerification SHALL be `Skipped` and OriginalFileReason SHALL begin with `SKIPPED`.

#### Scenario: Switch given and relevant check fails
- **WHEN** the switch is given and a row's RelevantFileVerification is `Wrong`
- **THEN** its OriginalFileVerification is `Skipped`

#### Scenario: Switch given and relevant check passes
- **WHEN** the switch is given and a row's RelevantFileVerification is `Correct`
- **THEN** the original-file check runs normally

### Requirement: Overall verdict
OverallVerification SHALL be `Correct` only when both RelevantFileVerification and OriginalFileVerification are `Correct`, and `Wrong` otherwise, including when the original check was skipped.

#### Scenario: Both checks pass
- **WHEN** both checks are `Correct`
- **THEN** OverallVerification is `Correct`

#### Scenario: One check fails
- **WHEN** RelevantFileVerification is `Correct` and OriginalFileVerification is `Wrong`
- **THEN** OverallVerification is `Wrong`

#### Scenario: Original check skipped
- **WHEN** OriginalFileVerification is `Skipped`
- **THEN** OverallVerification is `Wrong`

### Requirement: DynamoDB JSON path resolution
Each parquet row holds one JSON document as text, in DynamoDB JSON form or plain JSON. The system SHALL resolve SourceElementPath, a JSONPath of keys and array indexes such as `$.Item.scopeIds[0].partnerId`, treating the DynamoDB type wrappers (`M`, `L`, `S`, `N`, `BOOL`, `NULL`) as transparent.

#### Scenario: Typed document
- **WHEN** the document is `{"Item":{"scopeIds":{"L":[{"M":{"partnerId":{"S":"A1"}}}]}}}`
- **THEN** the path `$.Item.scopeIds[0].partnerId` resolves to `A1`

#### Scenario: Plain document
- **WHEN** the document is `{"Item":{"scopeIds":[{"partnerId":"A1"}]}}`
- **THEN** the same path resolves to `A1`

#### Scenario: Index beyond the array
- **WHEN** the path uses `scopeIds[3]` but the array has one element
- **THEN** the path is reported as not found

#### Scenario: Key in brackets
- **WHEN** the path is `$.Item["a.b"]` or `$.Item['a.b']` and the item has an attribute named `a.b`
- **THEN** the path resolves to that attribute

### Requirement: JSON column identification
The system SHALL identify which parquet column holds the JSON text automatically, and SHALL accept an explicit column name for the original file (`--original-column`) and for the relevant file (`--relevant-column`). When it cannot decide, it SHALL stop and list the candidate columns.

#### Scenario: One text column
- **WHEN** a parquet file has a single string column
- **THEN** that column is used as the JSON column

#### Scenario: Several text columns, one holds JSON
- **WHEN** a parquet file has string columns `id` and `payload` and only `payload` holds a JSON object
- **THEN** `payload` is used

#### Scenario: Column given explicitly
- **WHEN** `--original-column body` is given
- **THEN** the original file's `body` column is used without detection

#### Scenario: Dictionary-encoded text column
- **WHEN** the only text column of a parquet file is dictionary-encoded
- **THEN** it is recognised as text and used as the JSON column

#### Scenario: Named column is not text
- **WHEN** `--original-column n` names an integer column
- **THEN** the system stops before checking any row and says that column `n` has type int64 and is not text

#### Scenario: A row holds something other than JSON text
- **WHEN** the value in a row of the JSON column is not text
- **THEN** that row's check is `Wrong` with reason `INVALID_JSON` and the run continues

### Requirement: Strict value comparison
The system SHALL treat Value as matching only when it equals the source value's text exactly, including whitespace and letter case. A string is compared as stored, a number as its stored number text, a boolean as `true` or `false`, and null as empty text. A map or list matches when Value is JSON equal in structure to it, whatever the spacing or key order.

#### Scenario: Identical string
- **WHEN** the source holds `P1` and Value is `P1`
- **THEN** the values match

#### Scenario: Number reformatted
- **WHEN** the source number is stored as `12345.0` and Value is `12345`
- **THEN** the values do not match

#### Scenario: Whitespace trimmed
- **WHEN** the source holds ` P1 ` and Value is `P1`
- **THEN** the values do not match

#### Scenario: Map with different spacing
- **WHEN** the source map is `{"a":"1"}` and Value is `{"a": "1"}`
- **THEN** the values match

#### Scenario: Number and string inside a map are different
- **WHEN** the source map is `{"a":{"N":"1"}}`
- **THEN** Value `{"a":1}` matches and Value `{"a":"1"}` does not

#### Scenario: String and number inside a map are different
- **WHEN** the source map is `{"a":{"S":"1"}}`
- **THEN** Value `{"a":"1"}` matches and Value `{"a":1}` does not

### Requirement: Format changes are distinguished from different values
When a value does not match strictly but would match if surrounding whitespace, letter case, numeric formatting or date formatting were ignored, the reason SHALL start with `FORMAT_CHANGED`. Otherwise the reason SHALL start with `VALUE_MISMATCH`. The verdict is `Wrong` in both cases.

#### Scenario: Reformatted number
- **WHEN** the source number is `12345.0` and Value is `12345`
- **THEN** the reason starts with `FORMAT_CHANGED`

#### Scenario: Reformatted date
- **WHEN** the source holds `2024-01-25` and Value is `01/25/2024`
- **THEN** the reason starts with `FORMAT_CHANGED`

#### Scenario: Different value
- **WHEN** the source holds `alice` and Value is `bob`
- **THEN** the reason starts with `VALUE_MISMATCH` and shows the expected and found values

### Requirement: Reason categories
Every `Wrong` result SHALL carry a reason starting with exactly one of these codes, followed by detail: `LINE_OUT_OF_RANGE`, `INVALID_LINE`, `UNSUPPORTED_PATH`, `INVALID_JSON`, `PATH_NOT_FOUND`, `VALUE_MISMATCH`, `FORMAT_CHANGED`, and, only with `--check-same-document`, `DOCUMENT_MISMATCH`. A `Correct` result SHALL have an empty reason.

#### Scenario: Line beyond the end of the file
- **WHEN** SourceLine is 99 and the original file has 3 rows
- **THEN** OriginalFileReason starts with `LINE_OUT_OF_RANGE` and states the row count

#### Scenario: Line is not a number
- **WHEN** SourceLine is `abc` or blank
- **THEN** OriginalFileReason starts with `INVALID_LINE`

#### Scenario: Path cannot be parsed
- **WHEN** SourceElementPath is `Item..x`
- **THEN** both reasons start with `UNSUPPORTED_PATH`

#### Scenario: Row is not valid JSON
- **WHEN** the parquet row at the line does not contain valid JSON
- **THEN** that lane's reason starts with `INVALID_JSON`

#### Scenario: One bad row does not stop the run
- **WHEN** a row has an invalid line number
- **THEN** that row is marked `Wrong` and processing continues with the next row

### Requirement: Adjacent-line hint
When a lane is `Wrong`, the system SHALL also check the lines immediately before and after the stated line, and when the path and value are found there it SHALL append `FOUND_AT_LINE_<n>` to that lane's reason.

#### Scenario: Off by one
- **WHEN** the row says SourceLine 42 but the path and value are at line 41 of the original file
- **THEN** OriginalFileReason ends with `FOUND_AT_LINE_41`

#### Scenario: Not found nearby
- **WHEN** the path and value are on neither neighbouring line
- **THEN** no `FOUND_AT_LINE` hint is added

### Requirement: Results do not depend on row order
The verdict and reason for a row SHALL be the same whether or not the extracted CSV is sorted by SourceLine or RelevancyParquetLine.

#### Scenario: Unsorted CSV
- **WHEN** the same rows are given once sorted and once shuffled
- **THEN** every row receives the same verdict and reason in both runs

### Requirement: Bounded memory at scale
The system SHALL verify an original parquet file of at least 1,000,000 rows and a correspondingly large extracted CSV without loading either file whole, so that memory use does not grow with the number of rows.

#### Scenario: Million-row original file
- **WHEN** the original file has 1,000,000 rows
- **THEN** the run completes and memory use stays bounded by a fixed batch size rather than the file size

### Requirement: Run summary
When a run finishes, the system SHALL print the number of rows checked, the number of `Correct` and `Wrong` results for each lane and overall, and a count of rows per reason code. During a long run it SHALL report progress periodically.

#### Scenario: Summary printed
- **WHEN** a run over 1,000 rows finishes
- **THEN** the console shows 1,000 rows checked with per-lane and overall Correct/Wrong counts and a per-reason-code count

### Requirement: CSV text encoding
The system SHALL read the extracted CSV as UTF-8 and, when it is not valid UTF-8 (for example Excel's "CSV (Comma delimited)", which is Windows-1252), as Windows-1252, saying which encoding it used. `--csv-encoding NAME` SHALL override the detection. The verified CSV SHALL be written as UTF-8 with a byte order mark whenever the input was not plain UTF-8, so that every character survives.

#### Scenario: Excel CSV with an accented value
- **WHEN** the CSV was saved as Windows-1252 and a Value is `café`
- **THEN** the run completes, says it read the file as Windows-1252, compares `café` with the document text, and the verified CSV holds `café`

#### Scenario: Encoding given explicitly
- **WHEN** `--csv-encoding cp1252` is given
- **THEN** the file is read as Windows-1252 without detection

#### Scenario: Unknown encoding name
- **WHEN** `--csv-encoding nonsense` is given
- **THEN** the system stops and names the unknown encoding

### Requirement: Fields of any length
The system SHALL accept a Value, or any other CSV field, of any length.

#### Scenario: Very long Value
- **WHEN** a Value is 300,000 characters long
- **THEN** its row is checked like any other row

### Requirement: One failing file does not stop the others
When checking one extracted CSV fails, for an input problem or for an unexpected error, the system SHALL report a message naming that file, SHALL leave no verified CSV for it, SHALL carry on with the next extracted CSV, and SHALL finish with a non-zero exit code.

#### Scenario: Unexpected error in the first file
- **WHEN** checking `aaa_extracted.csv` fails with an unexpected error
- **THEN** the message names `aaa_extracted.csv`, `bbb_extracted.csv` is still checked, and the exit code is non-zero

### Requirement: Output file that cannot be replaced
Before checking any row the system SHALL test that the output file can be written, and stop with a message naming it when another program holds it open. If the file cannot be replaced at the end of the run, the finished results SHALL be kept in `<md5>_verified.csv.partial` and the message SHALL name that file.

#### Scenario: Output held open before the run
- **WHEN** `abc123_verified.csv` is held open by another program
- **THEN** the run stops before checking any row and the message names `abc123_verified.csv`

#### Scenario: Output becomes locked during the run
- **WHEN** replacing `abc123_verified.csv` fails at the end of the run
- **THEN** the finished results remain in `abc123_verified.csv.partial` and the message says so

### Requirement: Console output never fails on characters
Printing to the console SHALL never make the run fail because the console encoding cannot represent a character; such characters SHALL be shown as escapes.

#### Scenario: Redirected output in a narrow encoding
- **WHEN** output is redirected to a Windows-1252 stream and a key in the data contains a CJK character
- **THEN** the run completes and the character appears as a `\u` escape

### Requirement: Exit code
The exit code SHALL be 0 when every extracted CSV was checked, whatever the verdicts, and 1 when any could not be checked. With `--fail-on-wrong`, a run that checked everything but produced any `Wrong` overall verdict SHALL exit with code 3.

#### Scenario: Some rows are wrong
- **WHEN** all files were checked and some rows are `Wrong`
- **THEN** the exit code is 0, or 3 when `--fail-on-wrong` is given

#### Scenario: A file cannot be checked
- **WHEN** one extracted CSV cannot be checked
- **THEN** the exit code is 1

### Requirement: Working files location
The working files of a run SHALL be written to the system temporary folder, or to the folder given with `--temp-dir`, and SHALL be removed when the run ends.

#### Scenario: Folder given
- **WHEN** `--temp-dir D` is given
- **THEN** the working files are created in `D` and none remain after the run

#### Scenario: Folder does not exist
- **WHEN** `--temp-dir` names a folder that does not exist
- **THEN** the system stops and names the folder

### Requirement: Each document is parsed once
Within one check of one file, each parquet document SHALL be parsed once, including when neighbouring lines are examined for the adjacent-line hint.

#### Scenario: Every row fails
- **WHEN** every row fails and the adjacent lines are examined for each
- **THEN** each document of the file is parsed once, not once per row that looks at it

### Requirement: Optional same-document check
When `--check-same-document` is given, the system SHALL also check for each row that every value in the relevant document at RelevancyParquetLine is present, at the same path, in the original document at SourceLine. If not, the original-file check SHALL be `Wrong` with a reason starting `DOCUMENT_MISMATCH` that names the first difference. Without the switch no such comparison is made.

#### Scenario: Same value in a different document
- **WHEN** the original line holds the path and value but belongs to another document than the relevant line
- **THEN** with the switch the original-file check is `Wrong` with `DOCUMENT_MISMATCH`, and without it the check is `Correct`

#### Scenario: Relevant document is a subset
- **WHEN** the relevant document holds only some of the values of the original document, at the same paths
- **THEN** no `DOCUMENT_MISMATCH` is reported

#### Scenario: Relevant line cannot be read
- **WHEN** RelevancyParquetLine is out of range or its row is not valid JSON
- **THEN** no `DOCUMENT_MISMATCH` is reported (the relevant-file check reports the problem)

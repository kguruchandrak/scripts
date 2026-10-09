# Spec Delta

## MODIFIED Requirements

### Requirement: Input discovery by file name
The system SHALL take each md5 to check from an extracted file and use the original and relevant parquet files of the same md5, finding every file by name only. It SHALL NOT use the SourceFilePath or RelevancyFileLocation columns to locate files. Where the files are looked for depends on the layout; a missing original or relevant file stops only that md5. Two md5s whose names differ only in letter case SHALL both be stopped, because on a case-insensitive file system they would share their input files and their verified file.

#### Scenario: Complete set of three files
- **WHEN** the working folder has no `extracted/` folder and contains `abc123_extracted.csv`, `abc123_original.parquet` and `abc123_relevant.parquet`
- **THEN** the system verifies `abc123_extracted.csv` against those two parquet files

#### Scenario: Path columns refer to another machine
- **WHEN** SourceFilePath holds a path that does not exist on the machine running the check
- **THEN** the system still reads the original file of that md5 from its place in the layout

#### Scenario: Missing sibling file
- **WHEN** `abc123_relevant.parquet` is absent from the working folder
- **THEN** the system stops before verifying any row of `abc123`, names the missing file, writes no verified CSV for `abc123`, and still checks the other md5s

#### Scenario: Several extracted CSVs
- **WHEN** the folder contains two `*_extracted.csv` files with different hash prefixes
- **THEN** each is verified against its own parquet files and produces its own verified CSV

#### Scenario: Md5s that differ only in letter case
- **WHEN** `extracted/` holds `abc.csv` and `ABC.parquet`
- **THEN** both md5s stop with a message that names both files and says they differ only in letter case, nothing is checked or written for either, the exit code is 1, and the other md5s are still checked

### Requirement: Required input columns
The system SHALL require the extracted file, CSV or parquet, to contain the columns SourceLine, RelevancyParquetLine, SourceElementPath and Value, and SHALL stop with a message naming any missing column before processing any row. A required column that appears more than once SHALL be reported as ambiguous.

#### Scenario: Required column missing
- **WHEN** the extracted CSV has no RelevancyParquetLine column
- **THEN** the system reports that RelevancyParquetLine is missing and produces no verified CSV

#### Scenario: Required column appears twice
- **WHEN** the extracted CSV has two columns named Value
- **THEN** the system stops, names Value as ambiguous, and produces no verified CSV

#### Scenario: Required column missing from a parquet file
- **WHEN** an extracted parquet file has no SourceLine column
- **THEN** the system reports that SourceLine is missing and produces no verified CSV for that md5

### Requirement: Output preserves the input
The system SHALL write the verified CSV, `<md5>_verified.csv`, containing every input row, in the original order, with every original column unchanged, and SHALL NOT modify any input file. Where it is written depends on the layout.

#### Scenario: Rows and values carried over unchanged
- **WHEN** an input row has Value `007` and another has Value ` P1 ` with surrounding spaces
- **THEN** the verified CSV holds the same rows in the same order with those Values character for character

#### Scenario: Blank line in the CSV
- **WHEN** the extracted CSV contains an empty line between two rows
- **THEN** the empty line is not a row: it is not counted in row numbers (for `--limit` and `--rows`) and is not written to the verified CSV

#### Scenario: Parquet rows carried over
- **WHEN** an extracted parquet file has three rows
- **THEN** the verified CSV has the same three rows in file order, each with its columns as text and the verification columns appended

### Requirement: Results do not depend on row order
The verdict and reason for a row SHALL be the same whether or not the extracted file is sorted by SourceLine or RelevancyParquetLine.

#### Scenario: Unsorted CSV
- **WHEN** the same rows are given once sorted and once shuffled
- **THEN** every row receives the same verdict and reason in both runs

### Requirement: Bounded memory at scale
The system SHALL verify an original parquet file of at least 1,000,000 rows and a correspondingly large extracted file, CSV or parquet, without loading either file whole, so that memory use does not grow with the number of rows.

#### Scenario: Million-row original file
- **WHEN** the original file has 1,000,000 rows
- **THEN** the run completes and memory use stays bounded by a fixed batch size rather than the file size

#### Scenario: Large extracted parquet
- **WHEN** an extracted parquet file has 4,000,000 rows in many row groups
- **THEN** memory use stays bounded by the size of one row group and does not grow as the rows are read

### Requirement: Run summary
When a run finishes, the system SHALL print for each checked file the rows checked, the `Correct` and `Wrong` counts per lane and overall, and a count per reason code; for each skipped md5 a line saying it was skipped and why; and one final line with the number of md5s checked, skipped and failed. During a long run it SHALL report progress periodically.

#### Scenario: Summary printed
- **WHEN** a run over 1,000 rows finishes
- **THEN** the console shows 1,000 rows checked with per-lane and overall Correct/Wrong counts and a per-reason-code count

#### Scenario: Total line
- **WHEN** a run checks two md5s, skips one and fails one
- **THEN** the last line reports 2 checked, 1 skipped and 1 failed

#### Scenario: Skipped md5 listed
- **WHEN** an md5 is skipped because its verified file is up to date
- **THEN** a line names that md5 and says it was skipped as already verified

### Requirement: One failing file does not stop the others
When checking one extracted file fails, for an input problem or for an unexpected error, the system SHALL report a message naming that file, SHALL leave no verified CSV for it, SHALL carry on with the next extracted file, and SHALL finish with a non-zero exit code.

#### Scenario: Unexpected error in the first file
- **WHEN** checking `aaa_extracted.csv` fails with an unexpected error
- **THEN** the message names `aaa_extracted.csv`, `bbb_extracted.csv` is still checked, and the exit code is non-zero

### Requirement: Output file that cannot be replaced
Before checking any row the system SHALL test that the output file can be written, and stop with a message naming it when another program holds it open. If the file cannot be replaced at the end of the run, the finished results SHALL be kept in a `.partial` file next to it (`<md5>_verified.csv.partial`) and the message SHALL name that file.

#### Scenario: Output held open before the run
- **WHEN** `abc123_verified.csv` is held open by another program
- **THEN** the run stops before checking any row and the message names `abc123_verified.csv`

#### Scenario: Output becomes locked during the run
- **WHEN** replacing `abc123_verified.csv` fails at the end of the run
- **THEN** the finished results remain in `abc123_verified.csv.partial` and the message says so

#### Scenario: Folder layout
- **WHEN** replacing `verified/abc123_verified.csv` fails at the end of a run in the folder layout
- **THEN** the finished results remain in `verified/abc123_verified.csv.partial`

### Requirement: Exit code
The exit code SHALL be 0 when every md5 was checked or skipped as already verified, whatever the verdicts, and 1 when any could not be checked. With `--fail-on-wrong`, a run that checked or skipped everything but has any `Wrong` overall verdict SHALL exit with code 3; a skipped md5 SHALL count by the `Wrong` rows of its verified file, read for that purpose only when `--fail-on-wrong` is given, and a verified file that cannot be read as a result SHALL be checked again instead of skipped.

#### Scenario: Some rows are wrong
- **WHEN** all files were checked and some rows are `Wrong`
- **THEN** the exit code is 0, or 3 when `--fail-on-wrong` is given

#### Scenario: A file cannot be checked
- **WHEN** one extracted CSV cannot be checked
- **THEN** the exit code is 1

#### Scenario: Skipped md5s
- **WHEN** every md5 was skipped as already verified
- **THEN** the exit code is 0, and also with `--fail-on-wrong` when none of the verified files holds a `Wrong` row

#### Scenario: Skipped md5 with wrong rows under the gate
- **WHEN** a first run with `--fail-on-wrong` exited 3 and the same command is run again and skips the md5
- **THEN** the exit code is 3 again, and the skip line says how many rows are `Wrong`

#### Scenario: Skipped md5 whose verified file cannot be read
- **WHEN** `--fail-on-wrong` is given and the verified file of an otherwise up-to-date md5 is not a verified CSV
- **THEN** the md5 is checked again

### Requirement: CSV text encoding
The system SHALL read the extracted CSV as UTF-16 or UTF-32 when it starts with that encoding's byte order mark, otherwise as UTF-8 and, when it is not valid UTF-8 (for example Excel's "CSV (Comma delimited)", which is Windows-1252), as Windows-1252, saying which encoding it used. A file that is UTF-8 apart from some bytes that are not, with more valid multi-byte characters than such bytes, SHALL be read as UTF-8 with those bytes as U+FFFD, and the system SHALL say how many there are and on which line the first is. A file with NUL bytes and no byte order mark SHALL stop that md5 with a message that names UTF-16 and `--csv-encoding`. `--csv-encoding NAME` SHALL override the detection. The verified CSV SHALL be written as UTF-8 with a byte order mark whenever the input was not plain UTF-8, so that every character survives.

#### Scenario: Excel CSV with an accented value
- **WHEN** the CSV was saved as Windows-1252 and a Value is `café`
- **THEN** the run completes, says it read the file as Windows-1252, compares `café` with the document text, and the verified CSV holds `café`

#### Scenario: Encoding given explicitly
- **WHEN** `--csv-encoding cp1252` is given
- **THEN** the file is read as Windows-1252 without detection

#### Scenario: Unknown encoding name
- **WHEN** `--csv-encoding nonsense` is given
- **THEN** the system stops and names the unknown encoding

#### Scenario: UTF-16 with a byte order mark
- **WHEN** the CSV was saved by Excel as "Unicode Text" (UTF-16 with a byte order mark)
- **THEN** the run reads it, says it read the file as UTF-16, and the header has its columns

#### Scenario: UTF-16 without a byte order mark
- **WHEN** the CSV is UTF-16 text with no byte order mark
- **THEN** the md5 stops with a message that the file holds NUL bytes, which is how UTF-16 looks without a mark, and that names `--csv-encoding utf-16-le`; it is not reported as a missing column

#### Scenario: One stray byte in UTF-8 text
- **WHEN** the CSV is UTF-8 and one value holds a single Windows-1252 byte
- **THEN** the other accented values are read correctly (not as two wrong characters each), the run says there is 1 byte that is not valid UTF-8 and on which line, and the row with that byte is `Wrong`

#### Scenario: Windows-1252 file is not mistaken for UTF-8
- **WHEN** a Windows-1252 file has accented characters and no valid multi-byte UTF-8 characters
- **THEN** it is read as Windows-1252

## ADDED Requirements

### Requirement: Folder layout
In the folder layout the system SHALL read `extracted/<md5>.csv` or `extracted/<md5>.parquet`, `original/<md5>.parquet` and `relevant/<md5>.parquet` under the working folder and write `verified/<md5>_verified.csv`, creating `verified/` when it is missing. The trial output, debug report and `.partial` file of an md5 SHALL be written to `verified/` too. `--folder` SHALL move the whole layout.

#### Scenario: Complete set
- **WHEN** the working folder holds `extracted/abc123.csv`, `original/abc123.parquet` and `relevant/abc123.parquet` and no `verified/` folder
- **THEN** the system creates `verified/` and writes `verified/abc123_verified.csv`

#### Scenario: Another folder
- **WHEN** `--folder D` is given
- **THEN** the four folders are looked for under `D` and the result is written to `D/verified/`

#### Scenario: Missing sibling file
- **WHEN** `relevant/abc123.parquet` is absent
- **THEN** that md5 stops before any row is checked, the message names `relevant/abc123.parquet`, no verified file is written for it, and the other md5s are still checked

#### Scenario: Files without an extracted file
- **WHEN** `original/zzz.parquet` exists but `extracted/` holds nothing for `zzz`
- **THEN** it is not used and not reported as an error

#### Scenario: Stray files in extracted
- **WHEN** `extracted/` also holds `~$abc123.csv` (an Excel lock file), `.hidden`, `notes.txt` and `abc123.csv.bak`
- **THEN** none of them is treated as an md5

#### Scenario: Other outputs
- **WHEN** a run is made with `--limit 5 --debug`
- **THEN** `verified/abc123_verified_trial.csv` and `verified/abc123_debug_report_trial.txt` are written

### Requirement: Layout selection
The system SHALL use the folder layout when an `extracted/` folder exists in the working folder and the flat layout otherwise. In the folder layout it SHALL ignore flat `<md5>_extracted.csv` files and say so. The flat layout SHALL behave as before: all files in one folder, outputs next to them, extracted files CSV only.

#### Scenario: Folder exists
- **WHEN** the working folder has an `extracted/` folder
- **THEN** the folder layout is used

#### Scenario: Flat files beside the folders
- **WHEN** an `extracted/` folder exists and `abc123_extracted.csv` is also in the working folder
- **THEN** the flat file is ignored and a note says so

#### Scenario: No extracted folder
- **WHEN** the working folder has no `extracted/` folder
- **THEN** the flat layout is used and its outputs are written next to the inputs

#### Scenario: Flat layout is CSV only
- **WHEN** the working folder has no `extracted/` folder and holds `abc123_extracted.parquet`
- **THEN** it is not used as an extracted file

### Requirement: Extracted file format
The system SHALL take the format of an extracted file from its extension, `.csv` or `.parquet`, ignoring case. When `extracted/` holds both for one md5 it SHALL use the newer file by modification time and say which one it used; when their times are equal it SHALL stop that md5 as ambiguous, naming both files. Files with another extension SHALL be ignored.

#### Scenario: Parquet extracted file
- **WHEN** `extracted/abc123.parquet` is the only file for `abc123`
- **THEN** it is read as parquet

#### Scenario: Upper-case extension
- **WHEN** the file is `extracted/abc123.CSV`
- **THEN** it is read as CSV

#### Scenario: Both formats, parquet newer
- **WHEN** `extracted/abc123.csv` and `extracted/abc123.parquet` exist and the parquet file was modified later
- **THEN** the parquet file is used and the output says so

#### Scenario: Both formats, equal times
- **WHEN** both files have the same modification time
- **THEN** that md5 stops as ambiguous with a message naming both files, and the other md5s are still checked

### Requirement: Extracted parquet columns
For an extracted parquet file the system SHALL require SourceElementPath and Value to be text columns (string, binary or a dictionary of them), and otherwise stop that md5 with a message naming the column and its type. It SHALL accept SourceLine and RelevancyParquetLine as integers, as whole floating-point numbers such as 5.0, or as text. Rows SHALL be numbered from 1 in file order, for `--limit` and `--rows` as well.

#### Scenario: Value is not text
- **WHEN** the Value column of an extracted parquet file has type double
- **THEN** that md5 stops before any row is checked and the message names Value and the type double

#### Scenario: Integer line numbers
- **WHEN** SourceLine is an int64 column
- **THEN** its values are used as line numbers

#### Scenario: Whole float line numbers
- **WHEN** SourceLine holds `5.0`
- **THEN** it is read as line 5

#### Scenario: Row numbers for a trial run
- **WHEN** `--rows 2,3` is given for an extracted parquet file
- **THEN** the second and third rows in file order are checked

### Requirement: Extracted parquet cells as text
The system SHALL write every cell of an extracted parquet file into the verified CSV as text: null as empty, integers as digits, booleans as `true` or `false`, floating-point numbers in their shortest exact form, decimals as plain digits, binary as UTF-8 text (hexadecimal when it is not valid UTF-8), and lists and structs as compact JSON. Dates, times, timestamps, durations and maps are covered by their own requirements. The verified CSV SHALL be written as UTF-8 with a byte order mark.

#### Scenario: Scalars
- **WHEN** cells hold null, the boolean true, the integer 7 and the float 12345.5
- **THEN** the verified CSV holds an empty cell, `true`, `7` and `12345.5`

#### Scenario: List and struct
- **WHEN** cells hold the list [1, 2] and the struct with a = 1
- **THEN** the verified CSV holds `[1,2]` and `{"a":1}`

#### Scenario: Non-ASCII text
- **WHEN** a Value is `café`
- **THEN** the verified CSV starts with a byte order mark and holds `café`

### Requirement: Extracted parquet temporal cells
The system SHALL write the dates, times, timestamps and durations of an extracted parquet file as text without needing pandas or a time zone database: dates, times and timestamps in ISO-8601 with a fraction of a second when there is one (six digits, or nine for nanoseconds), a timestamp with a time zone as its UTC instant with `+00:00` whatever the zone is called, and durations like Python writes a timedelta.

#### Scenario: Timestamp
- **WHEN** a cell holds the timestamp 2024-01-25 10:30:00
- **THEN** the verified CSV holds `2024-01-25T10:30:00`

#### Scenario: Time zone without a time zone database
- **WHEN** a timestamp column has the zone `America/New_York` (or a name that no database knows) and the machine has no tzdata
- **THEN** the md5 is checked and the cell holds the UTC instant, for example `2024-01-25T10:30:00+00:00`

#### Scenario: Nanoseconds without pandas
- **WHEN** a timestamp, time or duration column holds a value with a nanosecond part and pandas is not installed
- **THEN** the md5 is checked and the cell holds nine fractional digits, for example `1970-01-01T00:00:00.000001500`

#### Scenario: Duration
- **WHEN** a cell holds a duration of one hour and five minutes
- **THEN** the verified CSV holds `1:05:00`

### Requirement: Extracted parquet map cells and conversion failures
The system SHALL write a map cell of an extracted parquet file as a JSON object with its keys as text, or as a list of `[key, value]` pairs when a key repeats, and SHALL stop an md5 whose column cannot be converted to text with a message naming the column and its type, leaving no verified file for it.

#### Scenario: Map
- **WHEN** a cell holds the map k -> 1, j -> 2
- **THEN** the verified CSV holds `{"k":1,"j":2}`

#### Scenario: Map that repeats a key
- **WHEN** a cell holds a map with the key `a` twice
- **THEN** the verified CSV holds a list of pairs, `[["a",1],["a",2]]`

#### Scenario: Column that cannot be converted
- **WHEN** converting the column `When` of type `timestamp[us]` to text fails
- **THEN** that md5 stops with a message naming `When` and its type, no verified file is written for it, and the other md5s are still checked

### Requirement: Same result for either extracted format
For the same rows, an extracted CSV and an extracted parquet SHALL give the same verified rows: the same column values, verdicts and reasons.

#### Scenario: Same rows as CSV and as parquet
- **WHEN** one set of rows, all text, is supplied once as `extracted/abc123.csv` and once as `extracted/abc123.parquet`
- **THEN** the two verified CSVs hold identical rows when read as CSV

### Requirement: Skipping finished md5s
In the folder layout the system SHALL skip an md5 whose `verified/<md5>_verified.csv` exists and is newer than the extracted, original and relevant files it would use and than the script itself, and SHALL list it as skipped without opening its inputs. A verified file whose time equals that of an input or of the script SHALL not be trusted. A trial run SHALL never be skipped, and a `.partial` file SHALL not count as a result. The flat layout SHALL always recheck.

#### Scenario: Up to date
- **WHEN** `verified/abc123_verified.csv` is newer than all three inputs
- **THEN** `abc123` is skipped and listed

#### Scenario: An input is newer
- **WHEN** `relevant/abc123.parquet` was modified after `verified/abc123_verified.csv`
- **THEN** `abc123` is checked again and the verified file is replaced

#### Scenario: Equal times
- **WHEN** `verified/abc123_verified.csv` and `extracted/abc123.csv` have exactly the same modification time
- **THEN** `abc123` is checked again, because a coarse file system clock cannot tell a stale result from a current one

#### Scenario: The script changed
- **WHEN** `extraction_qc.py` was modified after `verified/abc123_verified.csv` was written
- **THEN** `abc123` is checked again, because the result may come from different code

#### Scenario: Trial run
- **WHEN** `--limit 10` is given and an up-to-date verified file exists
- **THEN** the md5 is checked and its trial output is written

#### Scenario: Interrupted run
- **WHEN** only `verified/abc123_verified.csv.partial` exists
- **THEN** `abc123` is checked

#### Scenario: Flat layout
- **WHEN** the flat layout is used and an up-to-date `abc123_verified.csv` exists
- **THEN** `abc123` is checked again

### Requirement: Forcing a recheck
The `--force` switch SHALL make the system check every md5 even when an up-to-date verified file exists. The documentation SHALL tell users to use it after changing switches that change results, such as `--check-same-document`, because the skip rule does not look at the switches behind an existing result.

#### Scenario: Forced
- **WHEN** `--force` is given and an up-to-date verified file exists
- **THEN** the md5 is checked and the verified file is replaced

#### Scenario: Switch changed without force
- **WHEN** `--check-same-document` is given for an md5 whose verified file is up to date
- **THEN** the md5 is skipped, as documented, until `--force` is given

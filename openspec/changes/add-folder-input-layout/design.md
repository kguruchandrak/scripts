# Design

## Context

See proposal.md for motivation. In `extraction_qc.py` every file name is built in one place: the `Triple` class (extracted, original, relevant, verified, trial, debug report) and `find_triples`, which globs `*_extracted.csv`. Everything else reaches files through `triple.*`, so a layout is a matter of how `Triple` objects are built. The extracted file is read through `CsvInput`, whose interface is small and is all the lanes, the merge pass and the debug collector use: `header`, `index` (column name to position), `rows()` yielding `(row number, list of text fields)`, and `output_encoding`. About 80 sites in the tests and 8 in the docs mention flat names; those tests keep exercising the flat layout, which stays supported.

Findings that shape the design:
- A single pyarrow `iter_batches` reader spanning many row groups keeps what it has read (see the archived `add-extraction-qc` design). The extracted parquet reader must therefore also read one row group at a time.
- The whole script treats extracted cells as text. A parquet extracted file only has to produce the same kind of rows; the open question is how typed cells become text, and whether `Value` can be typed at all.

## Goals / Non-Goals

**Goals:**
- The folder layout and the flat layout behind one discovery step that yields the same `Triple`.
- A parquet extracted file that plugs into the existing reader interface, so the lanes and merge pass do not change.
- Re-runs that skip finished md5s safely, with a clear way to force a recheck.
- Identical verdicts for the same rows whether they come as CSV or parquet.

**Non-Goals:**
- Parallel checking of several md5s (a handful per run), a parquet verified output, typed `Value` columns, detecting formats by content, or selecting a subset of md5s by switch.

## Decisions

### D1. One discovery step, two layouts
A `Layout` chooses where each file lives: the folder layout (`extracted/`, `original/`, `relevant/`, `verified/` under the working folder) or the flat layout (today's names in one folder). `Triple` keeps its attribute names (`extracted`, `original`, `relevant`, `verified`, `trial`, `debug_report`) and gains the `.partial` path, the extracted format and a printable name for each file, so `verify_triple`, `main` and the debug code change little. The folder layout is selected when `<working folder>/extracted` is a directory; otherwise the flat layout. In the folder layout, flat `<md5>_extracted.csv` files are not used and a note says so, so a half-migrated folder never mixes results. Alternative: let each folder be configured separately (`--extracted-dir` and so on): rejected for now, the four folders under one root were what was asked for and `--folder` moves them together.

### D2. Which files count as md5s in `extracted/`
Only regular files whose extension, compared case-insensitively, is `.csv` or `.parquet` and whose name does not start with `.` or `~$` (the lock file Excel leaves beside an open CSV) are considered; the md5 is the file name without the extension, not validated as hexadecimal because the tests and the pipeline are free to use any stem. The original and relevant files are looked up as `original/<md5>.parquet` and `relevant/<md5>.parquet` (lower-case extension). Files in `original/` or `relevant/` with no extracted file are never opened. md5s are processed in name order so output is stable.

### D3. Both formats for one md5: the newer wins
With both `<md5>.csv` and `<md5>.parquet`, the one with the later `st_mtime_ns` is used and the progress line says which; equal times stop that md5 as ambiguous naming both, because a copy resets times and the script must not guess. The flat layout never looks for parquet.

### D4. A parquet-backed extracted input beside `CsvInput`
`ParquetExtractedInput` has the same `header`, `index`, `rows()` and `output_encoding` (always UTF-8 with a byte order mark, so Excel shows the characters), and the same `--limit` / `--rows` filtering and 1-based numbering, which is simply file order since parquet has no blank lines. `rows()` reads one row group at a time (`row_groups=[g]`, a fresh reader per group), converts each batch column by column to text cells, and yields text rows. The converter for each column is chosen once from its Arrow type: text columns pass strings straight through (null becomes empty); other types use `to_pylist` and `cell_text`, or `arrow_to_python` for the types in D10; a conversion that fails names the column and its type (D10). Required-column and duplicate-column checks reuse `check_required_columns` on the schema's names. `SourceElementPath` and `Value` must be text columns (the existing text-type test, which already covers dictionary and view types) or the md5 stops with the column name and Arrow type; the line columns accept any type because their text is validated by `parse_line` exactly as for CSV (an integer gives digits, a float 5.0 gives `5.0`, which `parse_line` already accepts).

### D5. Cells as text
For columns that are not text: null becomes empty; bool `true`/`false`; int as digits; float with Python's shortest round-trip form (`repr`); decimal as plain digits (`format(d, "f")`); date, time and timestamp as ISO-8601 (see D10); binary as UTF-8 when it decodes, hexadecimal otherwise; list and struct as compact JSON with the same rules applied to nested values, and a map as a JSON object (D10). These are the rules for cells the script only carries through (and for the line columns). They are deliberately not a statement about how the extraction should format `Value`: a typed `Value` is rejected instead, because rendering a float or a timestamp would erase exactly the formatting differences the strict check exists to catch.

### D6. Skipping finished md5s, in `main`, before anything is opened
In the folder layout, before `verify_triple`, an md5 is skipped when `verified/<md5>_verified.csv` exists and its `st_mtime_ns` is greater than the latest of the three inputs it would use (the newer extracted file, original, relevant) and of the script file itself (D11). Skipped md5s are not opened, produce no debug report, and are listed with the reason and the `--force` hint. `--force`, any `--limit` / `--rows` (trial runs write their own file), and the flat layout bypass the check; a `.partial` file is a different name so never counts. If an input is missing, the md5 is not skipped, so the missing-file message still appears. Skipped md5s count as not failed, so the exit code stays 0; whether they count as wrong for `--fail-on-wrong` is D12.

### D7. Outputs and paths in the folder layout
`verified/` is created just before the first write (after the inputs have been validated, so a bad md5 leaves no folder behind). The verified CSV, trial CSV, debug report and `.partial` file all sit in `verified/`; the existing write-then-replace and writability test work unchanged on the new path. Messages show paths relative to the working folder with forward slashes (`relevant/abc123.parquet`), so they read the same on every platform.

### D8. Run total
`main` counts checked, skipped and failed md5s and prints one final line. The line is added in both layouts; it is informational and does not touch the flat layout's files or results.

### D9. Tests
A folder-layout fixture builds `extracted/`, `original/`, `relevant/` from the same documents and cases as the flat fixture, so the existing golden files apply to it unchanged. The parity check writes the same rows as CSV and as an all-text parquet and compares the verified rows. Typed-cell tests build parquet with integer, float, boolean, null, timestamp, list and struct columns. Skipping tests control times with `os.utime`. A regression test, like the one for `RowCursor`, scans a multi-row-group extracted parquet and fails if Arrow's allocation grows with the file.

### D10. Parquet cells without pandas or a time zone database (a review finding)
`to_pylist()` of a timestamp with a time zone needs the `tzdata` package on Windows, and of a timestamp, time or duration with nanoseconds needs pandas; a machine with only pyarrow stopped the md5 at the first such extra column, without naming it, and the suite passed only because pandas brings tzdata. Temporal arrays are therefore converted from their stored integers (`array.view` to int32 or int64, then integer arithmetic): a civil-date algorithm for the date, divmod for the clock and the fraction (nothing, six digits when the value is no finer than a microsecond, nine otherwise), and Python's timedelta text for durations with a minus sign for negatives. The text equals `isoformat()` and `str(timedelta)` for every value Python can hold (checked against 100,000 random values) and also works for years outside Python's range. A time zone is a label on a UTC instant, so a zone-aware timestamp is written as that instant with `+00:00` whatever the zone is called; applying the zone would need the database and would give different text on different machines. Maps become a JSON object with keys as text, because `to_pylist` gave a list of pairs and the documentation promised an object; a map with a repeated key, which an object cannot hold, stays a list of pairs. Temporal and map values inside lists and structs go through a recursive `arrow_to_python`; types with none of them keep the fast `to_pylist` path. A conversion that still fails raises an input error naming the column and its Arrow type, so only that md5 stops.

### D11. The script's own time is an input of the skip rule (a review finding)
A result written by older code is not trustworthy after the script is fixed, so the skip compares the verified file with the script file's modification time as well. Copying a new version of the script to the VM rechecks everything once, which is the safe direction; copying it with an old time does not, and `--force` covers that. The comparison is strict: equal times recheck, because a file system with coarse timestamps can give a stale result and a changed input the same tick; the cost of strictness is one unnecessary recheck, the cost of leniency a silent miss. The exploration answer was "newer than all inputs", which is what this is.

### D12. `--fail-on-wrong` counts skipped md5s
A gate that exits 3 on the first run and 0 on a re-run that skipped everything is worse than no gate. With `--fail-on-wrong`, the skip step reads the skipped verified file for its `Wrong` overall rows (a streaming pass with the csv module: 8 s for 4,000,000 rows in a 745 MB file) and the md5 counts like a checked one; the number is printed in the skip line. Without the switch the file is not read, so a plain re-run stays instant. A verified file that does not parse as a result (no header ending in the five verification columns, or an unreadable file) is checked again instead of skipped.

### D13. Md5s that differ only in letter case are stopped (a review finding)
`abc.csv` and `ABC.parquet` are two md5s to a case-sensitive file system and one to Windows, where both read `original/abc.parquet` and write `verified/abc_verified.csv`, so one result replaced the other and the first was reported as skipped without ever being checked. Discovery groups md5s by case-folded name and gives every member of a group of two or more an error naming the others, so neither runs and the exit code is 1. This is done on every system, not only Windows, so that a folder that works on Linux still works when copied to the VM.

### D14. CSV encodings (a review finding)
Detection reads the byte order mark first (UTF-32, then UTF-16, then UTF-8), then tries strict UTF-8, then Windows-1252, then Latin-1. A UTF-8 file with one stray byte used to fall to Windows-1252, which turns every accented character into two wrong ones and reports false mismatches; now, before accepting Windows-1252, a pass over the file counts valid multi-byte characters and invalid bytes, and when valid characters outnumber invalid bytes the file is UTF-8 read with `errors="replace"`, with the count and the first line in a note, and the output gets a byte order mark. A real Windows-1252 file has almost no valid multi-byte sequences, so it is unaffected. UTF-16 without a byte order mark cannot be told from other text except by its NUL bytes: they are rare in real CSV, so an automatic detection that finds one in the first 64 KB stops the md5 with a message naming UTF-16 and `--csv-encoding utf-16-le`, rather than reading a garbled header and reporting every column missing.

### D15. A trial run's debug report has its own file (a review finding)
`--debug --limit N` wrote `<md5>_debug_report.txt` over the report of a full run, which the trial CSV naming existed to prevent for results. The report of a trial run goes to `<md5>_debug_report_trial.txt`. The paste block shows the extracted file as `extracted/<md5>.csv` (or `<md5>_extracted.csv`), never the real md5, to keep the rule that the pasted text carries no names from the VM; the local report file keeps the real name.

## Risks / Trade-offs

- **Modification times decide two things (newer format, skip)** -> Copying files resets times, which can only cause a recheck (safe) for inputs; a verified file copied in from elsewhere with a newer time would be skipped wrongly. Mitigation: `--force`, the skip line says why, and the docs say so. Equal times for two formats stop rather than guess, and equal times between a verified file and an input recheck.
- **Reading a verified file for `--fail-on-wrong`** -> Seconds for millions of rows, only with that switch, and a file that cannot be read is checked again rather than trusted.
- **The skip rule does not know the switches behind an existing result** -> Running with `--check-same-document` or `--debug` on an up-to-date md5 changes nothing until `--force`. Stated in the requirement and the docs.
- **A real extracted parquet may have a typed `Value`** -> It stops that md5 with the column and type, and the first real file will show whether this happens; supporting it would be a new change with its own rules.
- **Hand-made and tool-made stray files in `extracted/`** -> `.`-prefixed and `~$` files and other extensions are ignored; any other `.csv` or `.parquet` is treated as an md5, so a stray `copy of abc.csv` becomes an md5 whose siblings are missing and is reported by name.
- **Sibling extensions are lower-case** -> `original/ABC.PARQUET` is not found on a case-sensitive file system; the message names the expected file.
- **Memory is bounded by one row group of the extracted parquet** -> As for the original file; the debug profile shows the row-group count.
- **The folder layout is selected by the existence of `extracted/`** -> An empty `extracted/` folder selects it and reports nothing to check, rather than silently falling back to flat files.

## Open Questions

- Whether a switch to check only named md5s is wanted once the folders grow (not needed for a handful of sets).

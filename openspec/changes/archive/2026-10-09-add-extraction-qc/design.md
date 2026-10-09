# Design

## Context

See proposal.md for motivation. The repository currently holds only `parquet_to_csv.py` (pandas + pyarrow, whole-file loads) and an unrelated nested `scripts/` repository, so there is no existing structure to extend.

Constraints that shape the approach:
- The real inputs live on a VM and cannot be copied out. Only a very small amount of masked text can be reported back, so the design cannot be tuned against real data in advance and must make its own first run informative.
- `pyarrow` is installed on the VM; nothing else can be assumed.
- The original parquet has 500k to 1m rows, each a DynamoDB JSON document that may be tens of KB. The extracted CSV is likely several million rows (roughly 10 to 30 values per document), grouped by document.
- `SourceElementPath` has no DynamoDB type tags (`$.Item.scopeIds[0].partnerId`), but the documents do.

## Goals / Non-Goals

**Goals:**
- Correct verdicts at 1m-row scale with memory independent of row count.
- A first real run that exposes any wrong assumption (index shifts, value spellings, JSON column) through the debug report.
- Strict checking whose failures are explained, never silent.

**Non-Goals:**
- Recall (relevant fields that were not extracted), `domain.csv` validation, md5 verification.
- Parallel execution, cloud storage inputs, or any change to the extraction process itself.

## Decisions

### D1. One script, standard library plus pyarrow
A single `extraction_qc.py` at the repo root using `csv`, `json`, `argparse` and `pyarrow.parquet`. Alternatives: pandas (loads whole files, defeats the memory goal), DuckDB or orjson (not guaranteed on the VM). Reading the parquet through `ParquetFile.iter_batches` limited to the JSON column uses an API present in long-supported pyarrow versions. It must be used one row group at a time: measured with pyarrow 25.0.1, a single `iter_batches` reader spanning many row groups keeps everything it has read (Arrow allocation grew to the size of the compressed file even though no batch was kept), whereas a fresh reader per row group (`row_groups=[g]`) stays flat. The cursor therefore moves from one row group's reader to the next.

### D2. Two lanes that each stream the CSV, joined in a final merge pass

```
 extracted.csv --> [lane: relevant] --> relevant_results (temp, one line per CSV row)
        |                 ^ relevant.parquet
        +-------> [lane: original] --> original_results (temp)
        |                 ^ original.parquet
        v
   merge pass: CSV row + relevant result + original result
               -> overall verdict -> <md5>_verified.csv
```

A lane is one routine parameterised by (parquet file, line column). Each lane re-reads the CSV and writes a compact per-row result to a temporary file; the merge pass zips the CSV with both result streams and computes the overall verdict. Alternative: one pass with two parquet cursors, rejected because each lane's line order can differ, and because the lane split is what makes independent lanes, skip-on-failure and later parallelism simple. Lanes run one after the other, relevant first (the smaller file). Skip-on-failure works because the original lane can read the relevant lane's results before it starts.

### D3. Chunked sort-and-scan keeps memory bounded and order irrelevant
Within a lane the CSV is processed in fixed-size chunks. Each chunk's rows are grouped by line number and visited in ascending order, so the parquet is read forward through only the row groups the chunk needs, and each needed document is parsed once and used for every CSV row that shares its line. A CSV sorted by line (the expected case) makes the whole lane one forward pass. An unsorted CSV costs more passes but stays correct and bounded, which satisfies the "no dependence on row order" requirement. Alternative: random access with a row-group cache, rejected because a row group of large documents can itself be gigabytes.

### D4. Adjacent-line hint uses a sliding window, parsed lazily
While scanning, the lane keeps the previous, current and next raw rows. Neighbours are parsed only when the current row fails, so the hint costs nothing for passing rows and little for failing ones (the neighbours are already in memory). Batch boundaries are handled by carrying the last row of the previous batch and peeking the first row of the next.

### D5. Path resolution
`SourceElementPath` is tokenised into keys and array indexes (`.key`, `[n]`, `['key']`). The walk unwraps a DynamoDB wrapper (`M`, `L`, `S`, `N`, `BOOL`, `NULL`, and set types) only when it is a single-key object whose payload has the type that tag implies, which avoids misreading an attribute that happens to be named `S` or `M`. The resolved node is returned together with its source type tag, which the value check and the debug value profile both need. Numbers are parsed with hooks that keep their original text, because strict matching must see `12345.0`, not `12345`.

### D6. Strict match, with a separate classifier for the reason
Matching is exact text equality on the value's textual form (spec: strict value comparison). Only when matching fails, a classifier tries whitespace trimming, case folding, numeric equality and a fixed list of date formats; if any makes the two equal the reason is `FORMAT_CHANGED`, otherwise `VALUE_MISMATCH`. The classifier never changes a verdict, so its date-format ambiguity (for example `01/05/2024`) only affects the label. The textual form of booleans, nulls and maps lives in one function so it can be adjusted without touching the rest.

### D7. JSON column detection
If a parquet file has one string or binary column it is the JSON column. With several, the first column whose first row parses as a JSON object is chosen. Otherwise the run stops listing the candidates. `--original-column` and `--relevant-column` override detection. Binary values are decoded as UTF-8, and a document that parses to a string is parsed once more to cover double-encoded JSON.

### D8. Debug aggregation is bounded and masked at the source
A collector is fed during the lane and merge passes: counters keyed by path shape (collapsing to an `(other)` bucket beyond a few thousand shapes), reason counters, value-shape counters keyed by source type, a small reservoir of failure traces per reason, and a skeleton merged from the first 200 documents of each file (the item's own top-level attribute names are never collapsed, only nested maps with more than 20 keys). The paste block builder works to a line budget, ranks entries by failure count, and ends truncated sections with `(+n more)`. Masking (uppercase letters to `A`, other letters to `a`, digits to `9`; case is kept because the spelling of booleans matters) is applied when a value enters the collector, so real values are held only when `--show-values` is set and then only for the local report.

### D9. Row filtering for trial runs
`--limit` and `--rows` are a single filter on the CSV stream that feeds both lanes and the merge pass, so all three see the same rows and the verified CSV and debug report cover exactly those rows.

### D10. Input discovery and failure behaviour
The script globs `*_extracted.csv` in the working folder, takes the text before `_extracted.csv` as the hash, and requires `<hash>_original.parquet` and `<hash>_relevant.parquet`. Missing files or required columns stop that triple before any row is processed. Per-row problems (bad line number, unparsable path, invalid JSON) never stop a run; they become `Wrong` rows with a reason. The exit code is non-zero only for input errors, not for `Wrong` results. Progress goes to stderr periodically and the summary to stdout.

### D11. Testing with synthetic fixtures
There is no real data here, so tests generate small parquet and CSV fixtures: typed and plain JSON, several row groups, several string columns, unsorted rows, and one example of each reason code. A larger generated file (about 1m rows) is used once to check runtime and memory, not kept in the repo.

### D12. Robust CSV input
The CSV field size limit is raised to the platform maximum, so a very long Value is an ordinary row. The encoding is found by decoding the file once, strictly, in 1 MB pieces: UTF-8 if that succeeds, otherwise Windows-1252 (what Excel's "CSV (Comma delimited)" writes), otherwise Latin-1, which cannot fail. `--csv-encoding` overrides the guess. The verified CSV is written as UTF-8 with a byte order mark whenever the input was not plain UTF-8: reasons contain text from the parquet files that a Windows-1252 file could not hold. Alternative: decode with replacement characters, rejected because values are compared, so a silent substitution could turn a correct row into a wrong one or the reverse. A required column that appears twice in the header stops the run, since the script would otherwise silently use one of them.

### D13. One file's failure never costs the others, and exit codes mean something
Each extracted CSV is processed in its own guard that catches any exception, reports a message naming the file (with the traceback only under `--debug`), removes its partial output and continues. Exit code 1 means some file could not be checked; 0 means everything was checked, whatever the verdicts; `--fail-on-wrong` adds code 3 for "everything checked, some rows wrong", off by default so existing callers are not affected.

### D14. Protect finished work
Before any row is read, the script tests that the output file can be opened for writing if it already exists (a spreadsheet holding it open on Windows makes this fail) and stops at once. If the final replace still fails, the `.partial` file is kept and named in the message, because it holds hours of work. Trial runs (`--limit`, `--rows`) write `<md5>_verified_trial.csv`, so a trial can never overwrite a full result. `--temp-dir` moves the working files (about 12 bytes per correct row and some hundred per failing row, per file) off the system drive.

### D15. Console output cannot fail
stdout and stderr are reconfigured to escape characters the console encoding cannot show, because the paste block exists to be copied out and a traceback at that point would hide it. The debug report file is always UTF-8.

### D16. Each document is parsed once
A lane keeps the last few parsed documents (a small cache keyed by row index). The adjacent-line hint reads the lines before and after a failing row; with a sequential scan the next line to be checked is the one just read as a neighbour, so it is reused instead of parsed again. Without this a run in which every row fails parsed every document three times.

### D17. Optional same-document check
The lanes are independent, so a wrong SourceLine that lands on another original document holding the same value (`true`, an enum code) passes. `--check-same-document` closes this: the original lane also opens the relevant file, and for every pair (original line, relevant line) checks once that the relevant document is contained in the original one, with every value equal at the same path (lists compared by index, the same assumption as the paths themselves). A miss makes the original check `Wrong` with `DOCUMENT_MISMATCH` and the first differing path. It is opt-in because it is only valid if the relevant rows really are subsets of the originals, which is not known; with it on, an array-index shift between the files shows up as `DOCUMENT_MISMATCH` instead of staying hidden. Cost: a second parse per document.

### D18. Masking keys that are really data
Keys are structure, but a map keyed by email addresses or identifiers would put data in the paste block. A key is shown as it is only when it looks like an identifier (at most 40 letters, digits and underscores, not starting with a digit, no run of three or more digits); any other key is masked like a value. The rule is a heuristic and is applied to keys in the skeleton, path shapes and traces.

## Risks / Trade-offs

- **The extractor's spelling of booleans, nulls and maps is unknown** -> The strict rules in the spec are provisional for those types. The debug value-shape profile shows the real spellings after one run, and D6 keeps the rendering in one place. Expect to revisit the strict-comparison requirement once real shapes are seen.
- **Array indexes may differ between the relevant and original files** -> The path-shape table shows a lane-specific `PATH_NOT_FOUND` pattern immediately. If confirmed, the original lane needs an index mapping, which would be a new change.
- **The JSON column may not be a single string column** -> Detection plus explicit overrides plus a clear stop message. A nested-column layout would need a new reader.
- **Runtime at 1m documents is dominated by JSON parsing and is unmeasured** -> One parse per distinct line, lazy neighbour parsing, progress output, and a generated 1m-row timing check during implementation. Parallel lanes are a later option.
- **An unsorted CSV is slower** -> Bounded memory and correct results are kept; the debug input profile reports sortedness so the cause is visible.
- **Masked output could still leak structure** -> Nested maps with more than 20 distinct keys are collapsed, keys that do not look like identifiers are masked (D18), and values are never shown in the paste block. A data-like key that happens to look like an identifier (for example `alice`) would still be shown, so the user should still read the block before copying it out.
- **A wrong line that hits an identical value in another document passes** -> `--check-same-document` (D17), off by default.
- **Date-format classification is a best-effort label** -> It does not affect verdicts.
- **Older pyarrow on the VM** -> Only long-standing APIs are used; the version is printed in the debug profile.
- **Memory is bounded by the row-group size, not by the row count** -> Each reader holds one row group's pages while it is read. A file with very large row groups needs correspondingly more memory; the debug input profile shows the row-group count so this is visible. A regression test scans a 100 MB multi-row-group file and fails if Arrow's allocation grows with the file.

## Open Questions

- Whether to run the two lanes in parallel processes once real runtimes are known.
- The right progress-report interval for very large runs.

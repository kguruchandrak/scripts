"""QC check for the parquet extraction process.

Verifies that every row of an extracted-values CSV is true: that the row at
RelevancyParquetLine of the relevant parquet file, and the row at SourceLine of
the original parquet file, really contain SourceElementPath with Value.

Inputs, all in the working folder and sharing one hash prefix:
    <md5>_extracted.csv     one row per extracted value
    <md5>_original.parquet  original DynamoDB documents, one JSON text per row
    <md5>_relevant.parquet  relevant rows only, same layout

Output: <md5>_verified.csv = every column of the extracted CSV plus
    RelevantFileVerification, RelevantFileReason,
    OriginalFileVerification, OriginalFileReason, OverallVerification

Usage:
    python extraction_qc.py
    python extraction_qc.py --debug --limit 2000
    python extraction_qc.py --skip-original-on-relevant-failure

See EXTRACTION_QC.md for the rules and the reason codes.
Requires: pip install pyarrow   (Python 3.8+)
"""
import argparse
import codecs
import csv
import json
import os
import platform
import re
import sys
import tempfile
import traceback
from bisect import bisect_right
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # reported when a parquet file is first opened
    pa = pq = None

EXTRACTED_SUFFIX = "_extracted.csv"
REQUIRED_COLUMNS = ["SourceLine", "RelevancyParquetLine", "SourceElementPath", "Value"]


class InputError(Exception):
    """A problem with the input files that stops the affected run before any row is checked."""


def raise_csv_field_limit() -> None:
    """Accept CSV fields of any length (the default limit is 131,072 characters)."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:  # the limit is a C long, which is 32 bits on Windows
            limit //= 2


raise_csv_field_limit()


# --------------------------------------------------------------------------- #
# Input discovery
# --------------------------------------------------------------------------- #
class Triple:
    """The three input files that share one hash prefix, and the output they produce."""

    def __init__(self, folder: Path, prefix: str):
        self.prefix = prefix
        self.extracted = folder / (prefix + EXTRACTED_SUFFIX)
        self.original = folder / (prefix + "_original.parquet")
        self.relevant = folder / (prefix + "_relevant.parquet")
        self.verified = folder / (prefix + "_verified.csv")
        self.trial = folder / (prefix + "_verified_trial.csv")
        self.debug_report = folder / (prefix + "_debug_report.txt")
        self.encoding = "utf-8-sig"   # of the extracted CSV; set by prepare_triple
        self.encoding_note = ""

    def check_files(self) -> None:
        missing = [p.name for p in (self.original, self.relevant) if not p.is_file()]
        if missing:
            raise InputError(
                "%s: missing input file(s) in %s: %s"
                % (self.extracted.name, self.extracted.parent, ", ".join(missing))
            )


def find_triples(folder: Path) -> list:
    """One Triple per <prefix>_extracted.csv in the folder (files are located by name only)."""
    return [
        Triple(folder, path.name[: -len(EXTRACTED_SUFFIX)])
        for path in sorted(folder.glob("*" + EXTRACTED_SUFFIX))
        if path.is_file()
    ]


def check_required_columns(header: list, csv_name: str) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise InputError("%s: missing required column(s): %s" % (csv_name, ", ".join(missing)))
    twice = [c for c in REQUIRED_COLUMNS if header.count(c) > 1]
    if twice:
        raise InputError("%s: column(s) %s appear more than once in the header, so it is ambiguous which to use"
                         % (csv_name, ", ".join(twice)))


def _decodes(path, encoding: str) -> bool:
    """True when the whole file decodes as `encoding` (read in 1 MB pieces)."""
    decoder = codecs.getincrementaldecoder(encoding)()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            try:
                decoder.decode(chunk, final=not chunk)
            except UnicodeDecodeError:
                return False
            if not chunk:
                return True


def detect_csv_encoding(path) -> str:
    """UTF-8 when the file is valid UTF-8, else Windows-1252 (what Excel's "CSV (Comma delimited)"
    writes), else Latin-1, which can decode any bytes."""
    with open(str(path), "rb") as handle:
        bom = handle.read(3) == b"\xef\xbb\xbf"
    if _decodes(path, "utf-8"):
        return "utf-8-sig" if bom else "utf-8"
    return "cp1252" if _decodes(path, "cp1252") else "latin-1"


def read_header(csv_path: Path, encoding: str = "utf-8-sig") -> list:
    with open(str(csv_path), newline="", encoding=encoding) as handle:
        header = next(csv.reader(handle), None)
    if not header:
        raise InputError("%s: the file is empty (no header row)" % csv_path.name)
    return header


def prepare_triple(triple: Triple, header_out: list = None, encoding: str = None) -> list:
    """Validate the inputs of one triple before any row is processed; return the CSV header.

    Also settles the encoding of the extracted CSV (`encoding` forces it) into triple.encoding.
    """
    triple.check_files()
    if encoding:
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise InputError("unknown CSV encoding %r" % encoding)
        if not _decodes(triple.extracted, encoding):
            raise InputError("%s: the file cannot be decoded as %s" % (triple.extracted.name, encoding))
        triple.encoding = encoding
    else:
        triple.encoding = detect_csv_encoding(triple.extracted)
    is_utf8 = codecs.lookup(triple.encoding).name in ("utf-8", "utf-8-sig")
    triple.encoding_note = "" if is_utf8 else "%s is not UTF-8; it was read as %s" % (
        triple.extracted.name, triple.encoding)
    header = read_header(triple.extracted, triple.encoding)
    check_required_columns(header, triple.extracted.name)
    if header_out is not None:
        header_out[:] = header
    return header


# --------------------------------------------------------------------------- #
# DynamoDB JSON documents and SourceElementPath
# --------------------------------------------------------------------------- #
class QCError(Exception):
    """A per-row problem; becomes a Wrong result whose reason starts with `code`."""

    code = ""


class UnsupportedPath(QCError):
    code = "UNSUPPORTED_PATH"


class InvalidJson(QCError):
    code = "INVALID_JSON"


class PathNotFound(QCError):
    """The path does not exist in the document.  `walked` is the part that did resolve."""

    code = "PATH_NOT_FOUND"

    def __init__(self, message: str, walked: str, stopped_at: str):
        super().__init__(message)
        self.walked = walked
        self.stopped_at = stopped_at


class NumText(str):
    """A JSON number kept as its original text, so that 12345.0 is not read back as 12345."""


# DynamoDB attribute type tag -> Python type of its payload in the JSON
_WRAPPER_TYPES = {
    "S": str, "N": str, "B": str, "BOOL": bool, "NULL": bool,
    "SS": list, "NS": list, "BS": list, "M": dict, "L": list,
}
LIST_TAGS = ("L", "SS", "NS", "BS")
CONTAINER_TAGS = ("M",) + LIST_TAGS
_PATH_TOKEN = re.compile(r"\.([^.\[\]]+)|\[(\d+)\]|\['([^']*)'\]|\[\"([^\"]*)\"\]")


def parse_json_text(text):
    """json.loads that keeps every number as its original text."""
    return json.loads(text, parse_float=NumText, parse_int=NumText, parse_constant=NumText)


def parse_doc(raw):
    """Parse the JSON text of one parquet row (text or UTF-8 bytes; double-encoded JSON is unwrapped)."""
    if raw is None:
        raise InvalidJson("the row holds no value")
    if not isinstance(raw, (str, bytes, bytearray)):
        raise InvalidJson("the row holds %s, not JSON text" % type(raw).__name__)
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        doc = parse_json_text(raw)
        if isinstance(doc, str):  # JSON that was encoded twice
            doc = parse_json_text(doc)
    except (ValueError, RecursionError):
        raise InvalidJson("the row does not hold valid JSON")
    return doc


def unwrap(node):
    """Strip one DynamoDB type wrapper and return (type tag, value).

    {"S": "x"} -> ("S", "x"); {"L": [...]} -> ("L", [...]).  Plain JSON values get an
    inferred tag.  A wrapper is recognised only when it is a one-key object whose payload
    has the type its tag implies, so an attribute that merely happens to be named S or M
    is not misread.
    """
    if isinstance(node, dict) and len(node) == 1:
        (tag, inner), = node.items()
        expected = _WRAPPER_TYPES.get(tag)
        if expected is not None and isinstance(inner, expected):
            if tag == "NULL":
                return "NULL", None
            if tag == "N":
                return "N", NumText(inner)
            return tag, inner
    if node is None:
        return "NULL", None
    if isinstance(node, bool):
        return "BOOL", node
    if isinstance(node, NumText):
        return "N", node
    if isinstance(node, str):
        return "S", node
    if isinstance(node, dict):
        return "M", node
    if isinstance(node, list):
        return "L", node
    return "S", str(node)


def plain_of(tag, value):
    """A (tag, value) pair as plain Python data with every DynamoDB wrapper removed."""
    if tag == "M":
        return {key: to_plain(item) for key, item in value.items()}
    if tag == "L":
        return [to_plain(item) for item in value]
    if tag in ("SS", "NS", "BS"):
        return list(value)
    return value


def to_plain(node):
    return plain_of(*unwrap(node))


def render_path(tokens) -> str:
    return "$" + "".join("[%d]" % t if isinstance(t, int) else ".%s" % t for t in tokens)


@lru_cache(maxsize=50000)
def parse_path(path: str) -> tuple:
    """'$.Item.a[0].b' -> ('Item', 'a', 0, 'b'); integers are array indexes."""
    text = (path or "").strip()
    if text.startswith("$"):
        text = text[1:]
    if text and text[0] not in ".[":
        text = "." + text
    tokens, pos = [], 0
    for match in _PATH_TOKEN.finditer(text):
        if match.start() != pos:
            break
        key, index, single_quoted, double_quoted = match.groups()
        if index is not None:
            tokens.append(int(index))
        else:
            tokens.append(next(k for k in (key, single_quoted, double_quoted) if k is not None))
        pos = match.end()
    if not tokens or pos != len(text):
        raise UnsupportedPath("cannot parse SourceElementPath %r" % path)
    return tuple(tokens)


def resolve(doc, tokens) -> tuple:
    """Walk a parsed document along tokens; return (tag, value) of the node found."""
    node = doc
    for step, tok in enumerate(tokens):
        tag, value = unwrap(node)
        walked = render_path(tokens[:step])
        if isinstance(tok, int):
            if tag not in LIST_TAGS:
                raise PathNotFound("expected a list at %s but found %s" % (walked, tag), walked, "[%d]" % tok)
            if tok >= len(value):
                raise PathNotFound("index %d is outside the %d-element list at %s" % (tok, len(value), walked),
                                   walked, "[%d]" % tok)
            node = value[tok]
        else:
            if tag != "M":
                raise PathNotFound("expected a map at %s but found %s" % (walked, tag), walked, tok)
            if tok not in value:
                raise PathNotFound("no key %r under %s" % (tok, walked), walked, tok)
            node = value[tok]
    return unwrap(node)


# --------------------------------------------------------------------------- #
# Value comparison
# --------------------------------------------------------------------------- #
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y", "%Y%m%d",
    "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
)
_NULL_SPELLINGS = ("null", "none", "nan")


def source_text(tag, value) -> str:
    """The text a scalar source value is compared against (strict comparison)."""
    if tag == "BOOL":
        return "true" if value else "false"
    if tag == "NULL":
        return ""
    return str(value)


def found_text(tag, value, limit: int = 200) -> str:
    """The source value as text for messages; containers are shown as compact JSON."""
    if tag in CONTAINER_TAGS:
        text = json.dumps(plain_of(tag, value), separators=(",", ":"), ensure_ascii=False, default=str)
    else:
        text = source_text(tag, value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def json_equal(a, b) -> bool:
    """Equality of two plain JSON values in which a number (NumText) never equals a string."""
    if isinstance(a, NumText) != isinstance(b, NumText):
        return False
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def values_match(expected, tag, value) -> bool:
    """Strict comparison of the extracted Value with the source value.

    Scalars must be identical text.  A map or list matches when Value is JSON of the same
    structure (spacing and key order do not matter; a number is not a string).
    """
    expected = "" if expected is None else expected
    if tag in CONTAINER_TAGS:
        try:
            parsed = parse_json_text(expected)
        except (ValueError, RecursionError):
            return False
        plain = plain_of(tag, value)
        return json_equal(parsed, plain) or json_equal(to_plain(parsed), plain)
    return expected == source_text(tag, value)


@lru_cache(maxsize=8192)
def _parse_dates(text: str) -> frozenset:
    found = set()
    for fmt in _DATE_FORMATS:
        try:
            found.add(datetime.strptime(text, fmt))
        except ValueError:
            pass
    try:
        found.add(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    return frozenset(found)


def _same_number(a: str, b: str) -> bool:
    return bool(_NUMBER.match(a) and _NUMBER.match(b)) and Decimal(a) == Decimal(b)


def _same_date(a: str, b: str) -> bool:
    if len(a) < 6 or len(b) < 6 or not any(c.isdigit() for c in a) or not any(c.isdigit() for c in b):
        return False
    return bool(_parse_dates(a) & _parse_dates(b))


def classify_mismatch(expected, found: str) -> str:
    """FORMAT_CHANGED when the two texts differ only in formatting, else VALUE_MISMATCH.

    Only labels a failure; the verdict is Wrong either way.
    """
    e, f = (expected or "").strip(), found.strip()
    if e == f or e.casefold() == f.casefold():
        return "FORMAT_CHANGED"
    if _same_number(e, f) or _same_date(e, f):
        return "FORMAT_CHANGED"
    if f == "" and e.casefold() in _NULL_SPELLINGS:
        return "FORMAT_CHANGED"
    return "VALUE_MISMATCH"


def clip(text, limit: int = 60) -> str:
    text = repr(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def mismatch_reason(expected, tag, value) -> str:
    """The reason text for a value that does not match: a code followed by the details."""
    found = found_text(tag, value)
    code = classify_mismatch(expected, found) if tag not in CONTAINER_TAGS else "VALUE_MISMATCH"
    return "%s: expected %s, found %s (source type %s)" % (code, clip(expected), clip(found), tag)


# --------------------------------------------------------------------------- #
# Parquet files
# --------------------------------------------------------------------------- #
class ParquetJson:
    """A parquet file whose rows each hold one JSON document as text in a single column."""

    def __init__(self, path, column: str = None, option: str = "--original-column"):
        if pq is None:
            raise InputError("pyarrow is required to read parquet files: pip install pyarrow")
        self.path = Path(path)
        try:
            self.pf = pq.ParquetFile(str(self.path))
        except Exception as exc:
            raise InputError("%s: cannot read the parquet file: %s" % (self.path.name, exc))
        meta = self.pf.metadata
        self.num_rows = meta.num_rows
        self.num_groups = meta.num_row_groups
        self.starts = [0]  # absolute index of the first row of each row group, plus the total
        for group in range(self.num_groups):
            self.starts.append(self.starts[-1] + meta.row_group(group).num_rows)
        self.option = option
        try:
            self.column = self._choose_column(column)
        except Exception:
            self.close()
            raise
        self.column_type = str(self.pf.schema_arrow.field(self.column).type)

    def close(self) -> None:
        """Release the file handle (an open handle blocks deleting the file on Windows)."""
        close = getattr(self.pf, "close", None)
        if close is not None:
            close()

    @staticmethod
    def _is_text_type(kind) -> bool:
        """String or binary columns, including dictionary-encoded ones and the newer 'view' types."""
        if pa.types.is_dictionary(kind):
            kind = kind.value_type
        checks = [pa.types.is_string, pa.types.is_large_string, pa.types.is_binary, pa.types.is_large_binary]
        checks += [getattr(pa.types, name) for name in ("is_string_view", "is_binary_view") if hasattr(pa.types, name)]
        return any(check(kind) for check in checks)

    def _text_columns(self) -> list:
        return [field.name for field in self.pf.schema_arrow if self._is_text_type(field.type)]

    def _first_value_is_json_object(self, name: str) -> bool:
        if self.num_rows == 0:
            return False
        batch = next(self.pf.iter_batches(batch_size=1, columns=[name]), None)
        if batch is None:
            return False
        try:
            return isinstance(parse_doc(batch.column(0)[0].as_py()), dict)
        except QCError:
            return False

    def _choose_column(self, requested) -> str:
        names = self.pf.schema_arrow.names
        if requested:
            if requested not in names:
                raise InputError("%s: there is no column named %r (columns: %s)"
                                 % (self.path.name, requested, ", ".join(names)))
            kind = self.pf.schema_arrow.field(requested).type
            if not self._is_text_type(kind):
                raise InputError("%s: column %r has type %s, not text; the JSON must be stored as text"
                                 % (self.path.name, requested, kind))
            return requested
        candidates = self._text_columns()
        if not candidates:
            raise InputError("%s: no text column found to hold the JSON (columns: %s)"
                             % (self.path.name, ", ".join(names)))
        if len(candidates) == 1:
            return candidates[0]
        for name in candidates:
            if self._first_value_is_json_object(name):
                return name
        raise InputError("%s: cannot tell which column holds the JSON; candidates: %s. Name it with %s"
                         % (self.path.name, ", ".join(candidates), self.option))


BATCH_ROWS = 256


class RowCursor:
    """Mostly forward-only access to the raw JSON text of parquet rows (0-based row index).

    The JSON column is read in small batches, one row group at a time (a single reader spanning
    many row groups keeps everything it has read in memory, so memory would grow with the file).
    The cursor keeps the current batch plus the last two rows of the previous one, so a row just
    behind the position is still at hand; a row further back reopens the file at its row group.
    Whole row groups lying between two requested rows are never read.
    """

    def __init__(self, pj: ParquetJson, batch_rows: int = BATCH_ROWS):
        self.pj = pj
        self.batch_rows = batch_rows
        self._group = 0
        self._batches = None
        self._array = None
        self._start = 0   # absolute index of the first row of the current batch
        self._end = 0     # absolute index one past the last row of the current batch
        self._recent = {}

    def close(self) -> None:
        self._batches = None
        self._array = None
        self._recent = {}

    def _batches_of(self, group: int):
        pj = self.pj
        return pj.pf.iter_batches(batch_size=self.batch_rows, row_groups=[group], columns=[pj.column])

    def _open(self, group: int) -> None:
        self._group = group
        self._batches = self._batches_of(group)
        self._array = None
        self._start = self._end = self.pj.starts[group]
        self._recent = {}

    def _next_batch(self) -> None:
        if self._array is not None:
            self._recent = {
                i: self._array[i - self._start].as_py()
                for i in (self._end - 2, self._end - 1) if i >= self._start
            }
        batch = next(self._batches, None)
        while batch is None:  # this row group is used up: continue with the next one
            self._group += 1
            if self._group >= self.pj.num_groups:
                raise InputError("%s: the file ended before row %d" % (self.pj.path.name, self._end + 1))
            self._batches = self._batches_of(self._group)
            batch = next(self._batches, None)
        self._array = batch.column(0)
        self._start = self._end
        self._end += len(self._array)

    def get(self, idx: int):
        """Raw text of row idx, or None when idx is outside the file."""
        pj = self.pj
        if idx < 0 or idx >= pj.num_rows:
            return None
        try:
            if self._array is not None and self._start <= idx < self._end:
                return self._array[idx - self._start].as_py()
            if idx in self._recent:
                return self._recent[idx]
            group = bisect_right(pj.starts, idx) - 1
            if self._batches is None or idx < self._start:
                self._open(group)
            elif group > bisect_right(pj.starts, self._end) - 1:
                self._open(group)  # whole row groups lie in between: skip them
            while idx >= self._end:
                self._next_batch()
            return self._array[idx - self._start].as_py()
        except InputError:
            raise
        except Exception as exc:
            raise InputError("%s: cannot read row %d: %s" % (pj.path.name, idx + 1, exc))


# --------------------------------------------------------------------------- #
# The extracted CSV
# --------------------------------------------------------------------------- #
class CsvInput:
    """The extracted CSV: its header and column positions, and the row filter for trial runs."""

    def __init__(self, path, header: list, limit: int = None, rows: set = None, encoding: str = "utf-8-sig"):
        self.path = Path(path)
        self.header = header
        self.index = {name: i for i, name in enumerate(header)}
        self.limit = limit
        self.rows_filter = rows
        self.last_row = max(rows) if rows else None
        self.encoding = encoding

    @property
    def output_encoding(self) -> str:
        """The verified CSV is plain UTF-8 only when the input was; otherwise UTF-8 with a byte order mark
        (so Excel shows every character, including text from the parquet files that the input's own
        encoding may not be able to hold)."""
        return "utf-8" if codecs.lookup(self.encoding).name == "utf-8" else "utf-8-sig"

    def rows(self):
        """Yield (1-based data row number, fields) for the rows selected by --limit / --rows.

        Blank lines are not rows: they are skipped and not counted."""
        width = len(self.header)
        with open(str(self.path), newline="", encoding=self.encoding) as handle:
            reader = csv.reader(handle)
            next(reader, None)  # header
            number = 0
            for fields in reader:
                if not fields:  # blank line
                    continue
                number += 1
                if self.limit is not None and number > self.limit:
                    return
                if self.last_row is not None and number > self.last_row:
                    return
                if self.rows_filter is not None and number not in self.rows_filter:
                    continue
                if len(fields) < width:
                    fields = fields + [""] * (width - len(fields))
                yield number, fields


def parse_row_list(text: str) -> set:
    """'17,203' -> {17, 203} (1-based data row numbers)."""
    try:
        numbers = {int(part) for part in text.split(",") if part.strip()}
    except ValueError:
        raise InputError("--rows must be a comma-separated list of row numbers, got %r" % text)
    if not numbers or min(numbers) < 1:
        raise InputError("--rows must list row numbers starting at 1, got %r" % text)
    return numbers


# --------------------------------------------------------------------------- #
# Lanes: check every CSV row against one parquet file
# --------------------------------------------------------------------------- #
CORRECT, WRONG, SKIPPED = "Correct", "Wrong", "Skipped"
SKIP_REASON = "SKIPPED: relevant file check failed"
CHUNK_ROWS = 50000          # CSV rows per lane chunk; also the lane progress interval
MERGE_PROGRESS_ROWS = 100000
_LINE_NUMBER = re.compile(r"^[+-]?\d+(?:\.0*)?$")


class InvalidLine(QCError):
    code = "INVALID_LINE"


class Outcome:
    """The result of one lane for one CSV row.

    `tag` is the source value type found at the path ('-' when none); `info` carries the
    details a failure trace needs.
    """

    __slots__ = ("verdict", "reason", "tag", "info")

    def __init__(self, verdict: str, reason: str = "", tag: str = "-", info: dict = None):
        self.verdict = verdict
        self.reason = reason
        self.tag = tag
        self.info = info

    @property
    def code(self) -> str:
        return reason_code(self.reason)


def reason_code(reason: str) -> str:
    """'VALUE_MISMATCH: expected ...' -> 'VALUE_MISMATCH'; '' -> ''."""
    return reason.split(":", 1)[0].split(";", 1)[0] if reason else ""


def parse_line(text) -> int:
    """A line number as written in the CSV (a whole number; '5.0' from a float column is accepted)."""
    stripped = (text or "").strip()
    if not _LINE_NUMBER.match(stripped):
        raise InvalidLine("%s is not a whole number" % clip(text))
    return int(stripped.split(".")[0])


def check_row(doc, tokens, expected) -> Outcome:
    """Does the document hold the path with the expected value?"""
    try:
        tag, value = resolve(doc, tokens)
    except PathNotFound as exc:
        return Outcome(WRONG, "PATH_NOT_FOUND: %s" % exc, "-",
                       {"walked": exc.walked, "stopped_at": exc.stopped_at, "expected": expected})
    if values_match(expected, tag, value):
        return Outcome(CORRECT, "", tag)
    return Outcome(WRONG, mismatch_reason(expected, tag, value), tag,
                   {"expected": expected, "found": found_text(tag, value)})


def read_results(path):
    """Yield (verdict, reason, tag) for each row of a lane results file."""
    with open(str(path), newline="", encoding="utf-8") as handle:
        for record in csv.reader(handle):
            yield record[0], record[1], record[2]


_OUTSIDE = "outside the file"


class DocCache:
    """The last few parsed documents of a lane, by row index.

    The adjacent-line hint reads the lines before and after a failing row.  In a sequential scan the
    next line to be checked is the one just read as a neighbour, so it must not be parsed a second time
    (without this a run in which every row fails parsed every document three times).
    """

    def __init__(self, cursor: RowCursor, size: int = 8):
        self.cursor = cursor
        self.size = size
        self._docs = OrderedDict()

    def get(self, idx: int):
        """(document, problem): problem is None, _OUTSIDE (not a row of the file) or the InvalidJson raised."""
        if idx in self._docs:
            return self._docs[idx]
        raw = self.cursor.get(idx)
        if raw is None:
            result = (None, _OUTSIDE)
        else:
            try:
                result = (parse_doc(raw), None)
            except InvalidJson as exc:
                result = (None, exc)
        self._docs[idx] = result
        if len(self._docs) > self.size:
            self._docs.popitem(last=False)
        return result


def first_difference(small, big, path=()):
    """None when every value of `small` is also in `big` at the same path (plain data, lists by index);
    otherwise the path of the first difference as text."""
    try:
        if isinstance(small, dict):
            if not isinstance(big, dict):
                return render_path(path)
            for key, value in small.items():
                if key not in big:
                    return render_path(path + (key,))
                found = first_difference(value, big[key], path + (key,))
                if found is not None:
                    return found
            return None
        if isinstance(small, list):
            if not isinstance(big, list) or len(small) > len(big):
                return render_path(path)
            for i, value in enumerate(small):
                found = first_difference(value, big[i], path + (i,))
                if found is not None:
                    return found
            return None
    except RecursionError:
        return render_path(path)
    return None if json_equal(small, big) else render_path(path)


class SameDocument:
    """--check-same-document: reads the relevant file alongside the original lane."""

    def __init__(self, relevant: ParquetJson):
        self.cursor = RowCursor(relevant)
        self.docs = DocCache(self.cursor)

    def difference(self, original_doc, relevant_idx: int):
        """None if the relevant document is contained in original_doc (or cannot be read); else the
        path of the first difference."""
        relevant_doc, problem = self.docs.get(relevant_idx)
        if problem is not None:
            return None  # the relevant-file check reports an unreadable line
        return first_difference(to_plain(relevant_doc), to_plain(original_doc))

    def close(self) -> None:
        self.cursor.close()


def _evaluate_line(docs: DocCache, idx: int, items: list, line_col: str, outcomes: list, same=None) -> None:
    """Check every CSV row that points at row idx; fill outcomes by position."""
    doc, error = docs.get(idx)
    problem = None
    if error is _OUTSIDE:
        problem = Outcome(WRONG, "LINE_OUT_OF_RANGE: %s %d is outside the file (%d rows)"
                          % (line_col, idx + 1, docs.cursor.pj.num_rows))
    elif error is not None:
        problem = Outcome(WRONG, "INVALID_JSON: %s (%s %d)" % (error, line_col, idx + 1))

    compared = {}  # relevant row index -> first difference, for this original line

    for pos, tokens, expected, relevant_idx in items:
        outcome = problem if problem is not None else check_row(doc, tokens, expected)
        if outcome.verdict == WRONG:
            hints = ["FOUND_AT_LINE_%d" % (i + 1) for i in (idx - 1, idx + 1)
                     if docs.get(i)[0] is not None and check_row(docs.get(i)[0], tokens, expected).verdict == CORRECT]
            if hints:
                outcome = Outcome(WRONG, "%s; %s" % (outcome.reason, "; ".join(hints)), outcome.tag, outcome.info)
        if same is not None and problem is None and relevant_idx is not None:
            if relevant_idx not in compared:
                compared[relevant_idx] = same.difference(doc, relevant_idx)
            difference = compared[relevant_idx]
            if difference is not None:
                detail = "DOCUMENT_MISMATCH: relevant line %d is not contained in original line %d; first " \
                         "difference at %s" % (relevant_idx + 1, idx + 1, difference)
                info = dict(outcome.info or {}, difference=difference)
                outcome = Outcome(WRONG, detail if outcome.verdict == CORRECT else "%s; %s" % (outcome.reason, detail),
                                  outcome.tag, info)
        outcomes[pos] = outcome


def _flush_chunk(label, line_col, chunk, docs, skip_iter, writer, collector, same=None) -> None:
    outcomes = [None] * len(chunk)
    work = defaultdict(list)  # 0-based row index -> [(position in chunk, tokens, expected, relevant row index)]
    for pos, (rownum, line_text, path_text, expected, other_text) in enumerate(chunk):
        if skip_iter is not None and next(skip_iter)[0] != CORRECT:
            outcomes[pos] = Outcome(SKIPPED, SKIP_REASON)
            continue
        try:
            line = parse_line(line_text)
        except InvalidLine as exc:
            outcomes[pos] = Outcome(WRONG, "INVALID_LINE: %s %s" % (line_col, exc))
            continue
        try:
            tokens = parse_path(path_text)
        except UnsupportedPath as exc:
            outcomes[pos] = Outcome(WRONG, "UNSUPPORTED_PATH: %s" % exc)
            continue
        relevant_idx = None
        if same is not None:
            try:
                relevant_idx = parse_line(other_text) - 1
            except InvalidLine:
                pass
        work[line - 1].append((pos, tokens, expected, relevant_idx))
    for idx in sorted(work):
        _evaluate_line(docs, idx, work[idx], line_col, outcomes, same)
    for (rownum, line_text, path_text, _expected, _other), outcome in zip(chunk, outcomes):
        writer.writerow([outcome.verdict, outcome.reason, outcome.tag])
        if collector is not None:
            collector.lane_outcome(label, rownum, line_text, path_text, outcome)


def run_lane(label: str, csv_input: CsvInput, line_col: str, pj: ParquetJson, results_path,
             skip_from=None, collector=None, progress=None, chunk_rows: int = None,
             same_document_with: ParquetJson = None) -> None:
    """Check each CSV row against one parquet file; write one result line per row, in CSV order.

    Rows are taken in chunks and visited in ascending order of their line, so a CSV that is
    sorted by line makes the whole lane one forward pass over the parquet file, while an
    unsorted CSV is still checked correctly (at the cost of more passes).  With skip_from (the
    results file of an earlier lane) rows that failed there are marked Skipped and not read.
    With same_document_with (the relevant file) each row's relevant document must also be
    contained in the original document.
    """
    chunk_rows = chunk_rows or CHUNK_ROWS
    line_i = csv_input.index[line_col]
    path_i = csv_input.index["SourceElementPath"]
    value_i = csv_input.index["Value"]
    other_i = csv_input.index["RelevancyParquetLine"]
    cursor = RowCursor(pj)
    docs = DocCache(cursor)
    same = SameDocument(same_document_with) if same_document_with is not None else None
    skip_iter = read_results(skip_from) if skip_from is not None else None
    done = 0
    try:
        with open(str(results_path), "w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            chunk = []
            for rownum, fields in csv_input.rows():
                chunk.append((rownum, fields[line_i], fields[path_i], fields[value_i], fields[other_i]))
                if len(chunk) >= chunk_rows:
                    _flush_chunk(label, line_col, chunk, docs, skip_iter, writer, collector, same)
                    done += len(chunk)
                    chunk = []
                    if progress is not None:
                        progress("[%s] %s rows checked" % (label, format(done, ",")))
            if chunk:
                _flush_chunk(label, line_col, chunk, docs, skip_iter, writer, collector, same)
    finally:
        cursor.close()
        if same is not None:
            same.close()
        if skip_iter is not None:
            skip_iter.close()


# --------------------------------------------------------------------------- #
# Merge pass, output and summary
# --------------------------------------------------------------------------- #
OUTPUT_COLUMNS = [
    "RelevantFileVerification", "RelevantFileReason",
    "OriginalFileVerification", "OriginalFileReason",
    "OverallVerification",
]


class Summary:
    """Counts for the end-of-run report."""

    LANES = ("relevant", "original", "overall")

    def __init__(self):
        self.rows = 0
        self.verdicts = {lane: Counter() for lane in self.LANES}
        self.reasons = {"relevant": Counter(), "original": Counter()}
        self.extra_fields = 0
        self.output_name = ""

    def add(self, relevant, original, overall: str) -> None:
        self.rows += 1
        self.verdicts["relevant"][relevant[0]] += 1
        self.verdicts["original"][original[0]] += 1
        self.verdicts["overall"][overall] += 1
        for lane, result in (("relevant", relevant), ("original", original)):
            if result[0] != CORRECT:
                self.reasons[lane][reason_code(result[1])] += 1

    def lines(self, name: str, output: str) -> list:
        out = ["%s: %s rows checked -> %s" % (name, format(self.rows, ","), output),
               "%-10s %10s %10s %10s" % ("", CORRECT, WRONG, SKIPPED)]
        for lane in self.LANES:
            counts = self.verdicts[lane]
            out.append("%-10s %10s %10s %10s" % (
                lane, format(counts[CORRECT], ","), format(counts[WRONG], ","), format(counts[SKIPPED], ",")))
        for lane in ("relevant", "original"):
            if self.reasons[lane]:
                ranked = sorted(self.reasons[lane].items(), key=lambda item: (-item[1], item[0]))
                out.append("reasons, %s file: %s" % (lane, ", ".join("%s %s" % (c, format(n, ",")) for c, n in ranked)))
        if self.extra_fields:
            out.append("warning: %s row(s) had more fields than the header; the extra fields were not copied"
                       % format(self.extra_fields, ","))
        return out


def overall_verdict(relevant, original) -> str:
    return CORRECT if relevant[0] == CORRECT and original[0] == CORRECT else WRONG


def merge_results(csv_input: CsvInput, relevant_path, original_path, out_path, summary: Summary,
                  collector=None, progress=None) -> None:
    """Write the verified CSV: each input row followed by the five verification columns."""
    width = len(csv_input.header)
    encoding = csv_input.output_encoding
    relevant_results, original_results = read_results(relevant_path), read_results(original_path)
    try:
        with open(str(out_path), "w", newline="", encoding=encoding) as out:
            writer = csv.writer(out)
            writer.writerow(csv_input.header + OUTPUT_COLUMNS)
            for (rownum, fields), relevant, original in zip(csv_input.rows(), relevant_results, original_results):
                if len(fields) > width:
                    summary.extra_fields += 1
                    fields = fields[:width]
                overall = overall_verdict(relevant, original)
                writer.writerow(fields + [relevant[0], relevant[1], original[0], original[1], overall])
                summary.add(relevant, original, overall)
                if collector is not None:
                    collector.merged_row(csv_input, rownum, fields, relevant, original)
                if progress is not None and summary.rows % MERGE_PROGRESS_ROWS == 0:
                    progress("[output] %s rows written" % format(summary.rows, ","))
    finally:
        relevant_results.close()
        original_results.close()


# --------------------------------------------------------------------------- #
# Debug report: a compact, value-masked picture of the inputs and of where checks fail
# --------------------------------------------------------------------------- #
PASTE_MAX_LINES = 50
PASTE_MAX_WIDTH = 120
SKELETON_SAMPLE_DOCS = 200
SKELETON_MAX_KEYS = 20          # a nested map with more distinct keys is shown as {*}
SKELETON_MAX_DEPTH = 8
SKELETON_MAX_LIST_ITEMS = 20
MAX_SHAPES = 5000
MAX_VALUE_SHAPES = 100
MAX_CROSSTAB = 200
TRACES_PER_LANE = 50
TRACES_PER_CODE = 10
PASTE_SHAPES = 8
PASTE_TRACES = 3
PASTE_VALUE_SHAPES = 3
PASTE_VALUE_TYPES = 6
PASTE_CROSSTAB = 6
PASTE_SKELETON_LINES = {"original": 4, "relevant": 3}
LANE_FILES = ("relevant", "original")
_INDEX = re.compile(r"\[\d+\]")
_HINT = re.compile(r"FOUND_AT_LINE_\d+")


def mask(text) -> str:
    """Show the shape of a text, not the text: uppercase -> A, other letters -> a, digits -> 9."""
    out = []
    for ch in str(text):
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        elif ord(ch) < 32:
            out.append("~")
        else:
            out.append(ch)
    return "".join(out)


def clip_end(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 3, 0)] + "..."


def clip_middle(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    head = max((width - 3) // 3, 0)
    tail = max(width - 3 - head, 0)
    return text[:head] + "..." + (text[-tail:] if tail else "")


def count(number: int) -> str:
    return format(number, ",")


_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
_DIGIT_RUN = re.compile(r"\d{3,}")
_PATH_KEY = re.compile(r"\.([^.\[\]]+)|\['([^']*)'\]|\[\"([^\"]*)\"\]")


def safe_key(key) -> str:
    """A key is structure, and is shown as it is only when it looks like an identifier.  A key that is
    really data (an email address, a UUID, an id such as user12345) is masked like a value."""
    key = str(key)
    if _PLAIN_KEY.match(key) and not _DIGIT_RUN.search(key):
        return key
    return clip_end(mask(key), 40)


def mask_path_keys(path: str) -> str:
    """The path with every data-like key masked: $.Item.user12345.a[*] -> $.Item.aaaa99999.a[*]."""
    def replace(match):
        key = next(group for group in match.groups() if group is not None)
        shown = safe_key(key)
        if match.group(1) is not None:
            return "." + shown
        quote = "'" if match.group(2) is not None else '"'
        return "[%s%s%s]" % (quote, shown, quote)
    return _PATH_KEY.sub(replace, path)


def _same_key(key):
    return key


class Skel:
    """The merged structure of many documents at one position."""

    __slots__ = ("tags", "children", "items", "cut")

    def __init__(self):
        self.tags = set()
        self.children = {}
        self.items = None
        self.cut = False


def merge_skeleton(node: Skel, raw, depth: int = 0) -> None:
    tag, value = unwrap(raw)
    node.tags.add(tag)
    if tag not in ("M", "L"):
        return
    if depth >= SKELETON_MAX_DEPTH:
        node.cut = True
        return
    if tag == "M":
        for key, item in value.items():
            child = node.children.get(key)
            if child is None:
                if depth >= 2 and len(node.children) > SKELETON_MAX_KEYS * 2:
                    continue  # already far past the {*} threshold; stop growing
                child = node.children[key] = Skel()
            merge_skeleton(child, item, depth + 1)
    else:
        for item in value[:SKELETON_MAX_LIST_ITEMS]:
            if node.items is None:
                node.items = Skel()
            merge_skeleton(node.items, item, depth + 1)


def render_skeleton(node: Skel, keyf=_same_key) -> str:
    parts = []
    for tag in sorted(node.tags):
        if tag == "M":
            if node.cut:
                parts.append("M{...}")
            elif len(node.children) > SKELETON_MAX_KEYS:
                parts.append("M{*}")
            else:
                parts.append("M{%s}" % ",".join("%s:%s" % (keyf(k), render_skeleton(c, keyf))
                                                for k, c in sorted(node.children.items())))
        elif tag == "L":
            if node.cut:
                parts.append("L[...]")
            else:
                parts.append("L[%s]" % ("" if node.items is None else render_skeleton(node.items, keyf)))
        else:
            parts.append(tag)
    return "|".join(parts)


def skeleton_entries(root: Skel, keyf=_same_key) -> list:
    """One 'Item.attribute:type' entry per top-level attribute (never collapsed: they are the schema).
    keyf maps a key to the text shown for it."""
    prefix, attributes = "", root.children
    item = root.children.get("Item")
    if item is not None and set(root.children) == {"Item"} and "M" in item.tags:
        prefix, attributes = "Item.", item.children
    return ["%s%s:%s" % (prefix, keyf(key), render_skeleton(child, keyf))
            for key, child in sorted(attributes.items())]


def sample_skeleton(pj: ParquetJson, documents: int = SKELETON_SAMPLE_DOCS) -> Skel:
    root = Skel()
    if pj.num_rows:
        batch = next(pj.pf.iter_batches(batch_size=documents, columns=[pj.column]), None)
        for raw in ([] if batch is None else batch.column(0).to_pylist()):
            try:
                merge_skeleton(root, parse_doc(raw))
            except QCError:
                continue
    return root


def pack_entries(prefix: str, entries: list, width, max_lines) -> list:
    """Pack entries side by side into lines of at most `width` characters (None: unlimited)."""
    groups, pieces, size = [], [], len(prefix)
    for entry in entries:
        piece = clip_end(entry, width - len(prefix)) if width else entry
        extra = len(piece) + (2 if pieces else 0)
        if width and pieces and size + extra > width:
            groups.append(pieces)
            pieces, size, extra = [], len(prefix), len(piece)
        pieces.append(piece)
        size += extra
    if pieces:
        groups.append(pieces)
    hidden = 0
    if max_lines and len(groups) > max_lines:
        hidden = sum(len(g) for g in groups[max_lines:])
        groups = groups[:max_lines]
    lines = [prefix + "  ".join(g) for g in groups]
    if hidden and lines:
        suffix = " ...(+%d more)" % hidden
        while width and len(lines[-1]) + len(suffix) > width and "  " in lines[-1][len(prefix):]:
            lines[-1] = lines[-1].rsplit("  ", 1)[0]
            hidden += 1
            suffix = " ...(+%d more)" % hidden
        lines[-1] += suffix
    return lines


class ShapeStats:
    __slots__ = ("rows", "failing", "ok", "reasons")

    def __init__(self):
        self.rows = 0
        self.failing = 0
        self.ok = {"relevant": 0, "original": 0}
        self.reasons = {"relevant": Counter(), "original": Counter()}


class DebugCollector:
    """Gathers what the debug report needs, bounded in size and masked as it is collected."""

    def __init__(self, show_values: bool = False):
        self.show_values = show_values
        self.scope = ""
        self.profile = {}
        self.skeletons = {}       # lane -> skeleton entries with data-like keys masked
        self.skeletons_raw = {}   # the same with real keys; only kept with --show-values
        self.shapes = {}
        self.value_rows = Counter()
        self.value_shapes = defaultdict(Counter)
        self.value_examples = defaultdict(list)
        self.crosstab = Counter()
        self.traces = {lane: [] for lane in LANE_FILES}
        self._trace_codes = {lane: Counter() for lane in LANE_FILES}
        self.ascending = {"SourceLine": True, "RelevancyParquetLine": True}
        self._last_line = {}

    # -- hooks called while the lanes and the merge pass run ------------------
    def lane_outcome(self, label, rownum, line_text, path_text, outcome) -> None:
        if outcome.verdict != WRONG:
            return
        code = outcome.code
        if len(self.traces[label]) >= TRACES_PER_LANE or self._trace_codes[label][code] >= TRACES_PER_CODE:
            return
        self._trace_codes[label][code] += 1
        info = outcome.info or {}
        trace = {"row": rownum, "lane": label, "code": code, "line": line_text.strip(),
                 "hints": _HINT.findall(outcome.reason)}
        if code == "PATH_NOT_FOUND":
            walked, stopped = info.get("walked", "$"), str(info.get("stopped_at", ""))
            trace["walked"] = mask_path_keys(walked)
            trace["stopped_at"] = stopped if stopped.startswith("[") else safe_key(stopped)
            if self.show_values:
                trace["real_path"] = (walked, stopped)
        elif code == "DOCUMENT_MISMATCH":
            difference = info.get("difference", "")
            trace["difference"] = mask_path_keys(difference)
            if self.show_values:
                trace["real_difference"] = difference
        elif code in ("VALUE_MISMATCH", "FORMAT_CHANGED"):
            expected, found = info.get("expected", ""), info.get("found", "")
            trace["expected"], trace["found"] = mask(expected), mask(found)
            trace["lengths"] = (len(expected), len(found))
            if self.show_values:
                trace["real"] = (expected, found)
        elif code == "UNSUPPORTED_PATH":
            trace["path"] = mask(path_text.strip())
        elif code == "INVALID_LINE":
            trace["line"] = mask(line_text.strip())
        self.traces[label].append(trace)

    def merged_row(self, csv_input, rownum, fields, relevant, original) -> None:
        index = csv_input.index
        self._track_order(index, fields)
        shape = _INDEX.sub("[*]", fields[index["SourceElementPath"]].strip())
        if not self.show_values:
            shape = mask_path_keys(shape)  # real keys are kept only for the local report with --show-values
        stats = self.shapes.get(shape)
        if stats is None:
            if len(self.shapes) >= MAX_SHAPES:
                shape = "(other shapes)"
            stats = self.shapes.get(shape)
            if stats is None:
                stats = self.shapes[shape] = ShapeStats()
        stats.rows += 1
        if overall_verdict(relevant, original) != CORRECT:
            stats.failing += 1
        for lane, result in (("relevant", relevant), ("original", original)):
            if result[0] == CORRECT:
                stats.ok[lane] += 1
            else:
                stats.reasons[lane][reason_code(result[1])] += 1
        tag = original[2] if original[2] != "-" else relevant[2]
        if tag != "-":
            self._add_value(index, fields, tag)

    def _track_order(self, index, fields) -> None:
        for column in self.ascending:
            try:
                number = parse_line(fields[index[column]])
            except InvalidLine:
                continue
            last = self._last_line.get(column)
            if last is not None and number < last:
                self.ascending[column] = False
            self._last_line[column] = number

    def _add_value(self, index, fields, tag) -> None:
        value = fields[index["Value"]]
        self.value_rows[tag] += 1
        shapes = self.value_shapes[tag]
        shape = clip_end(mask(value), 40) if value != "" else "(empty)"
        if shape in shapes or len(shapes) < MAX_VALUE_SHAPES:
            shapes[shape] += 1
        examples = self.value_examples[tag]
        if self.show_values and len(examples) < 3 and value not in examples:
            examples.append(value)
        if "DataType" in index:
            cross = (fields[index["DataType"]].strip() or "(blank)", tag)
            if cross in self.crosstab or len(self.crosstab) < MAX_CROSSTAB:
                self.crosstab[cross] += 1

    # -- called once the run is over, while the parquet files are still open --
    def finish(self, relevant: ParquetJson, original: ParquetJson) -> None:
        for lane, pj in (("relevant", relevant), ("original", original)):
            self.profile[lane] = {"rows": pj.num_rows, "groups": pj.num_groups,
                                  "column": pj.column, "type": pj.column_type}
            root = sample_skeleton(pj)
            self.skeletons[lane] = skeleton_entries(root, safe_key)
            if self.show_values:
                self.skeletons_raw[lane] = skeleton_entries(root)

    # -- rendering ------------------------------------------------------------
    def _result_line(self, summary) -> str:
        parts = []
        for lane in Summary.LANES:
            counts = summary.verdicts[lane]
            parts.append("%s %s/%s correct" % (lane, count(counts[CORRECT]), count(summary.rows)))
        return "result: " + " | ".join(parts)

    def _reason_lines(self, summary, width) -> list:
        lines = []
        for lane in LANE_FILES:
            ranked = sorted(summary.reasons[lane].items(), key=lambda item: (-item[1], item[0]))
            text = ", ".join("%s %s" % (code, count(n)) for code, n in ranked) or "none"
            line = "%s: %s" % (lane, text)
            lines.append(clip_end(line, width) if width else line)
        return lines

    def _shape_lines(self, paste: bool, reveal: bool = False) -> list:
        ordered = sorted(self.shapes.items(), key=lambda kv: (-kv[1].failing, -kv[1].rows, kv[0]))
        shown = ordered[:PASTE_SHAPES] if paste else ordered
        lines = ["-- path shapes (%s of %s; worst first; n=rows, rel/orig=rows correct)"
                 % (count(len(shown)), count(len(ordered)))]
        for shape, stats in shown:
            why = []
            for lane in LANE_FILES:
                if stats.reasons[lane]:
                    why.append("%s %s" % ("rel" if lane == "relevant" else "orig",
                                          stats.reasons[lane].most_common(1)[0][0]))
            tail = "n=%s rel=%s orig=%s%s" % (count(stats.rows), count(stats.ok["relevant"]),
                                             count(stats.ok["original"]), " [%s]" % "; ".join(why) if why else "")
            if not reveal:
                shape = mask_path_keys(shape)
            if paste:
                shape = clip_middle(shape, max(PASTE_MAX_WIDTH - len(tail) - 2, 20))
            lines.append("%s  %s" % (shape, tail))
        return lines

    def _value_lines(self, paste: bool, reveal: bool) -> list:
        lines = ["-- value shapes by source type (rows; most common masked shapes)"]
        ordered = sorted(self.value_rows, key=lambda t: (-self.value_rows[t], t))
        for tag in (ordered[:PASTE_VALUE_TYPES] if paste else ordered):
            top = self.value_shapes[tag].most_common(PASTE_VALUE_SHAPES if paste else 10)
            text = "%s rows=%s: %s" % (tag, count(self.value_rows[tag]),
                                       " ".join("%s(%s)" % (shape, count(n)) for shape, n in top))
            if reveal and self.value_examples[tag]:
                text += " | e.g. " + " | ".join(repr(v) for v in self.value_examples[tag])
            lines.append(text)
        if len(lines) == 1:
            lines.append("(no value was found at any checked path)")
        if self.crosstab:
            lines.append("-- DataType / source type (rows)")
            items = self.crosstab.most_common(PASTE_CROSSTAB if paste else None)
            entries = ["%s/%s %s" % (datatype, tag, count(n)) for (datatype, tag), n in items]
            lines += pack_entries("", entries, PASTE_MAX_WIDTH if paste else None, 2 if paste else None)
        return lines

    @staticmethod
    def render_trace(trace, reveal: bool) -> str:
        head = "row %s %s line %s %s" % (count(trace["row"]), trace["lane"], trace["line"] or "(blank)", trace["code"])
        code = trace["code"]
        if code == "PATH_NOT_FOUND":
            walked, stopped = trace["real_path"] if reveal and "real_path" in trace else (trace["walked"],
                                                                                       trace["stopped_at"])
            detail = "%s resolved, stopped at %s" % (walked, stopped)
        elif code == "DOCUMENT_MISMATCH":
            difference = trace["real_difference"] if reveal and "real_difference" in trace else trace["difference"]
            detail = "relevant doc not in original; differs at %s" % difference
        elif code in ("VALUE_MISMATCH", "FORMAT_CHANGED"):
            expected, found = trace["real"] if reveal and "real" in trace else (trace["expected"], trace["found"])
            detail = "expected %r (%d chars) vs found %r (%d chars)" % (
                clip_end(expected, 40), trace["lengths"][0], clip_end(found, 40), trace["lengths"][1])
        elif code == "UNSUPPORTED_PATH":
            detail = "path shape %s" % trace["path"]
        else:
            detail = ""
        hints = " | " + ", ".join(trace["hints"]) if trace["hints"] else ""
        return head + (": " + detail if detail else "") + hints

    def _trace_lines(self, summary, paste: bool, reveal: bool) -> list:
        lines = ["-- failure traces"]
        if paste:
            ranked = sorted(((n, lane, code) for lane in LANE_FILES
                             for code, n in summary.reasons[lane].items() if code != "SKIPPED"),
                            key=lambda item: (-item[0], item[1], item[2]))
            # most common reasons first, a different reason code each time while there are any
            picked, codes = [], set()
            for item in ranked:
                if item[2] not in codes:
                    picked.append(item)
                    codes.add(item[2])
            picked += [item for item in ranked if item not in picked]
            for _n, lane, code in picked[:PASTE_TRACES]:
                trace = next((t for t in self.traces[lane] if t["code"] == code), None)
                if trace is not None:
                    lines.append(self.render_trace(trace, False))
        else:
            for lane in LANE_FILES:
                lines += [self.render_trace(t, reveal) for t in self.traces[lane]]
        if len(lines) == 1:
            lines.append("(no failures)")
        return lines

    def report(self, summary: Summary, csv_name: str, paste: bool, reveal: bool = False) -> list:
        """The paste block (at most 50 lines of 120 characters, always masked) or the full report."""
        reveal = reveal and not paste
        width = PASTE_MAX_WIDTH if paste else None
        lines = ["extraction_qc debug | pyarrow %s | python %s | values %s"
                 % (pa.__version__ if pa is not None else "missing", platform.python_version(),
                    "REAL (do not share)" if reveal else "masked")]
        if not paste:
            lines.append("input: %s" % csv_name)
        lines.append("-- input")
        for lane in LANE_FILES:
            p = self.profile.get(lane)
            if p:
                lines.append("%s: rows=%s row_groups=%s json_column=%r (%s)"
                             % (lane, count(p["rows"]), count(p["groups"]), p["column"], p["type"]))
        lines.append("csv: rows=%s%s | SourceLine order: %s | RelevancyParquetLine order: %s" % (
            count(summary.rows), " (%s)" % self.scope if self.scope else "",
            "ascending" if self.ascending["SourceLine"] else "not ascending",
            "ascending" if self.ascending["RelevancyParquetLine"] else "not ascending"))
        lines.append(self._result_line(summary))
        lines.append("-- json skeleton (first %d documents of each file)" % SKELETON_SAMPLE_DOCS)
        for lane in ("original", "relevant"):
            entries = (self.skeletons_raw if reveal and self.skeletons_raw else self.skeletons).get(lane)
            if entries is None:
                continue
            if not entries:
                lines.append("%s: (no map found)" % lane)
                continue
            lines += pack_entries("%s: " % lane, entries, width, PASTE_SKELETON_LINES[lane] if paste else None)
        lines += self._shape_lines(paste, reveal)
        lines.append("-- reasons (rows per code)")
        lines += self._reason_lines(summary, width)
        lines += self._value_lines(paste, reveal)
        lines += self._trace_lines(summary, paste, reveal)
        if paste:
            lines = [clip_end(line, PASTE_MAX_WIDTH) for line in lines]
            if len(lines) > PASTE_MAX_LINES:
                lines = lines[: PASTE_MAX_LINES - 1] + ["... (truncated)"]
        return lines


def is_trial(args) -> bool:
    """--limit / --rows check only some rows; their output must never replace a full result."""
    return args.limit is not None or getattr(args, "rows_set", None) is not None


def check_output_writable(path: Path) -> None:
    """Fail now, not after the whole run, if another program (Excel, on Windows) holds the output open."""
    if path.exists():
        try:
            with open(str(path), "ab"):
                pass
        except OSError as exc:
            raise InputError("cannot write %s: %s. Is it open in another program? Close it and run again."
                             % (path.name, exc))


def check_temp_dir(args) -> None:
    folder = getattr(args, "temp_dir", None)
    if folder is not None and not Path(folder).is_dir():
        raise InputError("--temp-dir %s is not a folder" % folder)


def verify_triple(triple: Triple, args, progress=None, collector=None) -> Summary:
    """Check one extracted CSV against its two parquet files and write <md5>_verified.csv
    (<md5>_verified_trial.csv for a --limit / --rows run)."""
    header = prepare_triple(triple, encoding=getattr(args, "csv_encoding", None))
    if progress is not None and triple.encoding_note:
        progress("note: " + triple.encoding_note)
    csv_input = CsvInput(triple.extracted, header, limit=args.limit, rows=getattr(args, "rows_set", None),
                         encoding=triple.encoding)
    target = triple.trial if is_trial(args) else triple.verified
    check_output_writable(target)
    check_temp_dir(args)
    relevant = ParquetJson(triple.relevant, args.relevant_column, "--relevant-column")
    try:
        original = ParquetJson(triple.original, args.original_column, "--original-column")
    except Exception:
        relevant.close()
        raise
    summary = Summary()
    summary.output_name = target.name
    partial = target.with_name(target.name + ".partial")
    keep_partial = False
    try:
        with tempfile.TemporaryDirectory(prefix="extraction_qc_", dir=getattr(args, "temp_dir", None)) as tmp:
            relevant_results, original_results = Path(tmp) / "relevant.csv", Path(tmp) / "original.csv"
            run_lane("relevant", csv_input, "RelevancyParquetLine", relevant, relevant_results,
                     collector=collector, progress=progress)
            run_lane("original", csv_input, "SourceLine", original, original_results,
                     skip_from=relevant_results if args.skip_original_on_relevant_failure else None,
                     collector=collector, progress=progress,
                     same_document_with=relevant if getattr(args, "check_same_document", False) else None)
            merge_results(csv_input, relevant_results, original_results, partial, summary,
                          collector=collector, progress=progress)
        if collector is not None:
            collector.scope = "; ".join(
                text for text in ("--limit %d" % args.limit if args.limit else "",
                                  "--rows %s" % args.rows if getattr(args, "rows_set", None) else "") if text)
            collector.finish(relevant, original)
        try:
            os.replace(str(partial), str(target))
        except OSError as exc:
            keep_partial = True  # the checks took the time; do not throw their results away
            raise InputError("the checks are finished but %s could not be replaced (%s). The results are kept in "
                             "%s: close the program that has %s open, then rename that file."
                             % (target.name, exc, partial.name, target.name))
    finally:
        relevant.close()
        original.close()
        if partial.exists() and not keep_partial:
            partial.unlink()
    return summary


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError("must be a whole number of at least 1, got %r" % text)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="QC: verify an extracted-values CSV against the relevant and original parquet files.",
        epilog="Files are found by name in the working folder: <md5>_extracted.csv, "
        "<md5>_original.parquet and <md5>_relevant.parquet.",
    )
    parser.add_argument("--folder", type=Path, default=Path("."),
                        help="Folder holding the input files (default: the current folder)")
    parser.add_argument("--debug", action="store_true",
                        help="Also print a short value-masked diagnostic block and write <md5>_debug_report.txt")
    parser.add_argument("--show-values", action="store_true",
                        help="With --debug: show real values in the local debug report (the printed block stays masked)")
    parser.add_argument("--limit", type=positive_int, metavar="N",
                        help="Verify only the first N data rows of the extracted CSV; the rows go to "
                        "<md5>_verified_trial.csv, never over a full <md5>_verified.csv")
    parser.add_argument("--rows", metavar="LIST",
                        help="Verify only these 1-based data rows of the extracted CSV, e.g. 17,203 (output as for "
                        "--limit). Blank lines in the CSV are not rows and are not counted")
    parser.add_argument("--skip-original-on-relevant-failure", action="store_true",
                        help="Skip the original-file check (result 'Skipped') for rows that fail the relevant-file check")
    parser.add_argument("--check-same-document", action="store_true",
                        help="Also require each relevant document to be contained in the original document at "
                        "SourceLine (every value present at the same path); a miss is DOCUMENT_MISMATCH. "
                        "Only valid if the relevant rows really are subsets of the originals")
    parser.add_argument("--original-column", metavar="NAME",
                        help="Column holding the JSON text in the original parquet (default: auto-detect)")
    parser.add_argument("--relevant-column", metavar="NAME",
                        help="Column holding the JSON text in the relevant parquet (default: auto-detect)")
    parser.add_argument("--csv-encoding", metavar="NAME",
                        help="Text encoding of the extracted CSV (default: UTF-8, else Windows-1252)")
    parser.add_argument("--temp-dir", type=Path, metavar="FOLDER",
                        help="Folder for the working files of a run (default: the system temporary folder)")
    parser.add_argument("--fail-on-wrong", action="store_true",
                        help="Exit with code 3 when all files were checked but some rows are Wrong")
    return parser


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def configure_console() -> None:
    """Never let a character the console cannot show make the run fail: the debug block is meant to be
    copied out, and a traceback at that point would hide it.  Such characters print as \\u escapes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def main(argv=None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.rows_set = parse_row_list(args.rows) if args.rows is not None else None
        check_temp_dir(args)
    except InputError as exc:
        parser.error(str(exc))
    if args.show_values and not args.debug:
        print("warning: --show-values has no effect without --debug", file=sys.stderr)
    triples = find_triples(args.folder)
    if not triples:
        print("No *%s file found in %s" % (EXTRACTED_SUFFIX, args.folder.resolve()), file=sys.stderr)
        return 1
    failed, any_wrong = 0, False
    for triple in triples:
        collector = DebugCollector(show_values=args.show_values) if args.debug else None
        try:
            summary = verify_triple(triple, args, progress=progress, collector=collector)
        except InputError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            failed += 1
            continue
        except Exception as exc:  # a bug or an odd file must not cost the other files their check
            print("ERROR: %s: unexpected failure (%s: %s)" % (triple.extracted.name, type(exc).__name__, exc),
                  file=sys.stderr)
            if args.debug:
                traceback.print_exc()
            failed += 1
            continue
        print("\n".join(summary.lines(triple.extracted.name, summary.output_name)))
        any_wrong = any_wrong or summary.verdicts["overall"][WRONG] > 0
        if collector is not None:
            full = collector.report(summary, triple.extracted.name, paste=False, reveal=args.show_values)
            try:
                triple.debug_report.write_text("\n".join(full) + "\n", encoding="utf-8")
                print("\ndebug report written to %s\n" % triple.debug_report)
            except OSError as exc:
                print("\nwarning: could not write %s: %s\n" % (triple.debug_report.name, exc), file=sys.stderr)
            print("\n".join(collector.report(summary, triple.extracted.name, paste=True)))
    if failed:
        return 1
    return 3 if args.fail_on_wrong and any_wrong else 0


if __name__ == "__main__":
    sys.exit(main())

"""Small parquet and CSV fixtures for the extraction QC tests.

Original file (8 rows, row groups of 3 -> 3 + 3 + 2) and relevant file (4 rows,
row groups of 3 -> 3 + 1).  Relevant lines 1..4 are original lines 1, 3, 4, 8.
"""
import csv
import json
from collections import namedtuple
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

PREFIX = "abc123"
JSON_COLUMN = "payload"

HEADER = [
    "SourceFileName", "SourceFilePath", "RelevancyFileLocation", "SourceLine",
    "RelevancyParquetLine", "EntityRole", "SourceElementPath", "NormalizedPath",
    "CanonicalField", "DataType", "Value",
]


def S(value):
    return {"S": value}


def N(value):
    return {"N": value}


def M(attrs):
    return {"M": attrs}


def L(items):
    return {"L": items}


def doc(**attrs):
    return {"Item": attrs}


DOC_1 = doc(
    plainId=S("P1"),
    createdBy=S("alice"),
    spaced=S(" P1 "),
    startDate=S("2024-01-25"),
    active={"BOOL": True},
    note={"NULL": True},
    options=M({"a": S("1")}),
    scopeIds=L([M({
        "partnerId": S("A1"),
        "enrollSettings": L([
            M({"carrierId": S("C0")}),
            M({"carrierId": S("C1"), "settings": M({"agencyTIN": N("12345.0")})}),
        ]),
    })]),
)
DOC_3_PLAIN = {"Item": {"plainId": "P3", "scopeIds": [{"partnerId": "Z"}]}}  # not DynamoDB typed
DOC_4 = doc(plainId=S("P4"), scopeIds=L([M({"partnerId": S("A4")})]))
DOC_8 = doc(plainId=S("P8"), createdBy=S("carol"))

ORIGINAL_DOCS = [
    DOC_1,                         # line 1
    doc(plainId=S("P2")),          # line 2
    DOC_3_PLAIN,                   # line 3
    DOC_4,                         # line 4 (first row of the second row group)
    doc(plainId=S("P5")),          # line 5
    "{not json",                   # line 6 (invalid JSON)
    doc(plainId=S("P7")),          # line 7
    DOC_8,                         # line 8
]
RELEVANT_DOCS = [DOC_1, DOC_3_PLAIN, DOC_4, DOC_8]

AGENCY_TIN = "$.Item.scopeIds[0].enrollSettings[1].settings.agencyTIN"

# name, SourceLine, RelevancyParquetLine, path, Value, expected relevant, expected original
# Expectations are "Correct", or the reason code a Wrong result must start with.
Case = namedtuple("Case", "name src rel path value rel_expect orig_expect")
CASES = [
    Case("ok_plain_string", "1", "1", "$.Item.plainId", "P1", "Correct", "Correct"),
    Case("ok_number_text", "1", "1", AGENCY_TIN, "12345.0", "Correct", "Correct"),
    Case("ok_bool", "1", "1", "$.Item.active", "true", "Correct", "Correct"),
    Case("ok_null", "1", "1", "$.Item.note", "", "Correct", "Correct"),
    Case("ok_map_other_spacing", "1", "1", "$.Item.options", '{"a":  "1"}', "Correct", "Correct"),
    Case("ok_plain_json_document", "3", "2", "$.Item.scopeIds[0].partnerId", "Z", "Correct", "Correct"),
    Case("ok_row_group_boundary", "4", "3", "$.Item.scopeIds[0].partnerId", "A4", "Correct", "Correct"),
    Case("ok_last_row", "8", "4", "$.Item.createdBy", "carol", "Correct", "Correct"),
    Case("value_mismatch", "1", "1", "$.Item.createdBy", "bob", "VALUE_MISMATCH", "VALUE_MISMATCH"),
    Case("number_reformatted", "1", "1", AGENCY_TIN, "12345", "FORMAT_CHANGED", "FORMAT_CHANGED"),
    Case("whitespace_trimmed", "1", "1", "$.Item.spaced", "P1", "FORMAT_CHANGED", "FORMAT_CHANGED"),
    Case("date_reformatted", "1", "1", "$.Item.startDate", "01/25/2024", "FORMAT_CHANGED", "FORMAT_CHANGED"),
    Case("bool_title_case", "1", "1", "$.Item.active", "True", "FORMAT_CHANGED", "FORMAT_CHANGED"),
    Case("path_not_found", "1", "1", "$.Item.scopeIds[3].partnerId", "A1", "PATH_NOT_FOUND", "PATH_NOT_FOUND"),
    Case("unsupported_path", "1", "1", "Item..x", "P1", "UNSUPPORTED_PATH", "UNSUPPORTED_PATH"),
    Case("line_out_of_range", "99", "1", "$.Item.plainId", "P1", "Correct", "LINE_OUT_OF_RANGE"),
    Case("line_zero", "0", "1", "$.Item.plainId", "P1", "Correct", "LINE_OUT_OF_RANGE"),
    Case("invalid_line", "abc", "1", "$.Item.plainId", "P1", "Correct", "INVALID_LINE"),
    Case("blank_line", "", "1", "$.Item.plainId", "P1", "Correct", "INVALID_LINE"),
    Case("invalid_json_row", "6", "1", "$.Item.plainId", "P1", "Correct", "INVALID_JSON"),
    Case("off_by_one_original", "2", "1", "$.Item.plainId", "P1", "Correct", "VALUE_MISMATCH"),
    Case("relevant_wrong_original_ok", "1", "2", "$.Item.plainId", "P1", "VALUE_MISMATCH", "Correct"),
]


def raw_text(item) -> str:
    return item if isinstance(item, str) else json.dumps(item)


def extracted_row(case: Case, prefix: str = PREFIX) -> list:
    return [
        "%s_original.parquet" % prefix,
        "C:\\vm\\data\\%s_original.parquet" % prefix,   # does not exist on the machine running the tests
        "/vm/data/%s_relevant.parquet" % prefix,
        case.src, case.rel, "Role", case.path, case.path, "Canon_" + case.name, "string", case.value,
    ]


def write_parquet(path: Path, docs: list, row_group_size: int = 3, column: str = JSON_COLUMN) -> None:
    table = pa.table({
        "id": ["r%d" % (i + 1) for i in range(len(docs))],
        column: [raw_text(d) for d in docs],
    })
    pq.write_table(table, str(path), row_group_size=row_group_size)


def write_csv(path: Path, rows: list, header: list = None) -> None:
    with open(str(path), "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER if header is None else header)
        writer.writerows(rows)


def write_fixture(folder: Path, prefix: str = PREFIX, cases: list = None) -> dict:
    """Write <prefix>_original.parquet, _relevant.parquet and _extracted.csv into folder."""
    folder = Path(folder)
    cases = CASES if cases is None else cases
    paths = {
        "original": folder / ("%s_original.parquet" % prefix),
        "relevant": folder / ("%s_relevant.parquet" % prefix),
        "extracted": folder / ("%s_extracted.csv" % prefix),
    }
    write_parquet(paths["original"], ORIGINAL_DOCS)
    write_parquet(paths["relevant"], RELEVANT_DOCS)
    write_csv(paths["extracted"], [extracted_row(c, prefix) for c in cases])
    return paths

"""An extracted parquet file: reading, validation, cells as text, and parity with CSV."""
import contextlib
import csv
import io
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest import mock
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import extraction_qc as qc
from tests import fixtures


def text_rows(count=25):
    """`count` rows of the standard extracted header, as strings."""
    return [["f%d.parquet" % i, "C:\\vm\\x", "/vm/y", str(i), str(i), "Role", "$.Item.a%d" % i, "n", "Canon", "string",
             "v%d" % i] for i in range(1, count + 1)]


class ParquetCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def write_text(self, name="x.parquet", rows=None, row_group_size=10):
        path = self.folder / name
        fixtures.write_extracted_parquet(path, text_rows() if rows is None else rows, row_group_size=row_group_size)
        return path

    def write_table(self, columns, name="t.parquet", row_group_size=None):
        path = self.folder / name
        pq.write_table(pa.table(columns), str(path), row_group_size=row_group_size)
        return path

    def open(self, path, **kwargs):
        return qc.ParquetExtractedInput.open(path, "x.parquet", **kwargs)

    def run_main(self, *extra, folder=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(folder or self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()


def standard_columns(**extra):
    """The required columns, one row, plus extra columns given as name=array."""
    columns = {"SourceLine": ["1"], "RelevancyParquetLine": ["1"], "SourceElementPath": ["$.Item.a"], "Value": ["v"]}
    columns.update(extra)
    return columns


class ReaderTest(ParquetCase):
    def test_rows_in_file_order_across_row_groups(self):
        path = self.write_text()
        self.assertEqual(pq.ParquetFile(str(path)).metadata.num_row_groups, 3)
        source = self.open(path)
        rows = list(source.rows())
        self.assertEqual([n for n, _ in rows], list(range(1, 26)))
        self.assertEqual(rows[0][1], text_rows()[0])
        self.assertEqual(rows[24][1], text_rows()[24])
        self.assertEqual(rows[10][1][6], "$.Item.a11")                       # first row of the second group

    def test_header_index_and_encoding(self):
        source = self.open(self.write_text())
        self.assertEqual(source.header, fixtures.HEADER)
        self.assertEqual(source.index["Value"], fixtures.HEADER.index("Value"))
        self.assertEqual(source.output_encoding, "utf-8-sig")
        self.assertIsInstance(source, qc.ExtractedInput)

    def test_limit(self):
        path = self.write_text()
        for limit in (1, 7, 10, 11, 25, 100):
            numbers = [n for n, _ in self.open(path, limit=limit).rows()]
            self.assertEqual(numbers, list(range(1, min(limit, 25) + 1)), limit)

    def test_rows(self):
        path = self.write_text()
        self.assertEqual([n for n, _ in self.open(path, rows={2, 3}).rows()], [2, 3])
        self.assertEqual([n for n, _ in self.open(path, rows={10, 11, 25, 99}).rows()], [10, 11, 25])
        self.assertEqual([n for n, _ in self.open(path, rows={2, 5}, limit=3).rows()], [2])

    def test_row_groups_without_a_wanted_row_are_not_read(self):
        path = self.write_text()
        calls = []
        real = pq.ParquetFile.iter_batches

        def spy(file, *args, **kwargs):
            calls.append(kwargs.get("row_groups"))
            return real(file, *args, **kwargs)

        pq.ParquetFile.iter_batches = spy
        self.addCleanup(setattr, pq.ParquetFile, "iter_batches", real)
        list(self.open(path, rows={24}).rows())
        self.assertEqual(calls, [[2]])                      # only the third row group
        del calls[:]
        list(self.open(path).rows())
        self.assertEqual(calls, [[0], [1], [2]])            # one reader per row group

    def test_batches_without_a_wanted_row_are_skipped_inside_a_group(self):
        path = self.write_text(row_group_size=25)
        real = qc.ParquetExtractedInput.BATCH_ROWS
        qc.ParquetExtractedInput.BATCH_ROWS = 4
        self.addCleanup(setattr, qc.ParquetExtractedInput, "BATCH_ROWS", real)
        converted = []
        original = qc._text_cells
        source = self.open(path, rows={13})
        source._converters = [lambda values: (converted.append(len(values)), original(values))[1]
                              for _ in source._converters]
        self.assertEqual([n for n, _ in source.rows()], [13])
        self.assertEqual(len(converted) // len(fixtures.HEADER), 1)     # one batch of four rows was converted

    def test_filters_give_the_same_rows_as_the_csv_reader(self):
        rows = text_rows(60)
        parquet = self.write_text(rows=rows, row_group_size=13)
        csv_path = self.folder / "x.csv"
        fixtures.write_csv(csv_path, rows)
        rng = random.Random(11)
        real = qc.ParquetExtractedInput.BATCH_ROWS
        qc.ParquetExtractedInput.BATCH_ROWS = 5
        self.addCleanup(setattr, qc.ParquetExtractedInput, "BATCH_ROWS", real)
        for _ in range(80):
            limit = rng.choice([None, 1, 7, 30, 61])
            wanted = rng.choice([None, {rng.randint(1, 62)}, {rng.randint(1, 62) for _ in range(4)}])
            expected = list(qc.CsvInput(csv_path, fixtures.HEADER, limit=limit, rows=wanted).rows())
            got = list(self.open(parquet, limit=limit, rows=wanted).rows())
            self.assertEqual(got, expected, (limit, wanted))

    def test_an_empty_file(self):
        path = self.write_text(rows=[])
        source = self.open(path)
        self.assertEqual(source.header, fixtures.HEADER)
        self.assertEqual(list(source.rows()), [])

    def test_an_empty_first_row_group(self):
        path = self.folder / "empty_first.parquet"
        table = pa.table({name: pa.array([r[i] for r in text_rows(5)], pa.string())
                          for i, name in enumerate(fixtures.HEADER)})
        writer = pq.ParquetWriter(str(path), table.schema)
        writer.write_table(table.slice(0, 0))
        writer.write_table(table)
        writer.close()
        self.assertEqual([n for n, _ in self.open(path).rows()], [1, 2, 3, 4, 5])
        self.assertEqual([n for n, _ in self.open(path, rows={5}).rows()], [5])

    def test_a_file_that_is_not_parquet(self):
        path = self.folder / "bad.parquet"
        path.write_text("not parquet")
        with self.assertRaises(qc.InputError) as ctx:
            self.open(path)
        self.assertIn("x.parquet: cannot read the parquet file", str(ctx.exception))

    def test_the_file_handle_is_released(self):
        path = self.write_text()
        source = self.open(path)
        list(source.rows())
        path.unlink()                                      # fails on Windows if a handle is still open
        self.assertFalse(path.exists())

    def test_leaving_rows_early_releases_the_file_too(self):
        path = self.write_text()
        generator = self.open(path).rows()
        next(generator)
        generator.close()
        path.unlink()
        self.assertFalse(path.exists())


class ValidationTest(ParquetCase):
    def test_a_missing_required_column(self):
        columns = standard_columns()
        del columns["SourceLine"]
        with self.assertRaises(qc.InputError) as ctx:
            self.open(self.write_table(columns))
        self.assertIn("SourceLine", str(ctx.exception))
        self.assertIn("missing required column", str(ctx.exception))

    def test_a_duplicated_required_column(self):
        table = pa.Table.from_arrays(
            [pa.array(["1"]), pa.array(["1"]), pa.array(["$.Item.a"]), pa.array(["v"]), pa.array(["w"])],
            names=["SourceLine", "RelevancyParquetLine", "SourceElementPath", "Value", "Value"])
        path = self.folder / "dup.parquet"
        pq.write_table(table, str(path))
        with self.assertRaises(qc.InputError) as ctx:
            self.open(path)
        self.assertIn("Value", str(ctx.exception))
        self.assertIn("more than once", str(ctx.exception))

    def test_a_double_value_column_names_the_column_and_the_type(self):
        path = self.write_table(standard_columns(Value=pa.array([1.5], pa.float64())))
        with self.assertRaises(qc.InputError) as ctx:
            self.open(path)
        message = str(ctx.exception)
        self.assertIn("'Value'", message)
        self.assertIn("double", message)
        self.assertIn("not text", message)

    def test_other_non_text_types_for_value_and_path(self):
        for kind, values in ((pa.int64(), [1]), (pa.bool_(), [True]), (pa.timestamp("us"), [datetime(2024, 1, 25)]),
                             (pa.list_(pa.string()), [["a"]]), (pa.decimal128(5, 2), [Decimal("1.50")])):
            for column in ("Value", "SourceElementPath"):
                path = self.write_table(standard_columns(**{column: pa.array(values, kind)}))
                with self.assertRaises(qc.InputError, msg=(column, str(kind))) as ctx:
                    self.open(path)
                message = str(ctx.exception)
                self.assertIn("%r has type %s" % (column, str(kind).split("<")[0]), message)   # list<item..> reads back as list<element..>
                self.assertIn("not text", message)

    def test_text_value_columns_of_every_kind_are_accepted(self):
        variants = [pa.array(["v"], pa.string()), pa.array(["v"], pa.large_string()), pa.array([b"v"], pa.binary()),
                    pa.array(["v"]).dictionary_encode()]
        for column in ("Value", "SourceElementPath"):
            for array in variants:
                columns = standard_columns(**{column: array})
                self.open(self.write_table(columns))               # must not raise

    def test_a_string_view_value_column_is_accepted(self):
        if not hasattr(pa, "string_view"):
            self.skipTest("this pyarrow has no string_view type")
        path = self.folder / "view.parquet"
        try:
            pq.write_table(pa.table(standard_columns(Value=pa.array(["v"], type=pa.string_view()))), str(path))
        except Exception as exc:
            self.skipTest("cannot write a string_view column: %s" % exc)
        self.open(path)

    def test_integer_line_columns_are_read_as_digits(self):
        path = self.write_table(standard_columns(SourceLine=pa.array([5, 12], pa.int64()),
                                                 RelevancyParquetLine=pa.array([7, 8], pa.int32()),
                                                 SourceElementPath=["$.a", "$.b"], Value=["x", "y"]))
        rows = [r for _, r in self.open(path).rows()]
        self.assertEqual([(r[0], r[1]) for r in rows], [("5", "7"), ("12", "8")])

    def test_whole_float_line_numbers_read_as_n_point_zero(self):
        path = self.write_table(standard_columns(SourceLine=pa.array([5.0, 1.0]), RelevancyParquetLine=pa.array([2.0, 3.0]),
                                                 SourceElementPath=["$.a", "$.b"], Value=["x", "y"]))
        rows = [r for _, r in self.open(path).rows()]
        self.assertEqual([(r[0], r[1]) for r in rows], [("5.0", "2.0"), ("1.0", "3.0")])
        self.assertEqual(qc.parse_line(rows[0][0]), 5)             # which parse_line accepts

    def test_text_line_numbers(self):
        path = self.write_table(standard_columns(SourceLine=["abc"], RelevancyParquetLine=[""]))
        (_, row), = self.open(path).rows()
        self.assertEqual((row[0], row[1]), ("abc", ""))

    def test_a_missing_line_number_is_empty_text(self):
        path = self.write_table(standard_columns(SourceLine=pa.array([None], pa.int64())))
        (_, row), = self.open(path).rows()
        self.assertEqual(row[0], "")

    def test_nan_in_a_float_line_column_is_an_invalid_line_not_a_crash(self):
        path = self.write_table(standard_columns(SourceLine=pa.array([float("nan")])))
        (_, row), = self.open(path).rows()
        self.assertEqual(row[0], "nan")
        with self.assertRaises(qc.InvalidLine):
            qc.parse_line(row[0])


class CellTextTest(ParquetCase):
    def cells(self, **columns):
        path = self.write_table(standard_columns(**columns))
        return {name: cell for name, cell in zip(self.open(path).header, [r for _, r in self.open(path).rows()][0])}

    def test_null_is_empty(self):
        cells = self.cells(i=pa.array([None], pa.int64()), s=pa.array([None], pa.string()), n=pa.nulls(1))
        self.assertEqual((cells["i"], cells["s"], cells["n"]), ("", "", ""))

    def test_booleans_are_true_and_false(self):
        cells = self.cells(a=pa.array([True]), b=pa.array([False]))
        self.assertEqual((cells["a"], cells["b"]), ("true", "false"))

    def test_integers_are_digits(self):
        cells = self.cells(a=pa.array([7], pa.int8()), b=pa.array([-12], pa.int64()), c=pa.array([2 ** 63 - 1], pa.int64()),
                           d=pa.array([2 ** 64 - 1], pa.uint64()))
        self.assertEqual((cells["a"], cells["b"], cells["c"], cells["d"]), ("7", "-12", "9223372036854775807",
                                                                          "18446744073709551615"))

    def test_floats_use_their_shortest_exact_form(self):
        values = [12345.5, 5.0, 0.1, 1e22, -0.0, 1 / 3, 123456789.123456789, float("inf"), float("-inf"), float("nan")]
        columns = {"f%d" % i: pa.array([v]) for i, v in enumerate(values)}
        cells = self.cells(**columns)
        self.assertEqual([cells["f%d" % i] for i in range(len(values))],
                         ["12345.5", "5.0", "0.1", "1e+22", "-0.0", "0.3333333333333333", "123456789.12345679",
                          "inf", "-inf", "nan"])
        for i, value in enumerate(values[:7]):
            self.assertEqual(float(cells["f%d" % i]), value)         # exact: it reads back to the same number

    def test_float32_is_shown_as_the_double_it_converts_to(self):
        cells = self.cells(f=pa.array([0.5], pa.float32()))
        self.assertEqual(cells["f"], "0.5")

    def test_decimals_are_plain_digits(self):
        cells = self.cells(a=pa.array([Decimal("1.50")], pa.decimal128(10, 2)), b=pa.array([Decimal("100")], pa.decimal128(10, 0)),
                           c=pa.array([Decimal("0.000012")], pa.decimal128(10, 6)), d=pa.array([Decimal("-3.1")], pa.decimal128(5, 1)))
        self.assertEqual((cells["a"], cells["b"], cells["c"], cells["d"]), ("1.50", "100", "0.000012", "-3.1"))

    def test_dates_times_and_timestamps_are_iso_8601(self):
        cells = self.cells(
            d=pa.array([date(2024, 1, 25)], pa.date32()),
            t=pa.array([time(10, 30, 15)], pa.time32("s")),
            ts=pa.array([datetime(2024, 1, 25, 10, 30)], pa.timestamp("us")),
            tsf=pa.array([datetime(2024, 1, 25, 10, 30, 15, 250000)], pa.timestamp("us")),
            tz=pa.array([datetime(2024, 1, 25, 10, 30, tzinfo=timezone.utc)], pa.timestamp("us", tz="UTC")),
            ms=pa.array([datetime(2024, 1, 25, 10, 30, 15, 123000)], pa.timestamp("ms")),
        )
        self.assertEqual(cells["d"], "2024-01-25")
        self.assertEqual(cells["t"], "10:30:15")
        self.assertEqual(cells["ts"], "2024-01-25T10:30:00")
        self.assertEqual(cells["tsf"], "2024-01-25T10:30:15.250000")
        self.assertEqual(cells["tz"], "2024-01-25T10:30:00+00:00")
        self.assertEqual(cells["ms"], "2024-01-25T10:30:15.123000")

    def test_durations(self):
        cells = self.cells(d=pa.array([timedelta(hours=1, minutes=5)], pa.duration("us")))
        self.assertEqual(cells["d"], "1:05:00")

    def without_pandas(self, **columns):
        """The cells of a file read while pandas cannot be imported: a machine that has only pyarrow."""
        path = self.write_table(standard_columns(**columns))        # written first: building tables looks for pandas
        with mock.patch.dict(sys.modules, {"pandas": None}):
            source = self.open(path)
            row = [r for _, r in source.rows()][0]
        return dict(zip(source.header, row))

    def test_nanoseconds_need_no_pandas(self):
        cells = self.without_pandas(
            ts=pa.array([1500], pa.timestamp("ns")), whole=pa.array([10 ** 9], pa.timestamp("ns")),
            t=pa.array([37815000000123], pa.time64("ns")), d=pa.array([86400 * 10 ** 9 + 1], pa.duration("ns")),
            tz=pa.array([1706178600 * 10 ** 9 + 7], pa.timestamp("ns", tz="UTC")))
        self.assertEqual(cells["ts"], "1970-01-01T00:00:00.000001500")
        self.assertEqual(cells["whole"], "1970-01-01T00:00:01")
        self.assertEqual(cells["t"], "10:30:15.000000123")
        self.assertEqual(cells["d"], "1 day, 0:00:00.000000001")
        self.assertEqual(cells["tz"], "2024-01-25T10:30:00.000000007+00:00")

    def test_a_time_zone_needs_no_time_zone_database(self):
        # an unknown zone name cannot be looked up anywhere, as on a machine without tzdata; the stored instant is UTC
        for zone in ("UTC", "America/New_York", "+05:30", "Mars/Olympus_Mons"):
            cells = self.cells(ts=pa.array([1706178600], pa.timestamp("s", tz=zone)))
            self.assertEqual(cells["ts"], "2024-01-25T10:30:00+00:00", zone)

    def test_maps_are_json_objects(self):
        cells = self.cells(m=pa.array([[("k", 1), ("j", 2)]], pa.map_(pa.string(), pa.int64())))
        self.assertEqual(cells["m"], '{"k":1,"j":2}')

    def test_a_failing_conversion_names_the_column_and_its_type(self):
        path = self.write_table(standard_columns(When=pa.array([1], pa.timestamp("us")), Other=["x"]))
        real = qc._converted_cells

        def explode(array):
            raise RuntimeError("boom")

        qc._converted_cells = explode
        self.addCleanup(setattr, qc, "_converted_cells", real)
        source = self.open(path)
        with self.assertRaises(qc.InputError) as ctx:
            list(source.rows())
        self.assertIn("x.parquet: column 'When' (type timestamp[us]) could not be converted to text: boom",
                      str(ctx.exception))

    def test_binary_is_utf8_text_or_hexadecimal(self):
        cells = self.cells(a=pa.array([b"abc"], pa.binary()), b=pa.array([b"\xff\xfe"], pa.binary()),
                           c=pa.array(["café".encode("utf-8")], pa.binary()), d=pa.array([b""], pa.binary()))
        self.assertEqual((cells["a"], cells["b"], cells["c"], cells["d"]), ("abc", "fffe", "café", ""))

    def test_lists_are_compact_json(self):
        cells = self.cells(a=pa.array([[1, 2]]), b=pa.array([[]], pa.list_(pa.int64())),
                           c=pa.array([["x", "y"]]), d=pa.array([[1.5, None]]))
        self.assertEqual((cells["a"], cells["b"], cells["c"], cells["d"]), ("[1,2]", "[]", '["x","y"]', "[1.5,null]"))

    def test_structs_are_compact_json(self):
        cells = self.cells(s=pa.array([{"a": 1, "b": "x"}]), e=pa.array([{"a": None}], pa.struct([("a", pa.int64())])))
        self.assertEqual((cells["s"], cells["e"]), ('{"a":1,"b":"x"}', '{"a":null}'))

    def test_nested_values_follow_the_same_rules(self):
        nested = pa.array([[{"when": datetime(2024, 1, 25, 10, 30), "n": Decimal("1.50"), "ok": True, "raw": b"hi"}]],
                          pa.list_(pa.struct([("when", pa.timestamp("us")), ("n", pa.decimal128(5, 2)),
                                              ("ok", pa.bool_()), ("raw", pa.binary())])))
        cells = self.cells(x=nested)
        self.assertEqual(cells["x"], '[{"when":"2024-01-25T10:30:00","n":"1.50","ok":true,"raw":"hi"}]')

    def test_a_struct_holding_a_list_of_structs(self):
        value = {"items": [{"id": 1}, {"id": 2}], "name": "café"}
        cells = self.cells(x=pa.array([value]))
        self.assertEqual(cells["x"], '{"items":[{"id":1},{"id":2}],"name":"café"}')


    def test_text_passes_through_unchanged(self):
        awkward = " P1 \n,\"quote\" café 中文 \t"
        cells = self.cells(a=pa.array([awkward]), b=pa.array(["007"]), c=pa.array([""]))
        self.assertEqual((cells["a"], cells["b"], cells["c"]), (awkward, "007", ""))

    def test_dictionary_encoded_text_and_binary_values(self):
        cells = self.cells(a=pa.array(["x", "x"]).dictionary_encode().slice(0, 1),
                           Value=pa.array([b"caf\xc3\xa9"], pa.binary()))
        self.assertEqual((cells["a"], cells["Value"]), ("x", "café"))

    def test_string_columns_are_not_converted_cell_by_cell(self):
        self.assertIs(qc.column_converter(pa.string()), qc._text_cells)
        self.assertIs(qc.column_converter(pa.large_string()), qc._text_cells)
        self.assertIs(qc.column_converter(pa.dictionary(pa.int32(), pa.string())), qc._text_cells)
        self.assertIs(qc.column_converter(pa.int64()), qc._all_cells)
        self.assertIs(qc.column_converter(pa.binary()), qc._all_cells)

    def test_cell_text_directly(self):
        self.assertEqual(qc.cell_text(None), "")
        self.assertEqual(qc.cell_text(True), "true")
        self.assertEqual(qc.cell_text(1), "1")
        self.assertEqual(qc.cell_text(1.0), "1.0")
        self.assertEqual(qc.cell_text(Decimal("1E+2")), "100")
        self.assertEqual(qc.cell_text(bytearray(b"ab")), "ab")
        self.assertEqual(qc.cell_text({"a": [1, date(2024, 1, 25)]}), '{"a":[1,"2024-01-25"]}')


def converted(array):
    """What a column of this array becomes in the verified CSV."""
    return qc.column_converter(array.type)(array)


class ArrowConversionTest(unittest.TestCase):
    """The text of temporal values and maps, from the arrays themselves."""

    def test_timestamps_in_every_unit(self):
        self.assertEqual(converted(pa.array([1706178615], pa.timestamp("s"))), ["2024-01-25T10:30:15"])
        self.assertEqual(converted(pa.array([1706178615123], pa.timestamp("ms"))), ["2024-01-25T10:30:15.123000"])
        self.assertEqual(converted(pa.array([1706178615123456], pa.timestamp("us"))), ["2024-01-25T10:30:15.123456"])
        self.assertEqual(converted(pa.array([1706178615123456789], pa.timestamp("ns"))),
                         ["2024-01-25T10:30:15.123456789"])
        self.assertEqual(converted(pa.array([1706178615123456000], pa.timestamp("ns"))), ["2024-01-25T10:30:15.123456"])

    def test_before_1970_and_at_the_ends_of_the_calendar(self):
        self.assertEqual(converted(pa.array([-1, -1000000000], pa.timestamp("ns"))),
                         ["1969-12-31T23:59:59.999999999", "1969-12-31T23:59:59"])
        self.assertEqual(converted(pa.array([-62135596800, 253402300799], pa.timestamp("s"))),
                         ["0001-01-01T00:00:00", "9999-12-31T23:59:59"])
        self.assertEqual(converted(pa.array([-62167219200 - 86400], pa.timestamp("s"))), ["-0001-12-31T00:00:00"])

    def test_dates(self):
        self.assertEqual(converted(pa.array([date(2024, 1, 25), date(1969, 12, 31), date(2024, 2, 29), None], pa.date32())),
                         ["2024-01-25", "1969-12-31", "2024-02-29", ""])
        self.assertEqual(converted(pa.array([date(2024, 1, 25)], pa.date64())), ["2024-01-25"])

    def test_times(self):
        self.assertEqual(converted(pa.array([37815], pa.time32("s"))), ["10:30:15"])
        self.assertEqual(converted(pa.array([37815250], pa.time32("ms"))), ["10:30:15.250000"])
        self.assertEqual(converted(pa.array([37815250000], pa.time64("us"))), ["10:30:15.250000"])
        self.assertEqual(converted(pa.array([37815000000123], pa.time64("ns"))), ["10:30:15.000000123"])

    def test_durations(self):
        self.assertEqual(converted(pa.array([3900, 86401, -1, 0, 172800], pa.duration("s"))),
                         ["1:05:00", "1 day, 0:00:01", "-0:00:01", "0:00:00", "2 days, 0:00:00"])
        self.assertEqual(converted(pa.array([1500000], pa.duration("us"))), ["0:00:01.500000"])
        self.assertEqual(converted(pa.array([1, -1500000001], pa.duration("ns"))),
                         ["0:00:00.000000001", "-0:00:01.500000001"])

    def test_nulls_are_empty(self):
        for kind in (pa.timestamp("ns"), pa.timestamp("s", tz="UTC"), pa.date32(), pa.time64("us"), pa.duration("ms")):
            self.assertEqual(converted(pa.array([None, None], kind)), ["", ""], kind)

    def test_the_text_is_what_python_writes_for_the_same_values(self):
        rng = random.Random(3)
        epoch, tick = datetime(1970, 1, 1), timedelta(microseconds=1)
        for _ in range(3000):
            moment = datetime(1, 1, 1) + timedelta(microseconds=rng.randrange(0, 253402300799 * 10 ** 6))
            micros = (moment - epoch) // tick
            self.assertEqual(converted(pa.array([micros], pa.timestamp("us")))[0], moment.isoformat())
            self.assertEqual(converted(pa.array([(moment.date() - date(1970, 1, 1)).days], pa.date32()))[0],
                             moment.date().isoformat())
            span = timedelta(microseconds=rng.randrange(0, 10 ** 13))
            self.assertEqual(converted(pa.array([span // tick], pa.duration("us")))[0], str(span))
            clock = time(rng.randrange(24), rng.randrange(60), rng.randrange(60), rng.choice([0, rng.randrange(10 ** 6)]))
            ticks = ((clock.hour * 60 + clock.minute) * 60 + clock.second) * 10 ** 6 + clock.microsecond
            self.assertEqual(converted(pa.array([ticks], pa.time64("us")))[0], clock.isoformat())

    def test_a_time_zone_is_the_utc_instant_whatever_it_is_called(self):
        for zone in ("UTC", "Europe/Paris", "-08:00", "Nowhere/At_All"):
            self.assertEqual(converted(pa.array([1706178600], pa.timestamp("s", tz=zone))),
                             ["2024-01-25T10:30:00+00:00"], zone)

    def test_maps_are_objects_with_text_keys(self):
        string_keys = pa.map_(pa.string(), pa.int64())
        self.assertEqual(converted(pa.array([[("k", 1), ("j", 2)], None, []], string_keys)), ['{"k":1,"j":2}', "", "{}"])
        self.assertEqual(converted(pa.array([[(1, "x"), (2, None)]], pa.map_(pa.int64(), pa.string()))),
                         ['{"1":"x","2":null}'])
        self.assertEqual(converted(pa.array([[(True, 1)]], pa.map_(pa.bool_(), pa.int64()))), ['{"true":1}'])

    def test_a_map_with_a_repeated_key_is_a_list_of_pairs(self):
        self.assertEqual(converted(pa.array([[("a", 1), ("a", 2)]], pa.map_(pa.string(), pa.int64()))),
                         ['[["a",1],["a",2]]'])

    def test_temporal_values_and_maps_inside_other_types(self):
        zone = pa.timestamp("s", tz="Nowhere/At_All")
        inner = pa.struct([("when", zone), ("tags", pa.map_(pa.string(), pa.int64())), ("on", pa.date32())])
        array = pa.array([[{"when": 1706178600, "tags": [("a", 1)], "on": 19747}, None], None, []], pa.list_(inner))
        self.assertEqual(converted(array), ['[{"when":"2024-01-25T10:30:00+00:00","tags":{"a":1},"on":"2024-01-25"},null]',
                                            "", "[]"])
        fixed = pa.array([[1, 2], None], pa.list_(pa.timestamp("s"), 2))
        self.assertEqual(converted(fixed), ['["1970-01-01T00:00:01","1970-01-01T00:00:02"]', ""])
        structs = pa.array([{"w": 1}, None, {"w": None}], pa.struct([("w", pa.timestamp("s"))]))
        self.assertEqual(converted(structs), ['{"w":"1970-01-01T00:00:01"}', "", '{"w":null}'])
        self.assertEqual(converted(pa.array([{}, None], pa.struct([]))), ["{}", ""])

    def test_dictionary_encoded_and_sliced_arrays(self):
        self.assertEqual(converted(pa.array([1, 2, 1], pa.timestamp("s")).dictionary_encode()),
                         ["1970-01-01T00:00:01", "1970-01-01T00:00:02", "1970-01-01T00:00:01"])
        sliced = pa.array([1, 2, 3, 4], pa.timestamp("s")).slice(1, 2)
        self.assertEqual(converted(sliced), ["1970-01-01T00:00:02", "1970-01-01T00:00:03"])
        nested = pa.array([{"w": 1}, None, {"w": 3}, {"w": 4}], pa.struct([("w", pa.timestamp("s"))])).slice(1, 3)
        self.assertEqual(converted(nested), ["", '{"w":"1970-01-01T00:00:03"}', '{"w":"1970-01-01T00:00:04"}'])

    def test_which_columns_need_the_conversion(self):
        for kind in (pa.timestamp("us"), pa.timestamp("ns", tz="UTC"), pa.date32(), pa.date64(), pa.time32("s"),
                     pa.time64("ns"), pa.duration("us"), pa.map_(pa.string(), pa.int64()),
                     pa.list_(pa.timestamp("s")), pa.struct([("a", pa.date32())]),
                     pa.list_(pa.struct([("m", pa.map_(pa.string(), pa.string()))])),
                     pa.dictionary(pa.int32(), pa.timestamp("s"))):
            self.assertIs(qc.column_converter(kind), qc._converted_cells, kind)
        for kind in (pa.int64(), pa.float64(), pa.bool_(), pa.binary(), pa.decimal128(5, 2), pa.list_(pa.int64()),
                     pa.struct([("a", pa.string())]), pa.null()):
            self.assertIs(qc.column_converter(kind), qc._all_cells, kind)
        self.assertIs(qc.column_converter(pa.string()), qc._text_cells)


class EndToEndParquetTest(ParquetCase):
    def write_case(self, extracted_format, folder, cases=None):
        return fixtures.write_folder_fixture(folder, cases=cases, extracted_format=extracted_format)

    def verified_rows(self, folder):
        with open(str(folder / "verified" / "abc123_verified.csv"), newline="", encoding="utf-8-sig") as handle:
            return list(csv.reader(handle))

    def test_the_same_rows_as_csv_and_as_parquet_give_identical_verified_rows(self):
        results = {}
        for extracted_format in ("csv", "parquet"):
            folder = self.folder / extracted_format
            folder.mkdir()
            self.write_case(extracted_format, folder)
            code, out, err = self.run_main(folder=folder)
            self.assertEqual(code, 0, err)
            results[extracted_format] = self.verified_rows(folder)
        self.assertEqual(results["csv"], results["parquet"])
        self.assertEqual(len(results["csv"]) - 1, len(fixtures.CASES))
        failing = [r for r in results["parquet"][1:] if r[-1] == "Wrong"]
        self.assertGreater(len(failing), 10)                      # the failing cases are part of the comparison

    def test_parity_with_skip_on_failure_and_the_same_document_check(self):
        for switches in (["--skip-original-on-relevant-failure"], ["--check-same-document"],
                         ["--limit", "9"], ["--rows", "2,15,22"]):
            results = []
            for extracted_format in ("csv", "parquet"):
                folder = self.folder / ("_".join(switches).replace("-", "") + extracted_format)
                folder.mkdir()
                self.write_case(extracted_format, folder)
                code, _, err = self.run_main(*switches, folder=folder)
                self.assertEqual(code, 0, err)
                kind = "verified_trial" if switches[0] in ("--limit", "--rows") else "verified"
                with open(str(folder / "verified" / ("abc123_%s.csv" % kind)), newline="", encoding="utf-8-sig") as h:
                    results.append(list(csv.reader(h)))
            self.assertEqual(results[0], results[1], switches)

    def test_typed_line_columns_end_to_end(self):
        cases = [c for c in fixtures.CASES if c.src.isdigit() and c.rel.isdigit()]
        folder_text, folder_typed = self.folder / "text", self.folder / "typed"
        for folder in (folder_text, folder_typed):
            folder.mkdir()
        self.write_case("parquet", folder_text, cases)
        paths = self.write_case("csv", folder_typed, cases)
        (folder_typed / "extracted" / "abc123.csv").unlink()
        rows = [fixtures.extracted_row(c) for c in cases]
        names = fixtures.HEADER
        columns = {}
        for i, name in enumerate(names):
            values = [r[i] for r in rows]
            if name in ("SourceLine", "RelevancyParquetLine"):
                columns[name] = pa.array([int(v) for v in values], pa.int64())
            else:
                columns[name] = pa.array(values, pa.string())
        pq.write_table(pa.table(columns), str(folder_typed / "extracted" / "abc123.parquet"))
        for folder in (folder_text, folder_typed):
            code, _, err = self.run_main(folder=folder)
            self.assertEqual(code, 0, err)
        self.assertEqual(self.verified_rows(folder_text)[0], self.verified_rows(folder_typed)[0])
        verdicts = lambda folder: [(r[-5], r[-4], r[-3], r[-2], r[-1]) for r in self.verified_rows(folder)[1:]]
        self.assertEqual(verdicts(folder_text), verdicts(folder_typed))
        # typed lines are written as digits, exactly like the text column
        self.assertEqual([r[3] for r in self.verified_rows(folder_typed)[1:]], [c.src for c in cases])

    def test_float_line_numbers_end_to_end(self):
        cases = [fixtures.CASES[0], fixtures.CASES[6], fixtures.CASES[7]]
        folder = self.folder / "floats"
        folder.mkdir()
        self.write_case("csv", folder, cases)
        (folder / "extracted" / "abc123.csv").unlink()
        rows = [fixtures.extracted_row(c) for c in cases]
        columns = {name: pa.array([r[i] for r in rows], pa.string()) for i, name in enumerate(fixtures.HEADER)}
        columns["SourceLine"] = pa.array([float(c.src) for c in cases])
        columns["RelevancyParquetLine"] = pa.array([float(c.rel) for c in cases])
        pq.write_table(pa.table(columns), str(folder / "extracted" / "abc123.parquet"))
        code, _, err = self.run_main(folder=folder)
        self.assertEqual(code, 0, err)
        rows = self.verified_rows(folder)
        self.assertEqual([r[-1] for r in rows[1:]], ["Correct", "Correct", "Correct"])
        self.assertEqual([r[3] for r in rows[1:]], ["1.0", "4.0", "8.0"])

    def test_a_double_value_column_stops_the_md5_before_any_row(self):
        folder = self.folder / "bad"
        folder.mkdir()
        self.write_case("csv", folder)
        (folder / "extracted" / "abc123.csv").unlink()
        rows = fixtures.extracted_row(fixtures.CASES[0])
        columns = {name: pa.array([rows[i]], pa.string()) for i, name in enumerate(fixtures.HEADER)}
        columns["Value"] = pa.array([1.5])
        pq.write_table(pa.table(columns), str(folder / "extracted" / "abc123.parquet"))
        code, _, err = self.run_main(folder=folder)
        self.assertEqual(code, 1)
        self.assertIn("extracted/abc123.parquet", err)
        self.assertIn("'Value' has type double", err)
        self.assertFalse((folder / "verified").exists())

    def test_non_ascii_text_is_written_with_a_byte_order_mark(self):
        folder = self.folder / "cafe"
        folder.mkdir()
        cases = [fixtures.Case("c", "1", "1", "$.Item.createdBy", "café", "", "")]
        self.write_case("parquet", folder, cases)
        code, _, err = self.run_main(folder=folder)
        self.assertEqual(code, 0, err)
        data = (folder / "verified" / "abc123_verified.csv").read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("café".encode("utf-8"), data)

    def test_a_csv_extracted_file_in_plain_utf8_has_no_byte_order_mark(self):
        folder = self.folder / "plain"
        folder.mkdir()
        self.write_case("csv", folder, [fixtures.CASES[0]])
        self.run_main(folder=folder)
        self.assertFalse((folder / "verified" / "abc123_verified.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_csv_encoding_is_ignored_for_a_parquet_extracted_file(self):
        folder = self.folder / "enc"
        folder.mkdir()
        self.write_case("parquet", folder)
        for switch in ("cp1252", "utf-8", "nonsense"):
            code, _, err = self.run_main("--csv-encoding", switch, folder=folder)
            self.assertEqual(code, 0, (switch, err))
        self.assertEqual(len(self.verified_rows(folder)) - 1, len(fixtures.CASES))

    def test_the_empty_extracted_parquet_gives_a_header_only_result(self):
        folder = self.folder / "none"
        folder.mkdir()
        self.write_case("csv", folder)
        (folder / "extracted" / "abc123.csv").unlink()
        fixtures.write_extracted_parquet(folder / "extracted" / "abc123.parquet", [])
        code, out, err = self.run_main(folder=folder)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.verified_rows(folder)), 1)
        self.assertIn("0 rows checked", out)

    def test_the_other_columns_are_carried_over(self):
        folder = self.folder / "extra"
        folder.mkdir()
        self.write_case("csv", folder)
        (folder / "extracted" / "abc123.csv").unlink()
        rows = [fixtures.extracted_row(fixtures.CASES[0]), fixtures.extracted_row(fixtures.CASES[1])]
        columns = {name: pa.array([r[i] for r in rows], pa.string()) for i, name in enumerate(fixtures.HEADER)}
        columns["Score"] = pa.array([1.5, None])
        columns["Active"] = pa.array([True, None])
        columns["When"] = pa.array([datetime(2024, 1, 25, 10, 30), None], pa.timestamp("us"))
        pq.write_table(pa.table(columns), str(folder / "extracted" / "abc123.parquet"))
        code, _, err = self.run_main(folder=folder)
        self.assertEqual(code, 0, err)
        rows = self.verified_rows(folder)
        self.assertEqual(rows[0][:len(fixtures.HEADER) + 3], fixtures.HEADER + ["Score", "Active", "When"])
        n = len(fixtures.HEADER)
        self.assertEqual(rows[1][n:n + 3], ["1.5", "true", "2024-01-25T10:30:00"])
        self.assertEqual(rows[2][n:n + 3], ["", "", ""])
        self.assertEqual(rows[0][n + 3:], qc.OUTPUT_COLUMNS)

    def extra_columns_file(self, folder, **extra):
        self.write_case("csv", folder)
        (folder / "extracted" / "abc123.csv").unlink()
        rows = [fixtures.extracted_row(c) for c in fixtures.CASES[:3]]
        columns = {name: pa.array([r[i] for r in rows], pa.string()) for i, name in enumerate(fixtures.HEADER)}
        columns.update(extra)
        pq.write_table(pa.table(columns), str(folder / "extracted" / "abc123.parquet"))

    def test_extra_columns_that_need_pandas_or_tzdata_do_not_stop_the_md5(self):
        # the reported failure: a machine with only pyarrow stopped at a time zone or a nanosecond value
        folder = self.folder / "exotic"
        folder.mkdir()
        self.extra_columns_file(
            folder,
            Zoned=pa.array([1706178600, None, 0], pa.timestamp("s", tz="Nowhere/At_All")),
            Nanos=pa.array([1706178600123456789, None, 1], pa.timestamp("ns")),
            Clock=pa.array([37815000000123, None, 0], pa.time64("ns")),
            Span=pa.array([1, None, -1], pa.duration("ns")),
            Tags=pa.array([[("a", 1)], None, []], pa.map_(pa.string(), pa.int64())))
        with mock.patch.dict(sys.modules, {"pandas": None}):
            code, _, err = self.run_main(folder=folder)
        self.assertEqual(code, 0, err)
        rows = self.verified_rows(folder)
        n = len(fixtures.HEADER)
        self.assertEqual(rows[0][n:n + 5], ["Zoned", "Nanos", "Clock", "Span", "Tags"])
        self.assertEqual(rows[1][n:n + 5], ["2024-01-25T10:30:00+00:00", "2024-01-25T10:30:00.123456789",
                                            "10:30:15.000000123", "0:00:00.000000001", '{"a":1}'])
        self.assertEqual(rows[2][n:n + 5], [""] * 5)
        self.assertEqual(rows[3][n:n + 5], ["1970-01-01T00:00:00+00:00", "1970-01-01T00:00:00.000000001",
                                            "00:00:00", "-0:00:00.000000001", "{}"])

    def test_a_column_that_cannot_be_converted_stops_the_md5_and_is_named(self):
        folder = self.folder / "broken"
        folder.mkdir()
        self.extra_columns_file(folder, When=pa.array([1, 2, 3], pa.timestamp("us")))
        real = qc._converted_cells
        qc._converted_cells = lambda array: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(setattr, qc, "_converted_cells", real)
        code, out, err = self.run_main(folder=folder)
        self.assertEqual(code, 1)
        self.assertIn("ERROR: extracted/abc123.parquet: column 'When' (type timestamp[us]) could not be converted to "
                      "text: boom", err)
        self.assertNotIn("unexpected failure", err)
        self.assertEqual(out.strip().splitlines()[-1], "total: 0 checked, 0 skipped, 1 failed")
        self.assertFalse((folder / "verified" / "abc123_verified.csv").exists())


class BoundedMemoryTest(ParquetCase):
    def test_reading_a_multi_row_group_extracted_parquet_does_not_grow_with_the_file(self):
        rng = random.Random(5)
        path = self.folder / "big.parquet"
        header = fixtures.HEADER
        writer = None
        for _ in range(25):                                   # 25 row groups of 1,000 rows
            rows = [["f.parquet", "C:\\vm\\x", "/vm/y", str(i), str(i), "Role", "$.Item.a", "n", "Canon", "string",
                     "".join(rng.choice("0123456789abcdef") for _ in range(900))] for i in range(1000)]
            table = pa.table({name: pa.array([r[i] for r in rows], pa.string()) for i, name in enumerate(header)})
            if writer is None:
                writer = pq.ParquetWriter(str(path), table.schema)
            writer.write_table(table)
        writer.close()
        size = path.stat().st_size
        self.assertGreater(size, 20 * 2 ** 20)
        source = self.open(path)
        baseline = pa.total_allocated_bytes()
        total = 0
        for _ in source.rows():
            total += 1
        grown = pa.total_allocated_bytes() - baseline
        self.assertEqual(total, 25000)
        self.assertLess(grown, size * 0.3, "allocated %d bytes while reading a %d byte file" % (grown, size))


if __name__ == "__main__":
    unittest.main()

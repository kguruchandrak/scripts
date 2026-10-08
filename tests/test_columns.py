import json
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import extraction_qc as qc

DOC = json.dumps({"Item": {"plainId": {"S": "P1"}}})


class ColumnDetectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def write(self, name, columns):
        path = self.folder / name
        pq.write_table(pa.table(columns), str(path))
        return path

    def open(self, path, **kwargs):
        pj = qc.ParquetJson(path, **kwargs)
        self.addCleanup(pj.close)
        return pj

    def test_single_text_column_is_used(self):
        path = self.write("a.parquet", {"payload": [DOC, DOC]})
        pj = self.open(path)
        self.assertEqual(pj.column, "payload")
        self.assertEqual(pj.column_type, "string")
        self.assertEqual(pj.num_rows, 2)

    def test_several_text_columns_one_holds_json(self):
        path = self.write("a.parquet", {"id": ["x1", "x2"], "payload": [DOC, DOC], "n": [1, 2]})
        self.assertEqual(self.open(path).column, "payload")

    def test_numeric_looking_text_column_is_not_a_json_object(self):
        path = self.write("a.parquet", {"id": ["1", "2"], "body": [DOC, DOC]})
        self.assertEqual(self.open(path).column, "body")

    def test_first_json_column_wins(self):
        path = self.write("a.parquet", {"first": [DOC], "second": [DOC]})
        self.assertEqual(self.open(path).column, "first")

    def test_explicit_column_skips_detection(self):
        path = self.write("a.parquet", {"payload": [DOC], "body": [DOC]})
        self.assertEqual(self.open(path, column="body").column, "body")

    def test_explicit_column_that_does_not_exist(self):
        path = self.write("a.parquet", {"payload": [DOC]})
        with self.assertRaises(qc.InputError) as ctx:
            self.open(path, column="nope")
        self.assertIn("nope", str(ctx.exception))
        self.assertIn("payload", str(ctx.exception))

    def test_undecidable_columns_stop_and_list_the_candidates(self):
        path = self.write("a.parquet", {"left": ["a", "b"], "right": ["c", "d"]})
        with self.assertRaises(qc.InputError) as ctx:
            self.open(path, option="--relevant-column")
        message = str(ctx.exception)
        self.assertIn("left", message)
        self.assertIn("right", message)
        self.assertIn("--relevant-column", message)

    def test_no_text_column(self):
        path = self.write("a.parquet", {"n": [1, 2]})
        with self.assertRaises(qc.InputError):
            self.open(path)

    def test_binary_column_is_detected_and_decoded(self):
        path = self.write("a.parquet", {"id": ["x"], "payload": pa.array([DOC.encode("utf-8")], pa.binary())})
        pj = self.open(path)
        self.assertEqual(pj.column, "payload")
        self.assertEqual(pj.column_type, "binary")
        raw = next(pj.pf.iter_batches(batch_size=1, columns=["payload"])).column(0)[0].as_py()
        self.assertEqual(qc.parse_doc(raw), {"Item": {"plainId": {"S": "P1"}}})

    def test_double_encoded_json_column(self):
        path = self.write("a.parquet", {"id": ["x"], "payload": [json.dumps(DOC)]})
        pj = self.open(path)
        self.assertEqual(pj.column, "payload")

    def test_empty_file_with_two_text_columns_cannot_be_decided(self):
        path = self.write("a.parquet", {"a": pa.array([], pa.string()), "b": pa.array([], pa.string())})
        with self.assertRaises(qc.InputError):
            self.open(path)

    def test_named_integer_column_is_rejected_with_its_type(self):
        path = self.write("a.parquet", {"payload": [DOC], "n": [1]})
        with self.assertRaises(qc.InputError) as ctx:
            self.open(path, column="n")
        message = str(ctx.exception)
        self.assertIn("'n'", message)
        self.assertIn("int64", message)
        self.assertIn("not text", message)

    def test_named_struct_column_is_rejected(self):
        path = self.write("a.parquet", {"payload": [DOC], "s": [{"a": 1}]})
        with self.assertRaises(qc.InputError) as ctx:
            self.open(path, column="s")
        self.assertIn("struct", str(ctx.exception))
        self.assertIn("not text", str(ctx.exception))

    def test_dictionary_encoded_text_column_is_detected_and_read(self):
        path = self.write("a.parquet", {"payload": pa.array([DOC, DOC, DOC]).dictionary_encode()})
        pj = self.open(path)
        self.assertEqual(pj.column, "payload")
        self.assertIn("dictionary", pj.column_type)
        cursor = qc.RowCursor(pj)
        self.assertEqual(cursor.get(2), DOC)
        cursor.close()

    def test_dictionary_encoded_column_among_several_text_columns(self):
        path = self.write("a.parquet", {"id": ["x1", "x2"], "payload": pa.array([DOC, DOC]).dictionary_encode()})
        self.assertEqual(self.open(path).column, "payload")

    def test_dictionary_of_integers_is_not_text(self):
        path = self.write("a.parquet", {"codes": pa.array([1, 2, 1]).dictionary_encode()})
        with self.assertRaises(qc.InputError):
            self.open(path)

    @unittest.skipUnless(hasattr(pa, "string_view"), "this pyarrow has no string_view type")
    def test_string_view_column_is_detected(self):
        path = self.folder / "view.parquet"
        try:
            pq.write_table(pa.table({"payload": pa.array([DOC, DOC], type=pa.string_view())}), str(path))
        except Exception as exc:  # parquet support for view types depends on the pyarrow version
            self.skipTest("cannot write a string_view column: %s" % exc)
        pj = self.open(path)
        self.assertEqual(pj.column, "payload")

    def test_a_row_that_is_not_text_is_invalid_json(self):
        for raw in (5, 2.5, True, {"a": 1}, [1], object()):
            with self.assertRaises(qc.InvalidJson, msg=repr(raw)) as ctx:
                qc.parse_doc(raw)
            self.assertIn("not JSON text", str(ctx.exception))

    def test_row_group_index(self):
        path = self.folder / "g.parquet"
        pq.write_table(pa.table({"payload": [DOC] * 8}), str(path), row_group_size=3)
        pj = self.open(path)
        self.assertEqual(pj.starts, [0, 3, 6, 8])

    def test_unreadable_file_is_an_input_error(self):
        path = self.folder / "bad.parquet"
        path.write_text("not parquet")
        with self.assertRaises(qc.InputError):
            self.open(path)


if __name__ == "__main__":
    unittest.main()

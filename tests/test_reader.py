import json
import random
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures


def plain_id(raw):
    return json.loads(raw)["Item"]["plainId"]["S"] if raw.startswith('{"Item": {"plainId": {') else raw


class RowCursorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.paths = fixtures.write_fixture(self.folder)
        self.pj = qc.ParquetJson(self.paths["original"])
        self.addCleanup(self.pj.close)
        self.truth = [fixtures.raw_text(d) for d in fixtures.ORIGINAL_DOCS]

    def cursor(self, batch_rows=2):
        return qc.RowCursor(self.pj, batch_rows=batch_rows)

    def test_first_row_is_index_zero(self):
        self.assertEqual(self.cursor().get(0), self.truth[0])

    def test_last_row(self):
        self.assertEqual(self.cursor().get(7), self.truth[7])

    def test_outside_the_file_is_none(self):
        cursor = self.cursor()
        for idx in (-1, -5, 8, 9, 1000):
            self.assertIsNone(cursor.get(idx), idx)

    def test_outside_the_file_does_not_disturb_the_position(self):
        cursor = self.cursor()
        self.assertEqual(cursor.get(3), self.truth[3])
        self.assertIsNone(cursor.get(99))
        self.assertEqual(cursor.get(4), self.truth[4])

    def test_neighbour_across_a_batch_boundary(self):
        cursor = self.cursor(batch_rows=2)
        cursor.get(1)                                   # last row of the first batch
        self.assertEqual(cursor.get(2), self.truth[2])  # next batch
        self.assertEqual(cursor.get(1), self.truth[1])  # row just behind: still available
        self.assertEqual(cursor.get(0), self.truth[0])  # and the one before it

    def test_neighbour_across_a_row_group_boundary(self):
        cursor = self.cursor(batch_rows=100)
        self.assertEqual(cursor.get(2), self.truth[2])   # last row of the first row group
        self.assertEqual(cursor.get(3), self.truth[3])   # first row of the second
        self.assertEqual(cursor.get(2), self.truth[2])

    def test_going_far_back_rewinds(self):
        cursor = self.cursor()
        self.assertEqual(cursor.get(7), self.truth[7])
        self.assertEqual(cursor.get(0), self.truth[0])
        self.assertEqual(cursor.get(5), self.truth[5])

    def test_row_groups_in_between_are_not_read(self):
        cursor = self.cursor(batch_rows=1)
        opened, read = [], []
        original_open, original_next = cursor._open, cursor._next_batch
        cursor._open = lambda group: (opened.append(group), original_open(group))[1]
        cursor._next_batch = lambda: (read.append(1), original_next())[1]
        cursor.get(0)
        cursor.get(7)                                    # the rest of group 0 and all of group 1 are skipped
        self.assertEqual(opened, [0, 2])
        self.assertEqual(len(read), 3)                   # row 0, then rows 6 and 7: rows 1..5 never read

    def test_consecutive_rows_across_row_groups_never_reopen(self):
        cursor = self.cursor(batch_rows=1)
        opened = []
        original_open = cursor._open
        cursor._open = lambda group: (opened.append(group), original_open(group))[1]
        for idx in range(8):
            self.assertEqual(cursor.get(idx), self.truth[idx])
        self.assertEqual(opened, [0])

    def test_a_large_batch_size_reads_whole_row_groups(self):
        cursor = self.cursor(batch_rows=100)
        self.assertEqual(cursor.get(0), self.truth[0])
        self.assertEqual(cursor.get(7), self.truth[7])
        self.assertEqual(cursor.get(3), self.truth[3])

    def test_row_groups_are_read_one_reader_at_a_time(self):
        cursor = self.cursor(batch_rows=100)
        readers = []
        real = cursor._batches_of
        cursor._batches_of = lambda group: (readers.append(group), real(group))[1]
        for idx in range(8):
            cursor.get(idx)
        self.assertEqual(readers, [0, 1, 2])            # a separate reader for each row group

    def test_random_access_matches_the_file_for_every_batch_size(self):
        rng = random.Random(7)
        for batch_rows in (1, 2, 3, 5, 100):
            cursor = self.cursor(batch_rows)
            for _ in range(300):
                idx = rng.randrange(-2, 11)
                expected = self.truth[idx] if 0 <= idx < 8 else None
                self.assertEqual(cursor.get(idx), expected, (batch_rows, idx))

    def test_mostly_forward_walk_with_neighbour_lookups(self):
        for batch_rows in (1, 2, 3, 100):
            cursor = self.cursor(batch_rows)
            for idx in range(8):
                self.assertEqual(cursor.get(idx), self.truth[idx])
                self.assertEqual(cursor.get(idx - 1), self.truth[idx - 1] if idx else None)
                self.assertEqual(cursor.get(idx + 1), self.truth[idx + 1] if idx < 7 else None)

    def test_line_numbers_are_one_based_for_callers(self):
        # line 1 of the file is row index 0
        self.assertEqual(self.cursor().get(1 - 1), self.truth[0])


class MemoryDoesNotGrowWithTheFileTest(unittest.TestCase):
    """A reader that spans many row groups keeps what it has read; the cursor must not."""

    def test_scanning_every_row_leaves_memory_far_below_the_file_size(self):
        import random
        import pyarrow as pa
        import pyarrow.parquet as pq

        rng = random.Random(3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.parquet"
            documents = ['{"Item":{"x":{"S":"%s"}}}' % "".join(rng.choice("0123456789abcdef") for _ in range(4000))
                         for _ in range(1000)]
            writer = pq.ParquetWriter(str(path), pa.schema([("payload", pa.string())]))
            for _ in range(25):                               # 25 row groups of 1000 documents
                writer.write_table(pa.table({"payload": documents}))
            writer.close()
            size = path.stat().st_size
            pj = qc.ParquetJson(path)
            try:
                self.assertEqual(pj.num_groups, 25)
                cursor = qc.RowCursor(pj)
                baseline = pa.total_allocated_bytes()
                for idx in range(pj.num_rows):
                    cursor.get(idx)
                grown = pa.total_allocated_bytes() - baseline
                cursor.close()
            finally:
                pj.close()                                    # an open file cannot be deleted on Windows
            self.assertGreater(size, 20 * 2 ** 20)
            self.assertLess(grown, size * 0.3, "allocated %d bytes while scanning a %d byte file" % (grown, size))


class CsvInputTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        path = self.folder / "x_extracted.csv"
        fixtures.write_csv(path, [fixtures.extracted_row(c) for c in fixtures.CASES[:6]])
        self.path = path

    def numbers(self, **kwargs):
        return [n for n, _ in qc.CsvInput(self.path, fixtures.HEADER, **kwargs).rows()]

    def test_all_rows(self):
        self.assertEqual(self.numbers(), [1, 2, 3, 4, 5, 6])

    def test_limit(self):
        self.assertEqual(self.numbers(limit=2), [1, 2])

    def test_rows_filter(self):
        self.assertEqual(self.numbers(rows={2, 5}), [2, 5])

    def test_rows_filter_and_limit(self):
        self.assertEqual(self.numbers(rows={2, 5}, limit=3), [2])

    def test_row_list_parsing(self):
        self.assertEqual(qc.parse_row_list("17,203"), {17, 203})
        self.assertEqual(qc.parse_row_list(" 4 , 5 "), {4, 5})
        for bad in ("0", "a", "", "1,-2"):
            with self.assertRaises(qc.InputError, msg=bad):
                qc.parse_row_list(bad)

    def test_short_rows_are_padded_and_blank_lines_skipped(self):
        path = self.folder / "short_extracted.csv"
        path.write_text("A,B,C\n1,2\n\n3,4,5\n", encoding="utf-8")
        rows = list(qc.CsvInput(path, ["A", "B", "C"]).rows())
        self.assertEqual(rows, [(1, ["1", "2", ""]), (2, ["3", "4", "5"])])

    def test_byte_order_mark_is_ignored(self):
        path = self.folder / "bom_extracted.csv"
        path.write_text("﻿A,B\n1,2\n", encoding="utf-8")
        self.assertEqual(qc.read_header(path), ["A", "B"])


if __name__ == "__main__":
    unittest.main()

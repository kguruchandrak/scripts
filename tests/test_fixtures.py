import csv
import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from tests import fixtures


class FixtureSanityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.paths = fixtures.write_fixture(self.folder)

    def test_original_parquet_reads_back(self):
        pf = pq.ParquetFile(str(self.paths["original"]))
        self.assertEqual(pf.metadata.num_rows, 8)
        self.assertEqual(pf.metadata.num_row_groups, 3)
        self.assertEqual(pf.schema_arrow.names, ["id", fixtures.JSON_COLUMN])
        first = pf.read_row_group(0).column(fixtures.JSON_COLUMN)[0].as_py()
        self.assertIn('"plainId"', first)

    def test_relevant_parquet_reads_back(self):
        pf = pq.ParquetFile(str(self.paths["relevant"]))
        self.assertEqual(pf.metadata.num_rows, 4)
        self.assertEqual(pf.metadata.num_row_groups, 2)

    def test_extracted_csv_reads_back(self):
        with open(str(self.paths["extracted"]), newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], fixtures.HEADER)
        self.assertEqual(len(rows) - 1, len(fixtures.CASES))
        self.assertTrue(all(len(r) == len(fixtures.HEADER) for r in rows))

    def test_relevant_documents_are_original_documents(self):
        self.assertIs(fixtures.RELEVANT_DOCS[0], fixtures.ORIGINAL_DOCS[0])   # relevant 1 = original 1
        self.assertIs(fixtures.RELEVANT_DOCS[1], fixtures.ORIGINAL_DOCS[2])   # relevant 2 = original 3
        self.assertIs(fixtures.RELEVANT_DOCS[2], fixtures.ORIGINAL_DOCS[3])   # relevant 3 = original 4
        self.assertIs(fixtures.RELEVANT_DOCS[3], fixtures.ORIGINAL_DOCS[7])   # relevant 4 = original 8


if __name__ == "__main__":
    unittest.main()

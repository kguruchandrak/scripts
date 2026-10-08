"""End to end: the whole fixture through the command line, compared with reviewed expected files.

tests/expected_verified.csv and tests/expected_verified_skip.csv were produced by a run and checked row by
row against the expectations in tests/fixtures.py.  If the output format changes on purpose, regenerate them
(run the fixture through extraction_qc.py) and review every row again.
"""
import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures

HERE = Path(__file__).resolve().parent


def read_rows(path):
    with open(str(path), newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        fixtures.write_fixture(self.folder)

    def run_main(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return qc.main(["--folder", str(self.folder)] + list(extra))

    def verified(self):
        return read_rows(self.folder / "abc123_verified.csv")

    def test_verified_csv_equals_the_expected_file(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.verified(), read_rows(HERE / "expected_verified.csv"))

    def test_skip_on_failure_output_equals_the_expected_file(self):
        self.assertEqual(self.run_main("--skip-original-on-relevant-failure"), 0)
        self.assertEqual(self.verified(), read_rows(HERE / "expected_verified_skip.csv"))

    def test_debug_does_not_change_the_output(self):
        self.run_main("--debug", "--show-values")
        self.assertEqual(self.verified(), read_rows(HERE / "expected_verified.csv"))

    def test_a_tiny_chunk_size_does_not_change_the_output(self):
        self.addCleanup(setattr, qc, "CHUNK_ROWS", qc.CHUNK_ROWS)
        qc.CHUNK_ROWS = 3                                   # many chunks, many rewinds
        self.run_main()
        self.assertEqual(self.verified(), read_rows(HERE / "expected_verified.csv"))

    def test_every_reason_code_and_both_outcomes_are_covered(self):
        self.run_main()
        header, rows = self.verified()[0], self.verified()[1:]
        col = {name: i for i, name in enumerate(header)}
        codes = set()
        for row in rows:
            for column in ("RelevantFileReason", "OriginalFileReason"):
                if row[col[column]]:
                    codes.add(qc.reason_code(row[col[column]]))
        self.assertEqual(codes, {"LINE_OUT_OF_RANGE", "INVALID_LINE", "UNSUPPORTED_PATH", "INVALID_JSON",
                                 "PATH_NOT_FOUND", "VALUE_MISMATCH", "FORMAT_CHANGED"})
        for column in ("RelevantFileVerification", "OriginalFileVerification", "OverallVerification"):
            self.assertEqual({row[col[column]] for row in rows}, {"Correct", "Wrong"}, column)
        self.run_main("--skip-original-on-relevant-failure")
        skipped = self.verified()[1:]
        self.assertIn("Skipped", {row[col["OriginalFileVerification"]] for row in skipped})
        self.assertTrue(any(row[col["OriginalFileReason"]].startswith("SKIPPED") for row in skipped))

    def test_every_row_matches_the_expectation_written_next_to_the_fixture(self):
        self.run_main()
        header, rows = self.verified()[0], self.verified()[1:]
        col = {name: i for i, name in enumerate(header)}
        self.assertEqual(len(rows), len(fixtures.CASES))
        for case, row in zip(fixtures.CASES, rows):
            for which, verdict_col, reason_col in (("rel_expect", "RelevantFileVerification", "RelevantFileReason"),
                                                   ("orig_expect", "OriginalFileVerification", "OriginalFileReason")):
                expect = getattr(case, which)
                if expect == "Correct":
                    self.assertEqual((row[col[verdict_col]], row[col[reason_col]]), ("Correct", ""), case.name)
                else:
                    self.assertEqual(row[col[verdict_col]], "Wrong", case.name)
                    self.assertEqual(qc.reason_code(row[col[reason_col]]), expect, case.name)

    def test_input_files_are_left_alone(self):
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        self.run_main("--debug")
        for name, content in before.items():
            self.assertEqual((self.folder / name).read_bytes(), content, name)


if __name__ == "__main__":
    unittest.main()

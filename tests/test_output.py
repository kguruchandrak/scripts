import contextlib
import csv
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures


class RunTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def write(self, cases=None, **kwargs):
        return fixtures.write_fixture(self.folder, cases=cases, **kwargs)

    def run_main(self, *extra, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def read_verified(self, prefix="abc123", kind="verified"):
        with open(str(self.folder / ("%s_%s.csv" % (prefix, kind))), newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        return rows[0], rows[1:]


def digest(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


class VerifiedCsvTest(RunTestCase):
    def test_header_is_the_original_columns_then_the_five_new_ones(self):
        self.write()
        code, _, _ = self.run_main()
        header, rows = self.read_verified()
        self.assertEqual(code, 0)
        self.assertEqual(header, fixtures.HEADER + [
            "RelevantFileVerification", "RelevantFileReason",
            "OriginalFileVerification", "OriginalFileReason", "OverallVerification"])
        self.assertEqual(len(rows), len(fixtures.CASES))

    def test_original_columns_rows_and_order_are_unchanged(self):
        awkward = [
            fixtures.Case("zeros", "1", "1", "$.Item.plainId", "007", "", ""),
            fixtures.Case("spaces", "1", "1", "$.Item.spaced", " P1 ", "", ""),
            fixtures.Case("comma", "1", "1", "$.Item.plainId", "a,b", "", ""),
            fixtures.Case("quote", "1", "1", "$.Item.plainId", 'say "hi"', "", ""),
            fixtures.Case("newline", "1", "1", "$.Item.plainId", "line1\nline2", "", ""),
            fixtures.Case("unicode", "1", "1", "$.Item.plainId", "café", "", ""),
        ]
        paths = self.write(awkward)
        with open(str(paths["extracted"]), newline="", encoding="utf-8") as handle:
            before = list(csv.reader(handle))
        self.run_main()
        header, rows = self.read_verified()
        self.assertEqual([r[:len(fixtures.HEADER)] for r in rows], before[1:])
        value_column = fixtures.HEADER.index("Value")
        self.assertEqual([r[value_column] for r in rows], ["007", " P1 ", "a,b", 'say "hi"', "line1\nline2", "café"])

    def test_input_files_are_not_modified(self):
        paths = self.write()
        before = {name: digest(path) for name, path in paths.items()}
        self.run_main()
        self.assertEqual({name: digest(path) for name, path in paths.items()}, before)

    def test_overall_verdict(self):
        self.write()
        self.run_main()
        header, rows = self.read_verified()
        col = {name: i for i, name in enumerate(header)}
        for case, row in zip(fixtures.CASES, rows):
            both = case.rel_expect == "Correct" and case.orig_expect == "Correct"
            self.assertEqual(row[col["OverallVerification"]], "Correct" if both else "Wrong", case.name)
            self.assertEqual(row[col["RelevantFileVerification"]],
                             "Correct" if case.rel_expect == "Correct" else "Wrong", case.name)
            self.assertEqual(row[col["OriginalFileVerification"]],
                             "Correct" if case.orig_expect == "Correct" else "Wrong", case.name)
            if case.rel_expect == "Correct":
                self.assertEqual(row[col["RelevantFileReason"]], "", case.name)
            else:
                self.assertTrue(row[col["RelevantFileReason"]].startswith(case.rel_expect), case.name)
            if case.orig_expect != "Correct":
                self.assertTrue(row[col["OriginalFileReason"]].startswith(case.orig_expect), case.name)

    def test_lanes_are_independent_by_default(self):
        self.write()
        self.run_main()
        header, rows = self.read_verified()
        col = {name: i for i, name in enumerate(header)}
        row = rows[[c.name for c in fixtures.CASES].index("relevant_wrong_original_ok")]
        self.assertEqual(row[col["RelevantFileVerification"]], "Wrong")
        self.assertEqual(row[col["OriginalFileVerification"]], "Correct")
        self.assertEqual(row[col["OverallVerification"]], "Wrong")

    def test_byte_order_mark_follows_the_input(self):
        paths = self.write()
        self.run_main()
        self.assertFalse((self.folder / "abc123_verified.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        text = paths["extracted"].read_text(encoding="utf-8")
        paths["extracted"].write_text("﻿" + text, encoding="utf-8")
        self.run_main()
        self.assertTrue((self.folder / "abc123_verified.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.read_verified()[0][0], "SourceFileName")

    def test_rows_with_extra_fields_keep_the_columns_aligned_and_warn(self):
        paths = self.write(fixtures.CASES[:2])
        with open(str(paths["extracted"]), "a", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(fixtures.extracted_row(fixtures.CASES[0]) + ["stray"])
        code, out, _ = self.run_main()
        header, rows = self.read_verified()
        self.assertTrue(all(len(r) == len(header) for r in rows))
        self.assertIn("1 row(s) had more fields", out)

    def test_short_rows_are_padded(self):
        paths = self.write(fixtures.CASES[:1])
        with open(str(paths["extracted"]), "a", newline="", encoding="utf-8") as handle:
            handle.write("only,three,fields\n")
        self.run_main()
        header, rows = self.read_verified()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(r) == len(header) for r in rows))
        self.assertTrue(rows[1][header.index("OriginalFileReason")].startswith("INVALID_LINE"))

    def test_no_partial_file_is_left_behind(self):
        self.write()
        self.run_main()
        self.assertEqual(sorted(p.name for p in self.folder.glob("*.partial")), [])

    def test_a_failure_while_writing_leaves_no_verified_csv(self):
        self.write()
        real = qc.merge_results

        def broken(*args, **kwargs):
            raise qc.InputError("boom")

        qc.merge_results = broken
        self.addCleanup(setattr, qc, "merge_results", real)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertFalse((self.folder / "abc123_verified.csv").exists())
        self.assertEqual(sorted(p.name for p in self.folder.glob("*.partial")), [])

    def test_a_previous_verified_csv_is_replaced(self):
        self.write()
        (self.folder / "abc123_verified.csv").write_text("old")
        self.run_main()
        self.assertEqual(self.read_verified()[0][0], "SourceFileName")


class SkipOnFailureTest(RunTestCase):
    def test_failed_relevant_rows_are_skipped(self):
        self.write()
        code, _, _ = self.run_main("--skip-original-on-relevant-failure")
        header, rows = self.read_verified()
        col = {name: i for i, name in enumerate(header)}
        self.assertEqual(code, 0)
        for case, row in zip(fixtures.CASES, rows):
            if case.rel_expect != "Correct":
                self.assertEqual(row[col["OriginalFileVerification"]], "Skipped", case.name)
                self.assertTrue(row[col["OriginalFileReason"]].startswith("SKIPPED"), case.name)
                self.assertEqual(row[col["OverallVerification"]], "Wrong", case.name)
            else:
                expected = "Correct" if case.orig_expect == "Correct" else "Wrong"
                self.assertEqual(row[col["OriginalFileVerification"]], expected, case.name)
                if case.orig_expect != "Correct":
                    self.assertTrue(row[col["OriginalFileReason"]].startswith(case.orig_expect), case.name)

    def test_skipped_rows_are_not_read_from_the_original_file(self):
        cases = [fixtures.Case("rel_wrong", "8", "1", "$.Item.plainId", "nope", "", ""),
                 fixtures.Case("rel_ok", "1", "1", "$.Item.plainId", "P1", "", "")]
        self.write(cases)
        requested = []
        real_get = qc.RowCursor.get

        def spy(cursor, idx):
            requested.append((cursor.pj.path.name, idx))
            return real_get(cursor, idx)

        qc.RowCursor.get = spy
        self.addCleanup(setattr, qc.RowCursor, "get", real_get)

        self.run_main("--skip-original-on-relevant-failure")
        self.assertNotIn(("abc123_original.parquet", 7), requested)   # SourceLine 8 was skipped
        self.assertIn(("abc123_original.parquet", 0), requested)

        del requested[:]
        self.run_main()
        self.assertIn(("abc123_original.parquet", 7), requested)      # read when the switch is off

    def test_summary_counts_skipped_rows(self):
        self.write()
        _, out, _ = self.run_main("--skip-original-on-relevant-failure")
        skipped = sum(1 for c in fixtures.CASES if c.rel_expect != "Correct")
        original_line = [l for l in out.splitlines() if l.startswith("original")][0]
        self.assertEqual(original_line.split()[-1], str(skipped))
        self.assertIn("SKIPPED %d" % skipped, out)


class SummaryTest(RunTestCase):
    def counts(self, which):
        right = sum(1 for c in fixtures.CASES if getattr(c, which) == "Correct")
        return right, len(fixtures.CASES) - right

    def test_summary_matches_the_known_results(self):
        self.write()
        code, out, _ = self.run_main()
        lines = {l.split()[0]: l.split() for l in out.splitlines() if l.split()[:1] in (["relevant"], ["original"], ["overall"])}
        rel_right, rel_wrong = self.counts("rel_expect")
        orig_right, orig_wrong = self.counts("orig_expect")
        both = sum(1 for c in fixtures.CASES if c.rel_expect == c.orig_expect == "Correct")
        self.assertIn("%d rows checked" % len(fixtures.CASES), out)
        self.assertEqual(lines["relevant"][1:], [str(rel_right), str(rel_wrong), "0"])
        self.assertEqual(lines["original"][1:], [str(orig_right), str(orig_wrong), "0"])
        self.assertEqual(lines["overall"][1:], [str(both), str(len(fixtures.CASES) - both), "0"])

    def test_reason_codes_are_counted_per_lane(self):
        self.write()
        _, out, _ = self.run_main()
        for code in ("VALUE_MISMATCH", "FORMAT_CHANGED", "PATH_NOT_FOUND", "UNSUPPORTED_PATH"):
            expected = sum(1 for c in fixtures.CASES if c.rel_expect == code)
            self.assertIn("%s %d" % (code, expected), out.split("reasons, original")[0], code)
        for code in ("LINE_OUT_OF_RANGE", "INVALID_LINE", "INVALID_JSON"):
            expected = sum(1 for c in fixtures.CASES if c.orig_expect == code)
            self.assertIn("%s %d" % (code, expected), out.split("reasons, original")[1], code)

    def test_progress_goes_to_stderr(self):
        self.write()
        _, out, err = self.run_main()
        self.assertNotIn("rows checked]", out)

    def test_lane_progress_is_reported_per_chunk(self):
        paths = self.write()
        csv_input = qc.CsvInput(paths["extracted"], fixtures.HEADER)
        pj = qc.ParquetJson(paths["relevant"])
        self.addCleanup(pj.close)
        messages = []
        qc.run_lane("relevant", csv_input, "RelevancyParquetLine", pj, self.folder / "r.csv",
                    progress=messages.append, chunk_rows=5)
        self.assertEqual(messages, ["[relevant] 5 rows checked", "[relevant] 10 rows checked",
                                    "[relevant] 15 rows checked", "[relevant] 20 rows checked"])

    def test_output_progress_is_reported_periodically(self):
        self.write()
        real = qc.MERGE_PROGRESS_ROWS
        qc.MERGE_PROGRESS_ROWS = 10
        self.addCleanup(setattr, qc, "MERGE_PROGRESS_ROWS", real)
        _, _, err = self.run_main()
        self.assertIn("[output] 10 rows written", err)
        self.assertIn("[output] 20 rows written", err)


class TrialRunLimitsTest(RunTestCase):
    def test_limit_keeps_only_the_first_rows(self):
        self.write()
        code, out, _ = self.run_main("--limit", "3")
        header, rows = self.read_verified(kind="verified_trial")
        self.assertEqual(code, 0)
        self.assertEqual([r[header.index("CanonicalField")] for r in rows],
                         ["Canon_" + c.name for c in fixtures.CASES[:3]])
        self.assertIn("3 rows checked", out)

    def test_rows_keeps_only_the_listed_rows_with_their_own_results(self):
        self.write()
        self.run_main("--rows", "9,14")
        header, rows = self.read_verified(kind="verified_trial")
        picked = [fixtures.CASES[8], fixtures.CASES[13]]
        self.assertEqual([r[header.index("CanonicalField")] for r in rows], ["Canon_" + c.name for c in picked])
        for case, row in zip(picked, rows):
            self.assertTrue(row[header.index("OriginalFileReason")].startswith(case.orig_expect), case.name)

    def test_limit_and_rows_together(self):
        self.write()
        self.run_main("--rows", "2,5", "--limit", "3")
        header, rows = self.read_verified(kind="verified_trial")
        self.assertEqual([r[header.index("CanonicalField")] for r in rows], ["Canon_" + fixtures.CASES[1].name])

    def test_rows_beyond_the_end_are_ignored(self):
        self.write()
        self.run_main("--rows", "2,999")
        self.assertEqual(len(self.read_verified(kind="verified_trial")[1]), 1)

    def test_bad_values_are_rejected(self):
        self.write()
        for args in (["--limit", "0"], ["--limit", "x"], ["--rows", "a,b"], ["--rows", "0"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_main(*args)
            self.assertEqual(ctx.exception.code, 2, args)

    def test_skip_switch_with_a_row_filter_stays_aligned(self):
        self.write()
        self.run_main("--skip-original-on-relevant-failure", "--rows", "1,9,14,22")
        header, rows = self.read_verified(kind="verified_trial")
        col = {name: i for i, name in enumerate(header)}
        picked = [fixtures.CASES[i - 1] for i in (1, 9, 14, 22)]
        self.assertEqual([r[col["CanonicalField"]] for r in rows], ["Canon_" + c.name for c in picked])
        for case, row in zip(picked, rows):
            if case.rel_expect != "Correct":
                self.assertEqual(row[col["OriginalFileVerification"]], "Skipped", case.name)


if __name__ == "__main__":
    unittest.main()

"""Skipping md5s that already have an up-to-date verified file, --force, and the run total."""
import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures

BASE_NS = 1700000000 * 10 ** 9


def stamp(path, seconds):
    """Set a file's modification time to BASE + seconds."""
    ns = BASE_NS + int(seconds * 10 ** 9)
    os.utime(str(path), ns=(ns, ns))


class SkipCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.calls = []                       # the md5s that were actually checked
        real = qc.verify_triple

        def spy(triple, args, **kwargs):
            self.calls.append(triple.prefix)
            return real(triple, args, **kwargs)

        qc.verify_triple = spy
        self.addCleanup(setattr, qc, "verify_triple", real)
        # the times below are in 2023; the script itself is newer, which would recheck everything
        self.default_script_path = qc.SCRIPT_PATH
        self.addCleanup(setattr, qc, "SCRIPT_PATH", qc.SCRIPT_PATH)
        qc.SCRIPT_PATH = None

    def run_main(self, *extra, folder=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(folder or self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def md5(self, prefix="abc123", extracted_format="csv", folder=None):
        return fixtures.write_folder_fixture(folder or self.folder, prefix=prefix, extracted_format=extracted_format)

    def set_times(self, prefix="abc123", folder=None, inputs_at=0, verified_at=100):
        """Inputs at `inputs_at`, the verified file at `verified_at` (seconds after a fixed base time)."""
        folder = folder or self.folder
        for sub, name in (("extracted", prefix + ".csv"), ("original", prefix + ".parquet"),
                          ("relevant", prefix + ".parquet")):
            path = folder / sub / name
            if path.exists():
                stamp(path, inputs_at)
        stamp(folder / "verified" / (prefix + "_verified.csv"), verified_at)

    def finished(self, prefix="abc123", folder=None, inputs_at=0, verified_at=100):
        """Check an md5 once, then set the times so that its result is up to date."""
        folder = folder or self.folder
        code, _, err = self.run_main(folder=folder)
        self.assertEqual(code, 0, err)
        self.set_times(prefix, folder, inputs_at, verified_at)
        del self.calls[:]

    def verified_bytes(self, prefix="abc123"):
        return (self.folder / "verified" / (prefix + "_verified.csv")).read_bytes()


class SkipRuleTest(SkipCase):
    def test_an_up_to_date_md5_is_skipped_and_listed(self):
        self.md5()
        self.finished()
        before = self.verified_bytes()
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])
        self.assertIn("extracted/abc123.csv: skipped, verified/abc123_verified.csv is newer than its inputs and "
                      "this script; --force rechecks it", out)
        self.assertEqual(self.verified_bytes(), before)
        self.assertNotIn("rows checked", out)

    def test_equal_times_are_not_up_to_date(self):
        # a file system with coarse timestamps can give a stale result and a changed input the same time
        self.md5()
        self.finished(inputs_at=50, verified_at=50)
        self.run_main()
        self.assertEqual(self.calls, ["abc123"])

    def test_a_microsecond_newer_is_up_to_date(self):
        self.md5()
        self.finished()
        path = self.folder / "verified" / "abc123_verified.csv"
        ns = BASE_NS + 1000                                               # NTFS keeps 100 ns steps
        os.utime(str(path), ns=(ns, ns))                                  # inputs are at BASE_NS exactly
        self.run_main()
        self.assertEqual(self.calls, [])

    def test_each_input_that_is_newer_causes_a_recheck(self):
        for sub, name in (("extracted", "abc123.csv"), ("original", "abc123.parquet"), ("relevant", "abc123.parquet")):
            folder = self.folder / sub
            work = self.folder / ("case_" + sub)
            work.mkdir()
            self.md5(folder=work)
            self.finished(folder=work)
            stamp(work / sub / name, 101)                        # one second after the verified file
            code, out, err = self.run_main(folder=work)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.calls, ["abc123"], sub)
            self.assertIn("rows checked", out)
            del self.calls[:]

    def test_the_recheck_replaces_the_verified_file(self):
        self.md5()
        self.finished()
        path = self.folder / "verified" / "abc123_verified.csv"
        path.write_text("stale")
        stamp(path, 100)
        stamp(self.folder / "original" / "abc123.parquet", 200)
        self.run_main()
        self.assertEqual(self.calls, ["abc123"])
        self.assertNotEqual(path.read_text(encoding="utf-8-sig")[:5], "stale")
        self.assertIn("OverallVerification", path.read_text(encoding="utf-8-sig"))

    def test_a_trial_run_is_never_skipped(self):
        self.md5()
        self.finished()
        before = self.verified_bytes()
        for switches in (["--limit", "5"], ["--rows", "2,3"]):
            code, out, err = self.run_main(*switches)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.calls, ["abc123"], switches)
            self.assertNotIn(": skipped, ", out)
            del self.calls[:]
        self.assertTrue((self.folder / "verified" / "abc123_verified_trial.csv").is_file())
        self.assertEqual(self.verified_bytes(), before)

    def test_a_partial_file_is_not_a_result(self):
        self.md5()
        (self.folder / "verified").mkdir()
        partial = self.folder / "verified" / "abc123_verified.csv.partial"
        partial.write_text("half done")
        for sub, name in (("extracted", "abc123.csv"), ("original", "abc123.parquet"), ("relevant", "abc123.parquet")):
            stamp(self.folder / sub / name, 0)
        stamp(partial, 1000)                                      # newer than every input
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, ["abc123"])
        self.assertTrue((self.folder / "verified" / "abc123_verified.csv").is_file())

    def test_a_trial_file_is_not_a_result_either(self):
        self.md5()
        self.run_main("--limit", "3")
        del self.calls[:]
        for sub, name in (("extracted", "abc123.csv"), ("original", "abc123.parquet"), ("relevant", "abc123.parquet")):
            stamp(self.folder / sub / name, 0)
        stamp(self.folder / "verified" / "abc123_verified_trial.csv", 1000)
        self.run_main()
        self.assertEqual(self.calls, ["abc123"])

    def test_the_flat_layout_always_rechecks(self):
        fixtures.write_fixture(self.folder)
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        for name in ("abc123_extracted.csv", "abc123_original.parquet", "abc123_relevant.parquet"):
            stamp(self.folder / name, 0)
        stamp(self.folder / "abc123_verified.csv", 1000)
        del self.calls[:]
        code, out, _ = self.run_main()
        self.assertEqual(self.calls, ["abc123"])
        self.assertNotIn(": skipped, ", out)

    def test_a_missing_input_is_not_skipped_and_is_reported(self):
        self.md5()
        self.finished()
        (self.folder / "relevant" / "abc123.parquet").unlink()
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, ["abc123"])
        self.assertIn("relevant/abc123.parquet", err)

    def test_an_ambiguous_extracted_file_is_not_skipped(self):
        self.md5()
        self.finished()
        parquet = self.folder / "extracted" / "abc123.parquet"
        parquet.write_bytes(b"x")
        stamp(parquet, 0)
        stamp(self.folder / "extracted" / "abc123.csv", 0)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)

    def test_with_both_formats_the_chosen_file_decides(self):
        self.md5()
        fixtures.write_extracted_parquet(self.folder / "extracted" / "abc123.parquet",
                                         [fixtures.extracted_row(c) for c in fixtures.CASES], row_group_size=7)
        csv_path = self.folder / "extracted" / "abc123.csv"
        parquet_path = self.folder / "extracted" / "abc123.parquet"
        stamp(csv_path, 0)
        stamp(parquet_path, 10)
        self.run_main("--force")
        for sub, name in (("original", "abc123.parquet"), ("relevant", "abc123.parquet")):
            stamp(self.folder / sub / name, 0)
        stamp(self.folder / "verified" / "abc123_verified.csv", 20)
        del self.calls[:]
        self.run_main()
        self.assertEqual(self.calls, [])                          # parquet (10) is older than verified (20)
        stamp(parquet_path, 30)
        self.run_main()
        self.assertEqual(self.calls, ["abc123"])

    def test_skipped_md5s_are_never_opened(self):
        self.md5()
        self.finished()
        opened = []
        patches = {
            (qc.ParquetJson, "__init__"): qc.ParquetJson.__init__,
            (qc.ParquetExtractedInput, "open"): qc.ParquetExtractedInput.open,
            (qc.CsvInput, "rows"): qc.CsvInput.rows,
        }
        real_open = qc.open_extracted
        qc.open_extracted = lambda *a, **k: (opened.append("extracted"), real_open(*a, **k))[1]
        self.addCleanup(setattr, qc, "open_extracted", real_open)
        real_init = qc.ParquetJson.__init__
        qc.ParquetJson.__init__ = lambda self_, *a, **k: (opened.append("parquet"), real_init(self_, *a, **k))[1]
        self.addCleanup(setattr, qc.ParquetJson, "__init__", real_init)
        real_prepare = qc.prepare_triple
        qc.prepare_triple = lambda *a, **k: (opened.append("prepare"), real_prepare(*a, **k))[1]
        self.addCleanup(setattr, qc, "prepare_triple", real_prepare)
        self.run_main()
        self.assertEqual(opened, [])


class ForceTest(SkipCase):
    def test_force_rechecks_an_up_to_date_md5(self):
        self.md5()
        self.finished()
        path = self.folder / "verified" / "abc123_verified.csv"
        mtime = path.stat().st_mtime_ns
        code, out, err = self.run_main("--force")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, ["abc123"])
        self.assertNotIn(": skipped, ", out)
        self.assertGreater(path.stat().st_mtime_ns, mtime)           # replaced

    def test_changing_a_switch_does_not_recheck_until_force(self):
        self.md5()
        self.finished()
        self.run_main("--check-same-document")
        self.assertEqual(self.calls, [])                              # skipped, as documented
        self.run_main("--check-same-document", "--force")
        self.assertEqual(self.calls, ["abc123"])

    def test_force_in_the_flat_layout_changes_nothing(self):
        fixtures.write_fixture(self.folder)
        code, _, err = self.run_main("--force")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.folder / "abc123_verified.csv").is_file())

    def test_the_help_explains_force(self):
        text = " ".join(qc.build_parser().format_help().split())
        self.assertIn("--force", text)
        self.assertIn("newer than its inputs and this script is skipped", text)
        self.assertIn("--check-same-document", text)


class SummaryTest(SkipCase):
    def test_the_total_line_counts_checked_skipped_and_failed(self):
        for prefix in ("aaa111", "bbb222", "ccc333", "ddd444"):
            self.md5(prefix=prefix)
        self.run_main()                                  # every md5 now has a verified file
        (self.folder / "relevant" / "ccc333.parquet").unlink()                  # ccc333 will fail
        self.set_times("bbb222", inputs_at=0, verified_at=100)                  # up to date: skipped
        self.set_times("aaa111", inputs_at=200, verified_at=100)                # an input is newer: checked
        self.set_times("ddd444", inputs_at=200, verified_at=100)                # checked
        (self.folder / "verified" / "ccc333_verified.csv").write_text("old")    # even with a result: not skipped
        stamp(self.folder / "verified" / "ccc333_verified.csv", 1000)
        del self.calls[:]
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.calls), ["aaa111", "ccc333", "ddd444"])
        self.assertEqual(out.strip().splitlines()[-1], "total: 2 checked, 1 skipped, 1 failed")
        self.assertIn("extracted/bbb222.csv: skipped", out)
        self.assertIn("relevant/ccc333.parquet", err)

    def test_the_total_line_is_printed_in_the_flat_layout_too(self):
        fixtures.write_fixture(self.folder)
        _, out, _ = self.run_main()
        self.assertEqual(out.strip().splitlines()[-1], "total: 1 checked, 0 skipped, 0 failed")

    def test_the_total_line_follows_the_debug_block(self):
        self.md5()
        _, out, _ = self.run_main("--debug")
        lines = out.strip().splitlines()
        self.assertEqual(lines[-1], "total: 1 checked, 0 skipped, 0 failed")
        self.assertIn("extraction_qc debug", "\n".join(lines))

    def test_every_skipped_md5_has_a_line(self):
        for prefix in ("aaa111", "bbb222"):
            self.md5(prefix=prefix)
            self.finished(prefix=prefix)
        _, out, _ = self.run_main()
        self.assertEqual(self.calls, [])
        self.assertIn("extracted/aaa111.csv: skipped", out)
        self.assertIn("extracted/bbb222.csv: skipped", out)
        self.assertEqual(out.strip().splitlines()[-1], "total: 0 checked, 2 skipped, 0 failed")


def wrong_count(path):
    """The rows a verified CSV marks Wrong overall, counted by the test itself."""
    with open(str(path), newline="", encoding="utf-8-sig") as handle:
        return sum(1 for row in list(csv.reader(handle))[1:] if row[-1] == "Wrong")


class ExitCodeTest(SkipCase):
    def test_all_skipped_exits_zero_without_fail_on_wrong(self):
        self.md5()
        self.finished()
        self.assertGreater(wrong_count(self.folder / "verified" / "abc123_verified.csv"), 0)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])
        self.assertNotIn("Wrong)", out)                  # the file is not read unless --fail-on-wrong asks

    def test_a_skipped_md5_with_wrong_rows_still_fails_the_gate(self):
        # the first run exits 3 and so must a re-run that skips: otherwise a gate goes green on the second run
        self.md5()
        code, _, err = self.run_main("--fail-on-wrong")
        self.assertEqual(code, 3, err)
        self.set_times()
        del self.calls[:]
        code, out, err = self.run_main("--fail-on-wrong")
        self.assertEqual(self.calls, [])                 # still skipped, nothing is checked again
        self.assertEqual(code, 3, err)
        wrong = wrong_count(self.folder / "verified" / "abc123_verified.csv")
        self.assertIn("(%d row(s) Wrong); --force rechecks it" % wrong, out)
        self.assertEqual(out.strip().splitlines()[-1], "total: 0 checked, 1 skipped, 0 failed")

    def test_a_skipped_md5_without_wrong_rows_passes_the_gate(self):
        self.md5()
        fixtures.write_csv(self.folder / "extracted" / "abc123.csv", [fixtures.extracted_row(fixtures.CASES[0])])
        self.run_main()
        self.set_times()
        del self.calls[:]
        code, out, err = self.run_main("--fail-on-wrong")
        self.assertEqual((code, self.calls), (0, []), err)
        self.assertIn("(0 row(s) Wrong); --force rechecks it", out)

    def test_wrong_rows_of_a_skipped_md5_count_next_to_a_checked_correct_one(self):
        self.md5(prefix="aaa111")
        self.finished(prefix="aaa111")                               # has Wrong rows, up to date
        self.md5(prefix="bbb222")
        fixtures.write_csv(self.folder / "extracted" / "bbb222.csv",
                           [fixtures.extracted_row(fixtures.CASES[0], "bbb222")])      # all Correct
        code, _, err = self.run_main("--fail-on-wrong")
        self.assertEqual(self.calls, ["bbb222"])
        self.assertEqual(code, 3, err)                               # bbb222 is Correct, but skipped aaa111 is not

    def test_wrong_rows_of_a_checked_md5_count_next_to_a_skipped_correct_one(self):
        self.md5(prefix="aaa111")
        fixtures.write_csv(self.folder / "extracted" / "aaa111.csv",
                           [fixtures.extracted_row(fixtures.CASES[0], "aaa111")])      # all Correct
        self.finished(prefix="aaa111")
        self.md5(prefix="bbb222")
        fixtures.write_csv(self.folder / "extracted" / "bbb222.csv",
                           [fixtures.extracted_row(fixtures.CASES[8], "bbb222")])      # a Wrong row
        code, _, err = self.run_main("--fail-on-wrong")
        self.assertEqual(self.calls, ["bbb222"])
        self.assertEqual(code, 3, err)

    def test_a_verified_file_that_cannot_be_read_is_checked_again_under_fail_on_wrong(self):
        self.md5()
        self.finished()
        path = self.folder / "verified" / "abc123_verified.csv"
        path.write_bytes(b"not,a,verified,file\n1,2,3,4\n")
        stamp(path, 100)
        self.run_main()
        self.assertEqual(self.calls, [])                             # without the switch it is not read
        code, out, err = self.run_main("--fail-on-wrong")
        self.assertEqual(self.calls, ["abc123"])                     # with it, a file it cannot read is no result
        self.assertEqual(code, 3, err)
        self.assertGreater(wrong_count(path), 0)

    def test_the_wrong_rows_are_read_from_a_file_with_or_without_a_byte_order_mark(self):
        for encoding in ("utf-8", "utf-8-sig"):
            path = self.folder / ("v_%s.csv" % encoding)
            header = ["A"] + qc.OUTPUT_COLUMNS
            path.write_text("\n".join([",".join(header), "x,Correct,,Wrong,r,Wrong", "y,Wrong,r,Correct,,Wrong",
                                       "z,Correct,,Correct,,Correct"]) + "\n", encoding=encoding)
            self.assertEqual(qc.wrong_rows(path), 2, encoding)
        self.assertIsNone(qc.wrong_rows(self.folder / "missing.csv"))

    def test_a_failed_md5_still_exits_one(self):
        self.md5(prefix="aaa111")
        self.finished(prefix="aaa111")
        self.md5(prefix="bbb222")
        (self.folder / "original" / "bbb222.parquet").unlink()
        code, _, _ = self.run_main("--fail-on-wrong")
        self.assertEqual(code, 1)


class DebugOnSkippedTest(SkipCase):
    def test_a_skipped_md5_produces_no_debug_report(self):
        self.md5()
        self.finished()
        code, out, err = self.run_main("--debug")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])
        self.assertNotIn("extraction_qc debug", out)
        self.assertFalse((self.folder / "verified" / "abc123_debug_report.txt").exists())
        self.assertIn("--force rechecks it (and is needed for --debug)", out)

    def test_force_with_debug_produces_the_report(self):
        self.md5()
        self.finished()
        code, out, err = self.run_main("--debug", "--force")
        self.assertEqual(code, 0, err)
        self.assertIn("extraction_qc debug", out)
        self.assertTrue((self.folder / "verified" / "abc123_debug_report.txt").is_file())


class ScriptTimeTest(SkipCase):
    """A result older than the script may come from different code, so it is not trusted."""

    def make_script(self, seconds):
        path = self.folder / "script.py"
        path.write_text("# stand-in for extraction_qc.py\n")
        stamp(path, seconds)
        qc.SCRIPT_PATH = path

    def test_a_script_changed_after_the_result_causes_a_recheck(self):
        self.md5()
        self.finished()                                  # inputs at 0, verified at 100
        self.make_script(150)
        code, out, err = self.run_main()
        self.assertEqual(self.calls, ["abc123"], err)
        self.assertNotIn(": skipped, ", out)

    def test_a_script_older_than_the_result_does_not(self):
        self.md5()
        self.finished()
        self.make_script(50)
        self.run_main()
        self.assertEqual(self.calls, [])

    def test_a_script_exactly_as_new_as_the_result_causes_a_recheck(self):
        self.md5()
        self.finished()
        self.make_script(100)
        self.run_main()
        self.assertEqual(self.calls, ["abc123"])

    def test_the_skip_message_mentions_the_script(self):
        self.md5()
        self.finished()
        _, out, _ = self.run_main()
        self.assertIn("and this script", out)

    def test_the_real_script_is_the_default(self):
        self.assertEqual(self.default_script_path, Path(qc.__file__))
        qc.SCRIPT_PATH = self.default_script_path
        self.assertEqual(qc.script_time_ns(), Path(qc.__file__).stat().st_mtime_ns)

    def test_a_script_that_cannot_be_found_counts_as_no_time(self):
        qc.SCRIPT_PATH = self.folder / "gone.py"
        self.assertEqual(qc.script_time_ns(), 0)
        qc.SCRIPT_PATH = None
        self.assertEqual(qc.script_time_ns(), 0)


class SecondRunTest(SkipCase):
    def test_an_immediate_second_run_skips_and_force_rechecks(self):
        self.md5()
        code, out, _ = self.run_main()
        self.assertEqual(self.calls, ["abc123"])
        del self.calls[:]
        code, out, _ = self.run_main()
        self.assertEqual((code, self.calls), (0, []))
        self.assertIn("skipped", out)
        code, out, _ = self.run_main("--force")
        self.assertEqual(self.calls, ["abc123"])


if __name__ == "__main__":
    unittest.main()

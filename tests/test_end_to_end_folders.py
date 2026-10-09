"""End to end through the folder layout, compared with the same golden files as the flat layout."""
import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures

HERE = Path(__file__).resolve().parent
BASE_NS = 1700000000 * 10 ** 9


def read_rows(path):
    with open(str(path), newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def tree(folder):
    """Every file under folder with its bytes, to prove that inputs are left alone."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


class FolderEndToEndCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def run_main(self, *extra, folder=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(folder or self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def verified(self, name="abc123_verified.csv", folder=None):
        return read_rows((folder or self.folder) / "verified" / name)


class GoldenTest(FolderEndToEndCase):
    def test_a_csv_extracted_file_gives_the_golden_result(self):
        fixtures.write_folder_fixture(self.folder)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.verified(), read_rows(HERE / "expected_verified.csv"))
        self.assertIn("-> verified/abc123_verified.csv", out)

    def test_a_parquet_extracted_file_gives_the_golden_result(self):
        fixtures.write_folder_fixture(self.folder, extracted_format="parquet")
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.verified(), read_rows(HERE / "expected_verified.csv"))
        self.assertIn("extracted/abc123.parquet: 22 rows checked", out)

    def test_both_formats_with_skip_on_failure_give_the_skip_golden_result(self):
        for extracted_format in ("csv", "parquet"):
            folder = self.folder / extracted_format
            folder.mkdir()
            fixtures.write_folder_fixture(folder, extracted_format=extracted_format)
            code, _, err = self.run_main("--skip-original-on-relevant-failure", folder=folder)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.verified(folder=folder), read_rows(HERE / "expected_verified_skip.csv"), extracted_format)

    def test_debug_does_not_change_the_result_in_either_format(self):
        for extracted_format in ("csv", "parquet"):
            folder = self.folder / extracted_format
            folder.mkdir()
            fixtures.write_folder_fixture(folder, extracted_format=extracted_format)
            code, out, err = self.run_main("--debug", "--show-values", folder=folder)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.verified(folder=folder), read_rows(HERE / "expected_verified.csv"))
            self.assertIn("extraction_qc debug", out)
            self.assertTrue((folder / "verified" / "abc123_debug_report.txt").is_file())

    def test_the_inputs_are_left_alone(self):
        for extracted_format in ("csv", "parquet"):
            folder = self.folder / extracted_format
            folder.mkdir()
            fixtures.write_folder_fixture(folder, extracted_format=extracted_format)
            before = tree(folder)
            self.run_main("--debug", folder=folder)
            self.run_main("--limit", "4", "--force", folder=folder)
            after = tree(folder)
            for name, content in before.items():
                self.assertEqual(after[name], content, name)
            self.assertEqual(sorted(set(after) - set(before)),
                             ["verified\\abc123_debug_report.txt", "verified\\abc123_verified.csv",
                              "verified\\abc123_verified_trial.csv"] if os.sep == "\\" else
                             ["verified/abc123_debug_report.txt", "verified/abc123_verified.csv",
                              "verified/abc123_verified_trial.csv"])

    def test_the_byte_order_mark_follows_the_extracted_format(self):
        for extracted_format, expected in (("csv", False), ("parquet", True)):
            folder = self.folder / extracted_format
            folder.mkdir()
            fixtures.write_folder_fixture(folder, extracted_format=extracted_format)
            self.run_main(folder=folder)
            data = (folder / "verified" / "abc123_verified.csv").read_bytes()
            self.assertEqual(data.startswith(b"\xef\xbb\xbf"), expected, extracted_format)

    def test_csv_and_parquet_results_have_identical_content(self):
        paths = {}
        for extracted_format in ("csv", "parquet"):
            folder = self.folder / extracted_format
            folder.mkdir()
            fixtures.write_folder_fixture(folder, extracted_format=extracted_format)
            self.run_main(folder=folder)
            paths[extracted_format] = (folder / "verified" / "abc123_verified.csv").read_bytes()
        strip = lambda data: data[3:] if data.startswith(b"\xef\xbb\xbf") else data
        self.assertEqual(strip(paths["csv"]), strip(paths["parquet"]))      # byte for byte, apart from the mark


class SeveralMd5sTest(FolderEndToEndCase):
    def test_a_mix_of_formats_in_one_run(self):
        fixtures.write_folder_fixture(self.folder, prefix="aaa111", extracted_format="csv")
        fixtures.write_folder_fixture(self.folder, prefix="bbb222", extracted_format="parquet")
        fixtures.write_folder_fixture(self.folder, prefix="ccc333", extracted_format="csv")
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        for prefix in ("aaa111", "bbb222", "ccc333"):
            rows = self.verified("%s_verified.csv" % prefix)
            self.assertEqual(len(rows) - 1, len(fixtures.CASES), prefix)
            # same verdicts as the golden file, whatever the format (the golden rows carry abc123 file names)
            golden = read_rows(HERE / "expected_verified.csv")
            self.assertEqual([r[-5:] for r in rows], [r[-5:] for r in golden], prefix)
        self.assertEqual(out.strip().splitlines()[-1], "total: 3 checked, 0 skipped, 0 failed")


class BothFormatsTest(FolderEndToEndCase):
    def test_the_newer_file_decides_which_rows_are_checked(self):
        fixtures.write_folder_fixture(self.folder)                      # extracted/abc123.csv with 22 rows
        fixtures.write_extracted_parquet(self.folder / "extracted" / "abc123.parquet",
                                         [fixtures.extracted_row(c) for c in fixtures.CASES[:3]])
        csv_path, parquet_path = self.folder / "extracted" / "abc123.csv", self.folder / "extracted" / "abc123.parquet"
        os.utime(str(csv_path), ns=(BASE_NS, BASE_NS))
        os.utime(str(parquet_path), ns=(BASE_NS + 10 ** 9, BASE_NS + 10 ** 9))
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("extracted/abc123.parquet: 3 rows checked", out)
        self.assertIn("using extracted/abc123.parquet (newer than extracted/abc123.csv)", err)
        self.assertEqual(len(self.verified()) - 1, 3)
        # now the csv is the newer one
        os.utime(str(csv_path), ns=(BASE_NS + 5 * 10 ** 9, BASE_NS + 5 * 10 ** 9))
        code, out, err = self.run_main("--force")
        self.assertIn("extracted/abc123.csv: 22 rows checked", out)
        self.assertEqual(self.verified(), read_rows(HERE / "expected_verified.csv"))


class SecondRunTest(FolderEndToEndCase):
    def test_a_second_run_skips_and_force_rechecks(self):
        for extracted_format in ("csv", "parquet"):
            folder = self.folder / extracted_format
            folder.mkdir()
            fixtures.write_folder_fixture(folder, extracted_format=extracted_format)
            code, out, err = self.run_main(folder=folder)
            self.assertEqual((code, out.strip().splitlines()[-1]), (0, "total: 1 checked, 0 skipped, 0 failed"))
            first = tree(folder / "verified")
            code, out, err = self.run_main(folder=folder)
            self.assertEqual(code, 0, err)
            self.assertIn("skipped, verified/abc123_verified.csv is newer than its inputs and this script", out)
            self.assertEqual(out.strip().splitlines()[-1], "total: 0 checked, 1 skipped, 0 failed")
            self.assertEqual(tree(folder / "verified"), first)                  # untouched
            code, out, err = self.run_main("--force", folder=folder)
            self.assertEqual(out.strip().splitlines()[-1], "total: 1 checked, 0 skipped, 0 failed")
            self.assertEqual(self.verified(folder=folder), read_rows(HERE / "expected_verified.csv"))

    def test_replacing_an_input_after_the_first_run_rechecks_it(self):
        fixtures.write_folder_fixture(self.folder, extracted_format="parquet")
        self.run_main()
        new = [fixtures.extracted_row(c) for c in fixtures.CASES[:2]]
        path = self.folder / "extracted" / "abc123.parquet"
        fixtures.write_extracted_parquet(path, new)
        ns = os.stat(str(self.folder / "verified" / "abc123_verified.csv")).st_mtime_ns + 5 * 10 ** 9
        os.utime(str(path), ns=(ns, ns))
        code, out, _ = self.run_main()
        self.assertIn("2 rows checked", out)
        self.assertEqual(len(self.verified()) - 1, 2)


class FlatLayoutStillWorksTest(FolderEndToEndCase):
    def test_the_flat_layout_gives_the_golden_result(self):
        fixtures.write_fixture(self.folder)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(read_rows(self.folder / "abc123_verified.csv"), read_rows(HERE / "expected_verified.csv"))
        self.assertIn("-> abc123_verified.csv", out)


if __name__ == "__main__":
    unittest.main()

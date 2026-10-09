"""The debug input profile states the layout and the extracted file used, with its format."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures


class DebugLayoutCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def run_debug(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(self.folder), "--debug"] + list(extra))
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        block = text[text.index("extraction_qc debug"):].split("\ntotal:")[0].splitlines()
        return block, err.getvalue()

    def report(self, name):
        return (self.folder / "verified" / name).read_text(encoding="utf-8").splitlines()


class ProfileTest(DebugLayoutCase):
    def check_limits(self, block):
        self.assertLessEqual(len(block), qc.PASTE_MAX_LINES)
        self.assertLessEqual(max(len(line) for line in block), qc.PASTE_MAX_WIDTH)

    def test_a_folder_layout_parquet_run(self):
        fixtures.write_folder_fixture(self.folder, extracted_format="parquet")
        block, _ = self.run_debug()
        self.assertIn("layout: folder; extracted file: extracted/<md5>.parquet (parquet)", block)
        line = [l for l in block if l.startswith("parquet:")][0]
        self.assertTrue(line.startswith("parquet: rows=22, row_groups=4 | SourceLine order:"), line)
        self.assertFalse(any(l.startswith("csv:") for l in block))
        self.check_limits(block)

    def test_a_folder_layout_csv_run(self):
        fixtures.write_folder_fixture(self.folder)
        block, _ = self.run_debug()
        self.assertIn("layout: folder; extracted file: extracted/<md5>.csv (csv)", block)
        line = [l for l in block if l.startswith("csv:")][0]
        self.assertTrue(line.startswith("csv: rows=22 | SourceLine order:"), line)
        self.assertNotIn("row_groups", line)
        self.check_limits(block)

    def test_a_flat_run(self):
        fixtures.write_fixture(self.folder)
        block, _ = self.run_debug()
        self.assertIn("layout: flat; extracted file: <md5>_extracted.csv (csv)", block)
        self.assertTrue(any(l.startswith("csv: rows=22") for l in block))
        self.check_limits(block)

    def test_the_layout_line_sits_in_the_input_section(self):
        fixtures.write_folder_fixture(self.folder)
        block, _ = self.run_debug()
        start = block.index("-- input")
        self.assertEqual(block[start + 1], "layout: folder; extracted file: extracted/<md5>.csv (csv)")
        self.assertTrue(block[start + 2].startswith("relevant: rows="))

    def test_with_both_formats_the_file_used_is_named(self):
        fixtures.write_folder_fixture(self.folder)
        fixtures.write_extracted_parquet(self.folder / "extracted" / "abc123.parquet",
                                         [fixtures.extracted_row(c) for c in fixtures.CASES], row_group_size=11)
        ns = 1700000000 * 10 ** 9
        os.utime(str(self.folder / "extracted" / "abc123.csv"), ns=(ns, ns))
        os.utime(str(self.folder / "extracted" / "abc123.parquet"), ns=(ns + 10 ** 9, ns + 10 ** 9))
        block, err = self.run_debug()
        self.assertIn("layout: folder; extracted file: extracted/<md5>.parquet (parquet)", block)
        self.assertIn("parquet: rows=22, row_groups=2", " ".join(block))
        self.assertIn("using extracted/abc123.parquet (newer than extracted/abc123.csv)", err)

    def test_a_trial_run_shows_its_scope_after_the_row_groups(self):
        fixtures.write_folder_fixture(self.folder, extracted_format="parquet")
        block, _ = self.run_debug("--limit", "5")
        line = [l for l in block if l.startswith("parquet:")][0]
        self.assertTrue(line.startswith("parquet: rows=5, row_groups=4 (--limit 5) | SourceLine order:"), line)

    def test_the_full_report_has_the_same_lines(self):
        fixtures.write_folder_fixture(self.folder, extracted_format="parquet")
        self.run_debug()
        report = self.report("abc123_debug_report.txt")
        self.assertIn("layout: folder; extracted file: extracted/abc123.parquet (parquet)", report)
        self.assertTrue(any(l.startswith("parquet: rows=22, row_groups=4") for l in report))

    def test_the_block_is_still_within_its_limits_whatever_the_md5_is_called(self):
        long_name = "x" * 100
        fixtures.write_folder_fixture(self.folder, prefix=long_name, extracted_format="parquet")
        block, _ = self.run_debug()
        self.check_limits(block)
        self.assertIn("layout: folder; extracted file: extracted/<md5>.parquet (parquet)", block)
        self.assertNotIn("xxxx", "\n".join(block))

    def test_the_pasted_block_never_holds_the_md5_but_the_report_file_does(self):
        for name, writer in (("folder_csv", lambda f: fixtures.write_folder_fixture(f)),
                             ("folder_parquet", lambda f: fixtures.write_folder_fixture(f, extracted_format="parquet")),
                             ("flat", lambda f: fixtures.write_fixture(f))):
            work = self.folder / name
            work.mkdir()
            writer(work)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = qc.main(["--folder", str(work), "--debug", "--show-values"])
            self.assertEqual(code, 0, err.getvalue())
            text = out.getvalue()
            block = text[text.index("extraction_qc debug"):].split("\ntotal:")[0]
            self.assertNotIn("abc123", block, name)
            report = ((work / "verified") if name != "flat" else work) / "abc123_debug_report.txt"
            self.assertIn("abc123", report.read_text(encoding="utf-8"), name)             # the local file keeps it

    def test_a_collector_without_layout_information_still_reports(self):
        collector = qc.DebugCollector()
        summary = qc.Summary()
        lines = collector.report(summary, "x.csv", paste=True)
        self.assertFalse(any(l.startswith("layout:") for l in lines))
        self.assertTrue(any(l.startswith("csv: rows=0") for l in lines))


class ProfileOnParquetOnlyTest(DebugLayoutCase):
    def test_a_parquet_extracted_file_reports_its_order_flags(self):
        rows = [fixtures.extracted_row(c) for c in fixtures.CASES if c.src.isdigit() and c.rel.isdigit()]
        rows.sort(key=lambda r: (int(r[3]), int(r[4])))
        fixtures.write_folder_fixture(self.folder, cases=[c for c in fixtures.CASES if c.src.isdigit() and c.rel.isdigit()])
        (self.folder / "extracted" / "abc123.csv").unlink()
        fixtures.write_extracted_parquet(self.folder / "extracted" / "abc123.parquet", rows, row_group_size=5)
        block, _ = self.run_debug()
        line = [l for l in block if l.startswith("parquet:")][0]
        self.assertIn("SourceLine order: ascending", line)


if __name__ == "__main__":
    unittest.main()

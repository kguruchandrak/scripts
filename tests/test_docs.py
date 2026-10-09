import contextlib
import datetime
import decimal
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "EXTRACTION_QC.md").read_text(encoding="utf-8")
SOURCE = (ROOT / "extraction_qc.py").read_text(encoding="utf-8")


def documented_reason_codes():
    section = DOC.split("## Reason codes", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `([A-Z_]+)` \|", section, re.M)


class DocumentedCommandTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        fixtures.write_fixture(self.folder)

    def test_plain_command_run_from_the_folder_with_the_files(self):
        done = subprocess.run([sys.executable, str(ROOT / "extraction_qc.py")], cwd=str(self.folder),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.folder / "abc123_verified.csv").is_file())
        self.assertIn("%d rows checked" % len(fixtures.CASES), done.stdout)

    def test_every_switch_in_the_doc_exists(self):
        help_text = qc.build_parser().format_help()
        for switch in re.findall(r"`(--[a-z-]+)", DOC):
            self.assertIn(switch, help_text, switch)

    def test_every_switch_of_the_program_is_in_the_doc(self):
        help_text = qc.build_parser().format_help()
        for switch in sorted(set(re.findall(r"(--[a-z][a-z-]+)", help_text))):
            if switch != "--help":
                self.assertIn("`%s" % switch, DOC, switch)

    def test_the_documented_exit_codes_are_the_real_ones(self):
        self.assertIn("0: every file was checked", DOC)
        self.assertIn("1: at least one file could not be checked", DOC)
        self.assertIn("3: everything was checked or skipped but some rows are `Wrong`", DOC)
        self.assertIn("2: a", DOC)

    def test_the_doc_names_the_trial_file(self):
        self.assertIn("<md5>_verified_trial.csv", DOC)

    def test_every_output_column_in_the_doc_exists(self):
        for column in re.findall(r"^\| `([A-Za-z]+Verification|[A-Za-z]+Reason)` \|", DOC, re.M):
            self.assertIn(column, qc.OUTPUT_COLUMNS)
        self.assertEqual(len(re.findall(r"^\| `[A-Za-z]+(?:Verification|Reason)` \|", DOC, re.M)), 5)


class DebugDocumentationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        fixtures.write_fixture(self.folder)
        self.done = subprocess.run(
            [sys.executable, str(ROOT / "extraction_qc.py"), "--debug", "--limit", "2000"], cwd=str(self.folder),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        text = self.done.stdout[self.done.stdout.index("extraction_qc debug"):]
        self.block = text.split("\ntotal:")[0].splitlines()   # the block ends before the run's total line

    def test_the_documented_command_runs_and_writes_the_report(self):
        self.assertEqual(self.done.returncode, 0, self.done.stderr)
        self.assertTrue((self.folder / "abc123_debug_report_trial.txt").is_file())   # --limit makes it a trial run
        self.assertFalse((self.folder / "abc123_debug_report.txt").exists())
        self.assertTrue((self.folder / "abc123_verified_trial.csv").is_file())
        self.assertFalse((self.folder / "abc123_verified.csv").exists())

    def test_every_documented_section_is_in_the_paste_block(self):
        sections = re.findall(r"^\| `(-- [^`]+)` \|", DOC, re.M)
        self.assertGreaterEqual(len(sections), 7)
        for section in sections:
            self.assertTrue(any(line.startswith(section) for line in self.block), section)

    def test_every_section_of_the_block_is_documented(self):
        sections = re.findall(r"^\| `(-- [^`]+)` \|", DOC, re.M)
        for line in self.block:
            if line.startswith("-- "):
                self.assertTrue(any(line.startswith(s) for s in sections), line)

    def test_the_documented_limits_are_the_real_ones(self):
        self.assertIn("at most %d lines of at most %d characters" % (qc.PASTE_MAX_LINES, qc.PASTE_MAX_WIDTH), DOC)
        self.assertLessEqual(len(self.block), qc.PASTE_MAX_LINES)
        self.assertLessEqual(max(len(line) for line in self.block), qc.PASTE_MAX_WIDTH)
        self.assertIn("up to %d failure" % qc.TRACES_PER_CODE, DOC)
        self.assertIn("at most %d per file" % qc.TRACES_PER_LANE, DOC)
        self.assertIn("the first %d documents" % qc.SKELETON_SAMPLE_DOCS, DOC)
        self.assertIn("more than %d keys" % qc.SKELETON_MAX_KEYS, DOC)

    def test_the_documented_masking_examples_are_right(self):
        self.assertIn("`2024-01-25` shows as `9999-99-99`, `alice` as `aaaaa`, `True` as `Aaaa`", DOC)
        for before, after in (("2024-01-25", "9999-99-99"), ("alice", "aaaaa"), ("True", "Aaaa")):
            self.assertEqual(qc.mask(before), after)

    def test_the_block_holds_no_real_value(self):
        text = "\n".join(self.block)
        for secret in ("alice", "carol", "12345", "2024-01-25"):
            self.assertNotIn(secret, text)

    def test_a_row_list_command_from_the_doc_runs(self):
        done = subprocess.run(
            [sys.executable, str(ROOT / "extraction_qc.py"), "--debug", "--rows", "17,203"], cwd=str(self.folder),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("1 rows checked", done.stdout)   # only row 17 exists in the fixture


def missing_switches(doc):
    """The switches of the program that the document does not mention."""
    help_text = qc.build_parser().format_help()
    return [s for s in sorted(set(re.findall(r"(--[a-z][a-z-]+)", help_text)))
            if s != "--help" and "`%s" % s not in doc]


def missing_folders(doc):
    return [name for name in ("extracted/", "original/", "relevant/", "verified/") if name not in doc]


class LayoutDocumentationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_every_switch_and_folder_is_documented(self):
        self.assertEqual(missing_switches(DOC), [])
        self.assertEqual(missing_folders(DOC), [])
        self.assertIn("`--force", DOC)

    def test_the_checks_fail_when_a_documented_switch_or_folder_is_removed(self):
        self.assertEqual(missing_switches(DOC.replace("`--force", "`--xforce")), ["--force"])
        self.assertEqual(missing_switches(DOC.replace("`--check-same-document", "`--other")), ["--check-same-document"])
        self.assertEqual(missing_folders(DOC.replace("verified/", "output/")), ["verified/"])
        self.assertEqual(sorted(missing_folders(DOC.replace("extracted/", "in/").replace("relevant/", "rel/"))),
                         ["extracted/", "relevant/"])

    def test_the_documented_file_names_are_the_ones_the_code_builds(self):
        folder = qc.Triple(Path("."), "<md5>", qc.FOLDER)
        flat = qc.Triple(Path("."), "<md5>")
        for text in (folder.show(folder.extracted), folder.show(folder.original), folder.show(folder.relevant),
                     folder.show(folder.verified), flat.show(flat.extracted), flat.show(flat.original),
                     flat.show(flat.relevant), flat.show(flat.verified)):
            self.assertIn(text, DOC, text)
        for name in (folder.trial.name, folder.debug_report.name, folder.trial_debug_report.name,
                     folder.verified.name + ".partial"):
            self.assertIn(name, DOC, name)

    def test_the_documented_skip_and_total_lines_are_the_real_ones(self):
        fixtures.write_folder_fixture(self.folder)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            qc.main(["--folder", str(self.folder)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            qc.main(["--folder", str(self.folder)])
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, lines)
        self.assertIn(lines[0], DOC)                  # the skip line
        self.assertIn(lines[1], DOC)                  # the total line

    def test_the_documented_skip_rules(self):
        for phrase in ("is **newer** than", "and than `extraction_qc.py` itself", "never skipped", "`.partial`",
                       "The flat layout always rechecks", "Use `--force` after changing switches",
                       "`--check-same-document`", "With `--fail-on-wrong` it still counts",
                       "equal times are checked again", "If `extraction_qc.py` was changed after the verified file"):
            self.assertIn(phrase, DOC, phrase)

    def test_the_documented_parquet_conversions_are_the_real_ones(self):
        examples = (("`12345.5`", 12345.5, "12345.5"), ("`1e+22`", 1e22, "1e+22"), ("`nan`", float("nan"), "nan"),
                    ("`2024-01-25`", datetime.date(2024, 1, 25), "2024-01-25"),
                    ("`2024-01-25T10:30:00`", datetime.datetime(2024, 1, 25, 10, 30), "2024-01-25T10:30:00"),
                    ("`[1,2]`", [1, 2], "[1,2]"), ("`{\"a\":1}`", {"a": 1}, '{"a":1}'))
        for shown, value, expected in examples:
            self.assertIn(shown, DOC, shown)
            self.assertEqual(qc.cell_text(value), expected)
        self.assertEqual(qc.cell_text(decimal.Decimal("1.50")), "1.50")
        self.assertIn("`1.50`", DOC)
        self.assertIn("`true` or `false`", DOC)

    def test_the_documented_typed_value_message_is_the_real_one(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        path = self.folder / "typed.parquet"
        pq.write_table(pa.table({"SourceLine": ["1"], "RelevancyParquetLine": ["1"], "SourceElementPath": ["$.a"],
                                 "Value": pa.array([1.5])}), str(path))
        with self.assertRaises(qc.InputError) as ctx:
            qc.ParquetExtractedInput.open(path, "extracted/abc123.parquet")
        self.assertIn("extracted/abc123.parquet: column 'Value' has type double, not text", str(ctx.exception))
        self.assertIn("extracted/abc123.parquet: column 'Value' has type double, not text", DOC)

    def test_the_documented_parquet_time_and_map_conversions_are_the_real_ones(self):
        import pyarrow as pa
        zones = ("UTC", "America/New_York", "+05:30", "Mars/Olympus_Mons")
        for zone in zones:                      # a zone name only labels the instant, which is stored in UTC
            array = pa.array([1706178600], pa.timestamp("s", tz=zone))
            self.assertEqual(qc.column_converter(array.type)(array), ["2024-01-25T10:30:00+00:00"], zone)
        self.assertIn("`2024-01-25T10:30:00+00:00`", DOC)
        array = pa.array([37815250000], pa.time64("us"))
        self.assertEqual(qc.column_converter(array.type)(array), ["10:30:15.250000"])
        self.assertIn("`10:30:15.250000`", DOC)
        durations = pa.array([3900 * 10 ** 6, (86400 + 1) * 10 ** 6 + 500000, -10 ** 6], pa.duration("us"))
        self.assertEqual(qc.column_converter(durations.type)(durations), ["1:05:00", "1 day, 0:00:01.500000", "-0:00:01"])
        for shown in ("`1:05:00`", "`2 days, 0:00:01.500000`", "a leading `-` when negative"):
            self.assertIn(shown, DOC, shown)
        maps = pa.array([[("k", 1), ("j", 2)]], pa.map_(pa.string(), pa.int64()))
        self.assertEqual(qc.column_converter(maps.type)(maps), ['{"k":1,"j":2}'])
        self.assertIn('`{"k":1,"j":2}`', DOC)
        self.assertIn("a list of `[key, value]` pairs", DOC)

    def test_the_documented_encodings(self):
        for phrase in ("UTF-16 or UTF-32 with a byte order mark", "UTF-8 with a few bad bytes",
                       "UTF-16 without a byte order mark", "`--csv-encoding utf-16-le`"):
            self.assertIn(phrase, DOC, phrase)

    def test_the_documented_case_clash_rule(self):
        self.assertIn("differ only in letter case", DOC)
        self.assertIn("extracted/abc.csv", DOC)

    def test_the_documented_layout_selection(self):
        for phrase in ("whenever an `extracted/` folder exists", "ignored, and a note says so", "Flat layout"):
            self.assertIn(phrase, DOC, phrase)


class ReasonCodeDocumentationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        fixtures.write_fixture(self.folder)

    def reasons_seen(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            qc.main(["--folder", str(self.folder)] + list(extra))
        text = (self.folder / "abc123_verified.csv").read_text(encoding="utf-8")
        return " ".join(re.findall(r"\b[A-Z][A-Z_]{4,}", text))

    def test_the_doc_lists_the_codes(self):
        self.assertEqual(
            sorted(documented_reason_codes()),
            sorted(["LINE_OUT_OF_RANGE", "INVALID_LINE", "UNSUPPORTED_PATH", "INVALID_JSON", "PATH_NOT_FOUND",
                    "VALUE_MISMATCH", "FORMAT_CHANGED", "DOCUMENT_MISMATCH", "SKIPPED"]))

    def test_every_documented_code_is_in_the_implementation(self):
        for code in documented_reason_codes():
            self.assertIn('"%s' % code, SOURCE.replace("'", '"'), code)

    def test_every_documented_code_appears_in_a_fixture_result(self):
        seen = " ".join([self.reasons_seen(), self.reasons_seen("--skip-original-on-relevant-failure"),
                         self.reasons_seen("--check-same-document")])
        for code in documented_reason_codes():
            self.assertIn(code, seen, code)

    def test_the_hint_is_documented_and_appears(self):
        self.assertIn("FOUND_AT_LINE_<n>", DOC)
        self.assertIn("FOUND_AT_LINE_", self.reasons_seen())


if __name__ == "__main__":
    unittest.main()

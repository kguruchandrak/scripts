import contextlib
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
        self.assertIn("3: everything was checked but some rows are `Wrong`", DOC)
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
        self.block = self.done.stdout[self.done.stdout.index("extraction_qc debug"):].splitlines()

    def test_the_documented_command_runs_and_writes_the_report(self):
        self.assertEqual(self.done.returncode, 0, self.done.stderr)
        self.assertTrue((self.folder / "abc123_debug_report.txt").is_file())
        self.assertTrue((self.folder / "abc123_verified_trial.csv").is_file())   # --limit makes it a trial run
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

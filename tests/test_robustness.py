"""Input and run robustness: odd CSV files, failing files, locked outputs, console encodings, trial runs."""
import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures
from tests.fixtures import S, doc


def row(src, rel, path, value, name="c"):
    return fixtures.extracted_row(fixtures.Case(name, str(src), str(rel), path, value, "", ""))


class RobustnessCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def write(self, docs, rows, prefix="abc123", encoding="utf-8", header=None):
        fixtures.write_parquet(self.folder / ("%s_original.parquet" % prefix), docs)
        fixtures.write_parquet(self.folder / ("%s_relevant.parquet" % prefix), docs)
        with open(str(self.folder / ("%s_extracted.csv" % prefix)), "w", newline="", encoding=encoding) as handle:
            writer = csv.writer(handle)
            writer.writerow(fixtures.HEADER if header is None else header)
            writer.writerows(rows)

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def verified(self, name="abc123_verified.csv"):
        with open(str(self.folder / name), newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        return rows[0], rows[1:]


class LongFieldTest(RobustnessCase):
    def test_a_value_longer_than_the_default_csv_limit(self):
        big = "x" * 300000
        self.write([doc(big=S(big), small=S("s"))], [row(1, 1, "$.Item.big", big), row(1, 1, "$.Item.small", "s"),
                                                      row(1, 1, "$.Item.small", big)])
        code, out, err = self.run_main()
        header, rows = self.verified()
        col = {n: i for i, n in enumerate(header)}
        self.assertEqual(code, 0, err)
        self.assertEqual([r[col["OverallVerification"]] for r in rows], ["Correct", "Correct", "Wrong"])
        self.assertEqual(rows[0][col["Value"]], big)
        self.assertLess(len(rows[2][col["OriginalFileReason"]]), 400)    # the reason clips the long value

    def test_the_field_limit_is_raised_not_just_for_this_run(self):
        self.assertGreater(csv.field_size_limit(), 10 ** 7)


class EncodingTest(RobustnessCase):
    DOCS = [doc(name=S("café"), plain=S("p"))]

    def test_utf8_is_detected(self):
        self.write(self.DOCS, [row(1, 1, "$.Item.name", "café")])
        triple = qc.find_triples(self.folder)[0]
        qc.prepare_triple(triple)
        self.assertEqual(triple.encoding, "utf-8")
        self.assertEqual(triple.encoding_note, "")

    def test_utf8_with_a_byte_order_mark_is_detected(self):
        self.write(self.DOCS, [row(1, 1, "$.Item.name", "café")], encoding="utf-8-sig")
        triple = qc.find_triples(self.folder)[0]
        qc.prepare_triple(triple)
        self.assertEqual(triple.encoding, "utf-8-sig")
        self.assertEqual(triple.encoding_note, "")

    def test_a_windows_1252_csv_from_excel(self):
        self.write(self.DOCS, [row(1, 1, "$.Item.name", "café"), row(1, 1, "$.Item.plain", "p")], encoding="cp1252")
        code, out, err = self.run_main()
        header, rows = self.verified()
        col = {n: i for i, n in enumerate(header)}
        self.assertEqual(code, 0, err)
        self.assertIn("abc123_extracted.csv is not UTF-8; it was read as cp1252", err)
        self.assertEqual([r[col["OverallVerification"]] for r in rows], ["Correct", "Correct"])
        self.assertEqual(rows[0][col["Value"]], "café")
        self.assertTrue((self.folder / "abc123_verified.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_the_mid_file_decoding_error_of_the_old_version_is_gone(self):
        # the first rows are pure ASCII: a header-only or first-block check would have passed as UTF-8
        rows = [row(1, 1, "$.Item.plain", "p")] * 2000 + [row(1, 1, "$.Item.name", "café")]
        self.write(self.DOCS, rows, encoding="cp1252")
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.verified()[1][-1][-1], "Correct")

    def test_explicit_encoding(self):
        self.write(self.DOCS, [row(1, 1, "$.Item.name", "café")], encoding="cp1252")
        code, _, err = self.run_main("--csv-encoding", "cp1252")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.verified()[1][0][-1], "Correct")

    def test_explicit_encoding_that_cannot_decode_the_file(self):
        self.write(self.DOCS, [row(1, 1, "$.Item.name", "café")], encoding="cp1252")
        code, _, err = self.run_main("--csv-encoding", "utf-8")
        self.assertEqual(code, 1)
        self.assertIn("cannot be decoded as utf-8", err)
        self.assertFalse((self.folder / "abc123_verified.csv").exists())

    def test_unknown_encoding_name(self):
        self.write(self.DOCS, [row(1, 1, "$.Item.name", "x")])
        code, _, err = self.run_main("--csv-encoding", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("unknown CSV encoding 'nonsense'", err)

    def test_bytes_windows_1252_cannot_decode_fall_back_to_latin_1(self):
        self.write(self.DOCS, [row(1, 1, "$.Item.plain", "p\x81")], encoding="latin-1")
        triple = qc.find_triples(self.folder)[0]
        qc.prepare_triple(triple)
        self.assertEqual(triple.encoding, "latin-1")
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)

    def test_the_output_is_plain_utf8_only_when_the_input_was(self):
        for encoding, bom in (("utf-8", False), ("utf-8-sig", True), ("cp1252", True)):
            self.write(self.DOCS, [row(1, 1, "$.Item.name", "café")], encoding=encoding)
            self.run_main()
            data = (self.folder / "abc123_verified.csv").read_bytes()
            self.assertEqual(data.startswith(b"\xef\xbb\xbf"), bom, encoding)
            self.assertIn("café".encode("utf-8"), data)

    def test_text_the_input_encoding_could_not_hold_is_still_written(self):
        # the reason quotes the document's value, which a Windows-1252 file could not store
        self.write([doc(name=S("中文"))], [row(1, 1, "$.Item.name", "café")], encoding="cp1252")
        code, _, err = self.run_main()
        header, rows = self.verified()
        self.assertEqual(code, 0, err)
        self.assertIn("中文", rows[0][header.index("OriginalFileReason")])


class HeaderAndBlankLineTest(RobustnessCase):
    def test_a_duplicated_required_column_stops_the_run(self):
        header = fixtures.HEADER + ["Value"]
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1") + ["other"]], header=header)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Value", err)
        self.assertIn("more than once", err)
        self.assertFalse((self.folder / "abc123_verified.csv").exists())

    def test_a_duplicated_column_that_is_not_required_is_allowed(self):
        header = fixtures.HEADER + ["EntityRole"]
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1") + ["again"]], header=header)
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.verified()[0], header + qc.OUTPUT_COLUMNS)

    def test_blank_lines_are_not_counted_or_written(self):
        docs = [doc(a=S("1"), b=S("2"), c=S("3"))]
        rows = [row(1, 1, "$.Item.a", "1", "first"), row(1, 1, "$.Item.b", "2", "second"),
                row(1, 1, "$.Item.c", "3", "third")]
        self.write(docs, rows)
        path = self.folder / "abc123_extracted.csv"
        lines = path.read_text(encoding="utf-8").splitlines(True)
        path.write_text("".join(lines[:2] + ["\n", "\n"] + lines[2:]), encoding="utf-8")      # blank lines inside
        code, out, err = self.run_main("--rows", "2")
        header, rows_out = self.verified("abc123_verified_trial.csv")
        self.assertEqual([r[header.index("CanonicalField")] for r in rows_out], ["Canon_second"])
        self.run_main()
        header, rows_out = self.verified()
        self.assertEqual(len(rows_out), 3)

    def test_the_help_says_blank_lines_are_not_counted(self):
        self.assertIn("Blank lines", qc.build_parser().format_help().replace("\n", " ").replace("  ", " "))


class FailureIsolationTest(RobustnessCase):
    def two_files(self):
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1")], prefix="aaa111")
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1")], prefix="bbb222")

    def break_first_file(self):
        real = qc.run_lane

        def lane(label, csv_input, *args, **kwargs):
            if csv_input.path.name.startswith("aaa111"):
                raise RuntimeError("boom")
            return real(label, csv_input, *args, **kwargs)

        qc.run_lane = lane
        self.addCleanup(setattr, qc, "run_lane", real)

    def test_an_unexpected_error_in_one_file_does_not_stop_the_next(self):
        self.two_files()
        self.break_first_file()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("aaa111_extracted.csv: unexpected failure (RuntimeError: boom)", err)
        self.assertNotIn("Traceback", err)
        self.assertTrue((self.folder / "bbb222_verified.csv").is_file())
        self.assertFalse((self.folder / "aaa111_verified.csv").exists())
        self.assertEqual(sorted(p.name for p in self.folder.glob("*.partial")), [])
        self.assertIn("bbb222_extracted.csv: 1 rows checked", out)

    def test_the_traceback_is_printed_with_debug(self):
        self.two_files()
        self.break_first_file()
        _, _, err = self.run_main("--debug")
        self.assertIn("Traceback", err)
        self.assertIn("RuntimeError: boom", err)

    def test_an_input_problem_in_one_file_does_not_stop_the_next(self):
        self.two_files()
        (self.folder / "aaa111_relevant.parquet").unlink()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("aaa111_relevant.parquet", err)
        self.assertTrue((self.folder / "bbb222_verified.csv").is_file())


class ExitCodeTest(RobustnessCase):
    def test_exit_codes(self):
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1")])
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.run_main("--fail-on-wrong")[0], 0)
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "wrong")])
        self.assertEqual(self.run_main()[0], 0)                 # wrong rows alone do not fail the run
        self.assertEqual(self.run_main("--fail-on-wrong")[0], 3)
        (self.folder / "abc123_relevant.parquet").unlink()
        self.assertEqual(self.run_main("--fail-on-wrong")[0], 1)    # a file that cannot be checked wins

    def test_the_help_documents_the_switch(self):
        self.assertIn("--fail-on-wrong", qc.build_parser().format_help())


class ConsoleEncodingTest(RobustnessCase):
    def test_characters_the_console_cannot_show_do_not_stop_the_run(self):
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1")], prefix="数据")
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = qc.main(["--folder", str(self.folder), "--debug"])
        self.assertEqual(code, 0)
        data = stdout.buffer.getvalue()
        self.assertIn(b"\\u6570", data)                      # shown as an escape, not a crash
        self.assertIn(b"extraction_qc debug", data)          # the paste block was printed after it

    def test_an_error_message_with_such_characters_is_printed_too(self):
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1")], prefix="数据")
        (self.folder / "数据_relevant.parquet").unlink()
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = qc.main(["--folder", str(self.folder)])
        self.assertEqual(code, 1)
        self.assertIn(b"\\u6570", stderr.buffer.getvalue())


class ProtectFinishedWorkTest(RobustnessCase):
    def setUp(self):
        super().setUp()
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1")])

    def test_an_output_that_cannot_be_written_stops_the_run_before_any_row(self):
        (self.folder / "abc123_verified.csv").mkdir()            # cannot be opened for writing
        calls = []
        real = qc.run_lane
        qc.run_lane = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        self.addCleanup(setattr, qc, "run_lane", real)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("cannot write abc123_verified.csv", err)
        self.assertIn("open in another program", err)
        self.assertEqual(calls, [])

    def test_the_check_does_not_create_a_missing_output(self):
        qc.check_output_writable(self.folder / "new_verified.csv")
        self.assertFalse((self.folder / "new_verified.csv").exists())

    def test_the_check_does_not_change_an_existing_output(self):
        path = self.folder / "old_verified.csv"
        path.write_bytes(b"keep me")
        qc.check_output_writable(path)
        self.assertEqual(path.read_bytes(), b"keep me")

    def test_a_replace_that_fails_at_the_end_keeps_the_results(self):
        real = os.replace

        def locked(src, dst):
            raise PermissionError("locked by another process")

        os.replace = locked
        self.addCleanup(setattr, os, "replace", real)
        code, _, err = self.run_main()
        os.replace = real
        partial = self.folder / "abc123_verified.csv.partial"
        self.assertEqual(code, 1)
        self.assertIn("abc123_verified.csv.partial", err)
        self.assertIn("close the program", err)
        self.assertTrue(partial.is_file())
        with open(str(partial), newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(len(rows), 2)                                  # header and the one row
        self.assertEqual(rows[1][-1], "Correct")
        self.assertFalse((self.folder / "abc123_verified.csv").exists())

    def test_a_trial_run_never_touches_the_full_result(self):
        fixtures.write_fixture(self.folder)
        self.run_main()
        full = (self.folder / "abc123_verified.csv").read_bytes()
        self.run_main("--limit", "3")
        self.run_main("--rows", "5,9")
        self.assertEqual((self.folder / "abc123_verified.csv").read_bytes(), full)
        header, rows = self.verified("abc123_verified_trial.csv")
        self.assertEqual(len(rows), 2)                                  # the last trial run: rows 5 and 9

    def test_the_summary_names_the_trial_file(self):
        fixtures.write_fixture(self.folder)
        _, out, _ = self.run_main("--limit", "3")
        self.assertIn("-> abc123_verified_trial.csv", out)
        _, out, _ = self.run_main()
        self.assertIn("-> abc123_verified.csv", out)

    def test_a_locked_trial_file_is_detected_too(self):
        (self.folder / "abc123_verified_trial.csv").mkdir()
        code, _, err = self.run_main("--limit", "1")
        self.assertEqual(code, 1)
        self.assertIn("cannot write abc123_verified_trial.csv", err)


class TempDirTest(RobustnessCase):
    def setUp(self):
        super().setUp()
        self.write([doc(a=S("1"))], [row(1, 1, "$.Item.a", "1")])

    def test_working_files_go_to_the_given_folder_and_are_removed(self):
        scratch = self.folder / "scratch"
        scratch.mkdir()
        seen = []
        real = tempfile.TemporaryDirectory

        def spy(*args, **kwargs):
            seen.append(kwargs.get("dir"))
            return real(*args, **kwargs)

        tempfile.TemporaryDirectory = spy
        self.addCleanup(setattr, tempfile, "TemporaryDirectory", real)
        code, _, err = self.run_main("--temp-dir", str(scratch))
        self.assertEqual(code, 0, err)
        self.assertEqual([str(Path(d)) for d in seen if d], [str(scratch)])
        self.assertEqual(list(scratch.iterdir()), [])

    def test_the_default_is_the_system_temporary_folder(self):
        seen = []
        real = tempfile.TemporaryDirectory
        tempfile.TemporaryDirectory = lambda *a, **k: (seen.append(k.get("dir")), real(*a, **k))[1]
        self.addCleanup(setattr, tempfile, "TemporaryDirectory", real)
        self.run_main()
        self.assertEqual(seen, [None])

    def test_a_folder_that_does_not_exist(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                qc.main(["--folder", str(self.folder), "--temp-dir", str(self.folder / "missing")])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("missing is not a folder", err.getvalue())
        self.assertFalse((self.folder / "abc123_verified.csv").exists())


if __name__ == "__main__":
    unittest.main()

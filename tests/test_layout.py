"""The folder layout (extracted/ original/ relevant/ verified/), the flat layout, and md5 discovery."""
import contextlib
import csv
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures

ROOT = Path(__file__).resolve().parent.parent


class LayoutCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def run_main(self, *extra, folder=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(folder or self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def rows(self, relative):
        with open(str(self.folder / relative), newline="", encoding="utf-8-sig") as handle:
            return list(csv.reader(handle))

    def verified_names(self):
        directory = self.folder / "verified"
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


class LayoutSelectionTest(LayoutCase):
    def test_the_folder_layout_is_used_when_extracted_exists(self):
        fixtures.write_folder_fixture(self.folder)
        found = qc.discover(self.folder)
        self.assertEqual(found.layout, "folder")
        self.assertEqual(found.notes, [])
        (triple,) = found.triples
        self.assertEqual(triple.prefix, "abc123")
        self.assertEqual(triple.layout, "folder")
        self.assertEqual(triple.extracted, self.folder / "extracted" / "abc123.csv")
        self.assertEqual(triple.original, self.folder / "original" / "abc123.parquet")
        self.assertEqual(triple.relevant, self.folder / "relevant" / "abc123.parquet")
        self.assertEqual(triple.verified, self.folder / "verified" / "abc123_verified.csv")
        self.assertEqual(triple.trial, self.folder / "verified" / "abc123_verified_trial.csv")
        self.assertEqual(triple.debug_report, self.folder / "verified" / "abc123_debug_report.txt")
        self.assertEqual(triple.trial_debug_report, self.folder / "verified" / "abc123_debug_report_trial.txt")
        self.assertEqual(triple.label, "extracted/abc123.csv")
        self.assertEqual(triple.pattern, "extracted/<md5>.csv")
        self.assertEqual(triple.show(triple.verified), "verified/abc123_verified.csv")

    def test_the_flat_layout_is_used_without_an_extracted_folder(self):
        fixtures.write_fixture(self.folder)
        found = qc.discover(self.folder)
        self.assertEqual(found.layout, "flat")
        (triple,) = found.triples
        self.assertEqual(triple.layout, "flat")
        self.assertEqual(triple.extracted, self.folder / "abc123_extracted.csv")
        self.assertEqual(triple.original, self.folder / "abc123_original.parquet")
        self.assertEqual(triple.verified, self.folder / "abc123_verified.csv")
        self.assertEqual(triple.debug_report, self.folder / "abc123_debug_report.txt")
        self.assertEqual(triple.trial_debug_report, self.folder / "abc123_debug_report_trial.txt")
        self.assertEqual(triple.label, "abc123_extracted.csv")
        self.assertEqual(triple.pattern, "<md5>_extracted.csv")

    def test_find_triples_still_returns_the_triples(self):
        fixtures.write_folder_fixture(self.folder, prefix="aaa111")
        self.assertEqual([t.prefix for t in qc.find_triples(self.folder)], ["aaa111"])

    def test_flat_files_beside_the_folders_are_ignored_with_a_note(self):
        fixtures.write_folder_fixture(self.folder, prefix="abc123")
        fixtures.write_fixture(self.folder, prefix="zzz999")
        found = qc.discover(self.folder)
        self.assertEqual(found.layout, "folder")
        self.assertEqual([t.prefix for t in found.triples], ["abc123"])
        self.assertEqual(len(found.notes), 1)
        self.assertIn("zzz999_extracted.csv", found.notes[0])
        self.assertIn("ignored", found.notes[0])
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("zzz999_extracted.csv", err)
        self.assertFalse((self.folder / "zzz999_verified.csv").exists())
        self.assertTrue((self.folder / "verified" / "abc123_verified.csv").is_file())

    def test_an_empty_extracted_folder_does_not_fall_back_to_flat_files(self):
        (self.folder / "extracted").mkdir()
        fixtures.write_fixture(self.folder, prefix="zzz999")
        found = qc.discover(self.folder)
        self.assertEqual((found.layout, found.triples), ("folder", []))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(".csv or .parquet", err)
        self.assertFalse((self.folder / "zzz999_verified.csv").exists())

    def test_no_files_at_all_in_the_flat_layout(self):
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("_extracted.csv", err)

    def test_folder_moves_the_whole_layout(self):
        deep = self.folder / "deep" / "er"
        deep.mkdir(parents=True)
        fixtures.write_folder_fixture(deep)
        code, _, err = self.run_main(folder=deep)
        self.assertEqual(code, 0, err)
        self.assertTrue((deep / "verified" / "abc123_verified.csv").is_file())
        self.assertFalse((self.folder / "verified").exists())

    def test_a_file_named_extracted_is_not_a_folder(self):
        (self.folder / "extracted").write_text("not a folder")
        fixtures.write_fixture(self.folder)
        self.assertEqual(qc.discover(self.folder).layout, "flat")


class Md5DiscoveryTest(LayoutCase):
    def test_several_md5s_are_processed_in_name_order(self):
        fixtures.write_folder_fixture(self.folder, prefix="bbb222")
        fixtures.write_folder_fixture(self.folder, prefix="aaa111")
        fixtures.write_folder_fixture(self.folder, prefix="Ccc333")
        self.assertEqual([t.prefix for t in qc.find_triples(self.folder)], ["Ccc333", "aaa111", "bbb222"])

    def test_a_missing_sibling_stops_only_that_md5(self):
        fixtures.write_folder_fixture(self.folder, prefix="aaa111")
        fixtures.write_folder_fixture(self.folder, prefix="bbb222")
        (self.folder / "relevant" / "aaa111.parquet").unlink()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("relevant/aaa111.parquet", err)          # forward slashes, relative to the working folder
        self.assertIn("extracted/aaa111.csv", err)
        self.assertEqual(self.verified_names(), ["bbb222_verified.csv"])
        self.assertIn("extracted/bbb222.csv: 22 rows checked -> verified/bbb222_verified.csv", out)

    def test_both_siblings_missing_are_both_named(self):
        fixtures.write_folder_fixture(self.folder)
        (self.folder / "relevant" / "abc123.parquet").unlink()
        (self.folder / "original" / "abc123.parquet").unlink()
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("original/abc123.parquet", err)
        self.assertIn("relevant/abc123.parquet", err)

    def test_originals_and_relevants_without_an_extracted_file_are_ignored(self):
        fixtures.write_folder_fixture(self.folder)
        shutil.copy(str(self.folder / "original" / "abc123.parquet"), str(self.folder / "original" / "zzz.parquet"))
        shutil.copy(str(self.folder / "relevant" / "abc123.parquet"), str(self.folder / "relevant" / "yyy.parquet"))
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual([t.prefix for t in qc.find_triples(self.folder)], ["abc123"])
        self.assertNotIn("zzz", err + out)
        self.assertEqual(self.verified_names(), ["abc123_verified.csv"])

    def test_stray_files_in_extracted_are_not_md5s(self):
        fixtures.write_folder_fixture(self.folder)
        extracted = self.folder / "extracted"
        for name in ("~$abc123.csv", ".hidden", ".hidden.csv", "notes.txt", "abc123.csv.bak", "abc123", "readme.md"):
            (extracted / name).write_text("x")
        (extracted / "subfolder").mkdir()
        (extracted / "subfolder.csv").mkdir()
        self.assertEqual([t.prefix for t in qc.find_triples(self.folder)], ["abc123"])
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)

    def test_any_other_csv_or_parquet_is_an_md5_and_is_reported_when_siblings_are_missing(self):
        fixtures.write_folder_fixture(self.folder)
        (self.folder / "extracted" / "copy of abc123.csv").write_text("x")
        self.assertEqual([t.prefix for t in qc.find_triples(self.folder)], ["abc123", "copy of abc123"])
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("original/copy of abc123.parquet", err)

    def test_the_md5_is_the_name_without_the_extension(self):
        fixtures.write_folder_fixture(self.folder, prefix="a.b-c_d")
        (triple,) = qc.find_triples(self.folder)
        self.assertEqual(triple.prefix, "a.b-c_d")
        self.assertEqual(triple.verified.name, "a.b-c_d_verified.csv")

    def test_extensions_are_read_in_any_case(self):
        fixtures.write_folder_fixture(self.folder)
        os.replace(str(self.folder / "extracted" / "abc123.csv"), str(self.folder / "extracted" / "abc123.CSV"))
        (triple,) = qc.find_triples(self.folder)
        self.assertEqual((triple.prefix, triple.extracted_format), ("abc123", "csv"))


class CaseClashTest(LayoutCase):
    """Two md5s that differ only in letter case would share their files and their verified file on Windows."""

    def clashing(self):
        fixtures.write_folder_fixture(self.folder, prefix="abc123", extracted_format="csv")
        fixtures.write_extracted_parquet(self.folder / "extracted" / "ABC123.parquet",
                                         [fixtures.extracted_row(c) for c in fixtures.CASES])

    def test_both_md5s_are_stopped_and_named(self):
        self.clashing()
        triples = qc.find_triples(self.folder)
        self.assertEqual(sorted(t.prefix for t in triples), ["ABC123", "abc123"])
        for triple in triples:
            self.assertIn("differs only in letter case", triple.error)
            self.assertIn("extracted/abc123.csv", triple.error)
            self.assertIn("extracted/ABC123.parquet", triple.error)
            self.assertRaises(qc.InputError, triple.check_files)

    def test_nothing_is_checked_and_nothing_is_written_for_either(self):
        self.clashing()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(err.count("differs only in letter case"), 2)
        self.assertEqual(out.strip().splitlines()[-1], "total: 0 checked, 0 skipped, 2 failed")
        self.assertFalse((self.folder / "verified").exists())

    def test_a_result_is_never_reported_as_skipped_for_a_clash(self):
        # the reported miss: abc.csv held Wrong rows, ABC.parquet's run wrote the shared file, abc.csv was "up to date"
        self.clashing()
        fixtures.write_folder_fixture(self.folder, prefix="zzz999")
        self.run_main()
        verified = self.folder / "verified"
        verified.mkdir(exist_ok=True)
        (verified / "abc123_verified.csv").write_text("stale")
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertNotIn("abc123.csv: skipped", out)
        self.assertNotIn("ABC123.parquet: skipped", out)
        self.assertEqual((verified / "abc123_verified.csv").read_text(), "stale")        # left alone

    def test_the_other_md5s_still_run(self):
        self.clashing()
        fixtures.write_folder_fixture(self.folder, prefix="zzz999")
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("extracted/zzz999.csv: 22 rows checked", out)
        self.assertEqual(out.strip().splitlines()[-1], "total: 1 checked, 0 skipped, 2 failed")
        self.assertEqual(self.verified_names(), ["zzz999_verified.csv"])

    def test_different_md5s_and_one_md5_in_two_formats_are_not_clashes(self):
        fixtures.write_folder_fixture(self.folder, prefix="abc123")
        fixtures.write_folder_fixture(self.folder, prefix="abc124")
        fixtures.write_folder_fixture(self.folder, prefix="abd123")
        fixtures.write_extracted_parquet(self.folder / "extracted" / "abc123.parquet",
                                         [fixtures.extracted_row(c) for c in fixtures.CASES])
        triples = qc.find_triples(self.folder)
        self.assertEqual([t.prefix for t in triples], ["abc123", "abc124", "abd123"])
        self.assertEqual([t.error for t in triples], [None, None, None])
        self.assertEqual(len(triples[0].notes), 1)                                       # the two-formats note

    def test_three_spellings_each_name_the_other_two(self):
        fixtures.write_folder_fixture(self.folder, prefix="abc123")
        for name in ("Abc123", "ABC123"):
            fixtures.write_extracted_parquet(self.folder / "extracted" / (name + ".parquet"),
                                             [fixtures.extracted_row(c) for c in fixtures.CASES])
        if len(list((self.folder / "extracted").iterdir())) != 3:
            self.skipTest("this file system ignores letter case in file names, so three spellings cannot coexist")
        errors = {t.prefix: t.error for t in qc.find_triples(self.folder)}
        self.assertEqual(sorted(errors), ["ABC123", "Abc123", "abc123"])
        for prefix, error in errors.items():
            others = [p for p in errors if p != prefix]
            self.assertTrue(all("extracted/%s" % o in error for o in others), (prefix, error))


class OutputFolderTest(LayoutCase):
    def test_the_verified_file_lands_in_verified_and_the_folder_is_created(self):
        fixtures.write_folder_fixture(self.folder)
        self.assertFalse((self.folder / "verified").exists())
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        rows = self.rows("verified/abc123_verified.csv")
        self.assertEqual(len(rows) - 1, len(fixtures.CASES))
        self.assertEqual(rows[0][-1], "OverallVerification")
        self.assertIn("-> verified/abc123_verified.csv", out)
        self.assertEqual(self.verified_names(), ["abc123_verified.csv"])
        self.assertFalse((self.folder / "abc123_verified.csv").exists())

    def test_trial_output_and_debug_report_go_to_verified_too(self):
        fixtures.write_folder_fixture(self.folder)
        code, out, err = self.run_main("--limit", "5", "--debug")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.verified_names(), ["abc123_debug_report_trial.txt", "abc123_verified_trial.csv"])
        self.assertEqual(len(self.rows("verified/abc123_verified_trial.csv")) - 1, 5)
        self.assertIn("debug report written to verified/abc123_debug_report_trial.txt", out)
        self.assertEqual(list(self.folder.glob("*_debug_report*.txt")), [])

    def test_a_trial_debug_run_does_not_replace_the_debug_report_of_a_full_run(self):
        for folder_name, writer in (("folder", fixtures.write_folder_fixture), ("flat", fixtures.write_fixture)):
            work = self.folder / folder_name
            work.mkdir()
            writer(work)
            where = work / "verified" if folder_name == "folder" else work
            self.run_main("--debug", folder=work)
            full = (where / "abc123_debug_report.txt").read_bytes()
            self.run_main("--debug", "--limit", "3", "--force", folder=work)
            self.run_main("--debug", "--rows", "2,3", "--force", folder=work)
            self.assertEqual((where / "abc123_debug_report.txt").read_bytes(), full, folder_name)
            trial = (where / "abc123_debug_report_trial.txt").read_text(encoding="utf-8")
            self.assertIn("rows=2 (--rows 2,3)", trial)                       # the last trial run wrote it
            self.assertNotEqual(trial.encode("utf-8"), full)

    def test_a_locked_replace_leaves_the_partial_file_in_verified(self):
        fixtures.write_folder_fixture(self.folder)
        real = os.replace

        def locked(src, dst):
            raise PermissionError("locked by another process")

        os.replace = locked
        self.addCleanup(setattr, os, "replace", real)
        code, _, err = self.run_main()
        os.replace = real
        self.assertEqual(code, 1)
        self.assertIn("verified/abc123_verified.csv.partial", err)
        self.assertEqual(self.verified_names(), ["abc123_verified.csv.partial"])
        self.assertEqual(len(self.rows("verified/abc123_verified.csv.partial")) - 1, len(fixtures.CASES))

    def test_an_output_that_cannot_be_written_is_named_relative_to_the_working_folder(self):
        fixtures.write_folder_fixture(self.folder)
        (self.folder / "verified").mkdir()
        (self.folder / "verified" / "abc123_verified.csv").mkdir()          # cannot be opened for writing
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("cannot write verified/abc123_verified.csv", err)

    def test_an_existing_verified_folder_with_other_files_is_left_alone(self):
        fixtures.write_folder_fixture(self.folder)
        (self.folder / "verified").mkdir()
        (self.folder / "verified" / "keep.txt").write_text("keep")
        self.run_main()
        self.assertEqual((self.folder / "verified" / "keep.txt").read_text(), "keep")
        self.assertEqual(self.verified_names(), ["abc123_verified.csv", "keep.txt"])

    def test_a_missing_sibling_leaves_no_verified_folder(self):
        fixtures.write_folder_fixture(self.folder)
        (self.folder / "relevant" / "abc123.parquet").unlink()
        self.run_main()
        self.assertFalse((self.folder / "verified").exists())

    def test_a_missing_required_column_leaves_no_verified_folder(self):
        fixtures.write_folder_fixture(self.folder)
        header = [h for h in fixtures.HEADER if h != "SourceLine"]
        rows = [[v for h, v in zip(fixtures.HEADER, fixtures.extracted_row(c)) if h != "SourceLine"]
                for c in fixtures.CASES]
        fixtures.write_csv(self.folder / "extracted" / "abc123.csv", rows, header)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("SourceLine", err)
        self.assertFalse((self.folder / "verified").exists())

    def test_an_undecidable_json_column_leaves_no_verified_folder(self):
        fixtures.write_folder_fixture(self.folder)
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.table({"left": ["a", "b"], "right": ["c", "d"]}),
                       str(self.folder / "relevant" / "abc123.parquet"))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("cannot tell which column holds the JSON", err)
        self.assertFalse((self.folder / "verified").exists())

    def test_the_flat_layout_still_writes_next_to_the_inputs(self):
        fixtures.write_fixture(self.folder)
        code, out, err = self.run_main("--limit", "3", "--debug")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.folder / "abc123_verified_trial.csv").is_file())
        self.assertTrue((self.folder / "abc123_debug_report_trial.txt").is_file())
        self.assertFalse((self.folder / "abc123_debug_report.txt").exists())
        self.assertFalse((self.folder / "verified").exists())
        self.assertIn("-> abc123_verified_trial.csv", out)

    def test_the_folder_layout_gives_the_same_rows_as_the_flat_layout(self):
        flat = self.folder / "flat"
        folders = self.folder / "folders"
        flat.mkdir()
        folders.mkdir()
        fixtures.write_fixture(flat)
        fixtures.write_folder_fixture(folders)
        self.run_main(folder=flat)
        self.run_main(folder=folders)
        with open(str(flat / "abc123_verified.csv"), newline="", encoding="utf-8") as handle:
            flat_rows = list(csv.reader(handle))
        with open(str(folders / "verified" / "abc123_verified.csv"), newline="", encoding="utf-8") as handle:
            folder_rows = list(csv.reader(handle))
        self.assertEqual(flat_rows, folder_rows)


BASE_NS = 1700000000 * 10 ** 9          # a fixed modification time, in nanoseconds


def stamp(path, seconds_after_base):
    ns = BASE_NS + seconds_after_base * 10 ** 9
    os.utime(str(path), ns=(ns, ns))


class ExtractedFormatTest(LayoutCase):
    def two_files(self, csv_seconds, parquet_seconds, prefix="abc123"):
        """A folder layout whose extracted/ holds both formats for one md5, with the given file times."""
        fixtures.write_folder_fixture(self.folder, prefix=prefix)
        csv_path = self.folder / "extracted" / (prefix + ".csv")
        parquet_path = self.folder / "extracted" / (prefix + ".parquet")
        parquet_path.write_bytes(b"PAR1 not read by discovery")
        stamp(csv_path, csv_seconds)
        stamp(parquet_path, parquet_seconds)
        return csv_path, parquet_path

    def test_a_parquet_only_md5(self):
        fixtures.write_folder_fixture(self.folder)
        os.remove(str(self.folder / "extracted" / "abc123.csv"))
        (self.folder / "extracted" / "abc123.parquet").write_bytes(b"x")
        (triple,) = qc.find_triples(self.folder)
        self.assertEqual((triple.prefix, triple.extracted_format), ("abc123", "parquet"))
        self.assertEqual(triple.extracted, self.folder / "extracted" / "abc123.parquet")
        self.assertEqual((triple.error, triple.notes), (None, []))

    def test_a_csv_only_md5(self):
        fixtures.write_folder_fixture(self.folder)
        (triple,) = qc.find_triples(self.folder)
        self.assertEqual(triple.extracted_format, "csv")

    def test_upper_and_mixed_case_extensions(self):
        cases = ((".PARQUET", "parquet"), (".Parquet", "parquet"), (".CSV", "csv"), (".Csv", "csv"))
        for number, (extension, expected) in enumerate(cases):
            folder = self.folder / ("case%d" % number)       # not named by extension: Windows folders ignore case
            (folder / "extracted").mkdir(parents=True)
            (folder / "extracted" / ("abc123" + extension)).write_bytes(b"x")
            (triple,) = qc.find_triples(folder)
            self.assertEqual((triple.prefix, triple.extracted_format), ("abc123", expected), extension)

    def test_both_formats_the_parquet_file_newer(self):
        csv_path, parquet_path = self.two_files(csv_seconds=0, parquet_seconds=60)
        (triple,) = qc.find_triples(self.folder)
        self.assertEqual(triple.extracted, parquet_path)
        self.assertEqual(triple.extracted_format, "parquet")
        self.assertIsNone(triple.error)
        self.assertEqual(triple.notes, ["note: abc123: using extracted/abc123.parquet "
                                        "(newer than extracted/abc123.csv)"])

    def test_both_formats_the_csv_file_newer(self):
        csv_path, parquet_path = self.two_files(csv_seconds=60, parquet_seconds=0)
        (triple,) = qc.find_triples(self.folder)
        self.assertEqual(triple.extracted, csv_path)
        self.assertEqual(triple.extracted_format, "csv")
        self.assertIn("using extracted/abc123.csv (newer than extracted/abc123.parquet)", triple.notes[0])

    def test_the_newer_file_is_used_and_announced_when_running(self):
        self.two_files(csv_seconds=60, parquet_seconds=0)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("note: abc123: using extracted/abc123.csv (newer than extracted/abc123.parquet)", err)
        self.assertIn("extracted/abc123.csv: 22 rows checked", out)

    def test_both_formats_with_equal_times_are_ambiguous(self):
        self.two_files(csv_seconds=30, parquet_seconds=30)
        (triple,) = qc.find_triples(self.folder)
        self.assertIn("extracted/abc123.csv", triple.error)
        self.assertIn("extracted/abc123.parquet", triple.error)
        self.assertIn("same modification time", triple.error)
        with self.assertRaises(qc.InputError) as ctx:
            qc.prepare_triple(triple)
        self.assertEqual(str(ctx.exception), triple.error)

    def test_an_ambiguous_md5_stops_alone(self):
        self.two_files(csv_seconds=30, parquet_seconds=30, prefix="aaa111")
        fixtures.write_folder_fixture(self.folder, prefix="bbb222")
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)
        self.assertIn("extracted/aaa111.csv", err)
        self.assertIn("extracted/aaa111.parquet", err)
        self.assertEqual(self.verified_names(), ["bbb222_verified.csv"])
        self.assertIn("extracted/bbb222.csv: 22 rows checked", out)

    def test_the_other_md5s_notes_do_not_leak(self):
        self.two_files(csv_seconds=0, parquet_seconds=60, prefix="aaa111")
        fixtures.write_folder_fixture(self.folder, prefix="bbb222")
        triples = {t.prefix: t for t in qc.find_triples(self.folder)}
        self.assertEqual(triples["bbb222"].notes, [])
        self.assertEqual(len(triples["aaa111"].notes), 1)

    def test_other_extensions_are_not_formats(self):
        fixtures.write_folder_fixture(self.folder)
        for name in ("abc123.xlsx", "abc123.json", "abc123.parquet.tmp"):
            (self.folder / "extracted" / name).write_bytes(b"x")
        (triple,) = qc.find_triples(self.folder)
        self.assertEqual((triple.extracted_format, triple.notes, triple.error), ("csv", [], None))

    def test_a_flat_parquet_extracted_file_is_not_used(self):
        fixtures.write_fixture(self.folder, prefix="abc123")
        (self.folder / "zzz999_extracted.parquet").write_bytes(b"x")
        (self.folder / "zzz999_original.parquet").write_bytes(b"x")
        found = qc.discover(self.folder)
        self.assertEqual(found.layout, "flat")
        self.assertEqual([t.prefix for t in found.triples], ["abc123"])
        self.assertEqual(found.triples[0].extracted_format, "csv")


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs git and a git checkout")
class GitIgnoreTest(unittest.TestCase):
    def ignored(self, path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=str(ROOT)).returncode == 0

    def test_the_four_folders_are_ignored(self):
        for folder in ("extracted", "original", "relevant", "verified"):
            self.assertTrue(self.ignored("%s/abc123.csv" % folder), folder)
            self.assertTrue(self.ignored("%s/" % folder), folder)

    def test_the_expected_test_files_are_not_ignored(self):
        self.assertFalse(self.ignored("tests/expected_verified.csv"))
        self.assertFalse(self.ignored("tests/expected_verified_skip.csv"))

    def test_the_script_and_the_tests_are_not_ignored(self):
        for path in ("extraction_qc.py", "EXTRACTION_QC.md", "tests/test_layout.py"):
            self.assertFalse(self.ignored(path), path)

    def test_flat_outputs_and_python_caches_are_still_ignored(self):
        for path in ("abc123_verified.csv", "abc123_extracted.csv", "abc123_original.parquet", "__pycache__/x.pyc"):
            self.assertTrue(self.ignored(path), path)


if __name__ == "__main__":
    unittest.main()

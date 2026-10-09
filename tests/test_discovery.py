import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_complete_set_of_three_files(self):
        fixtures.write_fixture(self.folder)
        triples = qc.find_triples(self.folder)
        self.assertEqual([t.prefix for t in triples], ["abc123"])
        header = qc.prepare_triple(triples[0])
        self.assertEqual(header, fixtures.HEADER)
        self.assertEqual(triples[0].original.name, "abc123_original.parquet")
        self.assertEqual(triples[0].relevant.name, "abc123_relevant.parquet")
        self.assertEqual(triples[0].verified.name, "abc123_verified.csv")

    def test_path_columns_pointing_elsewhere_are_not_used(self):
        fixtures.write_fixture(self.folder)
        triple = qc.find_triples(self.folder)[0]
        # the fixture's SourceFilePath / RelevancyFileLocation do not exist on this machine
        self.assertFalse(Path(fixtures.extracted_row(fixtures.CASES[0])[1]).exists())
        qc.prepare_triple(triple)                       # still succeeds: files come from the folder
        self.assertEqual(triple.original.parent, self.folder)

    def test_missing_sibling_file_stops_before_any_row(self):
        paths = fixtures.write_fixture(self.folder)
        paths["relevant"].unlink()
        triple = qc.find_triples(self.folder)[0]
        with self.assertRaises(qc.InputError) as ctx:
            qc.prepare_triple(triple)
        self.assertIn("abc123_relevant.parquet", str(ctx.exception))
        self.assertFalse(triple.verified.exists())

    def test_main_reports_missing_sibling_and_writes_no_output(self):
        paths = fixtures.write_fixture(self.folder)
        paths["original"].unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = qc.main(["--folder", str(self.folder)])
        self.assertEqual(code, 1)
        self.assertIn("abc123_original.parquet", err.getvalue())
        self.assertFalse((self.folder / "abc123_verified.csv").exists())

    def test_several_extracted_csvs_each_get_their_own_triple(self):
        fixtures.write_fixture(self.folder, prefix="aaa111")
        fixtures.write_fixture(self.folder, prefix="bbb222")
        triples = qc.find_triples(self.folder)
        self.assertEqual([t.prefix for t in triples], ["aaa111", "bbb222"])
        self.assertEqual(triples[1].original.name, "bbb222_original.parquet")

    def test_one_broken_triple_does_not_stop_the_others(self):
        fixtures.write_fixture(self.folder, prefix="aaa111")
        paths = fixtures.write_fixture(self.folder, prefix="bbb222")
        paths["relevant"].unlink()
        seen = []
        original = qc.verify_triple

        def fake(triple, args, **kwargs):
            seen.append(triple.prefix)
            qc.prepare_triple(triple)
            return qc.Summary()

        qc.verify_triple = fake
        self.addCleanup(setattr, qc, "verify_triple", original)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            code = qc.main(["--folder", str(self.folder)])
        self.assertEqual(seen, ["aaa111", "bbb222"])
        self.assertEqual(code, 1)

    def test_missing_required_column_is_named(self):
        fixtures.write_fixture(self.folder)
        header = [h for h in fixtures.HEADER if h != "RelevancyParquetLine"]
        rows = [[v for h, v in zip(fixtures.HEADER, fixtures.extracted_row(c)) if h != "RelevancyParquetLine"]
                for c in fixtures.CASES]
        fixtures.write_csv(self.folder / "abc123_extracted.csv", rows, header)
        triple = qc.find_triples(self.folder)[0]
        with self.assertRaises(qc.InputError) as ctx:
            qc.prepare_triple(triple)
        self.assertIn("RelevancyParquetLine", str(ctx.exception))
        self.assertFalse(triple.verified.exists())

    def test_empty_extracted_csv_is_an_input_error(self):
        fixtures.write_fixture(self.folder)
        (self.folder / "abc123_extracted.csv").write_text("")
        with self.assertRaises(qc.InputError):
            qc.prepare_triple(qc.find_triples(self.folder)[0])

    def test_no_extracted_csv_in_folder(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(self.folder)])
        self.assertEqual(code, 1)
        self.assertIn("_extracted.csv", err.getvalue())


class CommandLineTest(unittest.TestCase):
    def test_help_lists_every_switch(self):
        text = qc.build_parser().format_help()
        for switch in ("--debug", "--show-values", "--limit", "--rows",
                       "--skip-original-on-relevant-failure", "--original-column",
                       "--relevant-column", "--folder"):
            self.assertIn(switch, text)

    def test_switch_values_are_parsed(self):
        args = qc.build_parser().parse_args(
            ["--debug", "--show-values", "--limit", "10", "--rows", "1,5",
             "--skip-original-on-relevant-failure", "--original-column", "body", "--relevant-column", "doc"])
        self.assertTrue(args.debug and args.show_values and args.skip_original_on_relevant_failure)
        self.assertEqual((args.limit, args.rows, args.original_column, args.relevant_column),
                         (10, "1,5", "body", "doc"))


if __name__ == "__main__":
    unittest.main()

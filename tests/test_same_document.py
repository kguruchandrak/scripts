"""--check-same-document: the relevant document must be contained in the original document."""
import contextlib
import csv
import io
import random
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures
from tests.fixtures import L, M, N, S, doc


def row(src, rel, path, value, name="c"):
    return fixtures.extracted_row(fixtures.Case(name, str(src), str(rel), path, value, "", ""))


class SameDocumentCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def write(self, original_docs, relevant_docs, rows):
        fixtures.write_parquet(self.folder / "abc123_original.parquet", original_docs)
        fixtures.write_parquet(self.folder / "abc123_relevant.parquet", relevant_docs)
        fixtures.write_csv(self.folder / "abc123_extracted.csv", [r for r in rows])

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def results(self, *extra):
        code, _, err = self.run_main(*extra)
        self.assertEqual(code, 0, err)
        with open(str(self.folder / "abc123_verified.csv"), newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        col = {n: i for i, n in enumerate(rows[0])}
        return [(r[col["RelevantFileVerification"]], r[col["RelevantFileReason"]],
                 r[col["OriginalFileVerification"]], r[col["OriginalFileReason"]]) for r in rows[1:]]


class SameDocumentCheckTest(SameDocumentCase):
    ORIGINAL = [doc(a=S("x"), b=S("1")), doc(a=S("x"), b=S("2"))]
    RELEVANT = [doc(a=S("x"), b=S("1")), doc(a=S("x"))]

    def test_a_wrong_line_that_hits_an_identical_value_in_another_document(self):
        # SourceLine 2 holds a=x too, but it is not the document the relevant line comes from
        self.write(self.ORIGINAL, self.RELEVANT, [row(2, 1, "$.Item.a", "x")])
        without = self.results()
        with_switch = self.results("--check-same-document")
        self.assertEqual(without[0][2:], ("Correct", ""))
        self.assertEqual(with_switch[0][0:2], ("Correct", ""))
        self.assertEqual(with_switch[0][2], "Wrong")
        self.assertEqual(with_switch[0][3], "DOCUMENT_MISMATCH: relevant line 1 is not contained in original "
                                            "line 2; first difference at $.Item.b")

    def test_the_right_line_passes(self):
        self.write(self.ORIGINAL, self.RELEVANT, [row(1, 1, "$.Item.a", "x")])
        self.assertEqual(self.results("--check-same-document")[0], ("Correct", "", "Correct", ""))

    def test_a_relevant_document_that_is_a_subset_passes(self):
        self.write(self.ORIGINAL, self.RELEVANT, [row(2, 2, "$.Item.a", "x"), row(1, 2, "$.Item.a", "x")])
        for result in self.results("--check-same-document"):
            self.assertEqual(result[2:], ("Correct", ""))

    def test_an_unreadable_relevant_line_is_not_a_document_mismatch(self):
        rows = [row(1, 99, "$.Item.a", "x"), row(1, "abc", "$.Item.a", "x"), row(1, 0, "$.Item.a", "x")]
        self.write(self.ORIGINAL, self.RELEVANT, rows)
        for relevant_verdict, relevant_reason, original_verdict, original_reason in self.results("--check-same-document"):
            self.assertEqual((original_verdict, original_reason), ("Correct", ""))
            self.assertEqual(relevant_verdict, "Wrong")

    def test_an_invalid_relevant_row_is_not_a_document_mismatch(self):
        self.write(self.ORIGINAL, ["{not json"], [row(1, 1, "$.Item.a", "x")])
        result = self.results("--check-same-document", "--relevant-column", "payload")[0]
        self.assertTrue(result[1].startswith("INVALID_JSON"))
        self.assertEqual(result[2:], ("Correct", ""))

    def test_an_array_index_shift_between_the_files_shows_up(self):
        original = [doc(items=L([M({"v": S("p")}), M({"v": S("q")})]))]
        relevant = [doc(items=L([M({"v": S("q")})]))]            # the first element was dropped
        self.write(original, relevant, [row(1, 1, "$.Item.items[1].v", "q")])
        result = self.results("--check-same-document")[0]
        self.assertEqual(result[2], "Wrong")
        self.assertIn("first difference at $.Item.items[0].v", result[3])

    def test_a_number_is_not_the_same_as_a_string(self):
        original = [doc(a=S("1"))]
        relevant = [doc(a=N("1"))]
        self.write(original, relevant, [row(1, 1, "$.Item.a", "1")])
        self.assertEqual(self.results("--check-same-document")[0][2], "Wrong")

    def test_a_shorter_relevant_list_is_a_prefix_and_passes(self):
        original = [doc(items=L([S("p"), S("q"), S("r")]))]
        relevant = [doc(items=L([S("p"), S("q")]))]
        self.write(original, relevant, [row(1, 1, "$.Item.items[0]", "p")])
        self.assertEqual(self.results("--check-same-document")[0][2:], ("Correct", ""))

    def test_a_longer_relevant_list_does_not_pass(self):
        original = [doc(items=L([S("p")]))]
        relevant = [doc(items=L([S("p"), S("q")]))]
        self.write(original, relevant, [row(1, 1, "$.Item.items[0]", "p")])
        self.assertEqual(self.results("--check-same-document")[0][2], "Wrong")

    def test_plain_json_documents_work_too(self):
        original = [{"Item": {"a": "x", "b": 1}}, {"Item": {"a": "x", "b": 2}}]
        relevant = [{"Item": {"a": "x", "b": 1}}]
        self.write(original, relevant, [row(1, 1, "$.Item.a", "x"), row(2, 1, "$.Item.a", "x")])
        results = self.results("--check-same-document")
        self.assertEqual(results[0][2], "Correct")
        self.assertEqual(results[1][2], "Wrong")

    def test_typed_and_plain_forms_of_the_same_data_are_the_same_document(self):
        original = [{"Item": {"a": {"S": "x"}, "n": {"N": "5"}}}]
        relevant = [{"Item": {"a": "x", "n": 5}}]
        self.write(original, relevant, [row(1, 1, "$.Item.a", "x")])
        self.assertEqual(self.results("--check-same-document")[0][2], "Correct")

    def test_a_row_that_already_fails_keeps_its_reason_and_gains_the_mismatch(self):
        self.write(self.ORIGINAL, self.RELEVANT, [row(2, 1, "$.Item.a", "wrong")])
        result = self.results("--check-same-document")[0]
        self.assertEqual(qc.reason_code(result[3]), "VALUE_MISMATCH")
        self.assertIn("; DOCUMENT_MISMATCH: relevant line 1 is not contained in original line 2", result[3])

    def test_the_switch_changes_nothing_when_off(self):
        rows = [row(2, 1, "$.Item.a", "x"), row(1, 1, "$.Item.a", "x")]
        self.write(self.ORIGINAL, self.RELEVANT, rows)
        first = self.results()
        second = self.results()
        self.assertEqual(first, second)
        for result in first:
            self.assertNotIn("DOCUMENT_MISMATCH", result[3])

    def test_skipped_rows_are_not_compared(self):
        rows = [row(2, 1, "$.Item.a", "wrong")]
        self.write(self.ORIGINAL, self.RELEVANT, rows)
        result = self.results("--check-same-document", "--skip-original-on-relevant-failure")[0]
        self.assertEqual(result[0], "Wrong")
        self.assertEqual(result[2], "Skipped")

    def test_results_do_not_depend_on_row_order_or_chunk_size(self):
        original = [doc(a=S("x"), b=S(str(i))) for i in range(20)]
        relevant = [doc(a=S("x"), b=S(str(i))) for i in range(0, 20, 2)]
        rows = []
        for src in range(1, 21):
            for rel in (1, 2, 3):
                rows.append(row(src, rel, "$.Item.a", "x", "r%d_%d" % (src, rel)))
        self.write(original, relevant, rows)
        reference = self.results("--check-same-document")
        names = ["r%d_%d" % (src, rel) for src in range(1, 21) for rel in (1, 2, 3)]
        for seed in range(3):
            order = list(range(len(rows)))
            random.Random(seed).shuffle(order)
            self.write(original, relevant, [rows[i] for i in order])
            for chunk in (1, 7, 1000):
                real = qc.CHUNK_ROWS
                qc.CHUNK_ROWS = chunk
                try:
                    shuffled = self.results("--check-same-document")
                finally:
                    qc.CHUNK_ROWS = real
                by_name = {names[i]: shuffled[pos] for pos, i in enumerate(order)}
                self.assertEqual([by_name[n] for n in names], reference, (seed, chunk))

    def test_the_correct_pairs_are_exactly_the_matching_ones(self):
        original = [doc(a=S("x"), b=S(str(i))) for i in range(6)]
        relevant = [doc(a=S("x"), b=S("0")), doc(a=S("x"), b=S("2")), doc(a=S("x"), b=S("4"))]
        rows = [row(src, rel, "$.Item.a", "x") for src in range(1, 7) for rel in (1, 2, 3)]
        self.write(original, relevant, rows)
        results = self.results("--check-same-document")
        good = {(1, 1), (3, 2), (5, 3)}
        for (src, rel), result in zip([(s, r) for s in range(1, 7) for r in (1, 2, 3)], results):
            self.assertEqual(result[2] == "Correct", (src, rel) in good, (src, rel))

    def test_each_relevant_document_is_parsed_once_for_a_run_of_rows_with_the_same_pair(self):
        rows = [row(1, 1, "$.Item.a", "x")] * 10
        self.write(self.ORIGINAL, self.RELEVANT, rows)
        self.run_main()                                       # warm nothing: just make the files
        calls = []
        real = qc.parse_doc
        qc.parse_doc = lambda raw: (calls.append(raw), real(raw))[1]
        self.addCleanup(setattr, qc, "parse_doc", real)
        self.run_main("--check-same-document")
        # relevant lane: 1; original lane: 1 original + 1 relevant; plus column detection of the files
        detection = 4                                         # two text columns in each file
        self.assertEqual(len(calls), 3 + detection)


class SameDocumentInterfaceTest(unittest.TestCase):
    def test_the_help_documents_the_switch(self):
        self.assertIn("--check-same-document", qc.build_parser().format_help())

    def test_first_difference(self):
        self.assertIsNone(qc.first_difference({"a": 1}, {"a": 1, "b": 2}))
        self.assertEqual(qc.first_difference({"a": {"b": 1}}, {"a": {"b": 2}}), "$.a.b")
        self.assertEqual(qc.first_difference({"a": [1, 2]}, {"a": [1]}), "$.a")
        self.assertEqual(qc.first_difference({"a": [1, 2]}, {"a": [1, 3]}), "$.a[1]")
        self.assertEqual(qc.first_difference({"a": 1}, [1]), "$")
        self.assertEqual(qc.first_difference("x", "y"), "$")
        self.assertIsNone(qc.first_difference({}, {"a": 1}))

    def test_deeply_nested_documents_do_not_crash(self):
        deep_small = deep_big = "leaf"
        for _ in range(5000):
            deep_small, deep_big = {"k": deep_small}, {"k": deep_big}
        self.assertIsNotNone(qc.first_difference(deep_small, deep_big))     # reported, not raised


if __name__ == "__main__":
    unittest.main()

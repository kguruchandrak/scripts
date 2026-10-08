import random
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures


class LaneTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def open_inputs(self, cases=None, **csv_options):
        paths = fixtures.write_fixture(self.folder, cases=cases)
        csv_input = qc.CsvInput(paths["extracted"], fixtures.HEADER, **csv_options)
        original, relevant = qc.ParquetJson(paths["original"]), qc.ParquetJson(paths["relevant"])
        self.addCleanup(original.close)
        self.addCleanup(relevant.close)
        return csv_input, original, relevant

    def lane(self, label, csv_input, pj, line_col, name="results.csv", **kwargs):
        out = self.folder / name
        qc.run_lane(label, csv_input, line_col, pj, out, **kwargs)
        return list(qc.read_results(out))

    def assert_expectations(self, cases, results, which):
        self.assertEqual(len(results), len(cases))
        for case, (verdict, reason, _tag) in zip(cases, results):
            expect = getattr(case, which)
            if expect == "Correct":
                self.assertEqual((verdict, reason), ("Correct", ""), case.name)
            else:
                self.assertEqual(verdict, "Wrong", case.name)
                self.assertEqual(qc.reason_code(reason), expect, "%s: %s" % (case.name, reason))


class LaneVerdictTest(LaneTestCase):
    def test_original_lane_for_every_case(self):
        csv_input, original, _ = self.open_inputs()
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assert_expectations(fixtures.CASES, results, "orig_expect")

    def test_relevant_lane_for_every_case(self):
        csv_input, _, relevant = self.open_inputs()
        results = self.lane("relevant", csv_input, relevant, "RelevancyParquetLine")
        self.assert_expectations(fixtures.CASES, results, "rel_expect")

    def test_bad_rows_do_not_stop_the_run(self):
        cases = [
            fixtures.Case("a", "abc", "1", "$.Item.plainId", "P1", "", ""),
            fixtures.Case("b", "99", "1", "$.Item.plainId", "P1", "", ""),
            fixtures.Case("c", "1", "1", "Item..x", "P1", "", ""),
            fixtures.Case("d", "6", "1", "$.Item.plainId", "P1", "", ""),
            fixtures.Case("e", "1", "1", "$.Item.plainId", "P1", "", ""),
        ]
        csv_input, original, _ = self.open_inputs(cases)
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual([qc.reason_code(r) for _, r, _ in results],
                         ["INVALID_LINE", "LINE_OUT_OF_RANGE", "UNSUPPORTED_PATH", "INVALID_JSON", ""])
        self.assertEqual(results[-1][0], "Correct")

    def test_line_numbers_from_a_float_column_are_accepted(self):
        cases = [fixtures.Case("a", "1.0", "1", "$.Item.plainId", "P1", "", ""),
                 fixtures.Case("b", " 3 ", "1", "$.Item.plainId", "P3", "", "")]
        csv_input, original, _ = self.open_inputs(cases)
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual([v for v, _, _ in results], ["Correct", "Correct"])

    def test_non_integral_or_odd_line_numbers_are_invalid(self):
        for text in ("1.5", "1e2", "1_0", "one", "-", "٣x"):
            with self.assertRaises(qc.InvalidLine, msg=text):
                qc.parse_line(text)

    def test_reason_names_the_column_and_line(self):
        cases = [fixtures.Case("a", "99", "1", "$.Item.plainId", "P1", "", ""),
                 fixtures.Case("b", "abc", "1", "$.Item.plainId", "P1", "", "")]
        csv_input, original, _ = self.open_inputs(cases)
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assertIn("SourceLine 99", results[0][1])
        self.assertIn("8 rows", results[0][1])
        self.assertIn("SourceLine", results[1][1])

    def test_path_not_found_reason_says_where_the_walk_stopped(self):
        cases = [fixtures.Case("a", "1", "1", "$.Item.scopeIds[0].settings.agencyTIN", "1", "", "")]
        csv_input, original, _ = self.open_inputs(cases)
        (verdict, reason, tag), = self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual((verdict, tag), ("Wrong", "-"))
        self.assertIn("settings", reason)
        self.assertIn("$.Item.scopeIds[0]", reason)

    def test_tag_of_the_source_value_is_recorded(self):
        cases = [fixtures.Case("a", "1", "1", "$.Item.active", "true", "", ""),
                 fixtures.Case("b", "1", "1", "$.Item.plainId", "P1", "", ""),
                 fixtures.Case("c", "1", "1", fixtures.AGENCY_TIN, "12345.0", "", "")]
        csv_input, original, _ = self.open_inputs(cases)
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual([t for _, _, t in results], ["BOOL", "S", "N"])


class LaneOrderTest(LaneTestCase):
    def by_name(self, cases, results):
        return {c.name: (v, r) for c, (v, r, _t) in zip(cases, results)}

    def run_both(self, cases, chunk_rows):
        csv_input, original, relevant = self.open_inputs(cases)
        return (self.by_name(cases, self.lane("original", csv_input, original, "SourceLine",
                                              name="o.csv", chunk_rows=chunk_rows)),
                self.by_name(cases, self.lane("relevant", csv_input, relevant, "RelevancyParquetLine",
                                              name="r.csv", chunk_rows=chunk_rows)))

    def test_shuffled_csv_gives_the_same_results_as_a_sorted_one(self):
        def numeric(case):
            return int(case.src) if case.src.isdigit() else -1

        ordered = sorted(fixtures.CASES, key=numeric)
        reference = self.run_both(ordered, chunk_rows=1000)
        for seed in range(5):
            shuffled = list(fixtures.CASES)
            random.Random(seed).shuffle(shuffled)
            for chunk_rows in (1, 3, 1000):
                self.assertEqual(self.run_both(shuffled, chunk_rows), reference, (seed, chunk_rows))

    def test_results_are_in_csv_order_not_line_order(self):
        cases = [fixtures.Case("late", "8", "4", "$.Item.createdBy", "carol", "", ""),
                 fixtures.Case("early", "1", "1", "$.Item.plainId", "P1", "", ""),
                 fixtures.Case("wrong", "2", "1", "$.Item.plainId", "P2", "", ""),
                 fixtures.Case("bad", "2", "1", "$.Item.plainId", "nope", "", "")]
        csv_input, original, _ = self.open_inputs(cases)
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual([v for v, _, _ in results], ["Correct", "Correct", "Correct", "Wrong"])

    def test_each_distinct_line_is_parsed_once(self):
        cases = [fixtures.Case("x%d" % i, "1", "1", "$.Item.plainId", "P1", "", "") for i in range(10)]
        cases += [fixtures.Case("y%d" % i, "3", "2", "$.Item.plainId", "P3", "", "") for i in range(10)]
        csv_input, original, _ = self.open_inputs(cases)
        calls = []
        real = qc.parse_doc
        qc.parse_doc = lambda raw: (calls.append(1), real(raw))[1]
        self.addCleanup(setattr, qc, "parse_doc", real)
        self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual(len(calls), 2)   # lines 1 and 3 only: passing rows never parse neighbours


class ParseOnceTest(LaneTestCase):
    def count_parses(self):
        calls = []
        real = qc.parse_doc
        qc.parse_doc = lambda raw: (calls.append(raw), real(raw))[1]
        self.addCleanup(setattr, qc, "parse_doc", real)
        return calls

    def failing_rows(self, per_line=3):
        return [fixtures.Case("l%d_%d" % (line, k), str(line), "1", "$.Item.plainId", "nope", "", "")
                for line in range(1, 9) for k in range(per_line)]

    def test_every_row_failing_still_parses_each_document_once(self):
        cases = self.failing_rows()
        csv_input, original, _ = self.open_inputs(cases)
        calls = self.count_parses()
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual({v for v, _, _ in results}, {"Wrong"})
        self.assertEqual(len(calls), 8)                      # 8 documents, not 8 + 2 neighbours per failing line

    def test_unsorted_failing_rows_stay_within_the_neighbour_window(self):
        cases = self.failing_rows(per_line=1)
        random.Random(5).shuffle(cases)
        csv_input, original, _ = self.open_inputs(cases)
        calls = self.count_parses()
        self.lane("original", csv_input, original, "SourceLine", chunk_rows=1000)
        self.assertEqual(len(calls), 8)                      # sorted within the chunk, so still once each

    def test_the_relevant_lane_parses_each_document_once_too(self):
        cases = [fixtures.Case("r%d" % line, "1", str(line), "$.Item.plainId", "nope", "", "") for line in range(1, 5)]
        csv_input, _, relevant = self.open_inputs(cases)
        calls = self.count_parses()
        self.lane("relevant", csv_input, relevant, "RelevancyParquetLine")
        self.assertEqual(len(calls), 4)

    def test_hints_still_work_with_the_cache(self):
        cases = [fixtures.Case("a", "2", "1", "$.Item.plainId", "P1", "", ""),
                 fixtures.Case("b", "3", "1", "$.Item.plainId", "P2", "", "")]
        csv_input, original, _ = self.open_inputs(cases)
        results = self.lane("original", csv_input, original, "SourceLine")
        self.assertTrue(results[0][1].endswith("FOUND_AT_LINE_1"), results[0][1])
        self.assertTrue(results[1][1].endswith("FOUND_AT_LINE_2"), results[1][1])


class DocCacheTest(unittest.TestCase):
    class FakeCursor:
        class pj:
            num_rows = 100

        def __init__(self):
            self.reads = []

        def get(self, idx):
            self.reads.append(idx)
            return None if idx >= 100 else '{"n": %d}' % idx if idx != 5 else "{bad"

    def test_recent_documents_are_not_read_again(self):
        cursor = self.FakeCursor()
        cache = qc.DocCache(cursor, size=3)
        for idx in (1, 2, 3, 1, 2, 3):
            cache.get(idx)
        self.assertEqual(cursor.reads, [1, 2, 3])

    def test_the_cache_is_bounded_and_forgets_the_oldest(self):
        cursor = self.FakeCursor()
        cache = qc.DocCache(cursor, size=3)
        for idx in range(10):
            cache.get(idx)
        self.assertEqual(len(cache._docs), 3)
        cache.get(9)
        self.assertEqual(len(cursor.reads), 10)             # 9 is still cached
        cache.get(0)
        self.assertEqual(len(cursor.reads), 11)             # 0 was forgotten

    def test_problems_are_cached_too(self):
        cursor = self.FakeCursor()
        cache = qc.DocCache(cursor)
        self.assertIs(cache.get(500)[1], qc._OUTSIDE)
        self.assertIsInstance(cache.get(5)[1], qc.InvalidJson)
        cache.get(500)
        cache.get(5)
        self.assertEqual(cursor.reads, [500, 5])
        self.assertEqual(cache.get(7), ({"n": qc.NumText("7")}, None))


class AdjacentLineHintTest(LaneTestCase):
    def reasons(self, case, lane="original"):
        csv_input, original, relevant = self.open_inputs([case])
        if lane == "original":
            (_, reason, _), = self.lane("original", csv_input, original, "SourceLine")
        else:
            (_, reason, _), = self.lane("relevant", csv_input, relevant, "RelevancyParquetLine")
        return reason

    def test_off_by_one_is_hinted(self):
        reason = self.reasons(fixtures.Case("a", "2", "1", "$.Item.plainId", "P1", "", ""))
        self.assertTrue(reason.startswith("VALUE_MISMATCH:"), reason)
        self.assertTrue(reason.endswith("; FOUND_AT_LINE_1"), reason)

    def test_next_line_is_checked_too(self):
        reason = self.reasons(fixtures.Case("a", "1", "1", "$.Item.plainId", "P2", "", ""))
        self.assertTrue(reason.endswith("; FOUND_AT_LINE_2"), reason)

    def test_relevant_lane_hints_too(self):
        reason = self.reasons(fixtures.Case("a", "1", "2", "$.Item.plainId", "P1", "", ""), lane="relevant")
        self.assertTrue(reason.endswith("; FOUND_AT_LINE_1"), reason)

    def test_not_found_nearby_has_no_hint(self):
        reason = self.reasons(fixtures.Case("a", "1", "1", "$.Item.createdBy", "bob", "", ""))
        self.assertNotIn("FOUND_AT_LINE", reason)

    def test_value_two_lines_away_is_not_hinted(self):
        reason = self.reasons(fixtures.Case("a", "1", "1", "$.Item.plainId", "P3", "", ""))
        self.assertNotIn("FOUND_AT_LINE", reason)

    def test_hint_after_line_zero(self):
        reason = self.reasons(fixtures.Case("a", "0", "1", "$.Item.plainId", "P1", "", ""))
        self.assertTrue(reason.startswith("LINE_OUT_OF_RANGE:"), reason)
        self.assertTrue(reason.endswith("; FOUND_AT_LINE_1"), reason)

    def test_hint_one_line_past_the_end(self):
        reason = self.reasons(fixtures.Case("a", "9", "1", "$.Item.plainId", "P8", "", ""))
        self.assertTrue(reason.startswith("LINE_OUT_OF_RANGE:"), reason)
        self.assertTrue(reason.endswith("; FOUND_AT_LINE_8"), reason)

    def test_far_out_of_range_has_no_hint(self):
        reason = self.reasons(fixtures.Case("a", "99", "1", "$.Item.plainId", "P1", "", ""))
        self.assertNotIn("FOUND_AT_LINE", reason)

    def test_hint_after_invalid_json(self):
        reason = self.reasons(fixtures.Case("a", "6", "1", "$.Item.plainId", "P7", "", ""))
        self.assertTrue(reason.startswith("INVALID_JSON:"), reason)
        self.assertTrue(reason.endswith("; FOUND_AT_LINE_7"), reason)

    def test_no_hint_when_the_line_or_path_is_unusable(self):
        self.assertNotIn("FOUND_AT_LINE", self.reasons(fixtures.Case("a", "abc", "1", "$.Item.plainId", "P1", "", "")))
        self.assertNotIn("FOUND_AT_LINE", self.reasons(fixtures.Case("a", "1", "1", "Item..x", "P1", "", "")))

    def test_neighbours_are_parsed_only_for_failing_rows(self):
        passing = [fixtures.Case("p%d" % i, "2", "1", "$.Item.plainId", "P2", "", "") for i in range(3)]
        failing = [fixtures.Case("f", "2", "1", "$.Item.plainId", "nope", "", "")]
        calls = []
        real = qc.parse_doc
        self.addCleanup(setattr, qc, "parse_doc", real)

        csv_input, original, _ = self.open_inputs(passing)
        qc.parse_doc = lambda raw: (calls.append(1), real(raw))[1]    # after column detection
        self.lane("original", csv_input, original, "SourceLine")
        self.assertEqual(len(calls), 1)

        qc.parse_doc = real
        csv_input, original, _ = self.open_inputs(passing + failing)
        qc.parse_doc = lambda raw: (calls.append(1), real(raw))[1]
        del calls[:]
        self.lane("original", csv_input, original, "SourceLine", name="two.csv")
        self.assertEqual(len(calls), 3)   # the line itself plus both neighbours, parsed once each


if __name__ == "__main__":
    unittest.main()

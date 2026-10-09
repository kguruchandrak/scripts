import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import extraction_qc as qc
from tests import fixtures
from tests.fixtures import L, M, N, S, doc


def row(src, rel, path, value, datatype="string", name="c"):
    return ["abc123_original.parquet", "C:\\vm\\abc123_original.parquet", "/vm/abc123_relevant.parquet",
            str(src), str(rel), "Role", path, path, "Canon_" + name, datatype, value]


class DebugTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def write(self, original_docs, relevant_docs, rows):
        fixtures.write_parquet(self.folder / "abc123_original.parquet", original_docs)
        fixtures.write_parquet(self.folder / "abc123_relevant.parquet", relevant_docs)
        fixtures.write_csv(self.folder / "abc123_extracted.csv", rows)

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qc.main(["--folder", str(self.folder)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def collect(self, *extra, show_values=False):
        args = qc.build_parser().parse_args(["--folder", str(self.folder), "--debug"] + list(extra))
        args.rows_set = qc.parse_row_list(args.rows) if args.rows else None
        collector = qc.DebugCollector(show_values=show_values)
        summary = qc.verify_triple(qc.find_triples(self.folder)[0], args, collector=collector)
        return collector, summary

    def blocks(self, *extra, show_values=False):
        collector, summary = self.collect(*extra, show_values=show_values)
        return (collector.report(summary, "abc123_extracted.csv", paste=True),
                collector.report(summary, "abc123_extracted.csv", paste=False, reveal=show_values))

    def parquet(self, docs, name="one.parquet"):
        path = self.folder / name
        fixtures.write_parquet(path, docs)
        pj = qc.ParquetJson(path, column=fixtures.JSON_COLUMN)
        self.addCleanup(pj.close)
        return pj


class MaskingTest(unittest.TestCase):
    def test_letters_and_digits_become_shapes(self):
        self.assertEqual(qc.mask("2024-01-25"), "9999-99-99")
        self.assertEqual(qc.mask("alice"), "aaaaa")
        self.assertEqual(qc.mask("True"), "Aaaa")
        self.assertEqual(qc.mask("P1 x-9"), "A9 a-9")

    def test_other_characters_keep_their_shape(self):
        self.assertEqual(qc.mask("a.b@c.com"), "a.a@a.aaa")
        self.assertEqual(qc.mask(""), "")

    def test_non_ascii_letters_and_control_characters(self):
        self.assertEqual(qc.mask("éÉ"), "aA")
        self.assertEqual(qc.mask("a\nb\tc"), "a~a~a")

    def test_clipping(self):
        self.assertEqual(qc.clip_end("abcdef", 6), "abcdef")
        self.assertEqual(qc.clip_end("abcdefg", 6), "abc...")
        self.assertEqual(len(qc.clip_middle("x" * 100, 30)), 30)
        self.assertIn("...", qc.clip_middle("x" * 100, 30))


class InputProfileAndReasonCountsTest(DebugTestCase):
    def test_profile_lines_state_rows_groups_and_json_column(self):
        fixtures.write_fixture(self.folder)
        paste, _ = self.blocks()
        text = "\n".join(paste)
        self.assertIn("relevant: rows=4 row_groups=2 json_column='payload' (string)", text)
        self.assertIn("original: rows=8 row_groups=3 json_column='payload' (string)", text)

    def test_title_states_the_parquet_library_version(self):
        fixtures.write_fixture(self.folder)
        paste, full = self.blocks()
        import pyarrow
        self.assertIn("pyarrow %s" % pyarrow.__version__, paste[0])
        self.assertIn("values masked", paste[0])
        self.assertIn("pyarrow %s" % pyarrow.__version__, full[0])

    def test_csv_order_is_reported_as_ascending(self):
        docs = [doc(plainId=S("P1")), doc(plainId=S("P2")), doc(plainId=S("P3"))]
        rows = [row(1, 1, "$.Item.plainId", "P1"), row(1, 1, "$.Item.plainId", "P1"),
                row(2, 2, "$.Item.plainId", "P2"), row(3, 3, "$.Item.plainId", "P3")]
        self.write(docs, docs, rows)
        paste, _ = self.blocks()
        line = [l for l in paste if l.startswith("csv:")][0]
        self.assertIn("rows=4", line)
        self.assertIn("SourceLine order: ascending", line)
        self.assertIn("RelevancyParquetLine order: ascending", line)

    def test_csv_order_is_reported_as_not_ascending(self):
        docs = [doc(plainId=S("P1")), doc(plainId=S("P2")), doc(plainId=S("P3"))]
        rows = [row(3, 1, "$.Item.plainId", "P3"), row(1, 2, "$.Item.plainId", "P1"),
                row(2, 3, "$.Item.plainId", "P2")]
        self.write(docs, docs, rows)
        paste, _ = self.blocks()
        line = [l for l in paste if l.startswith("csv:")][0]
        self.assertIn("SourceLine order: not ascending", line)
        self.assertIn("RelevancyParquetLine order: ascending", line)

    def test_unreadable_line_numbers_do_not_change_the_order_flag(self):
        docs = [doc(plainId=S("P1")), doc(plainId=S("P2"))]
        rows = [row(1, 1, "$.Item.plainId", "P1"), row("abc", "", "$.Item.plainId", "P1"),
                row(2, 2, "$.Item.plainId", "P2")]
        self.write(docs, docs, rows)
        paste, _ = self.blocks()
        line = [l for l in paste if l.startswith("csv:")][0]
        self.assertIn("SourceLine order: ascending", line)

    def test_result_line_counts_correct_rows(self):
        fixtures.write_fixture(self.folder)
        paste, _ = self.blocks()
        line = [l for l in paste if l.startswith("result:")][0]
        self.assertEqual(line, "result: relevant 14/22 correct | original 9/22 correct | overall 8/22 correct")

    def test_reason_counts_per_lane(self):
        fixtures.write_fixture(self.folder)
        paste, _ = self.blocks()
        relevant = [l for l in paste if l.startswith("relevant: ") and "FORMAT_CHANGED" in l][0]
        original = [l for l in paste if l.startswith("original: ") and "FORMAT_CHANGED" in l][0]
        self.assertIn("FORMAT_CHANGED 4", relevant)
        self.assertIn("VALUE_MISMATCH 2", relevant)
        self.assertIn("PATH_NOT_FOUND 1", relevant)
        self.assertNotIn("LINE_OUT_OF_RANGE", relevant)
        self.assertIn("LINE_OUT_OF_RANGE 2", original)
        self.assertIn("INVALID_LINE 2", original)

    def test_scope_is_stated_when_the_run_was_limited(self):
        fixtures.write_fixture(self.folder)
        paste, _ = self.blocks("--limit", "3")
        self.assertIn("rows=3 (--limit 3)", [l for l in paste if l.startswith("csv:")][0])
        paste, _ = self.blocks("--rows", "9,14")
        self.assertIn("rows=2 (--rows 9,14)", [l for l in paste if l.startswith("csv:")][0])


class NoRealValuesTest(DebugTestCase):
    SECRETS = ("alice", "carol", "bob", "12345", "2024", "01/25", "P1", "P3", "Canon_", "C:\\vm")

    def setUp(self):
        super().setUp()
        fixtures.write_fixture(self.folder)

    def test_the_paste_block_never_holds_a_real_value(self):
        paste, _ = self.blocks()
        text = "\n".join(paste)
        for secret in self.SECRETS:
            self.assertNotIn(secret, text, secret)

    def test_the_full_report_is_masked_by_default(self):
        _, full = self.blocks()
        text = "\n".join(full)
        for secret in self.SECRETS[:6]:
            self.assertNotIn(secret, text, secret)

    def test_show_values_unmasks_only_the_full_report(self):
        paste, full = self.blocks(show_values=True)
        self.assertIn("alice", "\n".join(full))
        self.assertIn("values REAL", full[0])
        for secret in self.SECRETS:
            self.assertNotIn(secret, "\n".join(paste), secret)
        self.assertIn("values masked", paste[0])

    def test_names_and_dates_appear_only_as_shapes(self):
        _, full = self.blocks()
        text = "\n".join(full)
        self.assertIn("found 'aaaaa'", text)   # the source value alice, as a shape in a trace
        self.assertIn("99/99/9999", text)      # the reformatted date Value, as a shape


class SkeletonTest(DebugTestCase):
    def entries(self, docs, **kwargs):
        return qc.skeleton_entries(qc.sample_skeleton(self.parquet(docs), **kwargs))

    def test_nested_list_of_maps(self):
        entries = self.entries([doc(scopeIds=L([M({"partnerId": S("A1")})]))])
        self.assertEqual(entries, ["Item.scopeIds:L[M{partnerId:S}]"])

    def test_scalar_tags(self):
        entries = self.entries([doc(a=S("x"), b=N("1"), c={"BOOL": True}, d={"NULL": True}, e={"SS": ["p"]})])
        self.assertEqual(entries, ["Item.a:S", "Item.b:N", "Item.c:BOOL", "Item.d:NULL", "Item.e:SS"])

    def test_documents_are_merged(self):
        entries = self.entries([doc(a=S("x"), only_first=S("1")), doc(a={"NULL": True}, only_second=S("2"))])
        self.assertEqual(entries, ["Item.a:NULL|S", "Item.only_first:S", "Item.only_second:S"])

    def test_plain_json_documents(self):
        entries = self.entries([{"Item": {"a": "x", "b": [1, 2], "c": {"d": True}}}])
        self.assertEqual(entries, ["Item.a:S", "Item.b:L[N]", "Item.c:M{d:BOOL}"])

    def test_nested_map_with_many_keys_is_collapsed(self):
        many = {"key%02d" % i: S("v") for i in range(21)}
        entries = self.entries([doc(byId=M(many), small=M({"k": S("v")}))])
        self.assertEqual(entries, ["Item.byId:M{*}", "Item.small:M{k:S}"])
        self.assertTrue(all("key00" not in e for e in entries))

    def test_nested_map_with_exactly_twenty_keys_is_listed(self):
        twenty = {"key%02d" % i: S("v") for i in range(20)}
        entries = self.entries([doc(byId=M(twenty))])
        self.assertIn("key19:S", entries[0])

    def test_keys_spread_over_documents_count_towards_the_collapse(self):
        docs = [doc(byId=M({"k%d_%d" % (d, i): S("v") for i in range(5)})) for d in range(5)]
        self.assertEqual(self.entries(docs), ["Item.byId:M{*}"])

    def test_item_with_thirty_top_level_attributes_lists_them_all(self):
        entries = self.entries([doc(**{"attr%02d" % i: S("v") for i in range(30)})])
        self.assertEqual(len(entries), 30)
        self.assertTrue(all(not e.endswith("{*}") for e in entries))

    def test_only_the_first_documents_are_sampled(self):
        docs = [doc(a=S("x")) for _ in range(5)] + [doc(late=S("x"))]
        self.assertEqual(self.entries(docs, documents=5), ["Item.a:S"])

    def test_unreadable_documents_are_skipped(self):
        entries = self.entries(["{not json", doc(a=S("x"))])
        self.assertEqual(entries, ["Item.a:S"])

    def test_deep_nesting_is_cut_off(self):
        deep = S("x")
        for _ in range(12):
            deep = M({"k": deep})
        entries = self.entries([doc(deep=deep)])
        self.assertIn("{...}", entries[0])
        self.assertLess(len(entries[0]), 200)

    def test_empty_list_and_empty_map(self):
        entries = self.entries([doc(l=L([]), m=M({}))])
        self.assertEqual(entries, ["Item.l:L[]", "Item.m:M{}"])

    def test_no_map_found(self):
        self.assertEqual(self.entries(["[1,2]"]), [])

    def test_skeleton_appears_in_the_paste_block(self):
        docs = [doc(scopeIds=L([M({"partnerId": S("A1")})]))]
        self.write(docs, docs, [row(1, 1, "$.Item.scopeIds[0].partnerId", "A1")])
        paste, _ = self.blocks()
        self.assertIn("original: Item.scopeIds:L[M{partnerId:S}]", paste)
        self.assertIn("relevant: Item.scopeIds:L[M{partnerId:S}]", paste)


class KeyMaskingTest(DebugTestCase):
    def test_ordinary_names_are_shown_as_they_are(self):
        for key in ("scopeIds", "partnerId", "line1", "_private", "GSI1PK", "a", "x" * 40, "Item", "address2"):
            self.assertEqual(qc.safe_key(key), key, key)

    def test_data_like_keys_are_masked(self):
        self.assertEqual(qc.safe_key("ann@example.com"), "aaa@aaaaaaa.aaa")
        self.assertEqual(qc.safe_key("user12345"), "aaaa99999")
        self.assertEqual(qc.safe_key("3f2a9c1e-8d4b-4c6e-9a1b-2d3e4f5a6b7c"), "9a9a9a9a-9a9a-9a9a-9a9a-9a9a9a9a9a9a")
        self.assertEqual(qc.safe_key("created-by"), "aaaaaaa-aa")
        self.assertEqual(qc.safe_key("1abc"), "9aaa")
        self.assertEqual(qc.safe_key("Ann Lee"), "Aaa Aaa")
        self.assertEqual(qc.safe_key("x" * 41), "a" * 37 + "...")
        self.assertEqual(qc.safe_key("中文"), "aa")

    def test_two_digits_in_a_row_are_fine_but_three_are_not(self):
        self.assertEqual(qc.safe_key("field12"), "field12")
        self.assertEqual(qc.safe_key("field123"), "aaaaa999")

    def test_path_keys_are_masked_in_place(self):
        path = "$.Item.user12345.name[*]['ann@example.com'].x[\"a b\"]"
        self.assertEqual(qc.mask_path_keys(path), "$.Item.aaaa99999.name[*]['aaa@aaaaaaa.aaa'].x[\"a a\"]")
        self.assertEqual(qc.mask_path_keys("$.Item.scopeIds[*].partnerId"), "$.Item.scopeIds[*].partnerId")
        self.assertEqual(qc.mask_path_keys("$.Item.a[0].b"), "$.Item.a[0].b")

    def test_a_map_keyed_by_email_addresses_in_the_paste_block(self):
        docs = [doc(byEmail=M({"ann@example.com": S("v"), "bob@example.org": S("w")}), plain=S("p"))]
        self.write(docs, docs, [row(1, 1, "$.Item.plain", "p")])
        paste, full = self.blocks()
        text = "\n".join(paste)
        self.assertIn("aaa@aaaaaaa.aaa", text)
        self.assertNotIn("ann@example.com", text)
        self.assertNotIn("bob@example.org", text)
        self.assertNotIn("ann@example.com", "\n".join(full))

    def test_ordinary_attribute_names_still_show_in_the_skeleton(self):
        docs = [doc(scopeIds=L([M({"partnerId": S("A1"), "line1": S("x")})]))]
        self.write(docs, docs, [row(1, 1, "$.Item.scopeIds[0].partnerId", "A1")])
        paste, _ = self.blocks()
        self.assertTrue(any("Item.scopeIds:L[M{line1:S,partnerId:S}]" in l for l in paste), paste)

    def test_a_hyphenated_top_level_attribute_is_masked_too(self):
        docs = [doc(**{"created-by": S("x")})]
        self.write(docs, docs, [row(1, 1, "$.Item['created-by']", "x")])
        paste, full = self.blocks()
        self.assertIn("Item.aaaaaaa-aa:S", "\n".join(paste))
        self.assertNotIn("created-by", "\n".join(paste))

    def test_a_path_shape_with_a_data_like_key(self):
        docs = [doc(byId=M({"user12345": M({"name": S("n")})}))]
        self.write(docs, docs, [row(1, 1, "$.Item.byId.user12345.name", "n"),
                                row(1, 1, "$.Item.byId.user12345.name", "wrong")])
        paste, full = self.blocks()
        shape = [l for l in paste if l.startswith("$.Item.byId")][0]
        self.assertTrue(shape.startswith("$.Item.byId.aaaa99999.name"), shape)
        self.assertNotIn("user12345", "\n".join(paste))
        self.assertNotIn("user12345", "\n".join(full))

    def test_a_trace_with_a_data_like_key(self):
        docs = [doc(byId=M({"user12345": M({"name": S("n")})}))]
        self.write(docs, docs, [row(1, 1, "$.Item.byId.user12345.missing", "n")])
        paste, _ = self.blocks()
        trace = [l for l in paste if l.startswith("row 1 original")][0]
        self.assertEqual(trace, "row 1 original line 1 PATH_NOT_FOUND: $.Item.byId.aaaa99999 resolved, "
                                "stopped at missing")

    def test_a_data_like_key_where_the_walk_stops(self):
        docs = [doc(byId=M({"name": S("n")}))]
        self.write(docs, docs, [row(1, 1, "$.Item.byId.user12345", "n")])
        paste, _ = self.blocks()
        trace = [l for l in paste if l.startswith("row 1 original")][0]
        self.assertTrue(trace.endswith("stopped at aaaa99999"), trace)

    def test_show_values_reveals_real_keys_in_the_full_report_only(self):
        docs = [doc(byEmail=M({"ann@example.com": S("v")}), byId=M({"user12345": M({"name": S("n")})}))]
        self.write(docs, docs, [row(1, 1, "$.Item.byId.user12345.missing", "n"),
                                row(1, 1, "$.Item.byId.user12345.name", "wrong")])
        paste, full = self.blocks(show_values=True)
        full_text, paste_text = "\n".join(full), "\n".join(paste)
        self.assertIn("ann@example.com", full_text)
        self.assertIn("$.Item.byId.user12345.name", full_text)
        self.assertIn("$.Item.byId.user12345 resolved, stopped at missing", full_text)
        for secret in ("ann@example.com", "user12345"):
            self.assertNotIn(secret, paste_text)

    def test_the_masked_form_is_what_is_kept_without_show_values(self):
        docs = [doc(byId=M({"user12345": S("n")}))]
        self.write(docs, docs, [row(1, 1, "$.Item.byId.user12345", "n")])
        collector, _ = self.collect()
        self.assertEqual(list(collector.shapes), ["$.Item.byId.aaaa99999"])
        self.assertEqual(collector.skeletons_raw, {})

    def test_distinct_keys_that_mask_alike_are_listed_separately_in_the_skeleton(self):
        docs = [doc(m=M({"ann@a.com": S("v"), "bob@b.com": S("v")}))]
        pj = self.parquet(docs)
        entries = qc.skeleton_entries(qc.sample_skeleton(pj), qc.safe_key)
        self.assertEqual(entries, ["Item.m:M{aaa@a.aaa:S,aaa@a.aaa:S}"])

    def test_document_mismatch_trace_masks_the_path(self):
        original = [doc(byId=M({"user12345": S("x")}), a=S("same"))]
        relevant = [doc(byId=M({"user12345": S("y")}), a=S("same"))]
        self.write(original, relevant, [row(1, 1, "$.Item.a", "same")])
        collector, summary = self.collect("--check-same-document")
        paste = collector.report(summary, "x.csv", paste=True)
        trace = [l for l in paste if "DOCUMENT_MISMATCH" in l and l.startswith("row")][0]
        self.assertIn("differs at $.Item.byId.aaaa99999", trace)
        full = collector.report(summary, "x.csv", paste=False, reveal=True)
        self.assertTrue(any("user12345" in l for l in full) is False)    # not collected raw without --show-values


class PackEntriesTest(unittest.TestCase):
    def test_entries_are_packed_into_lines_of_limited_width(self):
        lines = qc.pack_entries("p: ", ["aaaaaaaa"] * 10, 30, None)
        self.assertTrue(all(len(l) <= 30 for l in lines))
        self.assertEqual(sum(l.count("aaaaaaaa") for l in lines), 10)

    def test_extra_lines_become_a_count(self):
        lines = qc.pack_entries("p: ", ["aaaaaaaa"] * 10, 30, 2)
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[-1], r"\.\.\.\(\+\d+ more\)$")
        self.assertTrue(all(len(l) <= 30 for l in lines))
        shown = sum(l.count("aaaaaaaa") for l in lines)
        self.assertEqual(shown + int(lines[-1].rsplit("+", 1)[1].split(" ")[0]), 10)

    def test_a_single_long_entry_is_clipped(self):
        lines = qc.pack_entries("p: ", ["x" * 100], 30, None)
        self.assertEqual(len(lines[0]), 30)

    def test_no_limits(self):
        self.assertEqual(qc.pack_entries("", ["a", "b"], None, None), ["a  b"])


class PathShapeTest(DebugTestCase):
    def test_systematic_failure_in_one_lane(self):
        original = [doc(plainId=S("P1"))]
        relevant = [doc(plainId=S("P1"), scopeIds=L([M({"partnerId": S("A1")}), M({"partnerId": S("A2")})]))]
        rows = [row(1, 1, "$.Item.scopeIds[0].partnerId", "A1"), row(1, 1, "$.Item.scopeIds[1].partnerId", "A2")]
        self.write(original, relevant, rows)
        paste, _ = self.blocks()
        self.assertIn("$.Item.scopeIds[*].partnerId  n=2 rel=2 orig=0 [orig PATH_NOT_FOUND]", paste)

    def test_index_values_are_collapsed_into_one_shape(self):
        docs = [doc(items=L([M({"v": S("x")}), M({"v": S("x")}), M({"v": S("x")})]))]
        rows = [row(1, 1, "$.Item.items[%d].v" % i, "x") for i in range(3)]
        self.write(docs, docs, rows)
        collector, _ = self.collect()
        self.assertEqual(list(collector.shapes), ["$.Item.items[*].v"])
        self.assertEqual(collector.shapes["$.Item.items[*].v"].rows, 3)

    def test_worst_shapes_come_first(self):
        docs = [doc(a=S("1"), b=S("1"), c=S("1"))]
        rows = [row(1, 1, "$.Item.a", "1")] * 5 + [row(1, 1, "$.Item.b", "x")] * 2 + [row(1, 1, "$.Item.c", "x")] * 3
        self.write(docs, docs, rows)
        paste, _ = self.blocks()
        shapes = [l.split("  ")[0] for l in paste if l.startswith("$.Item.")]
        self.assertEqual(shapes, ["$.Item.c", "$.Item.b", "$.Item.a"])

    def test_paste_lists_eight_shapes_and_the_report_lists_all(self):
        docs = [doc(**{"f%02d" % i: S("v") for i in range(40)})]
        rows = [row(1, 1, "$.Item.f%02d" % i, "wrong") for i in range(40)]
        self.write(docs, docs, rows)
        paste, full = self.blocks()
        self.assertEqual(len([l for l in paste if l.startswith("$.Item.f")]), 8)
        self.assertEqual(len([l for l in full if l.startswith("$.Item.f")]), 40)
        self.assertTrue(any("(8 of 40" in l for l in paste))
        self.assertTrue(any("(40 of 40" in l for l in full))

    def test_long_paths_are_clipped_to_the_width(self):
        path = "$.Item." + ".".join("segment%02d" % i for i in range(30))
        docs = [doc(x=S("v"))]
        self.write(docs, docs, [row(1, 1, path, "v")])
        paste, _ = self.blocks()
        line = [l for l in paste if l.startswith("$.Item.")][0]
        self.assertLessEqual(len(line), qc.PASTE_MAX_WIDTH)
        self.assertIn("...", line)
        self.assertTrue(line.rstrip().endswith("orig=0 [rel PATH_NOT_FOUND; orig PATH_NOT_FOUND]"))

    def test_shapes_are_bounded(self):
        collector = qc.DebugCollector(show_values=True)      # real keys, so that every path is its own shape
        csv_input = qc.CsvInput(self.folder / "x.csv", fixtures.HEADER)
        ok = (qc.CORRECT, "", "S")
        for i in range(qc.MAX_SHAPES + 50):
            collector.merged_row(csv_input, i, row(1, 1, "$.Item.p%d" % i, "v"), ok, ok)
        self.assertEqual(len(collector.shapes), qc.MAX_SHAPES + 1)
        self.assertEqual(collector.shapes["(other shapes)"].rows, 50)


class ValueShapeTest(DebugTestCase):
    def test_boolean_spelling_is_visible(self):
        docs = [doc(active={"BOOL": True})]
        self.write(docs, docs, [row(1, 1, "$.Item.active", "True")])
        paste, _ = self.blocks()
        self.assertIn("BOOL rows=1: Aaaa(1)", paste)

    def test_dates_and_datatype_cross_tab(self):
        docs = [doc(startDate=S("2024-01-25"), other=S("abc"))]
        rows = [row(1, 1, "$.Item.startDate", "2024-01-25", datatype="date"),
                row(1, 1, "$.Item.other", "abc", datatype="string")]
        self.write(docs, docs, rows)
        paste, _ = self.blocks()
        self.assertTrue(any(l.startswith("S rows=2:") and "9999-99-99(1)" in l for l in paste), paste)
        self.assertTrue(any("date/S 1" in l and "string/S 1" in l for l in paste), paste)

    def test_source_types_found_at_the_paths(self):
        docs = [doc(s=S("x"), n=N("5"), b={"BOOL": False}, z={"NULL": True}, m=M({"a": S("1")}), l=L([S("x")]))]
        rows = [row(1, 1, "$.Item.s", "x"), row(1, 1, "$.Item.n", "5"), row(1, 1, "$.Item.b", "false"),
                row(1, 1, "$.Item.z", ""), row(1, 1, "$.Item.m", '{"a":"1"}'), row(1, 1, "$.Item.l", '["x"]')]
        self.write(docs, docs, rows)
        collector, _ = self.collect()
        self.assertEqual(dict(collector.value_rows), {"S": 1, "N": 1, "BOOL": 1, "NULL": 1, "M": 1, "L": 1})
        self.assertIn("(empty)", collector.value_shapes["NULL"])

    def test_rows_whose_path_was_not_found_have_no_source_type(self):
        docs = [doc(a=S("x"))]
        self.write(docs, docs, [row(1, 1, "$.Item.missing", "x")])
        collector, _ = self.collect()
        self.assertEqual(sum(collector.value_rows.values()), 0)
        paste, _ = self.blocks()
        self.assertIn("(no value was found at any checked path)", paste)

    def test_missing_datatype_column_is_fine(self):
        docs = [doc(a=S("x"))]
        header = [h for h in fixtures.HEADER if h != "DataType"]
        rows = [[v for h, v in zip(fixtures.HEADER, row(1, 1, "$.Item.a", "x")) if h != "DataType"]]
        fixtures.write_parquet(self.folder / "abc123_original.parquet", docs)
        fixtures.write_parquet(self.folder / "abc123_relevant.parquet", docs)
        fixtures.write_csv(self.folder / "abc123_extracted.csv", rows, header)
        paste, _ = self.blocks()
        self.assertFalse(any(l.startswith("-- DataType") for l in paste))

    def test_distinct_shapes_are_bounded(self):
        collector = qc.DebugCollector()
        csv_input = qc.CsvInput(self.folder / "x.csv", fixtures.HEADER)
        ok = (qc.CORRECT, "", "S")
        for i in range(qc.MAX_VALUE_SHAPES + 100):
            collector.merged_row(csv_input, i, row(1, 1, "$.Item.p", "x" * (i + 1)), ok, ok)
        self.assertLessEqual(len(collector.value_shapes["S"]), qc.MAX_VALUE_SHAPES)
        self.assertEqual(collector.value_rows["S"], qc.MAX_VALUE_SHAPES + 100)


class FailureTraceTest(DebugTestCase):
    def test_path_that_stops_partway(self):
        docs = [doc(scopeIds=L([M({"partnerId": S("A1")})]))]
        self.write(docs, docs, [row(1, 1, "$.Item.scopeIds[0].settings.agencyTIN", "1")])
        paste, _ = self.blocks()
        trace = [l for l in paste if l.startswith("row 1 original")][0]
        self.assertEqual(trace, "row 1 original line 1 PATH_NOT_FOUND: $.Item.scopeIds[0] resolved, stopped at settings")

    def test_value_mismatch_is_masked(self):
        docs = [doc(createdBy=S("alice"))]
        self.write(docs, docs, [row(1, 1, "$.Item.createdBy", "bob")])
        paste, _ = self.blocks()
        trace = [l for l in paste if l.startswith("row 1 original")][0]
        self.assertEqual(trace, "row 1 original line 1 VALUE_MISMATCH: expected 'aaa' (3 chars) vs found 'aaaaa' (5 chars)")
        self.assertNotIn("alice", "\n".join(paste))

    def test_show_values_reveals_the_values_in_the_report_only(self):
        docs = [doc(createdBy=S("alice"))]
        self.write(docs, docs, [row(1, 1, "$.Item.createdBy", "bob")])
        paste, full = self.blocks(show_values=True)
        self.assertTrue(any("expected 'bob' (3 chars) vs found 'alice' (5 chars)" in l for l in full))
        self.assertFalse(any("bob" in l or "alice" in l for l in paste))

    def test_off_by_one_hint_is_in_the_trace(self):
        docs = [doc(plainId=S("P1")), doc(plainId=S("P2"))]
        self.write(docs, docs, [row(2, 1, "$.Item.plainId", "P1")])
        paste, _ = self.blocks()
        trace = [l for l in paste if l.startswith("row 1 original")][0]
        self.assertTrue(trace.endswith("| FOUND_AT_LINE_1"), trace)

    def test_other_reason_codes(self):
        docs = [doc(plainId=S("P1"))]
        rows = [row(99, 1, "$.Item.plainId", "P1"), row("abc", 1, "$.Item.plainId", "P1"),
                row(1, 1, "Item..x", "P1")]
        self.write(docs, docs, rows)
        collector, _ = self.collect()
        text = "\n".join(collector.report(qc.Summary(), "x", paste=False))
        self.assertIn("row 1 original line 99 LINE_OUT_OF_RANGE", text)
        self.assertIn("row 2 original line aaa INVALID_LINE", text)
        self.assertIn("row 3 original line 1 UNSUPPORTED_PATH: path shape Aaaa..a", text)

    def test_skipped_rows_have_no_trace(self):
        docs = [doc(plainId=S("P1"))]
        self.write(docs, docs, [row(1, 1, "$.Item.plainId", "nope")])
        collector, _ = self.collect("--skip-original-on-relevant-failure")
        self.assertEqual(len(collector.traces["original"]), 0)
        self.assertEqual(len(collector.traces["relevant"]), 1)

    def test_paste_has_at_most_three_traces_with_different_reasons_first(self):
        docs = [doc(a=S("1"), b=S("1"))]
        rows = ([row(1, 1, "$.Item.a", "x")] * 6 + [row(1, 1, "$.Item.zz", "x")] * 4
                + [row(99, 1, "$.Item.a", "1")] * 3 + [row("q", 1, "$.Item.a", "1")] * 2 + [row(1, 1, "Item..", "1")])
        self.write(docs, docs, rows)
        paste, _ = self.blocks()
        start = paste.index("-- failure traces")
        traces = paste[start + 1:]
        self.assertEqual(len(traces), 3)
        codes = [t.split()[5].rstrip(":") for t in traces]
        self.assertEqual(len(set(codes)), 3, traces)

    def test_traces_are_capped_per_code_and_per_lane(self):
        docs = [doc(a=S("1"))]
        self.write(docs, docs, [row(1, 1, "$.Item.a", "x")] * 120)
        collector, _ = self.collect()
        self.assertEqual(len(collector.traces["original"]), qc.TRACES_PER_CODE)
        many_codes = [row(1, 1, "$.Item.a", "x")] * 30 + [row(1, 1, "$.Item.nope", "x")] * 30
        many_codes += [row(99, 1, "$.Item.a", "1")] * 30 + [row("q", 1, "$.Item.a", "1")] * 30 + [row(1, 1, "Item..", "1")] * 30
        self.write(docs, docs, many_codes)
        collector, _ = self.collect()
        for lane in qc.LANE_FILES:
            self.assertLessEqual(len(collector.traces[lane]), qc.TRACES_PER_LANE)

    def test_traces_cover_only_the_rows_that_were_processed(self):
        fixtures.write_fixture(self.folder)
        collector, _ = self.collect("--rows", "9,14")
        rows = {t["row"] for lane in qc.LANE_FILES for t in collector.traces[lane]}
        self.assertEqual(rows, {9, 14})

    def test_no_failures(self):
        docs = [doc(a=S("1"))]
        self.write(docs, docs, [row(1, 1, "$.Item.a", "1")])
        paste, _ = self.blocks()
        self.assertEqual(paste[-2:], ["-- failure traces", "(no failures)"])


class SizeLimitTest(DebugTestCase):
    def heavy_inputs(self):
        attrs = {}
        for i in range(60):
            attrs["attributeNumber%02d" % i] = L([M({"nestedField%d" % j: S("v") for j in range(6)})])
        docs = [doc(**attrs)]
        rows = []
        for i in range(60):
            for j in range(6):
                rows.append(row(1, 1, "$.Item.attributeNumber%02d[0].nestedField%d.with.a.rather.long.tail.here" % (i, j), "x" * (j + 1)))
        rows += [row(99, 1, "$.Item.attributeNumber00[0].nestedField0", "1"), row("q", 1, "$.Item.a", "1")]
        self.write(docs, docs, rows)

    def test_paste_block_stays_within_the_limits_with_large_inputs(self):
        self.heavy_inputs()
        paste, full = self.blocks()
        self.assertLessEqual(len(paste), qc.PASTE_MAX_LINES)
        self.assertLessEqual(max(len(l) for l in paste), qc.PASTE_MAX_WIDTH)
        self.assertGreater(len(full), len(paste))

    def test_paste_block_is_printed_by_main_within_the_limits(self):
        self.heavy_inputs()
        _, out, _ = self.run_main("--debug")
        block = out[out.index("extraction_qc debug"):].split("\ntotal:")[0].splitlines()   # the block ends before the total line
        self.assertLessEqual(len(block), qc.PASTE_MAX_LINES)
        self.assertLessEqual(max(len(l) for l in block), qc.PASTE_MAX_WIDTH)

    def test_report_with_extreme_collector_contents_is_cut_to_fit(self):
        collector = qc.DebugCollector()
        collector.profile = {lane: {"rows": 10 ** 9, "groups": 10 ** 6, "column": "c" * 300, "type": "string"}
                             for lane in qc.LANE_FILES}
        collector.skeletons = {lane: ["Item.%s:%s" % ("k" * 90, "S") for _ in range(200)] for lane in qc.LANE_FILES}
        for i in range(5000):
            stats = collector.shapes["$.Item.%s" % ("s" * 200 + str(i))] = qc.ShapeStats()
            stats.rows, stats.failing = 10 ** 6, i
            stats.reasons["relevant"]["PATH_NOT_FOUND"] += 1
            stats.reasons["original"]["VALUE_MISMATCH"] += 1
        for tag in ("S", "N", "BOOL", "NULL", "M", "L", "SS", "NS"):
            collector.value_rows[tag] = 10 ** 6
            for i in range(10):
                collector.value_shapes[tag]["Aa9" * 13 + str(i)] += 1
        for i in range(200):
            collector.crosstab[("dtype%d" % i, "S")] += 1000
        summary = qc.Summary()
        for lane in qc.LANE_FILES:
            for code in ("A" * 40, "B" * 40, "C" * 40):
                summary.reasons[lane][code] = 10 ** 6
        paste = collector.report(summary, "x.csv", paste=True)
        self.assertLessEqual(len(paste), qc.PASTE_MAX_LINES)
        self.assertLessEqual(max(len(l) for l in paste), qc.PASTE_MAX_WIDTH)


class DebugSwitchTest(DebugTestCase):
    def test_debug_writes_the_report_and_prints_the_block(self):
        fixtures.write_fixture(self.folder)
        code, out, _ = self.run_main("--debug")
        self.assertEqual(code, 0)
        self.assertTrue((self.folder / "abc123_verified.csv").is_file())
        self.assertTrue((self.folder / "abc123_debug_report.txt").is_file())
        self.assertIn("extraction_qc debug |", out)
        self.assertIn("-- path shapes", out)

    def test_without_debug_there_is_no_report(self):
        fixtures.write_fixture(self.folder)
        _, out, _ = self.run_main()
        self.assertFalse((self.folder / "abc123_debug_report.txt").exists())
        self.assertNotIn("extraction_qc debug", out)

    def test_verdicts_are_identical_with_and_without_debug(self):
        fixtures.write_fixture(self.folder)
        self.run_main()
        plain = (self.folder / "abc123_verified.csv").read_bytes()
        for extra in (["--debug"], ["--debug", "--show-values"]):
            self.run_main(*extra)
            self.assertEqual((self.folder / "abc123_verified.csv").read_bytes(), plain, extra)

    def test_verdicts_are_identical_with_skip_on_failure_too(self):
        fixtures.write_fixture(self.folder)
        self.run_main("--skip-original-on-relevant-failure")
        plain = (self.folder / "abc123_verified.csv").read_bytes()
        self.run_main("--skip-original-on-relevant-failure", "--debug")
        self.assertEqual((self.folder / "abc123_verified.csv").read_bytes(), plain)

    def test_report_file_matches_the_full_report(self):
        fixtures.write_fixture(self.folder)
        self.run_main("--debug")
        text = (self.folder / "abc123_debug_report.txt").read_text(encoding="utf-8")
        self.assertIn("input: abc123_extracted.csv", text)
        self.assertIn("values masked", text)
        self.assertNotIn("alice", text)
        self.run_main("--debug", "--show-values")
        text = (self.folder / "abc123_debug_report.txt").read_text(encoding="utf-8")
        self.assertIn("alice", text)
        self.assertIn("values REAL (do not share)", text)

    def test_show_values_without_debug_warns(self):
        fixtures.write_fixture(self.folder)
        _, _, err = self.run_main("--show-values")
        self.assertIn("--show-values has no effect without --debug", err)

    def test_a_failed_run_writes_no_report(self):
        paths = fixtures.write_fixture(self.folder)
        paths["relevant"].unlink()
        code, _, _ = self.run_main("--debug")
        self.assertEqual(code, 1)
        self.assertFalse((self.folder / "abc123_debug_report.txt").exists())


if __name__ == "__main__":
    unittest.main()

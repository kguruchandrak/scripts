import unittest

import extraction_qc as qc


def walk(doc_text, path):
    return qc.resolve(qc.parse_doc(doc_text), qc.parse_path(path))


class PathResolutionTest(unittest.TestCase):
    def test_typed_document(self):
        doc = '{"Item":{"scopeIds":{"L":[{"M":{"partnerId":{"S":"A1"}}}]}}}'
        self.assertEqual(walk(doc, "$.Item.scopeIds[0].partnerId"), ("S", "A1"))

    def test_plain_document(self):
        doc = '{"Item":{"scopeIds":[{"partnerId":"A1"}]}}'
        self.assertEqual(walk(doc, "$.Item.scopeIds[0].partnerId"), ("S", "A1"))

    def test_index_beyond_the_array_is_not_found(self):
        doc = '{"Item":{"scopeIds":{"L":[{"M":{"partnerId":{"S":"A1"}}}]}}}'
        with self.assertRaises(qc.PathNotFound) as ctx:
            walk(doc, "$.Item.scopeIds[3].partnerId")
        self.assertEqual(ctx.exception.walked, "$.Item.scopeIds")
        self.assertEqual(ctx.exception.stopped_at, "[3]")
        self.assertIn("1-element", str(ctx.exception))

    def test_missing_key_reports_where_the_walk_stopped(self):
        doc = '{"Item":{"scopeIds":{"L":[{"M":{"partnerId":{"S":"A1"}}}]}}}'
        with self.assertRaises(qc.PathNotFound) as ctx:
            walk(doc, "$.Item.scopeIds[0].settings.agencyTIN")
        self.assertEqual(ctx.exception.walked, "$.Item.scopeIds[0]")
        self.assertEqual(ctx.exception.stopped_at, "settings")

    def test_key_on_a_scalar_is_not_found(self):
        with self.assertRaises(qc.PathNotFound):
            walk('{"Item":{"a":{"S":"x"}}}', "$.Item.a.b")

    def test_index_on_a_map_is_not_found(self):
        with self.assertRaises(qc.PathNotFound):
            walk('{"Item":{"a":{"M":{"b":{"S":"x"}}}}}', "$.Item.a[0]")

    def test_attribute_literally_named_s_is_not_mistaken_for_a_wrapper(self):
        doc = '{"Item":{"S":{"S":"inner"}}}'
        self.assertEqual(walk(doc, "$.Item.S"), ("S", "inner"))

    def test_attribute_named_l_with_a_typed_payload(self):
        doc = '{"Item":{"L":{"S":"x"}}}'
        self.assertEqual(walk(doc, "$.Item.L"), ("S", "x"))

    def test_number_text_is_preserved_in_typed_and_plain_documents(self):
        typed = walk('{"Item":{"n":{"N":"12345.0"}}}', "$.Item.n")
        plain = walk('{"Item":{"n":12345.0}}', "$.Item.n")
        self.assertEqual(typed, ("N", "12345.0"))
        self.assertEqual(plain, ("N", "12345.0"))
        self.assertIsInstance(plain[1], qc.NumText)

    def test_scalar_tags(self):
        doc = '{"Item":{"b":{"BOOL":true},"n":{"NULL":true},"s":{"S":""},"p":true,"q":null}}'
        self.assertEqual(walk(doc, "$.Item.b"), ("BOOL", True))
        self.assertEqual(walk(doc, "$.Item.n"), ("NULL", None))
        self.assertEqual(walk(doc, "$.Item.s"), ("S", ""))
        self.assertEqual(walk(doc, "$.Item.p"), ("BOOL", True))
        self.assertEqual(walk(doc, "$.Item.q"), ("NULL", None))

    def test_containers_and_sets(self):
        doc = '{"Item":{"m":{"M":{"a":{"S":"1"}}},"l":{"L":[{"S":"x"},{"N":"2"}]},"ss":{"SS":["p","q"]}}}'
        self.assertEqual(walk(doc, "$.Item.m")[0], "M")
        self.assertEqual(qc.plain_of(*walk(doc, "$.Item.m")), {"a": "1"})
        self.assertEqual(qc.plain_of(*walk(doc, "$.Item.l")), ["x", "2"])
        self.assertEqual(walk(doc, "$.Item.ss"), ("SS", ["p", "q"]))
        self.assertEqual(walk(doc, "$.Item.ss[1]"), ("S", "q"))

    def test_bracket_quoted_key(self):
        doc = '{"Item":{"a.b":{"S":"dotted"}}}'
        self.assertEqual(walk(doc, "$.Item['a.b']"), ("S", "dotted"))

    def test_double_quoted_bracket_key(self):
        doc = '{"Item":{"a.b":{"S":"dotted"}}}'
        self.assertEqual(walk(doc, '$.Item["a.b"]'), ("S", "dotted"))
        self.assertEqual(walk(doc, '$["Item"]["a.b"]'), ("S", "dotted"))
        self.assertEqual(walk(doc, "$['Item']['a.b']"), ("S", "dotted"))

    def test_both_quote_styles_in_one_path(self):
        doc = '{"Item":{"x y":{"M":{"p.q":{"S":"deep"}}}}}'
        self.assertEqual(walk(doc, "$.Item['x y'][\"p.q\"]"), ("S", "deep"))

    def test_path_without_dollar_prefix(self):
        self.assertEqual(walk('{"Item":{"a":{"S":"x"}}}', "Item.a"), ("S", "x"))


class PathParsingTest(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(qc.parse_path("$.Item.scopeIds[0].enrollSettings[12].carrierId"),
                         ("Item", "scopeIds", 0, "enrollSettings", 12, "carrierId"))

    def test_render_path_round_trip(self):
        path = "$.Item.scopeIds[0].partnerId"
        self.assertEqual(qc.render_path(qc.parse_path(path)), path)

    def test_unsupported_paths(self):
        for bad in ("Item..x", "$", "", "   ", "$.Item[", "$.Item.a[x]", "$..Item", '$["a"', '$["a\'] ', "$['a\"]"):
            with self.assertRaises(qc.UnsupportedPath, msg=bad):
                qc.parse_path(bad)


class ParseDocTest(unittest.TestCase):
    def test_invalid_json(self):
        for raw in ("{not json", "", None, "[1,"):
            with self.assertRaises(qc.InvalidJson, msg=repr(raw)):
                qc.parse_doc(raw)

    def test_bytes_are_decoded_as_utf8(self):
        self.assertEqual(qc.parse_doc('{"Item":{"a":"é"}}'.encode("utf-8")), {"Item": {"a": "é"}})

    def test_undecodable_bytes_are_invalid_json(self):
        with self.assertRaises(qc.InvalidJson):
            qc.parse_doc(b"\xff\xfe{")

    def test_double_encoded_json(self):
        import json
        inner = json.dumps({"Item": {"a": {"S": "x"}}})
        self.assertEqual(qc.parse_doc(json.dumps(inner)), {"Item": {"a": {"S": "x"}}})


if __name__ == "__main__":
    unittest.main()

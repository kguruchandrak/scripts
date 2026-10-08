import unittest

import extraction_qc as qc


def match(expected, doc_text, path="$.Item.v"):
    tag, value = qc.resolve(qc.parse_doc(doc_text), qc.parse_path(path))
    return qc.values_match(expected, tag, value)


class StrictComparisonTest(unittest.TestCase):
    def test_identical_string(self):
        self.assertTrue(match("P1", '{"Item":{"v":{"S":"P1"}}}'))

    def test_number_reformatted_does_not_match(self):
        self.assertFalse(match("12345", '{"Item":{"v":{"N":"12345.0"}}}'))
        self.assertTrue(match("12345.0", '{"Item":{"v":{"N":"12345.0"}}}'))

    def test_plain_json_number_keeps_its_text(self):
        self.assertTrue(match("12345.0", '{"Item":{"v":12345.0}}'))
        self.assertFalse(match("12345", '{"Item":{"v":12345.0}}'))

    def test_whitespace_trimmed_does_not_match(self):
        self.assertFalse(match("P1", '{"Item":{"v":{"S":" P1 "}}}'))
        self.assertTrue(match(" P1 ", '{"Item":{"v":{"S":" P1 "}}}'))

    def test_case_matters(self):
        self.assertFalse(match("p1", '{"Item":{"v":{"S":"P1"}}}'))

    def test_booleans_are_true_or_false(self):
        self.assertTrue(match("true", '{"Item":{"v":{"BOOL":true}}}'))
        self.assertTrue(match("false", '{"Item":{"v":{"BOOL":false}}}'))
        self.assertFalse(match("True", '{"Item":{"v":{"BOOL":true}}}'))

    def test_null_is_empty_text(self):
        self.assertTrue(match("", '{"Item":{"v":{"NULL":true}}}'))
        self.assertFalse(match("null", '{"Item":{"v":{"NULL":true}}}'))

    def test_missing_expected_value_counts_as_empty(self):
        self.assertTrue(qc.values_match(None, "NULL", None))

    def test_map_with_different_spacing_and_key_order_matches(self):
        doc = '{"Item":{"v":{"M":{"a":{"S":"1"},"b":{"S":"2"}}}}}'
        self.assertTrue(match('{"a":"1","b":"2"}', doc))
        self.assertTrue(match('{ "b": "2",  "a": "1" }', doc))

    def test_a_number_inside_a_map_is_not_a_string(self):
        number = '{"Item":{"v":{"M":{"a":{"N":"1"}}}}}'
        string = '{"Item":{"v":{"M":{"a":{"S":"1"}}}}}'
        self.assertTrue(match('{"a":1}', number))
        self.assertFalse(match('{"a":"1"}', number))
        self.assertTrue(match('{"a":"1"}', string))
        self.assertFalse(match('{"a":1}', string))

    def test_a_number_inside_a_list_is_not_a_string(self):
        doc = '{"Item":{"v":{"L":[{"N":"1"},{"N":"2.0"}]}}}'
        self.assertTrue(match("[1, 2.0]", doc))
        self.assertFalse(match('["1","2.0"]', doc))
        self.assertFalse(match("[1, 2]", doc))         # 2 is not 2.0

    def test_dynamodb_form_keeps_the_number_string_difference(self):
        number = '{"Item":{"v":{"M":{"a":{"N":"1"}}}}}'
        self.assertTrue(match('{"a":{"N":"1"}}', number))
        self.assertFalse(match('{"a":{"S":"1"}}', number))

    def test_booleans_and_strings_inside_a_map_differ(self):
        doc = '{"Item":{"v":{"M":{"a":{"BOOL":true}}}}}'
        self.assertTrue(match('{"a":true}', doc))
        self.assertFalse(match('{"a":"true"}', doc))
        self.assertFalse(match('{"a":1}', doc))

    def test_plain_json_number_inside_a_map(self):
        self.assertTrue(match('{"a":1}', '{"Item":{"v":{"a":1}}}'))
        self.assertFalse(match('{"a":"1"}', '{"Item":{"v":{"a":1}}}'))

    def test_nested_structures_and_nulls(self):
        doc = '{"Item":{"v":{"M":{"a":{"L":[{"M":{"b":{"NULL":true}}}]}}}}}'
        self.assertTrue(match('{"a":[{"b":null}]}', doc))
        self.assertFalse(match('{"a":[{"b":""}]}', doc))

    def test_json_equal_directly(self):
        n, s = qc.NumText("1"), "1"
        self.assertFalse(qc.json_equal(n, s))
        self.assertFalse(qc.json_equal({"k": n}, {"k": s}))
        self.assertTrue(qc.json_equal({"k": [n, s]}, {"k": [qc.NumText("1"), "1"]}))
        self.assertFalse(qc.json_equal({"k": 1}, {"j": 1}))
        self.assertFalse(qc.json_equal([1], [1, 2]))

    def test_map_given_in_dynamodb_form_matches(self):
        doc = '{"Item":{"v":{"M":{"a":{"S":"1"}}}}}'
        self.assertTrue(match('{"a": {"S": "1"}}', doc))

    def test_plain_map_with_other_spacing(self):
        self.assertTrue(match('{"a": "1"}', '{"Item":{"v":{"a":"1"}}}'))

    def test_map_with_different_content_does_not_match(self):
        self.assertFalse(match('{"a":"2"}', '{"Item":{"v":{"M":{"a":{"S":"1"}}}}}'))

    def test_list_matches_in_order(self):
        doc = '{"Item":{"v":{"L":[{"S":"x"},{"S":"y"}]}}}'
        self.assertTrue(match('["x","y"]', doc))
        self.assertFalse(match('["y","x"]', doc))

    def test_container_value_that_is_not_json_does_not_match(self):
        self.assertFalse(match("not json", '{"Item":{"v":{"M":{"a":{"S":"1"}}}}}'))


class ClassifierTest(unittest.TestCase):
    def test_reformatted_number(self):
        self.assertEqual(qc.classify_mismatch("12345", "12345.0"), "FORMAT_CHANGED")
        self.assertEqual(qc.classify_mismatch("007", "7"), "FORMAT_CHANGED")
        self.assertEqual(qc.classify_mismatch("1e3", "1000"), "FORMAT_CHANGED")

    def test_reformatted_date(self):
        self.assertEqual(qc.classify_mismatch("01/25/2024", "2024-01-25"), "FORMAT_CHANGED")
        self.assertEqual(qc.classify_mismatch("2024-01-25T00:00:00", "2024-01-25"), "FORMAT_CHANGED")
        self.assertEqual(qc.classify_mismatch("25 Jan 2024", "2024-01-25"), "FORMAT_CHANGED")

    def test_different_dates_are_a_different_value(self):
        self.assertEqual(qc.classify_mismatch("01/26/2024", "2024-01-25"), "VALUE_MISMATCH")

    def test_whitespace_and_case(self):
        self.assertEqual(qc.classify_mismatch("P1", " P1 "), "FORMAT_CHANGED")
        self.assertEqual(qc.classify_mismatch("Alice", "alice"), "FORMAT_CHANGED")
        self.assertEqual(qc.classify_mismatch("True", "true"), "FORMAT_CHANGED")

    def test_null_spellings(self):
        self.assertEqual(qc.classify_mismatch("null", ""), "FORMAT_CHANGED")
        self.assertEqual(qc.classify_mismatch("None", ""), "FORMAT_CHANGED")

    def test_different_value(self):
        self.assertEqual(qc.classify_mismatch("bob", "alice"), "VALUE_MISMATCH")
        self.assertEqual(qc.classify_mismatch("12346", "12345.0"), "VALUE_MISMATCH")
        self.assertEqual(qc.classify_mismatch("abc", ""), "VALUE_MISMATCH")

    def test_non_numbers_are_not_compared_as_numbers(self):
        self.assertEqual(qc.classify_mismatch("1_000", "1000"), "VALUE_MISMATCH")
        self.assertEqual(qc.classify_mismatch("nan", "NaN1"), "VALUE_MISMATCH")


class ReasonTextTest(unittest.TestCase):
    def test_value_mismatch_shows_expected_and_found(self):
        reason = qc.mismatch_reason("bob", "S", "alice")
        self.assertTrue(reason.startswith("VALUE_MISMATCH:"))
        self.assertIn("'bob'", reason)
        self.assertIn("'alice'", reason)

    def test_format_changed_reason(self):
        reason = qc.mismatch_reason("12345", "N", qc.NumText("12345.0"))
        self.assertTrue(reason.startswith("FORMAT_CHANGED:"))
        self.assertIn("'12345.0'", reason)

    def test_container_mismatch_is_always_a_value_mismatch(self):
        reason = qc.mismatch_reason('{"a":"2"}', "M", {"a": {"S": "1"}})
        self.assertTrue(reason.startswith("VALUE_MISMATCH:"))
        self.assertIn('{"a":"1"}', reason)

    def test_long_values_are_clipped(self):
        reason = qc.mismatch_reason("x" * 500, "S", "y" * 500)
        self.assertLess(len(reason), 250)


if __name__ == "__main__":
    unittest.main()

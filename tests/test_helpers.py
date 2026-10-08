"""Unit tests for the small pure helpers around extraction, sheets and search."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from jotra.extraction import field_description, parse_json_array
from jotra.search import url_tokens
from jotra.sheets import formula_separator_for_locale, resolve_worksheet_name


class ParseJsonArrayTests(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(parse_json_array('[{"position": "Engineer"}]'), [{"position": "Engineer"}])

    def test_strips_markdown_fence(self):
        raw = '```json\n[{"position": "Engineer"}]\n```'
        self.assertEqual(parse_json_array(raw), [{"position": "Engineer"}])

    def test_non_array_exits(self):
        with self.assertRaises(SystemExit):
            parse_json_array('{"position": "Engineer"}')

    def test_invalid_json_exits(self):
        with self.assertRaises(SystemExit):
            parse_json_array("not json")


class FieldDescriptionTests(unittest.TestCase):
    def test_known_field_uses_builtin_description(self):
        self.assertEqual(field_description({"source": "employer"}), "the company/organization name")

    def test_custom_description_wins(self):
        self.assertEqual(field_description({"source": "employer", "description": "who"}), "who")

    def test_unknown_field_without_description_exits(self):
        with self.assertRaises(SystemExit):
            field_description({"source": "shift"})


class SheetsHelperTests(unittest.TestCase):
    def test_formula_separator_depends_on_locale(self):
        self.assertEqual(formula_separator_for_locale("en_US"), ",")
        self.assertEqual(formula_separator_for_locale("de_DE"), ";")
        self.assertEqual(formula_separator_for_locale(""), ";")

    def test_resolve_worksheet_name_test_and_prod(self):
        profile = {"name": "p", "worksheet_name": "Jobs", "TEST_worksheet_name": "test"}
        self.assertEqual(resolve_worksheet_name(profile, use_test=True), "test")
        self.assertEqual(resolve_worksheet_name(profile, use_test=False), "Jobs")

    def test_resolve_worksheet_name_falls_back_without_test_tab(self):
        profile = {"name": "p", "worksheet_name": "Jobs"}
        self.assertEqual(resolve_worksheet_name(profile, use_test=True), "Jobs")


class UrlTokensTests(unittest.TestCase):
    def test_extracts_job_ids_and_ignores_short_words(self):
        tokens = url_tokens("https://boards.greenhouse.io/acme/jobs/4802146007?gh_src=abc")
        self.assertIn("4802146007", tokens)
        self.assertNotIn("acme", tokens)
        self.assertNotIn("jobs", tokens)


if __name__ == "__main__":
    unittest.main()

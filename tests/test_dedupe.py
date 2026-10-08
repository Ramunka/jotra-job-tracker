"""Unit tests for the normalize/dedupe logic.

Uses tests/fixtures/expected_jobs.json as a stand-in for what extract_jobs() would
return after parsing Claude's response for tests/fixtures/sample_input.txt, so these
tests don't require network access or an API key.

Column/dedupe-key config comes from tests/fixtures/profile.json, a copy of the
"example" profile in profiles.example.json.
"""

import json
import os
import sys
import unittest
from urllib.parse import quote_plus

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from jotra.console import ANSI_RED
from jotra.dedupe import dedupe_jobs, describe_job
from jotra.extraction import extraction_fields, normalize_job
from jotra.sheets import job_row, link_cell, load_existing_records, search_link

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_expected_jobs():
    with open(os.path.join(FIXTURES_DIR, "expected_jobs.json"), encoding="utf-8") as f:
        return json.load(f)


def load_default_profile():
    with open(os.path.join(FIXTURES_DIR, "profile.json"), encoding="utf-8") as f:
        return json.load(f)


class FakeWorksheet:
    """Minimal stand-in for a gspread Worksheet, only implementing get_all_records."""

    def __init__(self, records):
        self._records = records

    def get_all_records(self, expected_headers=None):
        return self._records


def existing_record(row_number: int, **record) -> dict:
    """Build one existing_records entry (as load_existing_records() would), from
    header-text kwargs, e.g. existing_record(2, position="Engineer", employer="Acme")."""
    return {"row_number": row_number, "record": record}


class DedupeJobsTests(unittest.TestCase):
    def setUp(self):
        profile = load_default_profile()
        self.columns = profile["columns"]
        self.dedupe_keys = profile["dedupe_keys"]
        self.fields = extraction_fields(self.columns)

    def test_all_new_jobs_are_added(self):
        jobs = load_expected_jobs()
        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            jobs, {}, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(len(added), 14)
        self.assertEqual(len(new_rows), 14)
        self.assertEqual(updates, [])
        self.assertEqual(skipped_duplicates, [])
        self.assertEqual(skipped_invalid, [])

        first_row = new_rows[0]
        self.assertEqual(len(first_row), len(self.columns))
        self.assertEqual(first_row[0], "2026-09-17")
        self.assertEqual(
            first_row[8],
            '=HYPERLINK("https://job-boards.greenhouse.io/andurilindustries/jobs/4802146007", '
            '"job-boards.greenhouse.io")',
        )
        self.assertIn('"SEARCH"', first_row[9])  # search link is always populated too
        self.assertEqual(first_row[10], "NEW")
        self.assertEqual(first_row[11], "")

    def test_job_already_in_sheet_with_a_link_is_skipped(self):
        jobs = load_expected_jobs()
        existing_records = {
            ("2026 early career software engineer", "anduril industries"): existing_record(
                2,
                position="2026 Early Career Software Engineer",
                employer="Anduril Industries",
                **{"direct link": "job-boards.greenhouse.io"},
            )
        }

        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            jobs, existing_records, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(len(added), 13)
        self.assertEqual(updates, [])
        self.assertEqual(len(skipped_duplicates), 1)
        self.assertEqual(skipped_duplicates[0]["employer"], "Anduril Industries")

    def test_job_already_in_sheet_without_a_link_is_upgraded(self):
        jobs = load_expected_jobs()
        existing_records = {
            ("2026 early career software engineer", "anduril industries"): existing_record(
                7,
                position="2026 Early Career Software Engineer",
                employer="Anduril Industries",
                **{
                    "direct link": "",
                    "date added": "2026-01-01",
                    "STATUS": "Applied",
                    "COMMENTS": "Nice role",
                },
            )
        }

        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            jobs, existing_records, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(len(added), 13)  # the Anduril job isn't re-added as a new row
        self.assertEqual(skipped_duplicates, [])
        self.assertEqual(len(updates), 1)

        row_number, row_values, job = updates[0]
        self.assertEqual(row_number, 7)
        self.assertEqual(job["employer"], "Anduril Industries")
        self.assertIn("job-boards.greenhouse.io", row_values[8])  # new direct link written
        # date_added / STATUS / COMMENTS survive untouched from the existing row
        self.assertEqual(row_values[0], "2026-01-01")
        self.assertEqual(row_values[10], "Applied")
        self.assertEqual(row_values[11], "Nice role")

    def test_duplicate_within_same_batch_is_skipped(self):
        jobs = load_expected_jobs()
        jobs_with_dupe = jobs + [jobs[0]]

        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            jobs_with_dupe, {}, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(len(added), 14)
        self.assertEqual(updates, [])
        self.assertEqual(len(skipped_duplicates), 1)

    def test_batch_duplicate_without_link_is_upgraded_by_later_one_with_link(self):
        no_link_job = {
            "domain": "X",
            "employer": "Acme",
            "position": "Engineer",
            "location": "Remote",
            "salary": "",
            "qualifications": "",
            "summary": "",
            "link": "NONE",
        }
        with_link_job = {**no_link_job, "link": "https://boards.greenhouse.io/acme/jobs/1"}

        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            [no_link_job, with_link_job], {}, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(len(added), 1)
        self.assertEqual(updates, [])
        self.assertEqual(skipped_duplicates, [])
        self.assertIn("boards.greenhouse.io", new_rows[0][8])

    def test_dedupe_is_case_and_whitespace_insensitive(self):
        jobs = load_expected_jobs()
        existing_records = {
            (
                "  2026 EARLY CAREER software engineer  ".strip().lower(),
                "anduril industries",
            ): existing_record(2, **{"direct link": "job-boards.greenhouse.io"})
        }

        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            jobs, existing_records, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(len(added), 13)
        self.assertEqual(len(skipped_duplicates), 1)

    def test_job_missing_position_or_employer_is_invalid(self):
        jobs = [
            {
                "domain": "X",
                "employer": "",
                "position": "Engineer",
                "location": "",
                "salary": "",
                "qualifications": "",
                "summary": "",
                "link": "",
            },
            {
                "domain": "X",
                "employer": "Acme",
                "position": "",
                "location": "",
                "salary": "",
                "qualifications": "",
                "summary": "",
                "link": "",
            },
        ]

        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            jobs, {}, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(added, [])
        self.assertEqual(updates, [])
        self.assertEqual(len(skipped_invalid), 2)

    def test_load_existing_records_reads_position_and_employer_columns(self):
        ws = FakeWorksheet(
            [
                {
                    "date added": "2026-01-01",
                    "domain": "X",
                    "employer": "Acme",
                    "position": "Engineer",
                    "location": "",
                    "posted or suggested salary": "",
                    "key required qualifications": "",
                    "short summary of duties": "",
                    "direct link": "",
                    "STATUS": "Applied",
                    "COMMENTS": "Followed up",
                }
            ]
        )

        records = load_existing_records(ws, self.dedupe_keys, self.columns)

        self.assertEqual(list(records.keys()), [("engineer", "acme")])
        entry = records[("engineer", "acme")]
        self.assertEqual(entry["row_number"], 2)  # header is row 1, first record is row 2
        self.assertEqual(entry["record"]["STATUS"], "Applied")

    def test_normalize_job_defaults_missing_fields_to_empty_string(self):
        job = normalize_job({"employer": "Acme", "position": "Engineer"}, self.fields)

        self.assertEqual(job["domain"], "")
        self.assertEqual(job["location"], "")
        self.assertEqual(job["salary"], "")
        self.assertEqual(job["qualifications"], "")
        self.assertEqual(job["summary"], "")
        self.assertEqual(job["link"], "")

    def test_link_cell_builds_hyperlink_formula_with_domain_as_text(self):
        cell = link_cell("https://job-boards.greenhouse.io/acme/jobs/123")

        self.assertEqual(
            cell,
            '=HYPERLINK("https://job-boards.greenhouse.io/acme/jobs/123", "job-boards.greenhouse.io")',
        )

    def test_link_cell_empty_for_missing_link(self):
        self.assertEqual(link_cell(""), "")

    def test_link_cell_empty_for_non_url_text(self):
        self.assertEqual(link_cell("Shake Shack Career Posting"), "")

    def test_link_cell_escapes_embedded_quotes(self):
        cell = link_cell('https://example.com/path?q="x"')

        self.assertEqual(cell, '=HYPERLINK("https://example.com/path?q=""x""", "example.com")')

    def test_search_link_builds_google_search_with_search_label(self):
        job = {"position": "Shift Lead", "employer": "Acme Cafe", "location": "Seattle, WA"}

        cell = search_link(job)

        expected_query = quote_plus("job Shift Lead at Acme Cafe in Seattle, WA")
        self.assertEqual(cell, f'=HYPERLINK("https://www.google.com/search?q={expected_query}", "SEARCH")')

    def test_search_link_omits_location_when_missing(self):
        job = {"position": "Shift Lead", "employer": "Acme Cafe", "location": ""}

        cell = search_link(job)

        expected_query = quote_plus("job Shift Lead at Acme Cafe")
        self.assertEqual(cell, f'=HYPERLINK("https://www.google.com/search?q={expected_query}", "SEARCH")')

    def test_job_row_direct_link_and_search_link_are_independent_columns(self):
        columns = [
            {"header": "employer", "source": "employer"},
            {"header": "position", "source": "position"},
            {"header": "direct link", "source": "link"},
            {"header": "search link", "source": "search_link"},
        ]
        job = {
            "employer": "Acme",
            "position": "Engineer",
            "location": "Remote",
            "link": "https://boards.greenhouse.io/acme/jobs/1",
        }

        row = job_row(job, "2026-09-17", columns)

        self.assertIn("boards.greenhouse.io", row[2])  # direct link: real URL
        self.assertIn('"SEARCH"', row[3])  # search link: always constructed too

    def test_job_row_direct_link_is_blank_when_no_real_link(self):
        columns = [
            {"header": "employer", "source": "employer"},
            {"header": "position", "source": "position"},
            {"header": "direct link", "source": "link"},
            {"header": "search link", "source": "search_link"},
        ]

        for missing_link in ("NONE", "", "Company Careers Page"):
            with self.subTest(link=missing_link):
                job = {"employer": "Acme", "position": "Engineer", "location": "Remote", "link": missing_link}

                row = job_row(job, "2026-09-17", columns)

                self.assertEqual(row[2], "")  # direct link: blank, not a search fallback
                self.assertIn('"SEARCH"', row[3])  # search link: still always populated

    def test_job_row_preserve_keeps_existing_date_status_comments(self):
        columns = [
            {"header": "date added", "source": "date_added"},
            {"header": "employer", "source": "employer"},
            {"header": "position", "source": "position"},
            {"header": "direct link", "source": "link"},
            {"header": "STATUS", "source": "status"},
            {"header": "COMMENTS", "source": "comments"},
        ]
        job = {
            "employer": "Acme",
            "position": "Engineer",
            "link": "https://boards.greenhouse.io/acme/jobs/1",
        }
        preserve = {
            "date added": "2026-01-01",
            "STATUS": "Applied",
            "COMMENTS": "Called recruiter",
        }

        row = job_row(job, "2026-09-17", columns, preserve=preserve)

        self.assertEqual(row[0], "2026-01-01")  # original discovery date kept, not today
        self.assertIn("boards.greenhouse.io", row[3])  # new link still applied
        self.assertEqual(row[4], "Applied")  # user's STATUS kept, not reset to "NEW"
        self.assertEqual(row[5], "Called recruiter")  # user's COMMENTS kept

    def test_describe_job_flags_missing_link(self):
        job = {"position": "Engineer", "employer": "Acme", "link": ""}

        label = describe_job(job, ["position", "employer"])

        self.assertIn("Engineer / Acme", label)
        self.assertIn(f"{ANSI_RED}No link", label)

    def test_describe_job_flags_none_link(self):
        job = {"position": "Engineer", "employer": "Acme", "link": "NONE"}

        label = describe_job(job, ["position", "employer"])

        self.assertIn("No link", label)

    def test_describe_job_no_marker_when_link_present(self):
        job = {"position": "Engineer", "employer": "Acme", "link": "https://example.com/job/1"}

        label = describe_job(job, ["position", "employer"])

        self.assertNotIn("No link", label)
        self.assertEqual(label, "Engineer / Acme")


class ProfileSchemaTests(unittest.TestCase):
    """Tests for the per-profile schema mechanism itself, using an ad-hoc second profile
    with an extra field, independent of profiles.json."""

    def setUp(self):
        self.columns = [
            {"header": "date added", "source": "date_added"},
            {"header": "employer", "source": "employer"},
            {"header": "position", "source": "position"},
            {
                "header": "referral contact",
                "source": "referral_contact",
                "description": "name of a known contact at the employer, if mentioned",
            },
            {"header": "job posting link", "source": "link"},
            {"header": "STATUS", "source": "status"},
            {"header": "COMMENTS", "source": "comments"},
        ]
        self.dedupe_keys = ["position", "employer"]
        self.fields = extraction_fields(self.columns)

    def test_extraction_fields_excludes_fixed_sources(self):
        sources = {f["source"] for f in self.fields}
        self.assertEqual(sources, {"employer", "position", "referral_contact", "link"})

    def test_extra_field_flows_through_to_row(self):
        jobs = [
            {
                "employer": "Acme",
                "position": "Engineer",
                "referral_contact": "Jane Doe",
                "link": "https://example.com/job/1",
            }
        ]

        new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
            jobs, {}, "2026-09-17", self.columns, self.dedupe_keys
        )

        self.assertEqual(len(added), 1)
        self.assertEqual(updates, [])
        row = new_rows[0]
        # columns: date_added, employer, position, referral_contact, link, status, comments
        self.assertEqual(row[3], "Jane Doe")


if __name__ == "__main__":
    unittest.main()

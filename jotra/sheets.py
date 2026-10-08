"""Google Sheets storage: connecting, reading existing rows, and building row cells."""

import os
import sys
import time
from urllib.parse import quote_plus, urlparse

import gspread
from google.oauth2.service_account import Credentials
from gspread.http_client import HTTPClient

from .console import log

# Scopes are the specific permissions we ask Google to grant our service account's
# access token. "spreadsheets" lets us read/write sheet contents; "drive.readonly" lets
# gspread look up a spreadsheet's metadata (like its locale) by ID.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# HTTP statuses worth retrying: Google's own "try again shortly" responses (timeouts,
# rate limits, and 5xx server errors like the occasional 502 Bad Gateway).
RETRYABLE_SHEETS_STATUSES = {408, 429, 500, 502, 503, 504}
SHEETS_RETRY_DELAYS = [2, 5, 15, 30]  # seconds between attempts


def formula_separator_for_locale(locale: str) -> str:
    """Pick the right argument separator for a Google Sheets formula.

    Google Sheets' formula syntax depends on the spreadsheet's locale: locales that
    use a comma as the decimal separator (most non-English locales) expect ";" between
    a function's arguments instead of ",". Get this wrong and a formula like
    =HYPERLINK("url", "text") shows #ERROR! instead of a working link.
    """
    return "," if (locale or "").lower().startswith("en") else ";"


def resolve_worksheet_name(profile: dict, use_test: bool) -> str:
    """Pick which worksheet tab to use: the profile's real one, or its test one.

    Test mode is the CLI default (see the --prod flag) precisely so an accidental or
    exploratory run can never touch real data. If a profile is missing
    "TEST_worksheet_name", we fall back to the real worksheet with a loud warning
    rather than silently writing to production anyway.
    """
    if not use_test:
        return profile.get("worksheet_name", "Sheet1")

    test_worksheet_name = profile.get("TEST_worksheet_name")
    if not test_worksheet_name:
        log(
            f"Profile '{profile.get('name')}' has no \"TEST_worksheet_name\" set — "
            "falling back to the real worksheet even though this is a test run."
        )
        return profile.get("worksheet_name", "Sheet1")
    return test_worksheet_name


class RetryingHTTPClient(HTTPClient):
    """gspread HTTP client that retries transient Google errors before giving up.

    gspread ships a BackOffHTTPClient, but it decides based on the error code parsed
    from Google's JSON error body — and a 502 comes back as an HTML page, which gspread
    records as code -1, so it never retries. We check the real HTTP status instead.
    Without this, one flaky request at the very end of a run throws away the (slow,
    paid-for) Gemini search and Claude extraction that came before it.
    """

    def request(self, *args, **kwargs):
        for delay in SHEETS_RETRY_DELAYS:
            try:
                return super().request(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                if e.response.status_code not in RETRYABLE_SHEETS_STATUSES:
                    raise
                log(f"Google Sheets returned HTTP {e.response.status_code}; retrying in {delay}s...")
                time.sleep(delay)
        return super().request(*args, **kwargs)


def open_worksheet(profile: dict, use_test: bool = False):
    """Connect to a profile's Google Sheet and make sure its header row is set up.

    `use_test` selects the profile's test worksheet (see resolve_worksheet_name())
    instead of its real one. The "test mode by default" policy lives in the CLI (see
    its --prod flag), not in this shared function.

    Returns (worksheet, formula_separator) — the worksheet object for reading/writing
    rows, and the locale-appropriate separator link_cell() needs for its HYPERLINK
    formulas.
    """
    creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    sheet_id = profile.get("google_sheet_id")
    worksheet_name = resolve_worksheet_name(profile, use_test)
    columns = profile["columns"]
    header = [col["header"] for col in columns]

    if not sheet_id or sheet_id.startswith("YOUR_"):
        log(f"Profile '{profile.get('name')}' has no google_sheet_id set.")
        sys.exit(1)
    if not os.path.exists(creds_file):
        log(f"Service account file '{creds_file}' not found.")
        sys.exit(1)

    # A service account is a "robot" Google identity: instead of a human logging in
    # via browser, our script proves its identity with this private key file. The
    # sheet's owner has to explicitly share the sheet with the service account's email
    # (see README) for any of this to be allowed to read/write it.
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    gc = gspread.authorize(creds, http_client=RetryingHTTPClient)
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=100, cols=len(header))

    # A brand-new sheet (or one that only has an empty placeholder first row) needs
    # its header row written before we can do anything else. We check "is every cell
    # in row 1 blank?" rather than just "is the sheet empty?", because Sheets can
    # report a single all-blank row as present rather than as zero rows.
    values = ws.get_all_values()
    if not values or not any(cell.strip() for cell in values[0]):
        ws.update(range_name="A1", values=[header])

    formula_sep = formula_separator_for_locale(sh.locale)
    return ws, formula_sep


def load_existing_records(ws, dedupe_keys: list[str], columns: list[dict]) -> dict[tuple[str, ...], dict]:
    """Read the sheet's existing rows, keyed by dedupe key (e.g. position + employer,
    lowercased and trimmed), each with its sheet row number and full record.

    Beyond simple duplicate detection, dedupe_jobs() uses the row number to overwrite
    a row in place when a later run finds a better direct link for it, and the record
    to check whether it already has one and to preserve its date_added/STATUS/COMMENTS
    when doing so.
    """
    # header_by_source maps a column's internal "source" name (e.g. "position") to
    # the literal header text used in the sheet itself (e.g. "position" or, for a
    # profile with a custom header, something like "Job Title").
    header_by_source = {col["source"]: col["header"] for col in columns}
    # Passing our own expected_headers avoids a hard crash on sheets with extra blank
    # trailing columns beyond what this profile declares — gspread otherwise refuses
    # to proceed when it finds more than one blank header cell, treating them as
    # duplicates.
    records = ws.get_all_records(expected_headers=[col["header"] for col in columns])
    existing = {}
    for i, row in enumerate(records):
        values = [str(row.get(header_by_source[src], "")).strip().lower() for src in dedupe_keys]
        if all(values):
            # Row 1 is the header, and get_all_records() is 0-indexed from row 2.
            existing[tuple(values)] = {"row_number": i + 2, "record": row}
    return existing


def link_cell(url: str, formula_sep: str = ",") -> str:
    """Build a HYPERLINK formula for a real URL, showing its domain as the link text.

    Returns "" if `url` doesn't look like a real, fetchable URL — for example, some
    source text just has a hyperlink's leftover label text like "Company Career
    Posting" with no real href, or Gemini wrote "NONE". A "hyperlink" pointing at
    made-up text would just be a link to nowhere, so the "link" column is simply left
    blank in that case (see search_link() for the separate, always-populated column).
    """
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    # Sheets formulas use "" to represent a literal double-quote inside a quoted
    # string, so any quote characters in the URL/display text need doubling up.
    escaped_url = url.replace('"', '""')
    escaped_display = parsed.netloc.replace('"', '""')
    return f'=HYPERLINK("{escaped_url}"{formula_sep} "{escaped_display}")'


def search_link(job: dict, formula_sep: str = ",") -> str:
    """Build a Google search link for a job, as a fallback for when we don't have a
    genuine posting URL (Gemini wrote "NONE", left it blank, or gave non-URL text).

    Rather than leaving the cell empty, this gives the user a one-click way to go
    looking for the actual posting themselves.
    """
    query = f"job {job.get('position', '')} at {job.get('employer', '')}"
    location = job.get("location", "")
    if location:
        query += f" in {location}"
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    escaped_url = url.replace('"', '""')
    return f'=HYPERLINK("{escaped_url}"{formula_sep} "SEARCH")'


def job_row(
    job: dict,
    today: str,
    columns: list[dict],
    formula_sep: str = ",",
    preserve: dict | None = None,
) -> list:
    """Build one sheet row (a plain list of cell values, in column order) for a job.

    "link" and "search_link" are two independent columns: "link" is the genuine URL
    Claude extracted (blank if there wasn't a real one), and "search_link" is *always*
    a constructed Google search for the job, regardless of whether a direct link
    exists — so there's always at least one clickable way to go looking for the
    posting.

    `preserve`, when given, is an existing sheet record (as from
    load_existing_records()'s "record" field) to pull date_added/STATUS/COMMENTS from
    instead of the defaults for a brand-new row. This is used when upgrading an
    existing row with a newly-found direct link: the row's original discovery date
    and anything the user has set by hand in STATUS/COMMENTS must survive the update
    untouched, even though every other column is refreshed from the new data.
    """
    header_by_source = {col["source"]: col["header"] for col in columns} if preserve else {}
    row = []
    for col in columns:
        source = col["source"]
        if source == "date_added":
            row.append(preserve.get(header_by_source[source], "") if preserve else today)
        elif source == "status":
            row.append(preserve.get(header_by_source[source], "") if preserve else "NEW")
        elif source == "comments":
            row.append(preserve.get(header_by_source[source], "") if preserve else "")
        elif source == "link":
            row.append(link_cell(job.get("link", ""), formula_sep))
        elif source == "search_link":
            row.append(search_link(job, formula_sep))
        else:
            row.append(job.get(source, ""))
    return row

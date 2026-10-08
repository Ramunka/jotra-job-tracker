"""The shared extract → dedupe → write → report pipeline both commands end at."""

import sys
from datetime import date

from .config import find_candidate_cv
from .console import ANSI_RESET, ANSI_YELLOW, log
from .dedupe import dedupe_jobs, describe_job
from .extraction import (
    CV_REQUIRING_SOURCES,
    extract_jobs,
    extraction_fields,
    load_fit_score_instructions,
)
from .sheets import load_existing_records, open_worksheet


def read_input_text(input_file: str | None) -> str:
    """Get the raw job-listing text to feed to Claude.

    Two modes: read a file (handy for scripting/testing), or read interactively from
    the terminal. For the interactive case we can't just call sys.stdin.read() and
    wait for end-of-file, because on Windows the usual Ctrl+Z-then-Enter EOF signal
    doesn't reliably reach Python in every terminal — so instead we read line by line
    and stop at a literal "END" line, which works the same way everywhere.
    """
    if input_file:
        with open(input_file, encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            log(f"Input file '{input_file}' is empty.")
            sys.exit(1)
        return text

    log("Paste the job listing text below.")
    log("When done, type END on its own line and press Enter.\n")

    lines = []
    for line in sys.stdin:
        if line.rstrip("\r\n").strip().upper() == "END":
            break
        lines.append(line)
    text = "".join(lines)

    if not text.strip():
        log("No text received. Exiting.")
        sys.exit(1)
    return text


def sync(profile: dict, text: str, use_test: bool = False) -> None:
    """Extract jobs from text with Claude, dedupe against the profile's sheet, and
    append new rows, then print a summary report.

    This is the shared core both commands end at: sync_jobs.py calls it with
    manually-provided text, and update_jobs.py calls it with Gemini's search results,
    so the extraction/dedupe/append/report logic only has to exist in one place.
    """
    columns = profile["columns"]
    dedupe_keys = profile.get("dedupe_keys", ["position", "employer"])
    fields = extraction_fields(columns)

    needs_cv = any(col["source"] in CV_REQUIRING_SOURCES for col in fields)
    cv_file = find_candidate_cv(profile) if needs_cv else None
    if needs_cv and not cv_file:
        log(
            f"Profile '{profile['name']}' has a fit_score/fit_reason column but no CV "
            f"file found (expected prompts/cv/{profile['name']}.pdf, or set a "
            '"cv_file" key in profiles.json). Add the CV and try again.'
        )
        sys.exit(1)
    fit_score_instructions = load_fit_score_instructions(profile) if needs_cv else None

    log("Extracting job data with Claude...")
    extracted = extract_jobs(text, fields, cv_file=cv_file, fit_score_instructions=fit_score_instructions)
    log(f"Extracted {len(extracted)} job(s).")

    ws, formula_sep = open_worksheet(profile, use_test=use_test)
    existing_records = load_existing_records(ws, dedupe_keys, columns)

    today = date.today().isoformat()
    new_rows, added, updates, skipped_duplicates, skipped_invalid = dedupe_jobs(
        extracted, existing_records, today, columns, dedupe_keys, formula_sep
    )

    # append_rows (rather than rewriting the whole sheet) only touches the new rows,
    # so it's safe even if you're editing the sheet by hand at the same time.
    # value_input_option="USER_ENTERED" tells Sheets to interpret our HYPERLINK
    # formulas as formulas instead of storing them as literal "=HYPERLINK(...)" text.
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    # Each update overwrites one existing row (found without a direct link, now with
    # one) in place. These are separate, single-row writes rather than a batch call,
    # since there are normally only a handful per run and it keeps the logic simple.
    for row_number, row_values, _job in updates:
        ws.update(range_name=f"A{row_number}", values=[row_values], value_input_option="USER_ENTERED")

    log("\n--- Report ---")
    log(f"Added ({len(added)}):")
    for job in added:
        log(f"  + {describe_job(job, dedupe_keys)}")

    if updates:
        log(f"\nUpdated with a newly-found direct link ({len(updates)}):")
        for _row_number, _row_values, job in updates:
            log(f"  ~ {describe_job(job, dedupe_keys)}  {ANSI_YELLOW}link updated{ANSI_RESET}")

    log(f"\nSkipped as duplicates ({len(skipped_duplicates)}):")
    for job in skipped_duplicates:
        log(f"  - {describe_job(job, dedupe_keys)}")

    if skipped_invalid:
        log(f"\nSkipped as invalid, missing {' or '.join(dedupe_keys)} ({len(skipped_invalid)}):")
        for job in skipped_invalid:
            log(f"  ! {job}")

"""Merging freshly extracted jobs against what's already in the sheet.

This is the pure core of the sync process: no network access, which is why the tests
can exercise it directly without any Google/Anthropic credentials.
"""

from .console import ANSI_RED, ANSI_RESET
from .extraction import extraction_fields, normalize_job
from .sheets import job_row, link_cell


def dedupe_jobs(
    extracted: list[dict],
    existing_records: dict[tuple[str, ...], dict],
    today: str,
    columns: list[dict],
    dedupe_keys: list[str],
    formula_sep: str = ",",
):
    """Normalize, validate, and merge extracted jobs against what's already in the sheet.

    Beyond simple dedupe-key matching, this mitigates Gemini's inconsistent sourcing
    of direct links from one run to the next: if a job is already in the sheet
    *without* a real direct link, and this batch has the same job *with* one, the
    existing row is upgraded in place (see job_row()'s `preserve` for what survives
    the update) instead of being silently skipped or added as a duplicate second row.
    A job that's already in the sheet *with* a link is always treated as a plain
    duplicate, regardless of what this batch found for it.

    Returns (new_rows, added, updates, skipped_duplicates, skipped_invalid).
    - new_rows / added: rows to append for genuinely new jobs, and the job dicts they
      came from (for the report).
    - updates: (row_number, row_values, job) for existing sheet rows to overwrite
      with a newly-found direct link.
    """
    header_by_source = {col["source"]: col["header"] for col in columns}
    link_header = header_by_source.get("link")
    fields = extraction_fields(columns)

    new_rows = []
    added = []
    updates = []
    skipped_duplicates = []
    skipped_invalid = []
    # Tracks jobs already staged into new_rows/added *within this batch* (as opposed
    # to existing_records, which is what's already in the sheet), so a later
    # duplicate in the same batch with a better link can upgrade the staged entry
    # instead of being skipped or creating a second row for the same job.
    staged_by_key: dict[tuple[str, ...], int] = {}

    for raw_job in extracted:
        job = normalize_job(raw_job, fields)
        if not all(job.get(src) for src in dedupe_keys):
            skipped_invalid.append(raw_job)
            continue

        key = tuple(job[src].lower() for src in dedupe_keys)
        has_link = bool(link_cell(job.get("link", ""), formula_sep))

        existing = existing_records.get(key)
        if existing is not None:
            existing_has_link = bool(str(existing["record"].get(link_header, "")).strip())
            if not existing_has_link and has_link:
                row_values = job_row(job, today, columns, formula_sep, preserve=existing["record"])
                updates.append((existing["row_number"], row_values, job))
                # Mark it as having a link now, so if a later job in this same batch
                # also matches this key, it's treated as a plain duplicate rather than
                # triggering a second, redundant update to the same row.
                existing["record"][link_header] = "(upgraded this run)"
            else:
                skipped_duplicates.append(job)
            continue

        staged_index = staged_by_key.get(key)
        if staged_index is not None:
            staged_has_link = bool(link_cell(added[staged_index].get("link", ""), formula_sep))
            if not staged_has_link and has_link:
                added[staged_index] = job
                new_rows[staged_index] = job_row(job, today, columns, formula_sep)
            else:
                skipped_duplicates.append(job)
            continue

        staged_by_key[key] = len(new_rows)
        added.append(job)
        new_rows.append(job_row(job, today, columns, formula_sep))

    return new_rows, added, updates, skipped_duplicates, skipped_invalid


def describe_job(job: dict, dedupe_keys: list[str]) -> str:
    """One-line human-readable label for a job, used in the report output.

    Flags a missing link in red so a "NONE" from Gemini or an empty link field stands
    out at a glance instead of blending into the list.
    """
    label = " / ".join(job.get(src, "") for src in dedupe_keys)
    link = (job.get("link") or "").strip()
    if not link or link.upper() == "NONE":
        label += f"  {ANSI_RED}No link{ANSI_RESET}"
    return label

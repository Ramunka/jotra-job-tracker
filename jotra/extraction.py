"""Structured job extraction from free-form text with Claude.

The set of fields to extract is driven entirely by a profile's column schema, so the
same code serves any sheet layout. When a profile asks for a fit score, the
candidate's CV is attached to the request as a PDF so Claude can score each job
against real qualifications rather than guessing.
"""

import base64
import json
import os
import sys

from anthropic import Anthropic

from .console import log

# Override with the CLAUDE_MODEL environment variable to try a different model.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"

# Every profile's sheet has some columns whose values we compute ourselves (today's
# date, a fixed "NEW" status, a blank comments cell, a constructed search-engine link)
# rather than asking Claude to extract them from text. Every other column "source" is
# a field Claude must extract.
FIXED_SOURCES = {"date_added", "status", "comments", "search_link"}

# Fields that only make sense with the candidate's CV in front of Claude (a fit score
# against qualifications Claude never saw is a guess, not a judgment). Any profile
# whose columns include one of these needs a CV file to exist (see
# config.find_candidate_cv()) — sync() hard-fails rather than silently scoring
# without one.
CV_REQUIRING_SOURCES = {"fit_score", "fit_reason"}

# Fallback descriptions for the standard fields, so a profile only needs to write out a
# "description" in profiles.json for fields it introduces beyond these (see
# field_description() below).
KNOWN_FIELD_DESCRIPTIONS = {
    "domain": 'the industry/business domain of the employer or role (e.g. "Fintech", "Healthcare", "E-commerce")',
    "employer": "the company/organization name",
    "position": "the job title",
    "location": "the job location",
    "salary": "posted or suggested salary/compensation, if mentioned",
    "qualifications": "key required qualifications, briefly summarized",
    "summary": "a short summary of duties/responsibilities",
    "link": "the job posting URL",
}

# {field_lines} is filled in per-profile by extract_jobs() with one "- "source":
# description" line per field Claude needs to extract (see field_description()).
EXTRACTION_PROMPT_TEMPLATE = """Extract every job posting from the text below into a JSON array.
Each item must be an object with exactly these fields:
{field_lines}
{cv_instruction}
If a field is not present in the text, use an empty string for it.
Respond with JSON only, no markdown fences, no commentary.

TEXT:
{text}
"""

# Default scoring instructions for fit_score/fit_reason, used as the fallback when a
# profile has no prompts/fit_score/<profile>.txt file (see
# load_fit_score_instructions()).
DEFAULT_FIT_SCORE_INSTRUCTIONS = (
    'The candidate\'s CV is attached as a PDF. Use it to fill in "fit_score" and '
    '"fit_reason": fit_score is 1-10, using the full range honestly rather than '
    "clustering around 7-9; fit_reason is a couple of very short, comma-separated "
    'reasons (a few words each, e.g. "no .NET, no PhD" or "strong Python match"), '
    "never a full sentence."
)


def load_fit_score_instructions(profile: dict) -> str:
    """Load a profile's editable scoring criteria for fit_score/fit_reason.

    Defaults to prompts/fit_score/<profile_name>.txt (override via a
    "fit_score_prompt_file" key in profiles.json). Unlike the Gemini search prompt, a
    missing file here isn't an error — it just falls back to
    DEFAULT_FIT_SCORE_INSTRUCTIONS, since this file only tunes *how* scoring is
    worded, not whether scoring happens at all (that's gated by CV presence, checked
    separately in sync()).
    """
    prompt_file = profile.get("fit_score_prompt_file") or os.path.join(
        "prompts", "fit_score", f"{profile['name']}.txt"
    )
    if not os.path.exists(prompt_file):
        return DEFAULT_FIT_SCORE_INSTRUCTIONS
    with open(prompt_file, encoding="utf-8") as f:
        text = f.read().strip()
    return text or DEFAULT_FIT_SCORE_INSTRUCTIONS


def extraction_fields(columns: list[dict]) -> list[dict]:
    """Return the subset of a profile's columns that Claude must extract from text,
    i.e. every column except the fixed ones (see FIXED_SOURCES)."""
    return [col for col in columns if col["source"] not in FIXED_SOURCES]


def field_description(col: dict) -> str:
    """Get the human-readable description of a field to put in the extraction prompt.

    A column can supply its own "description" (required for any custom field a
    profile introduces); otherwise we fall back to the built-in description for
    well-known fields like "domain" or "salary".
    """
    description = col.get("description") or KNOWN_FIELD_DESCRIPTIONS.get(col["source"])
    if not description:
        log(
            f"Column with source '{col['source']}' has no description and isn't a "
            f"known field ({', '.join(KNOWN_FIELD_DESCRIPTIONS)}). "
            'Add a "description" for it in profiles.json.'
        )
        sys.exit(1)
    return description


def normalize_job(job: dict, fields: list[dict]) -> dict:
    """Coerce one raw job dict (as returned by Claude) into a clean dict with every
    expected field present as a trimmed string, even if Claude omitted or nulled it."""
    return {col["source"]: str(job.get(col["source"]) or "").strip() for col in fields}


def parse_json_array(raw: str) -> list:
    """Parse Claude's answer as a JSON array, tolerating a ```json fence around it.

    Even though the prompt asks for "JSON only, no markdown fences", models sometimes
    wrap their answer in a ```json ... ``` block anyway; strip that off if present so
    json.loads() doesn't choke on the backticks.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        jobs = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"Could not parse Claude's response as JSON: {e}")
        log(raw)
        sys.exit(1)

    if not isinstance(jobs, list):
        log("Expected a JSON array of jobs, got something else:")
        log(raw)
        sys.exit(1)

    return jobs


def extract_jobs(
    text: str,
    fields: list[dict],
    cv_file: str | None = None,
    fit_score_instructions: str | None = None,
) -> list[dict]:
    """Ask Claude to pull structured job data out of free-form text.

    `fields` (from extraction_fields()) drives what we ask for: we build one prompt
    line per field so the same function works for any profile's schema, not just a
    hardcoded set of columns.

    `cv_file`, if given, is read and attached to the request as a PDF document block
    so Claude can score "fit_score"/"fit_reason" against the candidate's actual CV
    content (see CV_REQUIRING_SOURCES) — the CV is read fresh and inlined as base64
    on every call rather than uploaded once via Claude's Files API, since it's a
    small, static file and this avoids having to manage/delete an uploaded copy
    afterwards.

    `fit_score_instructions` is the editable scoring-criteria text (see
    load_fit_score_instructions()) inserted into the prompt whenever `cv_file` is
    set; falls back to DEFAULT_FIT_SCORE_INSTRUCTIONS if not given.

    Returns a list of plain dicts, one per job Claude found, e.g.
    [{"employer": "Acme", "position": "Engineer", ...}, ...].
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    field_lines = "\n".join(f'- "{col["source"]}": {field_description(col)}' for col in fields)
    cv_instruction = f"\n{fit_score_instructions or DEFAULT_FIT_SCORE_INSTRUCTIONS}\n" if cv_file else ""
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        field_lines=field_lines, cv_instruction=cv_instruction, text=text
    )

    content: str | list[dict] = prompt
    if cv_file:
        with open(cv_file, "rb") as f:
            cv_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
        content = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": cv_b64},
            },
            {"type": "text", "text": prompt},
        ]

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
        # Generous headroom: the model can emit an internal "thinking" block before
        # its actual answer, and a batch of a dozen-plus detailed jobs adds up in
        # tokens fast. Too low a limit here would silently truncate the JSON.
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "max_tokens":
        log(
            "Warning: Claude's response was cut off at the max_tokens limit. "
            "Try splitting the input into smaller batches."
        )

    # response.content is a list of "blocks" — for models with extended thinking, the
    # first block(s) can be ThinkingBlock rather than the text we actually want, so we
    # explicitly pick out the text block(s) instead of assuming content[0] is text.
    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        log("Claude's response had no text content:")
        log(response.content)
        sys.exit(1)

    return parse_json_array(text_blocks[0])

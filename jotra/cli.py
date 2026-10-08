"""Command-line entry points: `sync` (manual paste) and `update` (Gemini web search)."""

import argparse
import os
import sys

import gspread
from anthropic import AnthropicError
from dotenv import load_dotenv

from .config import (
    check_credentials,
    find_candidate_cv,
    get_global_setting,
    load_gemini_prompt,
    load_profile,
)
from .console import ANSI_RED, ANSI_RESET, ANSI_YELLOW, log, print_header
from .pipeline import read_input_text, sync
from .sheets import RETRYABLE_SHEETS_STATUSES


def run_cli(main_func) -> None:
    """Run a command's main() and turn any exception into a short, friendly message
    instead of a raw Python traceback.

    Every expected failure path already calls sys.exit(1) with its own clear message
    (that still works normally here, since SystemExit isn't caught by
    "except Exception"). This wrapper is the catch-all safety net for everything
    else — network hiccups, API errors, etc.
    """
    # load_dotenv() reads a local ".env" file (if present) and copies its KEY=VALUE
    # lines into os.environ, so the rest of the code can just use os.environ.get(...).
    load_dotenv()
    try:
        main_func()
    except KeyboardInterrupt:
        log("\nInterrupted.")
        sys.exit(1)
    except AnthropicError as e:
        log(f"\nClaude API error: {e}")
        sys.exit(1)
    except gspread.exceptions.APIError as e:
        status = e.response.status_code
        if status in RETRYABLE_SHEETS_STATUSES:
            # Google's error pages are full HTML documents — not worth dumping.
            log(f"\nGoogle Sheets error: HTTP {status} (temporary Google-side failure).")
            log("Retries were exhausted; try again in a minute or two.")
        else:
            log(f"\nGoogle Sheets error: {e}")
            if status in (403, 404):
                log(
                    "Check that the service account (see credentials.json's client_email) "
                    "has Editor access to the sheet."
                )
        sys.exit(1)
    except PermissionError as e:
        log(f"\nGoogle Sheets error: {e}")
        log(
            "Check that the service account (see credentials.json's client_email) "
            "has Editor access to the sheet."
        )
        sys.exit(1)
    except Exception as e:
        log(f"\nError: {type(e).__name__}: {e}")
        sys.exit(1)


def _base_parser(description: str) -> argparse.ArgumentParser:
    """Arguments every command shares: the profile, where profiles live, and --prod."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("profile", help="Profile name from profiles.json")
    parser.add_argument(
        "--profiles-file",
        default=os.environ.get("PROFILES_FILE", "profiles.json"),
        help="Path to the profiles JSON file (default: profiles.json)",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Write to the profile's real worksheet. Without this flag, every run"
        " writes to the profile's TEST_worksheet_name instead, so an accidental or"
        " exploratory run can never touch real data.",
    )
    return parser


def _start(parser: argparse.ArgumentParser) -> tuple[argparse.Namespace, bool]:
    """Parse args, print the banner and which mode (TEST/PROD) this run is in."""
    args = parser.parse_args()
    print_header()
    log(parser.format_usage())

    use_test = not args.prod
    mode_label = f"{ANSI_YELLOW}TEST{ANSI_RESET}" if use_test else f"{ANSI_RED}PROD{ANSI_RESET}"
    log(f"Mode: {mode_label}" + ("" if args.prod else " (pass --prod to write to the real sheet)"))
    return args, use_test


def sync_main() -> None:
    """Extract jobs from pasted (or file-provided) text and sync them to the sheet."""
    parser = _base_parser(sync_main.__doc__)
    parser.add_argument(
        "--input",
        "-i",
        default=None,
        help="Read job text from this file instead of an interactive paste",
    )
    args, use_test = _start(parser)

    check_credentials(["ANTHROPIC_API_KEY"])

    profile = load_profile(args.profiles_file, args.profile)
    log(f"Using profile '{profile['name']}'\n")

    text = read_input_text(args.input)
    sync(profile, text, use_test=use_test)


def update_main() -> None:
    """Search the web for jobs with Gemini and sync the results to the sheet."""
    # Imported here so `sync` works even without the Gemini SDK installed.
    from .search import DEFAULT_GEMINI_MODEL, search_jobs_with_gemini, verify_links_against_grounding

    parser = _base_parser(update_main.__doc__)
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Also write Gemini's raw response text to gemini_text.txt, for debugging"
        " what Gemini actually returned (e.g. checking links it provided).",
    )
    parser.add_argument(
        "--verify-links",
        action="store_true",
        help="Cross-check links in Gemini's answer against its real grounding search"
        " results and print a warning for any that don't match. Makes real network"
        " requests to every cited source, so it's slower and best used for"
        " spot-checking rather than every routine run.",
    )
    parser.add_argument(
        "--no-cv",
        action="store_true",
        help="Don't attach the candidate's CV to the Gemini search, even if one is"
        " found for this profile. By default, a CV is attached whenever one exists.",
    )
    args, use_test = _start(parser)

    # sync() further down needs Claude too, so check both keys up front rather than
    # spending a Gemini search only to fail on the Anthropic key afterwards.
    check_credentials(["ANTHROPIC_API_KEY", "GEMINI_API_KEY"])

    profile = load_profile(args.profiles_file, args.profile)
    log(f"Using profile '{profile['name']}'")

    # GEMINI_MODEL (env) lets you override for a one-off run without editing the
    # file; normally the model comes from profiles.json's global "gemini_model", so
    # every profile stays in sync when you want to switch models.
    model = os.environ.get("GEMINI_MODEL") or get_global_setting(
        args.profiles_file, "gemini_model", DEFAULT_GEMINI_MODEL
    )
    log(f"Using Gemini model '{model}'")

    prompt = load_gemini_prompt(profile)
    temperature = profile.get("params", {}).get("gemini_temperature")
    cv_file = None if args.no_cv else find_candidate_cv(profile)
    # The "match to the CV" instruction only makes sense when a CV is actually being
    # attached this run — added here rather than baked into the prompt file itself,
    # so --no-cv cleanly removes both the file and the mention.
    if cv_file:
        prompt += "\n- match positions to the attached CV\n"
    log("Searching for jobs with Gemini...")
    text, response = search_jobs_with_gemini(prompt, model, temperature, cv_file)
    log(f"Gemini returned {len(text)} characters of job text.\n")

    if args.dump:
        with open("gemini_text.txt", "w", encoding="utf-8") as f:
            f.write(text)
        log("Dumped Gemini's raw response to gemini_text.txt\n")

    if args.verify_links:
        verify_links_against_grounding(response, text)
        log()

    sync(profile, text, use_test=use_test)

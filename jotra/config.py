"""Loading profiles, credentials and per-profile files (CVs, prompts) from disk."""

import json
import os
import sys

from .console import log


def check_credentials(required_env_vars: list[str]) -> None:
    """Fail fast, with a clear message, if required secrets aren't in place.

    Without this check, a missing API key or credentials file would only surface deep
    inside a function call (e.g. a confusing 401 from an HTTP library), and a
    from-scratch user wouldn't know where those files even come from.
    """
    missing = []

    for var in required_env_vars:
        value = os.environ.get(var)
        # .env.example ships with placeholder values like "your-anthropic-api-key" so
        # the file has the right shape; if someone copies it to .env without editing,
        # treat that placeholder the same as "not set".
        if not value or value.startswith("your-"):
            missing.append(var)

    creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    if not os.path.exists(creds_file):
        missing.append(f"{creds_file} (Google service account key file)")

    if missing:
        log("Missing required credentials:")
        for item in missing:
            log(f"  - {item}")
        log(
            "\nSecrets aren't included in the repository. See the README's Setup "
            "section for how to create .env and credentials.json."
        )
        sys.exit(1)


def load_profiles_data(profiles_file: str) -> dict:
    """Load the profiles file's whole top-level object: global settings that apply
    across every profile (e.g. "gemini_model") alongside the "profiles" list itself.
    """
    if not os.path.exists(profiles_file):
        log(
            f"Profiles file '{profiles_file}' not found. "
            "Copy profiles.example.json to profiles.json and edit it."
        )
        sys.exit(1)

    with open(profiles_file, encoding="utf-8") as f:
        return json.load(f)


def get_global_setting(profiles_file: str, key: str, default=None):
    """Read one top-level, cross-profile setting from the profiles file (e.g. which
    Gemini model to use), falling back to `default` if it isn't set."""
    return load_profiles_data(profiles_file).get(key, default)


def load_profile(profiles_file: str, profile_name: str) -> dict:
    """Look up one profile by name inside a profiles JSON file.

    A "profile" bundles together which Google Sheet to use, that sheet's column
    layout, and how to detect duplicate jobs for it — see profiles.example.json and
    the README's "Profiles" section for the full shape.
    """
    profiles = load_profiles_data(profiles_file).get("profiles", [])

    for profile in profiles:
        if profile.get("name") == profile_name:
            return profile

    # No matching profile: rather than a bare KeyError, tell the user what names
    # *are* available so they can fix a typo immediately.
    available = ", ".join(p.get("name", "?") for p in profiles)
    log(f"Profile '{profile_name}' not found in {profiles_file}. Available: {available}")
    sys.exit(1)


def find_candidate_cv(profile: dict) -> str | None:
    """Find a profile's CV file, if one exists.

    Defaults to prompts/cv/<profile_name>.pdf (override with a "cv_file" key in
    profiles.json). Shared by fit-score extraction (Claude) and CV-matched web search
    (Gemini) — both need the candidate's actual CV content, for different reasons,
    but look it up the same way.
    """
    cv_file = profile.get("cv_file") or os.path.join("prompts", "cv", f"{profile['name']}.pdf")
    return cv_file if os.path.exists(cv_file) else None


def load_gemini_prompt(profile: dict) -> str:
    """Read the search-instructions text file for a profile.

    Keeping each profile's search criteria in its own plain-text file (rather than,
    say, a big string in profiles.json) makes them easy to write and edit as prose,
    which is what they really are — instructions for Gemini, not structured data.
    """
    prompt_file = profile.get("gemini_prompt_file") or os.path.join("prompts", f"{profile['name']}.txt")
    if not os.path.exists(prompt_file):
        log(
            f"Gemini prompt file '{prompt_file}' not found for profile '{profile['name']}'. "
            "Create it with the search instructions for this profile "
            "(see prompts/example.txt)."
        )
        sys.exit(1)
    with open(prompt_file, encoding="utf-8") as f:
        prompt = f.read()
    if not prompt.strip():
        log(f"Gemini prompt file '{prompt_file}' is empty.")
        sys.exit(1)
    return prompt

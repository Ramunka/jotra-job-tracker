"""Live web job search with Gemini's Google Search grounding.

Gemini's answer is plain text shaped like something a person would paste into
sync_jobs.py by hand, which is then fed through the same Claude extraction pipeline.
"""

import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from google import genai
from google.genai import errors, types

from .console import log

# Fallback if profiles.json doesn't set "gemini_model" at all.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
# A "server error" (like 503 UNAVAILABLE, "high demand") is Google's infrastructure
# being temporarily overloaded, not a problem with our request, so it's worth a few
# automatic retries — especially since this is meant to run unattended (e.g. from a
# scheduled task) where there's no one around to just try again by hand.
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 20
LINK_VERIFY_TIMEOUT_SECONDS = 6


def search_jobs_with_gemini(
    prompt: str, model: str, temperature: float | None = None, cv_file: str | None = None
):
    """Ask Gemini to search the web for jobs, optionally matching them to a candidate's CV.

    Returns (text, response): `text` is Gemini's answer as plain text — a list of jobs
    with employer/position/location/etc. — since that's exactly what the
    Claude-extraction step expects as input. `response` is the full API response
    object, kept around so verify_links_against_grounding() can inspect its grounding
    metadata (the text alone doesn't carry that).

    `temperature` controls how deterministic vs. varied Gemini's output is. This has
    to be set here as a real API parameter — asking for a specific temperature in the
    prompt's own text has no effect.

    `cv_file`, if given, is uploaded via Gemini's Files API and attached to the
    request so Gemini can read its actual content when matching jobs (a Google Drive
    link wouldn't work: Gemini can't fetch through Drive's auth wall). The uploaded
    copy is deleted again right after the call.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    contents = [prompt]
    uploaded_cv = None
    if cv_file:
        log(f"Attaching candidate CV: {cv_file}")
        uploaded_cv = client.files.upload(file=cv_file)
        contents = [uploaded_cv, prompt]

    try:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        # google_search is what actually gives Gemini live internet
                        # access for this call — without it, Gemini would just answer
                        # from its training data instead of searching for current
                        # postings.
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                        # "Automatic function calling" is a different feature (Gemini
                        # calling Python functions we hand it), which we never use
                        # here — disabling it skips an unused code path and its
                        # console warning.
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                        temperature=temperature,
                    ),
                )
                break
            except errors.ServerError as e:
                if attempt == MAX_RETRIES:
                    log(f"Gemini is unavailable after {MAX_RETRIES} attempts: {e}")
                    sys.exit(1)
                log(f"Gemini server error (attempt {attempt}/{MAX_RETRIES}): {e}. Retrying...")
                time.sleep(RETRY_DELAY_SECONDS)
    finally:
        if uploaded_cv is not None:
            client.files.delete(name=uploaded_cv.name)

    text = response.text
    if not text or not text.strip():
        log("Gemini returned no text.")
        sys.exit(1)
    return text, response


def _resolve_redirect_url(url: str) -> str | None:
    """Follow a URL's redirects and return where it actually ends up.

    Grounding chunks come back as Google redirect links, not plain URLs, so this is
    how we find out what real page a citation points to. Many job sites block
    automated requests with an error *after* redirecting to the real destination, so
    even a failed request's URL still tells us the truth — we only give up if the
    request fails before any redirect happens at all.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=LINK_VERIFY_TIMEOUT_SECONDS) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as e:
        return e.geturl() if e.geturl() else None
    except Exception:
        return None


def url_tokens(url: str) -> set[str]:
    """Pull out the "distinguishing" parts of a URL (job IDs, hex hashes, slugs) from
    its path and query string, ignoring generic short words, so two differently
    formatted URLs for the same posting can still be recognized as a match."""
    parsed = urlparse(url)
    return set(re.findall(r"[A-Za-z0-9]{6,}", parsed.path + "?" + parsed.query))


def verify_links_against_grounding(response, text: str) -> None:
    """Best-effort sanity check: does each link Gemini wrote in its answer correspond
    to one of the real search results behind its own grounding citations?

    Gemini's search grounding is genuinely backed by real search results, but its
    free-text summary of them isn't always faithful — it can still write a
    plausible-looking URL (a generic careers page, or a made-up job ID) instead of a
    real one. This is a diagnostic aid, not a filter: it only prints a warning, since
    the matching is heuristic and network calls to arbitrary job sites can fail or
    time out for reasons unrelated to whether the link is genuine.
    """
    candidate = response.candidates[0] if response.candidates else None
    grounding_metadata = getattr(candidate, "grounding_metadata", None) if candidate else None
    chunks = getattr(grounding_metadata, "grounding_chunks", None) if grounding_metadata else None
    if not chunks:
        log("Grounding check: no grounding sources in this response to check against.")
        return

    log(f"Grounding check: resolving {len(chunks)} search source(s)...")
    # Gemini sometimes echoes a grounding chunk's raw Google redirect link directly as
    # its answer instead of the page it points to — that's still a genuine citation,
    # so it counts as verified without needing to resolve anything.
    raw_redirect_urls = {chunk.web.uri for chunk in chunks if chunk.web and chunk.web.uri}
    real_urls = set()
    for redirect_url in raw_redirect_urls:
        resolved = _resolve_redirect_url(redirect_url)
        if resolved:
            real_urls.add(resolved)
    real_tokens_by_domain: dict[str, set[str]] = {}
    for url in real_urls:
        real_tokens_by_domain.setdefault(urlparse(url).netloc, set()).update(url_tokens(url))

    text_urls = set(re.findall(r'https?://[^\s)\]"\'>]+', text))
    unverified = []
    for url in text_urls:
        if url in raw_redirect_urls:
            continue
        domain = urlparse(url).netloc
        tokens = url_tokens(url)
        real_tokens = real_tokens_by_domain.get(domain)
        if real_tokens is None:
            unverified.append(url)  # domain doesn't even appear among real sources
        elif tokens and not (tokens & real_tokens):
            unverified.append(url)  # same domain, but no matching job ID/slug

    log(f"Grounding check: {len(real_urls)} real search source(s) resolved.")
    if unverified:
        log(
            f"Warning: {len(unverified)} link(s) in Gemini's answer don't clearly "
            "match a real search source (could be genuine but reformatted, or could "
            "be fabricated — worth spot-checking):"
        )
        for url in sorted(unverified):
            log(f"  ? {url}")
    else:
        log("Grounding check: every link in the answer matches a real search source.")

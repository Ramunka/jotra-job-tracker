"""Search the web for jobs with Gemini and sync the results to a profile's Google Sheet.

Usage:
    python update_jobs.py <profile_name> [--verify-links] [--no-cv] [--dump] [--prod]
"""

from jotra.cli import run_cli, update_main

if __name__ == "__main__":
    run_cli(update_main)

"""Paste job listings (or pass a file) and sync them to a profile's Google Sheet.

Usage:
    python sync_jobs.py <profile_name> [--input jobs.txt] [--prod]
"""

from jotra.cli import run_cli, sync_main

if __name__ == "__main__":
    run_cli(sync_main)

"""Terminal output helpers shared by every JoTra command."""

PROJECT_BANNER = "===============  Project JoTra: jobs tracker =================================================================="

# ANSI escape codes for colored terminal output. Supported by modern terminals
# (Windows Terminal, PowerShell 7+, most Unix shells); older cmd.exe just shows the
# raw codes, so this is a cosmetic touch rather than something to rely on.
ANSI_GREEN = "\033[92m"
ANSI_RED = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_RESET = "\033[0m"
JOTRA_PREFIX = "  Jotra: "


def log(*args) -> None:
    """Works like Python's built-in print(), but with a colored "  Jotra: " prefix on
    every non-blank line, so this tool's output is easy to pick out from other
    terminal noise. A genuinely blank line (from an empty string, e.g. for spacing)
    stays blank rather than becoming a line of nothing but the prefix.
    """
    message = " ".join(str(a) for a in args)
    prefixed_lines = [
        f"{ANSI_GREEN}{JOTRA_PREFIX}{ANSI_RESET}{line}" if line else "" for line in message.split("\n")
    ]
    print("\n".join(prefixed_lines))


def print_header() -> None:
    """Print the banner every command shows on startup, so it's obvious which
    project/tool produced the terminal output that follows."""
    log(f"\n{ANSI_GREEN}{PROJECT_BANNER}{ANSI_RESET}\n")

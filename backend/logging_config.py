"""Colored logging for demo clarity.

Shows the ACP→AG-UI translation visually:
  ◀ ACP  — events arriving from the agent subprocess
  ▶ AG-UI — events emitted to the frontend
  ● BRIDGE — internal lifecycle
"""

import logging

# ANSI color codes
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


class DemoFormatter(logging.Formatter):
    """Color-coded formatter that highlights the protocol translation story."""

    LEVEL_COLORS = {
        logging.DEBUG: DIM,
        logging.INFO: "",
        logging.WARNING: YELLOW,
        logging.ERROR: "\033[31m",
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, "")
        msg = record.getMessage()

        # Suppress noisy uvicorn access logs for OPTIONS/PATCH
        if "OPTIONS" in msg or "PATCH" in msg:
            return ""

        # Tag messages by direction
        name = record.name
        if "acp_protocol" in name or "runner" in name:
            prefix = f"{CYAN}◀ ACP {RESET}"
        elif "acp_to_agui" in name:
            prefix = f"{MAGENTA}● BRIDGE{RESET}"
        elif "sse" in name:
            prefix = f"{GREEN}▶ AG-UI{RESET}"
        elif "manager" in name:
            prefix = f"{BOLD}  ◆    {RESET}"
        elif "store" in name:
            prefix = f"{DIM}  SYS  {RESET}"
        else:
            prefix = f"{DIM}  ...  {RESET}"

        timestamp = self.formatTime(record, "%H:%M:%S")
        return f"{DIM}{timestamp}{RESET} {prefix} {color}{msg}{RESET}"


def setup_logging() -> None:
    """Configure colored logging for the bridge."""
    handler = logging.StreamHandler()
    handler.setFormatter(DemoFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Quiet down noisy loggers
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

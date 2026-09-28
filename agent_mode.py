"""
Agent run mode — the ONE control for the autonomous agent: off | paper | live.

  off   — the agent does nothing (not even exits). The emergency stop. Resting GTC stops on share
          positions still protect at the broker.
  paper — the agent trades a virtual account (storage/paper_book) at live prices; zero money. Any
          REAL positions it opened earlier keep getting live EXITS so switching away from live never
          strands them.
  live  — real orders on the agentic account (options + shares; shares capped by
          config.AGENT_STOCK_BUDGET_CAP).

Stored in agent_mode.txt (gitignored), set from the Agent tab. It replaces the old arm file
(agent_live.arm), kill switch (agentic_halt.flag) and config.AGENT_TRADE_STOCKS. No file → paper
(never real money by default); unreadable / unknown content → off (fail safe). Nothing else — not the
market-open task, not a restart — changes the mode.

File-only, no network, so it's unit-testable and safe to import anywhere.
"""
from __future__ import annotations

import os

MODES = ("off", "paper", "live")
DEFAULT_MODE = "paper"
MODE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_mode.txt")


def get_mode() -> str:
    try:
        with open(MODE_FILE, encoding="utf-8") as f:
            mode = f.read().strip().lower()
    except FileNotFoundError:
        return DEFAULT_MODE
    except OSError:
        return "off"
    return mode if mode in MODES else "off"


def set_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown agent mode {mode!r} (expected one of {MODES})")
    with open(MODE_FILE, "w", encoding="utf-8") as f:
        f.write(mode + "\n")

"""
Shared configuration constants for Argus.

Single source of truth for values used across multiple modules, so changing one
doesn't mean hunting down duplicated literals (e.g. the Claude model id, which was
previously hardcoded in the analyst, the exit checker, and the chatbot proxy).
"""

# Claude model for the judgment-heavy work (news analysis, chatbot). Change here only.
CLAUDE_MODEL = "claude-sonnet-4-6"

# Cheaper/faster model for mechanical classification (the news-driven exit check), where
# the task is "does this headline mean this exit fired?" — Haiku handles it at ~1/3 the
# cost of Sonnet. Used by alerts/exit_checker._check_event_exits.
CLAUDE_CHEAP_MODEL = "claude-haiku-4-5"

# --- Robinhood agentic execution (Path B / Design A) — OFF by default ---------------
# These gate the official Robinhood Trading MCP integration (ingestion/robinhood_mcp.py).
# With both at their defaults, Argus behaves EXACTLY as it does today: no MCP, no orders.
#
#   USE_MCP  — when False, the MCP client is never touched; all account reads and any
#              execution stay on the existing robin_stocks path. Flip to True only after
#              the OAuth handshake spike clears (see the plan file's "hard gates").
#   DRY_RUN  — when True, order placement LOGS the intended order and places NOTHING.
#              This is the safety default and must stay True until a clean multi-day dry
#              run is diffed against manual orders. Never default this to False.
#
# The Robinhood Trading MCP endpoint (first-party OAuth server). Reads/writes only ever
# hit the dedicated *Agentic* account, never the main account (Robinhood-enforced).
USE_MCP = False
DRY_RUN = True
ROBINHOOD_MCP_URL = "https://agent.robinhood.com/mcp/trading"

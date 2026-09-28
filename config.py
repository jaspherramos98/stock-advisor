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
#   MCP_PERSISTENT_SESSION — when True, MCP tool calls share ONE open connection per process
#              (ingestion/mcp_session.py) instead of a ~1.2s handshake per call. Reads retry once
#              on a broken connection; order placement never retries. False = per-call sessions.
#   AGENT_STOCK_BUDGET_CAP — max TOTAL $ (entry cost) the agent may hold in SHARES at once (the stock
#              leg is always on; routing in alerts/agentic_stocks.py: HR / conviction ≥75 → options,
#              other buys → shares). 20.0 while the stock leg is new. None = no cap (the pyramid sizes
#              on the full agentic buying power). ← set None to take the training wheels off.
#   Whether the agent trades at all (off / paper / live) is NOT here — see agent_mode.py (Agent tab).
#
# USE_MCP=True: the dashboard/pipeline read account data via the official MCP (OAuth refresh
# tokens → no 429), defaulting to the MAIN account. Requires `mcp[cli]` in the venv + a stored
# session from scripts/mcp_login.py; if absent, reads degrade to unavailable ($0) and the app
# still runs. To return to the unofficial robin_stocks path, set False. ORDERS remain gated by
# DRY_RUN and only ever hit the AGENTIC account.
USE_MCP = True
DRY_RUN = True
MCP_PERSISTENT_SESSION = True
AGENT_STOCK_BUDGET_CAP = 20.0
# Entry judge (analysis/agent_judge.py, plan J2): "shadow" = a Sonnet call reviews each candidate and its
# verdict is LOGGED beside the rules' decision but changes NOTHING (the J4 review compares them);
# "off" = no judge calls. (A binding mode arrives only in J5, if the shadow data shows it helps.)
AGENT_JUDGE = "shadow"
ROBINHOOD_MCP_URL = "https://agent.robinhood.com/mcp/trading"

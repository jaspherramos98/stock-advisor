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

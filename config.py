"""
Shared configuration constants for Argus.

Single source of truth for values used across multiple modules, so changing one
doesn't mean hunting down duplicated literals (e.g. the Claude model id, which was
previously hardcoded in the analyst, the exit checker, and the chatbot proxy).
"""

# Claude model used everywhere Argus calls the Anthropic API (analyst, exit-event
# checker, chatbot proxy). Change here only.
CLAUDE_MODEL = "claude-sonnet-4-6"

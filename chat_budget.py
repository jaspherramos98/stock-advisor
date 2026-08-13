"""
Token-budget helpers for the Argus chat proxy.

The chat widget is the biggest Claude cost in the app: every message resends the
system prompt plus the whole conversation so far, so an untrimmed history grows the
bill quadratically across a session. This module holds the two knobs that bound it,
kept out of dashboard/app.py so they can be unit-tested without booting Streamlit
(same reason market_hours.py is its own module).
"""

# Room for the action list + short explanation without truncation.
CHAT_MAX_TOKENS = 450

# How many past chat messages get resent to Claude. The browser still shows the full
# transcript; only this tail is billed. 12 = six exchanges. Safe to keep small here
# because the facts Argus reasons over (positions, P&L, buying power, today's picks)
# live in the system prompt and are refreshed on every chat open — history only
# carries conversational thread, not state.
CHAT_HISTORY_LIMIT = 12

# The static half of the system prompt must clear the model's minimum cacheable
# prefix (1024 tokens for Sonnet 4.6) or prompt caching silently does nothing — no
# error, just a cache that never gets written. ARGUS_SYSTEM_BASE measured 1,222
# tokens at 4,626 chars, so the margin is only ~16%. This floor is a drift guard:
# shortening the prompt past it turns caching off without any visible symptom.
SYSTEM_BASE_MIN_CHARS = 4200


def trim_history(messages, limit=CHAT_HISTORY_LIMIT):
    """
    Keeps the last `limit` chat messages, snapped forward to start on a 'user' turn.

    The API requires messages[0] to be 'user', so a naive tail slice that happens to
    land on an assistant reply is a 400. Returns a new list; never mutates the input.
    """
    if limit is None or len(messages) <= limit:
        return list(messages)
    window = messages[-limit:]
    for i, msg in enumerate(window):
        if msg.get("role") == "user":
            return window[i:]
    return []  # all-assistant window (shouldn't happen) — send nothing rather than a 400

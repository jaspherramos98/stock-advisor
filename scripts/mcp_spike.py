"""
Robinhood Trading MCP — OAuth-handshake spike (probe, NOT part of the app).

Run this ONCE when picking Path B back up. It answers the plan's hard gates before any
build effort is spent:

  Gate #1  order types  — does the server expose native stop / limit / bracket orders?
                          (decides whether exits are set-and-forget or need polling)
  Gate #2  handshake    — does the official `mcp` Python SDK complete Robinhood's OAuth
                          flow from a local process (one browser auth acceptable)?
  Gate #3  lifetimes    — what are the access/refresh token TTLs (confirms the 429 dies)?
  Gate #4  read scope   — agentic account only, or the main account too?

It PLACES NO ORDERS. It only connects, runs tools/list, and prints each tool's schema.
If Robinhood requires opening/funding an Agentic account before it will even list tools,
the script stops at that wall at zero cost — that itself is a finding worth recording.

Usage:
    venv\\Scripts\\python.exe scripts\\mcp_spike.py

Requires the official MCP SDK (not an app dependency; install into the venv just for this):
    pip install "mcp[cli]"

NOTE: the exact client-connect + OAuth call below depends on the installed `mcp` SDK
version's API. This is written against the streamable-HTTP client pattern; if the import
or connect signature differs in your installed version, adjust HERE only — the finding
(what tools/schemas exist) is what matters, not this glue.
"""
import asyncio
import sys
import os

# Make repo-root imports work when run as `python scripts/mcp_spike.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


async def main() -> int:
    url = config.ROBINHOOD_MCP_URL
    print(f"== Robinhood MCP spike ==\nEndpoint: {url}\n")

    try:
        # Lazy import so this file never affects the app or CI (mcp isn't an app dep).
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
    except ImportError:
        print("FAIL(import): `mcp` SDK not installed. Run:  pip install \"mcp[cli]\"")
        return 2

    try:
        # streamablehttp_client handles the OAuth challenge → browser redirect on first
        # connect. If Robinhood gates listing behind a funded agentic account, this is
        # where it fails — record the exact error (that answers the funding gate).
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("OK(handshake): OAuth + MCP session established.\n")

                tools = await session.list_tools()
                print(f"OK(tools/list): {len(tools.tools)} tools exposed:\n")
                for t in tools.tools:
                    print(f"  • {t.name}: {t.description or '(no description)'}")
                    schema = getattr(t, "inputSchema", None)
                    if schema:
                        props = list((schema.get("properties") or {}).keys())
                        print(f"      args: {props}")
                print(
                    "\nNEXT: from the list above, record for the plan —\n"
                    "  gate#1 order types (look for stop/limit/bracket in the order tool's args)\n"
                    "  gate#4 read scope (do position/account tools name an account? main or agentic?)\n"
                    "Then fill the _TOOL_* names + _order_args schema in ingestion/robinhood_mcp.py."
                )
        return 0
    except Exception as e:  # noqa: BLE001 — a spike; any failure is a finding to record
        print(f"FAIL(connect): {type(e).__name__}: {e}")
        print(
            "\nIf this is an auth/permission error, it likely means an Agentic account must be\n"
            "opened + funded before the server will talk. Record that as the gate answer."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

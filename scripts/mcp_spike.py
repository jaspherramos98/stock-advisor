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


def _print_causes(exc, indent="  ") -> None:
    """Unwrap ExceptionGroup / __cause__ chains so the real HTTP/auth error is visible.
    ASCII-only (Windows cp1252 console can't encode fancy arrows)."""
    # Surface MCP protocol error fields — code/message/data may carry the OAuth challenge.
    for attr in ("code", "message", "data"):
        val = getattr(exc, attr, None)
        if val is not None:
            print(f"{indent}[MCPError {attr}] {val}")
    subs = getattr(exc, "exceptions", None)
    if subs:  # ExceptionGroup / BaseExceptionGroup
        for sub in subs:
            print(f"{indent}-> {type(sub).__name__}: {sub}")
            _print_causes(sub, indent + "  ")
        return
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause:
        print(f"{indent}-> caused by {type(cause).__name__}: {cause}")
        _print_causes(cause, indent + "  ")


async def main() -> int:
    url = config.ROBINHOOD_MCP_URL
    print(f"== Robinhood MCP spike ==\nEndpoint: {url}\n")

    try:
        # Lazy import so this file never affects the app or CI (mcp isn't an app dep).
        # mcp 2.x high-level client: Client(url) is an async context manager that connects
        # + initializes on __aenter__ over streamable-HTTP.
        from mcp import Client
    except ImportError:
        print("FAIL(import): `mcp` SDK not installed. Run:  pip install \"mcp[cli]\"")
        return 2

    try:
        # Connecting triggers Robinhood's OAuth challenge. If the SDK can't complete auth
        # non-interactively (no browser flow wired), or the server gates listing behind a
        # funded Agentic account, this is where it fails — the exact error is the finding.
        async with Client(url) as client:
            print("OK(handshake): MCP session established.\n")

            result = await client.list_tools()
            tools = getattr(result, "tools", result)  # ListToolsResult.tools, or a bare list
            print(f"OK(tools/list): {len(tools)} tools exposed:\n")
            for t in tools:
                name = getattr(t, "name", "?")
                desc = getattr(t, "description", "") or "(no description)"
                print(f"  • {name}: {desc}")
                schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
                if isinstance(schema, dict):
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
        _print_causes(e)
        print(
            "\nIf this is an auth/permission error, it likely means either OAuth needs an\n"
            "interactive browser flow the SDK didn't run, or an Agentic account must be\n"
            "opened + funded before the server will talk. Record which, as the gate answer."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

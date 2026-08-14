"""
Robinhood Trading MCP — one-time interactive login (Path B).

Run this AFTER you've opened + funded the dedicated Agentic account. It:
  1. registers Argus as an OAuth client (Dynamic Client Registration, once),
  2. opens your browser to Robinhood to authorize,
  3. captures the callback, exchanges the code, and stores the tokens under
     ~/.tokens/robinhood_mcp.json (access + refresh — the refresh token is what ends the
     every-few-days 429 re-login),
  4. prints the tool list + each tool's argument schema — which answers the last two gates:
       gate #1 order types (native stop / limit / bracket?)
       gate #4 read scope (do account/position tools reveal main vs agentic?)

After this succeeds once, the app reads/orders run non-interactively (silent refresh).

Usage:
    venv\\Scripts\\python.exe scripts\\mcp_login.py
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    try:
        from ingestion import mcp_auth
    except ImportError as e:
        print(f"FAIL(import): {e}\nInstall the SDK into the venv:  pip install \"mcp[cli]\"")
        return 2

    print("== Robinhood MCP login ==")
    if mcp_auth.has_session():
        print("A stored session already exists — re-running will refresh/re-authorize.\n")

    try:
        tools = mcp_auth.login()
    except Exception as e:  # noqa: BLE001 — surface the real failure
        print(f"FAIL(login): {type(e).__name__}: {e}")
        print(
            "\nIf this is an account/permission error, make sure the dedicated Agentic\n"
            "account is open + funded (the onboarding auto-opens on first authorize)."
        )
        return 1

    print(f"\nOK — authenticated. {len(tools)} tools exposed:\n")
    for t in tools:
        name = getattr(t, "name", "?")
        desc = getattr(t, "description", "") or ""
        print(f"  • {name}: {desc}")
        schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
        if isinstance(schema, dict):
            print(f"      args: {list((schema.get('properties') or {}).keys())}")
    print(
        "\nNEXT (tell Argus): paste this tool list back so the _TOOL_* names + _order_args\n"
        "schema in ingestion/robinhood_mcp.py get wired to the real tools, and we settle\n"
        "gate #1 (order types) + gate #4 (read scope)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

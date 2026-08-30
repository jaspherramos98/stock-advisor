"""
OAuth transport for the Robinhood Trading MCP (Path B) — isolated from robinhood_mcp so the
auth/token plumbing stays separate from the read/order interface.

Uses the official `mcp` SDK's OAuthClientProvider, which handles Dynamic Client Registration,
the authorization-code + PKCE flow, and silent refresh-token renewal. We supply only:
  - file-backed token storage (~/.tokens/robinhood_mcp.json)
  - a browser redirect + a one-shot localhost callback server to capture the auth code.

DESIGN RULE — auth is an EXPLICIT action, never a side effect of a read:
  - login()      : interactive. May open a browser. Run once (scripts/mcp_login.py or a
                   dashboard button). Persists tokens.
  - call_tool()  : NON-interactive. Uses stored tokens (the provider refreshes silently via
                   the refresh token). If there is NO stored token it raises NotAuthenticated
                   rather than launching a browser — so a Streamlit rerun can never spontaneously
                   pop an OAuth window.

SECRETS: the token file holds access + refresh tokens — treat it like the robin_stocks pickle.
It lives under the user home (NOT the repo) and must never be committed or logged. The client_id
is a public PKCE client (no client secret). Nothing here prints token values.

This module imports the `mcp` SDK at top level, so it must be imported LAZILY by callers
(robinhood_mcp does) — CI does not install `mcp`.
"""
from __future__ import annotations

import asyncio
import http.server
import logging
import threading
import urllib.parse
from pathlib import Path

# Robinhood answers the MCP session-terminate (DELETE) with a 400, so the SDK logs a benign
# "Session termination failed: 400" WARNING on every call teardown. The reads still succeed;
# quiet just that logger (ERROR+ still surfaces real transport failures) so it doesn't spam
# each Streamlit rerun.
logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)

import anyio
import httpx2

import config
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.auth import OAuthClientProvider, OAuthFlowError, TokenStorage
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

_TOKEN_PATH = Path.home() / ".tokens" / "robinhood_mcp.json"
_CALLBACK_PORT = 8765
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}/callback"


class NotAuthenticated(RuntimeError):
    """Raised by call_tool() when no stored token exists — caller must run login() first.
    Kept distinct so reads can degrade quietly while orders surface a clear 'authenticate' message."""


# --- token persistence ----------------------------------------------------------------

class _FileTokenStorage(TokenStorage):
    """Persists the OAuth tokens + registered client info as JSON under the user home, so
    DCR happens once and the refresh token survives across runs (this is what ends the 429)."""

    def __init__(self, path: Path = _TOKEN_PATH):
        self._path = path

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            import json
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        import json
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data), encoding="utf-8")
        # Best-effort: keep the token file readable only by the user (no-op on some FS).
        try:
            import os
            os.chmod(self._path, 0o600)
        except Exception:
            pass

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(exclude_none=True, mode="json")
        self._write(data)


def has_session() -> bool:
    """True if a token is already stored (login() has been completed at least once). Cheap
    synchronous check used to gate call_tool() without touching the network."""
    return bool(_FileTokenStorage()._read().get("tokens"))


# --- interactive auth handlers (only used by login()) ---------------------------------

async def _redirect_handler(authorization_url: str) -> None:
    import webbrowser
    print("\nOpening your browser to authorize Argus with Robinhood...")
    print(f"If it doesn't open, paste this URL:\n  {authorization_url}\n")
    webbrowser.open(authorization_url)


async def _callback_handler() -> AuthorizationCodeResult:
    """Run a one-shot localhost server to capture ?code&state from the OAuth redirect."""
    captured: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404); self.end_headers(); return
            for k, v in urllib.parse.parse_qs(parsed.query).items():
                captured[k] = v[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Robinhood authorization received. You can close this tab.</h3>")

        def log_message(self, *args):  # silence default logging
            pass

    server = http.server.HTTPServer(("127.0.0.1", _CALLBACK_PORT), _Handler)
    try:
        # Handle requests until the code arrives (a browser may hit /favicon.ico first).
        while "code" not in captured:
            await anyio.to_thread.run_sync(server.handle_request)
    finally:
        server.server_close()

    if "code" not in captured:
        raise OAuthFlowError("no authorization code received on the callback")
    return AuthorizationCodeResult(
        code=captured["code"], state=captured.get("state"), iss=captured.get("iss")
    )


def _client_metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="Argus Stock Advisor",
        redirect_uris=[_REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # public client + PKCE (no secret)
        scope="internal",
    )


async def _no_browser_redirect(authorization_url: str) -> None:
    """Non-interactive redirect handler: refuse to open a browser. The SDK only reaches here
    when a FULL re-auth (authorization_code grant) is needed — i.e. the stored token is gone or
    its refresh failed. From a read/order that must be a hard error, never a surprise OAuth
    window (which, fired by concurrent Streamlit reruns, races into 'State parameter mismatch')."""
    raise NotAuthenticated(
        "Robinhood MCP needs re-authentication (token expired and silent refresh failed) — "
        "run `python scripts/mcp_login.py` once. Reads/orders will not open a browser."
    )


async def _no_browser_callback() -> AuthorizationCodeResult:
    raise NotAuthenticated("re-authentication required — run `python scripts/mcp_login.py`")


def _provider(interactive: bool) -> OAuthClientProvider:
    """Build the SDK OAuth provider. Only login() (interactive=True) may open a browser; every
    read/order path passes handlers that RAISE instead, so the 'auth is never a side effect of a
    read' design rule is actually enforced — not merely relied on via has_session()."""
    return OAuthClientProvider(
        server_url=config.ROBINHOOD_MCP_URL,
        client_metadata=_client_metadata(),
        storage=_FileTokenStorage(),
        redirect_handler=_redirect_handler if interactive else _no_browser_redirect,
        callback_handler=_callback_handler if interactive else _no_browser_callback,
    )


# --- session + calls ------------------------------------------------------------------

def _unwrap(exc: BaseException) -> BaseException:
    """The SDK runs the auth flow inside an anyio TaskGroup, so our NotAuthenticated surfaces
    wrapped in an ExceptionGroup. Dig it back out so callers can catch NotAuthenticated (and log
    the 'run mcp_login' hint) instead of an opaque 'unhandled errors in a TaskGroup'."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _unwrap(sub)
            if isinstance(found, NotAuthenticated):
                return found
    return exc


def _run_sync(interactive: bool, fn):
    """asyncio.run(_with_session(...)) with ExceptionGroup unwrapping for NotAuthenticated."""
    try:
        return asyncio.run(_with_session(interactive, fn))
    except BaseException as e:  # noqa: BLE001 — re-raised below
        unwrapped = _unwrap(e)
        if isinstance(unwrapped, NotAuthenticated):
            raise unwrapped from None
        raise


async def _with_session(interactive: bool, fn):
    provider = _provider(interactive)
    async with httpx2.AsyncClient(auth=provider, timeout=60.0) as http:
        # Keep terminate_on_close=True (default): Robinhood 400s the teardown DELETE, but that
        # warning is already silenced above; disabling termination instead breaks the stream's
        # async-generator close (a noisier failure). Log-suppression is the clean fix here.
        async with streamable_http_client(config.ROBINHOOD_MCP_URL, http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


def login() -> list:
    """Interactive one-time auth. Opens a browser, completes the OAuth flow, persists tokens,
    and returns the tool list as proof of a working session. Safe to re-run (reuses stored
    client registration; refreshes tokens)."""
    async def _run(session):
        result = await session.list_tools()
        return getattr(result, "tools", result)
    return _run_sync(interactive=True, fn=_run)


def call_tool(name: str, arguments: dict | None = None):
    """Non-interactive tool call. Requires an existing stored session (login() done once);
    the provider refreshes the access token silently. Raises NotAuthenticated if no token
    is stored, so a read can never trigger a browser popup."""
    if not has_session():
        raise NotAuthenticated("no Robinhood MCP token — run scripts/mcp_login.py first")

    async def _run(session):
        return await session.call_tool(name, arguments=arguments or {})
    return _run_sync(interactive=False, fn=_run)


def list_tools() -> list:
    """Non-interactive tools/list against the stored session (for diagnostics / the wiring
    step). Raises NotAuthenticated if not logged in."""
    if not has_session():
        raise NotAuthenticated("no Robinhood MCP token — run scripts/mcp_login.py first")

    async def _run(session):
        result = await session.list_tools()
        return getattr(result, "tools", result)
    return _run_sync(interactive=False, fn=_run)

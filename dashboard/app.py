import streamlit as st
import sys
import os
import json
import pandas as pd
from datetime import datetime
import datetime as dt
from functools import lru_cache
import plotly.express as px
import plotly.graph_objects as go

# Force UTF-8 stdout/stderr. Pipeline print()s contain non-ASCII symbols
# (→, —, ⭐, ⚠, ✓); on a Windows cp1252 console these raise UnicodeEncodeError
# and crash the pipeline mid-run (symptom: "0 recommendations"). errors="replace"
# guarantees a print can never crash the run even if reconfigure is unavailable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- THIS MUST COME BEFORE ANY LOCAL IMPORTS ---
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Local imports after path is set ---
from config import CLAUDE_MODEL
from ingestion.prices import fetch_prices
from ingestion.coingecko import TICKER_TO_COINGECKO_ID
from main import run_ingestion_and_analysis
from calculator.portfolio import calculate_allocations
from storage.positions import add_position, get_open_positions, get_closed_positions, close_position, update_manual_price, update_amount_invested, update_exit_condition
from storage.watchlist import load_watchlist, save_watchlist, add_ticker, remove_ticker, reset_to_defaults
from alerts.snooze import is_snoozed, snooze_ticker, dismiss_ticker, clear_snooze
from dotenv import load_dotenv
load_dotenv()


# --- File paths ---
CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache.json")
CACHE_BACKUP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache_backup.json")
# Budget is driven ENTIRELY by live Robinhood buying power (see _effective_budget below).
# There is no manual budget setting — the old number_input + budget.json were removed
# because a hand-typed budget could disagree with the real cash the user has, which
# confused Argus chat. Buying power is now the single source of truth for sizing.
from calculator.portfolio import MIN_ALLOCATION_BUDGET


# --- Helper functions (must be defined before any UI code) ---


# =========================================================
# CHATBOT PROXY SERVER — keeps API key server-side
# =========================================================
import threading
import requests as _requests

# Market-session logic lives in the shared market_hours module (used by the dashboard
# header badge, the chatbot context, and the alert checker — one source of truth).
from market_hours import market_session, market_status_line


# Live buying power is read on every chat open AND for the header badge; cache it for a
# short TTL so reopening the chat / reruns don't re-hit Robinhood each time.
_BP_CACHE = {"value": None, "ts": 0.0}
_BP_TTL_SECONDS = 60

# Chat token controls live in chat_budget so they can be unit-tested without
# importing this module (which boots Streamlit and the proxy thread).
from chat_budget import CHAT_MAX_TOKENS, trim_history as _trim_history


def _log_chat_usage(usage, sent_messages):
    """
    Prints one token line per chat call so the cost of a session is observable.

    `cache_read` is the number to watch: it should be 0 on the first message of a
    session and non-zero on every message after. Zero throughout means the cached
    prefix is being invalidated and the system prompt is being billed in full every
    time. Diagnostic only — never let it break a reply.
    """
    try:
        cache_read  = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        print(
            f"Chat tokens: in={usage.get('input_tokens', 0)} "
            f"out={usage.get('output_tokens', 0)} "
            f"cache_read={cache_read} cache_write={cache_write} "
            f"msgs_sent={sent_messages}"
        )
    except Exception:
        pass


def _live_buying_power(force: bool = False):
    """
    Returns live Robinhood buying power (float) or None, cached for _BP_TTL_SECONDS.
    `force=True` bypasses the cache (used by the sidebar 'Sync' button).
    """
    import time as _t
    now = _t.monotonic()
    if not force and _BP_CACHE["value"] is not None and (now - _BP_CACHE["ts"]) < _BP_TTL_SECONDS:
        return _BP_CACHE["value"]
    try:
        from ingestion.robinhood import fetch_buying_power, is_available
        bp = fetch_buying_power() if is_available() else None
    except Exception:
        bp = None
    _BP_CACHE["value"], _BP_CACHE["ts"] = bp, now
    return bp


def _effective_budget() -> float:
    """
    The allocation budget = live Robinhood buying power, always. Single source of
    truth so the dollar allocations can never disagree with the real cash available.
    Returns 0.0 when buying power can't be read (not connected / read failed) — recs
    then show at $0 (analysis still visible) rather than sizing against a fake number.
    """
    bp = _live_buying_power()
    return float(bp) if bp is not None else 0.0


def _stop_loss_price(exit_condition: str, ref_price: float, direction: str = "buy"):
    """
    Parse "stop loss at X%" from an exit_condition and return the trigger PRICE, or
    None if there's no parseable stop or no reference price.
    - Long:  ref × (1 − X/100)  (you exit when price falls X% below entry)
    - Short: ref × (1 + X/100)  (you exit when price rises X% against you)
    """
    import re
    if not exit_condition or not ref_price:
        return None
    m = re.search(r"stop\s*loss\s*at\s*(\d+(?:\.\d+)?)\s*%", exit_condition.lower())
    if not m:
        return None
    pct = float(m.group(1)) / 100.0
    return round(ref_price * (1 + pct), 2) if direction == "short" else round(ref_price * (1 - pct), 2)


def _capture_chat_suggestions(reply_text: str):
    """
    Pulls Buy/Watch lines out of Argus's action-list reply and stores them as entry-watch
    candidates, so alerts/entry_checker.py can email when one of those "buy when" levels
    is actually hit. Chat replies are otherwise ephemeral — this is what makes alerting on
    "Argus chat's last suggestion" possible. Each capture REPLACES the previous set.

    Matches the enforced action-list format, e.g. "Watch — AAPL, buy above $338.48".
    Ticker must be uppercase so prose like "Buy the dip" never registers. Best-effort:
    a parse failure must never break the chat reply.
    """
    import re
    try:
        pattern = re.compile(r"^\s*([Bb]uy|[Ww]atch)\s*[—–\-:]+\s*\$?([A-Z]{1,6})\b[,\s]*(.*)$",
                             re.MULTILINE)
        found = [
            {"ticker": m.group(2).upper(),
             "action": m.group(1).lower(),
             "trigger_text": (m.group(3) or "").strip()}
            for m in pattern.finditer(reply_text or "")
        ]
        if found:
            from storage.entry_watch import set_chat_suggestions
            n = set_chat_suggestions(found)
            print(f"Chat suggestions captured for entry alerts: {n}")
    except Exception as e:
        print(f"Chat suggestion capture failed: {e}")


def _build_argus_context() -> str:
    """
    Builds a real-time snapshot of the user's portfolio and today's
    recommendations to inject into the chatbot system prompt.
    """
    lines = []

    # --- Market session (so advice can be timed to the session) ---
    lines.append(market_status_line())

    # --- Live Robinhood buying power = THE budget (single source of truth) ---
    # There is no separate manual budget anymore; buying power IS the money to size to.
    try:
        from ingestion.robinhood import is_available
        if is_available():
            bp = _live_buying_power()
            if bp is not None:
                lines.append(
                    f"BUYING POWER = YOUR BUDGET (live real cash, size everything to this): ${bp:,.2f}"
                )
            else:
                lines.append("BUYING POWER: unavailable (could not read account — can't size dollar amounts)")
        else:
            lines.append("BUYING POWER: not connected (no Robinhood credentials — can't size dollar amounts)")
    except Exception as e:
        lines.append(f"BUYING POWER: could not load ({e})")

    # --- Open positions ---
    try:
        from storage.positions import get_open_positions
        from ingestion.prices import fetch_prices as _fp
        positions = get_open_positions()
        if positions:
            tickers    = [p["ticker"] for p in positions]
            live_prices = _fp(tickers)
            lines.append("\nOPEN POSITIONS:")
            for p in positions:
                ticker     = p["ticker"]
                ref_price  = p.get("manual_price") or p.get("reference_price", 0)
                live       = live_prices.get(ticker)
                live_price = live["price"] if live else ref_price
                change_pct = ((live_price - ref_price) / ref_price * 100) if ref_price else 0
                if p.get("direction") == "short":
                    change_pct = -change_pct  # shorts profit when price falls
                amount_inv = p.get("amount_invested", 0) or 0
                side_tag = " [SHORT]" if p.get("direction") == "short" else ""
                # Shares held, so Argus knows whether limit/stop orders are even possible:
                # Robinhood only allows limit/stop orders on WHOLE-share holdings; a
                # fractional (<1 share) position can be exited by MARKET order only.
                shares = (amount_inv / ref_price) if ref_price else 0
                if shares and shares < 1:
                    share_tag = f" | shares: {shares:.3f} (FRACTIONAL — market orders only, NO limit/stop)"
                elif shares:
                    share_tag = f" | shares: {shares:.2f} (whole — limit orders OK)"
                else:
                    share_tag = ""
                lines.append(
                    f"  {ticker}{side_tag} — {p['company_name']} | "
                    f"entry: ${ref_price:.2f} | live: ${live_price:.2f} | "
                    f"P&L: {change_pct:+.1f}% | invested: ${amount_inv:.2f}{share_tag} | "
                    f"exit when: {p.get('exit_condition', 'not set')}"
                )
        else:
            lines.append("\nOPEN POSITIONS: None")
    except Exception as e:
        lines.append(f"\nOPEN POSITIONS: Could not load ({e})")

    # --- Closed positions summary ---
    try:
        from storage.positions import get_closed_positions
        closed = get_closed_positions()
        if closed:
            with_pnl = [p for p in closed if p.get("pnl_pct") is not None]
            winners  = [p for p in with_pnl if p["pnl_pct"] > 0]
            avg_pnl  = sum(p["pnl_pct"] for p in with_pnl) / len(with_pnl) if with_pnl else 0
            win_rate = round(len(winners) / len(with_pnl) * 100) if with_pnl else 0
            lines.append(
                f"\nCLOSED POSITIONS: {len(closed)} total | "
                f"win rate: {win_rate}% | avg P&L: {avg_pnl:+.1f}%"
            )
    except Exception:
        pass

    # --- Today's recommendations ---
    try:
        cache_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pipeline_cache.json"
        )
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                cache = json.load(f)
            recs = cache.get("recommendations", [])
            if recs:
                lines.append(f"\nTODAY'S RECOMMENDATIONS ({cache.get('last_run', 'unknown run time')}):")
                for r in recs:
                    hr    = " ⭐ HIGHLY RECOMMENDED" if r.get("highly_recommended") else ""
                    conv = r.get("conviction")
                    lines.append(
                        f"  {r.get('ticker','?')} — {r.get('company_name','?')} | "
                        f"{r.get('direction','?').upper()}{hr} | "
                        f"conviction: {int(conv) if conv is not None else 'n/a'}/100 (edge) | "
                        f"confidence: {r.get('confidence_score',0):.2f} (source) | "
                        f"risk: {r.get('risk_level','?')} | "
                        f"buy when: {r.get('entry_trigger') or 'now'} | "
                        f"exit: {r.get('exit_condition','?')} | "
                        f"rationale: {r.get('entry_rationale','?')}"
                    )
            else:
                lines.append("\nTODAY'S RECOMMENDATIONS: None yet — run the pipeline first.")
    except Exception as e:
        lines.append(f"\nTODAY'S RECOMMENDATIONS: Could not load ({e})")

    # --- Watchlist ---
    try:
        from storage.watchlist import load_watchlist
        wl = load_watchlist()
        for asset_type in ["stocks", "etfs", "crypto"]:
            tickers = wl.get(asset_type, [])
            if tickers:
                lines.append(f"\nWATCHLIST ({asset_type.upper()}): {', '.join(tickers)}")
    except Exception:
        pass

    return "\n".join(lines)


def _start_proxy_server():
    """
    Starts a tiny Flask proxy on port 8502.
    The chatbot iframe calls this instead of Anthropic directly.
    The real API key never leaves the server.
    """
    try:
        from flask import Flask, request, jsonify
        from flask_cors import CORS
    except ImportError:
        print("Proxy: flask or flask-cors not installed — chatbot will be disabled.")
        return

    proxy_app = Flask(__name__)
    CORS(proxy_app, origins=["http://localhost:8501", "http://127.0.0.1:8501"])

    @proxy_app.route("/chat", methods=["POST"])
    def chat():
        try:
            data    = request.get_json()
            api_key = os.getenv("ANTHROPIC_API_KEY", "")

            if not api_key:
                return jsonify({"error": "API key not configured"}), 500

            messages = data.get("messages", [])
            if not messages:
                return jsonify({"error": "No messages provided"}), 400

            # Only the tail of the conversation is billed; the browser still shows all of it.
            messages = _trim_history(messages)
            if not messages:
                return jsonify({"error": "No usable messages after trimming"}), 400

            # Prompt caching is an exact-prefix byte match, so the two halves of the system
            # prompt have to be split at their stability boundary:
            #   - system_base:    the static Argus rules, identical on every request
            #   - system_context: the live portfolio snapshot (prices, P&L, buying power),
            #                     rebuilt on every chat open and different almost every time
            # The cache breakpoint goes on the base ONLY. Content after a breakpoint isn't
            # part of the cached prefix, so the volatile half can change freely. Sending both
            # under one breakpoint (as this did previously) meant a single changed price digit
            # invalidated the whole thing — paying the ~1.25x write premium on every message
            # and never getting a read.
            system_base    = data.get("system_base", "")
            system_context = data.get("system_context", "")
            if not system_base:
                # Older client (stale browser tab) sends one pre-joined "system" string.
                # Cache it whole rather than 500 — it still beats no caching at all.
                system_base = data.get("system", "")

            system_field = []
            if system_base:
                system_field.append({
                    "type": "text",
                    "text": system_base,
                    "cache_control": {"type": "ephemeral"},
                })
            if system_context:
                system_field.append({"type": "text", "text": system_context})

            body = {
                "model":      CLAUDE_MODEL,
                "max_tokens": CHAT_MAX_TOKENS,
                "messages":   messages,
            }
            if system_field:
                body["system"] = system_field

            resp = _requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type":      "application/json",
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                },
                json=body,
                timeout=30,
            )
            payload = resp.json()

            # Record any Buy/Watch ideas in this reply so the entry checker can alert
            # when their "buy when" level is hit. Never let this break the reply.
            if resp.status_code == 200:
                _log_chat_usage(payload.get("usage") or {}, len(messages))
                try:
                    reply_text = "".join(
                        b.get("text", "") for b in payload.get("content", [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    _capture_chat_suggestions(reply_text)
                except Exception as cap_err:
                    print(f"Chat capture skipped: {cap_err}")

            return jsonify(payload), resp.status_code

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @proxy_app.route("/context", methods=["GET"])
    def context():
        """Returns a real-time snapshot of the user's portfolio and recommendations."""
        try:
            ctx = _build_argus_context()
            return jsonify({"context": ctx}), 200
        except Exception as e:
            return jsonify({"context": "", "error": str(e)}), 200

    @proxy_app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"}), 200

    thread = threading.Thread(
        target=lambda: proxy_app.run(
            host="127.0.0.1",
            port=8502,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
    )
    thread.start()
    print("Proxy: chatbot proxy server started on localhost:8502")

if "proxy_started" not in st.session_state:
    _start_proxy_server()
    st.session_state.proxy_started = True



def save_cache(recommendations, prices, last_run):
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                existing = json.load(f)
            if existing.get("recommendations"):
                with open(CACHE_BACKUP_FILE, "w") as f:
                    json.dump(existing, f)
        except Exception:
            pass
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "date":            datetime.now().strftime("%Y-%m-%d"),
            "last_run":        last_run,
            "recommendations": recommendations,
            "prices":          prices,
        }, f)


def load_cache() -> dict | None:

    def try_load(path) -> dict | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return None

    data = try_load(CACHE_FILE)
    if data and data.get("date") == datetime.now().strftime("%Y-%m-%d"):
        return data

    backup = try_load(CACHE_BACKUP_FILE)
    if backup and backup.get("date") == datetime.now().strftime("%Y-%m-%d"):
        st.toast("Loaded from backup cache — today's pipeline may have failed mid-run.")
        return backup

    return None


# --- Page config ---
st.set_page_config(
    page_title="Argus",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 Argus")
st.caption("AI-powered market intelligence. Experimental — not financial advice.")

# --- Market session + live buying power badge (at-a-glance, mirrors the chatbot) ---
try:
    _sess = market_session()
    _hdr_l, _hdr_r = st.columns([3, 2])
    with _hdr_l:
        st.markdown(f"**{_sess['badge']}**  ·  {_sess['stamp']}")
    with _hdr_r:
        from ingestion.robinhood import is_available as _rh_avail
        if _rh_avail():
            _bp = _live_buying_power()
            if _bp is not None:
                st.markdown(f"**💵 Buying power:** \\${_bp:,.2f}")
except Exception:
    pass  # badge is informational — never block the dashboard on it

# Mock mode banner
if os.getenv("MOCK_MODE", "false").lower() == "true":
    st.warning("⚠️ MOCK MODE active — showing test data. No real Claude API calls. Set MOCK_MODE=false in .env for real analysis.")

# --- Sidebar ---
with st.sidebar:
    st.header("Settings")

    # Budget = live Robinhood buying power (single source of truth — no manual entry).
    budget = _effective_budget()
    if budget > 0:
        st.metric("Budget = live buying power", f"\\${budget:,.2f}")
        st.caption(f"Allocations size to your real Robinhood cash. Dollar allocation runs at "
                   f"\\${MIN_ALLOCATION_BUDGET:,.0f}+; below that, ideas still show with \\$0.")
    else:
        st.metric("Budget = live buying power", "—")
        st.caption("Connect Robinhood (credentials in .env) to size positions. "
                   "Ideas still show with \\$0 until buying power is available.")

    st.divider()

    st.subheader("Asset types")
    show_stocks = st.checkbox("US Stocks",  value=True)
    show_etfs   = st.checkbox("ETFs",       value=False)
    show_crypto = st.checkbox("Crypto",     value=False)

    if show_etfs or show_crypto:
        st.info("ETF and crypto news will be included in the next pipeline run.")

    st.divider()

    run_button = st.button("🔄 Run pipeline", use_container_width=True, type="primary")
    st.caption("Fetches fresh news, scores it, and runs Claude analysis. Takes ~30 seconds. "
               "💸 Each run costs Claude tokens — news rarely shifts intraday, so **once a day is "
               "usually enough**; re-running the same day mostly re-spends for the same read.")

    # Robinhood sync
    from ingestion.robinhood import is_available as rh_available, fetch_positions as rh_fetch, fetch_buying_power as rh_buying_power
    if rh_available():
        st.divider()
        st.subheader("Robinhood")

        if st.button("💵 Refresh buying power", use_container_width=True):
            with st.spinner("Reading Robinhood buying power..."):
                bp = _live_buying_power(force=True)
            if bp is None:
                st.error("Could not read buying power. Check credentials in .env.")
            else:
                st.success(f"Live buying power: ${bp:,.2f} — this is your budget.")
                st.rerun()
        if st.button("🔄 Sync positions", use_container_width=True):
            with st.spinner("Connecting to Robinhood..."):
                rh_positions = rh_fetch()
            if rh_positions:
                synced = 0
                skipped = 0
                existing_tickers = {p["ticker"] for p in get_open_positions()}
                for rp in rh_positions:
                    if rp["ticker"] in existing_tickers:
                        skipped += 1
                        continue
                    add_position(
                        ticker=          rp["ticker"],
                        company_name=    rp["company_name"],
                        reference_price= rp["avg_cost"],
                        exit_condition=  "target 10% gain, stop loss at 5%",
                        direction=       "buy",
                        confidence=      0.0,
                        source_title=    "Robinhood sync",
                    )
                    update_amount_invested(rp["ticker"], rp["amount_invested"])
                    synced += 1
                st.success(f"Synced {synced} positions. Skipped {skipped} already in Argus.")
                if synced > 0:
                    st.rerun()
            else:
                st.error("Could not fetch Robinhood positions. Check credentials in .env.")
        st.caption("Read-only — imports positions, does not trade. Synced positions get a default 10% gain / 5% stop exit you can edit under My Positions → Manage positions.")

# --- Session state ---
if "recommendations" not in st.session_state:
    cache = load_cache()
    if cache:
        st.session_state.recommendations = cache["recommendations"]
        st.session_state.prices          = cache["prices"]
        st.session_state.last_run        = cache["last_run"]
        st.session_state._from_cache     = True
    else:
        st.session_state.recommendations = None
        st.session_state.prices          = {}
        st.session_state.last_run        = None
        st.session_state._from_cache     = False

if "last_run" not in st.session_state:
    st.session_state.last_run = None
if "prices" not in st.session_state:
    st.session_state.prices = {}

# --- Run pipeline ---
if run_button:
    with st.spinner("Fetching news and running analysis... this takes about 30 seconds."):
        try:
            recs = run_ingestion_and_analysis(
                include_stocks=show_stocks,
                include_etfs=show_etfs,
                include_crypto=show_crypto,
            )
            st.session_state.recommendations = recs

            tickers = [r["ticker"] for r in recs if r.get("ticker")]
            st.session_state.prices = fetch_prices(tickers)

            last_run = datetime.now().strftime("%B %d, %Y at %I:%M %p")
            st.session_state.last_run    = last_run
            st.session_state._from_cache = False

            save_cache(recs, st.session_state.prices, last_run)
            st.success(f"Pipeline complete — {len(recs)} recommendations found.")
        except Exception as e:
            st.error(f"Pipeline error: {e}")

# --- Display results ---
# The dashboard always renders so the user can reach My Positions, Watch List,
# and History without being forced to run the pipeline first. When there are no
# recommendations yet, a dismissible banner (below) nudges them to run it.
if True:
    recs        = st.session_state.recommendations or []
    allocations = calculate_allocations(recs, budget) if recs else []
    prices      = st.session_state.prices or {}

    for idx, a in enumerate(allocations):
        ticker     = a.get("ticker")
        price_data = prices.get(ticker)
        if price_data:
            a["current_price"] = f"${price_data['price']:.2f}"
            a["change_pct"]    = f"{price_data['change_pct']:+.2f}%"
        else:
            a["current_price"] = "N/A"
            a["change_pct"]    = "N/A"

    if st.session_state.last_run:
        if st.session_state.get("_from_cache"):
            st.caption(f"Loaded from today's cache — last run: {st.session_state.last_run}")
        else:
            st.caption(f"Last run: {st.session_state.last_run}")

    # Non-blocking welcome banner — shown until the user runs the pipeline or
    # dismisses it. Lets them explore the rest of the app immediately.
    if not recs and not st.session_state.get("welcome_dismissed"):
        bcol1, bcol2 = st.columns([0.93, 0.07])
        with bcol1:
            st.info(
                "👋 **No recommendations yet.** Click **🔄 Run pipeline** in the sidebar "
                "to fetch today's signals. Allocations size to your live Robinhood buying power. "
                "You can still use My Positions, Watch List, and History below."
            )
        with bcol2:
            if st.button("✕", key="dismiss_welcome", help="Dismiss"):
                st.session_state.welcome_dismissed = True
                st.rerun()

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 Today's Recommendations", "💼 Portfolio", "📌 My Positions", "🔭 Watch List", "📊 History"])

    # =========================================================
    # TAB 1 — Today's Recommendations
    # =========================================================
    with tab1:
        if not allocations:
            if not recs:
                st.info("No recommendations yet. Click **🔄 Run pipeline** in the sidebar to fetch today's signals.")
            else:
                st.warning("No actionable recommendations after filtering. Try running the pipeline again.")
        else:
            # If sizing is $0 because buying power couldn't be read (Robinhood session
            # expired / not connected), say so — otherwise an all-$0 table looks like a
            # dead market when it's really just a login refresh needed.
            if budget <= 0:
                st.warning(
                    "💤 **Buying power unavailable — showing analysis only, \\$0 sizing.** "
                    "This usually means the Robinhood session expired. Re-authenticate "
                    "(run `python ingestion\\robinhood.py` once and approve the app prompt), "
                    "then **💵 Refresh buying power** in the sidebar. The recommendations below "
                    "are still today's real read."
                )
            col1, col2, col3, col4, col5 = st.columns(5)
            buy_count     = sum(1 for a in allocations if a["direction"] == "buy")
            short_count   = sum(1 for a in allocations if a["direction"] == "short")
            watch_count   = sum(1 for a in allocations if a["direction"] == "watch")
            flagged_count = sum(1 for a in allocations if a["flagged"])

            col1.metric("Total stocks",  len(allocations))
            col2.metric("Buy signals",   buy_count)
            col3.metric("🔻 Short signals", short_count)
            col4.metric("Watch signals", watch_count)
            col5.metric("⚠ Flagged",     flagged_count)

            st.divider()
            st.subheader("Portfolio allocation")

            # Ensure highly_recommended exists even for old cache data
            for a in allocations:
                a.setdefault("highly_recommended", False)
                a["highly_recommended_display"] = "⭐" if a["highly_recommended"] else ""

            for a in allocations:
                a.setdefault("conviction", None)
                a.setdefault("entry_trigger", "")

            df = pd.DataFrame(allocations)
            # Merge the two flag columns into one narrow badge column, and drop Company
            # (it's in the expander title right below) — both changes buy horizontal room
            # so the table fits without scrolling right.
            df["flags"] = [
                ("⭐" if a.get("highly_recommended") else "") + ("⚠" if a.get("flagged") else "")
                for a in allocations
            ]

            # The analyst writes long prose triggers (100-180 chars). Truncate for the
            # table so each cell fits the 2-line row height without forcing a horizontal
            # scroll; the FULL text is shown in the "Stock details" expander below.
            def _short(text, limit=72):
                t = " ".join(str(text or "").split())
                return t if len(t) <= limit else t[:limit - 1].rstrip(" ,.;") + "…"

            df["entry_trigger"] = df["entry_trigger"].map(_short)
            df["exit_condition"] = df["exit_condition"].map(_short)
            df = df[[
                "ticker", "direction",
                "current_price", "change_pct",
                "dollar_amount", "percentage",
                "risk_level", "conviction", "confidence_score",
                "entry_trigger", "exit_condition", "flags"
            ]].rename(columns={
                "ticker":             "Ticker",
                "direction":          "Direction",
                "current_price":      "Price",
                "change_pct":         "Today",
                "dollar_amount":      "Amount ($)",
                "percentage":         "Alloc %",
                "risk_level":         "Risk",
                "conviction":         "Conviction",
                "confidence_score":   "Confidence",
                "entry_trigger":      "Buy when",
                "exit_condition":     "Sell when",
                "flags":              "Flags",
            })

            def color_direction(val):
                if val == "buy":   return "color: #2ecc71; font-weight: bold"
                if val == "short": return "color: #e74c3c; font-weight: bold"
                if val == "watch": return "color: #f39c12"
                return "color: #e74c3c"

            def color_risk(val):
                if val == "low":    return "color: #2ecc71"
                if val == "medium": return "color: #f39c12"
                return "color: #e74c3c; font-weight: bold"

            def color_change(val):
                if val == "N/A":        return ""
                if val.startswith("+"): return "color: #2ecc71"
                if val.startswith("-"): return "color: #e74c3c"
                return ""

            def highlight_hr(row):
                if "⭐" in str(row.get("Flags", "")):
                    return ["background-color: rgba(255, 215, 0, 0.08); border-left: 3px solid #FFD700"] * len(row)
                return [""] * len(row)

            styled_df = (
                df.style
                .apply(highlight_hr, axis=1)
                .map(color_direction, subset=["Direction"])
                .map(color_risk,      subset=["Risk"])
                .map(color_change,    subset=["Today"])
                .format({
                    "Amount ($)": "${:.2f}",
                    "Alloc %":    "{:.1f}%",
                    "Confidence": "{:.2f}",
                    "Conviction": lambda v: f"{int(v)}" if pd.notna(v) else "—",
                })
            )

            # Fixed narrow widths on the numeric columns leave the rest of the width for
            # the two long text columns, so nothing needs horizontal scrolling. Double
            # row height lets "Buy when"/"Sell when" wrap onto a second line.
            # Widths are tuned so all 12 columns sum to roughly the content area of a
            # normal desktop window — no horizontal scrolling — leaving the remainder to
            # the two text columns, which wrap onto the 2-line row height.
            _narrow = {"Direction": 74, "Price": 74, "Today": 72, "Amount ($)": 84,
                       "Alloc %": 72, "Risk": 74, "Conviction": 84, "Confidence": 84}
            # Size the table to show every row at once (no inner vertical scrollbar),
            # capped so a long list still can't push the rest of the page off-screen.
            _table_height = min(len(df) * 70 + 45, 800)
            st.dataframe(
                styled_df,
                use_container_width=True,
                hide_index=True,
                row_height=70,
                height=_table_height,
                column_config={
                    "Ticker":    st.column_config.TextColumn("Ticker", width=64, pinned=True),
                    "Flags":     st.column_config.TextColumn("Flags", width=48),
                    "Buy when":  st.column_config.TextColumn("Buy when",  width=215),
                    "Sell when": st.column_config.TextColumn("Sell when", width=215),
                    **{c: st.column_config.Column(c, width=w) for c, w in _narrow.items()},
                },
            )
            # NOTE: escape every '$' as '\$' — Streamlit renders paired '$...$' as LaTeX,
            # which silently ate the dollar signs and mangled this caption.
            st.caption(
                "ℹ️ **Conviction** (0-100) — the analyst's EDGE score; drives position size.  "
                "**Confidence** — source credibility only (1.0 = SEC filing, 0.15 = Reddit), NOT trade edge.  "
                "**Amount (\\$)** — \\$0.00 means watch only, no capital allocated.  "
                "**Flags** — ⭐ highly recommended, ⚠ unverified source (treat with extra caution).  "
                "Buy/Sell text is shortened here — full wording is in **Stock details** below."
            )

            col_export, col_spacer = st.columns([1, 4])
            with col_export:
                if st.button("📤 Export to Google Sheets", use_container_width=True):
                    from storage.sheets import export_to_sheets
                    with st.spinner("Exporting..."):
                        success = export_to_sheets(allocations, budget)
                    if success:
                        sheet_id  = os.getenv("GOOGLE_SHEET_ID", "")
                        sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                        st.success(f"Exported! [Open sheet]({sheet_url})")
                    else:
                        st.error("Export failed — check your Google credentials and Sheet ID in .env")

            st.divider()
            st.subheader("Stock details")

            open_tickers = {p["ticker"] for p in get_open_positions()}

            for idx, a in enumerate(allocations):
                flag_label      = " ⚠ Unverified source" if a["flagged"] else ""
                direction_emoji = {"buy": "🟢", "short": "🔻", "watch": "🟡"}.get(a["direction"], "🟡")
                hr_badge        = " ⭐ HIGHLY RECOMMENDED" if a.get("highly_recommended") else ""
                amount_label    = f"short ${a['dollar_amount']:.2f}" if a["direction"] == "short" else f"${a['dollar_amount']:.2f}"
                is_open         = a["ticker"] in open_tickers

                with st.expander(
                    f"{direction_emoji} {a['ticker']} — {a['company_name']} "
                    f"| {amount_label} ({a['percentage']:.1f}%){flag_label}{hr_badge}"
                ):
                    # Colored bar — green for buy, red for short, orange for watch
                    bar_color = (
                        "#FFD700" if a.get("highly_recommended")
                        else "#2ecc71" if a["direction"] == "buy"
                        else "#e74c3c" if a["direction"] == "short"
                        else "#f39c12"
                    )
                    st.markdown(
                        f'<div style="height:3px; background:{bar_color}; border-radius:2px; margin-bottom:12px"></div>',
                        unsafe_allow_html=True,
                    )
                    if a.get("highly_recommended"):
                        st.markdown(
                            '<div style="background:rgba(255,215,0,0.1); border:1px solid #FFD700; '
                            'border-radius:8px; padding:8px 14px; margin-bottom:12px; '
                            'color:#FFD700; font-size:13px; font-weight:700;">⭐ Highly Recommended — '
                            'Strong catalyst, high-conviction signal, aggressive targets set.</div>',
                            unsafe_allow_html=True,
                        )
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Direction",  a["direction"].upper())
                    c2.metric("Risk",       a["risk_level"].upper())
                    conv = a.get("conviction")
                    c3.metric("Conviction", f"{int(conv)}" if conv is not None else "—",
                              help="The analyst's EDGE score (0-100): how strong/timely/un-priced-in the opportunity is. Drives position size. Separate from source credibility.")
                    c4.metric("Confidence", f"{a['confidence_score']:.2f}",
                              help="SOURCE CREDIBILITY (how much to trust the report). 1.0 = SEC filing, 0.7 = Finnhub, 0.15 = Reddit. NOT a measure of trade edge — see Conviction.")

                    why_label = {"buy": "Why buy", "short": "Why short", "watch": "Why watch"}.get(a["direction"], "Why watch")
                    st.markdown(f"**{why_label}:** {a['entry_rationale']}")
                    trigger = a.get("entry_trigger")
                    if trigger and trigger.lower() not in ("now", "n/a", ""):
                        st.markdown(f"**Buy when:** {trigger}")
                        # Pin this watch so the entry checker keeps monitoring it even
                        # after a pipeline rerun overwrites pipeline_cache.json.
                        from storage.entry_watch import add_pinned, remove_pinned, is_pinned
                        pin_key = f"pin_{idx}_{a['ticker']}"
                        if is_pinned(a["ticker"]):
                            st.caption("👁 Watching — you'll get an email when this trigger is hit.")
                            if st.button("✕ Stop watching", key=f"unpin_{pin_key}"):
                                remove_pinned(a["ticker"])
                                st.success(f"Stopped watching {a['ticker']}.")
                                st.rerun()
                        else:
                            if st.button("👁 Watch this trigger", key=pin_key,
                                         help="Keep monitoring this 'buy when' level even after "
                                              "the pipeline reruns. Emails you when it's hit."):
                                add_pinned(a["ticker"], a["company_name"], trigger,
                                           a.get("exit_condition", ""))
                                st.success(f"Watching {a['ticker']} — alert when the trigger hits.")
                                st.rerun()
                    st.markdown(f"**Sell when:** {a['exit_condition']}")
                    st.markdown(f"**Based on:** _{a['source_title']}_")

                    # White paper and info links for crypto assets
                    
                    if a.get("asset_type") == "crypto":
                        coin_id = TICKER_TO_COINGECKO_ID.get(a.get("ticker", ""), "")
                        if coin_id:
                            st.markdown(
                                f"🔗 [White paper](https://www.coingecko.com/en/coins/{coin_id}) · "
                                f"[CoinMarketCap](https://coinmarketcap.com/currencies/{coin_id}/)"
                            )

                    if a["flagged"]:
                        st.warning(
                            "This recommendation is based on an unverified source. "
                            "Treat with extra caution and verify independently before acting."
                        )

                    st.divider()
                    if is_open:
                        st.success(f"✓ {a['ticker']} is already in your open positions.")
                    else:
                        # --- Add to positions UI ---
                        ticker_key = f"{idx}_{a['ticker']}"

                        # Toggle: did you buy at a different price?
                        different_price = st.checkbox(
                            "I bought this at a different price",
                            key=f"diff_price_toggle_{ticker_key}",
                        )

                        ref_price    = None
                        entry_date   = datetime.now().strftime("%Y-%m-%d")
                        price_source = "market"

                        if different_price:
                            col_price, col_date = st.columns(2)

                            with col_price:
                                manual_ref = st.number_input(
                                    "Price you paid per share ($)",
                                    min_value=0.01,
                                    value=0.01,
                                    step=0.01,
                                    key=f"manual_price_{ticker_key}",
                                )

                            with col_date:
                                # Quick date buttons + date picker
                                st.markdown("**When did you buy it?**")
                                d_col1, d_col2, d_col3 = st.columns(3)
                                with d_col1:
                                    if st.button("Today", key=f"date_today_{ticker_key}", use_container_width=True):
                                        st.session_state[f"entry_date_{ticker_key}"] = datetime.now().strftime("%Y-%m-%d")
                                with d_col2:
                                    if st.button("Yesterday", key=f"date_yest_{ticker_key}", use_container_width=True):
                                        from datetime import timedelta
                                        st.session_state[f"entry_date_{ticker_key}"] = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                                with d_col3:
                                    st.write("")  # spacer

                                
                                picked_date = st.date_input(
                                    "Or pick a date",
                                    value=datetime.today().date(),
                                    max_value=datetime.today().date(),
                                    key=f"date_picker_{ticker_key}",
                                    label_visibility="collapsed",
                                )
                                entry_date = picked_date.strftime("%Y-%m-%d")

                            # Validate price against market price
                            if manual_ref > 0.01:
                                market_data  = prices.get(a["ticker"])
                                market_price = market_data["price"] if market_data else None

                                if market_price:
                                    ratio = manual_ref / market_price
                                    if ratio < 0.5 or ratio > 2.0:
                                        st.warning(
                                            f"⚠️ You entered ${manual_ref:.2f} but the current market price is "
                                            f"${market_price:.2f}. That's a {abs(1-ratio)*100:.0f}% difference. "
                                            f"Double-check before adding."
                                        )

                                ref_price    = manual_ref
                                price_source = f"manual entry (${manual_ref:.2f})"

                        # Add button — always visible
                        if st.button(
                            f"📌 Add {a['ticker']} to positions",
                            key=f"add_pos_{ticker_key}",
                            use_container_width=True,
                            type="primary",
                        ):
                            if different_price and manual_ref <= 0.01:
                                st.error("Enter the price you paid before adding.")
                            else:
                                if not different_price:
                                    # Fetch current market price
                                    with st.spinner(f"Fetching current price for {a['ticker']}..."):
                                        price_data = fetch_prices([a["ticker"]])
                                        pd_entry   = price_data.get(a["ticker"])
                                        if pd_entry:
                                            ref_price    = pd_entry["price"]
                                            price_source = f"market (${ref_price:.2f})"
                                        else:
                                            st.error(f"Could not fetch price for {a['ticker']}. Enter it manually.")

                                if ref_price:
                                    add_position(
                                        ticker=          a["ticker"],
                                        company_name=    a["company_name"],
                                        reference_price= ref_price,
                                        exit_condition=  a["exit_condition"],
                                        direction=       a["direction"],
                                        confidence=      a["confidence_score"],
                                        source_title=    a["source_title"],
                                        entry_date=      entry_date,
                                    )
                                    open_tickers.add(a["ticker"])
                                    st.success(
                                        f"✓ {a['ticker']} added to positions "
                                        f"at {price_source}, entry date {entry_date}."
                                    )

    # =========================================================
    # TAB 2 — Portfolio Overview
    # =========================================================
    with tab2:
        st.subheader("Portfolio overview")
        st.caption("Your real invested money across all open positions.")

        open_positions = get_open_positions()
        # Long-only money graph: shorts use a different P&L model (cash in, owe shares),
        # so exclude them here to keep the invested/current-value math correct. Shorts are
        # still shown in My Positions with inverted P&L.
        invested_positions = [
            p for p in open_positions
            if p.get("amount_invested", 0) > 0 and p.get("direction") != "short"
        ]

        if not invested_positions:
            st.info(
                "No investment amounts recorded yet. "
                "Go to **My Positions** → expand a position → set **Amount invested ($)**."
            )
        else:
            # Fetch live prices for all invested positions
            inv_tickers = [p["ticker"] for p in invested_positions]
            with st.spinner("Fetching live prices..."):
                inv_prices = fetch_prices(inv_tickers)

            # Calculate portfolio summary
            total_invested    = sum(p.get("amount_invested", 0) for p in invested_positions)
            total_current     = 0.0
            position_data     = []

            for p in invested_positions:
                ticker         = p["ticker"]
                amount_inv     = p.get("amount_invested", 0)
                entry_price    = p.get("manual_price") or p.get("reference_price", 1)
                shares         = amount_inv / entry_price if entry_price > 0 else 0
                live           = inv_prices.get(ticker)
                live_price     = live["price"] if live else entry_price
                current_value  = shares * live_price
                pnl_dollars    = current_value - amount_inv
                pnl_pct        = ((live_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

                total_current += current_value
                position_data.append({
                    "ticker":        ticker,
                    "company":       p["company_name"],
                    "entry_date":    p.get("entry_date", "—"),
                    "amount_inv":    amount_inv,
                    "shares":        shares,
                    "entry_price":   entry_price,
                    "live_price":    live_price,
                    "current_value": current_value,
                    "pnl_dollars":   pnl_dollars,
                    "pnl_pct":       pnl_pct,
                })

            total_pnl_dollars = total_current - total_invested
            total_pnl_pct     = ((total_current - total_invested) / total_invested * 100) if total_invested > 0 else 0

            # Summary metrics
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total invested",  f"${total_invested:,.2f}")
            m2.metric("Current value",   f"${total_current:,.2f}")
            m3.metric("Total P&L",       f"${total_pnl_dollars:+,.2f}", f"{total_pnl_pct:+.1f}%")
            m4.metric("Positions",       len(invested_positions))

            st.divider()

            # --- Portfolio trend graph ---
            st.subheader("Portfolio value over time")

            import yfinance as yf
            import pandas as pd

            # Build combined daily portfolio value from each position's entry date
            all_dates    = pd.Series(dtype=float)
            earliest_date = None

            for pd_pos in position_data:
                try:
                    entry_date_str = pd_pos["entry_date"]
                    if entry_date_str == "—":
                        continue

                    # Find earliest date across all positions
                    entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d")
                    if earliest_date is None or entry_dt < earliest_date:
                        earliest_date = entry_dt

                    # Fetch daily history from entry date to today
                    hist = yf.Ticker(pd_pos["ticker"]).history(start=entry_date_str, interval="1d")
                    if hist.empty:
                        continue

                    # Value of this position on each day = shares × daily close
                    daily_value = hist["Close"] * pd_pos["shares"]
                    # Strip timezone info cleanly
                    if hasattr(daily_value.index, 'tz') and daily_value.index.tz is not None:
                        daily_value.index = daily_value.index.tz_convert("UTC").tz_localize(None)
                    daily_value.index = daily_value.index.normalize()

                    if all_dates.empty:
                        all_dates = daily_value
                    else:
                        all_dates = all_dates.add(daily_value, fill_value=0)

                except Exception as e:
                    print(f"Portfolio graph error for {pd_pos['ticker']}: {e}")
                    continue

            if not all_dates.empty:
                portfolio_df = all_dates.reset_index()
                portfolio_df.columns = ["Date", "Value ($)"]
                portfolio_df["Date"] = pd.to_datetime(portfolio_df["Date"]).dt.date

                # Add total invested line as reference
                fig = go.Figure()

                fig.add_trace(go.Scatter(
                    x=portfolio_df["Date"],
                    y=portfolio_df["Value ($)"],
                    mode="lines",
                    name="Portfolio value",
                    line=dict(color="#2ecc71", width=2),
                    fill="tozeroy",
                    fillcolor="rgba(46, 204, 113, 0.1)",
                ))

                fig.add_hline(
                    y=total_invested,
                    line_dash="dash",
                    line_color="#f39c12",
                    annotation_text=f"Invested: ${total_invested:,.0f}",
                    annotation_position="bottom right",
                )

                fig.update_layout(
                    plot_bgcolor="rgba(0,0,0,0)",
                    paper_bgcolor="rgba(0,0,0,0)",
                    font_color="#ffffff",
                    margin=dict(t=20, b=20),
                    xaxis=dict(gridcolor="rgba(255,255,255,0.1)"),
                    yaxis=dict(gridcolor="rgba(255,255,255,0.1)", tickprefix="$"),
                    hovermode="x unified",
                    showlegend=False,
                )

                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Not enough price history to build the chart yet.")

            st.divider()

            # --- Individual position breakdown ---
            st.subheader("Position breakdown")

            breakdown_rows = []
            for pd_pos in position_data:
                breakdown_rows.append({
                    "Ticker":          pd_pos["ticker"],
                    "Company":         pd_pos["company"],
                    "Invested ($)":    f"${pd_pos['amount_inv']:,.2f}",
                    "Shares":          f"{pd_pos['shares']:.4f}",
                    "Entry price":     f"${pd_pos['entry_price']:.2f}",
                    "Live price":      f"${pd_pos['live_price']:.2f}",
                    "Current value":   f"${pd_pos['current_value']:,.2f}",
                    "P&L ($)":         f"${pd_pos['pnl_dollars']:+,.2f}",
                    "P&L %":           f"{pd_pos['pnl_pct']:+.1f}%",
                })

            breakdown_df = pd.DataFrame(breakdown_rows)

            def color_portfolio_pnl(val):
                if val == "—":          return ""
                if val.startswith("+"): return "color: #2ecc71; font-weight: bold"
                if val.startswith("-"): return "color: #e74c3c; font-weight: bold"
                return ""

            styled_breakdown = (
                breakdown_df.style
                .map(color_portfolio_pnl, subset=["P&L ($)", "P&L %"])
            )
            st.dataframe(styled_breakdown, use_container_width=True, hide_index=True)
  
    # =========================================================
    # TAB 3 — My Positions
    # =========================================================
    with tab3:
        all_positions = get_open_positions()

       # --- Manual position entry ---
        with st.expander("➕ Add a position manually"):
            st.caption("Use this to track stocks you already own that weren't recommended by the pipeline.")

            m_col1, m_col2 = st.columns(2)
            with m_col1:
                m_ticker  = st.text_input("Ticker symbol", placeholder="e.g. MSFT", key="manual_ticker").strip().upper()
                m_company = st.text_input("Company name", placeholder="e.g. Microsoft Corp.", key="manual_company").strip()
                m_price   = st.number_input("Price you paid per share ($)", min_value=0.01, value=100.00, step=0.01, key="manual_entry_price")
            with m_col2:
                m_exit    = st.text_input("Exit condition", placeholder="e.g. target 10% gain, stop loss at 5%", key="manual_exit")
                m_date    = st.date_input("Date you bought it", value=datetime.today().date(), max_value=datetime.today().date(), key="manual_date")
                m_direction = st.selectbox("Direction", ["buy", "short", "watch"], key="manual_direction")

            if st.button("📌 Add to positions", key="manual_add_btn", use_container_width=True, type="primary"):
                import re as _re
                if not m_ticker:
                    st.error("Enter a ticker symbol.")
                elif not _re.match(r'^[A-Z0-9.\-]{1,10}$', m_ticker):
                    st.error("Invalid ticker symbol. Use letters, numbers, dots, or hyphens only (e.g. AAPL, BRK.B).")
                elif not m_company:
                    st.error("Enter a company name.")
                elif m_price <= 0.01:
                    st.error("Enter the price you paid per share.")
                else:
                    # Validate price against market
                    with st.spinner(f"Checking current price for {m_ticker}..."):
                        market_data = fetch_prices([m_ticker])
                        market_info = market_data.get(m_ticker)

                    if market_info:
                        ratio = m_price / market_info["price"]
                        if ratio < 0.5 or ratio > 2.0:
                            st.warning(
                                f"⚠️ You entered ${m_price:.2f} but the current market price is "
                                f"${market_info['price']:.2f}. That's a {abs(1-ratio)*100:.0f}% difference. "
                                f"The position has been added but double-check your entry price."
                            )

                    add_position(
                        ticker=          m_ticker,
                        company_name=    m_company,
                        reference_price= m_price,
                        exit_condition=  m_exit or "No exit condition set",
                        direction=       m_direction,
                        confidence=      0.0,
                        source_title=    "Manually added",
                        entry_date=      m_date.strftime("%Y-%m-%d"),
                    )
                    st.success(f"✓ {m_ticker} added at ${m_price:.2f}, bought on {m_date}.")
                    st.rerun()

        if not all_positions:
            st.info("No open positions yet. Add stocks from the Recommendations tab.")
        else:
            pos_tickers = [p["ticker"] for p in all_positions]
            with st.spinner("Fetching live prices..."):
                live_prices = fetch_prices(pos_tickers)

            col1, col2 = st.columns(2)
            col1.metric("Open positions", len(all_positions))
            col2.metric("Stocks tracked", len(pos_tickers))

            st.divider()
            st.subheader("Open positions")

            rows = []
            for p in all_positions:
                ticker     = p["ticker"]
                ref_price  = p["manual_price"] or p["reference_price"]
                live       = live_prices.get(ticker)
                live_price = live["price"] if live else None
                change_pct = ((live_price - ref_price) / ref_price * 100) if live_price else None
                if change_pct is not None and p.get("direction") == "short":
                    change_pct = -change_pct  # shorts profit when price falls
                opened     = datetime.fromisoformat(p["opened_at"]).strftime("%Y-%m-%d")
                stop_px    = _stop_loss_price(p["exit_condition"], ref_price, p.get("direction", "buy"))

                rows.append({
                    "Ticker":     ticker,
                    "Company":    p["company_name"],
                    "Side":       "SHORT" if p.get("direction") == "short" else "long",
                    "Ref Price":  f"${ref_price:.2f}",
                    "Live Price": f"${live_price:.2f}" if live_price else "N/A",
                    "Change":     f"{change_pct:+.1f}%" if change_pct is not None else "N/A",
                    "Stop @":     f"${stop_px:.2f}" if stop_px is not None else "—",
                    "Exit when":  p["exit_condition"],
                    "Bought":     p.get("entry_date") or opened,
                })

            pos_df = pd.DataFrame(rows)

            def color_pos_change(val):
                if val == "N/A":        return ""
                if val.startswith("+"): return "color: #2ecc71; font-weight: bold"
                if val.startswith("-"): return "color: #e74c3c; font-weight: bold"
                return ""

            styled_pos = pos_df.style.map(color_pos_change, subset=["Change"])
            st.dataframe(styled_pos, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("Manage positions")

            for p in all_positions:
                ticker     = p["ticker"]
                ref_price  = p["manual_price"] or p["reference_price"]
                live       = live_prices.get(ticker)
                live_price = live["price"] if live else None
                change_pct = ((live_price - ref_price) / ref_price * 100) if live_price else None
                if change_pct is not None and p.get("direction") == "short":
                    change_pct = -change_pct  # shorts profit when price falls
                opened     = datetime.fromisoformat(p["opened_at"]).strftime("%Y-%m-%d %H:%M")

                change_str = f"{change_pct:+.1f}%" if change_pct is not None else "N/A"
                emoji      = "📈" if (change_pct or 0) >= 0 else "📉"

                side_label = " 🔻SHORT" if p.get("direction") == "short" else ""
                with st.expander(f"{emoji} {ticker}{side_label} — {p['company_name']} | {change_str} since entry"):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Reference price", f"${ref_price:.2f}")
                    c2.metric("Live price",       f"${live_price:.2f}" if live_price else "N/A")
                    c3.metric("Change",           change_str)
                    entry_date_display = p.get("entry_date") or opened[:10]
                    c4.metric("Bought on", entry_date_display)

                    st.markdown(f"**Exit when:** {p['exit_condition']}")
                    stop_px = _stop_loss_price(p["exit_condition"], ref_price, p.get("direction", "buy"))
                    if stop_px is not None:
                        st.markdown(f"**Stop-loss price:** \\${stop_px:.2f}  _(from {ref_price:.2f} reference)_")
                    st.markdown(f"**Based on:** _{p['source_title']}_")

                    # White paper link for crypto positions
                    coin_id = TICKER_TO_COINGECKO_ID.get(p.get("ticker", ""), "")
                    if coin_id:
                        st.markdown(
                            f"🔗 [White paper & info](https://www.coingecko.com/en/coins/{coin_id})"
                        )

                    st.divider()

                    col_manual, col_invest, col_spacer = st.columns([2, 2, 1])
                    with col_manual:
                        new_price = st.number_input(
                            "Update reference price ($)",
                            min_value=0.01,
                            value=float(ref_price),
                            step=0.01,
                            key=f"update_price_{ticker}",
                        )
                        if st.button("💾 Update price", key=f"update_btn_{ticker}"):
                            update_manual_price(ticker, new_price)
                            st.success(f"Reference price updated to ${new_price:.2f}")

                    with col_invest:
                        current_invested = p.get("amount_invested", 0.0) or 0.0
                        new_amount = st.number_input(
                            "Amount invested ($)",
                            min_value=0.0,
                            value=float(current_invested),
                            step=10.0,
                            key=f"amount_invested_{ticker}",
                            help="How much real money you put into this position.",
                        )
                        if st.button("💾 Save amount", key=f"amount_btn_{ticker}"):
                            update_amount_invested(ticker, new_amount)
                            st.success(f"Amount invested set to ${new_amount:.2f}")

                    st.divider()

                    new_exit = st.text_input(
                        "Exit strategy",
                        value=p["exit_condition"],
                        key=f"exit_cond_{ticker}",
                        help="e.g. 'target 10% gain, stop loss at 4%'. Edit this for synced positions.",
                    )
                    if st.button("💾 Save exit strategy", key=f"exit_btn_{ticker}"):
                        update_exit_condition(ticker, new_exit)
                        st.success("Exit strategy updated.")
                        st.rerun()

                    st.divider()

                    col_close, col_reason, col_spacer = st.columns([2, 3, 2])
                    with col_reason:
                        close_reason = st.text_input(
                            "Reason for closing (optional)",
                            placeholder="e.g. 10% gain reached",
                            key=f"reason_{ticker}",
                        )
                    with col_close:
                        st.markdown("<div style='margin-top: 28px'>", unsafe_allow_html=True)
                        if st.button(
                            f"❌ Close {ticker} position",
                            key=f"close_{ticker}",
                            use_container_width=True,
                        ):
                            reason      = close_reason or f"Manually closed on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                            close_price = live_price  # use the live price we already fetched
                            close_position(ticker, reason, close_price=close_price)
                            pnl = ((close_price - ref_price) / ref_price * 100) if close_price else None
                            pnl_str = f" | P&L: {pnl:+.1f}%" if pnl is not None else ""
                            st.warning(f"{ticker} position closed at ${close_price:.2f}{pnl_str}.")
                            st.rerun()
                        st.markdown("</div>", unsafe_allow_html=True)

                    st.divider()

                    st.markdown("**Alert snooze**")
                    currently_snoozed = is_snoozed(ticker)

                    if currently_snoozed:
                        st.warning(f"Alerts for {ticker} are currently snoozed.")
                        if st.button(f"🔔 Re-enable alerts for {ticker}", key=f"unsnooze_{ticker}"):
                            clear_snooze(ticker)
                            st.success(f"Alerts re-enabled for {ticker}.")
                            st.rerun()
                    else:
                        snooze_col1, snooze_col2, snooze_col3 = st.columns(3)
                        with snooze_col1:
                            if st.button(f"😴 Snooze 1 day", key=f"snooze1_{ticker}", use_container_width=True):
                                snooze_ticker(ticker, days=1)
                                st.success(f"{ticker} snoozed for 1 day.")
                                st.rerun()
                        with snooze_col2:
                            if st.button(f"😴 Snooze 1 week", key=f"snooze7_{ticker}", use_container_width=True):
                                snooze_ticker(ticker, days=7)
                                st.success(f"{ticker} snoozed for 1 week.")
                                st.rerun()
                        with snooze_col3:
                            if st.button(f"🔕 Dismiss permanently", key=f"dismiss_{ticker}", use_container_width=True):
                                dismiss_ticker(ticker)
                                st.success(f"{ticker} alerts dismissed permanently.")
                                st.rerun()
                # --- Closed positions history ---
        closed_positions = get_closed_positions()
        if closed_positions:
            st.divider()
            st.subheader("Closed positions")

            # ── Scorecard: the honest read on whether Argus is actually earning ──
            from analysis.scorecard import compute_scorecard, spy_benchmark
            sc = compute_scorecard(closed_positions)

            if sc.get("trades"):
                net      = sc["net_dollars"]
                pf       = sc["profit_factor"]
                payoff   = sc["payoff_ratio"]
                disc     = sc["discipline"]

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Net realized",  f"${net:+,.2f}")
                m2.metric("Win rate",      f"{sc['win_rate']}%")
                m3.metric("Profit factor", f"{pf:.2f}" if pf is not None else "—",
                          help="Gross profit ÷ gross loss. >1 = profitable; >2 = strong.")
                m4.metric("Payoff ratio",  f"{payoff:.2f}" if payoff is not None else "—",
                          help="Avg win ÷ avg loss. >1 means winners are bigger than losers — "
                               "you can be profitable below a 50% win rate.")
                m5.metric("Expectancy",    f"{sc['expectancy_pct']:+.2f}%",
                          help="Average % you make per trade taken, long-run.")

                # Discipline callout — the headline leak. Let-run vs closed-early.
                lr_n, lr_avg = disc["let_run_count"], disc["let_run_avg"]
                e_n,  e_avg  = disc["early_count"],   disc["early_avg"]
                if e_n and lr_n and lr_avg - e_avg >= 2.0:
                    st.error(
                        f"⚠️ **Discipline leak — you're closing trades early.** "
                        f"The **{lr_n}** trade(s) you let run to their target/stop averaged "
                        f"**{lr_avg:+.1f}%**. The **{e_n}** you manually closed *before* either "
                        f"band averaged just **{e_avg:+.1f}%**. The picks work; cutting them early "
                        f"at breakeven is what's flattening your P&L. Let the exit plan play out."
                    )
                elif e_n:
                    st.caption(
                        f"Let-run trades: {lr_n} (avg {lr_avg:+.1f}%) · "
                        f"Closed early: {e_n} (avg {e_avg:+.1f}%)."
                    )

                # Concentration — is the profit one lucky trade or a repeatable process?
                top = sc.get("top_trade")
                share = sc.get("top_trade_profit_share")
                if top and share and share >= 50:
                    st.warning(
                        f"📌 **Concentration risk:** {top['ticker']} alone "
                        f"(${top['pnl_dollars']:+,.2f}) is **{share}%** of all gross profit. "
                        f"Strip it and the rest of the book is near breakeven — the edge isn't "
                        f"proven across trades yet, so don't over-trust a single winner."
                    )

                # SPY opportunity cost — did active trading beat just holding the index?
                bench = spy_benchmark(closed_positions)
                if bench:
                    edge = bench["edge"]
                    verdict = "beat" if edge >= 0 else "LAGGED"
                    st.caption(
                        f"vs SPY over the same dates ({bench['trades']} trades): Argus "
                        f"${bench['argus_net']:+,.2f} vs SPY ${bench['spy_net']:+,.2f} → "
                        f"you **{verdict}** the index by ${abs(edge):,.2f}."
                    )

            st.divider()

            # Closed positions table
            closed_rows = []
            for p in closed_positions:
                entry_price  = p.get("manual_price") or p.get("reference_price", 0)
                close_price  = p.get("close_price")
                pnl_pct      = p.get("pnl_pct")
                closed_at    = datetime.fromisoformat(p["closed_at"]).strftime("%Y-%m-%d") if p.get("closed_at") else "—"
                entry_date   = p.get("entry_date") or "—"

                # Calculate days held
                if p.get("entry_date") and p.get("closed_at"):
                    try:
                        entry_dt  = datetime.strptime(p["entry_date"], "%Y-%m-%d")
                        close_dt  = datetime.fromisoformat(p["closed_at"])
                        days_held = (close_dt - entry_dt).days
                    except Exception:
                        days_held = "—"
                else:
                    days_held = "—"

                closed_rows.append({
                    "Ticker":       p["ticker"],
                    "Company":      p["company_name"],
                    "Entry ($)":    f"${entry_price:.2f}",
                    "Close ($)":    f"${close_price:.2f}" if close_price else "—",
                    "P&L %":        f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—",
                    "Days held":    days_held,
                    "Reason":       p.get("close_reason", "—"),
                    "Closed":       closed_at,
                })

            closed_df = pd.DataFrame(closed_rows)

            def color_pnl(val):
                if val == "—":          return ""
                if val.startswith("+"): return "color: #2ecc71; font-weight: bold"
                if val.startswith("-"): return "color: #e74c3c; font-weight: bold"
                return ""

            styled_closed = closed_df.style.map(color_pnl, subset=["P&L %"])
            st.dataframe(styled_closed, use_container_width=True, hide_index=True)

    # =========================================================
    # TAB 4 — Watch List Editor
    # =========================================================
    with tab4:
        # --- Pinned buy triggers (entry alerts) -------------------------------
        # Central place to review/remove what the entry checker is monitoring. Without
        # this, a pin whose ticker dropped out of the recommendations was orphaned:
        # still alerting every 15 min with no UI to remove it.
        from storage.entry_watch import (
            get_pinned as _get_pinned, remove_pinned as _remove_pinned,
            get_chat_suggestions as _get_chat_sugg, set_chat_suggestions as _set_chat_sugg,
        )
        from alerts.entry_checker import _parse_triggers as _parse_trig

        # Streamlit renders paired '$...$' as LaTeX, which mangles trigger text full of
        # prices. Escape every '$' before displaying any analyst-written string.
        def _esc(s: str) -> str:
            return str(s or "").replace("$", "\\$")

        st.subheader("🔎 What Argus is monitoring")
        st.caption(
            "Everything checked every 15 minutes during market hours. **Open positions** are "
            "watched for EXIT (stop / target / time / news). **Pinned triggers** are watched for "
            "ENTRY. Owned tickers are deliberately excluded from entry alerts — you're already in."
        )

        # --- Open positions → exit alerts (auto-included, nothing to pin) ------
        st.markdown("##### 📍 Open positions — exit alerts")
        _open_pos = get_open_positions()
        if not _open_pos:
            st.caption("No open positions.")
        else:
            _pos_px = {}
            try:
                from ingestion.prices import fetch_prices as _fp3
                _pos_px = _fp3([p["ticker"] for p in _open_pos]) or {}
            except Exception:
                pass
            for p in _open_pos:
                t = p["ticker"]
                ref = p.get("manual_price") or p.get("reference_price", 0)
                q = _pos_px.get(t)
                live = q["price"] if q else ref
                pnl = ((live - ref) / ref * 100) if ref else 0
                if p.get("direction") == "short":
                    pnl = -pnl
                stop_px = _stop_loss_price(p.get("exit_condition", ""), ref, p.get("direction", "buy"))
                stop_str = f" · stop @ \\${stop_px:,.2f}" if stop_px else ""
                st.markdown(
                    f"**{t}** — entry \\${ref:,.2f} · live \\${live:,.2f} · "
                    f"P&L {pnl:+.1f}%{stop_str}"
                )
                st.caption(f"Exit when: {_esc(p.get('exit_condition', 'not set'))}")
            st.caption("Manage these under **My Positions**.")

        st.divider()
        st.markdown("##### 📌 Pinned buy triggers — entry alerts")
        _pinned = _get_pinned()
        if not _pinned:
            st.caption(
                "Nothing pinned. On a watch recommendation (Today's Recommendations → "
                "Stock details), click **👁 Watch this trigger** to get an email when its "
                "\"buy when\" level is hit — it keeps working after the pipeline reruns."
            )
        else:
            st.caption(
                f"{len(_pinned)} trigger(s) checked every 15 minutes during market hours. "
                "These survive pipeline reruns — remove any you no longer want."
            )
            _live = {}
            try:
                from ingestion.prices import fetch_prices as _fp2
                _live = _fp2([p["ticker"] for p in _pinned]) or {}
            except Exception:
                pass

            for p in _pinned:
                t = p["ticker"]
                c_info, c_btn = st.columns([9, 1])
                with c_info:
                    levels = _parse_trig(p.get("trigger_text", ""))
                    q = _live.get(t)
                    live_str = f" · live \\${q['price']:,.2f}" if q else ""
                    if levels:
                        lvl_str = ", ".join(
                            f"{'breakout' if d == 'above' else 'pullback'} \\${v:,.2f}"
                            for d, v in levels
                        )
                        st.markdown(f"**{t}** — {lvl_str}{live_str}")
                    else:
                        # No price in the trigger means the checker can never fire on it —
                        # say so instead of leaving a pin that silently does nothing.
                        st.markdown(f"**{t}** — ⚠️ no price level{live_str}")
                        st.warning(
                            "This trigger is event-based (no \\$ price), so it will NEVER "
                            "fire an alert. Remove it and watch the news instead, or re-pin "
                            "once a recommendation gives a concrete price.",
                            icon="⚠️",
                        )
                    st.caption(
                        f"Pinned {p.get('pinned_at', '?')} · "
                        f"{_esc(p.get('trigger_text', ''))[:180]}"
                    )
                with c_btn:
                    if st.button("✕", key=f"rm_pin_{t}", help=f"Stop watching {t}"):
                        _remove_pinned(t)
                        st.success(f"Removed {t} from pinned triggers.")
                        st.rerun()

        # Chat-sourced suggestions also drive entry alerts — surface them so they can
        # be cleared too (they're replaced automatically on the next chat suggestion).
        _chat_sugg = _get_chat_sugg()
        if _chat_sugg:
            with st.expander(f"💬 From Argus chat's last suggestion ({len(_chat_sugg)})"):
                st.caption(
                    "Captured from the last action list Argus chat gave. Replaced automatically "
                    "the next time it suggests moves."
                )
                for s in _chat_sugg:
                    st.markdown(
                        f"**{s['ticker']}** ({s.get('action', 'watch')}) — "
                        f"{_esc(s.get('trigger_text', ''))[:180]}"
                    )
                if st.button("Clear chat suggestions"):
                    _set_chat_sugg([])
                    st.success("Cleared.")
                    st.rerun()

        st.divider()

        st.subheader("Watch list")
        st.caption(
            "These are the tickers Finnhub monitors for company-specific news. "
            "Changes take effect on the next pipeline run."
        )

        watchlist = load_watchlist()

        for asset_type, label, default_color in [
            ("stocks", "US Stocks",    "#1e3a2f"),
            ("etfs",   "ETFs",         "#1a2e3a"),
            ("crypto", "Crypto",       "#2e1a3a"),
        ]:
            text_colors = {
                "stocks": "#2ecc71",
                "etfs":   "#3498db",
                "crypto": "#9b59b6",
            }
            text_color = text_colors[asset_type]
            tickers    = watchlist.get(asset_type, [])

            st.markdown(f"### {label} — {len(tickers)} tickers")

            if tickers:
                cols_per_row = 5
                rows = [tickers[i:i+cols_per_row] for i in range(0, len(tickers), cols_per_row)]
                for row in rows:
                    cols = st.columns(cols_per_row)
                    for i, ticker in enumerate(row):
                        with cols[i]:
                            st.markdown(
                                f"<div style='background:{default_color}; color:{text_color}; "
                                f"padding:6px 10px; border-radius:6px; text-align:center; "
                                f"font-weight:bold; margin-bottom:6px'>{ticker}</div>",
                                unsafe_allow_html=True
                            )
                            if st.button("✕", key=f"remove_{asset_type}_{ticker}", use_container_width=True):
                                remove_ticker(ticker, asset_type)
                                st.success(f"{ticker} removed from {label}.")
                                st.rerun()
            else:
                st.caption(f"No {label} tickers yet.")

            # Add ticker for this asset type
            col_input, col_add, col_spacer = st.columns([2, 1, 3])
            with col_input:
                new_ticker = st.text_input(
                    f"Add {label} ticker",
                    placeholder="e.g. PLTR",
                    label_visibility="collapsed",
                    key=f"add_input_{asset_type}",
                ).strip().upper()
            with col_add:
                if st.button(f"➕ Add", key=f"add_btn_{asset_type}", use_container_width=True):
                    if not new_ticker:
                        st.warning("Enter a ticker symbol first.")
                    elif new_ticker in watchlist.get(asset_type, []):
                        st.warning(f"{new_ticker} is already in {label}.")
                    else:
                        add_ticker(new_ticker, asset_type)
                        st.success(f"{new_ticker} added to {label}.")
                        st.rerun()

            st.divider()

        # Bulk edit and reset at the bottom
        col_reset, col_spacer = st.columns([1, 4])
        with col_reset:
            if st.button("↺ Reset all to defaults", use_container_width=True):
                reset_to_defaults()
                st.success("All watch lists reset to defaults.")
                st.rerun()

    # =========================================================
    # TAB 5 — History
    # =========================================================
    with tab5:
        st.subheader("Historical performance")
        st.caption("Based on your exported runs in Google Sheets.")

        from storage.sheets import read_history

        with st.spinner("Loading history from Google Sheets..."):
            history = read_history()

        if not history:
            st.info(
                "No history found. Export at least one pipeline run to Google Sheets "
                "using the Export button in the Recommendations tab."
            )
        else:
            df_hist = pd.DataFrame(history)

            # Clean up date column — take just the date part
            df_hist["date"] = df_hist["date"].str[:10]
            df_hist         = df_hist[df_hist["ticker"] != ""]

            # --- Summary metrics ---
            total_runs    = df_hist["date"].nunique()
            total_tickers = df_hist["ticker"].nunique()
            total_buys    = len(df_hist[df_hist["direction"] == "buy"])
            total_watches = len(df_hist[df_hist["direction"] == "watch"])

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total runs exported", total_runs)
            c2.metric("Unique tickers seen", total_tickers)
            c3.metric("Total buy signals",   total_buys)
            c4.metric("Total watch signals", total_watches)

            st.divider()

            # --- Chart 1: Most frequently recommended tickers ---
            st.subheader("Most recommended tickers")
            ticker_counts = (
                df_hist.groupby("ticker")
                .size()
                .reset_index(name="appearances")
                .sort_values("appearances", ascending=False)
                .head(15)
            )

            fig1 = px.bar(
                ticker_counts,
                x="ticker",
                y="appearances",
                color="appearances",
                color_continuous_scale="Teal",
                labels={"ticker": "Ticker", "appearances": "Times recommended"},
            )
            fig1.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#ffffff",
                showlegend=False,
                coloraxis_showscale=False,
                margin=dict(t=20, b=20),
            )
            fig1.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
            fig1.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
            st.plotly_chart(fig1, use_container_width=True)

            st.divider()

            # --- Chart 2: Direction breakdown per ticker ---
            st.subheader("Buy vs watch breakdown")
            direction_counts = (
                df_hist.groupby(["ticker", "direction"])
                .size()
                .reset_index(name="count")
            )

            color_map = {
                "buy":   "#2ecc71",
                "watch": "#f39c12",
                "avoid": "#e74c3c",
            }

            fig2 = px.bar(
                direction_counts,
                x="ticker",
                y="count",
                color="direction",
                color_discrete_map=color_map,
                labels={"ticker": "Ticker", "count": "Count", "direction": "Direction"},
                barmode="stack",
            )
            fig2.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#ffffff",
                margin=dict(t=20, b=20),
            )
            fig2.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
            fig2.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
            st.plotly_chart(fig2, use_container_width=True)

            st.divider()

            # --- Chart 3: Average confidence score per ticker ---
            st.subheader("Average confidence score by ticker")
            avg_confidence = (
                df_hist.groupby("ticker")["confidence"]
                .mean()
                .reset_index()
                .sort_values("confidence", ascending=False)
                .head(15)
            )

            fig3 = px.bar(
                avg_confidence,
                x="ticker",
                y="confidence",
                color="confidence",
                color_continuous_scale="Greens",
                range_y=[0, 1],
                labels={"ticker": "Ticker", "confidence": "Avg confidence"},
            )
            fig3.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#ffffff",
                showlegend=False,
                coloraxis_showscale=False,
                margin=dict(t=20, b=20),
            )
            fig3.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
            fig3.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
            st.plotly_chart(fig3, use_container_width=True)

            st.divider()

            # --- Chart 4: Recommended amount over time per ticker ---
            st.subheader("Allocation over time")
            st.caption("How much was suggested to invest in each ticker across runs.")

            # Ticker filter
            all_tickers = sorted(df_hist["ticker"].unique().tolist())
            selected    = st.multiselect(
                "Filter by ticker",
                options=all_tickers,
                default=all_tickers[:5],
            )

            df_filtered = df_hist[df_hist["ticker"].isin(selected)] if selected else df_hist

            fig4 = px.line(
                df_filtered,
                x="date",
                y="amount",
                color="ticker",
                markers=True,
                labels={"date": "Date", "amount": "Amount ($)", "ticker": "Ticker"},
            )
            fig4.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#ffffff",
                margin=dict(t=20, b=20),
            )
            fig4.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
            fig4.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
            st.plotly_chart(fig4, use_container_width=True)

            st.divider()

            # --- Raw data table ---
            st.subheader("Raw history")
            st.dataframe(
                df_hist[[
                    "date", "ticker", "company", "direction",
                    "amount", "allocation_pct", "risk", "confidence"
                ]].rename(columns={
                    "date":           "Date",
                    "ticker":         "Ticker",
                    "company":        "Company",
                    "direction":      "Direction",
                    "amount":         "Amount ($)",
                    "allocation_pct": "Allocation (%)",
                    "risk":           "Risk",
                    "confidence":     "Confidence",
                }),
                use_container_width=True,
                hide_index=True,
            )


# =========================================================
# ARGUS CHATBOT — Floating assistant widget
# =========================================================


st.write("")  # chatbot anchor
from streamlit.components.v1 import html as st_html

st_html("""
<script>
(function() {
  const parentDoc = window.parent.document;
  const parentWin = window.parent;

  // Chat state lives on the parent window so it survives Streamlit reruns,
  // which tear down and recreate this component's iframe on every interaction.
  if (!parentWin.argusState) {
    parentWin.argusState = { open: false, history: [], context: "" };
  }
  const state = parentWin.argusState;

  // Build the widget DOM only once (it lives in the parent document, so it
  // persists across reruns). Listeners are re-bound every run further below.
  if (!parentDoc.getElementById('argus-chat-injected')) {
  const container = parentDoc.createElement('div');
  container.id = 'argus-chat-injected';
  container.innerHTML = `
    <style>
      #argus-chat-btn {
        position: fixed; bottom: 20px; right: 20px;
        width: 52px; height: 52px; border-radius: 50%;
        background: linear-gradient(135deg, #2ecc71, #1a8a4a);
        border: none; cursor: pointer; z-index: 999999;
        box-shadow: 0 4px 24px rgba(46,204,113,0.4);
        display: flex; align-items: center; justify-content: center;
        font-size: 22px; color: white;
      }
      #argus-chat-btn:hover { transform: scale(1.08); }
      #argus-chat-panel {
        position: fixed; bottom: 85px; right: 20px;
        width: 370px; height: 500px;
        background: #0f1a14; border: 1px solid #2ecc71;
        border-radius: 16px; z-index: 999998;
        display: none; flex-direction: column;
        overflow: hidden; box-shadow: 0 8px 48px rgba(0,0,0,0.6);
        font-family: 'Segoe UI', sans-serif;
      }
      #argus-chat-panel.open { display: flex; }
      #argus-chat-header {
        padding: 14px 18px; background: #0d1f12;
        border-bottom: 1px solid rgba(46,204,113,0.3);
        display: flex; align-items: center; gap: 10px;
      }
      .argus-dot {
        width: 8px; height: 8px; border-radius: 50%;
        background: #2ecc71; box-shadow: 0 0 8px #2ecc71;
      }
      .argus-title { color: #2ecc71; font-size: 13px; font-weight: 700; text-transform: uppercase; }
      .argus-subtitle { color: rgba(255,255,255,0.4); font-size: 11px; margin-left: auto; }
      #argus-close-btn { background: none; border: none; color: rgba(255,255,255,0.4); font-size: 16px; cursor: pointer; }
      #argus-clear-btn { background: none; border: none; color: rgba(255,255,255,0.4); font-size: 15px; cursor: pointer; }
      #argus-clear-btn:hover { color: rgba(255,255,255,0.8); }
      #argus-messages {
        flex: 1; overflow-y: auto; padding: 14px;
        display: flex; flex-direction: column; gap: 12px;
      }
      .argus-msg { max-width: 85%; padding: 10px 14px; border-radius: 12px; font-size: 13px; line-height: 1.5; }
      .argus-user { align-self: flex-end; background: linear-gradient(135deg, #1a4a2e, #2ecc71); color: #fff; }
      .argus-assistant { align-self: flex-start; background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.9); border: 1px solid rgba(46,204,113,0.15); font-size: 12px; }
      .argus-disclaimer { margin-top: 6px; font-size: 10px; color: rgba(255,255,255,0.3); border-top: 1px solid rgba(255,255,255,0.1); padding-top: 6px; }
      .argus-thinking { align-self: flex-start; color: #2ecc71; font-size: 11px; padding: 8px 14px; background: rgba(46,204,113,0.05); border-radius: 12px; }
      #argus-input-area { padding: 12px 16px; border-top: 1px solid rgba(46,204,113,0.2); background: #0a1210; display: flex; gap: 8px; }
      #argus-input {
        flex: 1; background: rgba(255,255,255,0.05);
        border: 1px solid rgba(46,204,113,0.3); border-radius: 8px;
        color: #fff; padding: 10px 12px; font-size: 13px;
        resize: none; outline: none; min-height: 40px; max-height: 100px;
        font-family: 'Segoe UI', sans-serif;
      }
      #argus-send {
        background: linear-gradient(135deg, #2ecc71, #1a8a4a);
        border: none; border-radius: 8px; width: 40px; height: 40px;
        cursor: pointer; color: white; font-size: 16px;
      }
    </style>
    <button id="argus-chat-btn">🔍</button>
    <div id="argus-chat-panel">
      <div id="argus-chat-header">
        <div class="argus-dot"></div>
        <div class="argus-title">Argus Assistant</div>
        <div class="argus-subtitle">investing only</div>
        <button id="argus-clear-btn" title="Clear chat (resets token cost)">⟲</button>
        <button id="argus-close-btn">✕</button>
      </div>
      <div id="argus-messages">
        <div class="argus-msg argus-assistant">
          Hey — I'm Argus. Ask me anything about investing, how this app works, or what any of the signals mean.
          <div class="argus-disclaimer">Not financial advice. For informational purposes only.</div>
        </div>
      </div>
      <div id="argus-input-area">
        <textarea id="argus-input" placeholder="Ask about investing or how Argus works..." rows="1"></textarea>
        <button id="argus-send">➤</button>
      </div>
    </div>
  `;
  parentDoc.body.appendChild(container);
  }

  const ARGUS_SYSTEM_BASE = `You are Argus, a sharp, disciplined banker running the user's trading desk. Mandate: grow capital without chasing moves already priced in — a skipped trade costs nothing, a top bought costs real money; when unsure, prefer watch over buy. You have the user's live portfolio, positions, P&L, buying power, market status, and today's recommendations. STYLE (important): be brief and lead with the call — no preamble, no restating the question. ACTION SUMMARY FIRST: when the user asks what to do with their positions or buying power (any "what moves should I make", "what should I do", "review my portfolio" ask), you MUST open with a compact action list, ONE LINE PER TICKER, in this exact shape:
Buy — TICKER, exit 10% gain / 5% stop
Sell — TICKER, flat
Sell — TICKER, hit target
Hold — TICKER, exit 8% gain / 4% stop
Watch — TICKER, buy above $X
Verb is Buy / Sell / Hold / Watch. For Buy or Hold give the exit rule (target% gain / stop%); for Sell give a short reason (flat, hit target, thesis broke, stop hit); for Watch give the trigger price. Cover EVERY open position, plus any new Buy you propose — leave nothing for the user to guess or ask back about. After the list, put a line "Explanation:" then 2-4 tight sentences of the money logic. For any OTHER kind of question, just answer briefly (2-3 sentences) with no action list. ORDER TYPES (Robinhood mechanic — respect it): a position tagged FRACTIONAL (<1 share) can ONLY be exited with a MARKET order — NEVER tell the user to set a limit or stop-limit on it; say "market sell" (or "set a price alert and sell manually"). Only WHOLE-share positions can use limit / stop-limit orders. Read the shares tag on each position line and phrase every exit accordingly. AFFORDABILITY / EXECUTABILITY (critical — never recommend a move the user cannot actually place): SHORTS, LIMIT/STOP orders, and options all require at least ONE WHOLE share (and shorts also need a margin account). If live buying power is LESS than one share's price, NONE of those are executable for that name — do NOT recommend shorting it or putting a limit/stop on it. If the thesis is bearish but a share costs more than the buying power, say plainly it's "not actionable at your current buying power (a short needs ≥1 share ≈ $<price>)" instead of telling the user to short. A LONG can still be a FRACTIONAL market buy (dollar-based) as long as buying power ≥ ~$1, so on an expensive stock the only executable move is a small fractional buy — never a short or a limit order. Before every Buy/Short line, sanity-check the share price against buying power and never propose an action the user's cash can't place. RULES: only discuss investing/markets/how Argus works; if asked anything else say "I'm here for your portfolio and the markets — let's stick to that."; give direct actionable calls plus the one-line money logic; end with a short "Not advice — DYOR." PRIORITY CHECKS: (1) Catalyst timing — if the stock already ran on the exact news, the edge is gone → watch, not buy. (2) Conviction (0-100 = edge, drives size) beats confidence (source credibility only); a credible source on a priced-in event is still a bad buy; crypto/ETF can be high-conviction despite low-credibility sources. (3) M&A: an announced all-cash deal pins the target near offer (watch, not buy); a closed deal = delisting, skip. POSITION SIZING — PYRAMID (of the long budget, by risk tier): high-risk/high-reward = TOP ~20% (small satellite); medium-risk/medium-high-reward = CORE ~55% (most of the money); low-risk/low-reward = BASE ~25% (ballast). Risk tier picks the pool, conviction sizes within it, empty tier is held as CASH (not redistributed). When advising buys keep this shape: most fresh capital into solid medium-risk core ideas, only a small slice into high-risk shots, never over 40% in one name. DISCIPLINE (the user's real leak): they tend to hand-close trades early at breakeven, cutting winners AND losers before target/stop — the picks work when let run. Push them to respect the exit plan; a -1% to -3% wobble is not a stop. SESSION & BUYING POWER: size every idea to live buying power (never more cash than they have); market OPEN → "now"; CLOSED/weekend → "at the next open"/limit order; PRE/AFTER-HOURS → warn liquidity is thin; crypto trades 24/7. WATCHES & HONESTY: today's list always includes watches by design — on a weak day don't say "sit out"; walk the top watches and the exact trigger that flips each to a buy, and review the open book (P&L, anything near a stop/target, hold-or-close). Shorts (bearish, stocks only) profit when price falls — tight stops, never short a squeeze-prone name.`;
  async function loadContext() {
    try {
      const res = await fetch('http://localhost:8502/context');
      const data = await res.json();
      state.context = data.context || "";
    } catch(e) { state.context = ""; }
  }

  // Sent as two separate fields, NOT joined. The proxy puts the prompt cache
  // breakpoint on the base only; joining them here would make the live portfolio
  // numbers part of the cached prefix and invalidate it on every price change.
  function buildSystemContext() {
    if (!state.context) return "";
    return "=== LIVE PORTFOLIO DATA ===\\n" + state.context;
  }

  function toggle() {
    state.open = !state.open;
    parentDoc.getElementById('argus-chat-panel').className = state.open ? 'open' : '';
    if (state.open) {
      loadContext();
      setTimeout(() => parentDoc.getElementById('argus-input').focus(), 150);
    }
  }

  // The widget DOM outlives Streamlit reruns, so state.history would otherwise grow
  // for as long as the tab stays open. This is the manual reset: every message sent
  // is billed against the current history, so starting fresh is the cheapest turn.
  function clearChat() {
    state.history = [];
    parentDoc.getElementById('argus-messages').innerHTML =
      '<div class="argus-msg argus-assistant">Chat cleared — starting fresh.' +
      '<div class="argus-disclaimer">Not financial advice. For informational purposes only.</div></div>';
  }

  function appendMsg(role, text) {
    const c = parentDoc.getElementById('argus-messages');
    const d = parentDoc.createElement('div');
    d.className = 'argus-msg argus-' + role;
    if (role === 'assistant') {
      // Escape HTML FIRST so model output can never inject active markup (DOM XSS),
      // then apply our own safe **bold** / newline formatting on the escaped text.
      const esc = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
      d.innerHTML = esc.replace(/\\n/g,'<br>').replace(/[*][*](.*?)[*][*]/g,'<strong>$1</strong>') +
        '<div class="argus-disclaimer">Not financial advice — always do your own research.</div>';
    } else {
      d.textContent = text;
    }
    c.appendChild(d);
    c.scrollTop = c.scrollHeight;
  }
  
  async function send() {
    const input = parentDoc.getElementById('argus-input');
    const sendBtn = parentDoc.getElementById('argus-send');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    sendBtn.disabled = true;
    appendMsg('user', text);
    state.history.push({role:'user', content:text});
    
    const c = parentDoc.getElementById('argus-messages');
    const thinking = parentDoc.createElement('div');
    thinking.className = 'argus-thinking';
    thinking.textContent = '▋ analyzing...';
    c.appendChild(thinking);
    c.scrollTop = c.scrollHeight;
    
    try {
      const response = await fetch('http://localhost:8502/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          system_base:    ARGUS_SYSTEM_BASE,
          system_context: buildSystemContext(),
          messages:       state.history
        })
      });
      const data = await response.json();
      thinking.remove();
      if (data.content && data.content[0]) {
        const reply = data.content[0].text;
        state.history.push({role:'assistant', content:reply});
        appendMsg('assistant', reply);
      } else {
        appendMsg('assistant', 'Error: ' + JSON.stringify(data));
      }
    } catch(e) {
      thinking.remove();
      appendMsg('assistant', 'Could not reach the API.');
    }
    sendBtn.disabled = false;
    input.focus();
  }
  
  // Re-bind listeners on every run. The widget DOM persists in the parent
  // document, but its old listeners were closures owned by a previous (now
  // destroyed) iframe and are dead. Cloning each control drops those stale
  // listeners; we then attach fresh ones from this live iframe. This is what
  // fixes the "button click does nothing until I refresh" bug.
  function rebind(id, event, handler) {
    const el = parentDoc.getElementById(id);
    if (!el) return null;
    const fresh = el.cloneNode(true);
    el.parentNode.replaceChild(fresh, el);
    fresh.addEventListener(event, handler);
    return fresh;
  }
  rebind('argus-chat-btn', 'click', toggle);
  rebind('argus-close-btn', 'click', toggle);
  rebind('argus-clear-btn', 'click', clearChat);
  rebind('argus-send', 'click', send);
  rebind('argus-input', 'keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  // Reflect persisted open/closed state in case a rerun happened mid-session.
  parentDoc.getElementById('argus-chat-panel').className = state.open ? 'open' : '';
})();
</script>
""", height=0)
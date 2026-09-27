import streamlit as st
import sys
import os
import json
from datetime import datetime

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
from storage.positions import add_position, get_open_positions, get_closed_positions, update_amount_invested
from storage.watchlist import load_watchlist
from dashboard.common import _cached_prices, _live_buying_power, _suggested_exit
from dashboard.tabs import (
    recommendations as recommendations_tab,
    portfolio as portfolio_tab,
    positions as positions_tab,
    watchlist as watchlist_tab,
    history as history_tab,
    agent as agent_tab,
)
from dotenv import load_dotenv
load_dotenv()


# --- File paths ---
CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache.json")
CACHE_BACKUP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache_backup.json")
# Budget is driven ENTIRELY by live Robinhood buying power (see _effective_budget below).
# There is no manual budget setting — a hand-typed budget could disagree with the real
# cash the user has, which confused Argus chat. Buying power is the single source of truth.


# --- Helper functions (must be defined before any UI code) ---


# =========================================================
# CHATBOT PROXY SERVER — keeps API key server-side
# =========================================================
import threading
import requests as _requests

# Market-session logic lives in the shared market_hours module (used by the dashboard
# header badge, the chatbot context, and the alert checker — one source of truth).
from market_hours import market_session, market_status_line


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
        # Decrement the local credit ledger (Anthropic exposes no live balance API).
        from llm_budget import cost_of, record_cost
        record_cost(cost_of(CLAUDE_MODEL, usage.get("input_tokens", 0),
                            usage.get("output_tokens", 0), cache_read))
    except Exception:
        pass


def _effective_budget() -> float:
    """
    The allocation budget = live Robinhood buying power, always. Single source of
    truth so the dollar allocations can never disagree with the real cash available.
    Returns 0.0 when buying power can't be read (not connected / read failed) — recs
    then show at $0 (analysis still visible) rather than sizing against a fake number.
    """
    bp = _live_buying_power()
    return float(bp) if bp is not None else 0.0


def _structure_exit_condition(ticker: str, asset_type: str = "stock"):
    """R24 structure exit as a 'target X% gain, stop loss at Y%' string, or None when there's
    no usable price structure. Blue-sky (no overhead resistance) → target a measured move of
    2× the ATR stop so reward stays ≥ 2× risk. Used so SYNCED positions get a per-chart exit
    instead of a flat 10/5 guess (same math as the My Positions 'Apply' button)."""
    kl = _suggested_exit(ticker, asset_type)
    if not kl:
        return None
    stp = kl.get("stop_pct_atr")
    if stp is None:
        return None
    tgt = kl.get("target_pct_resist")
    if tgt is None:
        tgt = round(2 * stp, 1)
    return f"target {tgt}% gain, stop loss at {stp}%"


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
        # Capture Buy / Short / Sell / Watch so a bearish chat call isn't lost or misread as a buy
        # (that flipped a TLT short into a bought call). Direction is resolved downstream from the
        # verb + text so "Buy — TLT (short)" still becomes a short.
        pattern = re.compile(r"^\s*([Bb]uy|[Ss]hort|[Ss]ell|[Ww]atch)\s*[—–\-:]+\s*\$?([A-Z]{1,6})\b[,\s]*(.*)$",
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
        from ingestion.account_reads import is_available
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
        positions = get_open_positions()
        if positions:
            tickers    = [p["ticker"] for p in positions]
            live_prices = _cached_prices(tuple(sorted(set(tickers))))
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
        from ingestion.account_reads import is_available as _rh_avail
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
    else:
        st.metric("Budget = live buying power", "—")

    st.divider()

    st.subheader("Asset types")
    show_stocks = st.checkbox("US Stocks",  value=True)
    show_etfs   = st.checkbox("ETFs",       value=False)
    show_crypto = st.checkbox("Crypto",     value=False)

    if show_etfs or show_crypto:
        st.info("ETF and crypto news will be included in the next pipeline run.")

    st.divider()

    run_button = st.button("🔄 Run pipeline", use_container_width=True, type="primary")

    # Robinhood sync
    from ingestion.account_reads import is_available as rh_available, positions as rh_fetch
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
                    _crypto = bool(TICKER_TO_COINGECKO_ID.get(rp["ticker"], ""))
                    exit_cond = (_structure_exit_condition(rp["ticker"], "crypto" if _crypto else "stock")
                                 or "target 10% gain, stop loss at 5%")
                    add_position(
                        ticker=          rp["ticker"],
                        company_name=    rp["company_name"],
                        reference_price= rp["avg_cost"],
                        exit_condition=  exit_cond,
                        direction=       "buy",
                        confidence=      0.0,
                        source_title=    "Robinhood sync",
                    )
                    update_amount_invested(rp["ticker"], rp["amount_invested"])
                    synced += 1
                if synced:
                    st.success(f"Synced {synced} new. Skipped {skipped} already tracked.")
                else:
                    st.info(f"Nothing new — all {skipped} equity/ETF position(s) already tracked.")
                st.caption("⚠️ Crypto (DOGE, XRP, etc.) can't sync — the Robinhood Trading MCP has no "
                           "crypto endpoint. Add coins manually below (Manage positions → add).")
                if synced > 0:
                    st.rerun()
            else:
                st.info("No equity/ETF positions to sync (crypto isn't readable via the MCP — "
                        "add coins manually below).")

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

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["📈 Today's Recommendations", "💼 Portfolio", "📌 My Positions", "🔭 Watch List", "📊 History", "🤖 Agent"])

# =========================================================
# TAB 1 — Today's Recommendations
# =========================================================
with tab1:
    recommendations_tab.render(allocations=allocations, budget=budget, prices=prices, recs=recs)

# =========================================================
# TAB 2 — Portfolio Overview
# =========================================================
with tab2:
    portfolio_tab.render()

# =========================================================
# TAB 3 — My Positions
# =========================================================
with tab3:
    positions_tab.render()

# =========================================================
# TAB 4 — Watch List Editor
# =========================================================
with tab4:
    watchlist_tab.render()

# =========================================================
# TAB 5 — History
# =========================================================
with tab5:
    history_tab.render()


with tab6:
    agent_tab.render()


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
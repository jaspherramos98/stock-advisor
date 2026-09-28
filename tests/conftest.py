import pytest


@pytest.fixture(autouse=True)
def _isolated_decision_log(tmp_path, monkeypatch):
    """Every agent entry/exit path now writes storage/decision_log.py records; point it at a temp
    file so no test ever appends to the real agent_decisions.jsonl."""
    from storage import decision_log
    monkeypatch.setattr(decision_log, "_FILE", str(tmp_path / "agent_decisions.jsonl"))


@pytest.fixture(autouse=True)
def _no_live_briefing(monkeypatch):
    """The entry loop gathers a live per-candidate briefing (analysis/agent_context.gather: yfinance,
    Finnhub, the MCP). Tests must never hit the network — stub it; build()/render() are tested directly."""
    from analysis import agent_context
    monkeypatch.setattr(agent_context, "gather", lambda *a, **k: {"stub": True})


@pytest.fixture(autouse=True)
def _no_live_watch_scan(monkeypatch):
    """The entry loop scans today's watches/pins for fired triggers (live cache + quotes). Default to
    none in tests; tests of the watch path patch it explicitly."""
    from alerts import agentic_watch
    monkeypatch.setattr(agentic_watch, "watch_signals", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_live_holding_review(monkeypatch, tmp_path):
    """Exit passes run an event-driven holding review (analysis/holding_review.review: news, price
    history, earnings, the judge). Stub it; keep its state file off the real one. Tests of the review
    call holding_review._review_impl directly."""
    from analysis import holding_review
    monkeypatch.setattr(holding_review, "_FILE", str(tmp_path / "holding_review.json"))
    monkeypatch.setattr(holding_review, "_review_impl", holding_review.review, raising=False)
    monkeypatch.setattr(holding_review, "review", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_live_judge(monkeypatch):
    """The entry judge calls Claude — never from tests. Its pure parts are tested directly; the
    model call itself is replaced where a test needs a verdict."""
    from analysis import agent_judge
    monkeypatch.setattr(agent_judge, "_call_model", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("live Claude call attempted in a test")))

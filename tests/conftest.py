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

import pytest


@pytest.fixture(autouse=True)
def _isolated_decision_log(tmp_path, monkeypatch):
    """Every agent entry/exit path now writes storage/decision_log.py records; point it at a temp
    file so no test ever appends to the real agent_decisions.jsonl."""
    from storage import decision_log
    monkeypatch.setattr(decision_log, "_FILE", str(tmp_path / "agent_decisions.jsonl"))

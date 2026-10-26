import pytest

from app.common import trace as trace_module


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(trace_module._logger, "debug", lambda *args: calls.append(args))
    return calls


@pytest.mark.parametrize("environment", ["sandbox", "dev", "test", "prod", "LOCAL "])
def test_disabled_outside_local(monkeypatch, recorded, environment):
    monkeypatch.setattr(trace_module.settings, "environment", environment)
    monkeypatch.setattr(trace_module.settings, "debug_trace", True)
    assert not trace_module.enabled()
    trace_module.trace("event", secret_prompt="should not be logged")
    assert recorded == []


def test_disabled_locally_when_switched_off(monkeypatch, recorded):
    monkeypatch.setattr(trace_module.settings, "environment", "local")
    monkeypatch.setattr(trace_module.settings, "debug_trace", False)
    trace_module.trace("event", x=1)
    assert recorded == []


def test_enabled_locally(monkeypatch, recorded):
    monkeypatch.setattr(trace_module.settings, "environment", "local")
    monkeypatch.setattr(trace_module.settings, "debug_trace", True)
    trace_module.trace("query rewriter: output", rewritten_query="BCeID PROD approval")
    (fmt, event, body), = recorded
    assert event == "query rewriter: output"
    assert '"rewritten_query": "BCeID PROD approval"' in body


def test_preview_truncates_and_collapses_whitespace():
    assert trace_module.preview("a\n\n b") == "a b"
    assert trace_module.preview("x" * 10, limit=4) == "xxxx…"
    assert trace_module.preview(None) == ""

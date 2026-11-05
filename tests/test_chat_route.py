"""POST /chat input handling, with the chat service mocked (no AWS calls)."""

import pytest
from fastapi.testclient import TestClient

import main
from app.api.dependencies.authentication import require_service_account
from app.api.routes import chat as chat_route
from app.services.authentication import ServiceAccount
from app.services.chat_service import Answer


@pytest.fixture(autouse=True)
def authenticated():
    # These tests cover chat behaviour; authentication is tested in
    # tests/test_authentication.py.
    main.app.dependency_overrides[require_service_account] = lambda: ServiceAccount("test-client")
    yield
    main.app.dependency_overrides.pop(require_service_account, None)


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def stream(question, session_id, cancel_signal=None):
        calls.append(question)
        yield Answer("ok", None, [])

    monkeypatch.setattr(chat_route.chat_service, "stream", stream)
    test_client = TestClient(main.app)
    test_client.calls = calls
    return test_client


def _post(client, prompt):
    return client.post("/chat", json={"prompt": prompt})


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t \n"])
def test_blank_prompt_is_rejected_with_a_friendly_message(client, prompt):
    response = _post(client, prompt)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert client.calls == []


def test_very_long_prompt_is_rejected_with_a_friendly_message(client):
    response = _post(client, "x" * 100_000)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert client.calls == []


def test_normal_prompt_streams(client):
    response = _post(client, "how do I request an integration?")
    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    assert client.calls == ["how do I request an integration?"]


def test_config_reports_the_limit(client, monkeypatch):
    monkeypatch.setattr(chat_route.settings, "max_prompt_tokens", 150)
    assert client.get("/chat/config").json() == {"max_prompt_tokens": 150, "max_prompt_chars": 600}


def test_too_long_message_names_the_limit(client, monkeypatch):
    monkeypatch.setattr(chat_route.settings, "max_prompt_tokens", 200)
    response = _post(client, "word " * 200)
    assert response.status_code == 422
    assert "200 tokens" in response.json()["detail"]


def test_prompt_is_passed_on_stripped(client):
    _post(client, "  how do I request an integration?  \n")
    assert client.calls == ["how do I request an integration?"]


def _stream_failing_with(monkeypatch, error):
    async def stream(question, session_id, cancel_signal=None):
        yield chat_route.Status("searching")
        raise error

    monkeypatch.setattr(chat_route.chat_service, "stream", stream)
    return TestClient(main.app).post("/chat", json={"prompt": "q"})


def test_turn_timeout_is_reported_in_band(monkeypatch):
    response = _stream_failing_with(monkeypatch, chat_route.TurnTimeout("slow"))
    assert response.status_code == 200
    assert chat_route.TIMEOUT_MESSAGE in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_failure_is_reported_in_band(monkeypatch):
    response = _stream_failing_with(monkeypatch, RuntimeError("bedrock down"))
    assert chat_route.FAILED_MESSAGE in response.text
    assert chat_route.TIMEOUT_MESSAGE not in response.text

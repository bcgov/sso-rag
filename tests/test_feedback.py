"""Turn records, ratings and the rated-turn listing, against a moto DynamoDB
table (no AWS access)."""

import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import main
from app.api.dependencies.authentication import require_service_account
from app.api.routes import chat as chat_route
from app.common.config import settings
from app.jobs import feedback_report
from app.services import feedback_service, interaction_store
from app.services.authentication import ServiceAccount
from app.services.chat_service import Answer, Status

SESSION = "session_11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "session_22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def table(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setattr(settings, "analytics_enabled", True)
    monkeypatch.setattr(settings, "feedback_enabled", True)
    # Deployed behaviour: locally, turns are only stored in DynamoDB Local.
    monkeypatch.setattr(settings, "environment", "sandbox")
    monkeypatch.setattr(settings, "aws_endpoint_url_dynamodb", None)
    monkeypatch.setattr(settings, "session_cookie_secure", False)
    interaction_store.dynamodb_client.cache_clear()
    with mock_aws():
        interaction_store.ensure_table()
        yield
    interaction_store.dynamodb_client.cache_clear()


@pytest.fixture(autouse=True)
def authenticated():
    main.app.dependency_overrides[require_service_account] = lambda: ServiceAccount("test-client")
    yield
    main.app.dependency_overrides.pop(require_service_account, None)


def _answering(monkeypatch, answer=Answer("Open the CSS app.", "SUPPORTED", [
    {"title": "Integrations", "uri": "https://docs.example/integrations", "score": 0.81, "text": "..."}
])):
    async def stream(question, session_id, cancel_signal=None, report=None):
        report.add("rewrite", {"intent": "sso_question", "query": question, "latency_ms": 120, "usage": {}})
        yield Status("searching")
        yield answer

    monkeypatch.setattr(chat_route.chat_service, "stream", stream)


def _client(session=SESSION) -> TestClient:
    return TestClient(main.app, cookies={settings.session_cookie_name: session})


def _ask(client, prompt="how do I add a redirect URI?") -> tuple[str, str]:
    """Sends a question; returns the turn id and the raw stream."""
    response = client.post("/chat", json={"prompt": prompt})
    assert response.status_code == 200
    first = next(line for line in response.text.splitlines() if line.startswith("data: {"))
    return json.loads(first.removeprefix("data: "))["id"], response.text


def _rate(client, turn_id, rating):
    return client.post("/chat/feedback", json={"turn_id": turn_id, "rating": rating})


def _turn(turn_id, *attributes):
    return interaction_store.get_turn(turn_id, attributes or ("outcome",))


def _listed(rating, day=None):
    day = day or datetime.now(interaction_store.LOCAL_TZ).date()
    return [r.turn["turn_id"] for r in feedback_service.list_rated_turns([rating], day, day)]


# ── Turn records ──────────────────────────────────────────────────────────────

def test_turn_is_stored_before_the_stream_ends(monkeypatch):
    _answering(monkeypatch)
    turn_id, text = _ask(_client())

    assert '"finish_reason": "stop"' in text
    turn = _turn(turn_id, "question", "answer", "outcome", "grounding_status", "session_hash",
                 "citations", "rewrite", "latency_ms", "ttl", "created_at")
    assert turn["question"] == "how do I add a redirect URI?"
    assert turn["answer"] == "Open the CSS app."
    assert (turn["outcome"], turn["grounding_status"]) == ("ok", "SUPPORTED")
    assert turn["session_hash"] == interaction_store.hash_id(SESSION)
    assert turn["citations"] == [{"uri": "https://docs.example/integrations", "title": "Integrations", "score": 0.81}]
    assert turn["rewrite"]["intent"] == "sso_question"
    assert turn["latency_ms"]["rewrite"] == 120
    created = datetime.fromisoformat(turn["created_at"])
    assert turn["ttl"] == int(created.timestamp()) + 180 * 86400


@pytest.mark.parametrize("error,outcome", [
    (chat_route.TurnTimeout("slow"), "timeout"),
    (RuntimeError("bedrock down"), "failed"),
])
def test_timed_out_and_failed_turns_are_stored(monkeypatch, error, outcome):
    async def stream(question, session_id, cancel_signal=None, report=None):
        yield Status("searching")
        raise error

    monkeypatch.setattr(chat_route.chat_service, "stream", stream)
    turn_id, text = _ask(_client())

    assert '"error"' in text
    turn = _turn(turn_id, "outcome", "answer")
    assert turn["outcome"] == outcome
    assert "answer" not in turn


def test_stream_completes_when_the_store_fails(monkeypatch):
    _answering(monkeypatch)

    def broken(item):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(interaction_store, "record_turn", broken)
    _, text = _ask(_client())

    assert '"finish_reason": "stop"' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_nothing_is_stored_when_analytics_is_off(monkeypatch):
    monkeypatch.setattr(settings, "analytics_enabled", False)

    async def stream(question, session_id, cancel_signal=None, report=None):
        assert report is None
        yield Answer("ok", None, [])

    monkeypatch.setattr(chat_route.chat_service, "stream", stream)
    turn_id, _ = _ask(_client())

    assert _turn(turn_id) is None


def test_turn_date_is_the_bc_date():
    # 06:30 UTC on Jul 17 is 23:30 on Jul 16 in Vancouver (UTC-7). July, so
    # the offset is the same with or without BC's permanent daylight time.
    item = _record(datetime(2026, 7, 17, 6, 30, tzinfo=UTC))
    assert item["created_date"] == "2026-07-16"
    assert item["created_at"] == "2026-07-17T06:30:00.000Z"


@pytest.mark.parametrize("text,expected", [
    ("token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl here",
     "token [REDACTED] here"),
    ("Authorization: Bearer abcDEF123456.xyz", "Authorization: Bearer [REDACTED]"),
    ("client_secret=s3cr3t-Value&grant_type=x", "client_secret=[REDACTED]&grant_type=x"),
    ('{"client-secret": "abc123", "password": "hunter2"}',
     '{"client-secret": "[REDACTED]", "password": "[REDACTED]"}'),
    ("KEYCLOAK_CLIENT_SECRET=s3cr3tvalue", "KEYCLOAK_CLIENT_SECRET=[REDACTED]"),
    ('DB_PASSWORD=hunter2 and SSO_CLIENT_SECRET: "xyz"',
     'DB_PASSWORD=[REDACTED] and SSO_CLIENT_SECRET: "[REDACTED]"'),
    ('{"credentials": {"secret": "abcd-1234"}}', '{"credentials": {"secret": "[REDACTED]"}}'),
    ("the secret: rotate it in the CSS app", "the secret: rotate it in the CSS app"),
    ("passwordless login", "passwordless login"),
    ("my client id is sso-app-1234 and email a@b.ca", "my client id is sso-app-1234 and email a@b.ca"),
    ("set the `client_secret` in your app", "set the `client_secret` in your app"),
])
def test_credentials_are_masked(text, expected):
    assert interaction_store.mask_credentials(text) == expected


def test_stored_turn_masks_credentials_everywhere():
    item = _record(
        datetime(2026, 11, 16, 18, tzinfo=UTC),
        question="why does password=hunter2 fail?",
        events=[{"event": "rewrite", "query": "password=hunter2 login failure"}],
    )
    assert "hunter2" not in json.dumps(item)


def test_rejected_reply_is_kept_for_review():
    events = [
        {"event": "attempt", "attempt": "first", "raw_reply": "made-up answer", "reasons": ["uncited URL"],
         "replaced": True, "response_type": "answer"},
        {"event": "attempt", "attempt": "repair", "raw_reply": "good answer", "reasons": [],
         "replaced": False, "response_type": "answer"},
    ]
    item = _record(datetime(2026, 11, 16, 18, tzinfo=UTC), events=events)
    assert [a["raw_reply"] for a in item["attempts"]] == ["made-up answer", None]
    assert item["repaired"] is True


# ── Rating endpoint ───────────────────────────────────────────────────────────

def test_setting_changing_and_clearing_a_rating(monkeypatch):
    _answering(monkeypatch)
    client = _client()
    turn_id, _ = _ask(client)

    assert _rate(client, turn_id, "up").status_code == 204
    assert _listed("up") == [turn_id]

    assert _rate(client, turn_id, "down").status_code == 204
    assert _rate(client, turn_id, "down").status_code == 204
    assert (_listed("up"), _listed("down")) == ([], [turn_id])

    assert _rate(client, turn_id, None).status_code == 204
    assert (_listed("up"), _listed("down")) == ([], [])


def test_another_sessions_answer_cannot_be_rated(monkeypatch):
    _answering(monkeypatch)
    turn_id, _ = _ask(_client())

    assert _rate(_client(OTHER_SESSION), turn_id, "down").status_code == 404
    assert _rate(TestClient(main.app), turn_id, "down").status_code == 404
    assert _listed("down") == []


def test_unknown_turn_is_404():
    assert _rate(_client(), "chatcmpl-33333333-3333-4333-8333-333333333333", "up").status_code == 404


@pytest.mark.parametrize("body", [
    {"turn_id": "TURN#x", "rating": "up"},
    {"turn_id": "chatcmpl-33333333-3333-4333-8333-333333333333", "rating": "meh"},
    {"turn_id": "chatcmpl-33333333-3333-4333-8333-333333333333"},
    {"turn_id": "chatcmpl-33333333-3333-4333-8333-333333333333", "rating": "up", "comment": "x"},
])
def test_invalid_rating_requests_are_rejected(body):
    assert _client().post("/chat/feedback", json=body).status_code == 422


def test_rating_is_404_when_feedback_is_off(monkeypatch):
    _answering(monkeypatch)
    client = _client()
    turn_id, _ = _ask(client)
    monkeypatch.setattr(settings, "feedback_enabled", False)

    assert _rate(client, turn_id, "up").status_code == 404
    assert client.get("/chat/config").json()["feedback_enabled"] is False


def test_storage_failure_is_a_500(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(feedback_service, "set_rating", broken)
    response = _rate(_client(), "chatcmpl-33333333-3333-4333-8333-333333333333", "up")
    assert response.status_code == 500
    assert response.json()["detail"] == chat_route.FEEDBACK_FAILED


def test_delete_history_keeps_turns_and_ratings(monkeypatch):
    _answering(monkeypatch)

    async def delete_browser_history(session_id):
        return []

    monkeypatch.setattr(chat_route, "delete_browser_history", delete_browser_history)
    client = _client()
    turn_id, _ = _ask(client)
    _rate(client, turn_id, "down")

    assert client.delete("/chat/history").status_code == 204

    assert _turn(turn_id) is not None
    assert _listed("down") == [turn_id]


# ── Listing rated turns ───────────────────────────────────────────────────────

def _record(created_at, turn_id="chatcmpl-44444444-4444-4444-8444-444444444444", **fields):
    return interaction_store.build_turn_record(**{
        "turn_id": turn_id,
        "session_id": SESSION,
        "conversation_id": SESSION,
        "question": "q",
        "outcome": "ok",
        "events": [],
        "created_at": created_at,
        "latency_ms": 1000,
        "answer": "a",
        **fields,
    })


def _stored_rated_turn(n: int, created_at: datetime, rating: str) -> str:
    turn_id = f"chatcmpl-{n:08d}-5555-4555-8555-555555555555"
    interaction_store.record_turn(_record(created_at, turn_id, question=f"question {n}"))
    assert feedback_service.set_rating(turn_id, f"rater{n}", rating, "web")
    return turn_id


def test_a_range_across_a_month_boundary():
    october = _stored_rated_turn(1, datetime(2026, 10, 31, 20, tzinfo=UTC), "down")
    november = _stored_rated_turn(2, datetime(2026, 11, 1, 20, tzinfo=UTC), "down")
    _stored_rated_turn(3, datetime(2026, 11, 5, 20, tzinfo=UTC), "down")
    liked = _stored_rated_turn(4, datetime(2026, 11, 1, 21, tzinfo=UTC), "up")

    rated = feedback_service.list_rated_turns(["down"], date(2026, 10, 30), date(2026, 11, 2))
    assert [r.turn["turn_id"] for r in rated] == [october, november]
    assert rated[0].turn["question"] == "question 1"
    assert (rated[0].up, rated[0].down) == (0, 1)

    both = feedback_service.list_rated_turns(["up", "down"], date(2026, 11, 1), date(2026, 11, 1))
    assert [r.turn["turn_id"] for r in both] == [november, liked]


def test_a_turn_near_midnight_is_listed_under_its_bc_date():
    turn_id = _stored_rated_turn(5, datetime(2026, 7, 17, 6, 30, tzinfo=UTC), "down")

    assert _listed("down", date(2026, 7, 16)) == [turn_id]
    assert _listed("down", date(2026, 7, 17)) == []


def test_several_raters_of_one_turn_are_counted():
    turn_id = _stored_rated_turn(6, datetime(2026, 11, 16, 18, tzinfo=UTC), "down")
    feedback_service.set_rating(turn_id, "another-rater", "down", "teams")
    feedback_service.set_rating(turn_id, "third-rater", "up", "teams")

    [rated] = feedback_service.list_rated_turns(["up", "down"], date(2026, 11, 16), date(2026, 11, 16))
    assert (rated.up, rated.down) == (1, 2)


def test_report_prints_json_lines(capsys):
    turn_id = _stored_rated_turn(7, datetime(2026, 11, 16, 18, tzinfo=UTC), "down")

    feedback_report.main(["--rating", "down", "--date", "2026-11-16", "--json"])

    [line] = capsys.readouterr().out.strip().splitlines()[-1:]
    row = json.loads(line)
    assert (row["turn_id"], row["question"], row["down"]) == (turn_id, "question 7", 1)


@pytest.mark.parametrize("options,expected", [
    ({}, (date(2026, 11, 16), date(2026, 11, 16))),
    ({"on": date(2026, 11, 2)}, (date(2026, 11, 2), date(2026, 11, 2))),
    ({"days": 30}, (date(2026, 10, 18), date(2026, 11, 16))),
    ({"first": date(2026, 11, 1)}, (date(2026, 11, 1), date(2026, 11, 16))),
])
def test_report_date_range(options, expected):
    assert feedback_report.date_range(date(2026, 11, 16), **options) == expected

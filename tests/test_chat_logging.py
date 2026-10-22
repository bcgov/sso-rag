import json

from app.services.chat_service import replacement_log
from app.services.grounding import verify

RETRIEVED = [
    {"title": "DR", "uri": "https://docs/uptime#dr", "score": 0.701, "text": "Fails over to the DR cluster."},
    {"title": "Features", "uri": "https://docs/#features", "score": 0.4, "text": "Primary and secondary site."},
]


def test_logs_model_self_labelled_not_supported_with_retrieval_strength():
    reply = ('The docs do not cover this.\n<grounding>{"response_type": "answer", '
             '"grounding_status": "NOT_SUPPORTED", "citations": []}</grounding>')
    verdict = verify(reply, RETRIEVED, "does keycloak fail over?")
    log = replacement_log(verdict, reply, RETRIEVED, "end_turn")
    assert log["event"] == "answer_not_supported"
    assert log["reasons"] == []
    assert log["model_status"] == "NOT_SUPPORTED"
    assert log["top_score"] == 0.701
    assert log["sources"] == ["https://docs/uptime#dr", "https://docs/#features"]
    assert log["reply_preview"] == "The docs do not cover this."
    json.dumps(log)


def test_logs_grounding_rejection_reasons_and_stop_reason():
    reply = ('Yes, see https://made.up/page.\n<grounding>{"response_type": "answer", '
             '"grounding_status": "SUPPORTED", "citations": ["https://docs/uptime#dr"]}</grounding>')
    verdict = verify(reply, RETRIEVED, "q")
    log = replacement_log(verdict, reply, RETRIEVED, "limit_turns")
    assert log["event"] == "answer_rejected"
    assert any("undocumented URLs" in r for r in log["reasons"])
    assert log["model_status"] == "SUPPORTED"
    assert log["stop_reason"] == "limit_turns"


def test_logs_missing_grounding_block():
    verdict = verify("Yes it does.", RETRIEVED, "q")
    log = replacement_log(verdict, "Yes it does.", [], None)
    assert log["event"] == "answer_rejected"
    assert log["model_status"] is None and log["top_score"] is None

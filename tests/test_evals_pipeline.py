"""collect -> score -> report on a fake ChatService, without Bedrock."""

import asyncio
import json

import pytest

pytest.importorskip("deepeval")

from app.agents import rag_agent  # noqa: E402
from app.common.turn_report import record  # noqa: E402
from app.services import chat_service as chat_module  # noqa: E402
from app.services.chat_service import Answer, Status  # noqa: E402
from evals import collect, score  # noqa: E402
from evals.dataset import EvalCase  # noqa: E402

DOCS = "https://bcgov.github.io/sso-docs/"


class FakeChatService:
    def __init__(self):
        self.calls = []

    async def stream(self, question, session_id, cancel_signal=None):
        self.calls.append((question, session_id))
        record("rewrite", intent="sso_question", query=question, latency_ms=300,
               usage={"inputTokens": 2000, "outputTokens": 50})
        yield Status("searching")
        record("search", intent="sso_question", hits=[{"uri": DOCS + "FAQs/css-app#rotate"}])
        record("model_call", attempt="first", usage={"inputTokens": 4000, "outputTokens": 200})
        record("attempt", attempt="first", raw_reply="ok", response_type="answer",
               grounding_status="SUPPORTED", reasons=[], replaced=False,
               citations=[DOCS + "FAQs/css-app#rotate"],
               retrieved=[{"uri": DOCS + "FAQs/css-app#rotate", "title": "Rotate", "score": 0.9,
                           "text": "Regenerate the secret under Secrets."}])
        yield Answer("Regenerate it under Secrets.", "SUPPORTED",
                     [{"uri": DOCS + "FAQs/css-app#rotate", "title": "Rotate", "score": 0.9}])


@pytest.fixture
def fake_service(monkeypatch):
    service = FakeChatService()
    deleted = []

    async def delete_session(session_id):
        deleted.append(session_id)

    monkeypatch.setattr(chat_module, "chat_service", service)
    monkeypatch.setattr(rag_agent, "delete_session", delete_session)
    service.deleted = deleted
    return service


def test_run_case_uses_one_conversation_per_case(fake_service):
    case = EvalCase(id="rotate", category="multi_turn", turns=["I have a confidential client", "how do I rotate it?"],
                    expected_outcome="answer")
    result = asyncio.run(collect._run_case(case))

    sessions = {s for _, s in fake_service.calls}
    assert len(sessions) == 1 and fake_service.deleted == list(sessions)
    assert [q for q, _ in fake_service.calls] == case.turns
    assert result["error"] is None
    last = result["turns"][-1]
    assert last["outcome"] == "answer"
    assert last["citations"] == [DOCS + "FAQs/css-app#rotate"]
    assert last["retrieved"][0]["text"] == "Regenerate the secret under Secrets."
    assert last["tokens"]["agent_input"] == 4000


def test_score_and_report_without_a_judge(fake_service, tmp_path):
    case = EvalCase(id="rotate", category="sso_question", turns=["how do I rotate my secret?"],
                    expected_intent="sso_question", expected_outcome="answer",
                    expected_sources=[DOCS + "FAQs/css-app"], expected_output="Regenerate it under Secrets.")
    result = asyncio.run(collect._run_case(case))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"run_id": "run", "settings": {"number_of_reranked_results": 6}}))
    (run_dir / "results.jsonl").write_text(json.dumps(result) + "\n")

    report_path = score.main([str(run_dir), "--deterministic-only"])

    rows = [json.loads(line) for line in (run_dir / "scores.jsonl").read_text().splitlines()]
    assert {r["metric"] for r in rows} == {"intent_match", "outcome_match", "retrieval_hit@k",
                                           "retrieval_recall@k", "retrieval_mrr", "citation_precision",
                                           "first_pass_accepted"}
    assert all(r["success"] and r["error"] is None for r in rows)
    assert json.loads((run_dir / "scoring.json").read_text())["judge"] is None
    assert "judge `none (deterministic only)`" in report_path.read_text()

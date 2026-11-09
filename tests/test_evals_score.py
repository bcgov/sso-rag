"""Scoring and the judge. Needs the `eval` dependency group (DeepEval):
skipped without it (`uv sync --group eval`)."""

import asyncio

import pytest

pytest.importorskip("deepeval")

from pydantic import BaseModel  # noqa: E402

from evals.config import JudgeConfig  # noqa: E402
from evals.dataset import EvalCase  # noqa: E402
from evals.judge import BedrockJudge, JudgeError, parse_json_reply  # noqa: E402
from evals.score import CheckMetric, plan_metrics  # noqa: E402
from evals import metrics  # noqa: E402

DOCS = "https://bcgov.github.io/sso-docs/"


class Verdicts(BaseModel):
    verdict: str
    score: float


class FakeBedrock:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _response(*blocks, input_tokens=100, output_tokens=20, stop="end_turn"):
    return {"output": {"message": {"content": list(blocks)}}, "stopReason": stop,
            "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens}}


def _judge(*replies, **config) -> tuple[BedrockJudge, FakeBedrock]:
    client = FakeBedrock(replies)
    return BedrockJudge(JudgeConfig(**config), client=client), client


# --- judge ------------------------------------------------------------------

def test_parse_json_reply_accepts_fences_and_prose():
    assert parse_json_reply('```json\n{"verdict": "yes", "score": 1}\n```', Verdicts).score == 1
    assert parse_json_reply('Sure: {"verdict": "no", "score": 0.2} done', Verdicts).verdict == "no"
    with pytest.raises(ValueError):
        parse_json_reply("no json", Verdicts)
    with pytest.raises(ValueError):
        parse_json_reply('{"verdict": "x"}', Verdicts)


def test_judge_keeps_text_blocks_only_and_counts_tokens():
    judge, client = _judge(
        _response({"reasoningContent": {"reasoningText": {"text": "thinking..."}}},
                  {"text": '{"verdict": "yes", "score": 0.9}'}),
        reasoning_effort="low", temperature=0.0,
    )
    result = judge.generate("judge this", schema=Verdicts)

    assert result == Verdicts(verdict="yes", score=0.9)
    request = client.requests[0]
    assert request["modelId"] == "global.moonshotai.kimi-k3"
    assert request["inferenceConfig"] == {"maxTokens": 8192, "temperature": 0.0}
    assert request["additionalModelRequestFields"] == {"reasoning_effort": "low"}
    assert "system" in request  # JSON-only instruction
    assert judge.usage() == {"model": "global.moonshotai.kimi-k3", "calls": 1, "errors": 0,
                             "input_tokens": 100, "output_tokens": 20}


def test_judge_retries_once_on_invalid_json():
    judge, client = _judge(_response({"text": "I think yes"}),
                           _response({"text": '{"verdict": "yes", "score": 1}'}))
    assert judge.generate("p", schema=Verdicts).score == 1
    assert client.requests[1]["messages"][0]["content"][0]["text"].endswith("only the JSON object.")


def test_judge_plain_text_and_empty_replies():
    judge, client = _judge(_response({"text": "plain"}), _response(stop="max_tokens"),
                           reasoning_effort=None)
    assert judge.generate("p") == "plain"
    assert "system" not in client.requests[0]
    assert "additionalModelRequestFields" not in client.requests[0]
    assert "temperature" not in client.requests[0]["inferenceConfig"]
    with pytest.raises(JudgeError, match="max_tokens"):
        judge.generate("p")


def test_judge_counts_errors():
    judge, _ = _judge(RuntimeError("throttled"))
    with pytest.raises(RuntimeError):
        asyncio.run(judge.a_generate("p"))
    assert judge.usage()["errors"] == 1


def test_judge_config_from_env(monkeypatch):
    monkeypatch.setenv("EVAL_JUDGE_MODEL_ID", "global.example.judge")
    monkeypatch.setenv("EVAL_JUDGE_REASONING_EFFORT", "")
    monkeypatch.setenv("EVAL_JUDGE_TEMPERATURE", "0")
    monkeypatch.setenv("EVAL_JUDGE_MAX_TOKENS", "2048")
    config = JudgeConfig.from_env()
    assert (config.model_id, config.reasoning_effort, config.temperature, config.max_tokens) == (
        "global.example.judge", None, 0.0, 2048)


# --- metric selection ---------------------------------------------------------

def _turn(**fields) -> dict:
    turn = {"answer": "Regenerate it under Secrets.", "outcome": "answer", "intent": "sso_question",
            "searched": True, "retrieved": [{"uri": DOCS + "FAQs/css-app#rotate", "text": "chunk"}],
            "citations": [DOCS + "FAQs/css-app#rotate"], "attempts": [{"reasons": []}], "error": None}
    turn.update(fields)
    return turn


def _names(planned) -> list[str]:
    return [p.name for p in planned]


def test_without_a_judge_only_checks_run():
    case = EvalCase(id="c", category="sso_question", turns=["q"], expected_intent="sso_question",
                    expected_outcome="answer", expected_sources=[DOCS + "FAQs/css-app"], expected_output="ref")
    planned = plan_metrics(case, _turn(), k=6)
    assert _names(planned) == list(metrics.CHECKS)
    assert {p.kind for p in planned} == {"check"}


def test_answer_with_reference_gets_the_rag_metrics():
    case = EvalCase(id="c", category="sso_question", turns=["q"], expected_output="ref")
    planned = plan_metrics(case, _turn(), k=6, judge=_judge()[0])
    assert [p.name for p in planned if p.kind == "judge"] == [
        "faithfulness", "answer_relevancy", "contextual_precision", "contextual_recall", "correctness"]
    assert all(p.metric.model is not None for p in planned if p.kind == "judge")


def test_general_answer_is_faithful_only_in_its_bcgov_part():
    case = EvalCase(id="c", category="general_auth", turns=["what is PKCE?"], expected_intent="general_auth")
    answer = "PKCE binds the code to the client.\n\n**For BC Gov teams:** CSS public clients use PKCE."
    planned = plan_metrics(case, _turn(answer=answer, outcome="general"), k=6, judge=_judge()[0])
    judged = {p.name: p for p in planned if p.kind == "judge"}
    assert set(judged) == {"faithfulness", "answer_relevancy", "bcgov_bias"}
    assert judged["faithfulness"].test_case.actual_output.startswith("**For BC Gov teams:**")
    assert judged["answer_relevancy"].test_case.actual_output == answer

    no_part = plan_metrics(case, _turn(answer="PKCE binds the code.", outcome="general"), k=6, judge=_judge()[0])
    assert "faithfulness" not in _names(no_part)


def test_clarification_and_refusal_selection():
    case = EvalCase(id="c", category="clarification", turns=["login isn't working"],
                    expected_outcome="clarification")
    clarify = plan_metrics(case, _turn(answer="Which environment?", outcome="clarification"), k=6, judge=_judge()[0])
    assert [p.name for p in clarify if p.kind == "judge"] == ["clarification_quality"]

    refused = plan_metrics(case, _turn(answer="no docs", outcome="not_supported"), k=6, judge=_judge()[0])
    assert [p.name for p in refused if p.kind == "judge"] == []


def test_failed_turns_and_metric_filter():
    case = EvalCase(id="c", category="sso_question", turns=["q"], expected_outcome="answer")
    assert [p.kind for p in plan_metrics(case, _turn(error="boom"), k=6, judge=_judge()[0])] == ["check", "check"]
    only = plan_metrics(case, _turn(), k=6, judge=_judge()[0], only={"outcome_match"})
    assert _names(only) == ["outcome_match"]


def test_check_metric_measures_from_metadata():
    case = EvalCase(id="c", category="sso_question", turns=["q"], expected_outcome="answer")
    planned = plan_metrics(case, _turn(outcome="not_supported"), k=6, only={"outcome_match"})
    metric: CheckMetric = planned[0].metric
    assert metric.measure(planned[0].test_case) == 0.0
    assert metric.is_successful() is False
    assert "not_supported" in metric.reason
    assert metric.__name__ == "outcome_match"

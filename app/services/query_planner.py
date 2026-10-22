"""LLM calls that shape retrieval: the query plan, the gap check between
retrieval rounds, and (optionally) HyDE passages. Each returns its result and
the tokens it used, so callers can enforce a budget."""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from app.agents.planner_agent import create_gap_agent, create_hyde_agent, create_planner_agent
from app.retrieval.models import RetrievedChunk
from app.schemas.query_plan import GapAssessment, QueryPlan
from app.services.grounding import split_grounding

HISTORY_MESSAGE_CHARS = 600
EVIDENCE_CHARS = 500
EVIDENCE_HITS = 8

_THINKING_RE = re.compile(r"<thinking>.*?(?:</thinking>|\Z)", re.S | re.I)

# Runs planner-side model calls so the caller can stop waiting after a hard
# timeout. Sized for a few concurrent chats; extra calls queue.
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="planner")


def _invoke(agent, prompt: str, timeout_s: float, **kwargs):
    """Calls `agent` and waits at most `timeout_s`. On timeout the agent is
    signalled to stop and TimeoutError is raised (callers fall back)."""
    if timeout_s <= 0:
        raise TimeoutError("no time left in the retrieval budget")
    cancel = threading.Event()
    future = _executor.submit(agent, prompt, cancel_signal=cancel, **kwargs)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout:
        cancel.set()
        future.cancel()
        raise TimeoutError(f"model call exceeded {timeout_s:.1f}s") from None


@dataclass(frozen=True)
class Turn:
    role: str
    text: str


def _tokens(result) -> int:
    try:
        return int(result.metrics.accumulated_usage.get("totalTokens", 0))
    except AttributeError:
        return 0


def _text_blocks(message: dict) -> str:
    content = message.get("content") or []
    return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))


def conversation(messages: list[dict]) -> tuple[str | None, list[Turn]]:
    """(the user's latest message, earlier turns) from an agent's message list.

    Tool calls and results are skipped; assistant text has its <thinking>
    and grounding blocks removed.
    """

    turns: list[Turn] = []
    for m in messages or []:
        role = m.get("role")
        text = _text_blocks(m).strip()
        if role not in ("user", "assistant") or not text:
            continue
        if role == "assistant":
            text = split_grounding(_THINKING_RE.sub("", text))[0].strip()
            if not text:
                continue
        turns.append(Turn(role, text))

    last_user = next((i for i in range(len(turns) - 1, -1, -1) if turns[i].role == "user"), None)
    if last_user is None:
        return None, turns
    return turns[last_user].text, turns[:last_user]


def _format_history(history: list[Turn], limit: int) -> str:
    if not history or limit <= 0:
        return "(none)"
    lines = []
    for t in history[-limit:]:
        text = t.text if len(t.text) <= HISTORY_MESSAGE_CHARS else t.text[:HISTORY_MESSAGE_CHARS] + "…"
        lines.append(f"{t.role.upper()}: {text}")
    return "\n".join(lines)


def plan_query(
    question: str,
    history: list[Turn],
    draft_question: str | None = None,
    draft_queries: list[str] | None = None,
    history_turns: int = 6,
    timeout_s: float = 8.0,
) -> tuple[QueryPlan, int]:
    hints = ""
    if draft_question or draft_queries:
        hints = (
            "\nThe assistant's draft (may help, may be wrong):\n"
            f"question: {draft_question or ''}\n"
            f"queries: {json.dumps(draft_queries or [])}\n"
        )
    prompt = (
        f"Conversation so far:\n{_format_history(history, history_turns)}\n\n"
        f"Current user question:\n{question}\n"
        f"{hints}\n"
        "Create the retrieval plan."
    )
    result = _invoke(create_planner_agent(), prompt, timeout_s, structured_output_model=QueryPlan)
    plan = result.structured_output
    if not isinstance(plan, QueryPlan):
        raise ValueError("planner returned no structured output")
    return plan, _tokens(result)


def assess_gaps(
    plan: QueryPlan,
    hits: list[RetrievedChunk],
    searched: list[str],
    uncovered: list[str],
    timeout_s: float = 8.0,
) -> tuple[GapAssessment, int]:
    evidence = []
    for i, h in enumerate(hits[:EVIDENCE_HITS], 1):
        title = h.metadata.get("section") or h.metadata.get("page_title", "")
        evidence.append(f"[{i}] {title}\n{h.text[:EVIDENCE_CHARS]}")

    facets = "\n".join(f"- {s.facet}: {s.query}" for s in plan.sub_questions) or "(single topic)"
    prompt = (
        f"Question: {plan.original_question or plan.canonical_query}\n"
        f"Standalone form: {plan.canonical_query}\n"
        f"Parts:\n{facets}\n"
        f"Parts with no relevant results: {json.dumps(uncovered)}\n"
        f"Already searched: {json.dumps(searched)}\n\n"
        "Excerpts:\n" + ("\n\n".join(evidence) or "(none)")
    )
    result = _invoke(create_gap_agent(), prompt, timeout_s, structured_output_model=GapAssessment)
    gaps = result.structured_output
    if not isinstance(gaps, GapAssessment):
        raise ValueError("gap check returned no structured output")
    return gaps, _tokens(result)


def hyde_passage(question: str, timeout_s: float = 8.0) -> tuple[str, int]:
    result = _invoke(create_hyde_agent(), f"Question: {question}", timeout_s)
    return _THINKING_RE.sub("", str(result)).strip(), _tokens(result)

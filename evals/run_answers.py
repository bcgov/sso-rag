"""Run the full agent per strategy and measure answer faithfulness.

    uv run python evals/run_answers.py --strategies baseline planner_iterative

Per question:
- faithfulness: an LLM judge splits the answer into factual claims and marks
  each as supported or not by the retrieved text; score = supported / claims.
  Measured on the model's raw answer, before the grounding checks.
- grounding: whether app/services/grounding.verify delivered or replaced it.
- cited recall: share of expected pages among the delivered citations.

Needs AWS credentials and the usual .env.
"""

import argparse
import json
import time
from collections import defaultdict

from common import (
    RESULTS,
    STRATEGIES,
    load_golden,
    markdown_table,
    mean,
    messages_for,
    ranked_pages,
    recall_at_k,
)
from pydantic import BaseModel, Field
from strands import Agent

from app.agents.bedrock_model import agent_retry_strategy, model, planner_model
from app.common.config import settings
from app.prompts.system_prompt import SYSTEM_PROMPT
from app.services.chat_service import ThinkingFilter
from app.services.grounding import split_grounding, verify
from app.tools.knowledge_base_tool import CITATIONS_KEY, search_knowledge_base

JUDGE_PROMPT = """
You grade a support answer for faithfulness to retrieved documentation.
Split the answer into atomic factual claims (ignore greetings, offers to
help, and statements that the documentation doesn't cover something). For
each claim decide whether the documentation excerpts explicitly support it.
Do not use outside knowledge.
"""


class Claim(BaseModel):
    claim: str
    supported: bool


class Judgement(BaseModel):
    claims: list[Claim] = Field(default_factory=list)


def answer(row: dict) -> tuple[str, list[dict]]:
    retrieved: list[dict] = []
    agent = Agent(
        model=model,
        tools=[search_knowledge_base],
        system_prompt=SYSTEM_PROMPT,
        messages=messages_for(row, include_question=False),
        callback_handler=None,
        retry_strategy=agent_retry_strategy,
    )
    result = agent(row["question"], invocation_state={CITATIONS_KEY: retrieved},
                   limits={"turns": settings.agent_max_turns})
    f = ThinkingFilter()
    return f.feed(str(result)) + f.flush(), retrieved


def judge(body: str, retrieved: list[dict]) -> float | None:
    if not body.strip():
        return None
    excerpts = "\n\n".join(f"[{i}] {r.get('text', '')}" for i, r in enumerate(retrieved, 1)) or "(none)"
    result = Agent(model=planner_model, system_prompt=JUDGE_PROMPT, callback_handler=None)(
        f"Documentation excerpts:\n{excerpts}\n\nAnswer:\n{body}",
        structured_output_model=Judgement,
    )
    claims = result.structured_output.claims
    return sum(c.supported for c in claims) / len(claims) if claims else None


def evaluate(row: dict) -> dict:
    started = time.monotonic()
    reply, retrieved = answer(row)
    ms = int((time.monotonic() - started) * 1000)
    body, _ = split_grounding(reply)
    verdict = verify(reply, retrieved, row["question"])
    cited = ranked_pages([c.get("uri") for c in verdict.citations])
    return {
        "id": row["id"],
        "tags": row.get("tags", []),
        "faithfulness": judge(body, retrieved),
        "status": verdict.status,
        "rejected": verdict.rejected,
        "replaced": verdict.replaced,
        "cited_recall": recall_at_k(cited, row["expected_urls"], len(cited) or 1),
        "ms": ms,
        "reasons": verdict.reasons,
        "answer": body,
    }


def summarize(name: str, rows: list[dict]) -> list:
    faith = [r["faithfulness"] for r in rows if r["faithfulness"] is not None]
    return [
        name, len(rows),
        mean(faith),
        mean([float(r["rejected"]) for r in rows]),
        mean([float(not r["replaced"]) for r in rows]),
        mean([r["cited_recall"] for r in rows]),
        round(mean([r["ms"] for r in rows])),
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    ap.add_argument("--tags", nargs="*")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    rows = load_golden(tags=args.tags, limit=args.limit)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    headers = ["strategy", "n", "faithfulness", "rejected", "answered", "cited recall", "mean ms"]
    overall, by_tag = [], defaultdict(list)

    for name in args.strategies:
        settings.query_strategy, settings.hyde_enabled = STRATEGIES[name]
        results = []
        for row in rows:
            try:
                results.append(evaluate(row))
            except Exception as e:
                print(f"{name} {row['id']}: {type(e).__name__}: {e}")
        (RESULTS / f"answers-{stamp}-{name}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in results) + "\n", encoding="utf-8")
        overall.append(summarize(name, results))
        for tag in sorted({t for r in results for t in r["tags"]}):
            by_tag[tag].append(summarize(name, [r for r in results if tag in r["tags"]]))

    report = ["# Answer evaluation", "", markdown_table(headers, overall), ""]
    for tag, table in by_tag.items():
        report += [f"## {tag}", "", markdown_table(headers, table), ""]
    text = "\n".join(report)
    (RESULTS / f"answers-{stamp}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

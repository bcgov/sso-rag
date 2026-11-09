"""Step 1: run every case through the real pipeline and save what happened.

    uv run --group eval python -m evals.collect [--label main] [--ids a,b]
        [--categories c1,c2] [--exclude-drafts] [--limit N] [--concurrency 2]

Each case runs in a fresh conversation through ChatService.stream, the same
entry point /chat uses: query rewriter, search, rerank, the agent, the
grounding check and its repair attempt all run as in production. A
TurnReport (app/common/turn_report.py) captures the intermediate values.

Conversations are stored in a temporary directory (ENVIRONMENT=local,
AGENT_STORE_DIR=<tmp>) that is removed afterwards; the run refuses to start
if the app's storage is anything else, so it never touches the S3 store.

Writes evals/runs/<run id>/run.json (settings, models, git state) and
results.jsonl (one line per case). Score them with `python -m evals.score`.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from evals.config import DEFAULT_DATASET, REPO_DIR, RUNS_DIR
from evals.dataset import EvalCase, load_cases, select

NOT_SUPPORTED = "NOT_SUPPORTED"
GENERAL_STATUS = "GENERAL"


def _usage_sum(events: list[dict], key: str) -> int:
    return sum((e.get("usage") or {}).get(key) or 0 for e in events)


def outcome_of(grounding_status: str | None, attempts: list[dict], link_only: bool) -> str:
    """What the user got: not_supported (the no-documentation reply), answer,
    general, clarification or conversational."""
    if grounding_status == NOT_SUPPORTED:
        return "not_supported"
    if grounding_status == GENERAL_STATUS:
        return "general"
    if link_only or not attempts:
        return "clarification"
    return attempts[-1].get("response_type") or "answer"


def summarize_turn(
    question: str,
    answer: dict | None,
    events: list[dict],
    latency_ms: int,
    error: str | None = None,
) -> dict:
    """One turn's result from the delivered answer and the TurnReport events."""

    def of(name: str) -> list[dict]:
        return [e for e in events if e.get("event") == name]

    rewrites = of("rewrite")
    rewrite = rewrites[-1] if rewrites else None
    searches = of("search")
    attempts = [
        {k: v for k, v in e.items() if k not in ("event", "retrieved")}
        for e in of("attempt")
    ]
    first_attempt = next(iter(of("attempt")), None)
    model_calls = of("model_call")
    rewriter_calls = [*rewrites, *of("rewrite_retry")]
    status = answer.get("grounding_status") if answer else None

    return {
        "question": question,
        "answer": answer.get("text") if answer else None,
        "grounding_status": status,
        "citations": answer.get("citations", []) if answer else [],
        "outcome": outcome_of(status, attempts, bool(of("link_only"))) if answer else None,
        "intent": rewrite.get("intent") if rewrite else None,
        "rewrite": {k: v for k, v in rewrite.items() if k not in ("event", "usage")} if rewrite else None,
        "searched": bool(searches),
        "search": {k: v for k, v in searches[-1].items() if k != "event"} if searches else None,
        "search_skipped": next((e.get("reason") for e in of("search_skipped")), None),
        # The evidence the grounding check used, best first, with full text.
        "retrieved": (first_attempt or {}).get("retrieved") or [],
        "attempts": attempts,
        "repair_attempted": len(attempts) > 1,
        "tokens": {
            "agent_calls": len(model_calls),
            "agent_input": _usage_sum(model_calls, "inputTokens"),
            "agent_output": _usage_sum(model_calls, "outputTokens"),
            "agent_cache_read": _usage_sum(model_calls, "cacheReadInputTokens"),
            "agent_cache_write": _usage_sum(model_calls, "cacheWriteInputTokens"),
            "rewriter_calls": len([e for e in rewriter_calls if e.get("usage")]),
            "rewriter_input": _usage_sum(rewriter_calls, "inputTokens"),
            "rewriter_output": _usage_sum(rewriter_calls, "outputTokens"),
            "rewriter_cache_read": _usage_sum(rewriter_calls, "cacheReadInputTokens"),
        },
        "latency_ms": latency_ms,
        "rewrite_latency_ms": rewrite.get("latency_ms") if rewrite else None,
        "error": error,
    }


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _prepare_environment(store_dir: str) -> None:
    """Must run before any `app` import: the app reads settings and picks its
    storage at import time."""
    loaded = [m for m in sys.modules if m == "app" or m.startswith("app.")]
    if loaded:
        raise RuntimeError(f"app modules imported before the eval environment was set: {loaded[:3]}")
    os.environ["ENVIRONMENT"] = "local"
    os.environ["DEBUG_TRACE"] = "false"
    os.environ["AGENT_STORE_DIR"] = store_dir


def _check_isolated_storage(store_dir: str) -> None:
    from strands.storage import LocalFileStorage

    from app.agents import rag_agent

    if not isinstance(rag_agent.storage, LocalFileStorage) or rag_agent.LOCAL_STORAGE_DIR != store_dir:
        raise RuntimeError("refusing to run: agent storage is not the temporary eval directory")


def _run_settings() -> dict:
    from app.common.config import settings

    fields = [
        "aws_region", "knowledge_base_id", "model_arn", "model_max_tokens", "model_prompt_caching",
        "query_rewriter_model_id", "query_rewriter_prompt_caching", "query_rewriter_timeout_seconds",
        "query_rewriter_history_turns", "query_rewriter_step_back_enabled",
        "query_rewriter_decomposition_enabled", "query_rewriter_retry_enabled",
        "reranker_model_arn", "number_of_results", "number_of_reranked_results",
        "min_rerank_score", "search_type", "grounding_repair_enabled",
        "conversation_window_messages", "turn_timeout_seconds",
    ]
    return {f: getattr(settings, f) for f in fields}


async def _run_turn(chat_service, text: str, session_id: str) -> dict:
    from app.common.turn_report import capture
    from app.services.chat_service import Answer

    answer = None
    error = None
    started = time.monotonic()
    with capture() as report:
        try:
            async for item in chat_service.stream(text, session_id):
                if isinstance(item, Answer):
                    answer = {
                        "text": item.text,
                        "grounding_status": item.grounding_status,
                        "citations": [c.get("uri") for c in item.citations],
                    }
        except Exception as e:  # recorded per case; the run goes on
            error = f"{type(e).__name__}: {e}"
    latency_ms = int((time.monotonic() - started) * 1000)
    return summarize_turn(text, answer, list(report.events), latency_ms, error)


async def _run_case(case: EvalCase) -> dict:
    from app.agents.rag_agent import delete_session, new_session_id
    from app.services.chat_service import chat_service

    session_id = new_session_id()
    turns: list[dict] = []
    try:
        for text in case.turns:
            turn = await _run_turn(chat_service, text, session_id)
            turns.append(turn)
            if turn["error"]:
                break
    finally:
        try:
            await delete_session(session_id)
        except Exception:
            pass
    error = next((t["error"] for t in turns if t["error"]), None)
    return {"id": case.id, "case": case.model_dump(), "turns": turns, "error": error}


async def _run_all(cases: list[EvalCase], concurrency: int, out: Path) -> int:
    semaphore = asyncio.Semaphore(concurrency)
    done = 0
    failures = 0
    lock = asyncio.Lock()

    async def one(case: EvalCase) -> None:
        nonlocal done, failures
        async with semaphore:
            result = await _run_case(case)
        async with lock:
            done += 1
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
            last = result["turns"][-1] if result["turns"] else {}
            failures += bool(result["error"])
            status = result["error"] or f"{last.get('outcome')} {last.get('grounding_status') or ''}".strip()
            print(f"[{done}/{len(cases)}] {case.id}: {status} ({last.get('latency_ms', 0) / 1000:.1f}s)",
                  flush=True)

    await asyncio.gather(*(one(c) for c in cases))
    return failures


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(prog="python -m evals.collect", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--ids", help="comma-separated case ids")
    parser.add_argument("--categories", help="comma-separated categories")
    parser.add_argument("--exclude-drafts", action="store_true", help="skip cases not yet reviewed")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=2,
                        help="cases run at once (each makes several Bedrock calls)")
    parser.add_argument("--label", default="", help="added to the run id, e.g. main or my-branch")
    parser.add_argument("--out-dir", type=Path, default=RUNS_DIR)
    args = parser.parse_args(argv)

    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else None  # noqa: E731
    cases = select(load_cases(args.dataset), split(args.ids), split(args.categories),
                   include_drafts=not args.exclude_drafts, limit=args.limit)
    if not cases:
        parser.error("no cases selected")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    store_dir = tempfile.mkdtemp(prefix="sso-rag-eval-")
    _prepare_environment(store_dir)
    try:
        _check_isolated_storage(store_dir)
        sha = _git("rev-parse", "--short", "HEAD") or "nogit"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_id = "-".join(p for p in (stamp, sha, args.label) if p)
        run_dir = args.out_dir / run_id
        run_dir.mkdir(parents=True)

        meta = {
            "run_id": run_id,
            "label": args.label,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "git": {"commit": _git("rev-parse", "HEAD"), "branch": _git("branch", "--show-current"),
                    "dirty": bool(_git("status", "--porcelain"))},
            "dataset": {"path": str(args.dataset),
                        "sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
                        "cases": len(cases), "drafts": sum(c.draft for c in cases)},
            "settings": _run_settings(),
            "concurrency": args.concurrency,
        }
        print(f"run {run_id}: {len(cases)} cases -> {run_dir}", flush=True)
        failures = asyncio.run(_run_all(cases, args.concurrency, run_dir / "results.jsonl"))
        meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        meta["failed_cases"] = failures
        (run_dir / "run.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        print(f"done: {len(cases) - failures} ok, {failures} failed. "
              f"Next: uv run --group eval python -m evals.score {run_dir}")
        return run_dir
    finally:
        shutil.rmtree(store_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

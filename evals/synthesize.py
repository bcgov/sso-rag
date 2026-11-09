"""Draft test cases from the documentation chunks with DeepEval's
Synthesizer, for a person to review.

    uv run --group eval python -m evals.synthesize [--limit 30] [--per-page 1]
        [--pages css-application,FAQs/keycloak] [--out evals/datasets/drafts.jsonl]

Reads chunker/chunks/*.md and their .metadata.json, picks up to --per-page
chunks from each guide and FAQ page (release notes and category pages are
skipped), and asks the judge model (evals/judge.py) to write one realistic
question and a reference answer per chunk. Each draft expects an "answer"
outcome with the chunk's page as the expected source.

Every draft has "draft": true. Review it (fix the question, the reference
answer and the expected sources), set "draft": false and move it into
evals/datasets/goldens.jsonl.
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from evals.config import CHUNKS_DIR, DEFAULT_DATASET, DEFAULT_DRAFTS, JudgeConfig
from evals.dataset import EvalCase, load_cases, write_cases
from evals.judge import BedrockJudge

SKIPPED_DOC_TYPES = {"release", "category"}

SCENARIO = (
    "A developer or product owner on a BC Government technical team is integrating, or "
    "already runs, an application that uses the BC Gov SSO service: the Common Hosted Single "
    "Sign-On (CSS) app and its Keycloak Gold identity providers (IDIR, BCeID, BC Services Card)."
)
TASK = "Ask the SSO assistant a question in their own words about the topic of the context."
INPUT_FORMAT = (
    "One short question as typed into a chat box (under 30 words). Use everyday wording, not "
    "the documentation's headings, and don't quote the context."
)
OUTPUT_FORMAT = (
    "A concise answer (at most about 120 words) that uses only facts stated in the context, "
    "keeping names, values and URLs exactly as written there."
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def load_chunks(chunks_dir: Path = CHUNKS_DIR) -> list[dict]:
    chunks = []
    for meta_path in sorted(chunks_dir.glob("*.md.metadata.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))["metadataAttributes"]
        text_path = meta_path.with_name(meta_path.name.removesuffix(".metadata.json"))
        if text_path.exists():
            chunks.append({**meta, "path": str(text_path), "text": text_path.read_text(encoding="utf-8")})
    return chunks


def pick_chunks(chunks: list[dict], per_page: int, limit: int, pages: list[str] | None = None) -> list[dict]:
    """Up to `per_page` chunks per page, spread across pages, at most `limit`."""
    by_page: dict[str, list[dict]] = defaultdict(list)
    for c in chunks:
        if c.get("doc_type") in SKIPPED_DOC_TYPES:
            continue
        if pages and not any(p in c["page_url"] for p in pages):
            continue
        if len(by_page[c["page_url"]]) < per_page:
            by_page[c["page_url"]].append(c)
    # Round-robin over pages, so a small limit still covers many pages.
    picked, rounds = [], max((len(v) for v in by_page.values()), default=0)
    for i in range(rounds):
        picked += [v[i] for v in by_page.values() if i < len(v)]
    return picked[:limit]


def main(argv: list[str] | None = None) -> Path:
    from deepeval.synthesizer import Evolution, Synthesizer
    from deepeval.synthesizer.config import EvolutionConfig, FiltrationConfig, StylingConfig

    parser = argparse.ArgumentParser(prog="python -m evals.synthesize", description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=30, help="chunks to draft from")
    parser.add_argument("--per-page", type=int, default=1)
    parser.add_argument("--pages", help="comma-separated substrings of page URLs to draft from")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, default=DEFAULT_DRAFTS)
    args = parser.parse_args(argv)

    pages = [p.strip() for p in args.pages.split(",")] if args.pages else None
    chunks = pick_chunks(load_chunks(), args.per_page, args.limit, pages)
    if not chunks:
        parser.error("no chunks selected")

    judge = BedrockJudge(JudgeConfig.from_env())
    synthesizer = Synthesizer(
        model=judge,
        max_concurrent=args.concurrency,
        filtration_config=FiltrationConfig(critic_model=judge),
        # One light rewording step, so questions don't mirror the headings.
        evolution_config=EvolutionConfig(
            num_evolutions=1,
            evolutions={Evolution.CONCRETIZING: 0.5, Evolution.IN_BREADTH: 0.5},
        ),
        styling_config=StylingConfig(scenario=SCENARIO, task=TASK, input_format=INPUT_FORMAT,
                                     expected_output_format=OUTPUT_FORMAT),
    )
    print(f"drafting from {len(chunks)} chunks with {judge.get_model_name()}", flush=True)
    goldens = synthesizer.generate_goldens_from_contexts(
        contexts=[[c["text"]] for c in chunks],
        source_files=[c["path"] for c in chunks],
        include_expected_output=True,
        max_goldens_per_context=1,
    )

    by_path = {c["path"]: c for c in chunks}
    existing = {c.id for c in load_cases(DEFAULT_DATASET)} if DEFAULT_DATASET.exists() else set()
    drafts: list[EvalCase] = []
    for golden in goldens:
        chunk = by_path.get(golden.source_file or "")
        if chunk is None or not golden.input:
            continue
        base = f"draft-{_slug(chunk['page_url'].split('/sso-docs/')[-1] or 'index')}-{chunk['chunk_index']}"
        case_id, n = base, 1
        while case_id in existing:
            n += 1
            case_id = f"{base}-{n}"
        existing.add(case_id)
        drafts.append(EvalCase(
            id=case_id,
            category="sso_question",
            turns=[golden.input.strip()],
            expected_intent="sso_question",
            expected_outcome=["answer"],
            expected_sources=[chunk["page_url"]],
            expected_output=(golden.expected_output or "").strip() or None,
            notes=f"Synthesized from {chunk['source_url']} ({chunk.get('section', '')}). Review before use.",
            draft=True,
        ))

    write_cases(args.out, drafts)
    print(f"{len(drafts)} drafts -> {args.out}. Judge usage: {judge.usage()}")
    return args.out


if __name__ == "__main__":
    main()

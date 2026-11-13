# Plan: HTML evaluation report

**Status:** proposed, awaiting review. Not implemented.

## Problem and goal

Each scored run produces [`report.md`](../../evals/README.md#run-output): tables of metric pass rates, categories, outcomes, cost, latency and failures. It's good for a terminal or a PR comment, but it's hard to see patterns in it. You can't easily tell which categories fail which metrics, whether a judged average hides a split between 0s and 1s, or where retrieval misses. Reading a failing case also means searching `results.jsonl` and `scores.jsonl` by hand.

The goal is a `report.html` for each run. It has charts of the same metrics and a case explorer for reading each case's details, and it's generated next to `report.md` from the run files that already exist.

## Confirmed decisions

- **Single-run report only.** No comparison page and no trend page across runs. `report.py --baseline` keeps producing the markdown comparison as today.
- **Charts are inline SVG generated in Python.** No JavaScript chart library and no CDN.
- **`report.md` stays.** Both reports are written from the same summary.
- **Local only.** The report stays on the developer's machine under the git-ignored `evals/runs/`, like the rest of the run output. It is never published or uploaded, and it loads nothing from outside the file.

## Out of scope

- Comparing runs, or trends across runs.
- Interactive charts (hover tooltips, zoom).
- Any hosting, sharing or upload of the report.
- Changes to collecting, scoring or the metrics themselves.

## Design

### Output

One self-contained file: `evals/runs/<run id>/report.html`. The CSS, the SVG charts, the case data and a small script are all inline, so the file opens offline from disk.

| How it's produced | When |
|---|---|
| `python -m evals.score <run dir>` | Automatically, after `report.md` (via `write_report()`) |
| `python -m evals.report <run dir>` | Regenerates `report.md` and `report.html` from an already scored run, without re-scoring |

### New and changed files

| File | Change |
|---|---|
| `evals/html_report.py` (new) | Builds the data for the page and renders the template |
| `evals/svg.py` (new) | Chart helpers that return SVG strings |
| `evals/templates/report.html.j2` (new) | The page: layout, inline CSS, the case explorer script |
| [evals/report.py](../../evals/report.py) | `write_report()` also writes `report.html`; `main()` without `--baseline` regenerates both reports |
| [pyproject.toml](../../pyproject.toml) | Add `jinja2` to the `eval` dependency group. It is already installed as a DeepEval dependency; this declares it explicitly |
| [evals/README.md](../../evals/README.md), [docs/components/evaluations.md](../components/evaluations.md) | Document `report.html` |

### Building the page data

`html_report.build(meta, scoring, results, scores)` returns a plain dict for the template. It reuses `report.summarize()` for the totals and adds:

| Data | Computed from |
|---|---|
| Per-case rows | Each result's case, its last turn, and that case's rows from `scores.jsonl` |
| Category × metric pass rate | `scores.jsonl` rows grouped by category and metric, every metric (not only the subset `summarize()` reports per category). Empty when the metric didn't apply |
| Hit rate at each rank 1…k | For cases with `expected_sources` and a search: whether an expected page is within the top r reranked pages, for r = 1…k, using `metrics.ranked_pages()` and `page_key()` |
| Judged score spread | The `score` of every judged row, per metric |
| Grounding funnel | Last turns with a checked reply: accepted first time, repair attempted, repaired, replaced by the no-documentation reply |
| Latency by category | Last-turn `latency_ms` per category |

Retrieval and citation matching reuse `evals/metrics.py`, so the chart and the `retrieval_hit@k` metric can't disagree.

### Page layout

Sections, top to bottom:

1. **Header.** Run id, git commit and branch (with "uncommitted changes" when dirty), agent model, rewriter model, judge model, dataset hash and case count. A warning banner when draft cases are included.
2. **Headline tiles.** Cases run (and errors), outcome match, mean faithfulness, retrieval hit@k, first-reply acceptance, agent tokens per case, p50 / p95 turn latency, judge tokens and judge errors. A tile shows "n/a" when its metric didn't run (for example after `--deterministic-only`).
3. **Metric pass rates.** Horizontal bars, one per metric, with the threshold marked and `n` and the mean beside each bar. Checks and judged metrics are in separate groups. Metrics with errors show the error count.
4. **By category.** Heatmap of category (rows) × metric (columns). Cells show the pass rate as text and colour, and are empty when the metric didn't apply.
5. **Outcomes.** Expected × actual outcome matrix. The diagonal (expected outcome met) is highlighted, and each cell shows the number of cases.
6. **Judged score spread.** A dot plot per judged metric, one dot per case on a 0–1 axis, with the threshold line. It shows whether a mean comes from consistent scores or from a mix of passes and failures.
7. **Retrieval.** Hit rate at each rank 1…k as bars, with the configured k marked.
8. **Grounding.** A funnel: replies checked → accepted first time → repair attempted → repaired → replaced.
9. **Cost and latency.**
   - Stacked bars of mean tokens per case: agent input, agent cache read, agent output, rewriter input, rewriter output.
   - A dot plot of turn latency by category, with p50 and p95 marked.
   - Judge tokens appear as a run total in the tiles; they aren't recorded per case.
10. **Case explorer.** A table of all cases: id, category, expected → actual outcome, grounding status, failed metrics, latency.
    - Filters: category, metric, "failing only", and a text search on id and question.
    - Expanding a row shows:
      - each turn's question and the delivered answer as plain text;
      - citations and expected sources, with matches marked;
      - the reference answer, key facts and must-nots;
      - the retrieved chunks (title, score, text, collapsed by default);
      - each grounding attempt with its status and rejection reasons;
      - every metric's score, pass or fail, and the judge's reason or the error.
11. **Footer.** Scoring settings (k, threshold, judge configuration) and paths to the run files.

### Charts

`evals/svg.py` has five helpers, each returning an SVG string:

| Helper | Used for |
|---|---|
| `bar_chart` | Metric pass rates, hit rate at each rank |
| `stacked_bars` | Tokens per case |
| `heatmap` | Category × metric, outcome matrix |
| `dot_plot` | Judged score spread, latency by category |
| `funnel` | Grounding |

Rules for every chart:

- Every label is escaped. Each chart has a `<title>` and a short text summary for screen readers.
- Charts scale to the container width using `viewBox`.
- Colours come from CSS variables, so the charts follow the light or dark theme.
- Colour is never the only signal: heatmap cells and bars also show their value as text.

### Styling

- Inline CSS. Colours are defined as variables on `:root`, with a dark theme under `prefers-color-scheme: dark`.
- System font stack, with no web fonts.
- Readable at phone width: tables scroll inside their own box, and the page itself never scrolls sideways.
- Printing to PDF from the browser gives a usable result: the case explorer prints expanded rows only.

### Case explorer script

- About 100 lines of plain JavaScript in the page, with no libraries.
- The case data is embedded once as JSON in `<script type="application/json">`, and the script builds the expanded rows from it with `textContent`.
- Without JavaScript the page still shows every chart and the case table. Only filtering and expanding need the script.

### Safety

Answers, questions and documentation text are model and user content, and the dataset includes a prompt-injection case.

- Jinja2 autoescaping is on for the whole template, and the SVG helpers escape their labels.
- The embedded JSON escapes `<`, `>` and `&` as `<`, `>` and `&`, so a value can't close the script tag.
- The script inserts case content with `textContent` only, never `innerHTML`.
- No external URLs are loaded. Citation and source URLs are shown as text and, as links, only when they start with `https://`, with `rel="noopener noreferrer"`.

### Size

Full chunk text for the current 40 cases comes to a few hundred KB, which is fine. If datasets grow a lot, a later `--no-chunks` option can leave the chunk text out.

## Testing

All tests run without Bedrock. Those that need Jinja2 skip unless the `eval` group is installed, like the existing DeepEval tests.

| Test | Checks |
|---|---|
| `tests/test_evals_html_report.py` | `build()` on a made-up run: per-case rows, category × metric rates, hit rate at each rank, funnel counts. A tile shows "n/a" when a metric didn't run |
| | Escaping: an answer containing `<script>` and `</script>` appears only escaped in the HTML and in the embedded JSON |
| | `write_report()` writes both `report.md` and `report.html` |
| `tests/test_evals_svg.py` | Each helper returns well-formed SVG (it parses as XML), escapes labels and handles empty input |
| [tests/test_evals_pipeline.py](../../tests/test_evals_pipeline.py) | Extend the existing collect → score test to assert that `report.html` exists |

Manual check: open a generated report in a browser in light and dark mode and at phone width. Do this again after the first real run against Bedrock, since the layout is otherwise only tried on made-up numbers.

## Estimate

| Task | Time |
|---|---|
| Page data (`build()`, hit rate at each rank, funnel, spread) | 0.5 day |
| SVG chart helpers | 0.5 day |
| Template, styling and case explorer | 0.75 day |
| Tests and docs | 0.25 day |
| **Total** | **About 2 days** |

Plus a small layout pass after the first real run.

## Open points

- **Cached input tokens (resolved).** The first real run showed that Bedrock's `inputTokens` excludes `cacheReadInputTokens`, so the stacked segments (uncached input, cache read, output) don't overlap. `summary.json` now reports `agent_cache_read` and `agent_cache_write` separately, and the cache shares are of all prompt tokens. See [evaluation-metrics.md](../components/evaluation-metrics.md#tokens_per_case).
- **Heatmap values.** The plan shows pass rate per category and metric. Mean score is the alternative, and could be shown on hover through the cell's `<title>`.
- **Failing-case order.** In the case explorer, sort by number of failed metrics (proposed), or keep dataset order?

## Done when

- `python -m evals.score <run>` writes `report.html` next to `report.md`, and `python -m evals.report <run>` regenerates both.
- The page opens offline, makes no network requests, and shows every section above for a full run and a `--deterministic-only` run.
- Every case can be read in full from the case explorer without opening the JSONL files.
- The escaping tests pass with a hostile answer.
- The full test suite passes with and without the `eval` group.

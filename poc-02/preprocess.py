#!/usr/bin/env python3
"""
Heading-aware chunker for the BC Gov SSO docs (github.com/bcgov/sso-docs).

Produces one Markdown file per chunk plus a Bedrock metadata sidecar
(<chunk>.md.metadata.json), ready to upload to S3 for an Amazon Bedrock
Knowledge Base whose data source uses chunking strategy NONE.

Strategy
  * Split on the document's heading structure (level A = first heading level
    below the H1, usually H2; level B = the next one, usually H3).
  * Never split inside a fenced code block or a table.
  * Prepend a breadcrumb ("Category > Page > Section > Subsection") to each
    chunk so it carries its context into the embedding.
  * Short pages stay whole; FAQ pages get one chunk per question.
  * Oversized sections are split at subsection, then block (paragraph / code /
    table / list) boundaries with overlap; oversized single blocks are split
    by rows (tables, header repeated), lines (code, fence repeated) or
    sentences (prose).
  * Tiny neighbouring sections are merged so no chunk is a useless fragment.
  * Cleans MDX/Docusaurus syntax outside code blocks only: imports/exports,
    admonitions (:::note), images, iframes, HTML comments, heading ids,
    horizontal rules; relative links are rewritten to absolute site URLs.

Usage
  python3 preprocess.py --repo ./sso-docs --out ./chunks
  python3 preprocess.py --repo ./sso-docs --out ./chunks --exclude 'releases/*'
  aws s3 sync ./chunks s3://<bucket>/sso-docs/ --delete

Requires: PyYAML (pip install pyyaml). tiktoken is used for token counts when
available; otherwise a conservative estimate is used. Neither matches Titan's
tokenizer exactly, so budgets are kept well under the embedding model limit.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

SITE = "https://bcgov.github.io/sso-docs"

# --------------------------------------------------------------------------
# Token counting
# --------------------------------------------------------------------------
try:  # pragma: no cover - depends on environment
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
    TOKENIZER = "tiktoken/cl100k_base"

    def ntok(text: str) -> int:
        return len(_ENC.encode(text, disallowed_special=()))

except Exception:  # no tiktoken, or encoding file not downloadable
    TOKENIZER = "estimate (max(chars/4, words*1.33))"

    def ntok(text: str) -> int:
        return max(1, int(max(len(text) / 4, len(text.split()) * 1.33)))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
ADMONITION_OPEN_RE = re.compile(r"^\s*:::\s*(\w+)\s*(.*)$")
ADMONITION_CLOSE_RE = re.compile(r"^\s*:::\s*$")
HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IFRAME_RE = re.compile(r"<iframe\b[^>]*?>.*?</iframe>|<iframe\b[^>]*/>", re.S | re.I)
HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
BR_RE = re.compile(r"<br\s*/?>", re.I)
HEADING_ID_RE = re.compile(r"\s*\{#[\w-]+\}\s*$")
MDX_IMPORT_RE = re.compile(r"^\s*(import\s.+\sfrom\s|import\s+['\"]|export\s+(default\s+)?(const|function|let|var)\b)")
DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`*\[(\"'])")

ADMONITION_LABELS = {
    "note": "Note", "tip": "Tip", "info": "Info", "warning": "Warning",
    "caution": "Caution", "danger": "Danger", "important": "Important",
}


def attr(tag: str, name: str) -> str:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag)
    return m.group(1) if m else ""


def slugify(text: str) -> str:
    """Approximate Docusaurus/github-slugger heading anchors."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def plain_heading(text: str) -> str:
    text = HEADING_ID_RE.sub("", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]", "", text)
    # drop emoji / symbols at ends, keep inner punctuation
    text = re.sub(r"^[^\w(]+|[^\w)?.!]+$", "", text, flags=re.UNICODE)
    return text.strip()


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def fence_marker(line: str) -> str | None:
    m = FENCE_RE.match(line)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Site model: categories and page URLs
# --------------------------------------------------------------------------
@dataclass
class Category:
    dir: Path
    label: str
    position: float
    description: str
    generated_index: bool
    slug: str


@dataclass
class Page:
    path: Path
    rel: PurePosixPath
    meta: dict
    body: str
    title: str
    url: str
    position: float
    category_path: list[str]


class Site:
    def __init__(self, repo: Path, docs: Path):
        self.repo, self.docs = repo, docs
        self.categories: dict[Path, Category] = {}
        self.pages: dict[Path, Page] = {}

    # -- categories ------------------------------------------------------
    def load_categories(self) -> None:
        for cat_file in sorted(self.docs.rglob("_category_.json")):
            try:
                data = json.loads(cat_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            d = cat_file.parent
            label = data.get("label") or d.name.replace("-", " ").title()
            link = data.get("link") or {}
            slug = link.get("slug") or "/category/" + slugify(label)
            self.categories[d] = Category(
                dir=d,
                label=label,
                position=float(data.get("position", 999)),
                description=link.get("description", ""),
                generated_index=link.get("type") == "generated-index",
                slug=slug,
            )

    def category_labels(self, path: Path) -> list[str]:
        labels = []
        d = path.parent
        while d != self.docs and self.docs in d.parents:
            cat = self.categories.get(d)
            labels.append(cat.label if cat else d.name.replace("-", " ").title())
            d = d.parent
        return list(reversed(labels))

    # -- pages -----------------------------------------------------------
    def page_url(self, path: Path, meta: dict) -> str:
        rel = PurePosixPath(path.relative_to(self.docs).as_posix())
        slug = meta.get("slug")
        if isinstance(slug, str) and slug:
            if slug.startswith("/"):
                return SITE + ("/" if slug == "/" else slug)
            return f"{SITE}/{rel.parent.as_posix()}/{slug}".replace("/./", "/")
        doc_id = meta.get("id") or rel.stem
        doc_id = re.sub(r"^\d+[-_]", "", str(doc_id))  # Docusaurus number prefixes
        parts = [re.sub(r"^\d+[-_]", "", p) for p in rel.parent.parts]
        if doc_id.lower() in ("index", "readme") or (parts and doc_id == parts[-1]):
            url_path = "/".join(parts)
        else:
            url_path = "/".join(parts + [doc_id])
        return f"{SITE}/{url_path}" if url_path else f"{SITE}/"

    def load_pages(self, exclude: list[str]) -> None:
        for path in sorted(list(self.docs.rglob("*.md")) + list(self.docs.rglob("*.mdx"))):
            rel = PurePosixPath(path.relative_to(self.docs).as_posix())
            if any(fnmatch.fnmatch(str(rel), pat) for pat in exclude):
                continue
            raw = path.read_text(encoding="utf-8")
            meta, body = split_frontmatter(raw)
            if meta.get("draft") or meta.get("unlisted"):
                continue
            title = meta.get("title")
            if not title:
                title = self._first_h1(body) or rel.stem.replace("-", " ").title()
            self.pages[path.resolve()] = Page(
                path=path,
                rel=rel,
                meta=meta,
                body=body,
                title=plain_heading(str(title)),
                url=self.page_url(path, meta),
                position=float(meta.get("sidebar_position", 999)),
                category_path=self.category_labels(path),
            )

    @staticmethod
    def _first_h1(body: str) -> str | None:
        fence = None
        for line in body.splitlines():
            mk = fence_marker(line)
            if mk:
                fence = None if fence and mk.startswith(fence[0]) and len(mk) >= len(fence) else (fence or mk)
                continue
            if fence:
                continue
            m = HEADING_RE.match(line)
            if m and len(m.group(1)) == 1:
                return m.group(2)
        return None

    # -- links -----------------------------------------------------------
    def resolve_link(self, target: str, page: Page) -> str:
        if re.match(r"^(https?:|mailto:|tel:|#)", target):
            if target.startswith("#"):
                return page.url + target
            return target
        path_part, _, anchor = target.partition("#")
        anchor = f"#{anchor}" if anchor else ""
        if not path_part:
            return target
        candidates = []
        if path_part.startswith("/"):
            candidates += [self.repo / path_part.lstrip("/"), self.docs / path_part.lstrip("/")]
        else:
            candidates.append(page.path.parent / path_part)
        for c in candidates:
            for cand in (c, c.with_suffix(".md"), c.with_suffix(".mdx")):
                p = self.pages.get(cand.resolve())
                if p:
                    return p.url + anchor
        if path_part.startswith("/"):
            # static asset or absolute site route
            return SITE + path_part + anchor
        if not re.search(r"\.mdx?$", path_part):
            # relative site route, e.g. ./category/frequently-asked-questions
            base = page.url if page.url.endswith("/") else page.url.rsplit("/", 1)[0] + "/"
            parts: list[str] = []
            for seg in (base[len(SITE):] + path_part).split("/"):
                if seg in ("", "."):
                    continue
                if seg == "..":
                    if parts:
                        parts.pop()
                    continue
                parts.append(seg)
            return f"{SITE}/{'/'.join(parts)}{anchor}"
        return target


# --------------------------------------------------------------------------
# Cleaning (fence-aware)
# --------------------------------------------------------------------------
def clean_markdown(body: str, page: Page, site: Site) -> str:
    out: list[str] = []
    fence: str | None = None
    in_comment = False
    open_admonitions = 0

    # iframes may span lines; handle them on the whole text but only outside
    # code by doing a first pass that protects code blocks.
    segments, buf, f = [], [], None
    for line in body.splitlines():
        mk = fence_marker(line)
        if mk and (f is None or (mk[0] == f[0] and len(mk) >= len(f))):
            if f is None:
                segments.append(("text", "\n".join(buf))); buf = [line]; f = mk
            else:
                buf.append(line); segments.append(("code", "\n".join(buf))); buf = []; f = None
            continue
        buf.append(line)
    segments.append(("code" if f else "text", "\n".join(buf)))

    def fix_iframe(m: re.Match) -> str:
        tag = m.group(0)
        src, title = attr(tag, "src"), attr(tag, "title") or "Embedded video"
        return f"[{title}]({src})" if src else ""

    rebuilt = []
    for kind, seg in segments:
        if kind == "text":
            seg = IFRAME_RE.sub(fix_iframe, seg)
            seg = re.sub(r"<!--.*?-->", "", seg, flags=re.S)
        rebuilt.append(seg)
    body = "\n".join(rebuilt)

    for line in body.splitlines():
        mk = fence_marker(line)
        if mk:
            if fence is None:
                fence = mk
            elif mk[0] == fence[0] and len(mk) >= len(fence):
                fence = None
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        if in_comment:
            continue

        if MDX_IMPORT_RE.match(line):
            continue
        if HR_RE.match(line):
            out.append("")
            continue
        m = ADMONITION_OPEN_RE.match(line)
        if m and m.group(1).lower() in ADMONITION_LABELS:
            label = ADMONITION_LABELS[m.group(1).lower()]
            title = m.group(2).strip()
            out.append(f"**{label}{': ' + title if title else ':'}**")
            open_admonitions += 1
            continue
        if ADMONITION_CLOSE_RE.match(line) and open_admonitions:
            open_admonitions -= 1
            continue

        h = HEADING_RE.match(line)
        if h:
            line = f"{h.group(1)} {HEADING_ID_RE.sub('', h.group(2))}"

        line = IMG_RE.sub(lambda m: f"[Image: {m.group(1)}]" if m.group(1).strip() else "", line)
        line = HTML_IMG_RE.sub(lambda m: f"[Image: {attr(m.group(0), 'alt')}]" if attr(m.group(0), "alt") else "", line)
        line = BR_RE.sub(" ", line)
        line = LINK_RE.sub(lambda m: f"[{m.group(1)}]({site.resolve_link(m.group(2), page)})", line)
        line = line.replace("\\<", "<").replace("\\>", ">")
        out.append(line.rstrip())

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# Structure: page -> preamble + level-A sections -> level-B subsections
# --------------------------------------------------------------------------
@dataclass
class Sub:
    heading: str | None
    text: str  # includes its own heading line


@dataclass
class Section:
    heading: str | None  # None for the page preamble
    intro: str           # heading line + text before first level-B heading
    subs: list[Sub] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join([t for t in [self.intro] + [s.text for s in self.subs] if t.strip()])


def heading_lines(text: str) -> list[tuple[int, int, str]]:
    """(line index, level, heading text) for headings outside code fences."""
    res, fence = [], None
    for i, line in enumerate(text.splitlines()):
        mk = fence_marker(line)
        if mk:
            if fence is None:
                fence = mk
            elif mk[0] == fence[0] and len(mk) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            res.append((i, len(m.group(1)), m.group(2)))
    return res


def parse_sections(text: str) -> tuple[list[Section], int | None, int | None]:
    lines = text.splitlines()
    heads = heading_lines(text)
    # Remove the H1 (title shown in breadcrumb instead)
    h1 = [h for h in heads if h[1] == 1]
    drop = {h1[0][0]} if h1 else set()
    levels = sorted({lvl for _, lvl, _ in heads if lvl > 1})
    lvl_a = levels[0] if levels else None
    lvl_b = levels[1] if len(levels) > 1 else None

    sections: list[Section] = [Section(None, "")]
    cur_lines: list[str] = []
    cur_sub: Sub | None = None
    head_at = {i: (lvl, t) for i, lvl, t in heads}

    def flush():
        nonlocal cur_lines
        chunk = "\n".join(cur_lines).strip()
        if cur_sub is not None:
            cur_sub.text = chunk
        else:
            sections[-1].intro = chunk
        cur_lines = []

    for i, line in enumerate(lines):
        if i in drop:
            continue
        if i in head_at:
            lvl, htxt = head_at[i]
            if lvl == lvl_a:
                flush()
                cur_sub = None
                sections.append(Section(plain_heading(htxt), ""))
            elif lvl == lvl_b and sections[-1].heading is not None:
                flush()
                cur_sub = Sub(plain_heading(htxt), "")
                sections[-1].subs.append(cur_sub)
        cur_lines.append(line)
    flush()
    return sections, lvl_a, lvl_b


# --------------------------------------------------------------------------
# Block splitting / packing
# --------------------------------------------------------------------------
def to_blocks(text: str) -> list[str]:
    """Split into atomic blocks: fenced code, tables, headings, paragraphs/list items."""
    blocks, buf, fence = [], [], None

    def push():
        if buf and "\n".join(buf).strip():
            blocks.append("\n".join(buf).strip("\n"))
        buf.clear()

    lines = text.splitlines()
    for idx, line in enumerate(lines):
        mk = fence_marker(line)
        if fence:
            buf.append(line)
            if mk and mk[0] == fence[0] and len(mk) >= len(fence):
                fence = None
                push()
            continue
        if mk:
            push()
            fence = mk
            buf.append(line)
            continue
        is_table = line.lstrip().startswith("|")
        prev_table = bool(buf) and buf[-1].lstrip().startswith("|")
        if is_table != prev_table and buf:
            push()
        if HEADING_RE.match(line):
            push()
            blocks.append(line)
            continue
        if not line.strip():
            push()
            continue
        buf.append(line)
    push()

    # Keep a heading glued to the block that follows it, and a lead-in sentence
    # ending in ":" glued to the table / code / list it introduces.
    def introduces(prev: str, nxt: str) -> bool:
        if not prev.rstrip().endswith(":") or fence_marker(prev.splitlines()[0]) or prev.lstrip().startswith("|"):
            return False
        first = nxt.lstrip()
        return bool(fence_marker(nxt.splitlines()[0]) or first.startswith("|") or re.match(r"([-*+]|\d+\.)\s", first))

    merged: list[str] = []
    for b in blocks:
        if merged and ((HEADING_RE.match(merged[-1]) and "\n" not in merged[-1]) or introduces(merged[-1], b)):
            merged[-1] = merged[-1] + "\n\n" + b
        else:
            merged.append(b)
    return merged


def split_big_block(block: str, budget: int) -> list[str]:
    lines = block.splitlines()
    first = lines[0] if lines else ""
    # Code block: repeat the fence
    mk = fence_marker(first)
    if mk:
        opener = first
        closer = lines[-1] if fence_marker(lines[-1]) and len(lines) > 1 else mk
        inner = lines[1:-1] if closer == lines[-1] else lines[1:]
        return [f"{opener}\n{p}\n{closer}" for p in pack_lines(inner, budget - ntok(opener) - ntok(closer) - 4)]
    # Heading or lead-in paragraph glued to a following block: peel it off,
    # split the rest on its own terms, and keep the lead-in with the first part.
    if "\n\n" in block and not first.lstrip().startswith("|"):
        head, rest = block.split("\n\n", 1)
        if ntok(head) < budget // 2:
            parts = split_big_block(rest, budget - ntok(head) - 2) if ntok(rest) > budget - ntok(head) - 2 else [rest]
            return [f"{head}\n\n{p}" if i == 0 else p for i, p in enumerate(parts)]
        return split_big_block(head, budget) + split_big_block(rest, budget)
    # Table: repeat header + separator
    if first.lstrip().startswith("|") and len(lines) > 2 and re.match(r"^\s*\|?\s*:?-{2,}", lines[1]):
        header = "\n".join(lines[:2])
        return [f"{header}\n{p}" for p in pack_lines(lines[2:], budget - ntok(header) - 2)]
    # Prose: sentences, then lines, then hard split
    pieces = SENTENCE_RE.split(block)
    if len(pieces) == 1:
        pieces = lines
    out = []
    for piece in pack_lines(pieces, budget, sep=" " if len(pieces) != len(lines) else "\n"):
        if ntok(piece) > budget:
            step = max(200, budget * 3)  # chars
            out += [piece[i:i + step] for i in range(0, len(piece), step)]
        else:
            out.append(piece)
    return out


def pack_lines(items: list[str], budget: int, sep: str = "\n") -> list[str]:
    out, cur, cur_t = [], [], 0
    for it in items:
        t = ntok(it) + 1
        if cur and cur_t + t > budget:
            out.append(sep.join(cur))
            cur, cur_t = [], 0
        cur.append(it)
        cur_t += t
    if cur:
        out.append(sep.join(cur))
    return out


def pack_blocks(blocks: list[str], budget: int, overlap: int) -> list[str]:
    """Greedy packing with block-level overlap between consecutive parts."""
    expanded: list[str] = []
    for b in blocks:
        expanded += split_big_block(b, budget) if ntok(b) > budget else [b]

    parts: list[list[str]] = []
    cur: list[str] = []
    cur_t = 0
    for b in expanded:
        bt = ntok(b) + 2
        if cur and cur_t + bt > budget:
            parts.append(cur)
            ov, ot = [], 0
            for x in reversed(cur):
                xt = ntok(x) + 2
                if ot + xt > overlap or fence_marker(x.splitlines()[0]) or x.lstrip().startswith("|"):
                    break  # don't duplicate code/tables as overlap
                ov.insert(0, x)
                ot += xt
            if len(ov) == len(cur) or ot + bt > budget:
                ov, ot = [], 0
            cur, cur_t = ov, ot
        cur.append(b)
        cur_t += bt
    if cur:
        parts.append(cur)
    return ["\n\n".join(p) for p in parts]


# --------------------------------------------------------------------------
# Chunk assembly
# --------------------------------------------------------------------------
@dataclass
class Unit:
    crumbs: list[str]          # section crumbs below the page title
    anchor: str | None
    text: str
    is_preamble: bool = False
    part: int = 1
    total_parts: int = 1


def has_content(text: str) -> bool:
    body = "\n".join(l for l in text.splitlines() if not HEADING_RE.match(l))
    return len(re.sub(r"\W", "", body)) >= 20


def build_units(page: Page, sections: list[Section], *, is_faq: bool,
                max_tokens: int, min_tokens: int, overlap_ratio: float, header_tokens: int) -> list[Unit]:
    budget = max_tokens - header_tokens
    overlap = int(budget * overlap_ratio)
    whole = "\n\n".join(s.text for s in sections if s.text.strip())

    # Small, non-FAQ page -> one chunk
    if not is_faq and ntok(whole) <= budget:
        return [Unit([], None, whole)] if has_content(whole) else []

    units: list[Unit] = []
    for sec in sections:
        if not sec.text.strip():
            continue
        crumbs = [sec.heading] if sec.heading else []
        anchor = slugify(sec.heading) if sec.heading else None
        pre = sec.heading is None
        if ntok(sec.text) <= budget:
            units.append(Unit(crumbs, anchor, sec.text, is_preamble=pre))
            continue
        # Oversized section with no subsections: split by blocks
        if not sec.subs:
            parts = pack_blocks(to_blocks(sec.text), budget, overlap)
            for i, p in enumerate(parts, 1):
                units.append(Unit(crumbs, anchor, p, is_preamble=pre, part=i, total_parts=len(parts)))
            continue

        # Oversized section: pack whole subsections; split any that are too big.
        # The section intro is packed first; if it alone is too big it's split too.
        pending: list[str] = []
        pending_names: list[str] = []
        pending_anchor = anchor
        pending_has_intro = False

        def flush_pending():
            nonlocal pending, pending_names, pending_anchor, pending_has_intro
            if pending:
                extra = [] if pending_has_intro or not pending_names else [" / ".join(pending_names)]
                units.append(Unit(crumbs + extra, pending_anchor, "\n\n".join(pending), is_preamble=pre))
            pending, pending_names, pending_anchor, pending_has_intro = [], [], anchor, False

        if sec.intro.strip():
            if ntok(sec.intro) > budget:
                parts = pack_blocks(to_blocks(sec.intro), budget, overlap)
                for i, p in enumerate(parts, 1):
                    units.append(Unit(crumbs, anchor, p, is_preamble=pre, part=i, total_parts=len(parts)))
            else:
                pending, pending_has_intro = [sec.intro], True

        for sub in sec.subs:
            st = ntok(sub.text)
            sub_anchor = slugify(sub.heading) if sub.heading else anchor
            if st > budget:
                flush_pending()
                # Keep the parent heading visible inside every part
                parts = pack_blocks(to_blocks(sub.text), budget, overlap)
                for i, p in enumerate(parts, 1):
                    units.append(Unit(crumbs + [sub.heading], sub_anchor, p, is_preamble=pre,
                                      part=i, total_parts=len(parts)))
                continue
            if pending and ntok("\n\n".join(pending)) + st > budget:
                flush_pending()
            if not pending:
                pending_anchor = sub_anchor
            pending.append(sub.text)
            if sub.heading:
                pending_names.append(sub.heading)
        flush_pending()

    # Merge tiny neighbours (never for FAQ questions, never across split parts)
    if is_faq:
        return [u for u in units if has_content(u.text)]
    merged: list[Unit] = []
    for u in units:
        if (merged and u.total_parts == 1 and merged[-1].total_parts == 1
                and (ntok(u.text) < min_tokens or ntok(merged[-1].text) < min_tokens)
                and ntok(merged[-1].text) + ntok(u.text) <= budget):
            prev = merged[-1]
            if prev.is_preamble:
                prev.crumbs, prev.anchor, prev.is_preamble = u.crumbs, u.anchor, False
            elif prev.crumbs != u.crumbs:
                common = []
                for a, b in zip(prev.crumbs, u.crumbs):
                    if a != b:
                        break
                    common.append(a)
                last_prev = prev.crumbs[len(common):]
                last_u = u.crumbs[len(common):]
                prev.crumbs = common + [" / ".join(filter(None, [" > ".join(last_prev), " > ".join(last_u)]))]
            prev.text = prev.text + "\n\n" + u.text
        else:
            merged.append(u)
    return [u for u in merged if has_content(u.text)]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def doc_type_for(rel: PurePosixPath) -> str:
    top = rel.parts[0].lower() if len(rel.parts) > 1 else ""
    if top == "faqs":
        return "faq"
    if top == "releases":
        return "release"
    return "guide"


def write_chunk(out_dir: Path, name: str, text: str, meta: dict) -> None:
    p = out_dir / f"{name}.md"
    p.write_text(text + "\n", encoding="utf-8")
    attrs = {k: v for k, v in meta.items() if v not in (None, "", [])}
    (out_dir / f"{name}.md.metadata.json").write_text(
        json.dumps({"metadataAttributes": attrs}, ensure_ascii=False, indent=2), encoding="utf-8")


def release_info(page: Page, body: str) -> tuple[str | None, int | None]:
    version = None
    m = re.search(r"v?(\d+\.\d+\.\d+)", page.title) or re.search(r"v?(\d+\.\d+\.\d+)", page.rel.stem)
    if m:
        version = m.group(1)
    d = DATE_RE.search(body[:400])
    date = int("".join(d.groups())) if d else None
    return version, date


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True, help="path to a clone of bcgov/sso-docs")
    ap.add_argument("--docs-dir", default="docs")
    ap.add_argument("--out", type=Path, default=Path("chunks"))
    ap.add_argument("--max-tokens", type=int, default=800, help="max tokens per chunk incl. breadcrumb")
    ap.add_argument("--min-tokens", type=int, default=120, help="merge neighbouring sections smaller than this")
    ap.add_argument("--overlap", type=float, default=0.15, help="overlap ratio between parts of a split section")
    ap.add_argument("--exclude", action="append", default=[], help="glob relative to docs dir, e.g. 'releases/*'")
    ap.add_argument("--no-url-in-text", action="store_true", help="don't append 'Source: <url>' to chunk text")
    ap.add_argument("--no-category-chunks", action="store_true", help="skip chunks describing each sidebar category")
    args = ap.parse_args()

    docs = (args.repo / args.docs_dir).resolve()
    if not docs.is_dir():
        print(f"docs dir not found: {docs}", file=sys.stderr)
        return 2
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    site = Site(args.repo.resolve(), docs)
    site.load_categories()
    site.load_pages(args.exclude)

    manifest = []

    def emit(name: str, crumb: str, body: str, url: str, meta: dict, part: int, total: int):
        header = f"# {crumb}" + (f" (part {part} of {total})" if total > 1 else "")
        text = f"{header}\n\n{body.strip()}"
        if not args.no_url_in_text:
            text += f"\n\nSource: {url}"
        meta = dict(meta, source_url=url, section=crumb, part=part, total_parts=total, token_count=ntok(text))
        write_chunk(args.out, name, text, meta)
        manifest.append({"file": f"{name}.md", **meta})

    # -- pages -------------------------------------------------------------
    for page in sorted(site.pages.values(), key=lambda p: str(p.rel)):
        dtype = doc_type_for(page.rel)
        cleaned = clean_markdown(page.body, page, site)
        sections, _, _ = parse_sections(cleaned)
        page_crumbs = page.category_path + [page.title]
        page_crumb_str = " > ".join(page_crumbs)
        header_tokens = ntok(f"# {page_crumb_str} > xxxxxxxxxxxxxxxxxxxxxxxx (part 9 of 9)\n\nSource: {page.url}#xxxxxxxxxxxxxxxx") + 8
        units = build_units(page, sections, is_faq=(dtype == "faq"), max_tokens=args.max_tokens,
                            min_tokens=args.min_tokens, overlap_ratio=args.overlap, header_tokens=header_tokens)
        tags = page.meta.get("tags") or []
        tags = [t if isinstance(t, str) else (t.get("label") if isinstance(t, dict) else str(t)) for t in tags]
        base = {
            "page_title": page.title,
            "page_url": page.url,
            "category": page.category_path[0] if page.category_path else "Overview",
            "doc_type": dtype,
            "file_path": f"{args.docs_dir}/{page.rel}",
            "tags": [t for t in tags if t],
            "description": str(page.meta.get("description", ""))[:500],
        }
        if dtype == "release":
            version, date = release_info(page, cleaned)
            base.update(release_version=version, release_date=date)
        stem = str(page.rel.with_suffix("")).replace("/", "__")
        for i, u in enumerate(units):
            crumb = " > ".join(page_crumbs + u.crumbs)
            url = page.url + (f"#{u.anchor}" if u.anchor else "")
            meta = dict(base, chunk_index=i)
            if dtype == "faq" and u.crumbs and not u.is_preamble:
                meta["question"] = u.crumbs[0]
            emit(f"{stem}__{i:03d}", crumb, u.text, url, meta, u.part, u.total_parts)

    # -- category overview chunks -------------------------------------------
    if not args.no_category_chunks:
        for d, cat in sorted(site.categories.items(), key=lambda kv: str(kv[0])):
            children = sorted([p for p in site.pages.values() if p.path.parent == d],
                              key=lambda p: (p.position, p.title))
            subcats = sorted([c for c in site.categories.values() if c.dir.parent == d], key=lambda c: c.position)
            if not children and not subcats:
                continue
            crumbs = site.category_labels(d / "_") + []
            crumb = " > ".join(crumbs)
            lines = [cat.description] if cat.description else []
            lines.append(f"\nThe {cat.label} section contains these pages:")
            lines += [f"- [{p.title}]({p.url})" + (f": {p.meta['description']}" if p.meta.get("description") else "")
                      for p in children]
            lines += [f"- [{c.label}]({SITE}{c.slug}) (sub-section)" for c in subcats]
            body = "\n".join(lines)
            url = SITE + cat.slug if cat.generated_index else (children[0].url if children else SITE + "/")
            rel = d.relative_to(docs).as_posix().replace("/", "__")
            budget = args.max_tokens - ntok(f"# {crumb} > Overview (part 9 of 9)\n\nSource: {url}") - 8
            parts = pack_blocks(to_blocks(body), budget, 0) if ntok(body) > budget else [body]
            for i, p in enumerate(parts):
                emit(f"_category__{rel}__{i:03d}", f"{crumb} > Overview", p, url,
                     {"page_title": cat.label, "page_url": url, "category": crumbs[0], "doc_type": "category",
                      "file_path": f"{args.docs_dir}/{d.relative_to(docs).as_posix()}/_category_.json",
                      "chunk_index": i}, i + 1, len(parts))

    (args.out.parent / f"{args.out.name}_manifest.jsonl").write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in manifest) + "\n", encoding="utf-8")

    # -- report ----------------------------------------------------------------
    toks = [m["token_count"] for m in manifest]
    by_type: dict[str, int] = {}
    for m in manifest:
        by_type[m["doc_type"]] = by_type.get(m["doc_type"], 0) + 1
    over = [m for m in manifest if m["token_count"] > args.max_tokens]
    print(f"Tokenizer: {TOKENIZER}")
    print(f"Pages: {len(site.pages)}   Chunks: {len(manifest)}   ->  {args.out}")
    print("By type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    if toks:
        q = statistics.quantiles(toks, n=20) if len(toks) > 1 else [toks[0]] * 19
        print(f"Tokens/chunk: min={min(toks)} p50={int(statistics.median(toks))} p95={int(q[18])} max={max(toks)}")
    if over:
        print(f"WARNING: {len(over)} chunk(s) exceed --max-tokens:")
        for m in over:
            print(f"  {m['file']}: {m['token_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
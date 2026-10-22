"""Deterministic checks around the LLM query planner.

The planner may rewrite a question; these functions make sure the rewrite
keeps what must not change (identifiers, error text, versions, negations),
only use metadata filters the knowledge base supports, and skip the planner
entirely for questions that are already precise.
"""

import logging
import re

from app.schemas.query_plan import MetadataFilter, QueryPlan

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Literals that must survive rewriting
# --------------------------------------------------------------------------

_QUOTED_RE = re.compile(r"\"([^\"\n]{2,200})\"|`([^`\n]{2,200})`|“([^”\n]{2,200})”")
_URL_RE = re.compile(r"(?:https?://|mailto:)[^\s<>()\[\]\"'`]+", re.I)
_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+)+(?:\.x)?\b", re.I)
# snake_case, SCREAMING_CASE, dotted keys, camelCase, kebab with digits,
# and numbers of 3+ digits (HTTP status codes, integration ids).
_IDENTIFIER_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"|[a-z]+[A-Z][A-Za-z0-9]*"
    r"|[A-Za-z]+(?:\.[A-Za-z0-9_]+){2,}"
    r"|(?=[\w-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+"
    r"|\d{3,})\b"
)
_NEGATION_RE = re.compile(
    r"\b(?:not|no|without|never|except|excluding|cannot|none|neither|nor)\b|n't\b",
    re.I,
)


def extract_literals(text: str) -> list[str]:
    """Substrings of `text` a rewrite must keep verbatim."""

    found: list[str] = []
    for m in _QUOTED_RE.finditer(text):
        found.append(next(g for g in m.groups() if g))
    found += [u.rstrip(".,;:!?") for u in _URL_RE.findall(text)]
    without_urls = _URL_RE.sub(" ", text)
    found += _VERSION_RE.findall(without_urls)
    found += _IDENTIFIER_RE.findall(without_urls)
    return list(dict.fromkeys(f.strip() for f in found if f.strip()))


def has_negation(text: str) -> bool:
    return bool(_NEGATION_RE.search(text))


def _contains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def enforce_preservation(plan: QueryPlan, question: str) -> QueryPlan:
    """Repairs a plan whose rewrites dropped literals or negations from
    `question`. Returns a new plan; the input is not modified."""

    literals = extract_literals(question)
    # Terms the planner flagged are kept only if they really are in the question.
    literals += [t for t in plan.preserved_terms if t.strip() and _contains(question, t)]
    literals = list(dict.fromkeys(literals))
    negated = has_negation(question)

    canonical = plan.canonical_query.strip() or question
    if negated and not has_negation(canonical):
        canonical = question
    missing = [lit for lit in literals if not _contains(canonical, lit)]
    if missing:
        canonical = f"{canonical} {' '.join(missing)}"

    variants = [v.strip() for v in plan.query_variants if v.strip()]
    if negated:
        dropped = [v for v in variants if not has_negation(v)]
        if dropped:
            logger.info("dropping variants that lost a negation: %s", dropped)
        variants = [v for v in variants if has_negation(v)]
    if literals and not any(all(_contains(v, lit) for lit in literals) for v in variants):
        variants = [question, *variants]
    variants = list(dict.fromkeys(variants))[:4]

    return plan.model_copy(update={
        "original_question": question,
        "canonical_query": canonical,
        "query_variants": variants,
        "preserved_terms": literals,
    })


# --------------------------------------------------------------------------
# Metadata filters
# --------------------------------------------------------------------------

# Fields written by chunker/preprocess.py, with the operators that make sense
# for them. A filter must also be "explicitly justified": its value (or one of
# the trigger words below) has to appear in the user's question.
DOC_TYPES = {"guide", "faq", "release", "category"}
_DOC_TYPE_TRIGGERS = {
    "faq": re.compile(r"\bfaqs?\b|frequently asked", re.I),
    "release": re.compile(r"\brelease(?:s| notes?)?\b|\bchangelog\b|\bwhat'?s new\b", re.I),
    "guide": re.compile(r"\bguides?\b|\bhow-?to\b|\btutorial\b", re.I),
    "category": re.compile(r"\bcategor(?:y|ies)\b|\boverview\b", re.I),
}
ALLOWED_FILTERS: dict[str, set[str]] = {
    "doc_type": {"equals", "in"},
    "category": {"equals", "in"},
    "tags": {"list_contains"},
    "release_version": {"equals", "in"},
}
_MAX_FILTER_VALUES = 5
_MAX_VALUE_LEN = 100


def _values(f: MetadataFilter) -> list[str] | None:
    values = f.value if isinstance(f.value, list) else [f.value]
    values = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    if not values or len(values) > _MAX_FILTER_VALUES or any(len(v) > _MAX_VALUE_LEN for v in values):
        return None
    if f.operator != "in" and len(values) != 1:
        return None
    return values


def _justified(field: str, value: str, question: str) -> bool:
    if field == "doc_type":
        trigger = _DOC_TYPE_TRIGGERS.get(value)
        return bool(trigger and trigger.search(question))
    if field == "release_version":
        return value.lstrip("vV") in {v.lstrip("vV") for v in _VERSION_RE.findall(question)}
    return _contains(question, value)


def validate_filters(filters: list[MetadataFilter], question: str) -> list[MetadataFilter]:
    """The subset of `filters` that are well-formed, on allowed fields, and
    justified by the question. Everything else is dropped and logged."""

    valid: list[MetadataFilter] = []
    for f in filters:
        reason = None
        values = _values(f)
        if f.field not in ALLOWED_FILTERS:
            reason = "field not allowed"
        elif f.operator not in ALLOWED_FILTERS[f.field]:
            reason = "operator not allowed for field"
        elif values is None:
            reason = "invalid value"
        elif f.field == "doc_type" and any(v not in DOC_TYPES for v in values):
            reason = "unknown doc_type"
        elif not all(_justified(f.field, v, question) for v in values):
            reason = "not justified by the question"
        if reason:
            logger.info("dropping filter %s: %s", f.model_dump(), reason)
            continue
        if f.field == "release_version":
            # The chunker stores versions as "X.Y.Z" without a "v" prefix.
            values = [v.lstrip("vV") for v in values]
            if not all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in values):
                logger.info("dropping filter %s: release_version must be X.Y.Z", f.model_dump())
                continue
        valid.append(f.model_copy(update={"value": values if f.operator == "in" else values[0]}))
    return valid


def to_retrieval_filter(filters: list[MetadataFilter]) -> dict | None:
    """Bedrock Knowledge Base RetrievalFilter for validated filters."""

    clauses = []
    for f in filters:
        if f.operator == "equals":
            clauses.append({"equals": {"key": f.field, "value": f.value}})
        elif f.operator == "in":
            clauses.append({"in": {"key": f.field, "value": list(f.value)}})
        elif f.operator == "list_contains":
            clauses.append({"listContains": {"key": f.field, "value": f.value}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"andAll": clauses}


# --------------------------------------------------------------------------
# Fast path: skip the planner for precise questions
# --------------------------------------------------------------------------

_ANAPHORA_RE = re.compile(
    r"\b(?:it|its|that|this|these|those|they|them|their|same|above|previous|"
    r"there|then|one|ones)\b",
    re.I,
)
_FOLLOW_UP_START_RE = re.compile(
    r"^\s*(?:and|also|what about|how about|and what|so|but|ok(?:ay)?|then|same)\b",
    re.I,
)
_MULTI_CLAUSE_RE = re.compile(
    r"\b(?:and (?:also|then|how|what|why|where|when|can|do|does|is|should|which)|"
    r"as well as|also|plus|in addition|followed by|after that)\b|;",
    re.I,
)
SIMPLE_MAX_WORDS = 12
PRECISE_MAX_WORDS = 25


def is_simple_query(question: str, has_history: bool) -> bool:
    """True when `question` can be searched as-is: self-contained, one
    topic, and short or anchored by an exact literal."""

    text = question.strip()
    if not text:
        return False
    if has_history and (_ANAPHORA_RE.search(_QUOTED_RE.sub(" ", text)) or _FOLLOW_UP_START_RE.search(text)):
        return False
    if text.count("?") > 1 or _MULTI_CLAUSE_RE.search(text):
        return False
    words = len(text.split())
    if words <= SIMPLE_MAX_WORDS:
        return True
    return words <= PRECISE_MAX_WORDS and bool(extract_literals(text))

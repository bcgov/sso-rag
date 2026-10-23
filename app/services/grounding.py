"""Grounding checks for the agent's answers.

The system prompt makes the model end every reply with a grounding block:

    <grounding>{"response_type": "answer", "grounding_status": "SUPPORTED",
                "citations": ["https://..."]}</grounding>

`verify()` strips that block and decides what the user gets. The answer is
replaced with NO_DOCUMENTATION_RESPONSE when the model says the docs don't
cover it (NOT_SUPPORTED), or when it is rejected because:

- `grounding_status` is SUPPORTED but `citations` is empty;
- a citation URL was not returned by the retrieval tool;
- retrieval returned no results but the answer contains factual guidance;
- it contains URLs, endpoint paths, code or configuration values (and roles or
  claims written as code) that appear neither in the retrieved text nor in
  the user's message.

Always allowed: the support contacts in NO_DOCUMENTATION_RESPONSE (email and
Teams link), `<placeholders>`, URLs whose whole host is a placeholder
(`https://<your-app-host>/callback`), which are values the user supplies, and
example URLs on reserved or loopback hosts (example.com/.net/.org, *.example,
*.test, *.invalid, localhost, 127.0.0.1).

The checks are deterministic string matches against the retrieved text. They
can't judge plain-prose claims; the prompt therefore asks for every role,
claim, configuration key/value and endpoint to be written as `code`, which
is checked.
"""

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.common.constants import NO_DOCUMENTATION_RESPONSE

SUPPORTED = "SUPPORTED"
PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
NOT_SUPPORTED = "NOT_SUPPORTED"
GROUNDING_STATUSES = {SUPPORTED, PARTIALLY_SUPPORTED, NOT_SUPPORTED}

ANSWER = "answer"
RESPONSE_TYPES = {ANSWER, "clarification", "conversational"}

_BLOCK_RE = re.compile(r"<grounding>(.*?)</grounding>", re.S | re.I)
# A block cut off before its closing tag (e.g. the model hit max tokens).
_OPEN_BLOCK_RE = re.compile(r"<grounding>.*\Z", re.S | re.I)
# A grounding block wrapped in a code fence leaves an empty fence behind.
_EMPTY_FENCE_RE = re.compile(r"(```|~~~)[^\n]*\s*\1\s*\Z")

_URL_RE = re.compile(r"(?:https?://|mailto:)[^\s<>()\[\]\"'`]+", re.I)
_FENCE_RE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)(?:^\1[ \t]*$|\Z)", re.S | re.M)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_DOUBLE_QUOTED_RE = re.compile(r'"([^"\n]{2,200})"')
_SINGLE_QUOTED_RE = re.compile(r"'([^'\s]{2,200})'")
# Endpoint-like paths with at least two segments: /auth/realms/standard, …
# The look-behind skips "IDIR/BCeID", "and/or" and URLs' own paths.
_PATH_RE = re.compile(r"(?<![\w/.:~-])/[\w.~{}<>$:-]+(?:/[\w.~{}<>$:-]*)+")
# Values the user is told to fill in, not documented values.
_PLACEHOLDER_RE = re.compile(
    r"<[^<>\n]+>|\{\{?[^{}\n]+\}?\}|\$\{[^}\n]+\}|\bYOUR[_-][A-Z0-9_-]+\b|\.\.\.|…",
    re.I,
)

CONTACT_URLS = {"mailto:bcgov.sso@gov.bc.ca"}


def _contact_urls() -> set[str]:
    """The support contacts the prompt tells the model to point to: the
    email plus every link in NO_DOCUMENTATION_RESPONSE (e.g. Teams)."""
    return CONTACT_URLS | {u.rstrip(".,;:!?*_") for u in _URL_RE.findall(NO_DOCUMENTATION_RESPONSE)}


# A URL whose whole host is a placeholder ("https://<your-app-host>/callback")
# is a value the user supplies, not a documented one. Hosts that only start
# with one ("https://<env>.loginproxy.gov.bc.ca/...") are still checked.
_PLACEHOLDER_HOST_URL_RE = re.compile(r"^https?://(?:<[^<>\s]+>|\{\{?[^{}\s]+\}?\}|\$\{[^}\s]+\})(?=[/:?#]|$)", re.I)

# Hosts reserved for documentation and testing (RFC 2606 / RFC 6761) and
# loopback addresses can never be real BC Gov services, so example URLs on
# them ("https://myapp.example.com/callback", "http://localhost:3000") are
# allowed. Real-looking invented hosts ("https://your-app.gov.bc.ca") are not.
_EXAMPLE_DOMAINS = ("example.com", "example.net", "example.org")
_EXAMPLE_TLDS = {"example", "test", "invalid", "localhost"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_example_url(url: str) -> bool:
    try:
        host = urlsplit(_clean_url(url)).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.rstrip(".").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    if any(host == d or host.endswith("." + d) for d in _EXAMPLE_DOMAINS):
        return True
    return "." in host and host.rsplit(".", 1)[1] in _EXAMPLE_TLDS


@dataclass(frozen=True)
class Grounding:
    response_type: str
    status: str | None
    citations: list[str]


@dataclass
class Verdict:
    """What to send to the user."""

    text: str
    # None for clarifications and greetings, which make no factual claims.
    status: str | None
    citations: list[dict] = field(default_factory=list)
    # Why the model's answer was rejected; empty when it was delivered.
    reasons: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.reasons)

    @property
    def replaced(self) -> bool:
        """True when the model's text was swapped for the no-docs reply."""
        return self.text == NO_DOCUMENTATION_RESPONSE


def grounding_block(status: str, citations: list[str] | None = None) -> str:
    return "<grounding>" + json.dumps({
        "response_type": ANSWER,
        "grounding_status": status,
        "citations": citations or [],
    }) + "</grounding>"


def split_grounding(text: str) -> tuple[str, Grounding | None]:
    """The reply without its grounding block, and the parsed block (None if
    missing or malformed). The last block wins; all are removed."""

    blocks = _BLOCK_RE.findall(text)
    body = _OPEN_BLOCK_RE.sub("", _BLOCK_RE.sub("", text)).rstrip()
    body = _EMPTY_FENCE_RE.sub("", body).strip()
    if not blocks:
        return body, None

    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", blocks[-1].strip())
    try:
        data = json.loads(raw)
    except ValueError:
        return body, None
    if not isinstance(data, dict):
        return body, None

    status = data.get("grounding_status")
    status = status.strip().upper() if isinstance(status, str) else None
    response_type = data.get("response_type")
    response_type = response_type.strip().lower() if isinstance(response_type, str) else ANSWER
    citations = data.get("citations")
    citations = [c.strip() for c in citations if isinstance(c, str) and c.strip()] \
        if isinstance(citations, list) else []

    return body, Grounding(
        response_type=response_type if response_type in RESPONSE_TYPES else ANSWER,
        status=status if status in GROUNDING_STATUSES else None,
        citations=citations,
    )


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:!?*_")


def _url_key(url: str) -> str:
    """Compare URLs without their #fragment, trailing slash or case."""
    return _clean_url(url).split("#", 1)[0].rstrip("/").lower()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class _Evidence:
    sources: dict[str, dict]  # url key -> best-scoring retrieved result
    urls: set[str]            # url keys allowed in the answer
    text: str                 # normalized corpus for value checks


def _evidence(retrieved: list[dict], prompt: str) -> _Evidence:
    sources: dict[str, dict] = {}
    for r in retrieved:
        uri = r.get("uri")
        if not uri:
            continue
        key = _url_key(uri)
        if key not in sources or r.get("score", 0) > sources[key].get("score", 0):
            sources[key] = r

    texts = [r.get("text") or "" for r in retrieved]
    contacts = _contact_urls()
    urls = set(sources) | {_url_key(u) for t in texts + [prompt] for u in _URL_RE.findall(t)}
    urls |= {_url_key(u) for u in contacts}

    # Backticks are formatting in the docs' markdown, not part of values.
    corpus = "\n".join(texts + [r.get("uri") or "" for r in retrieved]
                       + [prompt, NO_DOCUMENTATION_RESPONSE] + list(contacts))
    return _Evidence(sources, urls, _norm(corpus.replace("`", "")))


def _documented(value: str, evidence: _Evidence) -> bool:
    """Every literal part of `value` (placeholders excluded) is in the
    evidence text. A URL on a placeholder host is entirely user-supplied."""
    value = value.strip()
    if _PLACEHOLDER_HOST_URL_RE.match(value) or (_URL_RE.fullmatch(value) and _is_example_url(value)):
        return True
    for fragment in _PLACEHOLDER_RE.split(value):
        fragment = _norm(fragment).strip(" .,;:")
        if len(fragment) >= 2 and fragment not in evidence.text:
            return False
    return True


def _values(body: str) -> tuple[list[str], list[str]]:
    """(URLs, other checkable values) written in the answer."""

    urls = [_clean_url(u) for u in _URL_RE.findall(body)]
    values: list[str] = []

    prose = body
    for match in _FENCE_RE.finditer(body):
        code = match.group(2)
        values += _DOUBLE_QUOTED_RE.findall(code) + _SINGLE_QUOTED_RE.findall(code)
        values += _PATH_RE.findall(_URL_RE.sub(" ", code))
        prose = prose.replace(match.group(0), " ")

    values += [v.strip() for v in _INLINE_CODE_RE.findall(prose)]
    values += _PATH_RE.findall(_URL_RE.sub(" ", _INLINE_CODE_RE.sub(" ", prose)))
    return urls, [v for v in dict.fromkeys(values) if v and not _URL_RE.fullmatch(v)]


def _has_factual_markers(body: str) -> bool:
    urls, values = _values(body)
    contacts = {_url_key(c) for c in _contact_urls()}
    return bool(values) or any(
        _url_key(u) not in contacts and not _is_example_url(u) for u in urls
    )


def verify(reply: str, retrieved: list[dict], prompt: str) -> Verdict:
    """Checks the model's reply against what retrieval returned this turn.

    `retrieved` holds one dict per result: {"title", "uri", "score", "text"}.
    """

    body, grounding = split_grounding(reply)
    evidence = _evidence(retrieved, prompt)
    reasons: list[str] = []

    if grounding is None:
        reasons.append("missing or malformed grounding block")
        return Verdict(NO_DOCUMENTATION_RESPONSE, NOT_SUPPORTED, reasons=reasons)

    is_answer = grounding.response_type == ANSWER

    if is_answer and grounding.status is None:
        reasons.append("missing or invalid grounding_status")
    if is_answer and grounding.status == NOT_SUPPORTED:
        return Verdict(NO_DOCUMENTATION_RESPONSE, NOT_SUPPORTED)
    if is_answer and grounding.status == SUPPORTED and not grounding.citations:
        reasons.append("grounding_status is SUPPORTED but citations is empty")

    uncited = [c for c in grounding.citations if _url_key(c) not in evidence.sources]
    if uncited:
        reasons.append(f"citations not returned by retrieval: {uncited}")

    if not retrieved and (is_answer or _has_factual_markers(body)):
        reasons.append("retrieval returned no results but the answer contains factual guidance")

    urls, values = _values(body)
    unknown_urls = [
        u for u in dict.fromkeys(urls)
        if _url_key(u) not in evidence.urls and not _is_example_url(u)
    ]
    if unknown_urls:
        reasons.append(f"undocumented URLs: {unknown_urls}")
    unknown_values = [v for v in values if not _documented(v, evidence)]
    if unknown_values:
        reasons.append(f"undocumented values: {unknown_values}")

    if reasons or not body:
        return Verdict(NO_DOCUMENTATION_RESPONSE, NOT_SUPPORTED,
                       reasons=reasons or ["empty answer"])

    # Sources: the cited results plus any retrieved page the answer links.
    cited = list(dict.fromkeys(
        [_url_key(c) for c in grounding.citations]
        + [_url_key(u) for u in urls if _url_key(u) in evidence.sources]
    ))
    return Verdict(
        text=body,
        status=grounding.status if is_answer else None,
        citations=[evidence.sources[k] for k in cited],
    )

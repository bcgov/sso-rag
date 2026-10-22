from app.schemas.query_plan import MetadataFilter, QueryPlan
from app.services.query_guards import (
    enforce_preservation,
    extract_literals,
    has_negation,
    is_simple_query,
    to_retrieval_filter,
    validate_filters,
)


def plan(**kw) -> QueryPlan:
    return QueryPlan(**{"canonical_query": "x", **kw})


# -- literals ---------------------------------------------------------------

def test_extract_literals_finds_identifiers_errors_versions_urls():
    q = ('Getting "invalid_grant: Code not valid" with client sso-app-4567 '
         'on Keycloak 26.1 after v4.7.1, see https://example.com/a and HTTP 401')
    lits = extract_literals(q)
    assert "invalid_grant: Code not valid" in lits
    assert "sso-app-4567" in lits
    assert "26.1" in lits and "v4.7.1" in lits
    assert "https://example.com/a" in lits
    assert "401" in lits


def test_extract_literals_ignores_plain_hyphenated_words():
    assert extract_literals("how does single sign-on work for multi-factor") == []


def test_has_negation():
    assert has_negation("public client without a secret")
    assert has_negation("it doesn't work")
    assert not has_negation("how do I add a role")


# -- preservation -----------------------------------------------------------

def test_enforce_preservation_restores_dropped_literals():
    q = "What causes invalid_redirect_uri for client foo_bar in 4.7.1?"
    p = enforce_preservation(plan(canonical_query="redirect error", query_variants=["redirect uri"]), q)
    for lit in ("invalid_redirect_uri", "foo_bar", "4.7.1"):
        assert lit in p.canonical_query
    assert p.original_question == q
    # No variant kept every literal, so the verbatim question is added.
    assert p.query_variants[0] == q


def test_enforce_preservation_drops_variants_that_lose_negation():
    q = "How do I log in without IDIR?"
    p = enforce_preservation(
        plan(canonical_query="login with IDIR", query_variants=["IDIR login", "login not using IDIR"]),
        q,
    )
    assert p.canonical_query == q
    assert p.query_variants == ["login not using IDIR"]


def test_enforce_preservation_ignores_hallucinated_preserved_terms():
    q = "how do roles work"
    p = enforce_preservation(plan(canonical_query="client roles", preserved_terms=["made_up_term"]), q)
    assert "made_up_term" not in p.canonical_query
    assert p.preserved_terms == []


def test_enforce_preservation_caps_variants():
    p = enforce_preservation(plan(canonical_query="a", query_variants=["b", "c", "d", "e"]), "a")
    assert len(p.query_variants) <= 4


# -- filters ----------------------------------------------------------------

def test_validate_filters_keeps_justified_and_drops_others():
    q = "In the FAQ, what changed in release 4.7.1 for tag keycloak?"
    filters = [
        MetadataFilter(field="doc_type", operator="equals", value="faq", justification="FAQ"),
        MetadataFilter(field="release_version", operator="equals", value="v4.7.1", justification="4.7.1"),
        MetadataFilter(field="tags", operator="list_contains", value="keycloak", justification="tag"),
        MetadataFilter(field="file_path", operator="equals", value="x", justification="-"),
        MetadataFilter(field="doc_type", operator="equals", value="guide", justification="-"),
        MetadataFilter(field="doc_type", operator="equals", value="blog", justification="-"),
        MetadataFilter(field="tags", operator="equals", value="keycloak", justification="-"),
        MetadataFilter(field="category", operator="equals", value="Advanced", justification="-"),
    ]
    valid = validate_filters(filters, q)
    assert [(f.field, f.value) for f in valid] == [
        ("doc_type", "faq"), ("release_version", "4.7.1"), ("tags", "keycloak")
    ]


def test_validate_filters_rejects_bad_release_version_and_multi_value_equals():
    q = "release notes for 4.7 and 4.8"
    filters = [
        MetadataFilter(field="release_version", operator="equals", value="4.7", justification="-"),
        MetadataFilter(field="doc_type", operator="equals", value=["release", "faq"], justification="-"),
    ]
    assert validate_filters(filters, q) == []


def test_to_retrieval_filter():
    assert to_retrieval_filter([]) is None
    one = [MetadataFilter(field="doc_type", operator="equals", value="faq", justification="")]
    assert to_retrieval_filter(one) == {"equals": {"key": "doc_type", "value": "faq"}}
    many = one + [
        MetadataFilter(field="category", operator="in", value=["A", "B"], justification=""),
        MetadataFilter(field="tags", operator="list_contains", value="t", justification=""),
    ]
    assert to_retrieval_filter(many) == {"andAll": [
        {"equals": {"key": "doc_type", "value": "faq"}},
        {"in": {"key": "category", "value": ["A", "B"]}},
        {"listContains": {"key": "tags", "value": "t"}},
    ]}


# -- fast path --------------------------------------------------------------

def test_simple_queries_skip_planner():
    assert is_simple_query("How do I request a BCeID integration?", has_history=False)
    assert is_simple_query(
        'What does "Invalid parameter: redirect_uri" mean when I log in to my app from localhost?',
        has_history=False,
    )


def test_follow_ups_and_multi_part_need_planner():
    assert not is_simple_query("what about prod?", has_history=True)
    assert not is_simple_query("Does it support PKCE?", has_history=True)
    assert not is_simple_query("How do I add roles and how do I assign them to users?", has_history=False)
    assert not is_simple_query("What is PKCE? Do I need it?", has_history=False)
    assert not is_simple_query(" ".join(["word"] * 20), has_history=False)


def test_anaphora_ignored_without_history():
    assert is_simple_query("Does it support PKCE?", has_history=False)

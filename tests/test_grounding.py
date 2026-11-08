import re

import pytest

from app.common.constants import NO_DOCUMENTATION_RESPONSE
from app.services.grounding import NOT_SUPPORTED, PARTIALLY_SUPPORTED, SUPPORTED, verify

DOC = "https://bcgov.github.io/sso-docs/#requesting-integration"
CSS = "https://sso-requests.apps.gold.devops.gov.bc.ca/"
RETRIEVED = [{
    "title": "Requesting integration",
    "uri": DOC,
    "score": 0.9,
    "text": f"Sign in to [CSS]({CSS}) with your IDIR with MFA account and request a new integration.",
}]
TEAMS = re.search(r"\((https://teams\.microsoft\.com[^)]+)\)", NO_DOCUMENTATION_RESPONSE).group(1)


def _reply(body: str, status: str = SUPPORTED, citations=(DOC,)) -> str:
    cites = ", ".join(f'"{c}"' for c in citations)
    return f'{body}\n<grounding>{{"response_type": "answer", "grounding_status": "{status}", "citations": [{cites}]}}</grounding>'


def test_documented_urls_pass():
    verdict = verify(_reply(f"Sign in to the [CSS app]({CSS}) ([guide]({DOC}))."), RETRIEVED, "q")
    assert not verdict.rejected and verdict.status == SUPPORTED


def test_hallucinated_urls_are_rejected():
    body = "Go to https://sso.gov.bc.ca/ and set the redirect URI to https://your-app.gov.bc.ca/callback."
    verdict = verify(_reply(body), RETRIEVED, "q")
    assert verdict.text == NO_DOCUMENTATION_RESPONSE and verdict.status == NOT_SUPPORTED
    assert verdict.reasons == [
        "undocumented URLs: ['https://sso.gov.bc.ca/', 'https://your-app.gov.bc.ca/callback']"
    ]


def test_partial_answer_may_link_the_support_contacts():
    body = (
        f"Sign in to the [CSS app]({CSS}). The documentation doesn't cover custom claims; "
        f"ask in [Keycloak How-to]({TEAMS}) or email bcgov.sso@gov.bc.ca."
    )
    verdict = verify(_reply(body, PARTIALLY_SUPPORTED), RETRIEVED, "q")
    assert not verdict.rejected, verdict.reasons
    assert verdict.status == PARTIALLY_SUPPORTED


def test_angle_bracket_placeholder_urls_pass():
    body = f"Sign in to the [CSS app]({CSS}) and add `https://<your-app-host>/callback` as a redirect URI."
    verdict = verify(_reply(body), RETRIEVED, "q")
    assert not verdict.rejected, verdict.reasons


def test_partial_placeholder_host_is_still_checked():
    body = f"Sign in to the [CSS app]({CSS}) and call `https://<env>.loginproxy.gov.bc.ca/auth/realms/standard/made-up`."
    verdict = verify(_reply(body), RETRIEVED, "q")
    assert verdict.rejected and "undocumented URLs" in verdict.reasons[0]
    assert "made-up" in verdict.reasons[0]


def test_contact_links_alone_are_not_factual_guidance():
    # A conversational reply with no search may point to the support contacts.
    body = f"I can help with SSO questions, or you can ask in [Keycloak How-to]({TEAMS})."
    reply = body + '\n<grounding>{"response_type": "conversational", "citations": []}</grounding>'
    verdict = verify(reply, [], "what can you do?")
    assert not verdict.rejected, verdict.reasons
    assert verdict.status is None


@pytest.mark.parametrize("url", [
    "https://myapp.example.com/callback",
    "https://example.org",
    "http://localhost:3000/callback",
    "http://127.0.0.1:8080/",
    "https://app.test/logout",
    "https://my-app.example/cb",
    "http://app.localhost:5173",
])
def test_example_and_loopback_urls_are_allowed(url):
    for body in (f"Add {url} as a redirect URI.", f"Add `{url}` as a redirect URI.",
                 f'```json\n{{"redirect_uri": "{url}"}}\n```'):
        verdict = verify(_reply(f"Sign in to the [CSS app]({CSS}). {body}"), RETRIEVED, "q")
        assert not verdict.rejected, (body, verdict.reasons)


@pytest.mark.parametrize("url", [
    "https://your-app.gov.bc.ca/callback",
    "https://example.com.attacker.ca/",
    "https://notexample.com/",
    "https://localhost.gov.bc.ca/",
    "https://sso.gov.bc.ca/",
])
def test_real_looking_hosts_are_still_rejected(url):
    verdict = verify(_reply(f"Sign in to the [CSS app]({CSS}) and use {url}"), RETRIEVED, "q")
    assert verdict.rejected and "undocumented URLs" in verdict.reasons[0]


def _clarify(body: str) -> str:
    return body + '\n<grounding>{"response_type": "clarification", "citations": []}</grounding>'


@pytest.mark.parametrize("retrieved", [RETRIEVED, []])
def test_plain_text_clarifying_question_is_delivered(retrieved):
    body = ("Which identity provider are you using (IDIR or Business BCeID), and in which environment? "
            "What exact error do you see?")
    verdict = verify(_clarify(body), retrieved, "my login isn't working")
    assert not verdict.rejected, verdict.reasons
    assert verdict.text == body and verdict.status is None and verdict.citations == []


def test_clarifying_question_cannot_smuggle_undocumented_values():
    verdict = verify(_clarify("Do you see `invalid_grant` from https://sso.gov.bc.ca/?"), RETRIEVED, "q")
    assert verdict.rejected


# --- conversational replies without a grounding block -------------------------

GREETING_REPLY = "Hello! I can help with BC Government SSO questions. What do you need?"


def test_conversational_reply_without_block_is_delivered():
    verdict = verify(GREETING_REPLY, [], "Hello", conversational=True)
    assert not verdict.rejected
    assert verdict.text == GREETING_REPLY
    assert verdict.status is None


def test_missing_block_still_rejected_when_not_conversational():
    verdict = verify(GREETING_REPLY, [], "Hello")
    assert verdict.rejected
    assert verdict.text == NO_DOCUMENTATION_RESPONSE


@pytest.mark.parametrize("reply", [
    f"Hi! Start at {CSS} to request one.",
    "Hi! Use the `realm-admin` role.",
])
def test_conversational_reply_with_factual_markers_needs_block(reply):
    verdict = verify(reply, [], "Hello", conversational=True)
    assert verdict.rejected


def test_conversational_flag_ignored_when_retrieval_ran():
    verdict = verify(GREETING_REPLY, RETRIEVED, "Hello", conversational=True)
    assert verdict.rejected


# --- URLs with user-supplied placeholder values -------------------------------

LOGOUT_DOC = "https://bcgov.github.io/sso-docs/best-practices/dos-donts"
LOGOUT_RETRIEVED = [{
    "title": "Logout",
    "uri": LOGOUT_DOC,
    "score": 0.9,
    "text": (
        "Redirect the user to Keycloak's `end_session_endpoint` with `id_token_hint` "
        "(the user's ID token) and `post_logout_redirect_uri`.\n"
        "https://logon7.gov.bc.ca/clp-cgi/logoff.cgi?retnow=1&returl=https://loginproxy.gov.bc.ca"
        "/auth/realms/standard/protocol/openid-connect/logout?post_logout_redirect_uri=https://myapp/logout"
    ),
}]


def _logout_reply(body: str) -> str:
    return body + (
        '\n<grounding>{"response_type": "answer", "grounding_status": "SUPPORTED", '
        f'"citations": ["{LOGOUT_DOC}"]}}</grounding>'
    )


def test_documented_urls_with_placeholder_values_pass():
    body = (
        "Redirect to `https://loginproxy.gov.bc.ca/auth/realms/standard/protocol/openid-connect/logout"
        "?id_token_hint=<id_token>&post_logout_redirect_uri=<your-logout-uri>`.\n\n"
        "For SiteMinder: https://logon7.gov.bc.ca/clp-cgi/logoff.cgi?retnow=1&returl="
        "https://loginproxy.gov.bc.ca/auth/realms/standard/protocol/openid-connect/logout"
        "?post_logout_redirect_uri=<your-logout-uri>"
    )
    verdict = verify(_logout_reply(body), LOGOUT_RETRIEVED, "how do I log out?")
    assert not verdict.rejected, verdict.reasons


@pytest.mark.parametrize("url", [
    # undocumented path
    "https://loginproxy.gov.bc.ca/auth/realms/standard/protocol/openid-connect/revoke?token=<token>",
    # undocumented parameter name
    "https://loginproxy.gov.bc.ca/auth/realms/standard/protocol/openid-connect/logout?client_secret=<secret>",
    # undocumented literal value
    "https://loginproxy.gov.bc.ca/auth/realms/standard/protocol/openid-connect/logout"
    "?post_logout_redirect_uri=https://other.gov.bc.ca/bye",
])
def test_placeholder_urls_still_checked(url):
    verdict = verify(_logout_reply(f"Use {url}"), LOGOUT_RETRIEVED, "how do I log out?")
    assert verdict.rejected
    assert any("undocumented URLs" in r for r in verdict.reasons)


@pytest.mark.parametrize("reply, conversational, expected", [
    ('Which IdP?\n<grounding>{"response_type": "clarification", "citations": []}</grounding>', False, "clarification"),
    ('Hi!\n<grounding>{"response_type": "conversational", "citations": []}</grounding>', False, "conversational"),
    ("Hi! How can I help?", True, "conversational"),
    ("CSS is the SSO app.", False, None),
    (NO_DOCUMENTATION_RESPONSE + '\n<grounding>{"response_type": "answer", "grounding_status": "NOT_SUPPORTED", "citations": []}</grounding>', False, "answer"),
])
def test_verdict_reports_response_type(reply, conversational, expected):
    assert verify(reply, [], "q", conversational=conversational).response_type == expected


# --- general answers (general_auth questions) ---------------------------------

from app.services.grounding import GENERAL_STATUS  # noqa: E402


def _general(body: str, citations=(DOC,)) -> str:
    cites = ", ".join(f'"{c}"' for c in citations)
    return f'{body}\n<grounding>{{"response_type": "general", "citations": [{cites}]}}</grounding>'


GENERAL_BODY = (
    "Refresh token rotation issues a new refresh token on every use and invalidates the old one; "
    "SPAs request `offline_access` only when the provider supports it."
)
BCGOV_PART = f"\n\n**For BC Gov teams:** sign in to [CSS]({CSS}) with your IDIR with MFA account."


def test_general_answer_values_are_not_held_to_the_docs():
    verdict = verify(_general(GENERAL_BODY, citations=()), [], "q", general=True)
    assert not verdict.rejected
    assert verdict.status == GENERAL_STATUS and verdict.response_type == "general"
    assert verdict.text == GENERAL_BODY and verdict.citations == []


def test_general_answer_with_a_cited_bcgov_part():
    verdict = verify(_general(GENERAL_BODY + BCGOV_PART), RETRIEVED, "q", general=True)
    assert not verdict.rejected and verdict.status == GENERAL_STATUS
    assert [c["uri"] for c in verdict.citations] == [DOC]


def test_general_answer_may_not_invent_urls():
    body = GENERAL_BODY + " See https://auth0.com/docs/refresh-tokens."
    verdict = verify(_general(body, citations=()), [], "q", general=True)
    assert verdict.replaced
    assert verdict.reasons == [
        "undocumented URLs (never write URLs from memory): ['https://auth0.com/docs/refresh-tokens']"
    ]


def test_general_answer_bcgov_part_is_held_to_the_docs():
    body = GENERAL_BODY + "\n\n**For BC Gov teams:** set `refresh_token_rotation` to `true` in CSS."
    verdict = verify(_general(body), RETRIEVED, "q", general=True)
    assert verdict.replaced
    assert verdict.reasons == [
        "undocumented values in the For BC Gov teams part: ['refresh_token_rotation', 'true']"
    ]


def test_general_answer_bcgov_part_needs_results_and_a_citation():
    verdict = verify(_general(GENERAL_BODY + BCGOV_PART, citations=()), [], "q", general=True)
    assert "the For BC Gov teams part has no search results to rely on; remove it" in verdict.reasons

    body = GENERAL_BODY + "\n\nFor BC Gov teams: request an integration with your IDIR with MFA account."
    verdict = verify(_general(body, citations=()), RETRIEVED, "q", general=True)
    assert verdict.reasons == ["the For BC Gov teams part cites no search result"]


def test_general_answer_citations_must_come_from_retrieval():
    verdict = verify(_general(GENERAL_BODY, citations=("https://bcgov.github.io/sso-docs/made-up",)),
                     RETRIEVED, "q", general=True)
    assert verdict.replaced and verdict.reasons[0].startswith("citations not returned by retrieval")


def test_general_label_is_not_allowed_for_bcgov_questions():
    verdict = verify(_general(GENERAL_BODY), RETRIEVED, "q", general=False)
    assert verdict.replaced
    assert verdict.reasons[0].startswith("the reply is labelled general")
    assert "undocumented values: ['offline_access']" in verdict.reasons

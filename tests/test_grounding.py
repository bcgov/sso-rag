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
    assert verdict.rejected and "undocumented values" in verdict.reasons[0]


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

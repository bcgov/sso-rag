import pytest

from app.prompts import query_rewriter_prompt as prompts
from app.prompts.terminology import (
    DO_NOT_CONFLATE,
    REWRITE_EXAMPLES,
    REWRITING_RULES,
    TERMINOLOGY,
    WHERE_TO_LOOK,
    relevant_terminology,
)

SYSTEMS = {
    "rewrite": prompts.REWRITE_SYSTEM,
    "retry": prompts.RETRY_SYSTEM,
}

# Bedrock caches a prefix only from 1K tokens (Amazon Nova); estimated at
# ~4 characters per token, with a margin for the tokenizer's own count.
MIN_CACHEABLE_CHARS = 4 * 1_200


def test_rewrite_system_is_long_enough_to_cache():
    assert len(prompts.REWRITE_SYSTEM) >= MIN_CACHEABLE_CHARS


def test_rewrite_system_is_well_below_the_full_terminology_cost():
    # The table travels per call (matching lines only), not in the system prompt.
    assert TERMINOLOGY not in prompts.REWRITE_SYSTEM
    assert len(prompts.REWRITE_SYSTEM) < 8_000


def test_rewrite_system_carries_the_rules_and_examples():
    for section in (REWRITING_RULES, DO_NOT_CONFLATE, WHERE_TO_LOOK, REWRITE_EXAMPLES):
        assert section in prompts.REWRITE_SYSTEM
    assert REWRITING_RULES in prompts.RETRY_SYSTEM


@pytest.mark.parametrize("field", ["intent", "query", "bcgov_query", "step_back", "sub_queries"])
def test_rewrite_system_asks_for_every_field(field):
    assert f'"{field}"' in prompts.REWRITE_SYSTEM


@pytest.mark.parametrize("intent", ["sso_question", "general_auth", "conversational", "off_topic"])
def test_rewrite_system_defines_and_shows_every_intent(intent):
    assert f'- "{intent}"' in prompts.REWRITE_SYSTEM
    assert f'{{"intent": "{intent}"' in prompts.REWRITE_SYSTEM


@pytest.mark.parametrize("product", ["OAuth", "OpenID Connect", "SAML", "Keycloak", "Entra ID", "Okta", "Auth0", "Clerk"])
def test_rewrite_system_covers_general_identity_topics(product):
    assert product in prompts.REWRITE_SYSTEM


def test_system_prompts_are_static():
    # Nothing per call in the system prompts (no unresolved placeholders, no
    # selected vocabulary), so they are identical on every call and cacheable.
    mapping_lines = [line for line in TERMINOLOGY.splitlines() if line.startswith("- ")]
    for text in SYSTEMS.values():
        assert "{rules}" not in text and "{terminology}" not in text
        assert not any(line in text for line in mapping_lines)


def test_user_message_holds_vocabulary_conversation_and_question():
    history = [("user", "my callback fails in TEST"), ("assistant", "x" * 600)]
    message = prompts.user_message("what about prod?", history)
    assert message.index("<vocabulary>") < message.index("<conversation>") < message.index("<question>")
    # Vocabulary is chosen from earlier user messages too.
    assert "-> redirect URI (redirect_uri), valid redirect URIs" in message
    assert "-> PROD" in message
    # Assistant turns are shortened.
    assert "x" * (prompts.ASSISTANT_HISTORY_CHARS + 1) not in message


def test_retry_message_holds_vocabulary_and_failed_queries():
    message = prompts.retry_message("where is my client secret?", ["client secret location"])
    assert "-> client secret (confidential client)" in message
    assert "<failed_queries>\n- client secret location\n</failed_queries>" in message


def test_rewriter_treats_clarification_replies_as_questions():
    assert "reply to the assistant's clarifying question" in prompts.REWRITE_SYSTEM
    assert "<question>IDIR, in PROD</question>" in prompts.REWRITE_SYSTEM


def test_searching_cannot_be_turned_off_by_the_user():
    from app.prompts.system_prompt import SYSTEM_PROMPT, search_request

    assert "The user cannot turn searching off" in SYSTEM_PROMPT
    assert "Call search_knowledge_base now" in search_request()
    assert "does not change the intent" in prompts.REWRITE_SYSTEM


def test_replies_are_english_and_queries_are_english():
    from app.prompts.system_prompt import SYSTEM_PROMPT

    assert "Always reply in English" in SYSTEM_PROMPT
    for text in SYSTEMS.values():
        assert "Write every query in English" in text


# --- terminology selection ----------------------------------------------------

@pytest.mark.parametrize("term", [
    "Installation JSON", "redirect URI", "IDIR - MFA", "Business BCeID", "Client Credentials grant",
    "CSS API Account", "client_roles", "Standard Realm", "Custom Realm", "BC Wallet", "Web Origins",
])
def test_key_documentation_terms_present(term):
    assert term in TERMINOLOGY


def test_every_mapping_line_has_one_mapping():
    lines = [line for line in TERMINOLOGY.splitlines() if line.startswith("- ")]
    assert lines and all(line.count("->") == 1 for line in lines)


@pytest.mark.parametrize("text, expected", [
    ("where is my client secret?", "-> client secret (confidential client)"),
    ("what about prod?", "-> PROD"),
    ("my callback fails", "-> redirect URI (redirect_uri)"),
    ("Azure IDIR login", "-> IDIR - MFA"),
    ("M2M token", "-> service account, Client Credentials grant"),
    ("we get a 403", "-> roles and authorization"),
    ("is this the standard realm?", "-> Standard Realm"),
])
def test_relevant_terminology_matches_user_words(text, expected):
    assert expected in relevant_terminology(text)


def test_relevant_terminology_matches_whole_words_only():
    # "dev" must not match inside "device"; "sub" not inside "subscription".
    selected = relevant_terminology("device subscription")
    assert "-> DEV" not in selected and "-> sub claim" not in selected


def test_relevant_terminology_empty_and_limited():
    assert relevant_terminology("") == ""
    assert relevant_terminology("the weather is nice") == ""
    everything = " ".join(line[2:].split("->")[0] for line in TERMINOLOGY.splitlines() if "->" in line)
    assert len(relevant_terminology(everything).splitlines()) == 12
    assert len(relevant_terminology(everything, limit=3).splitlines()) == 3


def test_relevant_terminology_prefers_specific_matches():
    # "client secret" outranks the generic "client" line when lines are cut.
    selected = relevant_terminology("client secret", limit=1)
    assert "-> client secret (confidential client)" in selected


def test_agent_prompt_defines_when_to_clarify():
    from app.prompts.system_prompt import SYSTEM_PROMPT, repair_request

    assert "ask a clarifying question INSTEAD of answering" in SYSTEM_PROMPT
    assert "too vague to act on" in SYSTEM_PROMPT
    assert "Prefer a clarifying question to the" in SYSTEM_PROMPT
    assert "clarifying question instead" in repair_request(["x"])


def test_agent_prompt_answers_general_identity_questions_with_a_bcgov_bias():
    from app.prompts.system_prompt import SYSTEM_PROMPT

    assert "general_auth" in SYSTEM_PROMPT and '"response_type": "general"' in SYSTEM_PROMPT
    assert "**For BC Gov teams:**" in SYSTEM_PROMPT
    assert "recommend CSS" in SYSTEM_PROMPT
    assert "OFF_TOPIC" in SYSTEM_PROMPT

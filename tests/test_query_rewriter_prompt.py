import pytest

from app.prompts import query_rewriter_prompt as prompts
from app.prompts.terminology import (
    DO_NOT_CONFLATE,
    REWRITE_EXAMPLES,
    REWRITING_RULES,
    TERMINOLOGY,
    WHERE_TO_LOOK,
)

SYSTEMS = {
    "rewrite": prompts.REWRITE_SYSTEM,
    "step_back": prompts.STEP_BACK_SYSTEM,
    "decomposition": prompts.DECOMPOSE_SYSTEM,
    "retry": prompts.RETRY_SYSTEM,
}

# Llama 3 8B has an 8K-token context; leave room for history (up to
# 3 turns x 2 messages x 500 chars), the question and the reply.
MAX_SYSTEM_CHARS = 12_000


@pytest.mark.parametrize("name", SYSTEMS)
def test_system_prompts_fit_the_context_budget(name):
    assert len(SYSTEMS[name]) <= MAX_SYSTEM_CHARS


@pytest.mark.parametrize("name", SYSTEMS)
def test_every_template_carries_the_terminology(name):
    for section in (REWRITING_RULES, TERMINOLOGY, DO_NOT_CONFLATE, WHERE_TO_LOOK):
        assert section in SYSTEMS[name]


def test_rewrite_examples_only_in_rewrite_template():
    assert REWRITE_EXAMPLES in prompts.REWRITE_SYSTEM
    assert all(REWRITE_EXAMPLES not in SYSTEMS[n] for n in ("step_back", "decomposition", "retry"))


@pytest.mark.parametrize("term", [
    "Installation JSON", "redirect URI", "IDIR - MFA", "Business BCeID", "Client Credentials grant",
    "CSS API Account", "client_roles", "Standard Realm", "Custom Realm", "BC Wallet", "Web Origins",
])
def test_key_documentation_terms_present(term):
    assert term in TERMINOLOGY


def test_templates_keep_format_placeholders_resolved():
    for text in SYSTEMS.values():
        assert "{rules}" not in text and "{terminology}" not in text

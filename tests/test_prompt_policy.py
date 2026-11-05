import pytest

from app.services import prompt_policy as pp
from app.services.prompt_policy import PromptRejected, check_prompt, is_links_only


def test_strips_surrounding_whitespace():
    assert check_prompt("  how do I add a redirect URI?\n") == "how do I add a redirect URI?"


@pytest.mark.parametrize("prompt", ["", " ", "\n\t "])
def test_blank_is_rejected(prompt):
    with pytest.raises(PromptRejected, match="Please enter a question"):
        check_prompt(prompt)


def test_limit_is_estimated_tokens_and_configurable(monkeypatch):
    monkeypatch.setattr(pp.settings, "max_prompt_tokens", 200)
    assert pp.max_prompt_chars() == 800
    assert check_prompt("x" * 800) == "x" * 800
    with pytest.raises(PromptRejected, match="about 200 tokens") as raised:
        check_prompt("x" * 801)
    assert "800 characters" in str(raised.value)

    monkeypatch.setattr(pp.settings, "max_prompt_tokens", 10)
    with pytest.raises(PromptRejected):
        check_prompt("x" * 41)


@pytest.mark.parametrize("prompt", [
    "https://sso-requests.apps.gold.devops.gov.bc.ca/",
    "<https://bcgov.github.io/sso-docs/>",
    "https://a.example.com/x https://b.example.com/y",
    "www.gov.bc.ca/sso ?",
])
def test_links_only(prompt):
    assert is_links_only(prompt)


@pytest.mark.parametrize("prompt", [
    "what is https://sso-requests.apps.gold.devops.gov.bc.ca/ for?",
    "https://loginproxy.gov.bc.ca/auth gives a 404",
    "how do I add a redirect URI?",
    "?!",
])
def test_not_links_only(prompt):
    assert not is_links_only(prompt)

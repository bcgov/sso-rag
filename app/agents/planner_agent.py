from strands import Agent

from app.agents.bedrock_model import planner_model, planner_retry_strategy
from app.prompts.system_prompt import GAP_PROMPT, HYDE_PROMPT, PLANNER_PROMPT


def _agent(name: str, prompt: str) -> Agent:
    """A fresh, tool-less, stateless agent: these run once per search and must
    not share message history between requests."""
    return Agent(
        name=name,
        model=planner_model,
        system_prompt=prompt,
        tools=[],
        callback_handler=None,
        retry_strategy=planner_retry_strategy,
    )


def create_planner_agent() -> Agent:
    return _agent("Planner Agent", PLANNER_PROMPT)


def create_gap_agent() -> Agent:
    return _agent("Gap Check Agent", GAP_PROMPT)


def create_hyde_agent() -> Agent:
    return _agent("HyDE Agent", HYDE_PROMPT)

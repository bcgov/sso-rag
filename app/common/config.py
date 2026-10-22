from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = Field(
        alias="ENVIRONMENT",
        default="sandbox"
    )

    aws_region: str = Field(
        default="ca-central-1",
        alias="AWS_REGION"
    )

    knowledge_base_id: str = Field(
        alias="KNOWLEDGE_BASE_ID"
    )

    number_of_results: int = Field(
        default=25,
        alias="NUMBER_OF_RESULTS"
    )

    model_arn: str | None = Field(
        default=None,
        alias="MODEL_ARN"
    )

    reranker_model_arn: str | None = Field(
        default=None,
        alias="RERANKER_MODEL_ARN"
    )

    number_of_reranked_results: int = Field(
        default=6,
        alias="NUMBER_OF_RERANKED_RESULTS"
    )

    cors_origins: list[str] = Field(
        default=["*"],
        alias="CORS_ORIGINS"
    )

    min_rerank_score: float = Field(
        default=0.05,
        alias="MIN_RERANK_SCORE"
    )

    search_type: str = Field(
        default="SEMANTIC",
        alias="SEARCH_TYPE"
    )

    agent_storage_s3_bucket: str = Field(
        alias="AGENT_STORAGE_S3_BUCKET",
        default="sandbox-sso-rag-agent-context-store"
    )

    session_cookie_name: str = Field(
        default="strands_session",
        alias="SESSION_COOKIE_NAME"
    )

    session_cookie_max_age: int = Field(
        default=60 * 60 * 24,
        alias="SESSION_COOKIE_MAX_AGE"
    )

    session_cookie_secure: bool = Field(
        default=True,
        alias="SESSION_COOKIE_SECURE"
    )

    session_retention_days: float = Field(
        default=3,
        gt=0,
        alias="SESSION_RETENTION_DAYS"
    )

    # 0 disables the in-process cleanup job (e.g. when it runs as a separate
    # scheduled task via `python -m app.jobs.cleanup_sessions`).
    session_cleanup_interval_hours: float = Field(
        default=24,
        ge=0,
        alias="SESSION_CLEANUP_INTERVAL_HOURS"
    )

    # Retrieval strategy used by search_knowledge_base:
    #   baseline           - the agent's own queries, one round (previous behaviour)
    #   planner            - planner-rewritten queries, one round
    #   planner_iterative  - planner + gap check + targeted follow-up rounds
    query_strategy: Literal["baseline", "planner", "planner_iterative"] = Field(
        default="planner_iterative",
        alias="QUERY_STRATEGY"
    )

    candidates_per_query: int = Field(
        default=15,
        ge=1,
        le=100,
        alias="CANDIDATES_PER_QUERY"
    )

    max_retrieval_rounds: int = Field(
        default=2,
        ge=1,
        alias="MAX_RETRIEVAL_ROUNDS"
    )

    # Tokens spent by the planner and gap-check LLM calls per search.
    retrieval_token_budget: int = Field(
        default=6000,
        ge=0,
        alias="RETRIEVAL_TOKEN_BUDGET"
    )

    retrieval_latency_budget_ms: int = Field(
        default=12000,
        ge=0,
        alias="RETRIEVAL_LATENCY_BUDGET_MS"
    )

    # A follow-up round that adds fewer new chunks than this to the final
    # reranked set is treated as contributing no meaningful evidence.
    min_new_evidence: int = Field(
        default=1,
        ge=0,
        alias="MIN_NEW_EVIDENCE"
    )

    planner_history_turns: int = Field(
        default=6,
        ge=0,
        alias="PLANNER_HISTORY_TURNS"
    )

    # Adds a hypothetical answer passage as an extra retrieval query. Off
    # unless evals show it improves Recall@K without hurting faithfulness.
    hyde_enabled: bool = Field(
        default=False,
        alias="HYDE_ENABLED"
    )

    # Loop guards. AGENT_MAX_TURNS caps model calls per chat message (each
    # turn is one model call plus the tools it requests);
    # MAX_SEARCHES_PER_TURN caps search_knowledge_base calls per message.
    agent_max_turns: int = Field(
        default=6,
        ge=1,
        alias="AGENT_MAX_TURNS"
    )

    max_searches_per_turn: int = Field(
        default=3,
        ge=1,
        alias="MAX_SEARCHES_PER_TURN"
    )

    # Hard timeout for each planner / gap-check / HyDE call; also bounded by
    # what is left of RETRIEVAL_LATENCY_BUDGET_MS.
    planner_call_timeout_ms: int = Field(
        default=8000,
        ge=100,
        alias="PLANNER_CALL_TIMEOUT_MS"
    )

    # Attempts for the main agent's model calls on throttling/transient errors.
    model_max_attempts: int = Field(
        default=3,
        ge=1,
        alias="MODEL_MAX_ATTEMPTS"
    )

    bedrock_read_timeout_s: int = Field(
        default=60,
        ge=1,
        alias="BEDROCK_READ_TIMEOUT_S"
    )


settings = Settings()
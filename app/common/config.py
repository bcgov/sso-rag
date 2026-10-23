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

    # Output-token limit per agent model call. Bedrock's default varies by
    # model (Nova 2 is dynamic), so set it explicitly; a reply cut off at
    # this limit gets one repair attempt asking for a shorter answer.
    model_max_tokens: int = Field(
        default=4096,
        gt=0,
        alias="MODEL_MAX_TOKENS"
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

    # Query rewriter (app/services/query_rewriter.py): a small model that
    # turns the user's message into the search queries the agent's
    # search_knowledge_base tool runs.
    query_rewriter_model_id: str = Field(
        default="meta.llama3-8b-instruct-v1:0",
        alias="QUERY_REWRITER_MODEL_ID"
    )

    query_rewriter_timeout_seconds: float = Field(
        default=3,
        gt=0,
        alias="QUERY_REWRITER_TIMEOUT_SECONDS"
    )

    query_rewriter_history_turns: int = Field(
        default=3,
        ge=0,
        alias="QUERY_REWRITER_HISTORY_TURNS"
    )

    query_rewriter_step_back_enabled: bool = Field(
        default=True,
        alias="QUERY_REWRITER_STEP_BACK_ENABLED"
    )

    query_rewriter_decomposition_enabled: bool = Field(
        default=True,
        alias="QUERY_REWRITER_DECOMPOSITION_ENABLED"
    )

    query_rewriter_retry_enabled: bool = Field(
        default=True,
        alias="QUERY_REWRITER_RETRY_ENABLED"
    )

    # When an answer fails the grounding checks but the search returned
    # results, send the reasons back to the agent once for a corrected reply.
    grounding_repair_enabled: bool = Field(
        default=True,
        alias="GROUNDING_REPAIR_ENABLED"
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


settings = Settings()
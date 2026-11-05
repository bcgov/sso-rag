from urllib.parse import urlsplit

from pydantic import Field, model_validator
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

    # Local-only trace logs (app/common/trace.py): rewritten queries, search
    # results and the agent's raw reply before the grounding check. Has no
    # effect unless ENVIRONMENT=local.
    debug_trace: bool = Field(
        default=True,
        alias="DEBUG_TRACE"
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
    # Reasoning models' (e.g. Kimi K3) reasoning tokens count towards it:
    # check the `model_call` log lines' output_tokens before lowering it.
    model_max_tokens: int = Field(
        default=4096,
        gt=0,
        alias="MODEL_MAX_TOKENS"
    )

    # Messages of conversation history kept (sliding window, applied after
    # each request). Earlier turns' search calls and results are dropped when
    # a conversation is restored, so 20 is about the last 10 questions and
    # answers.
    conversation_window_messages: int = Field(
        default=10,
        ge=2,
        alias="CONVERSATION_WINDOW_MESSAGES"
    )

    # Longest accepted user message, in tokens estimated at ~4 characters
    # each (the deployed model's tokenizer isn't available locally), so the
    # default allows about 800 characters. The UI reads it from
    # GET /chat/config.
    max_prompt_tokens: int = Field(
        default=200,
        gt=0,
        alias="MAX_PROMPT_TOKENS"
    )

    # Wall-clock budget for one /chat turn. Keep it under the API Gateway
    # integration timeout (300s) so the user gets an in-band "took too long"
    # error rather than a cut-off stream.
    turn_timeout_seconds: float = Field(
        default=240,
        gt=0,
        alias="TURN_TIMEOUT_SECONDS"
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

    # Keycloak service-account authentication (app/services/authentication.py).
    # Callers send `Authorization: Bearer <access token>`; the token's
    # signature (RS256, realm JWKS), iss, aud, azp and exp are verified.
    keycloak_issuer_url: str | None = Field(
        default=None,
        alias="KEYCLOAK_ISSUER_URL"
    )

    keycloak_audience: str | None = Field(
        default=None,
        alias="KEYCLOAK_AUDIENCE"
    )

    keycloak_authorized_party: str | None = Field(
        default=None,
        alias="KEYCLOAK_AUTHORIZED_PARTY"
    )

    # Skips authentication. Only allowed with ENVIRONMENT=local.
    auth_local_bypass: bool = Field(
        default=False,
        alias="AUTH_LOCAL_BYPASS"
    )

    @property
    def is_local(self) -> bool:
        return self.environment == "local"

    @property
    def auth_bypassed(self) -> bool:
        return self.auth_local_bypass and self.is_local

    @model_validator(mode="after")
    def _check_auth(self) -> "Settings":
        if self.auth_local_bypass and not self.is_local:
            raise ValueError("AUTH_LOCAL_BYPASS is only allowed when ENVIRONMENT=local")

        for name in ("keycloak_issuer_url", "keycloak_audience", "keycloak_authorized_party"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, value.strip() or None)

        if self.keycloak_issuer_url:
            self.keycloak_issuer_url = _validated_issuer(self.keycloak_issuer_url)

        return self

    def require_auth_config(self) -> None:
        """Called at API startup; CLI jobs that serve no requests skip it."""
        if self.auth_bypassed:
            return
        missing = [
            name for name, value in (
                ("KEYCLOAK_ISSUER_URL", self.keycloak_issuer_url),
                ("KEYCLOAK_AUDIENCE", self.keycloak_audience),
                ("KEYCLOAK_AUTHORIZED_PARTY", self.keycloak_authorized_party),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} must be set (or AUTH_LOCAL_BYPASS=true with ENVIRONMENT=local)"
            )


def _validated_issuer(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "/realms/" not in parts.path
    ):
        raise ValueError(
            "KEYCLOAK_ISSUER_URL must be an https realm URL such as "
            "https://<host>/auth/realms/<realm>, without credentials, query or fragment"
        )
    return url.rstrip("/")


settings = Settings()
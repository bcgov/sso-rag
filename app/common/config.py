from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    aws_region: str = Field(
        default="ca-central-1",
        alias="AWS_REGION"
    )

    knowledge_base_id: str = Field(
        alias="KNOWLEDGE_BASE_ID"
    )

    number_of_results: int = Field(
        default=5,
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
        default=3,
        alias="NUMBER_OF_RERANKED_RESULTS"
    )

    cors_origins: list[str] = Field(
        default=["*"],
        alias="CORS_ORIGINS"
    )


settings = Settings()
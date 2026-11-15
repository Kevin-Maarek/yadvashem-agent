from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


Provider = Literal["ollama", "openai", "openrouter"]


class Settings(BaseSettings):
    # Process environment overrides .env, which overrides the defaults below.
    # Resolve paths from this module so running from backend/ also works.
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8", extra="ignore", populate_by_name=True, hide_input_in_errors=True,
    )

    pghost: str = "localhost"
    pgport: int = 5432
    pgdatabase: str = Field("archive", validation_alias=AliasChoices("PGDATABASE", "POSTGRES_DB"))
    pguser: str = Field("archive", validation_alias=AliasChoices("PGUSER", "POSTGRES_USER"))
    pgpassword: str = Field("archive-local", validation_alias=AliasChoices("PGPASSWORD", "POSTGRES_PASSWORD"))
    ai_provider: Provider = "ollama"
    embedding_provider: Provider = "ollama"
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    api_timeout_seconds: int = Field(150, ge=10, le=600)
    # Change to invalidate cached API outputs if a provider updates an alias.
    api_model_revision: str = "1"
    ollama_url: str = "http://localhost:11434"
    vision_model: str = "qwen3.8:latest"
    vision_output_tokens: int = Field(5200, ge=512, le=8192)
    chat_model: str = "qwen3.8:latest"
    embedding_model: str = "qwen3-embedding:latest"
    embedding_dimensions: int = Field(4096, ge=1, le=16000)
    ollama_timeout_seconds: int = Field(150, ge=10, le=600)
    source_delay_seconds: float = Field(1, ge=0.2)
    max_attempts: int = Field(3, ge=1, le=10)
    retry_base_seconds: float = Field(2, ge=0.5)
    search_limit: int = Field(5, ge=1, le=15)
    search_threshold: float = Field(0.85, ge=0, le=1)

    @model_validator(mode="after")
    def provider_credentials(self):
        if (self.ai_provider != self.embedding_provider
                and self.embedding_model in (self.chat_model, self.vision_model)):
            raise ValueError("Embedding and generation model names must differ when their providers differ")
        for provider in {self.ai_provider, self.embedding_provider} - {"ollama"}:
            if not getattr(self, f"{provider}_api_key").get_secret_value().strip():
                raise ValueError(f"{provider.upper()}_API_KEY is required for the selected provider")
        return self

    @property
    def model_timeout_seconds(self) -> int:
        timeouts = [self.ollama_timeout_seconds if provider == "ollama" else self.api_timeout_seconds
                    for provider in (self.ai_provider, self.embedding_provider)]
        return max(timeouts)

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.pguser,
            password=self.pgpassword,
            host=self.pghost,
            port=self.pgport,
            database=self.pgdatabase,
        )

settings = Settings()

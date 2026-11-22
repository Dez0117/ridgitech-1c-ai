from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Ridgitech 1C AI Backend"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://ridgitech:ridgitech@localhost:5432/ridgitech"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    agent_timeout_seconds: float = Field(default=60, gt=0, le=60)
    agent_history_messages: int = Field(default=20, ge=0, le=100)
    cors_origins: str = "http://localhost:3000"
    jwt_secret: SecretStr = SecretStr("")
    jwt_session_minutes: int = Field(default=720, ge=5, le=1440)
    chat_organization_id: str = Field(default="demo-org", min_length=1, max_length=128)
    analytics_backend: Literal["fixture", "onec"] = "fixture"
    onec_data_url: str = "http://127.0.0.1:8011"
    onec_data_token: str = ""
    onec_allow_demo_metrics: bool = False

    @model_validator(mode="after")
    def validate_jwt_secret(self):
        secret = self.jwt_secret.get_secret_value()
        if (secret or self.environment != "development") and len(secret.encode()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

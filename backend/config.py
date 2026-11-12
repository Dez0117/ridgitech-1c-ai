from functools import lru_cache

from pydantic import Field
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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DataSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    odata_base_url: str
    odata_username: str
    odata_password: SecretStr
    onec_data_token: SecretStr = SecretStr("")
    onec_base_id: str = "itmo"
    onec_organization_ids: str = ""
    odata_page_size: int = Field(default=100, ge=1, le=1000)
    odata_max_rows: int = Field(default=5000, ge=1, le=100000)
    odata_timeout_seconds: float = Field(default=15, gt=0, le=60)
    odata_connect_timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    odata_read_timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    odata_write_timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    odata_pool_timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    odata_max_retries: int = Field(default=1, ge=0, le=3)
    odata_retry_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    odata_max_connections: int = Field(default=4, ge=1, le=20)
    odata_requests_per_second: float = Field(default=2, gt=0, le=50)
    onec_catalog_ttl_seconds: float = Field(default=60, gt=0, le=3600)
    onec_metadata_ttl_seconds: float = Field(default=900, gt=0, le=86400)
    onec_metadata_refresh_seconds: float = Field(default=86400, ge=0, le=2592000)
    onec_metadata_state_dir: Path | None = Path("runs/onec-metadata")
    onec_dimension_search_limit: int = Field(default=20, ge=1, le=100)

    @field_validator("odata_base_url")
    @classmethod
    def validate_url(cls, value):
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Требуется адрес HTTPS без учётных данных и параметров")
        return value.rstrip("/")

    @field_validator("odata_username", "odata_password")
    @classmethod
    def nonempty(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw:
            raise ValueError("Учётные данные 1С обязательны")
        return value

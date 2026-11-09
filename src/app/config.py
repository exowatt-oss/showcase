from typing import ClassVar

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file.

    The database limits are deliberately finite.  The PostgreSQL-only values are
    applied by :func:`app.db._build_engine`; SQLite receives only its
    cross-thread connection option.
    """

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:8000"]
    # Standalone local SQLite; configure PostgreSQL for multi-instance deployments.
    DATABASE_URL: str = "sqlite:///./showcase.db"

    DB_CONNECT_TIMEOUT_SECONDS: int = 3
    DB_STATEMENT_TIMEOUT_MS: int = 5000
    DB_LOCK_TIMEOUT_MS: int = 1000
    DB_IDLE_IN_TRANSACTION_TIMEOUT_MS: int = 10000
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 0
    DB_POOL_TIMEOUT_SECONDS: int = 2
    DB_POOL_RECYCLE_SECONDS: int = 1800
    DB_MAX_WORKERS: int = 5
    DB_OPERATION_TIMEOUT_SECONDS: int = 8
    DB_INIT_RETRY_INTERVAL_SECONDS: int = 15
    DB_SHUTDOWN_TIMEOUT_SECONDS: int = 10

    # Guard individual writes. Also set reverse-proxy rate, disk and request limits.
    MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024
    MAX_REQUEST_BYTES: int = 70 * 1024 * 1024

    _POSITIVE_FIELDS: ClassVar[tuple[str, ...]] = (
        "MAX_UPLOAD_BYTES",
        "MAX_REQUEST_BYTES",
        "DB_CONNECT_TIMEOUT_SECONDS",
        "DB_STATEMENT_TIMEOUT_MS",
        "DB_LOCK_TIMEOUT_MS",
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        "DB_POOL_SIZE",
        "DB_POOL_TIMEOUT_SECONDS",
        "DB_POOL_RECYCLE_SECONDS",
        "DB_MAX_WORKERS",
        "DB_OPERATION_TIMEOUT_SECONDS",
        "DB_INIT_RETRY_INTERVAL_SECONDS",
        "DB_SHUTDOWN_TIMEOUT_SECONDS",
    )

    @field_validator(*_POSITIVE_FIELDS)
    @classmethod
    def _finite_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("database bounds must be positive")
        return value

    @field_validator("DB_MAX_OVERFLOW")
    @classmethod
    def _finite_overflow(cls, value: int) -> int:
        if value < 0:
            raise ValueError("DB_MAX_OVERFLOW must be non-negative")
        return value

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

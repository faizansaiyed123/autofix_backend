"""Application configuration management.

Uses Pydantic Settings to load and validate environment variables.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env path relative to the project root (three levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_ENV_PATHS = [".env", str(_PROJECT_ROOT / ".env")]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_PATHS,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8000

    # Database
    DATABASE_URL: str

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT Authentication
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Password hashing
    BCRYPT_ROUNDS: int = 12

    # CORS
    ALLOWED_ORIGINS: list[str] = ["*"]

    # File uploads
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_SIZE_MB: int = 10

    # Company settings
    COMPANY_NAME: str = "AutoFix Garage"
    COMPANY_CURRENCY: str = "USD"
    COMPANY_TAX_RATE: float = 0.0

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


settings = Settings()

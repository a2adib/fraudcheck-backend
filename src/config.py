import base64
import os
from typing import Any

from pydantic import PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.constants import Environment

AES_256_KEY_BYTES = 32


class CustomBaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


class Config(CustomBaseSettings):
    ENVIRONMENT: Environment = Environment.PRODUCTION
    DEBUG: bool = False

    # FR-11. Swaps every real courier adapter for a deterministic mock at registry
    # level, so a reviewer can run the whole service with no credentials at all.
    MOCK_MODE: bool = False

    # CORS
    CORS_ORIGINS: list[str] = ["*"]
    CORS_ORIGINS_REGEX: str | None = None
    CORS_HEADERS: list[str] = ["*"]

    # DATABASE
    DATABASE_URL: PostgresDsn
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 5
    DATABASE_POOL_PRE_PING: bool = True
    DATABASE_POOL_TTL: int = 3600

    # REDIS
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    CACHE_TIME_OUT: int = 300

    # JWT
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CREDENTIAL VAULT (FR-2)
    # AES-256-GCM key: exactly 32 bytes, urlsafe-base64 encoded.
    ENCRYPTION_KEY: str
    ENCRYPTION_KEY_VERSION: int = 1

    # EMAIL (FR-1.10 OTP delivery)
    # With SMTP_HOST unset the sender falls back to logging — allowed only on a
    # non-deployed environment, enforced by _validate_email_transport below.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str = "no-reply@fraudcheck.test"
    SMTP_FROM_NAME: str = "Fraud Checker BD"
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT_SECONDS: float = 10.0

    # AUTH POLICY
    MAX_FAILED_LOGIN_ATTEMPTS: int = 5
    ACCOUNT_LOCKOUT_MINUTES: int = 15
    OTP_EXPIRE_MINUTES: int = 15
    OTP_RETRY_LIMIT: int = 3
    OTP_RETRY_DELAY_MINUTES: int = 2
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 10

    # Cookies (auth token delivery)
    COOKIE_DOMAIN: str | None = None

    # COURIER PORTALS
    PATHAO_MERCHANT_URL: str = "https://merchant.pathao.com"
    REDX_BASE_URL: str = "https://redx.com.bd"
    STEADFAST_BASE_URL: str = "https://portal.packzy.com"
    PROVIDER_TIMEOUT_SECONDS: float = 5.0

    # TOKEN MANAGER (FR-4) — ported from govaly's PathaoTokenManager, which hardcoded these.
    TOKEN_SAFETY_MARGIN_SECONDS: int = 60
    TOKEN_GRACE_PERIOD_SECONDS: int = 300
    TOKEN_LOCK_TIMEOUT_MS: int = 10_000
    TOKEN_LOCK_POLL_INTERVAL: float = 0.1
    TOKEN_LOCK_MAX_POLL_SECONDS: float = 5.0

    # CIRCUIT BREAKER (FR-5)
    BREAKER_FAILURE_THRESHOLD: int = 5
    BREAKER_WINDOW_SECONDS: int = 60
    BREAKER_OPEN_SECONDS: int = 30

    # CHECK CACHE (FR-6.8)
    CHECK_CACHE_TTL_SECONDS: int = 900

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def _validate_encryption_key(cls, value: str) -> str:
        """
        Fail at startup, not at first encrypt.

        A short or malformed key would otherwise surface as a runtime error the
        first time a merchant saves a credential.
        """
        try:
            raw = base64.urlsafe_b64decode(value)
        except Exception as exc:
            msg = "ENCRYPTION_KEY must be urlsafe-base64 encoded"
            raise ValueError(msg) from exc
        if len(raw) != AES_256_KEY_BYTES:
            msg = f"ENCRYPTION_KEY must decode to exactly {AES_256_KEY_BYTES} bytes for AES-256-GCM"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _validate_email_transport(self) -> "Config":
        """
        Refuse to deploy without a real mail transport.

        The fallback sender writes the OTP to the log. That is a convenience for local
        development and a credential leak anywhere else, so a deployed environment must
        configure SMTP rather than discover this at the first password reset.
        """
        if self.ENVIRONMENT.is_deployed and not self.SMTP_HOST:
            msg = "SMTP_HOST is required on a deployed environment — OTP email cannot be logged"
            raise ValueError(msg)
        return self

    @property
    def encryption_key_bytes(self) -> bytes:
        """The decoded AES-256-GCM key. Validated at startup."""
        return base64.urlsafe_b64decode(self.ENCRYPTION_KEY)

    @property
    def cookie_secure(self) -> bool:
        """Send the Secure cookie flag only on deployed (HTTPS) environments."""
        return self.ENVIRONMENT.is_deployed


settings = Config()

app_configs: dict[str, Any] = {"title": "Fraud Checker BD API"}
if not settings.ENVIRONMENT.is_debug:
    app_configs["openapi_url"] = None  # hide docs in production

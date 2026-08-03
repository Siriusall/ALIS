import os

from pydantic import BaseModel

from app.core.project_env import load_project_env
from app.core.runtime_guard import assert_database_boundary

load_project_env()

_ALLOWED_ENVIRONMENTS = {"dev", "development", "test", "testing", "prod", "production"}


class Settings(BaseModel):
    app_name: str = "Quantification Server"
    # Do not silently treat an omitted deployment mode as development.
    env: str = os.getenv("APP_ENV", "").strip().lower()
    local_debug_auth_bypass: bool = os.getenv("LOCAL_DEBUG_AUTH_BYPASS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    log_limit: int = 200
    jwt_secret: str = os.getenv("JWT_SECRET", "quant-dev-secret-change-before-production")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 天
    # 生产数据库必须使用 PostgreSQL；SQLite 不再作为默认回退。
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://sirius@127.0.0.1:5432/alis_sim"
    )
    database_connect_timeout_seconds: int = int(os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "5"))
    database_statement_timeout_ms: int = int(os.getenv("DATABASE_STATEMENT_TIMEOUT_MS", "30000"))
    background_runtime_mode: str = os.getenv("ALIS_BACKGROUND_RUNTIME", "enabled").strip().lower()
    cors_origins: str = os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    )

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()

if not settings.env:
    raise RuntimeError("APP_ENV must be explicitly configured; use production for the formal runtime.")

if settings.env not in _ALLOWED_ENVIRONMENTS:
    raise RuntimeError(
        "APP_ENV must be one of: dev, development, test, testing, prod, production."
    )

if settings.background_runtime_mode not in {"enabled", "disabled"}:
    raise RuntimeError("ALIS_BACKGROUND_RUNTIME must be either 'enabled' or 'disabled'.")

if not settings.database_url.lower().startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
    raise RuntimeError("DATABASE_URL must use PostgreSQL; other runtime databases are not supported.")

assert_database_boundary(settings.database_url, role="business")

if settings.env in {"prod", "production"}:
    if settings.jwt_secret == "quant-dev-secret-change-before-production":
        raise RuntimeError("JWT_SECRET must be explicitly configured in production.")
    if settings.local_debug_auth_bypass:
        raise RuntimeError("LOCAL_DEBUG_AUTH_BYPASS must be false in production.")

import os

from pydantic import BaseModel

from app.core.project_env import load_project_env

load_project_env()


class Settings(BaseModel):
    app_name: str = "Quantification Server"
    env: str = "dev"
    log_limit: int = 200
    jwt_secret: str = os.getenv("JWT_SECRET", "quant-dev-secret-change-before-production")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 天
    # 生产数据库必须使用 PostgreSQL；SQLite 不再作为默认回退。
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://alis_app@127.0.0.1:5432/alis_sim"
    )
    database_connect_timeout_seconds: int = int(os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "5"))
    cors_origins: str = os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    )

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()

if not settings.database_url.lower().startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
    raise RuntimeError("DATABASE_URL must use PostgreSQL; other runtime databases are not supported.")

if settings.env.lower() in {"prod", "production"} and settings.jwt_secret == "quant-dev-secret-change-before-production":
    raise RuntimeError("JWT_SECRET must be explicitly configured in production.")

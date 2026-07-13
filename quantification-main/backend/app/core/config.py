import os

from pydantic import BaseModel

from app.core.project_env import load_project_env

load_project_env()


class Settings(BaseModel):
    app_name: str = "Quantification Server"
    env: str = "dev"
    log_limit: int = 200
    jwt_secret: str = "quant-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 天
    # 生产数据库必须使用 PostgreSQL；SQLite 不再作为默认回退。
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://sirius@127.0.0.1:5432/alis_sim"
    )


settings = Settings()

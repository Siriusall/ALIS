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
    # 数据库：默认 SQLite；可通过环境变量 DATABASE_URL 覆盖
    # 示例：sqlite:///.cache/quant.db 或 sqlite:////abs/path/quant.db
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///.cache/quant.db")


settings = Settings()

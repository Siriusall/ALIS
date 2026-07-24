from pathlib import Path

from app.core import project_env


def test_project_env_uses_dotenv_when_inherited_value_is_empty(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TUSHARE_TOKEN=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("TUSHARE_TOKEN", "")
    monkeypatch.setattr(project_env, "get_env_file_paths", lambda: [Path(env_file)])

    project_env.load_project_env()

    assert project_env.get_env_value("TUSHARE_TOKEN") == "from-dotenv"


def test_project_env_keeps_nonempty_inherited_value(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TUSHARE_TOKEN=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("TUSHARE_TOKEN", "from-launcher")
    monkeypatch.setattr(project_env, "get_env_file_paths", lambda: [Path(env_file)])

    project_env.load_project_env()

    assert project_env.get_env_value("TUSHARE_TOKEN") == "from-launcher"

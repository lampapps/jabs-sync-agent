import shutil
from pathlib import Path

import pytest

from app import settings

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_VARS = ("JABS_DASHBOARD_URL", "JABS_AGENT_KEY", "UPTIME_KUMA_URL", "RSYNC_BIN", "JABS_SMTP_USERNAME",
            "JABS_SMTP_PASSWORD", "JABS_SMTP_USER", "JABS_SMTP_PASS", "ENV_MODE", "JABS_HOME")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A throwaway install directory; the real config, .env and state are never touched."""
    (tmp_path / "config" / "jobs").mkdir(parents=True)
    paths = {
        "BASE_DIR": tmp_path, "CONFIG_DIR": tmp_path / "config", "CONFIG_PATH": tmp_path / "config" / "config.yaml",
        "JOBS_DIR": tmp_path / "config" / "jobs", "COMMON_EXCLUDE_PATH": tmp_path / "config" / "common_exclude.yaml",
        "DATA_DIR": tmp_path / "data", "LOCK_DIR": tmp_path / "data" / "locks",
        "STATE_PATH": tmp_path / "data" / "state.json", "ENV_PATH": tmp_path / ".env",
    }
    for name, value in paths.items():
        monkeypatch.setattr(settings, name, value)
    return tmp_path


def write_config(home: Path, text: str = "") -> None:
    (home / "config" / "config.yaml").write_text(text, encoding="utf-8")


def write_job(home: Path, name: str = "media", body: str = "") -> Path:
    path = home / "config" / "jobs" / f"{name}.yaml"
    path.write_text(body or f'job_name: "{name}"\npairs:\n  - source: /data/in\n    destination: /data/out\n',
                    encoding="utf-8")
    return path


requires_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")

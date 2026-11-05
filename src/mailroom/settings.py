"""Filesystem layout and runtime settings. No secrets live here."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings


def _default_root() -> Path:
    # Repo root = two levels up from this file's package dir.
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    root: Path = _default_root()
    host: str = "127.0.0.1"
    port: int = 8765

    # Keychain service name; every secret is stored under (service, account).
    keyring_service: str = "inbox-pilot"

    # OAuth loopback port used by the Google installed-app flow.
    oauth_port: int = 8766

    model_config = {"env_prefix": "MAILROOM_", "extra": "ignore"}

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        d = Path(os.environ.get("MAILROOM_DATA_DIR", self.root / "data"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "inboxpilot.sqlite3"

    @property
    def web_dir(self) -> Path:
        return self.root / "web"

    @property
    def rules_path(self) -> Path:
        p = self.config_dir / "rules.yaml"
        return p if p.exists() else self.config_dir / "rules.example.yaml"

    @property
    def jobs_path(self) -> Path:
        p = self.config_dir / "jobs.yaml"
        return p if p.exists() else self.config_dir / "jobs.example.yaml"


settings = Settings()

"""Paths, settings and secrets for scrapy-awesome.

Everything lives under a per-user data directory (platformdirs). Nothing here talks to the
network. Secrets are resolved through a chain: OS keyring -> environment variable -> 0600 file,
because keyring is unavailable on headless Linux/WSL/Docker and nags on macOS under `uvx`.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from platformdirs import PlatformDirs
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "scrapy-awesome"
_DIRS = PlatformDirs(appname=APP_NAME, appauthor=False)


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Paths:
    """Filesystem layout. Override the root with SCRAPY_AWESOME_HOME (tests, portable installs)."""

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "scrapy-awesome.sqlite3"

    @property
    def server_json(self) -> Path:
        return self.root / "server.json"

    @property
    def secrets_file(self) -> Path:
        return self.root / "secrets.json"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def httpcache(self) -> Path:
        return self.cache / "httpcache"

    @property
    def snapshots(self) -> Path:
        return self.cache / "snapshots"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def browsers(self) -> Path:
        return self.root / "browsers"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> Paths:
        for p in (
            self.root,
            self.runs,
            self.cache,
            self.httpcache,
            self.snapshots,
            self.sessions,
            self.browsers,
            self.exports,
            self.logs,
        ):
            p.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, stat.S_IRWXU)
        return self


def get_paths() -> Paths:
    override = os.environ.get("SCRAPY_AWESOME_HOME")
    root = Path(override).expanduser() if override else Path(_DIRS.user_data_dir)
    return Paths(root=root)


# --------------------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------------------
SecretName = Literal["anthropic_api_key", "gemini_api_key"]
SECRET_ENV: dict[str, str] = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
}
SecretSource = Literal["keyring", "env", "file", "none"]


class SecretStore:
    """keyring -> env -> 0600 file. `get` returns (value, source)."""

    service = APP_NAME

    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or get_paths()

    # -- keyring -----------------------------------------------------------------------
    def _keyring_get(self, name: str) -> str | None:
        try:
            import keyring
            from keyring.errors import KeyringError

            try:
                return keyring.get_password(self.service, name)
            except KeyringError:
                return None
        except Exception:  # keyring missing / no backend
            return None

    def _keyring_set(self, name: str, value: str) -> bool:
        try:
            import keyring
            from keyring.errors import KeyringError

            try:
                keyring.set_password(self.service, name, value)
                return True
            except KeyringError:
                return False
        except Exception:
            return False

    def _keyring_delete(self, name: str) -> None:
        try:
            import keyring

            keyring.delete_password(self.service, name)
        except Exception:
            pass

    # -- file --------------------------------------------------------------------------
    def _file_read(self) -> dict[str, str]:
        p = self.paths.secrets_file
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            return {}

    def _file_write(self, data: dict[str, str]) -> None:
        self.paths.ensure()
        p = self.paths.secrets_file
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(p)

    # -- public ------------------------------------------------------------------------
    def get(self, name: SecretName) -> tuple[str | None, SecretSource]:
        v = self._keyring_get(name)
        if v:
            return v, "keyring"
        env = os.environ.get(SECRET_ENV[name])
        if env:
            return env, "env"
        v = self._file_read().get(name)
        if v:
            return v, "file"
        return None, "none"

    def set(self, name: SecretName, value: str) -> SecretSource:
        if self._keyring_set(name, value):
            # keep file copy out of the way if keyring works
            data = self._file_read()
            if name in data:
                del data[name]
                self._file_write(data)
            return "keyring"
        data = self._file_read()
        data[name] = value
        self._file_write(data)
        return "file"

    def delete(self, name: SecretName) -> None:
        self._keyring_delete(name)
        data = self._file_read()
        if name in data:
            del data[name]
            self._file_write(data)

    def backend_name(self) -> str:
        try:
            import keyring

            return type(keyring.get_keyring()).__name__
        except Exception:
            return "unavailable"


# --------------------------------------------------------------------------------------
# User settings (persisted as JSON; env overrides with SCRAPY_AWESOME_ prefix)
# --------------------------------------------------------------------------------------
Provider = Literal["anthropic", "gemini", "claude_code"]


class RoleConfig(BaseModel):
    provider: Provider = "anthropic"
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"


class LLMSettings(BaseModel):
    designer: RoleConfig = Field(default_factory=lambda: RoleConfig(effort="high"))
    fallback: RoleConfig = Field(default_factory=lambda: RoleConfig(effort="low"))
    session_budget_usd: float = 2.0
    default_run_llm_budget_usd: float = 1.0
    # Gray-zone, off by default. See docs/auth-modes.md.
    cli_login_enabled: bool = False


class CrawlSettings(BaseModel):
    obey_robots: bool = True
    default_download_delay: float = 0.5
    default_concurrency_per_domain: int = 4
    autothrottle: bool = True
    httpcache_ttl_seconds: int = 60 * 60 * 24 * 7
    chrome_executable_path: str | None = None  # scrapy-stealth BROWSER_EXECUTABLE_PATH
    proxies: list[str] = Field(default_factory=list)


class RetentionSettings(BaseModel):
    """Storage caps applied by the daily prune (and Settings → Prune now)."""

    keep_runs_per_recipe: int = 30  # finished/failed/stopped runs kept per recipe (newest first)
    keep_samples_per_recipe: int = 12  # cached pages per recipe
    keep_days: int = 90  # anything older than this (runs, orphan pages) is pruned
    notifications: bool = True  # desktop/browser notifications for finished scheduled runs


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 0  # 0 = random free port
    open_browser: bool = True
    idle_exit_seconds: int | None = None


class UserSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCRAPY_AWESOME_", env_nested_delimiter="__")

    llm: LLMSettings = Field(default_factory=LLMSettings)
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    max_concurrent_runs: int = 2

    @classmethod
    def load(cls, paths: Paths | None = None) -> UserSettings:
        paths = paths or get_paths()
        data: dict = {}
        if paths.settings_file.exists():
            try:
                data = json.loads(paths.settings_file.read_text() or "{}")
            except json.JSONDecodeError:
                data = {}
        return cls(**data)

    def save(self, paths: Paths | None = None) -> None:
        paths = (paths or get_paths()).ensure()
        tmp = paths.settings_file.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2))
        tmp.replace(paths.settings_file)


DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-5",
    "gemini": "gemini-3.7-flash",
    "claude_code": "claude-opus-5",
}

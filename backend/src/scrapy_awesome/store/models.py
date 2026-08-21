"""SQLModel tables."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset.

    We store aware-UTC datetimes but SQLite hands them back naive; without the
    offset browsers parse them as *local* time and every "elapsed"/"ago" is off
    by the user's UTC offset.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


class RecipeRow(SQLModel, table=True):
    __tablename__ = "recipes"

    id: str = Field(primary_key=True)
    name: str = Field(index=True)
    version: int = 1
    data: dict[str, Any] = Field(sa_column=Column(JSON))
    # derived at validation time (not versioned): per-field element fingerprints for self-heal
    fingerprints: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_run_id: str | None = None
    archived: bool = False


class RecipeVersionRow(SQLModel, table=True):
    __tablename__ = "recipe_versions"

    id: int | None = Field(default=None, primary_key=True)
    recipe_id: str = Field(index=True, foreign_key="recipes.id")
    version: int
    data: dict[str, Any] = Field(sa_column=Column(JSON))
    note: str = ""
    created_at: datetime = Field(default_factory=_now)


class RunRow(SQLModel, table=True):
    __tablename__ = "runs"

    id: str = Field(primary_key=True)
    recipe_id: str | None = Field(default=None, index=True)
    recipe_version: int | None = None
    recipe_name: str = ""
    kind: str = "crawl"  # crawl | preview | snapshot
    status: str = "queued"  # queued | running | stopping | stopped | finished | failed | cancelled
    reason: str | None = None
    run_dir: str = ""
    pid: int | None = None
    token: str = ""
    limits: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    items: int = 0
    pages: int = 0
    blocked: int = 0
    escalations: int = 0
    error: str | None = None
    schedule_id: str | None = Field(default=None, index=True)  # set when a schedule started it
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ScheduleRow(SQLModel, table=True):
    """Run a recipe on a cron or interval; results are diffed against the previous run."""

    __tablename__ = "schedules"

    id: str = Field(primary_key=True)
    recipe_id: str = Field(index=True)
    name: str = ""
    kind: str = "cron"  # cron | interval
    cron: str | None = None  # 5-field cron expression (local time)
    every_seconds: int | None = None
    timezone: str | None = None  # IANA name; None → local
    enabled: bool = True
    max_pages: int | None = None
    max_items: int | None = None
    notify: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_id: str | None = None
    last_status: str | None = None
    last_diff: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class FailedPageRow(SQLModel, table=True):
    """A page the crawler fetched but could not extract; kept for LLM/agent fallback."""

    __tablename__ = "failed_pages"

    id: str = Field(primary_key=True)
    run_id: str = Field(index=True)
    url: str = ""
    kind: str = "list"  # list | detail
    reason: str = ""
    html_path: str = ""
    tier: str | None = None
    base_row: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    status: str = "pending"  # pending | recovered | skipped | failed
    rows_added: int = 0
    provider: str | None = None
    cost_usd: float = 0.0
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ItemRow(SQLModel, table=True):
    __tablename__ = "items"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True, foreign_key="runs.id")
    n: int = Field(index=True)
    data: dict[str, Any] = Field(sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class SampleRow(SQLModel, table=True):
    """A cached page snapshot used for design-time analysis / validation."""

    __tablename__ = "samples"

    id: str = Field(primary_key=True)
    recipe_id: str | None = Field(default=None, index=True)
    url: str = Field(index=True)
    final_url: str = ""
    status: int = 0
    tier: str | None = None
    kind: str = "list"  # list | detail | page
    html_path: str = ""
    bytes: int = 0
    title: str = ""
    blobs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    verdict: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    headers: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    analysis: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # JSON responses the page itself fetched, when the snapshot was taken with capture on
    xhr: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class DatasetRow(SQLModel, table=True):
    """One row of the *dataset*: everything this recipe has ever seen, keyed by its dedupe key.

    Runs are episodes; this is the thing people actually want to keep — when a row first showed
    up, when it was last seen, how many times it changed, and what the last change was.
    """

    __tablename__ = "dataset"

    id: str = Field(primary_key=True)  # recipe_id + dedupe key digest
    recipe_id: str = Field(index=True)
    key: str = Field(index=True)  # the dedupe key, joined
    url: str = ""
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    first_seen: datetime = Field(default_factory=_now)
    last_seen: datetime = Field(default_factory=_now, index=True)
    last_changed: datetime | None = None
    changes: int = 0
    runs: int = 0
    gone: bool = False  # last full run did not find it
    history: list[Any] = Field(default_factory=list, sa_column=Column(JSON))


class PageStateRow(SQLModel, table=True):
    """What we knew about one URL after the last run: enough to ask "has this changed?"."""

    __tablename__ = "page_state"

    id: str = Field(primary_key=True)  # recipe_id + url digest
    recipe_id: str = Field(index=True)
    url: str = Field(index=True)
    etag: str = ""
    last_modified: str = ""  # the HTTP header, verbatim
    lastmod: str = ""  # the sitemap's <lastmod>, verbatim
    content_hash: str = ""
    status: int = 0
    items: int = 0  # rows this page produced last time
    run_id: str = ""
    fetched_at: datetime = Field(default_factory=_now)


class SessionRow(SQLModel, table=True):
    """A login session: Playwright storage_state captured from a headed 'log in once' window."""

    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    name: str = ""
    start_url: str = ""
    domain: str = ""
    storage_state_path: str = ""
    status: str = "pending"  # pending | ready | failed | expired
    cookies: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_used_at: datetime | None = None


class ChatRow(SQLModel, table=True):
    """An in-app designer conversation (Claude / Gemini via API key)."""

    __tablename__ = "chats"

    id: str = Field(primary_key=True)
    recipe_id: str | None = Field(default=None, index=True)
    provider: str = "anthropic"
    model: str = ""
    effort: str = "high"
    title: str = ""
    status: str = "idle"  # idle | running | error
    messages: list[Any] = Field(default_factory=list, sa_column=Column(JSON))  # UI transcript
    history: list[Any] = Field(default_factory=list, sa_column=Column(JSON))  # provider-native
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class TierMemoryRow(SQLModel, table=True):
    __tablename__ = "tier_memory"

    domain: str = Field(primary_key=True)
    tier: str
    updated_at: datetime = Field(default_factory=_now)


class NoteRow(SQLModel, table=True):
    """Free-form key/value (schema version, migrations, misc)."""

    __tablename__ = "meta"

    key: str = Field(primary_key=True)
    value: str = Field(sa_column=Column(Text))

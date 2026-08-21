"""Store facade over SQLite (WAL, busy_timeout). Sync SQLAlchemy — FastAPI runs `def` routes in a
threadpool, and item inserts are batched. One `Store` per server process."""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event, func, text
from sqlmodel import Session, SQLModel, create_engine, select

from scrapy_awesome.config import Paths, get_paths
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store.models import (
    ChatRow,
    DatasetRow,
    FailedPageRow,
    ItemRow,
    NoteRow,
    PageStateRow,
    RecipeRow,
    RecipeVersionRow,
    RunRow,
    SampleRow,
    ScheduleRow,
    SessionRow,
    TierMemoryRow,
    iso,
)

SCHEMA_VERSION = "1"


def _now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, db_path: Path, paths: Paths | None = None) -> None:
        self.paths = paths or get_paths()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        self._write_lock = threading.Lock()
        SQLModel.metadata.create_all(self.engine)
        self._migrate_additive()
        with self.session() as s:
            row = s.get(NoteRow, "schema_version")
            if row is None:
                s.add(NoteRow(key="schema_version", value=SCHEMA_VERSION))
                s.commit()

    def _migrate_additive(self) -> None:
        """Add columns that exist on the models but not in an older on-disk DB (SQLite can only
        ADD COLUMN; renames/drops would need a real migration)."""
        with self.engine.begin() as conn:
            for table in SQLModel.metadata.sorted_tables:
                existing = {
                    r[1]
                    for r in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")').fetchall()
                }
                if not existing:
                    continue
                for col in table.columns:
                    if col.name in existing:
                        continue
                    ctype = col.type.compile(dialect=self.engine.dialect)
                    default = ""
                    if col.default is not None and getattr(col.default, "is_scalar", False):
                        v = col.default.arg
                        default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v)}"
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ctype}{default}'
                    )

    def session(self) -> Session:
        return Session(self.engine)

    # ------------------------------------------------------------------ recipes
    def save_recipe(self, recipe: Recipe, *, note: str = "", bump: bool = True) -> RecipeRow:
        data = recipe.to_dict()
        with self._write_lock, self.session() as s:
            row = s.get(RecipeRow, recipe.id)
            if row is None:
                row = RecipeRow(id=recipe.id, name=recipe.name, version=1, data=data)
                s.add(row)
                s.add(
                    RecipeVersionRow(
                        recipe_id=recipe.id, version=1, data=data, note=note or "created"
                    )
                )
            else:
                if bump and row.data != data:
                    row.version += 1
                    s.add(
                        RecipeVersionRow(
                            recipe_id=recipe.id, version=row.version, data=data, note=note
                        )
                    )
                row.name = recipe.name
                row.data = data
                row.updated_at = _now()
                s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def get_recipe(self, recipe_id: str) -> Recipe | None:
        with self.session() as s:
            row = s.get(RecipeRow, recipe_id)
            if not row:
                return None
            rec = Recipe.model_validate(row.data)
            if row.fingerprints:
                rec.fingerprints = dict(row.fingerprints)
            return rec

    def set_fingerprints(self, recipe_id: str, fingerprints: dict[str, Any]) -> None:
        with self._write_lock, self.session() as s:
            row = s.get(RecipeRow, recipe_id)
            if row:
                row.fingerprints = fingerprints
                s.add(row)
                s.commit()

    def get_recipe_row(self, recipe_id: str) -> RecipeRow | None:
        with self.session() as s:
            return s.get(RecipeRow, recipe_id)

    def get_recipe_version(self, recipe_id: str, version: int) -> Recipe | None:
        with self.session() as s:
            row = s.exec(
                select(RecipeVersionRow).where(
                    RecipeVersionRow.recipe_id == recipe_id, RecipeVersionRow.version == version
                )
            ).first()
            return Recipe.model_validate(row.data) if row else None

    def list_recipes(self, *, include_archived: bool = False) -> list[RecipeRow]:
        with self.session() as s:
            q = select(RecipeRow).order_by(RecipeRow.updated_at.desc())  # type: ignore[attr-defined]
            if not include_archived:
                q = q.where(RecipeRow.archived == False)  # noqa: E712
            return list(s.exec(q).all())

    def list_recipe_versions(self, recipe_id: str) -> list[RecipeVersionRow]:
        with self.session() as s:
            return list(
                s.exec(
                    select(RecipeVersionRow)
                    .where(RecipeVersionRow.recipe_id == recipe_id)
                    .order_by(RecipeVersionRow.version.desc())  # type: ignore[attr-defined]
                ).all()
            )

    def delete_recipe(self, recipe_id: str) -> bool:
        with self._write_lock, self.session() as s:
            row = s.get(RecipeRow, recipe_id)
            if row is None:
                return False
            row.archived = True
            row.updated_at = _now()
            s.add(row)
            s.commit()
            return True

    # ------------------------------------------------------------------ runs
    def create_run(
        self,
        *,
        run_id: str,
        recipe: Recipe | None,
        recipe_version: int | None,
        kind: str,
        run_dir: Path,
        token: str,
        limits: dict[str, Any] | None = None,
    ) -> RunRow:
        with self._write_lock, self.session() as s:
            row = RunRow(
                id=run_id,
                recipe_id=recipe.id if recipe else None,
                recipe_version=recipe_version,
                recipe_name=recipe.name if recipe else "",
                kind=kind,
                run_dir=str(run_dir),
                token=token,
                limits=limits or {},
            )
            s.add(row)
            if recipe:
                rr = s.get(RecipeRow, recipe.id)
                if rr:
                    rr.last_run_id = run_id
                    s.add(rr)
            s.commit()
            s.refresh(row)
            return row

    def update_run(self, run_id: str, **fields: Any) -> RunRow | None:
        with self._write_lock, self.session() as s:
            row = s.get(RunRow, run_id)
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def get_run(self, run_id: str) -> RunRow | None:
        with self.session() as s:
            return s.get(RunRow, run_id)

    def list_runs(self, *, recipe_id: str | None = None, limit: int = 100) -> list[RunRow]:
        with self.session() as s:
            q = select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
            if recipe_id:
                q = q.where(RunRow.recipe_id == recipe_id)
            return list(s.exec(q).all())

    def add_items(self, run_id: str, rows: list[tuple[int, dict[str, Any]]]) -> None:
        if not rows:
            return
        with self._write_lock, self.session() as s:
            s.add_all([ItemRow(run_id=run_id, n=n, data=data) for n, data in rows])
            s.commit()

    def count_items(self, run_id: str) -> int:
        with self.session() as s:
            return int(
                s.exec(
                    select(func.count()).select_from(ItemRow).where(ItemRow.run_id == run_id)
                ).one()
            )

    def list_items(self, run_id: str, *, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as s:
            q = (
                select(ItemRow)
                .where(ItemRow.run_id == run_id)
                .order_by(ItemRow.n)  # type: ignore[arg-type]
                .offset(offset)
                .limit(limit)
            )
            return [r.data for r in s.exec(q).all()]

    def iter_items(self, run_id: str, *, batch: int = 1000):
        offset = 0
        while True:
            chunk = self.list_items(run_id, offset=offset, limit=batch)
            if not chunk:
                return
            yield from chunk
            offset += len(chunk)

    # ------------------------------------------------------------------ samples
    def add_sample(
        self,
        *,
        url: str,
        html: str,
        final_url: str,
        status: int,
        tier: str | None,
        kind: str,
        recipe_id: str | None,
        blobs: dict[str, Any] | None,
        verdict: dict[str, Any] | None,
        headers: dict[str, Any] | None,
        title: str = "",
        analysis: dict[str, Any] | None = None,
        xhr: list[Any] | None = None,
    ) -> SampleRow:
        sid = uuid.uuid4().hex[:12]
        self.paths.ensure()
        html_path = self.paths.snapshots / f"{sid}.html"
        html_path.write_text(html, encoding="utf-8")
        with self._write_lock, self.session() as s:
            row = SampleRow(
                id=sid,
                recipe_id=recipe_id,
                url=url,
                final_url=final_url,
                status=status,
                tier=tier,
                kind=kind,
                html_path=str(html_path),
                bytes=len(html),
                title=title,
                blobs=blobs or {},
                verdict=verdict,
                headers=headers or {},
                analysis=analysis,
                xhr=xhr or [],
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def get_sample(self, sample_id: str) -> SampleRow | None:
        with self.session() as s:
            return s.get(SampleRow, sample_id)

    def sample_html(self, sample: SampleRow) -> str:
        p = Path(sample.html_path)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def list_samples(self, *, recipe_id: str | None = None, limit: int = 50) -> list[SampleRow]:
        with self.session() as s:
            q = select(SampleRow).order_by(SampleRow.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
            if recipe_id:
                q = q.where(SampleRow.recipe_id == recipe_id)
            return list(s.exec(q).all())

    def update_sample(self, sample_id: str, **fields: Any) -> SampleRow | None:
        with self._write_lock, self.session() as s:
            row = s.get(SampleRow, sample_id)
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def delete_sample(self, sample_id: str) -> None:
        with self._write_lock, self.session() as s:
            row = s.get(SampleRow, sample_id)
            if row:
                Path(row.html_path).unlink(missing_ok=True)
                s.delete(row)
                s.commit()

    # ------------------------------------------------------------------ sessions
    def upsert_session(self, row: SessionRow) -> SessionRow:
        with self._write_lock, self.session() as s:
            row.updated_at = _now()
            s.merge(row)
            s.commit()
            return s.get(SessionRow, row.id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> SessionRow | None:
        with self.session() as s:
            return s.get(SessionRow, session_id)

    def list_sessions(self) -> list[SessionRow]:
        with self.session() as s:
            return list(s.exec(select(SessionRow).order_by(SessionRow.updated_at.desc())).all())  # type: ignore[attr-defined]

    def delete_session(self, session_id: str) -> None:
        with self._write_lock, self.session() as s:
            row = s.get(SessionRow, session_id)
            if row:
                s.delete(row)
                s.commit()

    # ------------------------------------------------------------------ chats
    def create_chat(self, row: ChatRow) -> ChatRow:
        with self._write_lock, self.session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
            s.expunge(row)
            return row

    def get_chat(self, chat_id: str) -> ChatRow | None:
        with self.session() as s:
            row = s.get(ChatRow, chat_id)
            if row:
                s.expunge(row)
            return row

    def update_chat(self, chat_id: str, **fields: Any) -> ChatRow | None:
        with self._write_lock, self.session() as s:
            row = s.get(ChatRow, chat_id)
            if not row:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = _now()
            s.add(row)
            s.commit()
            s.refresh(row)
            s.expunge(row)
            return row

    def list_chats(self, recipe_id: str | None = None, limit: int = 50) -> list[ChatRow]:
        with self.session() as s:
            q = select(ChatRow)
            if recipe_id:
                q = q.where(ChatRow.recipe_id == recipe_id)
            q = q.order_by(ChatRow.updated_at.desc()).limit(limit)  # type: ignore[attr-defined]
            rows = list(s.exec(q).all())
            for r in rows:
                s.expunge(r)
            return rows

    def delete_chat(self, chat_id: str) -> None:
        with self._write_lock, self.session() as s:
            row = s.get(ChatRow, chat_id)
            if row:
                s.delete(row)
                s.commit()

    # ------------------------------------------------------------------ schedules
    def upsert_schedule(self, row: ScheduleRow) -> ScheduleRow:
        with self._write_lock, self.session() as s:
            row.updated_at = _now()
            merged = s.merge(row)
            s.commit()
            s.refresh(merged)
            s.expunge(merged)
            return merged

    def get_schedule(self, schedule_id: str) -> ScheduleRow | None:
        with self.session() as s:
            row = s.get(ScheduleRow, schedule_id)
            if row:
                s.expunge(row)
            return row

    def update_schedule(self, schedule_id: str, **fields: Any) -> ScheduleRow | None:
        with self._write_lock, self.session() as s:
            row = s.get(ScheduleRow, schedule_id)
            if not row:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = _now()
            s.add(row)
            s.commit()
            s.refresh(row)
            s.expunge(row)
            return row

    def list_schedules(self, recipe_id: str | None = None) -> list[ScheduleRow]:
        with self.session() as s:
            q = select(ScheduleRow)
            if recipe_id:
                q = q.where(ScheduleRow.recipe_id == recipe_id)
            rows = list(s.exec(q.order_by(ScheduleRow.created_at.desc())).all())  # type: ignore[attr-defined]
            for r in rows:
                s.expunge(r)
            return rows

    def due_schedules(self, now: datetime) -> list[ScheduleRow]:
        with self.session() as s:
            q = select(ScheduleRow).where(
                ScheduleRow.enabled == True,  # noqa: E712
                ScheduleRow.next_run_at != None,  # noqa: E711
                ScheduleRow.next_run_at <= now,  # type: ignore[operator]
            )
            rows = list(s.exec(q).all())
            for r in rows:
                s.expunge(r)
            return rows

    def delete_schedule(self, schedule_id: str) -> None:
        with self._write_lock, self.session() as s:
            row = s.get(ScheduleRow, schedule_id)
            if row:
                s.delete(row)
                s.commit()

    def previous_finished_run(self, recipe_id: str, before_run_id: str) -> RunRow | None:
        """The most recent *finished* crawl of this recipe created before `before_run_id`."""
        with self.session() as s:
            cur = s.get(RunRow, before_run_id)
            if not cur:
                return None
            q = (
                select(RunRow)
                .where(
                    RunRow.recipe_id == recipe_id,
                    RunRow.kind == "crawl",
                    RunRow.status == "finished",
                    RunRow.id != before_run_id,
                    RunRow.created_at < cur.created_at,
                )
                .order_by(RunRow.created_at.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
            row = s.exec(q).first()
            if row:
                s.expunge(row)
            return row

    # ------------------------------------------------------------------ dataset (across runs)
    HISTORY_MAX = 20

    def fold_run_into_dataset(
        self, recipe_id: str, run_id: str, keys: list[str], *, partial: bool = False
    ) -> dict[str, int]:
        """Merge a finished run into the recipe's dataset. Returns what happened, for the UI.

        `partial` (an incremental run) means "absent" carries no information, so rows the run did
        not visit keep whatever they had — only a full run may mark something gone.
        """
        now = _now()
        seen: set[str] = set()
        added = changed = unchanged = 0
        with self._write_lock, self.session() as s:
            for row in self.iter_items(run_id):
                key = "␟".join(str(row.get(k, "")) for k in (keys or ["_url"]))
                digest = hashlib.sha1(f"{recipe_id}|{key}".encode()).hexdigest()[:24]
                seen.add(digest)
                visible = {k: v for k, v in row.items() if not k.startswith("_")}
                existing = s.get(DatasetRow, digest)
                if existing is None:
                    s.add(
                        DatasetRow(
                            id=digest,
                            recipe_id=recipe_id,
                            key=key,
                            url=str(row.get("_url") or ""),
                            data=visible,
                            first_seen=now,
                            last_seen=now,
                            runs=1,
                        )
                    )
                    added += 1
                    continue
                existing.last_seen = now
                existing.runs += 1
                existing.gone = False
                diff = {
                    k: [existing.data.get(k), visible.get(k)]
                    for k in set(existing.data) | set(visible)
                    if existing.data.get(k) != visible.get(k)
                }
                if diff:
                    existing.history = (
                        [*(existing.history or []), {"at": iso(now), "diff": diff}]
                    )[-self.HISTORY_MAX :]
                    existing.data = visible
                    existing.url = str(row.get("_url") or existing.url)
                    existing.last_changed = now
                    existing.changes += 1
                    changed += 1
                else:
                    unchanged += 1
                s.add(existing)
            gone = 0
            if not partial:
                for other in s.exec(
                    select(DatasetRow).where(DatasetRow.recipe_id == recipe_id)
                ).all():
                    if other.id not in seen and not other.gone:
                        other.gone = True
                        s.add(other)
                        gone += 1
            s.commit()
        return {"added": added, "changed": changed, "unchanged": unchanged, "gone": gone}

    def dataset(
        self,
        recipe_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        include_gone: bool = True,
        changed_since: datetime | None = None,
    ) -> dict[str, Any]:
        with self.session() as s:
            q = select(DatasetRow).where(DatasetRow.recipe_id == recipe_id)
            if not include_gone:
                q = q.where(DatasetRow.gone == False)  # noqa: E712 - SQL, not Python
            if changed_since is not None:
                q = q.where(DatasetRow.last_changed >= changed_since)
            total = len(s.exec(q).all())
            rows = s.exec(
                q.order_by(DatasetRow.last_seen.desc()).offset(offset).limit(limit)  # type: ignore[union-attr]
            ).all()
        return {
            "total": total,
            "rows": [
                {
                    "key": r.key,
                    "url": r.url,
                    "first_seen": iso(r.first_seen),
                    "last_seen": iso(r.last_seen),
                    "last_changed": iso(r.last_changed) if r.last_changed else None,
                    "changes": r.changes,
                    "runs": r.runs,
                    "gone": r.gone,
                    **r.data,
                }
                for r in rows
            ],
        }

    def dataset_history(self, recipe_id: str, key: str) -> list[dict[str, Any]]:
        digest = hashlib.sha1(f"{recipe_id}|{key}".encode()).hexdigest()[:24]
        with self.session() as s:
            row = s.get(DatasetRow, digest)
            return list(row.history or []) if row else []

    def forget_dataset(self, recipe_id: str) -> int:
        with self._write_lock, self.session() as s:
            rows = s.exec(select(DatasetRow).where(DatasetRow.recipe_id == recipe_id)).all()
            for r in rows:
                s.delete(r)
            s.commit()
            return len(rows)

    # ------------------------------------------------------------------ page state (incremental)
    def page_state(self, recipe_id: str) -> dict[str, dict[str, Any]]:
        """What the last run learned about each URL, keyed by URL."""
        with self.session() as s:
            rows = s.exec(select(PageStateRow).where(PageStateRow.recipe_id == recipe_id)).all()
        return {
            r.url: {
                "etag": r.etag,
                "last_modified": r.last_modified,
                "lastmod": r.lastmod,
                "content_hash": r.content_hash,
                "items": r.items,
                "fetched_at": iso(r.fetched_at),
            }
            for r in rows
        }

    def remember_page_state(self, recipe_id: str, entries: list[dict[str, Any]]) -> int:
        """Upsert what a run learned. One transaction: a 25k-URL sitemap run is one write."""
        if not entries:
            return 0
        now = _now()
        with self._write_lock, self.session() as s:
            for e in entries:
                url = str(e.get("url") or "")
                if not url:
                    continue
                sid = hashlib.sha1(f"{recipe_id}|{url}".encode()).hexdigest()[:24]
                row = s.get(PageStateRow, sid) or PageStateRow(id=sid, recipe_id=recipe_id, url=url)
                row.etag = str(e.get("etag") or "")
                row.last_modified = str(e.get("last_modified") or "")
                row.lastmod = str(e.get("lastmod") or "")
                row.content_hash = str(e.get("content_hash") or "")
                row.status = int(e.get("status") or 0)
                row.items = int(e.get("items") or 0)
                row.run_id = str(e.get("run_id") or "")
                row.fetched_at = now
                s.add(row)
            s.commit()
        return len(entries)

    def forget_page_state(self, recipe_id: str) -> int:
        with self._write_lock, self.session() as s:
            rows = s.exec(select(PageStateRow).where(PageStateRow.recipe_id == recipe_id)).all()
            for r in rows:
                s.delete(r)
            s.commit()
            return len(rows)

    # ------------------------------------------------------------------ notes (key/value)
    def get_note(self, key: str) -> str | None:
        with self.session() as s:
            row = s.get(NoteRow, key)
            return row.value if row else None

    def set_note(self, key: str, value: str) -> None:
        with self._write_lock, self.session() as s:
            row = s.get(NoteRow, key)
            if row is None:
                s.add(NoteRow(key=key, value=value))
            else:
                row.value = value
                s.add(row)
            s.commit()

    # ------------------------------------------------------------------ failed pages
    def add_failed_page(self, row: FailedPageRow) -> FailedPageRow:
        with self._write_lock, self.session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
            s.expunge(row)
            return row

    def get_failed_page(self, page_id: str) -> FailedPageRow | None:
        with self.session() as s:
            row = s.get(FailedPageRow, page_id)
            if row:
                s.expunge(row)
            return row

    def list_failed_pages(
        self, run_id: str, *, status: str | None = None, limit: int = 200
    ) -> list[FailedPageRow]:
        with self.session() as s:
            q = select(FailedPageRow).where(FailedPageRow.run_id == run_id)
            if status:
                q = q.where(FailedPageRow.status == status)
            rows = list(s.exec(q.order_by(FailedPageRow.created_at).limit(limit)).all())  # type: ignore[attr-defined]
            for r in rows:
                s.expunge(r)
            return rows

    def update_failed_page(self, page_id: str, **fields: Any) -> FailedPageRow | None:
        with self._write_lock, self.session() as s:
            row = s.get(FailedPageRow, page_id)
            if not row:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = _now()
            s.add(row)
            s.commit()
            s.refresh(row)
            s.expunge(row)
            return row

    def iter_item_rows(self, run_id: str, *, batch: int = 500):
        """Yield (n, data) for a run, ordered by n (for post-processing that writes back)."""
        offset = 0
        while True:
            with self.session() as s:
                q = (
                    select(ItemRow)
                    .where(ItemRow.run_id == run_id)
                    .order_by(ItemRow.n)  # type: ignore[attr-defined]
                    .offset(offset)
                    .limit(batch)
                )
                rows = [(r.n, dict(r.data)) for r in s.exec(q).all()]
            if not rows:
                return
            yield from rows
            offset += len(rows)

    def update_items(self, run_id: str, updates: dict[int, dict[str, Any]]) -> int:
        """Replace `data` for items by n. Returns number updated."""
        if not updates:
            return 0
        done = 0
        with self._write_lock, self.session() as s:
            q = select(ItemRow).where(ItemRow.run_id == run_id, ItemRow.n.in_(list(updates)))  # type: ignore[attr-defined]
            for row in s.exec(q).all():
                row.data = updates[row.n]
                s.add(row)
                done += 1
            s.commit()
        return done

    def next_item_n(self, run_id: str) -> int:
        with self.session() as s:
            n = s.exec(select(func.max(ItemRow.n)).where(ItemRow.run_id == run_id)).one()
            return int(n or 0) + 1

    # ------------------------------------------------------------------ retention
    def delete_run(self, run_id: str) -> bool:
        """Delete a run's rows + items and its run directory (never call for an active run)."""
        import shutil

        with self._write_lock, self.session() as s:
            row = s.get(RunRow, run_id)
            if not row:
                return False
            run_dir = row.run_dir
            s.exec(ItemRow.__table__.delete().where(ItemRow.run_id == run_id))  # type: ignore[attr-defined]
            s.exec(FailedPageRow.__table__.delete().where(FailedPageRow.run_id == run_id))  # type: ignore[attr-defined]
            s.delete(row)
            s.commit()
        if run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)
        return True

    def prune(
        self,
        *,
        keep_runs_per_recipe: int,
        keep_samples_per_recipe: int,
        keep_days: int,
        active_run_ids: set[str] | None = None,
    ) -> dict[str, int]:
        """Apply retention caps. Returns counts of deleted runs / samples."""
        from datetime import timedelta

        active = active_run_ids or set()
        cutoff = _now() - timedelta(days=keep_days)
        deleted_runs = 0
        deleted_samples = 0
        with self.session() as s:
            runs = list(
                s.exec(select(RunRow).order_by(RunRow.created_at.desc())).all()  # type: ignore[attr-defined]
            )
            samples = list(
                s.exec(select(SampleRow).order_by(SampleRow.created_at.desc())).all()  # type: ignore[attr-defined]
            )
        seen_runs: dict[str | None, int] = {}
        for r in runs:
            if r.id in active or r.status in ("queued", "running", "stopping"):
                continue
            n = seen_runs.get(r.recipe_id, 0) + 1
            seen_runs[r.recipe_id] = n
            created = (
                r.created_at.replace(tzinfo=UTC) if r.created_at.tzinfo is None else r.created_at
            )
            if (n > keep_runs_per_recipe or created < cutoff) and self.delete_run(r.id):
                deleted_runs += 1
        seen_samples: dict[str | None, int] = {}
        for smp in samples:
            n = seen_samples.get(smp.recipe_id, 0) + 1
            seen_samples[smp.recipe_id] = n
            created = (
                smp.created_at.replace(tzinfo=UTC)
                if smp.created_at.tzinfo is None
                else smp.created_at
            )
            orphan_old = smp.recipe_id is None and created < cutoff
            if n > keep_samples_per_recipe or orphan_old:
                self.delete_sample(smp.id)
                deleted_samples += 1
        return {"runs": deleted_runs, "samples": deleted_samples}

    def data_size_bytes(self) -> int:
        total = 0
        for p in (self.db_path, self.paths.runs, self.paths.cache, self.paths.exports):
            if p.is_file():
                total += p.stat().st_size
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        with contextlib.suppress(OSError):
                            total += f.stat().st_size
        return total

    # ------------------------------------------------------------------ tier memory
    def tier_memory(self) -> dict[str, str]:
        with self.session() as s:
            return {r.domain: r.tier for r in s.exec(select(TierMemoryRow)).all()}

    def remember_tiers(self, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        with self._write_lock, self.session() as s:
            for domain, tier in mapping.items():
                s.merge(TierMemoryRow(domain=domain, tier=tier, updated_at=_now()))
            s.commit()

    def forget_tier(self, domain: str) -> None:
        with self._write_lock, self.session() as s:
            row = s.get(TierMemoryRow, domain)
            if row:
                s.delete(row)
                s.commit()

    # ------------------------------------------------------------------ misc
    def vacuum(self) -> None:
        with self.engine.connect() as c:
            c.execute(text("VACUUM"))

    def dump_json(self) -> str:
        return json.dumps({"schema": SCHEMA_VERSION, "db": str(self.db_path)})


_store: Store | None = None
_store_lock = threading.Lock()


def get_store(paths: Paths | None = None) -> Store:
    """Process-wide singleton (respects SCRAPY_AWESOME_HOME at first call)."""
    global _store
    with _store_lock:
        if _store is None:
            p = (paths or get_paths()).ensure()
            _store = Store(p.db, p)
        return _store


def reset_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.engine.dispose()
        _store = None

"""Recipe data model (Pydantic v2).

Design rules
------------
* Everything must round-trip through JSON/YAML unchanged and stay JSON-serializable, because
  recipes travel through Scrapy `cb_kwargs`/`meta` (JOBDIR pickling), the HTTP API, LLM tools and
  export files.
* One extractor "source" per Extractor (css | xpath | json_path | llm); `attr`/`regex` are modifiers.
* Selector strings are either CSS (default) or XPath (starts with `/`, `(`, `.` or `//`).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic import Field as PField

RECIPE_VERSION = 1

Tier = Literal["auto", "http", "browser", "interactive"]
FieldType = Literal[
    "text", "number", "price", "date", "url", "image", "bool", "enum", "list", "json"
]
Scope = Literal["list", "detail", "page"]
PaginationKind = Literal[
    "none", "next_link", "url_template", "load_more", "infinite_scroll", "xhr_json"
]
PageType = Literal["list", "single"]
IMPLICIT_FIELDS = ("_url", "_page_url", "_fetched_at", "_provenance", "_tier")

_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


_XPATH_PREFIXES = ("/", "(", "./", "../", ".//", "descendant::", "child::", "self::", "ancestor::")


def selector_kind(selector: str) -> Literal["css", "xpath"]:
    """CSS by default; XPath when it starts with `/`, `(`, `./`, `../`, `.//` or an axis name.
    (A bare leading `.` followed by a name is a CSS class selector.)"""
    s = selector.strip()
    return "xpath" if s.startswith(_XPATH_PREFIXES) else "css"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)


# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------
class Extractor(StrictModel):
    """How to pull one value out of an element / page / JSON blob.

    Exactly one of `css`, `xpath`, `json_path`, `llm` must be set. `attr` selects an attribute
    (`href`, `src`, `data-id`, ...) instead of text; `regex` post-filters the value (first capture
    group if any, else the whole match). `all=True` returns every match (for `list` fields).
    """

    css: str | None = None
    xpath: str | None = None
    json_path: str | None = None
    llm: str | None = None
    attr: str | None = None
    regex: str | None = None
    all: bool = False

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Extractor:
        sources = [k for k in ("css", "xpath", "json_path", "llm") if getattr(self, k) is not None]
        if len(sources) != 1:
            raise ValueError(
                f"extractor needs exactly one of css/xpath/json_path/llm, got {sources or 'none'}"
            )
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as exc:  # pragma: no cover - message matters, not path
                raise ValueError(f"invalid regex {self.regex!r}: {exc}") from exc
        return self

    @property
    def source(self) -> Literal["css", "xpath", "json_path", "llm"]:
        for k in ("css", "xpath", "json_path", "llm"):
            if getattr(self, k) is not None:
                return k  # type: ignore[return-value]
        raise AssertionError("unreachable")

    @property
    def selector(self) -> str | None:
        return self.css or self.xpath

    def describe(self) -> str:
        base = {
            "css": self.css,
            "xpath": self.xpath,
            "json_path": f"json:{self.json_path}",
            "llm": f"llm:{self.llm}",
        }[self.source]
        parts = [str(base)]
        if self.attr:
            parts.append(f"@{self.attr}")
        if self.regex:
            parts.append(f"~/{self.regex}/")
        return " ".join(parts)


class Field(StrictModel):
    name: str
    type: FieldType = "text"
    description: str = ""
    required: bool = False
    scope: Scope = "list"
    extract: Extractor
    alternates: list[Extractor] = PField(default_factory=list)
    enum: list[str] | None = None
    examples: list[str] = PField(default_factory=list)
    default: Any = None

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(
                f"field name {v!r} must be snake_case identifier (a-z, 0-9, _; max 64 chars)"
            )
        if v in IMPLICIT_FIELDS:
            raise ValueError(f"field name {v!r} is reserved")
        return v

    @model_validator(mode="after")
    def _enum_needs_values(self) -> Field:
        if self.type == "enum" and not self.enum:
            raise ValueError(f"field {self.name!r} has type enum but no `enum` values")
        if self.type == "list" and not (
            self.extract.all or self.extract.source in ("json_path", "llm")
        ):
            # lists from selectors need all=True to make sense; auto-correct instead of failing
            self.extract.all = True
        return self


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------
class Action(StrictModel):
    """Interactive-tier browser action (compiled to scrapy-playwright PageMethods)."""

    kind: Literal[
        "wait_for", "wait_ms", "scroll_until_stable", "scroll", "click", "fill", "press", "evaluate"
    ]
    selector: str | None = None
    ms: int | None = None
    js: str | None = None
    value: str | None = None
    times: int | None = None
    max_rounds: int | None = None
    optional: bool = False  # ignore failures (e.g. "load more" button gone)

    @model_validator(mode="after")
    def _args(self) -> Action:
        need = {
            "wait_for": ["selector"],
            "wait_ms": ["ms"],
            "click": ["selector"],
            "fill": ["selector", "value"],
            "press": ["selector", "value"],
            "evaluate": ["js"],
        }.get(self.kind, [])
        missing = [k for k in need if getattr(self, k) is None]
        if missing:
            raise ValueError(f"action {self.kind!r} requires {missing}")
        return self


class FetchConfig(StrictModel):
    tier: Tier = "auto"
    profile: str = (
        "chrome"  # scrapy-stealth fingerprint profile ("chrome", "chrome_147", "safari_ios", ...)
    )
    proxy: str | None = None
    session: str | None = None  # storage_state id (login profile) → interactive tier
    actions: list[Action] = PField(default_factory=list)  # interactive only
    wait_for: str | None = None  # shortcut: interactive `wait_for` selector
    settle_seconds: float | None = None  # browser tier settle time (scrapy-stealth)
    timeout_seconds: int = 30
    headers: dict[str, str] = PField(default_factory=dict)
    block_static_assets: bool = True

    @property
    def needs_interactive(self) -> bool:
        return bool(self.actions or self.session or self.wait_for)


# --------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------
class ListConfig(StrictModel):
    container: str  # css/xpath selecting one item element (repeated)
    alternates: list[str] = PField(default_factory=list)
    min_items: int = 1


class DetailConfig(StrictModel):
    enabled: bool = False
    link: Extractor | None = None  # relative to list container; must yield a URL
    max_concurrency: int = 4
    fetch: FetchConfig | None = None  # override tier/actions for detail pages

    @model_validator(mode="after")
    def _link_when_enabled(self) -> DetailConfig:
        if self.enabled and self.link is None:
            raise ValueError("detail.enabled requires detail.link")
        return self


class Pagination(StrictModel):
    kind: PaginationKind = "none"
    selector: str | None = (
        None  # next_link: css/xpath to <a> (href taken automatically), load_more: button
    )
    url_template: str | None = None  # url_template: "https://x/?page={page}"
    start: int = 1
    step: int = 1
    max_pages: int = 20
    xhr_url_template: str | None = None  # xhr_json: "https://x/api?page={page}"
    xhr_items_path: str | None = None  # json_path to the items array in the XHR response
    stop_when_no_new_items: bool = True

    @model_validator(mode="after")
    def _kind_args(self) -> Pagination:
        if self.kind in ("next_link", "load_more") and not self.selector:
            raise ValueError(f"pagination kind {self.kind!r} requires `selector`")
        if self.kind == "url_template" and not (
            self.url_template and "{page}" in self.url_template
        ):
            raise ValueError(
                "pagination kind 'url_template' requires url_template containing {page}"
            )
        if self.kind == "xhr_json" and not (self.xhr_url_template and self.xhr_items_path):
            raise ValueError(
                "pagination kind 'xhr_json' requires xhr_url_template and xhr_items_path"
            )
        return self


class Limits(StrictModel):
    max_pages: int = 20
    max_items: int = 1000
    max_detail_pages: int | None = None
    download_delay: float = 0.5
    concurrency_per_domain: int = 4
    per_run_llm_budget_usd: float = 1.0
    request_timeout_seconds: int = 30


class Fallback(StrictModel):
    llm_enabled: bool = True
    only_missing_fields: bool = True


class Recipe(StrictModel):
    version: Literal[1] = RECIPE_VERSION
    id: str = PField(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Untitled recipe"
    created_at: datetime = PField(default_factory=lambda: datetime.now(UTC))
    seeds: list[str] = PField(min_length=1)
    intent: str = ""
    page_type: PageType = "list"
    allowed_domains: list[str] = PField(default_factory=list)  # empty = domains of seeds
    fetch: FetchConfig = PField(default_factory=FetchConfig)
    list_: ListConfig | None = PField(default=None, alias="list")  # `list` shadows the builtin
    detail: DetailConfig = PField(default_factory=DetailConfig)
    pagination: Pagination = PField(default_factory=Pagination)
    fields: list[Field] = PField(default_factory=list)  # may be empty while drafting
    dedupe_key: list[str] = PField(default_factory=lambda: ["_url"])
    limits: Limits = PField(default_factory=Limits)
    fallback: Fallback = PField(default_factory=Fallback)
    fingerprints: dict[str, Any] = PField(default_factory=dict)
    notes: str = ""

    # ---- validation --------------------------------------------------------------------
    @field_validator("seeds")
    @classmethod
    def _seeds(cls, v: list[str]) -> list[str]:
        for s in v:
            if not re.match(r"^https?://", s):
                raise ValueError(f"seed {s!r} must be an http(s) URL")
        return v

    @model_validator(mode="after")
    def _structure(self) -> Recipe:
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate field names: {sorted(dupes)}")
        if any(f.scope == "detail" for f in self.fields) and not self.detail.enabled:
            raise ValueError(
                "fields with scope 'detail' require detail.enabled=true and detail.link"
            )
        if self.page_type == "single":
            for f in self.fields:
                if f.scope == "list":
                    f.scope = "page"
        for k in self.dedupe_key:
            if k not in names and k not in IMPLICIT_FIELDS:
                raise ValueError(f"dedupe_key {k!r} is not a field")
        return self

    # ---- readiness (drafts are valid; running needs more) ---------------------------------
    def readiness_errors(self) -> list[str]:
        errs: list[str] = []
        if not self.fields:
            errs.append("add at least one field")
        if self.page_type == "list" and not (self.list_ and self.list_.container.strip()):
            errs.append("list pages need an item container selector")
        for f in self.fields:
            if f.extract.source in ("css", "xpath") and not (f.extract.selector or "").strip():
                errs.append(f"field {f.name!r} has an empty selector")
            if f.extract.source == "json_path" and not (f.extract.json_path or "").strip():
                errs.append(f"field {f.name!r} has an empty json path")
        if self.pagination.kind == "next_link" and not (self.pagination.selector or "").strip():
            errs.append("pagination 'next_link' needs a selector")
        return errs

    @property
    def ready(self) -> bool:
        return not self.readiness_errors()

    # ---- helpers -----------------------------------------------------------------------
    @property
    def list_fields(self) -> list[Field]:
        return [f for f in self.fields if f.scope in ("list", "page")]

    @property
    def detail_fields(self) -> list[Field]:
        return [f for f in self.fields if f.scope == "detail"]

    @property
    def llm_fields(self) -> list[Field]:
        return [f for f in self.fields if f.extract.source == "llm"]

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def domains(self) -> list[str]:
        if self.allowed_domains:
            return list(self.allowed_domains)
        from urllib.parse import urlsplit

        out: list[str] = []
        for s in self.seeds:
            host = urlsplit(s).hostname or ""
            if host and host not in out:
                out.append(host)
        return out

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)


# Type alias for API payloads (accepts dict or Recipe)
RecipeLike = Annotated[Recipe | dict[str, Any], "recipe or raw dict"]

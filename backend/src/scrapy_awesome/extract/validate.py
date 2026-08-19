"""In-process validation of a recipe against cached sample pages.

Produces the diagnostics the UI shows in the Preview gate and the LLM designer feeds back into its
refinement loop: per-field fill rate / distinct count / examples, container matches, positional
selectors, identical or constant columns, pagination and detail-link checks.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from scrapy_awesome.extract.engine import (
    ExtractedItem,
    extract_list_items,
    extract_page_fields,
    item_url,
    next_page_url,
)
from scrapy_awesome.recipe.models import Extractor, Field, Recipe

Level = Literal["error", "warn", "info"]
_POSITIONAL = re.compile(r":nth-(child|of-type|last-child)|:first-child|:last-child|\[\d+\]")


@dataclass
class Sample:
    url: str
    html: str
    kind: Literal["list", "detail"] = "list"
    json_blobs: dict[str, Any] | None = None


@dataclass
class Issue:
    level: Level
    code: str
    message: str
    field: str | None = None


@dataclass
class FieldStats:
    name: str
    scope: str
    n_total: int = 0
    n_filled: int = 0
    distinct: int = 0
    examples: list[Any] = field(default_factory=list)
    provenance: dict[str, int] = field(default_factory=dict)
    selector: str = ""

    @property
    def fill_rate(self) -> float:
        return self.n_filled / self.n_total if self.n_total else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fill_rate"] = round(self.fill_rate, 3)
        return d


@dataclass
class ValidationReport:
    ok: bool = True
    containers: list[dict[str, Any]] = field(default_factory=list)
    fields: dict[str, FieldStats] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    pagination: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def add(self, level: Level, code: str, message: str, field_name: str | None = None) -> None:
        self.issues.append(Issue(level, code, message, field_name))
        if level == "error":
            self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "containers": self.containers,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "rows": self.rows,
            "issues": [asdict(i) for i in self.issues],
            "pagination": self.pagination,
            "detail": self.detail,
        }

    def summary(self) -> str:
        parts = [f"{'OK' if self.ok else 'FAIL'}: {len(self.rows)} rows"]
        for name, fs in self.fields.items():
            parts.append(f"{name}={fs.fill_rate:.0%}")
        return " · ".join(parts)


def _is_positional(ext: Extractor) -> bool:
    s = ext.selector or ""
    return bool(_POSITIONAL.search(s))


def _record(fs: FieldStats, item: ExtractedItem, name: str) -> None:
    fs.n_total += 1
    v = item.values.get(name)
    if v not in (None, "", []):
        fs.n_filled += 1
        if len(fs.examples) < 3 and v not in fs.examples:
            fs.examples.append(v)
    p = item.provenance.get(name, "missing")
    fs.provenance[p] = fs.provenance.get(p, 0) + 1


def _html_only(f: Field) -> bool:
    """True when every extractor of a field reads HTML (unusable on a JSON body)."""
    exts = [f.extract, *f.alternates]
    return bool(exts) and all(e.source in ("css", "xpath") for e in exts)


def validate_on_samples(
    recipe: Recipe, samples: list[Sample], *, max_rows: int = 50
) -> ValidationReport:
    rep = ValidationReport()
    for f in recipe.fields:
        rep.fields[f.name] = FieldStats(name=f.name, scope=f.scope, selector=f.extract.describe())
        if _is_positional(f.extract):
            rep.add(
                "warn",
                "positional_selector",
                f"field {f.name!r} uses a positional selector ({f.extract.selector}); prefer attributes/classes",
                f.name,
            )
    if recipe.list_ and _POSITIONAL.search(recipe.list_.container):
        rep.add("warn", "positional_selector", "list.container uses a positional selector")

    list_samples = [s for s in samples if s.kind == "list"]
    detail_samples = [s for s in samples if s.kind == "detail"]
    values_by_field: dict[str, list[Any]] = {f.name: [] for f in recipe.fields}
    detail_links: list[str] = []
    all_items: list[ExtractedItem] = []
    item_urls: dict[int, str] = {}

    for n, s in enumerate(list_samples):
        items, which = extract_list_items(recipe, s.html, s.url, json_blobs=s.json_blobs)
        rep.containers.append({"url": s.url, "matched": len(items), "provenance": which})
        if recipe.page_type == "list":
            if not items and recipe.api is not None and n > 0:
                # an empty page past the first is how a JSON API says "end of results"
                rep.add("info", "api_last_page", f"no more results after {list_samples[0].url}")
            elif not items or which == "missing":
                rep.add("error", "container_missing", f"list.container matched nothing on {s.url}")
            elif recipe.list_ and len(items) < recipe.list_.min_items:
                rep.add(
                    "warn",
                    "few_items",
                    f"list.container matched only {len(items)} on {s.url} (min_items={recipe.list_.min_items})",
                )
        for it in items:
            all_items.append(it)
            item_urls[id(it)] = item_url(recipe, it, s.url)
            for f in recipe.list_fields:
                _record(rep.fields[f.name], it, f.name)
                values_by_field[f.name].append(it.values.get(f.name))
            if it.detail_url:
                detail_links.append(it.detail_url)
        if recipe.pagination.kind == "next_link":
            nxt = next_page_url(recipe, s.html, s.url)
            rep.pagination.setdefault("next_urls", []).append({"from": s.url, "next": nxt})

    for s in detail_samples:
        it = extract_page_fields(recipe, s.html, s.url, scope="detail", json_blobs=s.json_blobs)
        for f in recipe.detail_fields:
            _record(rep.fields[f.name], it, f.name)
            values_by_field[f.name].append(it.values.get(f.name))
        rep.rows.append({"_url": s.url, "_kind": "detail", **it.values})

    # rows preview (list items)
    for it in all_items[:max_rows]:
        rep.rows.insert(
            len(rep.rows) - len(detail_samples), {"_url": item_urls[id(it)], **it.values}
        )

    # ---- field-level issues -------------------------------------------------------------
    for f in recipe.fields:
        fs = rep.fields[f.name]
        if f.extract.source == "llm":
            rep.add(
                "info", "llm_field", f"field {f.name!r} is an AI field (filled at run time)", f.name
            )
            continue
        if f.scope == "detail" and not detail_samples:
            rep.add(
                "info",
                "no_detail_samples",
                f"field {f.name!r} needs a detail sample to validate",
                f.name,
            )
            continue
        if fs.n_total == 0:
            continue
        vals = [v for v in values_by_field[f.name] if v not in (None, "", [])]
        fs.distinct = len({repr(v) for v in vals})
        if f.required and fs.fill_rate < 1.0:
            rep.add(
                "error",
                "required_missing",
                f"required field {f.name!r} filled {fs.fill_rate:.0%}",
                f.name,
            )
        elif fs.fill_rate == 0 and f.sparse:
            rep.add(
                "info", "sparse_empty", f"field {f.name!r} is empty on every sample row", f.name
            )
        elif fs.fill_rate == 0 and recipe.api is not None and _html_only(f):
            rep.add(
                "info",
                "fallback_only_field",
                f"field {f.name!r} reads the page, not the API — it only fills if the API is unavailable",
                f.name,
            )
        elif fs.fill_rate == 0:
            rep.add("error", "empty_field", f"field {f.name!r} extracted nothing", f.name)
        elif fs.fill_rate < 0.5 and not f.sparse:
            rep.add("warn", "low_fill", f"field {f.name!r} filled only {fs.fill_rate:.0%}", f.name)
        if fs.n_total >= 5 and fs.distinct == 1 and f.scope == "list":
            rep.add(
                "info",
                "constant_column",
                f"field {f.name!r} has the same value on every item ({vals[0]!r})",
                f.name,
            )
        alt_used = sum(n for p, n in fs.provenance.items() if p.startswith("alt:"))
        if alt_used:
            rep.add(
                "info",
                "alternate_used",
                f"field {f.name!r} used an alternate selector {alt_used}×",
                f.name,
            )

    # identical columns
    names = [f.name for f in recipe.list_fields if f.extract.source != "llm"]
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            va, vb = values_by_field[a], values_by_field[b]
            if len(va) >= 3 and va == vb and any(v not in (None, "", []) for v in va):
                rep.add(
                    "warn",
                    "identical_columns",
                    f"fields {a!r} and {b!r} extract identical values",
                    b,
                )

    # ---- pagination / detail --------------------------------------------------------------
    if recipe.pagination.kind == "next_link" and list_samples:
        found = [x for x in rep.pagination.get("next_urls", []) if x["next"]]
        rep.pagination["found_on_first"] = bool(rep.pagination["next_urls"][0]["next"])
        if not found:
            rep.add(
                "info" if recipe.api is not None else "warn",
                "next_link_missing",
                "pagination.selector found no next link on the sample pages"
                + (" (paging comes from the API instead)" if recipe.api is not None else ""),
            )
    if recipe.detail.enabled and all_items:
        ratio = len(detail_links) / len(all_items)
        rep.detail = {
            "items": len(all_items),
            "with_link": len(detail_links),
            "sample": detail_links[:3],
        }
        if ratio < 0.8:
            rep.add(
                "warn",
                "detail_links_missing",
                f"detail.link resolved for only {ratio:.0%} of items",
            )

    return rep


def rows_to_counter(rows: list[dict[str, Any]], key: str) -> Counter:
    return Counter(repr(r.get(key)) for r in rows)

"""Sitemaps and pasted URL lists: where a crawl starts when there is no list page to walk.

A list page is a convenience the site happens to offer. A sitemap is the site telling you every
URL it publishes, with the date it last changed — better coverage than pagination, and the
`lastmod` is what makes an incremental re-run possible.

Pure functions only: the spider and the design-time preview both parse the same way, so what you
preview is what the crawl will walk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

GZIP_MAGIC = b"\x1f\x8b"
MAX_CHILD_SITEMAPS = 50


@dataclass(frozen=True)
class SitemapUrl:
    loc: str
    lastmod: str = ""  # W3C date, verbatim: it is an opaque key for "has this changed?"


def maybe_gunzip(body: bytes, url: str = "") -> bytes:
    """Sitemaps are commonly served gzipped, sometimes without a helpful content-type."""
    if body[:2] == GZIP_MAGIC or url.endswith(".gz"):
        try:
            from scrapy.utils.gz import gunzip

            return gunzip(body)
        except Exception:  # not actually gzip, or truncated — let the XML parser complain
            return body
    return body


def parse(body: bytes, *, url: str = "") -> tuple[str, list[SitemapUrl]]:
    """`("sitemapindex" | "urlset", entries)`. An index yields the child sitemaps to fetch."""
    from scrapy.utils.sitemap import Sitemap

    raw = maybe_gunzip(body, url)
    try:
        sm = Sitemap(raw)
    except Exception:
        return "", []
    entries = [
        SitemapUrl(loc=urljoin(url, str(item["loc"])), lastmod=str(item.get("lastmod") or ""))
        for item in sm
        if item.get("loc")
    ]
    kind = "sitemapindex" if sm.type == "sitemapindex" else "urlset"
    if kind == "sitemapindex":
        entries = entries[:MAX_CHILD_SITEMAPS]
    return kind, entries


def sitemaps_in_robots(robots_txt: str, origin: str) -> list[str]:
    """`Sitemap:` lines — how a site says where its sitemap really lives."""
    out = []
    for line in robots_txt.splitlines():
        if line.strip().lower().startswith("sitemap:"):
            loc = line.split(":", 1)[1].strip()
            if loc:
                out.append(urljoin(origin, loc))
    return out


def default_sitemap(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/sitemap.xml"


def matches(url: str, *, include: str | None, exclude: str | None) -> bool:
    """A sitemap lists everything — the About page as readily as the products."""
    if include and not re.search(include, url):
        return False
    return not (exclude and re.search(exclude, url))


def select(
    entries: list[SitemapUrl],
    *,
    include: str | None = None,
    exclude: str | None = None,
    limit: int = 1000,
    seen: set[str] | None = None,
) -> list[SitemapUrl]:
    out: list[SitemapUrl] = []
    seen = seen if seen is not None else set()
    for e in entries:
        if e.loc in seen or not matches(e.loc, include=include, exclude=exclude):
            continue
        seen.add(e.loc)
        out.append(e)
        if len(out) >= limit:
            break
    return out


def clean_urls(raw: list[str] | str, *, limit: int = 5000) -> list[str]:
    """A pasted list: newlines, commas, quotes, stray blank lines and duplicates."""
    text = raw if isinstance(raw, str) else "\n".join(raw)
    out: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\s,]+", text):
        u = chunk.strip().strip("\"'<>")
        if not u.startswith(("http://", "https://")) or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def summarize(entries: list[SitemapUrl]) -> dict[str, Any]:
    dated = [e for e in entries if e.lastmod]
    return {
        "urls": len(entries),
        "with_lastmod": len(dated),
        "newest": max((e.lastmod for e in dated), default=""),
        "sample": [e.loc for e in entries[:5]],
    }


__all__ = [
    "SitemapUrl",
    "clean_urls",
    "default_sitemap",
    "matches",
    "maybe_gunzip",
    "parse",
    "select",
    "sitemaps_in_robots",
    "summarize",
]

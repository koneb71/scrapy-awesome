"""Resume compatibility: which recipe edits may be applied to a paused run (same JOBDIR)?

Field extractors, alternates, examples, notes, limits (except concurrency), fallback and
fingerprints may change — the spider re-reads them by (id, version) at start and the queued
requests are still meaningful. Anything that changes *which* URLs get requested (seeds, pagination,
detail link, tier/actions, allowed domains) makes the persisted request queue stale → new run.
"""

from __future__ import annotations

from scrapy_awesome.recipe.models import Recipe

INCOMPATIBLE_PATHS = (
    "seeds",
    "allowed_domains",
    "page_type",
    "list",
    "pagination",
    "detail.enabled",
    "detail.link",
    "detail.fetch",
    "fetch",
)


def _get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def incompatible_changes(old: Recipe, new: Recipe) -> list[str]:
    """Return the list of top-level paths whose change makes an existing JOBDIR unusable."""
    a, b = old.to_dict(), new.to_dict()
    changed: list[str] = []
    for path in INCOMPATIBLE_PATHS:
        if _get(a, path) != _get(b, path):
            changed.append(path)
    return changed


def is_resume_compatible(old: Recipe, new: Recipe) -> bool:
    return not incompatible_changes(old, new)

"""Diff two runs of the same recipe by its dedupe key (default `_url`).

Returns counts + bounded samples so the UI can show "+12 new, −3 gone, ~5 changed" and list them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

IMPLICIT = {"_url", "_page_url", "_fetched_at", "_tier", "_provenance"}


def _key(row: dict[str, Any], keys: list[str]) -> str:
    return "␟".join(str(row.get(k, "")) for k in keys)


def _visible(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in IMPLICIT or k == "_url"}


def diff_rows(
    old: Iterable[dict[str, Any]],
    new: Iterable[dict[str, Any]],
    keys: list[str] | None = None,
    *,
    sample: int = 25,
    partial: bool = False,
) -> dict[str, Any]:
    """`partial` means the new run deliberately skipped unchanged pages (an incremental run), so a
    row it did not produce is a row nobody looked at — not a row that disappeared."""
    keys = keys or ["_url"]
    old_by = {_key(r, keys): r for r in old}
    new_by = {_key(r, keys): r for r in new}
    added = [new_by[k] for k in new_by if k not in old_by]
    removed = [] if partial else [old_by[k] for k in old_by if k not in new_by]
    changed: list[dict[str, Any]] = []
    for k, n in new_by.items():
        o = old_by.get(k)
        if o is None:
            continue
        fields = sorted(
            f
            for f in set(o) | set(n)
            if f not in IMPLICIT and f not in keys and o.get(f) != n.get(f)
        )
        if fields:
            changed.append(
                {
                    "key": {kk: n.get(kk) for kk in keys},
                    "fields": {f: {"old": o.get(f), "new": n.get(f)} for f in fields},
                }
            )
    return {
        "keys": keys,
        "partial": partial,
        "old_count": len(old_by),
        "new_count": len(new_by),
        "added": len(added),
        "removed": len(removed),
        "changed": len(changed),
        "unchanged": len(new_by) - len(added) - len(changed),
        "samples": {
            "added": [_visible(r) for r in added[:sample]],
            "removed": [_visible(r) for r in removed[:sample]],
            "changed": changed[:sample],
        },
    }


def summary_line(d: dict[str, Any]) -> str:
    return f"+{d['added']} new · −{d['removed']} gone · ~{d['changed']} changed · {d['new_count']} total"

"""Find JSON embedded in HTML — the cheapest and most robust data source when present.

Blob names (used as `json:<name>...` containers and `json_path` roots):
  __NEXT_DATA__            <script id="__NEXT_DATA__" type="application/json">
  <id>                     any <script type="application/json" id="<id>">
  ld+json                  list of all <script type="application/ld+json"> objects
  __NUXT__ / __INITIAL_STATE__ / __PRELOADED_STATE__ / __APOLLO_STATE__ / __DATA__
                           `window.X = {...};` assignments (best-effort brace matching)
"""

from __future__ import annotations

import json
import re
from typing import Any

from parsel import Selector

_WINDOW_VARS = (
    "__NUXT__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "__APOLLO_STATE__",
    "__DATA__",
    "__INITIAL_DATA__",
    "__STATE__",
    "__remixContext",
)
_ASSIGN_RE = re.compile(
    r"(?:window\.|globalThis\.|self\.)?("
    + "|".join(re.escape(v) for v in _WINDOW_VARS)
    + r")\s*=\s*"
)


def _balanced_json(text: str, start: int) -> str | None:
    """Return the JSON object/array literal starting at `start` (text[start] in '{[')."""
    if start >= len(text) or text[start] not in "{[":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json_blobs(html: str, *, max_blob_bytes: int = 5_000_000) -> dict[str, Any]:
    blobs: dict[str, Any] = {}
    if not html:
        return blobs
    sel = Selector(text=html)

    for node in sel.xpath('//script[@type="application/json"]'):
        sid = node.xpath("@id").get()
        body = node.xpath("string(.)").get() or ""
        if not sid or not body.strip() or len(body) > max_blob_bytes:
            continue
        try:
            blobs[sid] = json.loads(body)
        except json.JSONDecodeError:
            continue

    ld: list[Any] = []
    for node in sel.xpath('//script[@type="application/ld+json"]'):
        body = (node.xpath("string(.)").get() or "").strip()
        if not body:
            continue
        try:
            ld.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    if ld:
        blobs["ld+json"] = ld

    for node in sel.xpath("//script[not(@type) or @type='text/javascript' or @type='module']"):
        body = node.xpath("string(.)").get() or ""
        if len(body) > max_blob_bytes:
            continue
        for m in _ASSIGN_RE.finditer(body):
            name = m.group(1)
            if name in blobs:
                continue
            lit = _balanced_json(body, m.end())
            if not lit:
                continue
            try:
                blobs[name] = json.loads(lit)
            except json.JSONDecodeError:
                continue
    return blobs


def summarize_blobs(
    blobs: dict[str, Any], *, max_depth: int = 4, max_keys: int = 25
) -> dict[str, Any]:
    """Shape-only summary (keys, types, array lengths) — what the designer LLM sees."""

    def shape(v: Any, depth: int) -> Any:
        if depth <= 0:
            return type(v).__name__
        if isinstance(v, dict):
            keys = list(v.keys())
            out = {k: shape(v[k], depth - 1) for k in keys[:max_keys]}
            if len(keys) > max_keys:
                out["…"] = f"+{len(keys) - max_keys} keys"
            return out
        if isinstance(v, list):
            return [f"array[{len(v)}]", shape(v[0], depth - 1) if v else None]
        return type(v).__name__

    return {name: shape(val, max_depth) for name, val in blobs.items()}

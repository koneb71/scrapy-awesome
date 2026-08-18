"""Tiny JSON path resolver: dotted keys, `[n]` indexes, `[*]` wildcards. No external deps.

Examples: ``props.pageProps.products[*].name``, ``items[0].price.amount``, ``[*].title``.
`resolve()` always returns a list of matches (possibly empty).
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\*|-?\d+)\]")


def tokenize(path: str) -> list[str | int | None]:
    """'a.b[0].c[*]' → ['a', 'b', 0, 'c', None]  (None = wildcard)."""
    out: list[str | int | None] = []
    for key, idx in _TOKEN.findall(path.strip()):
        if key:
            out.append(key)
        elif idx == "*":
            out.append(None)
        else:
            out.append(int(idx))
    return out


def resolve(data: Any, path: str) -> list[Any]:
    if path in ("", "$", "."):
        return [data]
    nodes: list[Any] = [data]
    for tok in tokenize(path):
        nxt: list[Any] = []
        for node in nodes:
            if tok is None:  # wildcard
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
            elif isinstance(tok, int):
                if isinstance(node, list) and -len(node) <= tok < len(node):
                    nxt.append(node[tok])
            elif isinstance(node, dict) and tok in node:
                nxt.append(node[tok])
            elif isinstance(node, list):
                # allow `items.name` to mean `items[*].name`
                for el in node:
                    if isinstance(el, dict) and tok in el:
                        nxt.append(el[tok])
        nodes = nxt
        if not nodes:
            break
    return nodes


def first(data: Any, path: str, default: Any = None) -> Any:
    hits = resolve(data, path)
    return hits[0] if hits else default

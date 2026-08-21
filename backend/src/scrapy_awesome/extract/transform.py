"""Small fixes applied to a value after it is extracted and before it is typed.

The long tail of scraping is not "I cannot find the value", it is "the value is nearly right":
`  Price:  £12.00 ` wants the label gone, `12,50 €` wants a comma turned into a point, a handle
wants to become a URL. Doing that in the recipe keeps it out of the selector, where it would be
brittle, and out of a post-processing script, where it would be invisible.

Transforms run in order, on the raw string, before coercion — so `strip_prefix` then `type: price`
is the natural way to write "drop the label, then read it as money".
"""

from __future__ import annotations

import re
from typing import Any

from scrapy_awesome.recipe.models import Transform

# A regex that can take exponential time on a hostile input is a scraper that hangs on one page.
MAX_PATTERN = 200


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def apply_one(value: Any, t: Transform) -> Any:
    """One transform. Anything that cannot apply returns the value untouched — a transform is a
    tidy-up, never a reason to lose the row."""
    if value is None:
        return None
    kind = t.kind
    if kind == "trim":
        return _as_text(value).strip(t.chars or None)
    if kind == "collapse_space":
        return re.sub(r"\s+", " ", _as_text(value)).strip()
    if kind == "lower":
        return _as_text(value).lower()
    if kind == "upper":
        return _as_text(value).upper()
    if kind == "title":
        return _as_text(value).title()
    if kind == "strip_prefix":
        text = _as_text(value)
        return text[len(t.value) :] if t.value and text.startswith(t.value) else text
    if kind == "strip_suffix":
        text = _as_text(value)
        return text[: -len(t.value)] if t.value and text.endswith(t.value) else text
    if kind == "replace":
        return _as_text(value).replace(t.pattern or "", t.value or "")
    if kind == "regex_replace":
        if not t.pattern or len(t.pattern) > MAX_PATTERN:
            return value
        try:
            return re.sub(t.pattern, t.value or "", _as_text(value))
        except re.error:
            return value
    if kind == "split":
        parts = [p.strip() for p in _as_text(value).split(t.pattern or ",") if p.strip()]
        if t.index is None:
            return parts
        return parts[t.index] if -len(parts) <= t.index < len(parts) else None
    if kind == "prepend":
        return f"{t.value or ''}{_as_text(value)}"
    if kind == "append":
        return f"{_as_text(value)}{t.value or ''}"
    if kind == "decimal_comma":
        # "1.234,56" (European) → "1234.56", leaving "1,234.56" alone
        text = _as_text(value)
        return text.replace(".", "").replace(",", ".") if re.search(r",\d{1,2}\b", text) else text
    if kind == "digits":
        return re.sub(r"[^\d.\-]", "", _as_text(value))
    if kind == "default":
        return value if _as_text(value).strip() else t.value
    return value


def apply(values: list[Any], transforms: list[Transform]) -> list[Any]:
    """Run the chain over every extracted value, dropping any that transform themselves away."""
    if not transforms:
        return values
    out: list[Any] = []
    for v in values:
        current: Any = v
        for t in transforms:
            current = apply_one(current, t)
            if isinstance(current, list):  # `split` without an index fans one value into many
                break
        if isinstance(current, list):
            out.extend(x for x in current if x not in (None, ""))
        elif current not in (None, ""):
            out.append(current)
    return out


__all__ = ["apply", "apply_one"]

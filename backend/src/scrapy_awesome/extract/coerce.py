"""Type coercion: raw strings → typed values (text/number/price/date/url/image/bool/enum/list/json)."""

from __future__ import annotations

import json
import re
from typing import Any

from scrapy_awesome.recipe.models import Field, FieldType

_NUM = re.compile(r"[-+]?\d[\d,.\s]*")
_TRUE = {"true", "yes", "y", "1", "in stock", "available", "on", "✓", "✔"}
_FALSE = {"false", "no", "n", "0", "out of stock", "unavailable", "sold out", "off", "✗", "✘"}


def _to_number(s: str) -> float | int | None:
    m = _NUM.search(s.replace(" ", " "))
    if not m:
        return None
    raw = m.group(0).strip()
    # decide decimal separator: last of ',' or '.' followed by 1-2 digits at the end
    if re.search(r"[.,]\d{1,2}$", raw) and raw.count(",") + raw.count(".") > 0:
        dec = raw[-3] if raw[-3] in ",." else raw[-2]
        thousands = "," if dec == "." else "."
        raw = raw.replace(thousands, "").replace(" ", "").replace(dec, ".")
    else:
        raw = re.sub(r"[,\s]", "", raw)
    try:
        f = float(raw)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in raw else f


def _to_price(s: str) -> float | None:
    try:
        from price_parser import Price

        p = Price.fromstring(s)
        if p.amount is not None:
            return float(p.amount)
    except Exception:
        pass
    return _to_number(s)


def _to_date(s: str) -> str | None:
    try:
        import dateparser

        dt = dateparser.parse(s)
    except Exception:
        dt = None
    if dt is None:
        return None
    return dt.date().isoformat() if dt.time().isoformat() == "00:00:00" else dt.isoformat()


def _to_bool(s: str) -> bool | None:
    t = s.strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    # phrase matching (negatives first: "unavailable" contains "available"); skip 1-2 char tokens
    for k in sorted((k for k in _FALSE if len(k) > 2), key=len, reverse=True):
        if k in t:
            return False
    for k in sorted((k for k in _TRUE if len(k) > 2), key=len, reverse=True):
        if k in t:
            return True
    return None


def _to_enum(s: str, options: list[str] | None) -> str | None:
    if not options:
        return s
    t = s.strip().lower()
    for o in options:
        if o.lower() == t:
            return o
    for o in options:  # substring match (e.g. "star-rating Three" → Three)
        if o.lower() in t:
            return o
    return None


def coerce_one(raw: Any, ftype: FieldType, field: Field | None = None) -> Any:
    if raw is None:
        return None
    if ftype in ("json",):
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return raw
    if not isinstance(raw, str):
        # json_path results may already be typed
        if ftype in ("number", "price") and isinstance(raw, int | float):
            return raw
        if ftype == "bool" and isinstance(raw, bool):
            return raw
        if ftype in ("text", "url", "image", "date", "enum"):
            raw = str(raw)
        else:
            return raw
    s = raw.strip()
    if s == "":
        return None
    if ftype == "text" or ftype in ("url", "image"):
        return s
    if ftype == "number":
        return _to_number(s)
    if ftype == "price":
        return _to_price(s)
    if ftype == "date":
        return _to_date(s)
    if ftype == "bool":
        return _to_bool(s)
    if ftype == "enum":
        return _to_enum(s, field.enum if field else None)
    return s


def coerce(values: list[Any], field: Field) -> Any:
    """Coerce a list of raw values into the field's final value (scalar or list)."""
    if field.type == "list" or field.extract.all:
        out = [coerce_one(v, "text" if field.type == "list" else field.type, field) for v in values]
        return [v for v in out if v is not None]
    for v in values:
        c = coerce_one(v, field.type, field)
        if c is not None:
            return c
    return None

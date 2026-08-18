"""Heuristic page analysis (no LLM): repeated-container candidates, field suggestions inside the best
container, pagination candidates, detail-link candidate, JSON blob shapes, page type guess.

Used by the UI's Analyze step directly and as the starting point the LLM designer refines.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from lxml import html as lxml_html
from lxml.etree import _Element

from scrapy_awesome.fetch.blocks import page_title, visible_text
from scrapy_awesome.snapshot.jsonblobs import summarize_blobs

_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "path",
    "head",
    "meta",
    "link",
    "br",
    "hr",
    "template",
}
_TEXT_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "span",
    "a",
    "li",
    "td",
    "th",
    "div",
    "strong",
    "em",
    "b",
    "time",
    "dd",
    "dt",
    "small",
    "label",
}
_PRICE_RE = re.compile(
    r"(?:[$€£¥₹]|USD|EUR|GBP|CHF)\s?\d[\d,.]*|\d[\d,.]*\s?(?:[$€£¥₹]|USD|EUR|GBP|CHF|zł|kr)", re.I
)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4})\b",
    re.I,
)
_NEXT_TEXT = re.compile(
    r"^\s*(next|next page|older|more|›|»|>|→|load more|show more|see more|view more)\s*$", re.I
)
_PAGE_PARAM = re.compile(r"[?&](page|p|pg|pagina|offset|start)=(\d+)", re.I)
_PAGE_PATH = re.compile(r"/(?:page|p)/(\d+)/?$", re.I)
_UNSTABLE_CLASS = re.compile(
    r"\d{2,}|^(css|sc|jsx|_)-|[A-Za-z0-9]{6,}[-_][A-Za-z0-9]{4,}$|^[a-z]{1,2}\d|__|hash|--\w+$"
)


@dataclass
class ContainerCandidate:
    selector: str
    count: int
    avg_text: float
    with_links: int
    score: float
    sample: list[str] = field(default_factory=list)


@dataclass
class FieldSuggestion:
    name: str
    type: str
    selector: str
    attr: str | None
    examples: list[str]
    fill: float


@dataclass
class PaginationCandidate:
    kind: str
    selector: str | None = None
    url_template: str | None = None
    evidence: str = ""


@dataclass
class Analysis:
    url: str
    title: str
    page_type: str
    text_length: int
    script_count: int
    containers: list[ContainerCandidate] = field(default_factory=list)
    fields: list[FieldSuggestion] = field(default_factory=list)
    detail_link: dict[str, Any] | None = None
    pagination: list[PaginationCandidate] = field(default_factory=list)
    json_blobs: dict[str, Any] = field(default_factory=dict)
    json_list_paths: list[dict[str, Any]] = field(default_factory=list)
    login_hint: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------------------- selectors
def _classes(el: _Element) -> list[str]:
    cls = el.get("class") or ""
    out = []
    for c in cls.split():
        if len(c) < 3 or len(c) > 40 or _UNSTABLE_CLASS.search(c):
            continue
        out.append(c)
    return out[:2]


def _step(el: _Element) -> str:
    tag = el.tag if isinstance(el.tag, str) else "div"
    tag = tag.lower()
    eid = el.get("id")
    if eid and not re.search(r"\d{3,}", eid) and re.match(r"^[A-Za-z_][\w-]*$", eid):
        return f"{tag}#{eid}"
    cls = _classes(el)
    if cls:
        return tag + "".join(f".{c}" for c in cls)
    for attr in ("data-testid", "data-test", "itemprop", "role", "name"):
        v = el.get(attr)
        if v and re.match(r"^[\w.-]+$", v):
            return f'{tag}[{attr}="{v}"]'
    return tag


def _count(root: _Element, css: str) -> int:
    try:
        return len(root.cssselect(css))
    except Exception:
        return -1


def css_for(el: _Element, root: _Element, *, max_depth: int = 4) -> str:
    """Short, cssselect-safe selector for `el` unique within `root` (or as specific as we get)."""
    parts: list[str] = []
    cur: _Element | None = el
    depth = 0
    while cur is not None and cur is not root and depth < max_depth:
        parts.insert(0, _step(cur))
        sel = " ".join(parts)
        if _count(root, sel) == 1:
            return sel
        cur = cur.getparent()
        depth += 1
    return " ".join(parts) if parts else (el.tag if isinstance(el.tag, str) else "*")


def _text(el: _Element, limit: int = 120) -> str:
    t = " ".join(el.itertext())
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


# ---------------------------------------------------------------------------------------- containers
def find_containers(
    root: _Element, *, min_count: int = 3, top: int = 5
) -> list[ContainerCandidate]:
    groups: dict[str, list[_Element]] = defaultdict(list)
    for parent in root.iter():
        if not isinstance(parent.tag, str) or parent.tag in _SKIP_TAGS:
            continue
        kids = [k for k in parent if isinstance(k.tag, str) and k.tag not in _SKIP_TAGS]
        if len(kids) < min_count:
            continue
        sig = Counter(_step(k) for k in kids)
        for step, n in sig.items():
            if (
                n >= min_count
                and step.split(".")[0] not in ("br", "hr", "option", "tr", "th")
                and "." in step
            ) or step in ("li", "article", "tr", "section", "div"):
                if n < min_count:
                    continue
                els = [k for k in kids if _step(k) == step]
                key = f"{css_for(parent, root, max_depth=2)} > {step}"
                groups[key].extend(els)
    cands: list[ContainerCandidate] = []
    for sel, els in groups.items():
        # dedupe elements (a parent selector may match several parents)
        seen: set[int] = set()
        uniq = [e for e in els if id(e) not in seen and not seen.add(id(e))]  # type: ignore[func-returns-value]
        n = len(uniq)
        if n < min_count:
            continue
        texts = [_text(e) for e in uniq[:20]]
        avg = sum(len(t) for t in texts) / max(1, len(texts))
        if avg < 8:  # nav bullets, icons
            continue
        with_links = sum(1 for e in uniq[:20] if e.cssselect("a[href]"))
        # simpler selector if the child step alone is precise enough
        child = sel.split(" > ")[-1]
        if _count(root, child) == n:
            sel = child
        score = n * min(avg, 400) * (1.0 + 0.5 * (with_links / max(1, min(n, 20))))
        cands.append(
            ContainerCandidate(
                selector=sel,
                count=n,
                avg_text=round(avg, 1),
                with_links=with_links,
                score=round(score, 1),
                sample=texts[:3],
            )
        )
    cands.sort(key=lambda c: c.score, reverse=True)
    # drop near-duplicates (same count & first sample)
    out: list[ContainerCandidate] = []
    seen_sig: set[tuple[int, str]] = set()
    for c in cands:
        sig = (c.count, c.sample[0] if c.sample else "")
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        out.append(c)
        if len(out) >= top:
            break
    return out


# ---------------------------------------------------------------------------------------- fields
def suggest_fields(
    root: _Element, container_sel: str, base_url: str
) -> tuple[list[FieldSuggestion], dict[str, Any] | None]:
    try:
        items = root.cssselect(container_sel)
    except Exception:
        return [], None
    if not items:
        return [], None
    sample = items[:12]
    # collect leaf-ish text elements per relative selector
    stats: dict[str, dict[str, Any]] = {}
    for it in sample:
        for el in it.iter():
            if el is it or not isinstance(el.tag, str) or el.tag in _SKIP_TAGS:
                continue
            tag = el.tag.lower()
            rel = css_for(el, it, max_depth=3)
            if tag == "img":
                src = el.get("src") or el.get("data-src")
                if src:
                    st = stats.setdefault(
                        rel + "@src", {"tag": tag, "attr": "src", "vals": [], "sel": rel}
                    )
                    st["vals"].append(urljoin(base_url, src))
                continue
            if tag == "a" and el.get("href"):
                st = stats.setdefault(
                    rel + "@href", {"tag": tag, "attr": "href", "vals": [], "sel": rel}
                )
                st["vals"].append(urljoin(base_url, el.get("href")))
            if tag in _TEXT_TAGS:
                own = "".join(el.xpath("./text()")).strip()
                full = _text(el)
                if not full or (tag in ("div", "li", "span") and len(el) > 2 and not own):
                    continue
                st = stats.setdefault(rel, {"tag": tag, "attr": None, "vals": [], "sel": rel})
                st["vals"].append(full)
    n = len(sample)
    sugg: list[FieldSuggestion] = []
    used_names: set[str] = set()
    for key, st in stats.items():
        vals = [v for v in st["vals"] if v]
        fill = len(vals) / n
        if fill < 0.5 or not vals:
            continue
        distinct = len(set(vals))
        if distinct <= 1 and n >= 3 and st["attr"] is None:
            continue  # boilerplate label
        tag, attr = st["tag"], st["attr"]
        ftype, name = "text", None
        v0 = vals[0]
        if attr == "src":
            ftype, name = "image", "image"
        elif attr == "href":
            ftype, name = "url", "url"
        elif _PRICE_RE.search(v0) and sum(1 for v in vals if _PRICE_RE.search(v)) / len(vals) > 0.7:
            ftype, name = "price", "price"
        elif _DATE_RE.search(v0) and sum(1 for v in vals if _DATE_RE.search(v)) / len(vals) > 0.6:
            ftype, name = "date", "date"
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") or (
            tag == "a" and attr is None and len(v0) < 120
        ):
            name = "title"
        elif re.search(r"\b(rating|stars?|score)\b", key, re.I):
            name = "rating"
        elif re.search(r"\b(desc|summary|excerpt|snippet)\b", key, re.I) or len(v0) > 160:
            name = "description"
        if name is None:
            base = re.sub(
                r"[^a-z0-9]+", "_", (st["sel"].split()[-1] if st["sel"] else tag).lower()
            ).strip("_")
            base = re.sub(r"^(div|span|p|li)_?", "", base) or tag
            name = base[:30]
        cand, i = name, 2
        while cand in used_names:
            cand = f"{name}_{i}"
            i += 1
        used_names.add(cand)
        sugg.append(
            FieldSuggestion(
                name=cand,
                type=ftype,
                selector=st["sel"],
                attr=attr,
                examples=vals[:3],
                fill=round(fill, 2),
            )
        )
    # dedupe suggestions with identical examples (parent/child echo) — keep the more specific selector
    by_examples: dict[tuple[str, ...], FieldSuggestion] = {}
    for sg in sugg:
        key = tuple(sg.examples)
        prev = by_examples.get(key)
        if prev is None or len(sg.selector) > len(prev.selector):
            by_examples[key] = sg
    kept = {id(v) for v in by_examples.values()}
    sugg = [sg for sg in sugg if id(sg) in kept]
    # after dedupe, collapse `title_2` → `title` when the base name is free again
    names = {sg.name for sg in sugg}
    for sg in sugg:
        m = re.match(r"^(.*)_(\d+)$", sg.name)
        if m and m.group(1) not in names:
            names.discard(sg.name)
            sg.name = m.group(1)
            names.add(sg.name)
    # order: title, price, others, image/url last
    order = {"title": 0, "price": 1, "rating": 2, "date": 3, "description": 4}
    sugg.sort(
        key=lambda s: (order.get(s.name.split("_")[0], 5), s.type in ("image", "url"), -s.fill)
    )
    sugg = sugg[:14]

    # detail link: prefer link inside heading, else the first link whose href differs per item
    detail: dict[str, Any] | None = None
    host = urlsplit(base_url).hostname
    for pref in ("h1 a", "h2 a", "h3 a", "h4 a", "a"):
        hrefs = []
        for it in sample:
            a = it.cssselect(pref)
            if a and a[0].get("href"):
                hrefs.append(urljoin(base_url, a[0].get("href")))
        if len(hrefs) >= max(2, n // 2) and len(set(hrefs)) >= len(hrefs) * 0.8:
            same_host = sum(1 for h in hrefs if urlsplit(h).hostname == host) / len(hrefs)
            detail = {"selector": pref, "sample": hrefs[:3], "same_host": round(same_host, 2)}
            break
    return sugg, detail


# ---------------------------------------------------------------------------------------- pagination
def find_pagination(root: _Element, base_url: str) -> list[PaginationCandidate]:
    out: list[PaginationCandidate] = []
    for a in root.cssselect("a[rel~=next]"):
        out.append(PaginationCandidate("next_link", css_for(a, root), evidence="rel=next"))
        break
    for a in root.cssselect("a[href]"):
        t = _text(a, 40)
        if _NEXT_TEXT.match(t) and not re.match(r"(load|show|see|view) more", t, re.I):
            sel = css_for(a, root)
            if not any(c.selector == sel for c in out):
                out.append(PaginationCandidate("next_link", sel, evidence=f"link text {t!r}"))
            break
    # url template from page params
    hrefs = [urljoin(base_url, a.get("href")) for a in root.cssselect("a[href]")]
    for h in hrefs:
        m = _PAGE_PARAM.search(h)
        if m:
            tpl = h[: m.start(2)] + "{page}" + h[m.end(2) :]
            out.append(
                PaginationCandidate(
                    "url_template", url_template=tpl, evidence=f"?{m.group(1)}= in links"
                )
            )
            break
    else:
        for h in hrefs:
            m = _PAGE_PATH.search(h)
            if m:
                tpl = h[: m.start(1)] + "{page}" + h[m.end(1) :]
                out.append(
                    PaginationCandidate(
                        "url_template", url_template=tpl, evidence="/page/N in links"
                    )
                )
                break
    for el in root.cssselect("button, a"):
        t = _text(el, 40)
        if re.match(r"^\s*(load|show|see|view) more", t, re.I):
            out.append(
                PaginationCandidate("load_more", css_for(el, root), evidence=f"button {t!r}")
            )
            break
    return out


def find_json_lists(
    blobs: dict[str, Any], *, min_items: int = 3, top: int = 5
) -> list[dict[str, Any]]:
    """BFS through blobs for arrays of objects (likely item lists)."""
    found: list[dict[str, Any]] = []

    def walk(node: Any, path: str, blob: str, depth: int) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            if len(node) >= min_items and sum(1 for x in node[:10] if isinstance(x, dict)) >= min(
                3, len(node)
            ):
                keys = sorted({k for x in node[:10] if isinstance(x, dict) for k in x})[:12]
                found.append(
                    {
                        "container": f"json:{blob}{('.' + path) if path else ''}[*]",
                        "count": len(node),
                        "keys": keys,
                    }
                )
            for i, x in enumerate(node[:3]):
                walk(x, f"{path}[{i}]" if path else f"[{i}]", blob, depth + 1)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k, blob, depth + 1)

    for name, val in blobs.items():
        walk(val, "", name, 0)
    found.sort(key=lambda f: -f["count"])
    return found[:top]


# ---------------------------------------------------------------------------------------- entry
def analyze_html(html: str, url: str, *, blobs: dict[str, Any] | None = None) -> Analysis:
    root = lxml_html.fromstring(html or "<html></html>")
    text = visible_text(html)
    a = Analysis(
        url=url,
        title=page_title(html),
        page_type="single",
        text_length=len(text),
        script_count=len(root.cssselect("script")),
    )
    a.containers = find_containers(root)
    if a.containers:
        a.page_type = "list"
        a.fields, a.detail_link = suggest_fields(root, a.containers[0].selector, url)
    a.pagination = find_pagination(root, url)
    if blobs:
        a.json_blobs = summarize_blobs(blobs, max_depth=3, max_keys=15)
        a.json_list_paths = find_json_lists(blobs)
    if root.cssselect('input[type="password"]'):
        a.login_hint = True
        a.notes.append("page contains a password field — a login session may be required")
    if a.script_count >= 3 and a.text_length < 300:
        a.notes.append(
            "little visible text and several scripts — content is probably rendered by JavaScript"
        )
    if a.json_list_paths:
        a.notes.append(
            "embedded JSON contains item arrays — a json: container is the most robust choice"
        )
    return a

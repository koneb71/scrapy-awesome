"""Page → markdown for humans and LLMs.

`fit=True` (default) uses trafilatura to keep the *main content* (article/listing) and drop chrome
(nav, footer, cookie banners); `fit=False` converts the whole body with markdownify. Both strip
scripts/styles. Long outputs are truncated to `max_chars` with a marker.
"""

from __future__ import annotations

import re

from lxml import html as lxml_html

_WS_LINES = re.compile(r"\n{3,}")


def _truncate(text: str, max_chars: int) -> str:
    text = _WS_LINES.sub("\n\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n\n… (truncated)"
    return text


def _full_markdown(html: str) -> str:
    from markdownify import markdownify

    try:
        root = lxml_html.fromstring(html)
    except Exception:
        return ""
    for bad in root.xpath("//script|//style|//noscript|//svg|//iframe|//template"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    body = root.find("body")
    src = lxml_html.tostring(body if body is not None else root, encoding="unicode")
    return markdownify(src, heading_style="ATX", strip=["img"])  # images add little for design


def to_markdown(
    html: str, url: str | None = None, *, fit: bool = True, max_chars: int = 20_000
) -> str:
    if not html:
        return ""
    if fit:
        try:
            import trafilatura

            out = trafilatura.extract(
                html,
                url=url,
                output_format="markdown",
                include_links=True,
                include_tables=True,
                include_images=False,
                favor_recall=True,
                with_metadata=False,
            )
            if out and len(out.strip()) > 80:
                return _truncate(out, max_chars)
        except Exception:
            pass
    return _truncate(_full_markdown(html), max_chars)

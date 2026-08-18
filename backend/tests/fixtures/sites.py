"""Local fixture web sites used by tests and spikes.

All sites are served by one FastAPI app (see `server.py`). Each site exercises one capability:

  /static/           list + detail + `li.next a` pagination (3 pages x 5 items)   -> http tier
  /spa/              client-rendered list (empty shell without JS)                -> browser tier
  /embedded/         list rendered from <script id="__NEXT_DATA__"> JSON          -> json_path
  /blocker/          403 "Just a moment..." JS challenge that sets a cookie       -> escalation
  /infinite/         infinite scroll: 5 items appended per scroll, 20 total       -> interactive
  /login/            form login; /login/private requires session cookie           -> sessions
"""

from __future__ import annotations

import json
from html import escape

ITEMS_PER_PAGE = 5
PAGES = 3
TOTAL = ITEMS_PER_PAGE * PAGES  # items reachable through paginated list pages
INFINITE_TOTAL = 20  # items on the infinite-scroll page
CATALOG_SIZE = max(TOTAL, INFINITE_TOTAL)

CATALOG = [
    {
        "id": i,
        "title": f"Widget {i:02d}",
        "price": f"£{10 + i * 1.5:.2f}",
        "rating": ["One", "Two", "Three", "Four", "Five"][i % 5],
        "in_stock": i % 4 != 0,
        "description": f"Widget {i:02d} is a fine widget. It has {i} knobs and a {i % 3 + 1}-year warranty.",
        "tags": ["a", "b", "c"][: (i % 3) + 1],
    }
    for i in range(1, CATALOG_SIZE + 1)
]


def _card(item: dict, base: str) -> str:
    stock = "In stock" if item["in_stock"] else "Out of stock"
    return f"""
    <article class="product_pod" data-id="{item["id"]}">
      <h3><a href="{base}/item/{item["id"]}" title="{escape(item["title"])}">{escape(item["title"])}</a></h3>
      <p class="price_color">{item["price"]}</p>
      <p class="star-rating {item["rating"]}"><i class="icon-star"></i></p>
      <p class="availability">{stock}</p>
    </article>"""


def _card_redesigned(item: dict, base: str) -> str:
    """The same catalog after a 'redesign': title moved to h2 > span, price class renamed and
    wrapped, availability became a data attribute. Used to test self-healing selectors."""
    stock = "In stock" if item["in_stock"] else "Out of stock"
    return f"""
    <article class="product_pod" data-id="{item["id"]}">
      <h2 class="name"><span>{escape(item["title"])}</span> <a class="more" href="{base}/item/{item["id"]}">details</a></h2>
      <div class="pricing"><span class="amount">{item["price"]}</span></div>
      <p class="star-rating {item["rating"]}"><i class="icon-star"></i></p>
      <p class="stock" data-state="{stock}">{stock}</p>
    </article>"""


def list_page(page: int, base: str, *, title: str = "Static list", redesigned: bool = False) -> str:
    start = (page - 1) * ITEMS_PER_PAGE
    items = CATALOG[start : start + ITEMS_PER_PAGE]
    card = _card_redesigned if redesigned else _card
    cards = "\n".join(card(i, base) for i in items)
    nxt = (
        f'<li class="next"><a href="{base}/?page={page + 1}">next</a></li>' if page < PAGES else ""
    )
    prev = (
        f'<li class="previous"><a href="{base}/?page={page - 1}">previous</a></li>'
        if page > 1
        else ""
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)} – page {page}</title></head>
<body>
<h1>{escape(title)}</h1>
<section id="results">{cards}</section>
<ul class="pager">{prev}<li class="current">Page {page} of {PAGES}</li>{nxt}</ul>
</body></html>"""


def detail_page(item_id: int, base: str) -> str:
    item = CATALOG[item_id - 1]
    tags = "".join(f"<li class='tag'>{t}</li>" for t in item["tags"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(item["title"])}</title></head>
<body>
<article class="product_page" data-id="{item["id"]}">
  <h1>{escape(item["title"])}</h1>
  <p class="price_color">{item["price"]}</p>
  <div id="product_description"><h2>Product Description</h2></div>
  <p>{escape(item["description"])}</p>
  <ul class="tags">{tags}</ul>
  <a class="back" href="{base}/">back</a>
</article>
</body></html>"""


def spa_page(base: str) -> str:
    data = json.dumps(CATALOG[:ITEMS_PER_PAGE])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>SPA list</title></head>
<body>
<noscript>Please enable JavaScript to view this page.</noscript>
<div id="app"></div>
<script>
  const DATA = {data};
  setTimeout(() => {{
    const app = document.getElementById('app');
    const list = document.createElement('section'); list.id = 'results';
    for (const it of DATA) {{
      const a = document.createElement('article'); a.className = 'product_pod'; a.dataset.id = it.id;
      a.innerHTML = `<h3><a href="{base}/item/${{it.id}}" title="${{it.title}}">${{it.title}}</a></h3>` +
                    `<p class="price_color">${{it.price}}</p>` +
                    `<p class="star-rating ${{it.rating}}"></p>`;
      list.appendChild(a);
    }}
    app.appendChild(list);
    document.title = 'SPA list – rendered';
  }}, 150);
</script>
</body></html>"""


def embedded_json_page(base: str) -> str:
    payload = {"props": {"pageProps": {"products": CATALOG[:ITEMS_PER_PAGE], "nextPage": None}}}
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Embedded JSON</title></head>
<body>
<div id="__next"><p>Loading…</p></div>
<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>
<script type="application/ld+json">{json.dumps({"@type": "ItemList", "numberOfItems": ITEMS_PER_PAGE})}</script>
</body></html>"""


CHALLENGE_COOKIE = "challenge"


def challenge_page() -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Just a moment...</title></head>
<body>
<h1>Checking your browser before accessing the site.</h1>
<p id="cf-please-wait">This process is automatic.</p>
<script>
  document.cookie = '{CHALLENGE_COOKIE}=passed; path=/';
  setTimeout(() => location.reload(), 100);
</script>
</body></html>"""


def infinite_page(base: str) -> str:
    data = json.dumps(CATALOG[:INFINITE_TOTAL])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Infinite scroll</title>
<style>article {{ height: 300px; border: 1px solid #ccc; margin: 8px; }}</style></head>
<body>
<section id="results"></section>
<p id="status">loaded 0</p>
<script>
  const DATA = {data}; let n = 0;
  function more() {{
    const list = document.getElementById('results');
    for (const it of DATA.slice(n, n + 5)) {{
      const a = document.createElement('article'); a.className = 'product_pod'; a.dataset.id = it.id;
      a.innerHTML = `<h3><a href="{base}/item/${{it.id}}" title="${{it.title}}">${{it.title}}</a></h3><p class="price_color">${{it.price}}</p>`;
      list.appendChild(a);
    }}
    n = Math.min(n + 5, DATA.length);
    document.getElementById('status').textContent = 'loaded ' + n;
  }}
  more();
  window.addEventListener('scroll', () => {{
    if (n < DATA.length && window.innerHeight + window.scrollY >= document.body.offsetHeight - 50) more();
  }});
</script>
</body></html>"""


def login_form(base: str, error: bool = False) -> str:
    err = "<p class='error'>Invalid credentials</p>" if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Login</title></head>
<body>{err}
<form method="post" action="{base}/">
  <input name="username" placeholder="username"><input name="password" type="password" placeholder="password">
  <button type="submit">Sign in</button>
</form>
</body></html>"""

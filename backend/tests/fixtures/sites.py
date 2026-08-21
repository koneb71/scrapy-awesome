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


SITEMAP_PARTS = 2


def sitemap_index() -> str:
    """The shape most sites serve: an index pointing at per-section url sets."""
    parts = "".join(
        f"<sitemap><loc>/sitemap-items-{i}.xml</loc><lastmod>2026-08-0{i + 1}</lastmod></sitemap>"
        for i in range(SITEMAP_PARTS)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{parts}</sitemapindex>'


def sitemap_urlset(part: int) -> str:
    """Half the catalogue per part, plus a page that is not an item (the include filter's job)."""
    half = len(CATALOG) // SITEMAP_PARTS
    items = CATALOG[part * half : (part + 1) * half]
    urls = "".join(
        f"<url><loc>/static/item/{i['id']}</loc>"
        f"<lastmod>2026-08-{(i['id'] % 28) + 1:02d}</lastmod></url>"
        for i in items
    )
    urls += f"<url><loc>/static/about-{part}.html</loc><lastmod>2026-01-01</lastmod></url>"
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def xhr_api(page: int = 1, limit: int = 5) -> dict:
    """`/xhr/api/items?page=&limit=` — the endpoint the SPA below reads itself from."""
    start = (page - 1) * limit
    items = CATALOG[start : start + limit]
    return {
        "meta": {"total": len(CATALOG), "page": page},
        "results": [
            {
                "id": i["id"],
                "productName": i["title"],
                "priceCents": int(float(i["price"].lstrip("£")) * 100),
                "price": i["price"].lstrip("£"),
                "detailUrl": f"/xhr/item/{i['id']}",
                "inStock": i["in_stock"],
                "publishedAt": "2026-01-0{}T10:00:00Z".format((i["id"] % 9) + 1),
            }
            for i in items
        ],
    }


def xhr_page(base: str) -> str:
    """A page that renders nothing server-side and fetches its list — the shape the capture is
    for. It also pings an analytics endpoint that returns a JSON array, which a scorer that only
    looked for "an array of objects" would happily pick instead."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>XHR list</title></head>
<body>
<div id="app">Loading…</div>
<script>
  fetch('{base}/collect', {{method: 'POST', headers: {{'content-type': 'application/json'}},
        body: JSON.stringify({{e: 'pageview'}})}});
  fetch('{base}/api/items?page=1&limit=5')
    .then(r => r.json())
    .then(d => {{
      const app = document.getElementById('app');
      app.innerHTML = '';
      const list = document.createElement('section'); list.id = 'results';
      for (const it of d.results) {{
        const a = document.createElement('article'); a.className = 'card';
        a.innerHTML = `<h3><a href="{base}/item/${{it.id}}">${{it.productName}}</a></h3>` +
                      `<p class="price">£${{it.price}}</p>`;
        list.appendChild(a);
      }}
      app.appendChild(list);
      document.title = 'XHR list – rendered';
    }});
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


# ---------------------------------------------------------------------------------------- shopify
# A faithful-enough fake Shopify store: the same CATALOG, exposed both as a themed storefront and
# through the public JSON endpoints a real store serves (`/products.json` & friends). Prices are
# strings and every product has a `variants` array — the two things that most often surprise a
# scraper moving from HTML to the API.
SHOP_DOMAIN = "fixture-shop.myshopify.com"
SHOP_ID = 61234567
SHOP_CDN = "https://cdn.shopify.com/s/files/1/0612/3456/7/files"
_ISO = "2026-01-%02dT10:%02d:00-05:00"


def _handle(item: dict) -> str:
    return item["title"].lower().replace(" ", "-")


def _variants(item: dict) -> list[dict]:
    """Most products have the single "Default Title" variant; every 5th has two real ones."""
    base_price = float(item["price"].lstrip("£"))
    if item["id"] % 5 == 0:
        return [
            {
                "id": item["id"] * 100 + n,
                "product_id": item["id"],
                "title": name,
                "option1": name,
                "option2": None,
                "option3": None,
                "sku": f"W{item['id']:02d}-{name[:1].upper()}",
                "price": f"{base_price + extra:.2f}",
                "compare_at_price": None,
                "position": n,
                "available": item["in_stock"] and n == 1,
                "grams": 500 * n,
                "requires_shipping": True,
                "taxable": True,
                "featured_image": None,
                "created_at": _ISO % (1, item["id"] % 60),
                "updated_at": _ISO % (2, item["id"] % 60),
            }
            for n, (name, extra) in enumerate([("Small", 0.0), ("Large", 5.0)], start=1)
        ]
    return [
        {
            "id": item["id"] * 100 + 1,
            "product_id": item["id"],
            "title": "Default Title",
            "option1": "Default Title",
            "option2": None,
            "option3": None,
            "sku": f"W{item['id']:02d}",
            "price": f"{base_price:.2f}",
            "compare_at_price": None,
            "position": 1,
            "available": item["in_stock"],
            "grams": 500,
            "requires_shipping": True,
            "taxable": True,
            "featured_image": None,
            "created_at": _ISO % (1, item["id"] % 60),
            "updated_at": _ISO % (2, item["id"] % 60),
        }
    ]


def shopify_product(item: dict) -> dict:
    """One product in the exact shape `/products.json` returns."""
    handle = _handle(item)
    return {
        "id": item["id"],
        "title": item["title"],
        "handle": handle,
        "body_html": f"<p>{escape(item['description'])}</p>",
        "published_at": _ISO % (1, item["id"] % 60),
        "created_at": _ISO % (1, item["id"] % 60),
        "updated_at": _ISO % (2, item["id"] % 60),
        "vendor": "Acme Widgets" if item["id"] % 2 else "Globex",
        "product_type": "Widget",
        "tags": item["tags"],
        "variants": _variants(item),
        "images": [
            {
                "id": item["id"] * 10,
                "product_id": item["id"],
                "position": 1,
                "src": f"{SHOP_CDN}/{handle}.jpg?v=1700000000",
                "width": 800,
                "height": 800,
                "variant_ids": [],
                "created_at": _ISO % (1, item["id"] % 60),
                "updated_at": _ISO % (1, item["id"] % 60),
            }
        ],
        "options": [
            {
                "name": "Size" if item["id"] % 5 == 0 else "Title",
                "position": 1,
                "values": ["Small", "Large"] if item["id"] % 5 == 0 else ["Default Title"],
            }
        ],
    }


def shopify_collection_items(collection: str | None = None) -> list[dict]:
    """What a storefront collection contains. `None`/"all" is the whole catalogue, the way
    /products.json and /collections/all/products.json both are."""
    if collection in (None, "", "all"):
        return CATALOG
    if collection == "gadgets":
        return [i for i in CATALOG if i["id"] % 3 == 0]
    if collection == "private":  # robots.txt forbids this one's endpoint (see server.py)
        return [i for i in CATALOG if i["id"] <= 4]
    return []  # a handle the store does not have


def shopify_products_json(page: int = 1, limit: int = 30, collection: str | None = None) -> dict:
    """`/products.json?limit=&page=` — 1-based pages, empty list past the end (the stop signal).
    Under a collection the same shape carries just that collection's products."""
    limit = max(1, min(limit, 250))  # Shopify caps at 250
    start = (page - 1) * limit
    items = shopify_collection_items(collection)
    return {"products": [shopify_product(i) for i in items[start : start + limit]]}


def shopify_product_json(handle: str) -> dict | None:
    for item in CATALOG:
        if _handle(item) == handle:
            return {"product": shopify_product(item)}
    return None


def shopify_cart_js() -> dict:
    return {
        "token": "fixture-cart-token",
        "note": None,
        "item_count": 0,
        "items": [],
        "currency": "GBP",
    }


def shopify_page(page: int, base: str, collection: str | None = None) -> str:
    """A themed storefront page carrying the markers a detector looks for. A collection page is
    the same theme over a subset — which is exactly why the endpoint under it matters."""
    all_items = shopify_collection_items(collection)
    pages = max(1, -(-len(all_items) // ITEMS_PER_PAGE))
    start = (page - 1) * ITEMS_PER_PAGE
    items = all_items[start : start + ITEMS_PER_PAGE]
    cards = "\n".join(
        f"""
      <div class="grid__item">
        <a class="product-card" href="{base}/products/{_handle(i)}">
          <img src="{SHOP_CDN}/{_handle(i)}.jpg?v=1700000000" alt="{escape(i["title"])}">
          <span class="product-card__title">{escape(i["title"])}</span>
          <span class="price">{i["price"]}</span>
        </a>
      </div>"""
        for i in items
    )
    path = f"{base}/collections/{collection or 'all'}"
    nxt = (
        f'<a class="pagination__next" href="{path}?page={page + 1}">Next</a>'
        if page < pages
        else ""
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{"Widgets" if not collection else collection.title()} – Fixture Shop</title>
<meta name="shopify-checkout-api-token" content="fixturecheckouttoken0000">
<meta id="shopify-digital-wallet" name="shopify-digital-wallet" content="/{SHOP_ID}/digital_wallets/dialog">
<link rel="stylesheet" href="{SHOP_CDN}/theme.css?v=1700000000">
<link rel="canonical" href="{path}">
</head>
<body class="template-collection">
<script>window.Shopify = {{shop: "{SHOP_DOMAIN}", locale: "en", currency: {{active: "GBP", rate: "1.0"}}, theme: {{id: 987654321, name: "Dawn", role: "main"}}}};</script>
<div id="shopify-section-template--17__product-grid" class="shopify-section">
  <h1>{"Widgets" if not collection else collection.title()}</h1>
  <div class="grid product-grid">{cards}
  </div>
  <nav class="pagination">{nxt}</nav>
</div>
<script src="https://cdn.shopify.com/shopifycloud/shopify/assets/storefront/features.js" defer></script>
<script>window.ShopifyAnalytics = window.ShopifyAnalytics || {{}}; ShopifyAnalytics.meta = {{page: {{pageType: "collection"}}}};</script>
</body></html>"""


def shopify_product_page(handle: str, base: str) -> str | None:
    data = shopify_product_json(handle)
    if data is None:
        return None
    p = data["product"]
    v = p["variants"][0]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{escape(p["title"])} – Fixture Shop</title>
<meta name="shopify-checkout-api-token" content="fixturecheckouttoken0000">
<link rel="stylesheet" href="{SHOP_CDN}/theme.css?v=1700000000">
<script type="application/ld+json">{json.dumps({"@context": "https://schema.org", "@type": "Product", "name": p["title"], "sku": v["sku"], "offers": {"@type": "Offer", "price": v["price"], "priceCurrency": "GBP", "availability": "https://schema.org/InStock" if v["available"] else "https://schema.org/OutOfStock"}})}</script>
</head>
<body class="template-product">
<script>window.Shopify = {{shop: "{SHOP_DOMAIN}", locale: "en"}};</script>
<div id="shopify-section-template--17__main" class="shopify-section">
  <h1 class="product__title">{escape(p["title"])}</h1>
  <span class="price">£{v["price"]}</span>
  <div class="product__description">{p["body_html"]}</div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------------------- wordpress
WP_POSTS_PER_PAGE = 4


def wp_post(item: dict) -> dict:
    """WP REST shape: rendered fields are nested objects, dates are local + gmt."""
    return {
        "id": item["id"],
        "date": f"2026-01-{item['id'] % 28 + 1:02d}T09:00:00",
        "date_gmt": f"2026-01-{item['id'] % 28 + 1:02d}T09:00:00",
        "modified": f"2026-02-{item['id'] % 28 + 1:02d}T09:00:00",
        "slug": _handle(item),
        "status": "publish",
        "type": "post",
        "link": f"https://fixture.wp/{_handle(item)}/",
        "title": {"rendered": item["title"]},
        "content": {"rendered": f"<p>{escape(item['description'])}</p>", "protected": False},
        "excerpt": {"rendered": f"<p>{escape(item['description'][:40])}…</p>", "protected": False},
        "author": 1,
        "categories": [2],
        "tags": [],
    }


def wp_posts(page: int = 1, per_page: int = WP_POSTS_PER_PAGE) -> tuple[list[dict], int, int]:
    """(posts, total, total_pages) — the REST API reports totals in X-WP-* headers."""
    per_page = max(1, min(per_page, 100))
    start = (page - 1) * per_page
    total = len(CATALOG)
    pages = (total + per_page - 1) // per_page
    return [wp_post(i) for i in CATALOG[start : start + per_page]], total, pages


def wp_page(base: str) -> str:
    posts = CATALOG[:WP_POSTS_PER_PAGE]
    articles = "\n".join(
        f'  <article class="post"><h2 class="entry-title"><a href="{base}/{_handle(i)}/">{escape(i["title"])}</a></h2>'
        f'<div class="entry-content">{escape(i["description"][:40])}…</div></article>'
        for i in posts
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Fixture Blog</title>
<meta name="generator" content="WordPress 6.7.1">
<link rel="https://api.w.org/" href="{base}/wp-json/">
<link rel="stylesheet" href="{base}/wp-content/themes/twentytwentyfive/style.css">
</head>
<body class="home blog">
<main id="main">
{articles}
</main>
</body></html>"""

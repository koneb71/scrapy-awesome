# API mode: use the site's own JSON instead of its HTML

Many sites hand their catalogue to your browser as JSON. When they do, reading that is better than
parsing the page in every way that matters: a few requests instead of hundreds, typed fields
instead of scraped strings, real pagination, and nothing to re-heal when the theme changes.

scrapy-awesome checks for this automatically. Paste a URL and, if the site is a recognised
platform whose endpoint answers, the recipe is built against the API — with the CSS selectors kept
as fallbacks.

## What happens when you paste a URL

1. **Score, for free.** The page we fetched anyway is scored against each platform: response
   headers, `Set-Cookie`, and body markers. A platform counts as detected at **≥ 6 points with at
   least one signal worth ≥ 4**, so three weak markers never add up — a WordPress site with a
   Shopify buy button embedded is still a WordPress site.
2. **Aim at the page you pasted.** The endpoint is derived from the URL, not just the host:
   `/collections/sale` → `/collections/sale/products.json` (that collection), anything else →
   `/products.json` (the whole catalogue). A store on a sub-path (`/shop/`, `/en-gb/`) keeps its
   sub-path. Both candidates are probed, most specific first, so a store that turns collection
   endpoints off still gets its catalogue — and so does a store whose robots.txt disallows
   `/collections/*/products*` while publishing `/products.json`.
3. **Confirm, cheaply.** Only for platforms that actually expose a catalogue endpoint: robots.txt
   first, then one or two small GETs. Nothing is offered until a real response has been parsed —
   status codes lie (a blocked endpoint answers 404, 403 or 429 with an HTML app shell, and a
   frontend router can answer *200* with HTML), so every probe requires JSON content-type, a
   successful parse, the expected shape, and a final URL that did not redirect away.
4. **Offer or explain.** Confirmed → the recipe switches (New does it for you; the editor shows a
   card with an escape hatch). Refused → the reason is shown and scraping continues on the page.
5. **Remember.** The verdict is cached per page path for 7 days, so the designer, the MCP tools and
   scheduled runs reuse one answer instead of re-probing.

## Platforms

| Platform | Detected by | API used | Notes |
|---|---|---|---|
| **Shopify** | `powered-by: Shopify`, `server-timing: theme;desc=`, `shopify-complexity-score`, `_shopify_*` cookies, `*.myshopify.com` in the body, `shopify-section-`/`ShopifyAnalytics`, `cdn.shopify.com` | `<base>/products.json?limit=250&page=N`, where `<base>` is the collection you pasted or the storefront root (confirmed with `/meta.json` + `<base>/products.json?limit=1`) | up to 250 products/request, walk until the array is empty |
| **WooCommerce** | WordPress + `woocommerce` body class / generator / `wc-*` assets | `/wp-json/wc/store/v1/products?per_page=100&page=N` | the *public* Store API, never the credentialed `wc/v3` admin API |
| **WordPress** | `Link: rel="https://api.w.org/"` (header or `<link>`), `wp-content`, generator | `/wp-json/wp/v2/posts?per_page=100&page=N` | ends the walk with `400 rest_post_invalid_page_number` |
| BigCommerce, Squarespace, Wix, Magento, Webflow | asset hosts and runtime globals | — | named in the UI, but they publish no tokenless catalogue API; scraping stays on the page |

## How an API recipe is shaped

The response body *is* the JSON document, so the ordinary extraction path does the work:

```jsonc
{
  "list": { "container": "json:body.products[*]",     // the item array in the response
            "alternates": ["div.grid__item"] },        // ← your CSS container, kept as fallback
  "api": {
    "url_template": "https://shop.example/products.json?limit={limit}&page={page}",
    "paging": { "kind": "page", "start": 1, "step": 1, "page_size": 250 },
    "explode": null,                                   // "variants" → one row per variant
    "on_error": "html",                                // endpoint dies mid-run → finish with selectors
    "platform": "shopify"
  },
  "fields": [
    { "name": "price", "type": "price", "extract": { "json_path": "$.variants[0].price" },
      "alternates": [{ "css": "span.price::text" }] }, // ← your selector, kept as fallback
    { "name": "url", "type": "url",
      "extract": { "json_path": "$.handle", "template": "https://shop.example/products/{value}" } }
  ]
}
```

Two details worth knowing:

* **Alternates make fallback free.** Both `select_containers` and field extraction try the primary
  and then the alternates, so one recipe reads the API when it answers and parses the page when it
  does not. If the endpoint stops answering mid-run the crawl finishes with the selectors and the
  rows say so (`_provenance: alt:1`).
* **`template` builds values the payload only implies.** `/products.json` carries no product URL,
  so the canonical link is derived from `handle`.

* **Page budgets are re-read for API-sized pages.** `max_pages: 20` / `max_items: 1000` is a sane
  budget for pages of ten; against pages of 250 it would stop the walk at 1,000 products. Switching
  raises *default* budgets to 100 pages / 25,000 items — a budget you chose yourself is left alone.
* **The detail hop is dropped when nothing reads it.** Page analysis usually turns "open each
  item" on; a `/products.json` row already carries every field, so switching turns it back off —
  unless the recipe has detail-scope fields, in which case the row's `url` field is followed.

## Preview reads the API, not the page

The preview gate fetches `api.url_template` (page 1, then page 2 of the API's own paging) — the
same requests the run makes — so what you approve is what you crawl. Three notes it may show, none
of which fail the gate:

| Note | Means |
|---|---|
| `api_last_page` | the second page came back empty — that is how a JSON API says "that is all" |
| `fallback_only_field` | a column still reads the page, not the API; it fills only if the API stops answering |
| `next_link_missing` | there is no next link because the API pages itself |

A column that is genuinely empty most of the time (a sale price) can be marked `sparse: true` in
the field, which turns its emptiness into a note instead of an error. The Shopify
`compare_at_price` field is generated that way.

## Correctness traps this handles (and ones to keep in mind)

* **Price types differ per endpoint.** `/products.json` and GraphQL give string decimals
  (`"91.00"`); the Ajax `.js` endpoints and WooCommerce give integer minor units (`9100`). Mixing
  them inflates prices 100×. The generated recipes only use the string-decimal endpoints.
* **Currency is absent from `/products.json`.** It is read from `/meta.json` (or the theme's
  `Shopify.currency`) and recorded in the recipe notes — never assumed to be USD.
* **Counts will differ from the page when the base is the store root.** `/products.json` lists
  everything published to the online store; a collection page shows one collection. Pasting a
  collection URL keeps you on that collection (`/collections/<handle>/products.json`), so the
  counts line up; falling back to the catalogue is called out in the card before you switch.
* **Variants multiply rows.** Default is one row per product (with `variants[0]` price/sku);
  choose *one row per variant* when you want each size/colour.
* **Sort order is the API's, not the page's** (`published_at` DESC on Shopify), and deep
  pagination of a catalogue that changes mid-crawl can shift pages.
* **Switching modes changes what a column means**, so the first diff after switching will be
  noisy — treat it as a new baseline.

## Etiquette

The endpoints used here are the same public data the site serves any browser, requested the same
way, with no login. scrapy-awesome still obeys robots.txt **per URL** — if the page is allowed but
the endpoint is not (some stores disallow `/collections/*/products*`, and `Disallow: /search`
blocks `/search/suggest.json` by prefix), API mode is refused and the reason is shown. API runs are
deliberately politer per request than HTML runs — one request at a time, ≥ 1 s apart, autothrottled
— and use conditional requests, so a nightly re-run of an unchanged catalogue costs a handful of
304s. Credentialed and admin APIs (`/admin/api/*`, `wc/v3`) are never touched.

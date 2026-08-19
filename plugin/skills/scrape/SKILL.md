---
name: scrape
description: Scrape a website into CSV/JSON/XLSX with the local scrapy-awesome app. Use when the user gives a URL and says what data/fields they want ("get all products with name and price from …", "export the job listings on … to Excel"), wants to follow detail pages, paginate, or re-run/export an existing recipe. Requires the scrapy-awesome MCP server (tools like fetch_page, save_recipe, start_run).
argument-hint: <url> [what to extract, which fields, limits]
---

# /scrape — build, validate, run, export

You are driving **scrapy-awesome**, a local-first scraper. Everything you do happens in the person's own app (a browser tab opens on their machine); crawls are deterministic Scrapy runs and cost **zero tokens** — tokens are only spent while *designing* the recipe against cached sample pages. Be economical: fetch once, read the outline, test a few selectors, save, validate, run.

## Procedure

1. **Understand the ask.** From `$ARGUMENTS` (and the conversation) extract: seed URL(s), the fields wanted (names + rough types), whether detail pages matter (e.g. description, specs → detail), limits (pages/items), output format. If the URL is missing, ask. Otherwise **do not** ask clarifying questions you can answer by looking at the page.
2. **Check for an API first.** `fetch_page(url)` → if `analysis.platform.api_available` is true, the
   site publishes its catalogue as JSON (Shopify `/products.json`, WooCommerce/WordPress REST, …).
   Save a first recipe, then `use_platform_api(page_id, recipe_id)` — far fewer requests, typed
   fields, real pagination, and the CSS selectors stay as fallbacks. Use `granularity="variant"`
   only when the person wants a row per size/colour. If `platform` is present but
   `api_available` is false, mention `why_not` in one clause and scrape the page as usual.
3. **Fetch & look.** `fetch_page(url)` → read `analysis` (best `containers`, `fields` guesses with examples, `pagination`, `detail_link`, `json_list_paths`, `login_hint`, `notes`) and `outline` (a folded DOM — read selectors straight off it). If `blocked` is true or the outline is an empty app shell, retry with `tier: "browser"` (real Chrome); if it needs scrolling/clicking, `tier: "interactive"`.
4. **Confirm selectors, cheaply.**
   - Per-item fields: `test_selector(page_id, selector, container=<container>)` → look at `fill_rate` and `values`. Aim for ≥ 0.9 fill on required fields.
   - Know a value but not its element? `search_page(page_id, "£11.50")` returns the element and its **relative** selector inside the container.
   - Data in embedded JSON (`__NEXT_DATA__`, `ld+json`)? `list_json_blobs(page_id)` then a `json_path` extractor — more robust than DOM selectors.
   - Prefer semantic selectors (`.price`, `[itemprop=name]`, `h3 a`) over positional ones (`div:nth-child(3)`).
   - Ambiguous, or the person can see the page better than you? `request_pick("click the price of the first product")` — one click beats three wrong guesses.
5. **Save.** `save_recipe({...})` — see the schema below; put the person's words in `intent`; give a short human `name`. Keep the field list to what was asked (plus `url`/detail link when following details). It returns `ready` + `readiness_errors`.
6. **Validate.** `validate_recipe(recipe_id)` fetches page 1, page 2 (via pagination) and two detail pages, then extracts in-process. Read `ok`, per-field `fill_rate`/`distinct`, `issues`, `pagination.next_found`, `detail`. Fix and repeat (edit selectors → `save_recipe(recipe, recipe_id=…)`) until it passes. Show the person 3–5 preview rows.
7. **Run.** `start_run(recipe_id, max_pages=…)` (a small trial first when the site is unknown), then `run_status(run_id, wait_seconds=60)` for short crawls, or hand back and let them watch at the `ui` link for long ones. `get_rows` to sanity-check.
8. **Export.** `export_run(run_id, "csv"|"xlsx"|"json"|"jsonl", dest="~/Downloads/…")` and tell them the path. Mention the recipe is saved (`open_ui("/recipes/<id>")`) and can be re-run or scheduled from the app.

## Recipe shape (JSON)

```json
{
  "name": "Books – toscrape",
  "seeds": ["https://books.toscrape.com/"],
  "intent": "all books with title, price, rating; open each book for the description",
  "fetch": {"tier": "auto"},
  "list": {"container": "article.product_pod"},
  "detail": {"enabled": true, "link": {"css": "h3 a"}},
  "pagination": {"kind": "next_link", "selector": "li.next a", "max_pages": 50},
  "fields": [
    {"name": "title", "type": "text", "scope": "list", "extract": {"css": "h3 a", "attr": "title"}, "required": true},
    {"name": "price", "type": "price", "scope": "list", "extract": {"css": ".price_color::text"}},
    {"name": "rating", "type": "enum", "enum": ["One","Two","Three","Four","Five"], "extract": {"css": "p.star-rating::attr(class)", "regex": "star-rating (\\w+)"}},
    {"name": "description", "type": "text", "scope": "detail", "extract": {"css": "#product_description ~ p::text"}}
  ],
  "limits": {"max_pages": 50, "max_items": 2000, "download_delay": 0.5}
}
```

- Field `type`: `text | number | price | date | url | image | bool | enum | list | json`. `scope`: `list` (from the listing item) or `detail` (from the followed page). Exactly one of `extract.css | xpath | json_path`; optional `attr`, `regex` (first group), `all: true` for lists.
- `pagination.kind`: `next_link | url_template | load_more | infinite_scroll | xhr_json | none` (`url_template` uses `{page}`; `load_more`/`infinite_scroll` need `fetch.tier: "interactive"`; `xhr_json` pages a JSON API via `xhr_url_template` + `xhr_items_path`).
- Page-level (single page, no list): omit `list`, use `scope: "page"` fields.
- Call `recipe_schema()` if unsure about a key.

## Rules

- Never invent values — every field comes from a selector or JSON path the tools confirmed.
- Respect the person's scope: only the site(s) and fields they asked for; keep default politeness (robots.txt on, delay ≥ 0.5 s) unless they explicitly change it in the app.
- Login-gated pages: don't ask for credentials. Tell them to add a login session in the app (Sessions → "Log in once") and set `fetch.session` to it.
- Don't dump huge outputs into the chat: summarize validation, show a handful of rows, give file paths and UI links.

# Recipe format (v1)

A recipe is plain YAML/JSON validated by `scrapy_awesome.recipe.models.Recipe`. The generic
`RecipeSpider` interprets it; the UI and the LLM designer edit it. Everything is JSON-serializable.

```yaml
version: 1
name: Books to Scrape
seeds: ["https://books.toscrape.com/"]
intent: Every book with title, price, rating; follow to detail for description
page_type: list                 # list (repeated container) | single (one item per page)
fetch:
  tier: auto                    # auto | http | browser | interactive
  profile: chrome               # scrapy-stealth fingerprint profile
  proxy: null
  session: null                 # login session id → storage_state, forces interactive tier
  actions: []                   # interactive tier only, see below
  wait_for: null                # shortcut: wait for a selector (interactive)
  settle_seconds: null          # browser tier settle time
  timeout_seconds: 30
  block_static_assets: true
list:
  container: article.product_pod         # CSS or XPath (XPath starts with / ( . or //)
  alternates: ["//article"]              # tried in order when the primary matches nothing
  min_items: 1
detail:
  enabled: true
  link: { css: "h3 a" }                  # bare element → href is taken automatically
  max_concurrency: 4
  fetch: null                            # optional per-detail fetch override
source:                                  # where the crawl starts (docs/sources-and-datasets.md)
  kind: seeds                            # seeds | urls | sitemap
  urls: []                               # urls: the list to crawl, one row per page
  sitemap: null                          # sitemap: empty = /sitemap.xml, then robots.txt
  include: null                          # regex a URL must match
  exclude: null
  max_urls: 1000
incremental:                             # re-run only what changed
  enabled: false
  refetch_after_days: 30
pagination:
  kind: next_link                        # none | next_link | url_template | load_more | infinite_scroll | xhr_json
  selector: "li.next a"                  # next_link / load_more
  url_template: null                     # "https://x/?page={page}"
  start: 1
  step: 1
  max_pages: 50
  stop_when_no_new_items: true
fields:
  - name: title                          # snake_case identifier
    type: text                           # text|number|price|date|url|image|bool|enum|list|json
    scope: list                          # list | detail | page
    required: true
    sparse: false                        # true = usually empty (a sale price); empty is a note, not an error
    extract: { css: "h3 a", attr: title }
    transforms:                          # applied in order, before the type is read
      - { kind: strip_prefix, value: "Title: " }
      - { kind: collapse_space }
    alternates: [{ css: "h3::text" }]
    examples: ["A Light in the Attic"]
  - { name: price, type: price, extract: { css: ".price_color::text" } }
  - { name: rating, type: enum, enum: [One, Two, Three, Four, Five],
      extract: { css: "p.star-rating::attr(class)", regex: "star-rating (\\w+)" } }
  - { name: description, scope: detail, extract: { css: "#product_description ~ p::text" } }
  - { name: summary, scope: detail, extract: { llm: "One-sentence summary" } }   # AI field
dedupe_key: [_url]              # implicit fields: _url _page_url _fetched_at _tier _provenance
limits: { max_pages: 20, max_items: 1000, download_delay: 0.5, concurrency_per_domain: 4,
          per_run_llm_budget_usd: 1.0 }
fallback: { llm_enabled: true, only_missing_fields: true }
```

## Extractors

Exactly one source: `css` | `xpath` | `json_path` | `llm`. Modifiers: `attr` (attribute instead of
text), `regex` (first capture group, else whole match), `all` (every match → list).

* `css: "h3 a::attr(title)"` and `css: "h3 a", attr: title` are equivalent.
* A bare element selector returns its normalized `string(.)` text.
* `json_path` walks embedded JSON blobs (`__NEXT_DATA__`, any `<script type="application/json" id>`,
  `ld+json`, `window.__NUXT__ = {...}` …): `props.pageProps.products[*].name`.
* A **JSON list container** looks like `container: "json:__NEXT_DATA__.props.pageProps.products[*]"`;
  fields then use `json_path` relative to each item.
* `llm` fields are filled at run time by the LLM fallback (budgeted); never by selectors.

## Interactive actions

```yaml
fetch:
  actions:
    - { kind: wait_for, selector: "article" }
    - { kind: scroll_until_stable, max_rounds: 40, ms: 250 }
    - { kind: click, selector: "button.load-more", times: 5, optional: true, ms: 500 }
    - { kind: wait_ms, ms: 500 }
    - { kind: fill, selector: "#q", value: "shoes" }
    - { kind: press, selector: "#q", value: "Enter" }
    - { kind: evaluate, js: "window.scrollTo(0, 0)" }
```

Any action, `wait_for` or `session` forces the interactive tier (Playwright/Patchright).

## Tiers and escalation

`tier: auto` starts at `http` (scrapy-stealth, TLS-impersonated), escalates to `browser`
(scrapy-stealth's real Chrome) on blocks/JS-only pages, then to `interactive` (Patchright). The
tier that worked is remembered per domain for the rest of the run. Explicit tiers never escalate.

## Resume compatibility

Editing field extractors/alternates/examples/limits/fallback keeps an existing JOBDIR usable;
changing `seeds`, `list`, `pagination`, `detail.link`, `detail.enabled`, `fetch` or `page_type`
requires a new run (`recipe/compat.py`).

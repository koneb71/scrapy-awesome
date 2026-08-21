# Where a crawl starts, what it re-fetches, and what it keeps

Three features that go together: a crawl needs somewhere to begin, a re-run should not redo work,
and what you actually want at the end is rarely "the rows from run #47".

## Where it starts — `source`

| `source.kind` | What it does | When |
|---|---|---|
| `seeds` (default) | walks `seeds` and their pagination | there is a list page |
| `urls` | crawls a list you already have, one row per page | 300 product URLs in a spreadsheet |
| `sitemap` | reads the site's own index of everything it publishes | better coverage than pagination, and the only place most sites say *when* a page changed |

```yaml
source:
  kind: sitemap
  sitemap: null            # empty = <seed origin>/sitemap.xml, then whatever robots.txt names
  include: "/product/"     # regex a URL must match
  exclude: "/tag/|/page/"  # ...and one that rules it out
  max_urls: 1000
```

A sitemap index is followed one level down (up to 50 child sitemaps, `.xml.gz` included). A missing
or empty sitemap is not the end: `robots.txt` usually names the real one, and that is tried before
giving up. Pasted URL lists are split on whitespace and commas, de-duplicated, and stripped of
stray quotes and angle brackets, so pasting out of a spreadsheet or an email just works.

A `urls`/`sitemap` crawl usually wants `page_type: single` with **page**-scope fields: each URL is
one row. The pasted list is also the page budget — 300 URLs is 300 pages, whatever `max_pages` said
back when the recipe walked a list page.

Preview resolves the source the same way the spider does, so the pages it samples are the pages the
run will fetch.

## What it re-fetches — `incremental`

```yaml
incremental:
  enabled: true
  refetch_after_days: 30   # look again eventually, however quiet the site claims to be
```

Two ways a site can say *don't bother*:

1. **A sitemap `lastmod` we have already crawled** — the page is not requested at all.
2. **`304 Not Modified`** — the request carries the `ETag` / `Last-Modified` from last time, and the
   answer costs a round trip and no body. (A byte-identical body counts too, for servers that
   validate nothing.)

Either way the page is not parsed and emits no rows, and the run reports `skipped`. A 304 is never
treated as a block — an empty body is the *point* here, and escalating to a browser would turn the
cheapest possible answer into the most expensive one.

Diffs know about this: an incremental run's diff reports **new and changed only**, because a page
nobody looked at is not a page that disappeared.

## What it keeps — the dataset

Runs are episodes. The **Dataset** tab is the other shape: one row per item, across every run.

- `first_seen`, `last_seen`, `last_changed`, `changes`, `runs`
- the last 20 changes per row, field by field (`price: 12.00 → 11.50`)
- `gone` — the last **full** run did not find it. An incremental run never marks anything gone, for
  the same reason its diffs don't.

Rows are keyed by the recipe's `dedupe_key` (default `_url`). "Start over" clears the dataset and
leaves every run untouched.

```
GET    /api/recipes/{id}/dataset?limit=200&include_gone=false&changed_days=7
GET    /api/recipes/{id}/dataset/history?key=…
DELETE /api/recipes/{id}/dataset
```

## Field transforms

The long tail of scraping is not "I cannot find the value", it is "the value is nearly right".
Transforms run in order on the raw string, **before** the field's type is read:

```yaml
- name: price
  type: price
  extract: { css: "span.p" }
  transforms:
    - { kind: strip_prefix, value: "Price: " }
    - { kind: decimal_comma }        # 1.234,56 → 1234.56
```

`trim · collapse_space · lower · upper · title · strip_prefix · strip_suffix · replace ·
regex_replace · split · prepend · append · decimal_comma · digits · default`

`split` without an index fans one value into many (a tag list); with an index it keeps one piece. A
transform that cannot apply returns the value untouched — a tidy-up is never a reason to lose a
row — and an unusable one (a regex that will not compile) is refused when the recipe is saved.

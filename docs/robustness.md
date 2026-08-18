# Robustness: what happens when a site changes or a page won't extract

Crawls are deterministic, but sites drift. scrapy-awesome layers four mechanisms, cheapest first,
and reports each of them so you stay in control.

## 1. Alternates (free)

Every field can carry `alternates` — extra selectors tried in order when the primary yields
nothing (`provenance = alt:<i>` on the row). Healing (below) writes the old primary here.

## 2. Self-healing selectors (free, in the worker)

At validation time (`Preview`, `validate_recipe`) the app fingerprints the element each list field
matched — tag, stable classes, semantic attributes, path inside the item, text shape and a coarse
*value shape* (`£11.50` → `£0.0`). The fingerprints are stored on the recipe row (derived data,
not a version).

During a run, if a fingerprinted field's fill rate collapses on a page (≤ 25 % while the container
still matches ≥ 3 items), the worker scans the item containers for the most similar element,
derives a short relative selector, checks it fills across the page (≥ 60 %), and swaps it in for
the rest of the run (old primary kept as an alternate). It emits `healed {field, old, new, fill,
examples}`; the run page shows a banner with **Apply to recipe** (creates a new recipe version).
A field that cannot be relocated emits `heal_failed` and triggers the drift notification.

Limits: list-scope css/xpath fields only; one attempt per field per run; the redesign must keep
*some* signal (tag/classes/attrs, or the same value shape).

## 3. Drift signals (free)

Workers emit per-page `fill` rates per field. The run page shows a sparkline per field; when a
field that used to fill ≥ 80 % drops to ≤ 25 % (and no heal fixed it) you get a notification
("Field stopped filling …") pointing at the run. Final `stats.fill_history` is kept per run.

## 4. Failed pages → LLM fallback or agent hand-off (paid / your agent)

If a list page yields **no items** or a detail page fills **nothing**, the worker saves the HTML
(`runs/<id>/failed/`, ≤ 50 per run) and reports `page_failed`. The server then:

* with `recipe.fallback.llm_enabled` (default) **and** a key for the *fallback* role: turns the
  page into markdown and asks the fallback model for the recipe's fields as JSON (structured
  output). Rows are appended with `_provenance = llm` per field and appear live; cost is charged
  against `limits.per_run_llm_budget_usd` and shown in the run stats (`stats.llm`).
* otherwise the pages stay `pending`/`failed` and any MCP agent (Claude Code / Gemini CLI) can
  recover them: `get_failed_pages(run_id)` → markdown + field spec, `submit_rows(run_id, page_id,
  rows)` → `_provenance = agent`. The run page has **Extract with AI now** for pages that were
  skipped/failed (e.g. after adding a key).

## 5. AI fields (paid, deterministic post-processing)

A field with `extract: {llm: "instruction"}` is computed **after** the crawl by the fallback model,
20 rows per call, from the row's own scraped fields only, under the same per-run budget;
provenance `llm`. Use them for summaries/normalisation, not for data a selector can reach.

## Provenance

Every row carries `_provenance: {field: primary | alt:<i> | missing | llm | agent | llm_pending}`;
exports include it (`include_meta`). The run grid badges rows extracted by **AI** or an **agent**.

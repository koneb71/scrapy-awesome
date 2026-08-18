"""AI fields: recipe fields whose extractor is `llm: "<instruction>"` — computed per row by the
*fallback* model after the crawl, in batches, under the run's LLM budget, with provenance `llm`.

They complement selectors (which are free and deterministic) for things a selector cannot do:
"one-line summary", "sentiment of the review", "brand normalised to a canonical name". Every
value is derived only from the row's own scraped fields, so a run stays reproducible.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from scrapy_awesome.llm.base import Budget, LLMError, LLMProvider, Usage
from scrapy_awesome.llm.registry import make_provider
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store import Store

log = logging.getLogger(__name__)

BATCH = 20
SYSTEM = (
    "You compute additional fields for scraped rows. For every input row (identified by `i`) "
    "return the requested fields, derived ONLY from that row's data. Follow each field's "
    "instruction and type. Use null when the data does not support a value. Never invent facts."
)


def _schema(recipe: Recipe) -> dict[str, Any]:
    props: dict[str, Any] = {"i": {"type": "integer"}}
    for f in recipe.llm_fields:
        t = {"number": "number", "price": "number", "bool": "boolean", "list": "array"}.get(
            f.type, "string"
        )
        prop: dict[str, Any] = {"type": [t, "null"], "description": f.extract.llm or ""}
        if t == "array":
            prop["items"] = {"type": "string"}
        if f.enum:
            prop.pop("type")
            prop["enum"] = [*f.enum, None]
        props[f.name] = prop
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": ["i", *[f.name for f in recipe.llm_fields]],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["rows"],
        "additionalProperties": False,
    }


def _needs(recipe: Recipe, row: dict[str, Any]) -> bool:
    prov = row.get("_provenance") or {}
    return any(
        row.get(f.name) in (None, "", []) or prov.get(f.name) == "llm_pending"
        for f in recipe.llm_fields
    )


async def compute_ai_fields(
    *,
    store: Store,
    bus: Any,
    recipe: Recipe,
    run_id: str,
    provider: LLMProvider,
    model: str,
    budget: Budget,
    only_missing: bool = True,
) -> dict[str, Any]:
    fields = recipe.llm_fields
    if not fields:
        return {"rows": 0, "calls": 0, "cost_usd": 0.0, "skipped": "no llm fields"}
    schema = _schema(recipe)
    spec = "\n".join(f"- {f.name} ({f.type}): {f.extract.llm}" for f in fields)
    visible = [f.name for f in recipe.fields if f.extract.source != "llm"]
    total = Usage()
    updated = 0
    batch: list[tuple[int, dict[str, Any]]] = []

    async def flush() -> None:
        nonlocal updated
        if not batch:
            return
        payload = [{"i": n, **{k: r.get(k) for k in visible}} for n, r in batch]
        prompt = f"Fields to compute:\n{spec}\n\nRows (JSON):\n{json.dumps(payload, default=str)[:60000]}"
        values, usage = await provider.extract_json(
            model=model, system=SYSTEM, prompt=prompt, schema=schema, budget=budget
        )
        total.add(usage)
        by_i = {
            int(x["i"]): x for x in (values.get("rows") or []) if isinstance(x, dict) and "i" in x
        }
        changes: dict[int, dict[str, Any]] = {}
        for n, r in batch:
            got = by_i.get(n)
            if not got:
                continue
            row = dict(r)
            prov = dict(row.get("_provenance") or {})
            for f in fields:
                if f.name in got:
                    row[f.name] = got[f.name]
                    prov[f.name] = "llm"
            row["_provenance"] = prov
            changes[n] = row
        updated += store.update_items(run_id, changes)
        for n, row in changes.items():
            bus.publish(run_id, {"t": "item_update", "run_id": run_id, "n": n, "row": row})
        batch.clear()

    try:
        for n, row in store.iter_item_rows(run_id):
            if only_missing and not _needs(recipe, row):
                continue
            batch.append((n, row))
            if len(batch) >= BATCH:
                await flush()
        await flush()
    except LLMError as exc:
        return {
            "rows": updated,
            "calls": total.calls,
            "cost_usd": total.cost_usd,
            "error": str(exc),
        }
    return {"rows": updated, "calls": total.calls, "cost_usd": round(total.cost_usd, 6)}


async def run_ai_fields_for(
    *, store: Store, bus: Any, paths: Any, settings: Any, run_id: str, provider_factory: Any = None
) -> dict[str, Any]:
    """Entry used by the finish hook and the API: resolves recipe/provider/budget for a run."""
    run = store.get_run(run_id)
    if not run:
        raise KeyError(run_id)
    recipe = store.get_recipe(run.recipe_id) if run.recipe_id else None
    if recipe is None or not recipe.llm_fields:
        return {"rows": 0, "calls": 0, "cost_usd": 0.0, "skipped": "no llm fields"}
    factory = provider_factory or (lambda name: make_provider(name, paths))
    role = settings.llm.fallback
    provider = factory(role.provider)  # raises LLMError without a key
    st = dict(run.stats or {})
    llm = dict(st.get("llm") or {"pages": 0, "rows": 0, "cost_usd": 0.0, "skipped": 0})
    limit = float(recipe.limits.per_run_llm_budget_usd or 0)
    budget = Budget(limit_usd=(limit - float(llm.get("cost_usd", 0.0))) if limit else None)
    out = await compute_ai_fields(
        store=store,
        bus=bus,
        recipe=recipe,
        run_id=run_id,
        provider=provider,
        model=role.model,
        budget=budget,
    )
    llm["cost_usd"] = round(float(llm.get("cost_usd", 0.0)) + float(out.get("cost_usd", 0.0)), 6)
    llm["ai_field_rows"] = int(llm.get("ai_field_rows", 0)) + int(out.get("rows", 0))
    st["llm"] = llm
    store.update_run(run_id, stats=st)
    return out

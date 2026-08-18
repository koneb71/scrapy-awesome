import type { Analysis, Extractor, Field, FieldType, Recipe } from "./types";

export function slugify(s: string): string {
  const base = s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^[0-9]/, "f$&");
  return (base || "field").slice(0, 64);
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function newRecipe(url: string, intent = ""): Recipe {
  return {
    version: 1,
    name: hostOf(url) || "Untitled recipe",
    seeds: [url],
    intent,
    page_type: "list",
    fetch: { tier: "auto", profile: "chrome", actions: [], block_static_assets: true, timeout_seconds: 30 },
    list: null,
    detail: { enabled: false, link: null, max_concurrency: 4 },
    pagination: { kind: "none", max_pages: 20, start: 1, step: 1, stop_when_no_new_items: true },
    fields: [],
    dedupe_key: ["_url"],
    limits: { max_pages: 20, max_items: 1000, download_delay: 0.5, concurrency_per_domain: 4, per_run_llm_budget_usd: 1 },
    fallback: { llm_enabled: true, only_missing_fields: true },
  };
}

export function emptyField(name = "field", scope: Field["scope"] = "list"): Field {
  return { name, type: "text", scope, extract: { css: "" }, required: false, examples: [] };
}

export function extractorLabel(e: Extractor): string {
  const src = e.css ?? e.xpath ?? (e.json_path ? `json:${e.json_path}` : e.llm ? `AI: ${e.llm}` : "");
  return `${src}${e.attr ? ` @${e.attr}` : ""}${e.regex ? ` ~/${e.regex}/` : ""}`;
}

/** Turn an Analysis into a starting recipe (container, fields, detail link, pagination). */
export function applyAnalysis(base: Recipe, a: Analysis): Recipe {
  const r: Recipe = JSON.parse(JSON.stringify(base));
  const usedNames = new Set<string>();
  const uniq = (n: string) => {
    let name = slugify(n);
    let i = 2;
    while (usedNames.has(name)) name = `${slugify(n)}_${i++}`;
    usedNames.add(name);
    return name;
  };
  if (a.json_list_paths?.length) {
    r.page_type = "list";
    r.list = { container: a.json_list_paths[0].container, alternates: [] };
    r.fields = a.json_list_paths[0].keys.slice(0, 12).map((k) => ({
      name: uniq(k),
      type: guessTypeFromKey(k),
      scope: "list",
      extract: { json_path: k },
      examples: [],
    }));
    r.detail = { enabled: false, link: null, max_concurrency: 4 };
  } else if (a.page_type === "list" && a.containers.length) {
    r.page_type = "list";
    r.list = { container: a.containers[0].selector, alternates: a.containers.slice(1, 3).map((c) => c.selector) };
    r.fields = a.fields.map((f) => ({
      name: uniq(f.name),
      type: f.type as FieldType,
      scope: "list",
      extract: { css: f.selector, attr: f.attr ?? undefined },
      examples: f.examples,
      required: f.name === "title",
    }));
    if (a.detail_link && a.detail_link.same_host >= 0.5) {
      r.detail = { enabled: true, link: { css: a.detail_link.selector }, max_concurrency: 4 };
    }
  } else {
    r.page_type = "single";
    r.list = null;
    r.fields = [{ name: "title", type: "text", scope: "page", extract: { css: "title::text" }, examples: [] }];
  }
  const pg = a.pagination.find((p) => p.kind === "next_link") ?? a.pagination.find((p) => p.kind === "url_template");
  if (pg) {
    r.pagination = { ...r.pagination, kind: pg.kind, selector: pg.selector ?? undefined, url_template: pg.url_template ?? undefined };
  }
  if (r.fields.length === 0) r.fields = [emptyField("title", r.page_type === "single" ? "page" : "list")];
  return r;
}

function guessTypeFromKey(k: string): FieldType {
  const s = k.toLowerCase();
  if (/price|amount|cost/.test(s)) return "price";
  if (/^(url|link|href)$|_url$/.test(s)) return "url";
  if (/image|img|thumbnail|photo/.test(s)) return "image";
  if (/date|time|created|updated|published/.test(s)) return "date";
  if (/^(is_|has_)|in_stock|available/.test(s)) return "bool";
  if (/count|qty|quantity|rating|score|number|id$/.test(s)) return "number";
  if (/tags|categories|images|authors/.test(s)) return "list";
  return "text";
}

export function fieldTypes(): FieldType[] {
  return ["text", "number", "price", "date", "url", "image", "bool", "enum", "list", "json"];
}

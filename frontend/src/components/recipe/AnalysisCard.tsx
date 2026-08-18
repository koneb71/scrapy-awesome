import type { Analysis, Recipe, Sample } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { applyAnalysis } from "@/lib/recipe";

export function TierBadge({ tier }: { tier?: string | null }) {
  const color =
    tier === "http" ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200"
    : tier === "browser" ? "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200"
    : tier === "interactive" ? "bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200"
    : "bg-muted text-muted-foreground";
  return <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ${color}`}>{tier ?? "—"}</span>;
}

export function AnalysisCard({
  sample,
  recipe,
  onChange,
}: {
  sample: Sample;
  recipe: Recipe;
  onChange: (r: Recipe) => void;
}) {
  const a: Analysis | null | undefined = sample.analysis;
  if (!a) return <p className="text-sm text-muted-foreground">No analysis for this page yet.</p>;
  const setContainer = (sel: string) => onChange({ ...recipe, page_type: "list", list: { ...(recipe.list ?? {}), container: sel } });
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base flex items-center gap-2">
            Page <TierBadge tier={sample.tier} />
            <Badge variant="outline">{a.page_type}</Badge>
          </CardTitle>
          <CardDescription className="truncate">{a.title || sample.final_url}</CardDescription>
        </CardHeader>
        <CardContent className="text-sm space-y-2">
          <div className="text-muted-foreground">
            {a.text_length.toLocaleString()} chars of text · {a.script_count} scripts · status {sample.status}
            {sample.verdict?.reason ? ` · ${sample.verdict.reason}` : ""}
          </div>
          {a.notes.map((n, i) => (
            <div key={i} className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-100">{n}</div>
          ))}
          <div className="pt-2">
            <Button size="sm" variant="secondary" onClick={() => onChange(applyAnalysis(recipe, a))}>
              Rebuild recipe from this analysis
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Repeated containers</CardTitle>
          <CardDescription>The element that repeats once per item.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {a.containers.length === 0 && <div className="text-muted-foreground">None found (single page?).</div>}
          {a.containers.map((c) => (
            <div key={c.selector} className="flex items-center justify-between gap-2 rounded border px-2 py-1.5">
              <div className="min-w-0">
                <code className="text-xs">{c.selector}</code>
                <div className="text-xs text-muted-foreground truncate">{c.count} items · “{c.sample[0]?.slice(0, 60)}”</div>
              </div>
              <Button size="sm" variant={recipe.list?.container === c.selector ? "default" : "outline"} onClick={() => setContainer(c.selector)}>
                {recipe.list?.container === c.selector ? "In use" : "Use"}
              </Button>
            </div>
          ))}
          {a.json_list_paths.map((j) => (
            <div key={j.container} className="flex items-center justify-between gap-2 rounded border border-dashed px-2 py-1.5">
              <div className="min-w-0">
                <code className="text-xs">{j.container}</code>
                <div className="text-xs text-muted-foreground truncate">JSON · {j.count} items · keys: {j.keys.slice(0, 6).join(", ")}</div>
              </div>
              <Button size="sm" variant="outline" onClick={() => setContainer(j.container)}>Use</Button>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Pagination & detail pages</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {a.pagination.length === 0 && <div className="text-muted-foreground">No pagination detected.</div>}
          {a.pagination.map((p, i) => (
            <div key={i} className="flex items-center justify-between gap-2 rounded border px-2 py-1.5">
              <div className="min-w-0">
                <Badge variant="secondary" className="mr-1">{p.kind}</Badge>
                <code className="text-xs">{p.selector ?? p.url_template}</code>
                <div className="text-xs text-muted-foreground">{p.evidence}</div>
              </div>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  onChange({ ...recipe, pagination: { ...recipe.pagination, kind: p.kind, selector: p.selector ?? undefined, url_template: p.url_template ?? undefined } })
                }
              >
                Use
              </Button>
            </div>
          ))}
          {a.detail_link ? (
            <div className="flex items-center justify-between gap-2 rounded border px-2 py-1.5">
              <div className="min-w-0">
                <div className="text-xs">Detail link: <code>{a.detail_link.selector}</code></div>
                <div className="text-xs text-muted-foreground truncate">{a.detail_link.sample[0]}</div>
              </div>
              <Button size="sm" variant={recipe.detail.enabled ? "default" : "outline"} onClick={() => onChange({ ...recipe, detail: { ...recipe.detail, enabled: true, link: { css: a.detail_link!.selector } } })}>
                {recipe.detail.enabled ? "Following" : "Follow"}
              </Button>
            </div>
          ) : (
            <div className="text-muted-foreground">No per-item link detected.</div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Suggested fields</CardTitle>
          <CardDescription>Inside the best container. Add the ones you want.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-1 text-sm">
          {a.fields.map((f) => {
            const present = recipe.fields.some((x) => x.extract.css === f.selector && (x.extract.attr ?? null) === f.attr);
            return (
              <div key={f.selector + f.attr} className="flex items-center justify-between gap-2 rounded border px-2 py-1">
                <div className="min-w-0">
                  <span className="font-medium">{f.name}</span> <Badge variant="outline" className="text-[10px]">{f.type}</Badge>{" "}
                  <code className="text-xs">{f.selector}{f.attr ? ` @${f.attr}` : ""}</code>
                  <div className="text-xs text-muted-foreground truncate">{f.examples.slice(0, 2).join(" · ")}</div>
                </div>
                <Button
                  size="sm"
                  variant={present ? "secondary" : "outline"}
                  disabled={present}
                  onClick={() =>
                    onChange({
                      ...recipe,
                      fields: [...recipe.fields, { name: uniqueName(recipe, f.name), type: f.type, scope: "list", extract: { css: f.selector, attr: f.attr ?? undefined }, examples: f.examples }],
                    })
                  }
                >
                  {present ? "Added" : "Add"}
                </Button>
              </div>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}

function uniqueName(recipe: Recipe, base: string): string {
  const names = new Set(recipe.fields.map((f) => f.name));
  if (!names.has(base)) return base;
  let i = 2;
  while (names.has(`${base}_${i}`)) i++;
  return `${base}_${i}`;
}

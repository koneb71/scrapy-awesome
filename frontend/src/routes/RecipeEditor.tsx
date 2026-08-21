import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, History, Loader2, Play, Save, Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import type { Recipe, RecipeRow, Sample, ValidationReport } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { AnalysisCard } from "@/components/recipe/AnalysisCard";
import { PlatformCard } from "@/components/recipe/PlatformCard";
import { XhrCard } from "@/components/recipe/XhrCard";
import { FieldsTab } from "@/components/recipe/FieldsTab";
import { PreviewTab } from "@/components/recipe/PreviewTab";
import { PlanTab } from "@/components/recipe/PlanTab";
import { DatasetTab } from "@/components/recipe/DatasetTab";
import { PickerDialog, type PickTarget } from "@/components/recipe/PickerDialog";
import { Skeleton } from "@/components/ui/skeleton";
import { ChatPanel } from "@/components/chat/ChatPanel";

function VersionsPanel({ id, current, onRolledBack }: { id: string; current: number; onRolledBack: (r: RecipeRow) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["recipe-versions", id, current], queryFn: () => api.recipeVersions(id) });
  const rollback = useMutation({
    mutationFn: (v: number) => api.rollback(id, v),
    onSuccess: (r) => {
      toast.success(`Restored as v${r.version}`);
      qc.invalidateQueries({ queryKey: ["recipe-versions", id] });
      onRolledBack(r);
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const rows = q.data ?? [];
  return (
    <div className="space-y-2 text-sm max-w-3xl">
      <p className="text-muted-foreground text-xs">Every save creates a version. Restoring an old one creates a new version (nothing is lost). Runs record which version they used.</p>
      {rows.map((v, i) => {
        const prev = rows[i + 1];
        const changed = prev ? diffFields(prev.recipe, v.recipe) : [];
        return (
          <div key={v.version} className="flex items-start gap-3 rounded border px-3 py-2">
            <Badge variant={v.version === current ? "default" : "outline"}>v{v.version}</Badge>
            <div className="min-w-0 flex-1">
              <div className="text-xs text-muted-foreground">{new Date(v.created_at).toLocaleString()}{v.note ? ` · ${v.note}` : ""}</div>
              <div className="text-xs">{changed.length ? `changed: ${changed.join(", ")}` : i === rows.length - 1 ? "initial version" : "no field-level changes"}</div>
              <div className="text-xs text-muted-foreground truncate">{v.recipe.fields.map((f) => f.name).join(", ")}</div>
            </div>
            {v.version !== current && <Button size="sm" variant="outline" onClick={() => rollback.mutate(v.version)} disabled={rollback.isPending}>Restore</Button>}
          </div>
        );
      })}
      {q.isLoading && <div className="text-muted-foreground">Loading…</div>}
    </div>
  );
}

function diffFields(a: Recipe, b: Recipe): string[] {
  const out: string[] = [];
  const keys: (keyof Recipe)[] = ["name", "seeds", "list", "detail", "pagination", "fetch", "limits", "dedupe_key"];
  for (const k of keys) if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) out.push(String(k));
  const fa = new Map(a.fields.map((f) => [f.name, JSON.stringify(f)]));
  const fb = new Map(b.fields.map((f) => [f.name, JSON.stringify(f)]));
  for (const [n, s] of fb) if (fa.get(n) !== s) out.push(fa.has(n) ? `field ${n}` : `+${n}`);
  for (const n of fa.keys()) if (!fb.has(n)) out.push(`−${n}`);
  return out;
}

export default function RecipeEditor() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const loc = useLocation() as { pathname: string; state?: { sampleId?: string; design?: string | null } };
  const qc = useQueryClient();
  const row = useQuery({ queryKey: ["recipe", id], queryFn: () => api.recipe(id), enabled: !!id });
  const samplesQ = useQuery({ queryKey: ["samples", id], queryFn: () => api.pages(id), enabled: !!id });
  const seedSample = useQuery({
    queryKey: ["sample", loc.state?.sampleId],
    queryFn: () => api.page(loc.state!.sampleId!),
    enabled: !!loc.state?.sampleId,
  });

  const [recipe, setRecipe] = useState<Recipe | null>(null);
  const recipeRef = useRef<Recipe | null>(null);
  recipeRef.current = recipe;
  const [dirty, setDirty] = useState(false);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [tab, setTab] = useState("analyze");
  const [json, setJson] = useState("");
  const [picker, setPicker] = useState<{ open: boolean; target: PickTarget | null }>({ open: false, target: null });
  // AI designer side panel; `design` in navigation state opens it with a first prompt
  const [chatOpen, setChatOpen] = useState<boolean>(!!loc.state?.design);
  const [chatPrompt, setChatPrompt] = useState<string | null>(loc.state?.design ?? null);

  // an agent (in-app or MCP) saved this recipe → refresh unless the user has unsaved edits
  useEffect(() => {
    const h = (e: Event) => {
      const ev = (e as CustomEvent).detail as { t: string; id?: string; version?: number };
      if (ev.t !== "recipe_saved" || ev.id !== id) return;
      qc.invalidateQueries({ queryKey: ["samples", id] });
      api.recipe(id).then((r) => {
        qc.setQueryData(["recipe", id], r);
        if (!dirty) {
          setRecipe(r.recipe);
          toast.info(`Recipe updated to v${r.version} by the assistant`);
        } else {
          toast.warning(`The assistant saved v${r.version}, but you have unsaved edits — save yours or reload to see theirs.`);
        }
      });
    };
    window.addEventListener("sa:event", h);
    return () => window.removeEventListener("sa:event", h);
  }, [id, dirty, qc]);

  useEffect(() => {
    if (row.data && !recipe) setRecipe(row.data.recipe);
  }, [row.data, recipe]);

  const samples: Sample[] = useMemo(() => {
    const list = samplesQ.data ?? [];
    if (seedSample.data && !list.some((s) => s.id === seedSample.data!.id)) return [seedSample.data, ...list];
    return list;
  }, [samplesQ.data, seedSample.data]);
  // The seed page is what "analyze" is about. An API recipe's preview fetches JSON pages into the
  // same cache, and analyzing one of those as HTML says "single page, no containers".
  const seedUrl = recipe?.seeds?.[0];
  const apiPrefix = recipe?.api?.url_template?.split("{")[0] ?? null;
  const isApiPage = (s: Sample) => !!apiPrefix && (s.url.startsWith(apiPrefix) || s.final_url?.startsWith(apiPrefix));
  const pageSamples = samples.filter((s) => !isApiPage(s));
  const analysisSample =
    pageSamples.find((s) => s.analysis && (s.url === seedUrl || s.final_url === seedUrl)) ??
    pageSamples.find((s) => s.kind === "list" && s.analysis) ??
    pageSamples[0] ??
    null;

  const change = (r: Recipe) => {
    setRecipe(r);
    setDirty(true);
  };

  const save = useMutation({
    mutationFn: async () => api.updateRecipe(id, recipe!),
    onSuccess: (r) => {
      setDirty(false);
      qc.setQueryData(["recipe", id], r);
      qc.invalidateQueries({ queryKey: ["recipes"] });
      toast.success(`Saved v${r.version}`);
      if (r.incompatible_with_resume?.length) toast.info(`Structural change (${r.incompatible_with_resume.join(", ")}) — paused runs can't be resumed with it.`);
    },
    onError: (e: Error) => toast.error(`Save failed: ${e.message}`),
  });

  const fetchSamples = useMutation({
    mutationFn: async () => api.fetchSamples(recipe!),
    onSuccess: (r) => {
      setReport(r.report);
      qc.setQueryData(["samples", id], r.samples);
      toast.success(r.report.ok ? "Preview passes" : "Preview has issues — see below");
      setTab("preview");
    },
    onError: (e: Error) => toast.error(`Preview failed: ${e.message}`),
  });
  const snapshotSeed = useMutation({
    // The seed *page*, not the API endpoint the recipe reads — this is what "analyze" looks at.
    mutationFn: async () => api.snapshot({ urls: [recipe!.seeds[0]], recipe_id: id, kind: "list" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["samples", id] }),
    onError: (e: Error) => toast.error(`Snapshot failed: ${e.message}`),
  });
  const revalidate = useMutation({
    mutationFn: async () => api.preview(recipe!, samples.map((s) => s.id)),
    onSuccess: (r) => setReport(r.report),
    onError: (e: Error) => toast.error(`Validate failed: ${e.message}`),
  });
  const run = useMutation({
    mutationFn: async () => {
      if (dirty) await save.mutateAsync();
      return api.startRun({ recipe_id: id });
    },
    onSuccess: (r) => nav(`/runs/${r.id}`),
    onError: (e: Error) => toast.error(`Could not start: ${e.message}`),
  });

  const applyJson = () => {
    try {
      const parsed = JSON.parse(json);
      api.validateRecipe(parsed).then((v) => {
        if (v.ok && v.recipe) {
          change(v.recipe);
          toast.success("Applied");
        } else toast.error(v.errors.map((e) => `${e.loc}: ${e.msg}`).join("\n"));
      });
    } catch (e) {
      toast.error(`Invalid JSON: ${(e as Error).message}`);
    }
  };

  const onPicked = (res: { selector: string; attr: string | null; relative: boolean }) => {
    if (!recipe || !picker.target) return;
    const t = picker.target;
    if (t.kind === "container") change({ ...recipe, page_type: "list", list: { ...(recipe.list ?? {}), container: res.selector } });
    else if (t.kind === "detail") change({ ...recipe, detail: { ...recipe.detail, enabled: true, link: { css: res.selector } } });
    else if (t.kind === "next") change({ ...recipe, pagination: { ...recipe.pagination, kind: "next_link", selector: res.selector } });
    else if (t.kind === "field" && t.index !== undefined) {
      const fields = recipe.fields.map((f, i) => (i === t.index ? { ...f, extract: { ...f.extract, css: res.selector, xpath: null, attr: res.attr } } : f));
      change({ ...recipe, fields });
    }
  };

  if (row.isLoading || !recipe) return <div className="p-8 space-y-3"><Skeleton className="h-8 w-64" /><Skeleton className="h-40 w-full" /></div>;
  if (row.isError) return <div className="p-8 text-destructive">Recipe not found.</div>;

  return (
    <div className="flex h-full min-h-screen">
    <div className="p-6 space-y-4 flex-1 min-w-0">
      <div className="flex items-start gap-3 flex-wrap">
        <div className="min-w-0 flex-1">
          <Input value={recipe.name} onChange={(e) => change({ ...recipe, name: e.target.value })} className="text-lg font-semibold h-10 max-w-xl" />
          <div className="text-xs text-muted-foreground mt-1 truncate">
            {recipe.seeds.join(", ")} · <Badge variant="outline">v{row.data?.version}</Badge>{dirty && <span className="ml-2 text-amber-600">unsaved changes</span>}
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" asChild><a href={api.exportRecipeUrl(id, "yaml")} download><Download className="size-4" /> YAML</a></Button>
          <Button variant="outline" size="sm" asChild title="Standalone Scrapy project (zip) — runs without the app"><a href={api.exportRecipeUrl(id, "scrapy")} download><Download className="size-4" /> Scrapy project</a></Button>
          <Button variant="outline" size="sm" onClick={() => nav(`/runs?recipe=${id}`)}><History className="size-4" /> Runs</Button>
          <Button variant={chatOpen ? "default" : "outline"} size="sm" onClick={() => setChatOpen((o) => !o)}><Sparkles className="size-4" /> AI designer</Button>
          <Button variant="secondary" size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>{save.isPending ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />} Save</Button>
          <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending}><Play className="size-4" /> Run</Button>
        </div>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="analyze">1 · Analyze</TabsTrigger>
          <TabsTrigger value="fields">2 · Fields</TabsTrigger>
          <TabsTrigger value="preview">3 · Preview {report && (report.ok ? "✓" : "!")}</TabsTrigger>
          <TabsTrigger value="plan">4 · Plan & run</TabsTrigger>
          <TabsTrigger value="dataset">Dataset</TabsTrigger>
          <TabsTrigger value="json" onClick={() => setJson(JSON.stringify(recipe, null, 2))}>JSON</TabsTrigger>
          <TabsTrigger value="versions">Versions</TabsTrigger>
        </TabsList>
        <TabsContent value="analyze" className="pt-3">
          {analysisSample && !analysisSample.analysis ? (
            <div className="text-sm text-muted-foreground">
              This cached page has not been analyzed yet.{" "}
              <Button size="sm" variant="secondary" onClick={() => api.analyzePage(analysisSample.id).then(() => qc.invalidateQueries({ queryKey: ["samples", id] }))}>Analyze cached page</Button>
            </div>
          ) : analysisSample ? (
            <div className="space-y-4">
              <PlatformCard
                sample={analysisSample}
                recipe={recipe}
                usingApi={!!recipe.api}
                onUseApi={(r, endpoint) => {
                  change(r);
                  setReport(null);
                  toast.success(`Reading ${endpoint} — press Preview to validate`);
                  setTab("preview");
                }}
                onUseHtml={() => {
                  const { api: _drop, ...rest } = recipe as Recipe & { api?: unknown };
                  const container = recipe.list?.alternates?.[0];
                  change({
                    ...(rest as Recipe),
                    api: null,
                    list: container ? { ...recipe.list!, container, alternates: (recipe.list?.alternates ?? []).slice(1) } : recipe.list,
                  });
                  toast.info("Back to scraping the page");
                }}
              />
              <XhrCard
                sample={analysisSample}
                recipe={recipe}
                usingApi={!!recipe.api}
                onUseXhr={(r, endpoint) => {
                  change(r);
                  setReport(null);
                  toast.success(`Reading ${endpoint} — press Preview to validate`);
                  setTab("preview");
                }}
              />
              <AnalysisCard sample={analysisSample} recipe={recipe} onChange={change} />
            </div>
          ) : (
            <div className="space-y-2 text-sm text-muted-foreground">
              {recipe.api && (
                <p>
                  This recipe reads <span className="font-mono text-xs">{recipe.api.url_template}</span>
                  {recipe.api.note ? ` — ${recipe.api.note}` : ""}. Snapshot the page behind it to
                  analyze the HTML (and to re-check the platform).
                </p>
              )}
              {!recipe.api && "No cached page yet. "}
              <Button
                size="sm"
                variant="secondary"
                onClick={() => snapshotSeed.mutate()}
                disabled={snapshotSeed.isPending}
              >
                {snapshotSeed.isPending ? "Fetching…" : "Fetch the seed page"}
              </Button>
            </div>
          )}
        </TabsContent>
        <TabsContent value="fields" className="pt-3">
          <FieldsTab recipe={recipe} report={report} onChange={change} onPick={(t) => (analysisSample ? setPicker({ open: true, target: t }) : toast.info("Fetch a page first (Preview tab)"))} />
        </TabsContent>
        <TabsContent value="preview" className="pt-3">
          <PreviewTab
            recipe={recipe}
            report={report}
            samples={samples}
            busy={fetchSamples.isPending || revalidate.isPending}
            onFetch={() => fetchSamples.mutate()}
            onRevalidate={() => revalidate.mutate()}
            onFix={(name, fill) => {
              setChatPrompt(`The field \`${name}\` only fills ${Math.round(fill * 100)}% of items. Find a better selector on the cached pages, update the recipe and validate again.`);
              setChatOpen(true);
            }}
          />
        </TabsContent>
        <TabsContent value="plan" className="pt-3">
          <PlanTab recipe={recipe} onChange={change} onRun={() => run.mutate()} running={run.isPending} recipeId={id} />
        </TabsContent>
        <TabsContent value="dataset" className="pt-3">
          <DatasetTab recipeId={id} />
        </TabsContent>
        <TabsContent value="json" className="pt-3 space-y-2">
          <Textarea value={json} onChange={(e) => setJson(e.target.value)} rows={28} className="font-mono text-xs" />
          <Button size="sm" onClick={applyJson}>Validate & apply</Button>
        </TabsContent>
        <TabsContent value="versions" className="pt-3">
          <VersionsPanel id={id} current={row.data?.version ?? 0} onRolledBack={(r) => { setRecipe(r.recipe); setDirty(false); qc.setQueryData(["recipe", id], r); }} />
        </TabsContent>
      </Tabs>

      <PickerDialog
        open={picker.open}
        onOpenChange={(o) => setPicker((p) => ({ ...p, open: o }))}
        sample={analysisSample}
        container={recipe.list?.container ?? null}
        target={picker.target}
        onPicked={onPicked}
      />
    </div>
    {chatOpen && (
      <div className="w-[420px] shrink-0 sticky top-0 h-screen">
        <ChatPanel
          recipeId={id}
          onClose={() => setChatOpen(false)}
          initialPrompt={chatPrompt}
          onConsumedInitialPrompt={() => {
            setChatPrompt(null);
            // drop it from history state so a reload doesn't send it again
            nav(loc.pathname, { replace: true, state: { sampleId: loc.state?.sampleId } });
          }}
          onTurnEnd={() => {
            // the assistant may have re-validated: refresh the preview report from the cached samples
            api.pages(id).then((pages) => {
              qc.setQueryData(["samples", id], pages);
              if (pages.length) api.preview(recipeRef.current!, pages.map((s) => s.id)).then((r) => setReport(r.report)).catch(() => undefined);
            });
          }}
        />
      </div>
    )}
    </div>
  );
}

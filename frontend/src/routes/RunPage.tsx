import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, OctagonX, Pause, Play, RefreshCw, Wand2 } from "lucide-react";
import { api } from "@/lib/api";
import { useRunEvents } from "@/lib/ws";
import type { HealedSelector, Recipe, RunEvent, RunRow } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { fmt } from "@/components/recipe/PreviewTab";
import { TierBadge } from "@/components/recipe/AnalysisCard";
import { DiffChip } from "@/components/recipe/ScheduleCard";

const ACTIVE = new Set(["queued", "running", "stopping"]);

export function StatusBadge({ status }: { status: RunRow["status"] | string }) {
  const map: Record<string, string> = {
    running: "bg-blue-600 text-white",
    queued: "bg-blue-400 text-white",
    stopping: "bg-amber-500 text-white",
    stopped: "bg-amber-600 text-white",
    finished: "bg-emerald-600 text-white",
    failed: "bg-red-600 text-white",
    cancelled: "bg-zinc-500 text-white",
  };
  return <Badge className={map[status] ?? ""}>{status}</Badge>;
}

export default function RunPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const run = useQuery({ queryKey: ["run", id], queryFn: () => api.run(id), refetchInterval: (q) => (ACTIVE.has(q.state.data?.status ?? "running") ? 3000 : false) });
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [progress, setProgress] = useState<Record<string, unknown> | null>(null);
  const [log, setLog] = useState<RunEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [healedLive, setHealedLive] = useState<HealedSelector[]>([]);
  const [fills, setFills] = useState<Record<string, number[]>>({});
  const loadedRef = useRef(false);

  // initial page of items (for finished or resumed runs)
  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    api.runItems(id, 0, 200).then((r) => {
      setRows(r.items);
      setTotal(r.total);
    });
    api.runEvents(id, "page,blocked,log,progress,done,started,healed,heal_failed", 200).then((evs) => setLog(evs as RunEvent[]));
    api.runEvents(id, "fill", 300).then((evs) => {
      const f: Record<string, number[]> = {};
      for (const ev of evs) for (const [k, v] of Object.entries((ev.rates as Record<string, number>) ?? {})) (f[k] ??= []).push(v);
      setFills(f);
    });
  }, [id]);

  const onEvent = useCallback(
    (ev: RunEvent) => {
      if (ev.t === "item") {
        const row = ev.row as Record<string, unknown>;
        setRows((r) => (r.length >= 500 ? [...r.slice(1), row] : [...r, row]));
        setTotal((n) => Math.max(n, Number(ev.n) || n + 1));
      } else if (ev.t === "progress") {
        setProgress(ev);
      } else if (ev.t === "fill") {
        const rates = (ev.rates as Record<string, number>) ?? {};
        setFills((f) => {
          const next = { ...f };
          for (const [k, v] of Object.entries(rates)) next[k] = [...(next[k] ?? []).slice(-119), v];
          return next;
        });
      } else if (ev.t === "healed") {
        setHealedLive((h) => [...h, ev as unknown as HealedSelector]);
        setLog((l) => [...l, ev]);
      } else if (ev.t === "item_update") {
        const row = ev.row as Record<string, unknown>;
        setRows((rs) => rs.map((x) => (x._url === row._url ? row : x)));
      } else if (ev.t === "ai_fields") {
        if (ev.error) toast.warning(`AI fields skipped: ${String(ev.error)}`);
        else if (Number(ev.rows) > 0) toast.success(`AI fields computed for ${String(ev.rows)} rows ($${Number(ev.cost_usd ?? 0).toFixed(3)})`);
        qc.invalidateQueries({ queryKey: ["run", id] });
        api.runItems(id, 0, 200).then((r) => setRows(r.items));
      } else if (ev.t === "fallback") {
        qc.invalidateQueries({ queryKey: ["run-failed", id] });
        qc.invalidateQueries({ queryKey: ["run", id] });
      } else if (ev.t === "status" || ev.t === "done") {
        qc.invalidateQueries({ queryKey: ["run", id] });
        if (ev.t === "done") setLog((l) => [...l, ev]);
      } else if (ev.t === "page" || ev.t === "blocked" || ev.t === "log" || ev.t === "started") {
        setLog((l) => (l.length >= 500 ? [...l.slice(1), ev] : [...l, ev]));
      }
    },
    [id, qc],
  );
  const connected = useRunEvents(id, onEvent);

  const stop = useMutation({ mutationFn: () => api.stopRun(id), onSuccess: () => toast.info("Stopping after in-flight requests…"), onError: (e: Error) => toast.error(e.message) });
  const cancel = useMutation({ mutationFn: () => api.cancelRun(id), onSuccess: () => toast.info("Cancelled"), onError: (e: Error) => toast.error(e.message) });
  const resume = useMutation({
    mutationFn: () => api.resumeRun(id),
    onSuccess: () => {
      toast.success("Resumed");
      qc.invalidateQueries({ queryKey: ["run", id] });
    },
    onError: (e: Error) => toast.error(`Resume failed: ${e.message}`),
  });
  const exp = useMutation({
    mutationFn: async (fmtName: string) => api.exportRun(id, fmtName),
    onSuccess: (r) => {
      window.location.href = r.download;
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const r = run.data;
  const healed: HealedSelector[] = useMemo(() => {
    const seen = new Set<string>();
    return [...(r?.stats?.healed ?? []), ...healedLive].filter((h) => (seen.has(h.field) ? false : (seen.add(h.field), true)));
  }, [r?.stats?.healed, healedLive]);
  const applyHeals = useMutation({
    mutationFn: async () => {
      if (!r?.recipe_id) throw new Error("no recipe");
      const row = await api.recipe(r.recipe_id);
      const rec: Recipe = row.recipe;
      const fields = rec.fields.map((f) => {
        const h = healed.find((x) => x.field === f.name);
        if (!h) return f;
        const oldExt = f.extract;
        return { ...f, extract: { ...oldExt, css: h.new.css, xpath: null, attr: h.new.attr ?? null }, alternates: [oldExt, ...(f.alternates ?? [])].slice(0, 3) };
      });
      return api.updateRecipe(r.recipe_id, { ...rec, fields }, "apply healed selectors");
    },
    onSuccess: (row) => toast.success(`Recipe updated to v${row.version} with the healed selectors`),
    onError: (e: Error) => toast.error(e.message),
  });
  const finished = r ? !ACTIVE.has(r.status) : false;
  const diffQ = useQuery({ queryKey: ["run-diff", id], queryFn: () => api.runDiff(id), enabled: finished && !!r?.recipe_id });
  const failedQ = useQuery({ queryKey: ["run-failed", id], queryFn: () => api.runFailed(id), enabled: !!r, refetchInterval: (q) => (r && ACTIVE.has(r.status)) || (q.state.data?.counts.pending ?? 0) > 0 ? 4000 : false });
  const fallbackNow = useMutation({ mutationFn: () => api.runFallbackNow(id), onSuccess: (o) => { toast.success(`Fallback: ${o.recovered ?? 0} recovered, ${o.skipped ?? 0} skipped, ${o.failed ?? 0} failed`); qc.invalidateQueries({ queryKey: ["run-failed", id] }); qc.invalidateQueries({ queryKey: ["run", id] }); }, onError: (e: Error) => toast.error(e.message) });
  const failedCounts = failedQ.data?.counts ?? {};
  const failedTotal = Object.values(failedCounts).reduce((a, b) => a + b, 0);
  const cols = useMemo(() => {
    const seen: string[] = [];
    for (const row of rows.slice(0, 50)) for (const k of Object.keys(row)) if (!k.startsWith("_") && !seen.includes(k)) seen.push(k);
    return seen;
  }, [rows]);
  const active = r ? ACTIVE.has(r.status) : false;
  const items = Math.max(total, Number(progress?.items ?? 0), r?.items ?? 0);
  const pages = Number(progress?.pages ?? r?.pages ?? 0);
  const blocked = Number(progress?.blocked ?? r?.blocked ?? 0);
  const esc = Number(progress?.escalations ?? r?.escalations ?? 0);
  const tiers = (progress?.tiers as Record<string, number> | undefined) ?? ((r?.stats as { tiers?: Record<string, number> } | undefined)?.tiers ?? {});

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-semibold truncate">{r?.recipe_name || "Run"}</h1>
            {r && <StatusBadge status={r.status} />}
            {r?.reason && r.reason !== r.status && <span className="text-xs text-muted-foreground">({r.reason})</span>}
            {r?.schedule_id && <Badge variant="outline" className="text-[10px]">scheduled</Badge>}
            {r?.stats?.diff && <DiffChip d={r.stats.diff} />}
            {active && <span className={`text-[10px] ${connected ? "text-emerald-600" : "text-muted-foreground"}`}>{connected ? "● live" : "○ reconnecting…"}</span>}
          </div>
          <div className="text-xs text-muted-foreground">
            {r?.recipe_id && <Link className="underline" to={`/recipes/${r.recipe_id}`}>recipe v{r.recipe_version}</Link>} · {id}
            {r?.error && <span className="text-red-600 ml-2">{r.error}</span>}
          </div>
        </div>
        <div className="flex gap-2">
          {active && <Button size="sm" variant="secondary" onClick={() => stop.mutate()} disabled={r?.status === "stopping"}><Pause className="size-4" /> Stop</Button>}
          {active && <Button size="sm" variant="destructive" onClick={() => cancel.mutate()}><OctagonX className="size-4" /> Cancel</Button>}
          {r && (r.status === "stopped" || r.status === "failed") && r.recipe_id && <Button size="sm" onClick={() => resume.mutate()}><Play className="size-4" /> Resume</Button>}
          {["json", "jsonl", "csv", "xlsx"].map((f) => (
            <Button key={f} size="sm" variant="outline" onClick={() => exp.mutate(f)} disabled={items === 0}><Download className="size-4" /> {f.toUpperCase()}</Button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        <Stat label="Items" value={items} />
        <Stat label="Pages" value={pages} />
        <Stat label="Blocked" value={blocked} tone={blocked ? "warn" : undefined} />
        <Stat label="Escalations" value={esc} />
        <Card><CardContent className="pt-4"><div className="text-xs text-muted-foreground">Tiers</div><div className="flex gap-1 mt-1 flex-wrap">{Object.entries(tiers).length ? Object.entries(tiers).map(([t, n]) => <span key={t} className="text-xs"><TierBadge tier={t} /> {n}</span>) : <span className="text-sm text-muted-foreground">—</span>}</div></CardContent></Card>
        <Stat label="Elapsed" value={r?.started_at ? elapsed(r.started_at, r.finished_at) : "—"} />
      </div>
      {failedTotal > 0 && (
        <Card className={failedCounts.pending ? "border-amber-300" : ""}>
          <CardContent className="pt-3 pb-3 flex flex-wrap items-center gap-3 text-sm">
            <span className="font-medium">{failedTotal} page{failedTotal === 1 ? "" : "s"} the selectors could not read</span>
            {failedCounts.recovered ? <span className="text-emerald-700 dark:text-emerald-300">{failedCounts.recovered} recovered by AI{r?.stats?.llm ? ` (${r.stats.llm.rows} rows · $${r.stats.llm.cost_usd.toFixed(3)})` : ""}</span> : null}
            {failedCounts.pending ? <span className="text-amber-700 dark:text-amber-300">{failedCounts.pending} pending</span> : null}
            {failedCounts.skipped ? <span className="text-muted-foreground">{failedCounts.skipped} skipped</span> : null}
            {failedCounts.failed ? <span className="text-red-700 dark:text-red-300">{failedCounts.failed} failed</span> : null}
            {(failedCounts.pending || failedCounts.failed || failedCounts.skipped) ? <Button size="sm" variant="outline" onClick={() => fallbackNow.mutate()} disabled={fallbackNow.isPending}>Extract with AI now</Button> : null}
            <span className="text-xs text-muted-foreground">Needs an API key for the fallback role (Settings), or ask your Claude Code / Gemini CLI agent: <code>get_failed_pages</code> → <code>submit_rows</code>.</span>
            {failedQ.data?.pages.some((p) => p.error) && <div className="w-full text-xs text-red-700 dark:text-red-300">{failedQ.data.pages.find((p) => p.error)?.error}</div>}
          </CardContent>
        </Card>
      )}

      {healed.length > 0 && (
        <Card className="border-emerald-300 bg-emerald-50/60 dark:bg-emerald-950/20">
          <CardContent className="pt-4 flex items-start gap-3 flex-wrap">
            <div className="min-w-0 flex-1 text-sm">
              <div className="font-medium flex items-center gap-2"><Wand2 className="size-4" /> Selectors healed during this run</div>
              <ul className="mt-1 text-xs space-y-0.5">
                {healed.map((h) => (
                  <li key={h.field}><code>{h.field}</code>: <code className="text-red-700 dark:text-red-300">{h.old.css ?? h.old.xpath}{h.old.attr ? ` @${h.old.attr}` : ""}</code> → <code className="text-emerald-700 dark:text-emerald-300">{h.new.css}{h.new.attr ? ` @${h.new.attr}` : ""}</code> <span className="text-muted-foreground">(fill {Math.round(h.fill * 100)}%, e.g. {(h.examples ?? []).slice(0, 2).join(" · ")})</span></li>
                ))}
              </ul>
              <div className="text-xs text-muted-foreground mt-1">The site changed; these replacements were used for the rest of the run. Apply them so future runs (and schedules) don't have to re-discover them.</div>
            </div>
            {r?.recipe_id && <Button size="sm" onClick={() => applyHeals.mutate()} disabled={applyHeals.isPending}>Apply to recipe</Button>}
          </CardContent>
        </Card>
      )}
      {Object.keys(fills).length > 0 && (
        <Card>
          <CardContent className="pt-3 pb-3 flex flex-wrap gap-4 text-xs">
            {Object.entries(fills).map(([name, hist]) => (
              <div key={name} className="flex items-center gap-2">
                <span className="font-mono">{name}</span>
                <Sparkline values={hist} />
                <span className={hist[hist.length - 1] < 0.5 ? "text-amber-600" : "text-muted-foreground"}>{Math.round(hist[hist.length - 1] * 100)}%</span>
              </div>
            ))}
            <span className="text-muted-foreground self-center">fill rate per page</span>
          </CardContent>
        </Card>
      )}

      <Tabs defaultValue="items">
        <TabsList>
          <TabsTrigger value="items">Items</TabsTrigger>
          <TabsTrigger value="log">Activity ({log.length})</TabsTrigger>
          {finished && r?.recipe_id && <TabsTrigger value="diff">Diff{diffQ.data?.diff ? ` (+${diffQ.data.diff.added} −${diffQ.data.diff.removed} ~${diffQ.data.diff.changed})` : ""}</TabsTrigger>}
        </TabsList>
        <TabsContent value="items" className="pt-2">
          <Card>
            <CardContent className="p-0 overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10">#</TableHead>
                    {cols.map((c) => <TableHead key={c}>{c}</TableHead>)}
                    <TableHead>tier</TableHead>
                    <TableHead>_url</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.length === 0 && <TableRow><TableCell colSpan={cols.length + 3} className="text-center text-muted-foreground py-8">{active ? "Waiting for the first items…" : "No items."}</TableCell></TableRow>}
                  {rows.slice(-200).map((row, i) => (
                    <TableRow key={i}>
                      <TableCell className="text-xs text-muted-foreground">{rows.length - Math.min(rows.length, 200) + i + 1}</TableCell>
                      {cols.map((c) => <TableCell key={c} className="max-w-[260px] truncate text-xs">{fmt(row[c])}</TableCell>)}
                      <TableCell><TierBadge tier={row._tier as string} /><ProvenanceBadge prov={row._provenance as Record<string, string> | undefined} /></TableCell>
                      <TableCell className="max-w-[220px] truncate text-xs text-muted-foreground">{fmt(row._url)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {total > 200 && <div className="px-3 py-2 text-xs text-muted-foreground border-t">Showing the latest 200 of {total.toLocaleString()} rows — export to get everything.</div>}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="log" className="pt-2">
          <Card>
            <ScrollArea className="h-[420px]">
              <div className="p-3 font-mono text-xs space-y-0.5">
                {log.map((e, i) => (
                  <div key={i} className={e.t === "blocked" ? "text-amber-700 dark:text-amber-300" : e.t === "done" ? "font-semibold" : ""}>
                    <span className="text-muted-foreground">{String(e.ts ?? "").slice(11, 19)}</span> {describe(e)}
                  </div>
                ))}
              </div>
            </ScrollArea>
          </Card>
          <div className="mt-2"><Button size="sm" variant="ghost" onClick={() => api.runEvents(id, "page,blocked,log,progress,done,started", 300).then((evs) => setLog(evs as RunEvent[]))}><RefreshCw className="size-3.5" /> Reload</Button></div>
        </TabsContent>
        <TabsContent value="diff" className="pt-2 space-y-3">
          {!diffQ.data?.diff && <div className="text-sm text-muted-foreground">{diffQ.isLoading ? "Comparing…" : "No previous finished run of this recipe to compare against."}</div>}
          {diffQ.data?.diff && (
            <>
              <div className="text-sm text-muted-foreground">
                Compared with run <Link className="underline" to={`/runs/${diffQ.data.against_run_id}`}>{diffQ.data.against_run_id}</Link> by <code>{diffQ.data.diff.keys.join(", ")}</code>: <strong>+{diffQ.data.diff.added} new</strong>, <strong>−{diffQ.data.diff.removed} gone</strong>, <strong>~{diffQ.data.diff.changed} changed</strong>, {diffQ.data.diff.unchanged} unchanged.
              </div>
              <DiffTable title="New rows" rows={diffQ.data.diff.samples?.added ?? []} />
              <DiffTable title="Removed rows" rows={diffQ.data.diff.samples?.removed ?? []} />
              {(diffQ.data.diff.samples?.changed ?? []).length > 0 && (
                <Card>
                  <CardContent className="p-0">
                    <div className="px-3 py-2 text-sm font-medium border-b">Changed rows</div>
                    <Table>
                      <TableHeader><TableRow><TableHead>key</TableHead><TableHead>field</TableHead><TableHead>old</TableHead><TableHead>new</TableHead></TableRow></TableHeader>
                      <TableBody>
                        {(diffQ.data.diff.samples?.changed ?? []).flatMap((c, i) =>
                          Object.entries(c.fields).map(([f, v], j) => (
                            <TableRow key={`${i}-${j}`}>
                              <TableCell className="text-xs max-w-[240px] truncate">{fmt(Object.values(c.key)[0])}</TableCell>
                              <TableCell className="text-xs font-mono">{f}</TableCell>
                              <TableCell className="text-xs max-w-[240px] truncate text-red-700 dark:text-red-300">{fmt(v.old)}</TableCell>
                              <TableCell className="text-xs max-w-[240px] truncate text-emerald-700 dark:text-emerald-300">{fmt(v.new)}</TableCell>
                            </TableRow>
                          )),
                        )}
                      </TableBody>
                    </Table>
                  </CardContent>
                </Card>
              )}
            </>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ProvenanceBadge({ prov }: { prov?: Record<string, string> }) {
  if (!prov) return null;
  const vals = Object.values(prov);
  const src = vals.some((v) => v === "llm") ? "AI" : vals.some((v) => v === "agent") ? "agent" : vals.some((v) => v?.startsWith("alt")) ? "alt" : null;
  if (!src) return null;
  const cls = src === "AI" ? "bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200" : src === "agent" ? "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200" : "bg-muted text-muted-foreground";
  return <span className={`ml-1 rounded px-1 py-0.5 text-[10px] ${cls}`} title={`extracted by ${src}`}>{src}</span>;
}

function Sparkline({ values }: { values: number[] }) {
  const w = 80;
  const h = 16;
  const v = values.slice(-40);
  const step = v.length > 1 ? w / (v.length - 1) : w;
  const pts = v.map((y, i) => `${(i * step).toFixed(1)},${(h - y * (h - 2) - 1).toFixed(1)}`).join(" ");
  return (
    <svg width={w} height={h} className="text-primary">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth={1.5} />
    </svg>
  );
}

function DiffTable({ title, rows }: { title: string; rows: Record<string, unknown>[] }) {
  if (!rows.length) return null;
  const cols = Object.keys(rows[0]).filter((k) => !k.startsWith("_") || k === "_url").slice(0, 8);
  return (
    <Card>
      <CardContent className="p-0 overflow-x-auto">
        <div className="px-3 py-2 text-sm font-medium border-b">{title} <span className="text-muted-foreground font-normal">({rows.length} shown)</span></div>
        <Table>
          <TableHeader><TableRow>{cols.map((c) => <TableHead key={c}>{c}</TableHead>)}</TableRow></TableHeader>
          <TableBody>{rows.map((r, i) => <TableRow key={i}>{cols.map((c) => <TableCell key={c} className="text-xs max-w-[240px] truncate">{fmt(r[c])}</TableCell>)}</TableRow>)}</TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function Stat({ label, value, tone }: { label: string; value: number | string; tone?: "warn" }) {
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className={`text-2xl font-semibold ${tone === "warn" ? "text-amber-600" : ""}`}>{typeof value === "number" ? value.toLocaleString() : value}</div>
      </CardContent>
    </Card>
  );
}

function elapsed(start: string, end?: string | null): string {
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const sec = Math.max(0, Math.round((e - s) / 1000));
  return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

function describe(e: RunEvent): string {
  switch (e.t) {
    case "started":
      return `started · seeds ${(e.seeds as string[])?.join(", ")} · max ${e.max_pages} pages / ${e.max_items} items`;
    case "page":
      return `${e.ok ? "page" : "PAGE FAILED"} [${e.tier}] ${e.kind} ${e.url} ${e.items !== undefined ? `→ ${e.items} items` : ""}${e.reason ? ` (${e.reason}: ${e.detail ?? ""})` : ""}`;
    case "blocked":
      return `blocked [${e.tier}] ${e.url} — ${e.reason}: ${e.detail} ${e.escalated_to ? `→ escalating to ${e.escalated_to}` : "(no higher tier)"}`;
    case "progress":
      return `progress · ${e.pages} pages · ${e.items} items · ${e.blocked} blocked`;
    case "done":
      return `done (${e.reason}) · ${e.items} items · ${e.pages} pages`;
    case "log":
      return `${e.level}: ${e.msg}`;
    case "healed":
      return `HEALED ${e.field}: ${(e.old as { css?: string })?.css ?? ""} → ${(e.new as { css?: string })?.css ?? ""} (fill ${e.fill})`;
    case "heal_failed":
      return `heal failed for ${e.field} on ${e.url}`;
    default:
      return JSON.stringify(e);
  }
}

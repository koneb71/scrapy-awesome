import { Loader2, RefreshCw } from "lucide-react";
import type { Recipe, Sample, ValidationReport } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { FillBadge } from "./FieldsTab";
import { TierBadge } from "./AnalysisCard";
import { Wand2 } from "lucide-react";

export function PreviewTab({
  recipe,
  report,
  samples,
  busy,
  onFetch,
  onRevalidate,
  onFix,
}: {
  recipe: Recipe;
  report?: ValidationReport | null;
  samples: Sample[];
  busy: boolean;
  onFetch: () => void;
  onRevalidate: () => void;
  /** "Fix with AI" for a field (opens the designer with a targeted prompt); absent → hidden */
  onFix?: (fieldName: string, fill: number) => void;
}) {
  const cols = recipe.fields.map((f) => f.name);
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <Button onClick={onFetch} disabled={busy}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          Fetch samples & validate
        </Button>
        <Button variant="secondary" onClick={onRevalidate} disabled={busy || samples.length === 0}>
          Re-validate on cached samples
        </Button>
        <span className="text-xs text-muted-foreground">
          Samples: page 1, page 2 (if pagination) and two detail pages. Validation runs in-process — no tokens spent.
        </span>
      </div>

      {samples.length > 0 && (
        <div className="flex flex-wrap gap-2 text-xs">
          {samples.map((s) => (
            <span key={s.id} className="inline-flex items-center gap-1 rounded border px-2 py-1">
              <Badge variant="outline">{s.kind}</Badge> <TierBadge tier={s.tier} /> <span className="truncate max-w-[280px]">{s.final_url}</span>
              <span className="text-muted-foreground">({s.status})</span>
            </span>
          ))}
        </div>
      )}

      {report && (
        <>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base flex items-center gap-2">
                {report.ok ? <Badge className="bg-emerald-600">passing</Badge> : <Badge variant="destructive">issues</Badge>}
                {report.rows.length} rows · {report.containers.map((c) => `${c.matched}`).join(" + ")} matched
              </CardTitle>
              <CardDescription>Fill rate per field across the samples; expand a column's selector by hovering.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-2 md:grid-cols-2">
              <div className="space-y-1 text-sm">
                {Object.values(report.fields).map((fs) => (
                  <div key={fs.name} className="flex items-center justify-between gap-2 rounded border px-2 py-1" title={fs.selector}>
                    <div className="min-w-0">
                      <span className="font-medium">{fs.name}</span> <span className="text-xs text-muted-foreground">{fs.scope}</span>
                      <div className="text-xs text-muted-foreground truncate">{fs.examples.slice(0, 2).map((x) => String(x).slice(0, 40)).join(" · ")}</div>
                    </div>
                    <div className="text-xs text-muted-foreground shrink-0 flex items-center gap-1">
                      <FillBadge rate={fs.fill_rate} /> {fs.n_filled}/{fs.n_total} · {fs.distinct} distinct
                      {onFix && fs.fill_rate < 0.9 && (
                        <button className="ml-1 rounded border px-1.5 py-0.5 text-[11px] hover:bg-accent" title="Ask the AI designer to fix this field" onClick={() => onFix(fs.name, fs.fill_rate)}>
                          <Wand2 className="inline size-3" /> Fix with AI
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
              <div className="space-y-1 text-sm">
                {report.issues.length === 0 && <div className="text-muted-foreground">No issues.</div>}
                {report.issues.map((i, k) => (
                  <div key={k} className={`rounded border px-2 py-1 text-xs ${i.level === "error" ? "border-red-300 bg-red-50 dark:bg-red-950/30" : i.level === "warn" ? "border-amber-300 bg-amber-50 dark:bg-amber-950/30" : "border-border"}`}>
                    <span className="uppercase font-semibold mr-1">{i.level}</span> <code>{i.code}</code> — {i.message}
                  </div>
                ))}
                {report.pagination?.found_on_first !== undefined && (
                  <div className="text-xs text-muted-foreground">Next link on page 1: {String(report.pagination.found_on_first)}</div>
                )}
                {report.detail && (report.detail as { with_link?: number }).with_link !== undefined && (
                  <div className="text-xs text-muted-foreground">Detail links: {String((report.detail as { with_link: number }).with_link)} / {String((report.detail as { items: number }).items)}</div>
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-base">Preview rows</CardTitle></CardHeader>
            <CardContent className="p-0 overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    {cols.map((c) => <TableHead key={c}>{c}</TableHead>)}
                    <TableHead>_url</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {report.rows.slice(0, 50).map((row, i) => (
                    <TableRow key={i}>
                      {cols.map((c) => (
                        <TableCell key={c} className={`max-w-[260px] truncate text-xs ${row[c] === null || row[c] === undefined || row[c] === "" ? "bg-red-50/60 dark:bg-red-950/20" : ""}`}>
                          {fmt(row[c])}
                        </TableCell>
                      ))}
                      <TableCell className="max-w-[220px] truncate text-xs text-muted-foreground">{fmt(row._url)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

export function fmt(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.map(fmt).join("; ");
  return JSON.stringify(v);
}

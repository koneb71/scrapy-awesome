import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CalendarClock, Play, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { DiffSummary, Schedule, ScheduleIn } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";

/** Presets → (kind, cron|every_seconds). "custom" shows the raw cron field. */
const PRESETS: { id: string; label: string; kind: "cron" | "interval"; cron?: string; every?: number }[] = [
  { id: "hourly", label: "Every hour", kind: "interval", every: 3600 },
  { id: "6h", label: "Every 6 hours", kind: "interval", every: 6 * 3600 },
  { id: "daily", label: "Daily at 06:00", kind: "cron", cron: "0 6 * * *" },
  { id: "weekdays", label: "Weekdays at 08:00", kind: "cron", cron: "0 8 * * 1-5" },
  { id: "weekly", label: "Weekly (Mon 06:00)", kind: "cron", cron: "0 6 * * 1" },
  { id: "custom", label: "Custom cron…", kind: "cron", cron: "*/30 * * * *" },
];

export function DiffChip({ d, className }: { d: DiffSummary | null | undefined; className?: string }) {
  if (!d) return null;
  const tone = d.added || d.removed || d.changed ? "text-amber-700 dark:text-amber-300 border-amber-300" : "text-muted-foreground";
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-mono ${tone} ${className ?? ""}`} title={`vs run ${d.against_run_id ?? "?"}`}>
      +{d.added} −{d.removed} ~{d.changed}
    </span>
  );
}

export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = d.getTime() - Date.now();
  const abs = Math.abs(diff);
  const rel = abs < 60_000 ? "now" : abs < 3_600_000 ? `${Math.round(abs / 60_000)} min` : abs < 86_400_000 ? `${Math.round(abs / 3_600_000)} h` : `${Math.round(abs / 86_400_000)} d`;
  return `${d.toLocaleString()} (${diff > 0 ? "in " + rel : rel + " ago"})`;
}

export function ScheduleCard({ recipeId }: { recipeId: string }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["schedules", recipeId], queryFn: () => api.schedules(recipeId), refetchInterval: 30_000 });
  const [preset, setPreset] = useState("daily");
  const [cron, setCron] = useState("*/30 * * * *");
  const [maxPages, setMaxPages] = useState<string>("");
  const p = PRESETS.find((x) => x.id === preset)!;

  const invalidate = () => qc.invalidateQueries({ queryKey: ["schedules"] });
  const create = useMutation({
    mutationFn: () => {
      const body: ScheduleIn = {
        recipe_id: recipeId,
        name: p.label,
        kind: p.kind,
        cron: p.kind === "cron" ? (preset === "custom" ? cron : p.cron) : null,
        every_seconds: p.kind === "interval" ? p.every : null,
        max_pages: maxPages ? +maxPages : null,
      };
      return api.createSchedule(body);
    },
    onSuccess: () => {
      invalidate();
      toast.success("Scheduled");
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const patch = useMutation({ mutationFn: ({ id, body }: { id: string; body: Partial<ScheduleIn> }) => api.patchSchedule(id, body), onSuccess: invalidate, onError: (e: Error) => toast.error(e.message) });
  const del = useMutation({ mutationFn: (id: string) => api.deleteSchedule(id), onSuccess: invalidate });
  const runNow = useMutation({ mutationFn: (id: string) => api.runScheduleNow(id), onSuccess: (r) => { toast.success("Run started"); qc.invalidateQueries({ queryKey: ["runs"] }); invalidate(); void r; }, onError: (e: Error) => toast.error(e.message) });

  return (
    <Card className="md:col-span-2">
      <CardHeader className="pb-3">
        <CardTitle className="text-base flex items-center gap-2"><CalendarClock className="size-4" /> Schedule</CardTitle>
        <CardDescription>Re-run this recipe automatically while the app is running (or installed as a service). Each run is diffed against the previous one and you get a notification.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <Label className="text-xs">When</Label>
            <Select value={preset} onValueChange={setPreset}>
              <SelectTrigger className="w-52"><SelectValue /></SelectTrigger>
              <SelectContent>{PRESETS.map((x) => <SelectItem key={x.id} value={x.id}>{x.label}</SelectItem>)}</SelectContent>
            </Select>
          </div>
          {preset === "custom" && (
            <div>
              <Label className="text-xs">Cron (min hour dom mon dow, local time)</Label>
              <Input value={cron} onChange={(e) => setCron(e.target.value)} className="w-48 font-mono" />
            </div>
          )}
          <div>
            <Label className="text-xs">Max pages (optional)</Label>
            <Input type="number" min={1} value={maxPages} onChange={(e) => setMaxPages(e.target.value)} className="w-32" placeholder="recipe default" />
          </div>
          <Button size="sm" onClick={() => create.mutate()} disabled={create.isPending}>Add schedule</Button>
        </div>

        {(q.data ?? []).length === 0 && <div className="text-xs text-muted-foreground">No schedules yet.</div>}
        {(q.data ?? []).map((s: Schedule) => (
          <div key={s.id} className="flex flex-wrap items-center gap-2 rounded border px-2 py-1.5">
            <Switch checked={s.enabled} onCheckedChange={(v) => patch.mutate({ id: s.id, body: { enabled: v } })} />
            <div className="min-w-0 flex-1">
              <div className="font-medium">{s.name || s.describe} <span className="text-xs text-muted-foreground font-normal">· {s.describe}{s.max_pages ? ` · ≤${s.max_pages} pages` : ""}</span></div>
              <div className="text-xs text-muted-foreground">
                next {fmtWhen(s.next_run_at)}
                {s.last_run_id && <> · last {s.last_status} <Link className="underline" to={`/runs/${s.last_run_id}`}>{fmtWhen(s.last_run_at)}</Link></>}
              </div>
            </div>
            {s.last_diff && <DiffChip d={s.last_diff} />}
            {s.last_status === "running" && <Badge className="bg-blue-600 text-white">running</Badge>}
            <Button size="sm" variant="outline" onClick={() => runNow.mutate(s.id)} disabled={runNow.isPending}><Play className="size-3.5" /> Run now</Button>
            <Button size="icon" variant="ghost" onClick={() => del.mutate(s.id)} aria-label="Delete schedule"><Trash2 className="size-3.5" /></Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

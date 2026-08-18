import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Play, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { DiffChip, fmtWhen } from "@/components/recipe/ScheduleCard";

export default function SchedulesPage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["schedules"], queryFn: () => api.schedules(), refetchInterval: 30_000 });
  const recipes = useQuery({ queryKey: ["recipes"], queryFn: api.recipes });
  const names = new Map((recipes.data ?? []).map((r) => [r.id, r.name]));
  const invalidate = () => qc.invalidateQueries({ queryKey: ["schedules"] });
  const patch = useMutation({ mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.patchSchedule(id, { enabled }), onSuccess: invalidate });
  const del = useMutation({ mutationFn: (id: string) => api.deleteSchedule(id), onSuccess: invalidate });
  const runNow = useMutation({ mutationFn: (id: string) => api.runScheduleNow(id), onSuccess: () => { toast.success("Run started"); invalidate(); }, onError: (e: Error) => toast.error(e.message) });

  return (
    <div className="p-6 space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Schedules</h1>
        <p className="text-sm text-muted-foreground">Recipes that re-run automatically while the app is running. Add one from a recipe's Plan tab. Each run is diffed against the previous run (+new −gone ~changed).</p>
      </div>
      <Card>
        <Table>
          <TableHeader><TableRow><TableHead>On</TableHead><TableHead>Recipe</TableHead><TableHead>When</TableHead><TableHead>Next</TableHead><TableHead>Last</TableHead><TableHead>Diff</TableHead><TableHead /></TableRow></TableHeader>
          <TableBody>
            {(q.data ?? []).map((s) => (
              <TableRow key={s.id}>
                <TableCell><Switch checked={s.enabled} onCheckedChange={(v) => patch.mutate({ id: s.id, enabled: v })} /></TableCell>
                <TableCell><Link className="font-medium underline-offset-2 hover:underline" to={`/recipes/${s.recipe_id}`}>{names.get(s.recipe_id) ?? s.recipe_id}</Link><div className="text-xs text-muted-foreground">{s.name}</div></TableCell>
                <TableCell className="text-xs">{s.describe}{s.max_pages ? ` · ≤${s.max_pages} pages` : ""}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{s.enabled ? fmtWhen(s.next_run_at) : "paused"}</TableCell>
                <TableCell className="text-xs">
                  {s.last_run_id ? <Link className="underline" to={`/runs/${s.last_run_id}`}>{s.last_status}</Link> : "—"}
                  {s.last_status === "running" && <Badge className="ml-1 bg-blue-600 text-white">running</Badge>}
                  <div className="text-muted-foreground">{s.last_run_at ? fmtWhen(s.last_run_at) : ""}</div>
                </TableCell>
                <TableCell><DiffChip d={s.last_diff} /></TableCell>
                <TableCell><div className="flex gap-1 justify-end"><Button size="sm" variant="outline" onClick={() => runNow.mutate(s.id)}><Play className="size-3.5" /> Run now</Button><Button size="icon" variant="ghost" onClick={() => del.mutate(s.id)} aria-label="Delete"><Trash2 className="size-3.5" /></Button></div></TableCell>
              </TableRow>
            ))}
            {q.data?.length === 0 && <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground py-8">No schedules yet — open a recipe → Plan & run → Schedule.</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
}

import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusBadge } from "./RunPage";
import { Card } from "@/components/ui/card";
import { DiffChip } from "@/components/recipe/ScheduleCard";
import { Badge } from "@/components/ui/badge";

export default function RunsPage() {
  const [sp] = useSearchParams();
  const recipe = sp.get("recipe") ?? undefined;
  const runs = useQuery({ queryKey: ["runs", recipe], queryFn: () => api.runs(recipe), refetchInterval: 4000 });
  return (
    <div className="p-6 space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Runs</h1>
        <p className="text-sm text-muted-foreground">Every crawl, with its status, counts and exports.{recipe && " Filtered by recipe."}</p>
      </div>
      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Recipe</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Items</TableHead>
              <TableHead className="text-right">Pages</TableHead>
              <TableHead className="text-right">Blocked</TableHead>
              <TableHead>Started</TableHead>
              <TableHead>Run id</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(runs.data ?? []).map((r) => (
              <TableRow key={r.id}>
                <TableCell><Link to={`/runs/${r.id}`} className="font-medium underline-offset-2 hover:underline">{r.recipe_name || "(ad-hoc)"}</Link> <span className="text-xs text-muted-foreground">v{r.recipe_version}</span></TableCell>
                <TableCell><StatusBadge status={r.status} />{r.schedule_id && <Badge variant="outline" className="ml-1 text-[10px]">sched</Badge>}{r.stats?.diff && <DiffChip d={r.stats.diff} className="ml-1" />}</TableCell>
                <TableCell className="text-right">{r.items.toLocaleString()}</TableCell>
                <TableCell className="text-right">{r.pages.toLocaleString()}</TableCell>
                <TableCell className="text-right">{r.blocked}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{r.started_at ? new Date(r.started_at).toLocaleString() : "—"}</TableCell>
                <TableCell className="text-xs text-muted-foreground font-mono">{r.id}</TableCell>
              </TableRow>
            ))}
            {runs.data?.length === 0 && <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground py-8">No runs yet.</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
}

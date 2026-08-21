import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { History, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { DatasetRow } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const META = ["key", "url", "first_seen", "last_seen", "last_changed", "changes", "runs", "gone"];
const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");

/**
 * Runs are episodes; this is the thing you keep. One row per item, with when it first appeared,
 * when it last changed, and what changed — which is what price and stock watching actually want.
 */
export function DatasetTab({ recipeId }: { recipeId: string }) {
  const qc = useQueryClient();
  const [includeGone, setIncludeGone] = useState(true);
  const [changedOnly, setChangedOnly] = useState(false);
  const [openKey, setOpenKey] = useState<string | null>(null);

  const data = useQuery({
    queryKey: ["dataset", recipeId, includeGone, changedOnly],
    queryFn: () => api.dataset(recipeId, { include_gone: includeGone, changed_days: changedOnly ? 7 : undefined }),
  });
  const history = useQuery({
    queryKey: ["dataset-history", recipeId, openKey],
    queryFn: () => api.datasetHistory(recipeId, openKey!),
    enabled: !!openKey,
  });
  const forget = useMutation({
    mutationFn: () => api.forgetDataset(recipeId),
    onSuccess: (r) => {
      toast.success(`Forgot ${r.forgotten} rows — runs are untouched`);
      qc.invalidateQueries({ queryKey: ["dataset", recipeId] });
    },
  });

  const rows = data.data?.rows ?? [];
  const columns = rows.length ? Object.keys(rows[0]).filter((k) => !META.includes(k)) : [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-4 text-sm">
        <div className="text-muted-foreground">
          {data.data?.total ?? 0} rows this recipe has ever seen
        </div>
        <div className="flex items-center gap-2">
          <Switch checked={includeGone} onCheckedChange={setIncludeGone} />
          <Label className="text-xs">Include rows that disappeared</Label>
        </div>
        <div className="flex items-center gap-2">
          <Switch checked={changedOnly} onCheckedChange={setChangedOnly} />
          <Label className="text-xs">Changed in the last 7 days</Label>
        </div>
        <Button size="sm" variant="ghost" onClick={() => forget.mutate()} disabled={forget.isPending}>
          <Trash2 className="size-3.5" /> Start over
        </Button>
      </div>

      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nothing yet — the dataset fills up as runs finish, and rows keep their history across them.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-40">Last seen</TableHead>
                <TableHead className="w-24">Changes</TableHead>
                {columns.slice(0, 6).map((c) => <TableHead key={c}>{c}</TableHead>)}
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r: DatasetRow) => (
                <>
                  <TableRow key={r.key} className={r.gone ? "opacity-60" : ""}>
                    <TableCell className="text-xs">
                      {when(r.last_seen)}
                      {r.gone && <Badge variant="outline" className="ml-1 text-[10px]">gone</Badge>}
                    </TableCell>
                    <TableCell className="text-xs">
                      {r.changes > 0 ? `${r.changes}× · ${when(r.last_changed)}` : "—"}
                    </TableCell>
                    {columns.slice(0, 6).map((c) => (
                      <TableCell key={c} className="max-w-56 truncate text-xs">
                        {String((r as Record<string, unknown>)[c] ?? "")}
                      </TableCell>
                    ))}
                    <TableCell>
                      {r.changes > 0 && (
                        <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => setOpenKey(openKey === r.key ? null : r.key)}>
                          <History className="size-3.5" />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                  {openKey === r.key && (
                    <TableRow key={`${r.key}-h`}>
                      <TableCell colSpan={columns.slice(0, 6).length + 3} className="bg-muted/40 text-xs">
                        {(history.data?.history ?? []).map((h, i) => (
                          <div key={i} className="py-0.5">
                            <span className="text-muted-foreground">{when(h.at)}</span>{" "}
                            {Object.entries(h.diff).map(([f, [was, now]]) => (
                              <span key={f} className="mr-3">
                                <b>{f}</b>: <s>{String(was ?? "")}</s> → {String(now ?? "")}
                              </span>
                            ))}
                          </div>
                        ))}
                        {history.isFetching && <span className="text-muted-foreground">loading…</span>}
                      </TableCell>
                    </TableRow>
                  )}
                </>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

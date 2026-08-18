import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Play, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export default function RecipesPage() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const recipes = useQuery({ queryKey: ["recipes"], queryFn: api.recipes });
  const del = useMutation({
    mutationFn: (id: string) => api.deleteRecipe(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recipes"] }),
    onError: (e: Error) => toast.error(e.message),
  });
  const run = useMutation({
    mutationFn: (id: string) => api.startRun({ recipe_id: id }),
    onSuccess: (r) => nav(`/runs/${r.id}`),
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Recipes</h1>
          <p className="text-sm text-muted-foreground">Saved, versioned scrape definitions. Plain JSON/YAML — export and share them.</p>
        </div>
        <Button asChild><Link to="/">New</Link></Button>
      </div>
      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Seed</TableHead>
              <TableHead>Fields</TableHead>
              <TableHead>Version</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead className="w-40" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {(recipes.data ?? []).map((r) => (
              <TableRow key={r.id}>
                <TableCell><Link to={`/recipes/${r.id}`} className="font-medium hover:underline">{r.name}</Link></TableCell>
                <TableCell className="text-xs text-muted-foreground max-w-[320px] truncate">{r.recipe.seeds[0]}</TableCell>
                <TableCell className="text-xs">{r.recipe.fields.map((f) => f.name).join(", ")}</TableCell>
                <TableCell><Badge variant="outline">v{r.version}</Badge></TableCell>
                <TableCell className="text-xs text-muted-foreground">{new Date(r.updated_at).toLocaleString()}</TableCell>
                <TableCell>
                  <div className="flex gap-1 justify-end">
                    <Button size="sm" variant="secondary" onClick={() => run.mutate(r.id)}><Play className="size-3.5" /> Run</Button>
                    <Button size="icon" variant="ghost" onClick={() => del.mutate(r.id)}><Trash2 className="size-3.5" /></Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
            {recipes.data?.length === 0 && <TableRow><TableCell colSpan={6} className="text-center text-muted-foreground py-8">No recipes yet — start with <Link className="underline" to="/">New</Link>.</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
}

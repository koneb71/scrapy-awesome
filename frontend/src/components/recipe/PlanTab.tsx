import { useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { api } from "@/lib/api";
import type { Action, Recipe } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ScheduleCard } from "./ScheduleCard";

const ACTION_KINDS: Action["kind"][] = ["wait_for", "scroll_until_stable", "click", "wait_ms", "scroll", "fill", "press", "evaluate"];

export function PlanTab({
  recipe,
  onChange,
  onRun,
  running,
  recipeId,
}: {
  recipe: Recipe;
  onChange: (r: Recipe) => void;
  onRun: () => void;
  running: boolean;
  recipeId?: string;
}) {
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const lim = recipe.limits;
  const setLim = (patch: Partial<Recipe["limits"]>) => onChange({ ...recipe, limits: { ...lim, ...patch } });
  const setFetch = (patch: Partial<Recipe["fetch"]>) => onChange({ ...recipe, fetch: { ...recipe.fetch, ...patch } });
  const actions = recipe.fetch.actions ?? [];
  const setActions = (a: Action[]) => setFetch({ actions: a });
  const estPages = Math.min(lim.max_pages, 9999) * (recipe.detail.enabled ? 1 + 20 : 1);

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Limits</CardTitle><CardDescription>Stop conditions for the run.</CardDescription></CardHeader>
        <CardContent className="grid grid-cols-2 gap-3 text-sm">
          <div><Label>Max pages</Label><Input type="number" min={1} value={lim.max_pages} onChange={(e) => setLim({ max_pages: +e.target.value || 1 })} /></div>
          <div><Label>Max items</Label><Input type="number" min={1} value={lim.max_items} onChange={(e) => setLim({ max_items: +e.target.value || 1 })} /></div>
          <div><Label>Delay between requests (s)</Label><Input type="number" min={0} step={0.1} value={lim.download_delay} onChange={(e) => setLim({ download_delay: +e.target.value })} /></div>
          <div><Label>Concurrency per domain</Label><Input type="number" min={1} max={16} value={lim.concurrency_per_domain} onChange={(e) => setLim({ concurrency_per_domain: +e.target.value || 1 })} /></div>
          <div className="col-span-2 text-xs text-muted-foreground">
            Rough estimate: up to ~{estPages.toLocaleString()} requests{recipe.detail.enabled ? " (list pages × ~20 detail pages)" : ""}, at ≥{lim.download_delay}s each per domain.
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Fetching</CardTitle><CardDescription>Tier, login session and interactive actions.</CardDescription></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Tier</Label>
              <Select value={recipe.fetch.tier} onValueChange={(v) => setFetch({ tier: v as Recipe["fetch"]["tier"] })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">auto (http → browser → interactive)</SelectItem>
                  <SelectItem value="http">http (TLS-impersonated)</SelectItem>
                  <SelectItem value="browser">browser (real Chrome)</SelectItem>
                  <SelectItem value="interactive">interactive (Playwright)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Login session</Label>
              <Select value={recipe.fetch.session ?? "none"} onValueChange={(v) => setFetch({ session: v === "none" ? null : v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">none</SelectItem>
                  {(sessions.data ?? []).map((s) => (
                    <SelectItem key={s.id} value={s.id}>{s.name} ({s.status})</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Wait for selector (interactive)</Label>
              <Input value={recipe.fetch.wait_for ?? ""} onChange={(e) => setFetch({ wait_for: e.target.value || null })} placeholder="article.product" />
            </div>
            <div className="flex items-end gap-2 pb-2">
              <Switch id="assets" checked={recipe.fetch.block_static_assets ?? true} onCheckedChange={(v) => setFetch({ block_static_assets: v })} />
              <Label htmlFor="assets">Block images/fonts</Label>
            </div>
          </div>
          <div>
            <div className="flex items-center justify-between">
              <Label>Actions (run before extraction, interactive tier)</Label>
              <Button size="sm" variant="outline" onClick={() => setActions([...actions, { kind: "scroll_until_stable" }])}>Add</Button>
            </div>
            <div className="space-y-1 mt-1">
              {actions.map((a, i) => (
                <div key={i} className="flex gap-1 items-center">
                  <Select value={a.kind} onValueChange={(v) => setActions(actions.map((x, j) => (j === i ? { ...x, kind: v as Action["kind"] } : x)))}>
                    <SelectTrigger className="h-8 w-44"><SelectValue /></SelectTrigger>
                    <SelectContent>{ACTION_KINDS.map((k) => <SelectItem key={k} value={k}>{k}</SelectItem>)}</SelectContent>
                  </Select>
                  {["wait_for", "click", "fill", "press"].includes(a.kind) && (
                    <Input className="h-8 font-mono text-xs" placeholder="selector" value={a.selector ?? ""} onChange={(e) => setActions(actions.map((x, j) => (j === i ? { ...x, selector: e.target.value } : x)))} />
                  )}
                  {["fill", "press"].includes(a.kind) && (
                    <Input className="h-8 w-32 text-xs" placeholder="value" value={a.value ?? ""} onChange={(e) => setActions(actions.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
                  )}
                  {["wait_ms", "scroll", "click"].includes(a.kind) && (
                    <Input className="h-8 w-24 text-xs" type="number" placeholder="ms" value={a.ms ?? ""} onChange={(e) => setActions(actions.map((x, j) => (j === i ? { ...x, ms: +e.target.value || null } : x)))} />
                  )}
                  {["click", "scroll"].includes(a.kind) && (
                    <Input className="h-8 w-20 text-xs" type="number" placeholder="times" value={a.times ?? ""} onChange={(e) => setActions(actions.map((x, j) => (j === i ? { ...x, times: +e.target.value || null } : x)))} />
                  )}
                  {a.kind === "evaluate" && (
                    <Input className="h-8 font-mono text-xs" placeholder="JS expression" value={a.js ?? ""} onChange={(e) => setActions(actions.map((x, j) => (j === i ? { ...x, js: e.target.value } : x)))} />
                  )}
                  <Button size="sm" variant="ghost" onClick={() => setActions(actions.filter((_, j) => j !== i))}>×</Button>
                </div>
              ))}
              {actions.length === 0 && <div className="text-xs text-muted-foreground">None. Infinite scroll needs <code>scroll_until_stable</code>; load-more needs <code>click</code>.</div>}
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="md:col-span-2">
        <CardContent className="pt-6 flex items-center justify-between">
          <div className="text-sm text-muted-foreground">
            The run is deterministic and costs no tokens. It streams items live; you can stop and resume it any time.
          </div>
          <Button size="lg" onClick={onRun} disabled={running}>
            <Play className="size-4" /> {running ? "Starting…" : "Start run"}
          </Button>
        </CardContent>
      </Card>

      {recipeId && <ScheduleCard recipeId={recipeId} />}
    </div>
  );
}

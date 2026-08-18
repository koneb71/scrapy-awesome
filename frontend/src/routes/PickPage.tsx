import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Bot, Check, MousePointerClick, X } from "lucide-react";
import { api } from "@/lib/api";
import type { PickRequest, Sample } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { PickerDialog, type PickTarget } from "@/components/recipe/PickerDialog";

/** An agent (Claude Code / Gemini CLI via MCP) asked the person to click an element. */
export default function PickPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const pick = useQuery({
    queryKey: ["pick", id],
    queryFn: () => api.pick(id),
    refetchInterval: (q) => (q.state.data?.status === "pending" ? 4000 : false),
  });
  const p = pick.data;

  // page to pick on: the request's page, else the recipe's latest list page
  const recipe = useQuery({ queryKey: ["recipe", p?.recipe_id], queryFn: () => api.recipe(p!.recipe_id!), enabled: !!p?.recipe_id });
  const pages = useQuery({ queryKey: ["samples", p?.recipe_id], queryFn: () => api.pages(p!.recipe_id!), enabled: !!p?.recipe_id && !p?.sample_id });
  const page = useQuery({ queryKey: ["sample", p?.sample_id], queryFn: () => api.page(p!.sample_id!), enabled: !!p?.sample_id });
  const list = pages.data ?? [];
  const sample: Sample | null = page.data ?? list.find((s) => s.kind === "list" && s.analysis) ?? list.find((s) => s.kind === "list") ?? list[0] ?? null;
  const container = recipe.data?.recipe.list?.container ?? sample?.analysis?.containers?.[0]?.selector ?? null;

  const target: PickTarget = useMemo(() => {
    switch (p?.kind) {
      case "container":
        return { kind: "container" };
      case "link":
        return { kind: "detail" };
      case "pagination":
        return { kind: "next" };
      default:
        return { kind: "field", index: 0 };
    }
  }, [p?.kind]);

  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (p?.status === "pending" && sample && !open) setOpen(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p?.status, sample?.id]);

  const answer = useMutation({
    mutationFn: (body: Parameters<typeof api.answerPick>[1]) => api.answerPick(id, body),
    onSuccess: (r) => {
      pick.refetch();
      toast.success(r.status === "answered" ? "Sent to your agent" : "Cancelled");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const onPicked = (r: { selector: string; attr: string | null; matches: number; relative: boolean }) => {
    answer.mutate({
      selector: r.relative ? null : r.selector,
      relative_selector: r.relative ? r.selector : null,
      container: r.relative ? container : null,
      attr: r.attr,
      examples: [],
      matches: r.matches,
      note: null,
    });
  };

  if (pick.isLoading) return <div className="p-8 text-muted-foreground">Loading…</div>;
  if (!p) return <div className="p-8 text-destructive">This pick request no longer exists (the app server restarted?).</div>;

  return (
    <div className="p-8 max-w-2xl mx-auto space-y-4">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2 text-xs text-muted-foreground"><Bot className="size-4" /> Your agent asks</div>
          <CardTitle className="text-lg">{p.prompt}</CardTitle>
          <CardDescription>
            {p.kind === "field" ? "Click the element holding the value" : p.kind === "container" ? "Click one repeating item (card / row)" : p.kind === "link" ? "Click the link to the detail page" : p.kind === "pagination" ? "Click the “next page” link" : "Click the element"}
            {p.field_name && <> for field <code>{p.field_name}</code></>}. {p.hint && <>Hint: <em>{p.hint}</em>.</>}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <StatusBadge status={p.status} />
            {p.recipe_id && <Link className="text-xs underline text-muted-foreground" to={`/recipes/${p.recipe_id}`}>open recipe</Link>}
          </div>
          {p.status === "pending" && (
            <div className="flex gap-2">
              <Button onClick={() => setOpen(true)} disabled={!sample}><MousePointerClick className="size-4" /> Pick on page</Button>
              <Button variant="ghost" onClick={() => answer.mutate({ cancelled: true })}><X className="size-4" /> Decline</Button>
            </div>
          )}
          {p.status === "pending" && !sample && <div className="text-sm text-muted-foreground">No cached page to pick on yet — the agent should call fetch_page first.</div>}
          {p.status === "answered" && p.answer && (
            <div className="text-sm rounded-md border p-3 space-y-1">
              <div className="flex items-center gap-2 text-emerald-700"><Check className="size-4" /> Sent</div>
              <div><span className="text-muted-foreground">selector:</span> <code>{p.answer.relative_selector ?? p.answer.selector}</code>{p.answer.relative_selector && <span className="text-xs text-muted-foreground"> (relative to <code>{p.answer.container}</code>)</span>}</div>
              {p.answer.attr && <div><span className="text-muted-foreground">attribute:</span> <code>{p.answer.attr}</code></div>}
              {p.answer.matches != null && <div><span className="text-muted-foreground">matches:</span> {p.answer.matches}</div>}
              <div className="pt-2 text-xs text-muted-foreground">You can go back to your agent now.</div>
            </div>
          )}
        </CardContent>
      </Card>
      <div className="text-xs text-muted-foreground text-center">
        <button className="underline" onClick={() => nav("/")}>Back to the app</button>
      </div>

      <PickerDialog open={open && p.status === "pending"} onOpenChange={setOpen} sample={sample} container={container} target={target} onPicked={onPicked} />
    </div>
  );
}

function StatusBadge({ status }: { status: PickRequest["status"] }) {
  const cls = status === "pending" ? "bg-blue-600 text-white" : status === "answered" ? "bg-emerald-600 text-white" : "bg-zinc-500 text-white";
  return <Badge className={cls}>{status}</Badge>;
}

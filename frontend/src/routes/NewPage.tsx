import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowRight, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { applyAnalysis, newRecipe } from "@/lib/recipe";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";

export default function NewPage() {
  const nav = useNavigate();
  const [url, setUrl] = useState("");
  const [intent, setIntent] = useState("");
  const [needsJs, setNeedsJs] = useState(false);
  const recent = useQuery({ queryKey: ["recipes"], queryFn: api.recipes });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const designerProvider = settings.data?.settings.llm.designer.provider ?? "anthropic";
  const keySet = !!settings.data?.fake_llm || designerProvider === "claude_code" || !!settings.data?.secrets[designerProvider === "gemini" ? "gemini_api_key" : "anthropic_api_key"]?.set;
  const [design, setDesign] = useState<boolean | null>(null); // null → follow keySet
  const designOn = design ?? keySet;

  const start = useMutation({
    mutationFn: async () => {
      const u = url.trim().match(/^https?:\/\//) ? url.trim() : `https://${url.trim()}`;
      let recipe = newRecipe(u, intent.trim());
      if (needsJs) recipe.fetch.tier = "browser";
      // 1) persist a draft so the sample can be linked to it
      const draft = await api.createRecipe(recipe);
      // 2) fetch the seed page through the engine (analysis comes back attached)
      const samples = await api.snapshot({ urls: [u], recipe_id: draft.id, kind: "list" });
      const s = samples[0];
      if (s.analysis) recipe = applyAnalysis(recipe, s.analysis);
      recipe.name = s.title || recipe.name;
      // 3) save the analyzed recipe
      const row = await api.updateRecipe(draft.id, recipe, "analyzed");
      return { row, sample: s };
    },
    onSuccess: ({ row, sample }) => {
      toast.success(`Analyzed ${sample.final_url} via ${sample.tier} tier`);
      const prompt = designOn
        ? intent.trim()
          ? `Build the recipe for this page. What I want: ${intent.trim()}. Confirm the selectors on the cached page, save the recipe, validate it, and tell me the fill rates.`
          : "Build a sensible recipe for this page: use the detected list container and the obvious fields (title/name, price, link, image if present), save it, validate it, and tell me what you found."
        : null;
      nav(`/recipes/${row.id}`, { state: { sampleId: sample.id, design: prompt } });
    },
    onError: (e: Error) => toast.error(`Could not analyze page: ${e.message}`),
  });

  return (
    <div className="p-8 max-w-3xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">New scrape</h1>
        <p className="text-muted-foreground mt-1">
          Paste a URL and say what you want. The page is fetched through the stealth engine, analyzed, and turned into an editable recipe.
        </p>
      </div>
      <Card>
        <CardContent className="pt-6 space-y-4">
          <div className="space-y-2">
            <Label htmlFor="url">URL</Label>
            <Input
              id="url"
              placeholder="https://books.toscrape.com/"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && url && start.mutate()}
              autoFocus
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="intent">What do you want? (fields, filters, follow detail pages…)</Label>
            <Textarea
              id="intent"
              placeholder="Every book with title, price and rating; open each book for the description and UPC"
              value={intent}
              onChange={(e) => setIntent(e.target.value)}
              rows={3}
            />
          </div>
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-4 text-sm flex-wrap">
              <div className="flex items-center gap-2">
                <Switch id="js" checked={needsJs} onCheckedChange={setNeedsJs} />
                <Label htmlFor="js">Needs JavaScript (start on the browser tier)</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch id="design" checked={designOn} onCheckedChange={setDesign} disabled={!keySet} />
                <Label htmlFor="design" className={!keySet ? "text-muted-foreground" : ""}>
                  Design with AI{!keySet && settings.data ? " (add an API key in Settings, or use your Claude Code / Gemini CLI)" : ""}
                </Label>
              </div>
            </div>
            <Button onClick={() => start.mutate()} disabled={!url || start.isPending}>
              {start.isPending ? <Loader2 className="size-4 animate-spin" /> : <ArrowRight className="size-4" />}
              {start.isPending ? "Fetching & analyzing…" : "Analyze page"}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Fetching escalates automatically: HTTP (TLS-impersonated) → real Chrome → interactive browser. robots.txt is respected by default (Settings).
          </p>
        </CardContent>
      </Card>

      {recent.data && recent.data.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Recent recipes</CardTitle>
            <CardDescription>Open one to preview, edit or run it again.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            {recent.data.slice(0, 6).map((r) => (
              <button
                key={r.id}
                onClick={() => nav(`/recipes/${r.id}`)}
                className="flex items-center justify-between rounded-md border px-3 py-2 text-left hover:bg-accent"
              >
                <div className="min-w-0">
                  <div className="font-medium truncate">{r.name}</div>
                  <div className="text-xs text-muted-foreground truncate">{r.recipe.seeds[0]}</div>
                </div>
                <Badge variant="secondary">v{r.version}</Badge>
              </button>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

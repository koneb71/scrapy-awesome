import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Boxes, Check, Info, Loader2, Plug, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import type { PlatformBlock, Recipe, Sample } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * "Is this a Shopify store?" — shown on the Analyze tab whenever a platform was recognised.
 * When its JSON API answered the confirmation probe, one click rewrites the recipe to read it
 * (the CSS selectors stay on as fallbacks).
 */
export function PlatformCard({
  sample,
  recipe,
  usingApi,
  onUseApi,
  onUseHtml,
}: {
  sample: Sample;
  recipe: Recipe;
  usingApi: boolean;
  onUseApi: (r: Recipe, endpoint: string) => void;
  onUseHtml: () => void;
}) {
  const [granularity, setGranularity] = useState<"product" | "variant">("product");
  const [block, setBlock] = useState<PlatformBlock | null>((sample.analysis?.platform as PlatformBlock) ?? null);

  const recheck = useMutation({
    mutationFn: () => api.detectPlatform(sample.id),
    onSuccess: (b) => {
      setBlock(b);
      toast.info(b.detected ? `${b.label} detected` : "No known platform detected");
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const use = useMutation({
    mutationFn: () => api.useApi(sample.id, recipe, granularity),
    onSuccess: (r) => onUseApi(r.recipe, r.endpoint),
    onError: (e: Error) => toast.error(e.message),
  });

  if (!block || !block.detected) return null;
  const offer = block.api;
  // /collections/<handle>/products.json is that collection; /products.json is the whole store.
  const endpointPath = offer ? new URL(offer.endpoint).pathname : "";
  const collection = endpointPath.match(/\/collections\/([^/]+)\//)?.[1] ?? null;

  return (
    <Card className={offer ? "border-emerald-300 bg-emerald-50/40 dark:bg-emerald-950/20" : ""}>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Plug className="size-4" />
          <CardTitle className="text-base">{block.label} detected</CardTitle>
          <Badge variant="outline" className="text-[10px]">score {block.score}</Badge>
          {block.cached && <span className="text-[10px] text-muted-foreground">remembered</span>}
          <Tooltip>
            <TooltipTrigger asChild><span className="text-muted-foreground"><Info className="size-3.5" /></span></TooltipTrigger>
            <TooltipContent className="max-w-sm text-xs">{block.signals.join(" · ")}</TooltipContent>
          </Tooltip>
        </div>
        <CardDescription>
          {offer ? (
            <>This site publishes {collection ? <>the <b>{collection}</b> collection</> : "its catalogue"} as
            JSON at <code>{endpointPath}</code> — the same public data it serves your browser, in far fewer
            requests and with clean fields.</>
          ) : (
            <>No public JSON API to use here — {block.reason}. Scraping the page instead.</>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {offer && (
          <>
            <ul className="text-xs text-muted-foreground space-y-0.5">
              {offer.evidence.map((e) => <li key={e}>✓ {e}</li>)}
              {offer.currency && <li>✓ prices in {offer.currency}</li>}
              {offer.robots_note && <li className="text-amber-700 dark:text-amber-500">! {offer.robots_note} — reading {collection ? "that collection" : "the whole catalogue"} instead</li>}
            </ul>
            {usingApi ? (
              <div className="flex items-center gap-2 flex-wrap">
                <Badge className="bg-emerald-600 text-white"><Check className="size-3 mr-1" /> reading the API</Badge>
                <span className="text-xs text-muted-foreground">Your CSS selectors are kept as fallbacks if the endpoint stops answering.</span>
                <Button size="sm" variant="ghost" onClick={onUseHtml}>Use the page instead</Button>
              </div>
            ) : (
              <div className="flex items-center gap-2 flex-wrap">
                <Button size="sm" onClick={() => use.mutate()} disabled={use.isPending}>
                  {use.isPending ? <Loader2 className="size-4 animate-spin" /> : <Boxes className="size-4" />}
                  Use the {block.label} API
                </Button>
                <Select value={granularity} onValueChange={(v) => setGranularity(v as "product" | "variant")}>
                  <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="product">one row per product</SelectItem>
                    <SelectItem value="variant">one row per variant</SelectItem>
                  </SelectContent>
                </Select>
                <span className="text-xs text-muted-foreground">
                  {collection
                    ? "Same collection as this page, every field the store holds — and pages of 250 instead of one screen at a time."
                    : "Counts may differ from the page: the API lists everything published, not just this collection."}
                </span>
              </div>
            )}
          </>
        )}
        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" onClick={() => recheck.mutate()} disabled={recheck.isPending}>
            <RefreshCw className="size-3.5" /> Re-check
          </Button>
          <span className="text-[11px] text-muted-foreground">
            robots.txt is honoured for the API exactly as for pages; no login or admin endpoints are ever used.
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

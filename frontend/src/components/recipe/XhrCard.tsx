import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, Loader2, Radio, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import type { Recipe, Sample, XhrBlock, XhrCandidate } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

/**
 * "What does this page fetch?" — for every site the platform detector does not recognise.
 * Opening a real browser costs a few seconds, so it is a button, not something we do on every
 * analyze; once the endpoint is confirmed the recipe reads JSON and never opens a browser again.
 */
export function XhrCard({
  sample,
  recipe,
  usingApi,
  onUseXhr,
}: {
  sample: Sample;
  recipe: Recipe;
  usingApi: boolean;
  onUseXhr: (r: Recipe, endpoint: string) => void;
}) {
  const [block, setBlock] = useState<XhrBlock | null>((sample.analysis?.xhr as XhrBlock) ?? null);
  const [sampleId, setSampleId] = useState(sample.id);

  const find = useMutation({
    mutationFn: () => api.findApi(sample.id),
    onSuccess: (b) => {
      setBlock(b);
      setSampleId(b.sample_id);
      toast[b.candidates.length ? "info" : "warning"](
        b.candidates.length
          ? `Watched ${b.watched} JSON responses, ${b.candidates.length} look like this page's list`
          : "The page fetched no JSON that looks like its list",
      );
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const use = useMutation({
    mutationFn: (c: XhrCandidate) => api.useXhr(sampleId, recipe, c.url_template),
    onSuccess: (r) => onUseXhr(r.recipe, r.endpoint),
    onError: (e: Error) => toast.error(e.message),
  });

  if (usingApi) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Radio className="size-4" />
          <CardTitle className="text-base">Read the API this page uses</CardTitle>
        </div>
        <CardDescription>
          Many pages draw their list from a JSON endpoint. Reading that directly is a handful of
          requests with typed fields, and nothing to re-heal when the design changes.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!block && (
          <Button size="sm" onClick={() => find.mutate()} disabled={find.isPending}>
            {find.isPending ? <Loader2 className="size-4 animate-spin" /> : <Radio className="size-4" />}
            {find.isPending ? "Opening the page in a browser…" : "Find the API this page uses"}
          </Button>
        )}
        {block && (
          <>
            <div className="text-xs text-muted-foreground">
              Watched {block.watched} JSON {block.watched === 1 ? "response" : "responses"} while the
              page loaded.{" "}
              <button className="underline" onClick={() => find.mutate()} disabled={find.isPending}>
                Look again
              </button>
            </div>
            {block.candidates.map((c) => {
              const confirmed = block.confirmed === c.url_template;
              return (
                <div key={c.url_template} className="rounded-md border p-2 space-y-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <code className="text-xs break-all">{c.url}</code>
                    <Badge variant="outline" className="text-[10px]">{c.count} rows</Badge>
                    {confirmed && (
                      <Badge className="bg-emerald-600 text-white text-[10px]">
                        <Check className="size-3 mr-0.5" /> answers on its own
                      </Badge>
                    )}
                  </div>
                  <div className="text-[11px] text-muted-foreground">{c.why.join(" · ")}</div>
                  <div className="text-[11px] text-muted-foreground">
                    fields: {c.keys.slice(0, 8).join(", ")}
                    {c.keys.length > 8 ? "…" : ""}
                  </div>
                  <Button
                    size="sm"
                    variant={confirmed ? "default" : "secondary"}
                    disabled={use.isPending || (!!block.confirmed && !confirmed)}
                    onClick={() => use.mutate(c)}
                  >
                    Use this endpoint
                  </Button>
                </div>
              );
            })}
            {block.reason && !block.confirmed && (
              <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-500">
                <TriangleAlert className="size-3.5 mt-0.5 shrink-0" /> {block.reason}
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

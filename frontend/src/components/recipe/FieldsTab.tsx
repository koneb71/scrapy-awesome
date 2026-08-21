import { useState } from "react";
import { Crosshair, Plus, Trash2, Wand2 } from "lucide-react";
import type { Field, FieldType, Recipe, ValidationReport } from "@/lib/types";
import { emptyField, fieldTypes, slugify } from "@/lib/recipe";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TransformsDialog } from "@/components/recipe/TransformsDialog";

type Source = "css" | "xpath" | "json_path" | "llm";

function sourceOf(f: Field): Source {
  if (f.extract.xpath) return "xpath";
  if (f.extract.json_path) return "json_path";
  if (f.extract.llm) return "llm";
  return "css";
}
function selectorOf(f: Field): string {
  return f.extract.css ?? f.extract.xpath ?? f.extract.json_path ?? f.extract.llm ?? "";
}

export function FieldsTab({
  recipe,
  report,
  onChange,
  onPick,
}: {
  recipe: Recipe;
  report?: ValidationReport | null;
  onChange: (r: Recipe) => void;
  onPick: (target: { kind: "container" | "detail" | "next" | "field"; index?: number }) => void;
}) {
  const [newName, setNewName] = useState("");
  const isJsonContainer = recipe.list?.container?.startsWith("json:");
  const update = (i: number, patch: Partial<Field>) => {
    const fields = recipe.fields.map((f, j) => (j === i ? { ...f, ...patch } : f));
    onChange({ ...recipe, fields });
  };
  const setSel = (i: number, source: Source, value: string) => {
    const f = recipe.fields[i];
    const extract = { ...f.extract, css: null, xpath: null, json_path: null, llm: null, [source]: value } as Field["extract"];
    update(i, { extract });
  };
  const remove = (i: number) => onChange({ ...recipe, fields: recipe.fields.filter((_, j) => j !== i) });
  const add = () => {
    const name = slugify(newName || `field_${recipe.fields.length + 1}`);
    const scope: Field["scope"] = recipe.page_type === "single" ? "page" : "list";
    const f = emptyField(name, scope);
    if (isJsonContainer) f.extract = { json_path: "" };
    onChange({ ...recipe, fields: [...recipe.fields, f] });
    setNewName("");
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Structure</CardTitle>
          <CardDescription>What repeats, where detail pages are, how to paginate.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-3 text-sm">
          <div className="space-y-2">
            <Label>Page type</Label>
            <Select value={recipe.page_type} onValueChange={(v) => onChange({ ...recipe, page_type: v as "list" | "single", list: v === "single" ? null : (recipe.list ?? { container: "" }) })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="list">List (many items per page)</SelectItem>
                <SelectItem value="single">Single page</SelectItem>
              </SelectContent>
            </Select>
            {recipe.page_type === "list" && (
              <>
                <Label>Item container</Label>
                <div className="flex gap-1">
                  <Input value={recipe.list?.container ?? ""} placeholder="article.product_pod or json:__NEXT_DATA__…" onChange={(e) => onChange({ ...recipe, list: { ...(recipe.list ?? {}), container: e.target.value } })} />
                  <Tooltip><TooltipTrigger asChild><Button size="icon" variant="outline" onClick={() => onPick({ kind: "container" })}><Crosshair className="size-4" /></Button></TooltipTrigger><TooltipContent>Pick on page</TooltipContent></Tooltip>
                </div>
              </>
            )}
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Switch id="detail" checked={recipe.detail.enabled} onCheckedChange={(v) => onChange({ ...recipe, detail: { ...recipe.detail, enabled: v, link: v ? (recipe.detail.link ?? { css: "a" }) : recipe.detail.link } })} />
              <Label htmlFor="detail">Follow detail pages</Label>
            </div>
            {recipe.detail.enabled && (
              <>
                <Label>Link selector (inside item)</Label>
                <div className="flex gap-1">
                  <Input value={recipe.detail.link?.css ?? recipe.detail.link?.xpath ?? ""} onChange={(e) => onChange({ ...recipe, detail: { ...recipe.detail, link: { css: e.target.value } } })} placeholder="h3 a" />
                  <Tooltip><TooltipTrigger asChild><Button size="icon" variant="outline" onClick={() => onPick({ kind: "detail" })}><Crosshair className="size-4" /></Button></TooltipTrigger><TooltipContent>Pick on page</TooltipContent></Tooltip>
                </div>
              </>
            )}
          </div>
          <div className="space-y-2">
            <Label>Pagination</Label>
            <Select value={recipe.pagination.kind} onValueChange={(v) => onChange({ ...recipe, pagination: { ...recipe.pagination, kind: v as Recipe["pagination"]["kind"] } })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                <SelectItem value="next_link">Next link</SelectItem>
                <SelectItem value="url_template">URL template ({"{page}"})</SelectItem>
                <SelectItem value="load_more">Load-more button (interactive)</SelectItem>
                <SelectItem value="infinite_scroll">Infinite scroll (interactive)</SelectItem>
              </SelectContent>
            </Select>
            {recipe.pagination.kind === "next_link" && (
              <div className="flex gap-1">
                <Input value={recipe.pagination.selector ?? ""} placeholder="li.next a" onChange={(e) => onChange({ ...recipe, pagination: { ...recipe.pagination, selector: e.target.value } })} />
                <Tooltip><TooltipTrigger asChild><Button size="icon" variant="outline" onClick={() => onPick({ kind: "next" })}><Crosshair className="size-4" /></Button></TooltipTrigger><TooltipContent>Pick on page</TooltipContent></Tooltip>
              </div>
            )}
            {recipe.pagination.kind === "url_template" && (
              <Input value={recipe.pagination.url_template ?? ""} placeholder="https://site/list?page={page}" onChange={(e) => onChange({ ...recipe, pagination: { ...recipe.pagination, url_template: e.target.value } })} />
            )}
            {recipe.pagination.kind === "load_more" && (
              <Input value={recipe.pagination.selector ?? ""} placeholder="button.load-more" onChange={(e) => onChange({ ...recipe, pagination: { ...recipe.pagination, selector: e.target.value } })} />
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Fields — the contract</CardTitle>
          <CardDescription>Name, type, where it comes from. Example values come from the last preview.</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-40">Name</TableHead>
                  <TableHead className="w-28">Type</TableHead>
                  <TableHead className="w-24">Scope</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead className="w-24">Attr</TableHead>
                  <TableHead className="w-32">Regex</TableHead>
                  <TableHead className="w-16">Clean</TableHead>
                  <TableHead className="w-16">Req.</TableHead>
                  <TableHead className="w-16">
                    <Tooltip>
                      <TooltipTrigger asChild><span>Sparse</span></TooltipTrigger>
                      <TooltipContent>Usually empty (a sale price) — an empty column is a note, not an error</TooltipContent>
                    </Tooltip>
                  </TableHead>
                  <TableHead className="w-44">Examples / fill</TableHead>
                  <TableHead className="w-24" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {recipe.fields.map((f, i) => {
                  const src = sourceOf(f);
                  const st = report?.fields?.[f.name];
                  return (
                    <TableRow key={i}>
                      <TableCell><Input value={f.name} onChange={(e) => update(i, { name: slugify(e.target.value) || f.name })} className="h-8" /></TableCell>
                      <TableCell>
                        <Select value={f.type} onValueChange={(v) => update(i, { type: v as FieldType })}>
                          <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                          <SelectContent>{fieldTypes().map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        <Select value={f.scope} onValueChange={(v) => update(i, { scope: v as Field["scope"] })}>
                          <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="list">list</SelectItem>
                            <SelectItem value="detail">detail</SelectItem>
                            <SelectItem value="page">page</SelectItem>
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        <div className="flex gap-1">
                          <Select value={src} onValueChange={(v) => setSel(i, v as Source, selectorOf(f))}>
                            <SelectTrigger className="h-8 w-24"><SelectValue /></SelectTrigger>
                            <SelectContent>
                              <SelectItem value="css">css</SelectItem>
                              <SelectItem value="xpath">xpath</SelectItem>
                              <SelectItem value="json_path">json</SelectItem>
                              <SelectItem value="llm">AI</SelectItem>
                            </SelectContent>
                          </Select>
                          <Input value={selectorOf(f)} onChange={(e) => setSel(i, src, e.target.value)} className="h-8 font-mono text-xs" placeholder={src === "llm" ? "instruction for the AI…" : src === "json_path" ? "props.items[*].name" : "h3 a::attr(title)"} />
                          {src !== "llm" && src !== "json_path" && (
                            <Tooltip><TooltipTrigger asChild><Button size="icon" variant="outline" className="h-8 w-8 shrink-0" onClick={() => onPick({ kind: "field", index: i })}><Crosshair className="size-3.5" /></Button></TooltipTrigger><TooltipContent>Pick on page</TooltipContent></Tooltip>
                          )}
                        </div>
                      </TableCell>
                      <TableCell><Input value={f.extract.attr ?? ""} onChange={(e) => update(i, { extract: { ...f.extract, attr: e.target.value || null } })} className="h-8 font-mono text-xs" placeholder="href" /></TableCell>
                      <TableCell><Input value={f.extract.regex ?? ""} onChange={(e) => update(i, { extract: { ...f.extract, regex: e.target.value || null } })} className="h-8 font-mono text-xs" placeholder="(\d+)" /></TableCell>
                      <TableCell><TransformsDialog field={f} onChange={(t) => update(i, { transforms: t })} /></TableCell>
                      <TableCell><Switch checked={!!f.required} onCheckedChange={(v) => update(i, { required: v })} /></TableCell>
                      <TableCell><Switch checked={!!f.sparse} onCheckedChange={(v) => update(i, { sparse: v })} /></TableCell>
                      <TableCell className="text-xs">
                        {st ? (
                          <div>
                            <FillBadge rate={st.fill_rate} />{" "}
                            <span className="text-muted-foreground">{st.examples.slice(0, 2).map((x) => String(x).slice(0, 24)).join(" · ")}</span>
                          </div>
                        ) : (
                          <span className="text-muted-foreground">{(f.examples ?? []).slice(0, 2).join(" · ")}</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex gap-1">
                          {src !== "llm" && (
                            <Tooltip><TooltipTrigger asChild><Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => setSel(i, "llm", f.description || `Extract ${f.name}`)}><Wand2 className="size-3.5" /></Button></TooltipTrigger><TooltipContent>Make it an AI field</TooltipContent></Tooltip>
                          )}
                          <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => remove(i)}><Trash2 className="size-3.5" /></Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <div className="flex items-center gap-2 p-3 border-t">
            <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="new field name" className="h-8 w-56" onKeyDown={(e) => e.key === "Enter" && add()} />
            <Button size="sm" variant="secondary" onClick={add}><Plus className="size-4" /> Add field</Button>
            <span className="text-xs text-muted-foreground ml-auto">Dedupe key: {(recipe.dedupe_key ?? ["_url"]).join(", ")}</span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export function FillBadge({ rate }: { rate: number }) {
  const pct = Math.round(rate * 100);
  const cls = rate >= 0.9 ? "bg-emerald-500" : rate >= 0.5 ? "bg-amber-500" : "bg-red-500";
  return (
    <Badge variant="outline" className="gap-1 font-normal">
      <span className={`inline-block size-2 rounded-full ${cls}`} /> {pct}%
    </Badge>
  );
}

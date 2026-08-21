import { useState } from "react";
import { Plus, Trash2, Wand2 } from "lucide-react";
import type { Field, Transform, TransformKind } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

/** What each transform needs typed in, and how to explain it in one line. */
const KINDS: { kind: TransformKind; label: string; needs: ("value" | "pattern" | "chars" | "index")[]; hint: string }[] = [
  { kind: "trim", label: "Trim", needs: ["chars"], hint: "cut whitespace (or the given characters) off both ends" },
  { kind: "collapse_space", label: "Collapse spaces", needs: [], hint: "runs of whitespace become one space" },
  { kind: "strip_prefix", label: "Strip prefix", needs: ["value"], hint: "drop a leading label like “Price: ”" },
  { kind: "strip_suffix", label: "Strip suffix", needs: ["value"], hint: "drop a trailing “ €”, “ each”, …" },
  { kind: "replace", label: "Replace text", needs: ["pattern", "value"], hint: "swap one substring for another" },
  { kind: "regex_replace", label: "Replace (regex)", needs: ["pattern", "value"], hint: "the same, by pattern" },
  { kind: "split", label: "Split", needs: ["pattern", "index"], hint: "one value into many; an index keeps just one" },
  { kind: "prepend", label: "Prepend", needs: ["value"], hint: "put something in front (a base URL, say)" },
  { kind: "append", label: "Append", needs: ["value"], hint: "put something on the end" },
  { kind: "decimal_comma", label: "European decimals", needs: [], hint: "1.234,56 → 1234.56" },
  { kind: "digits", label: "Digits only", needs: [], hint: "keep digits, a dot and a minus" },
  { kind: "lower", label: "lowercase", needs: [], hint: "" },
  { kind: "upper", label: "UPPERCASE", needs: [], hint: "" },
  { kind: "title", label: "Title Case", needs: [], hint: "" },
  { kind: "default", label: "Default if empty", needs: ["value"], hint: "a value to use when nothing was found" },
];

const blank = (kind: TransformKind): Transform => ({ kind, value: "", pattern: "", chars: "", index: null });

/**
 * The long tail of "nearly right" values, edited where the field lives. These run in order on the
 * raw string *before* the type is read, which is why "strip the label, then read it as a price"
 * works.
 */
export function TransformsDialog({ field, onChange }: { field: Field; onChange: (t: Transform[]) => void }) {
  const [open, setOpen] = useState(false);
  const list = field.transforms ?? [];
  const set = (i: number, patch: Partial<Transform>) =>
    onChange(list.map((t, n) => (n === i ? { ...t, ...patch } : t)));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant={list.length ? "secondary" : "ghost"} className="h-8 px-2">
          <Wand2 className="size-3.5" />
          {list.length || ""}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Clean up “{field.name}”</DialogTitle>
          <DialogDescription>
            Applied in order to every value, before the field's type is read.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {list.map((t, i) => {
            const spec = KINDS.find((k) => k.kind === t.kind) ?? KINDS[0];
            return (
              <div key={i} className="flex items-end gap-2 rounded-md border p-2">
                <div className="w-44">
                  <Label className="text-xs">Step {i + 1}</Label>
                  <Select value={t.kind} onValueChange={(v) => set(i, { ...blank(v as TransformKind), kind: v as TransformKind })}>
                    <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {KINDS.map((k) => <SelectItem key={k.kind} value={k.kind}>{k.label}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                {spec.needs.includes("pattern") && (
                  <div className="flex-1">
                    <Label className="text-xs">{t.kind === "split" ? "Separator" : "Find"}</Label>
                    <Input className="h-8 font-mono text-xs" value={t.pattern ?? ""} onChange={(e) => set(i, { pattern: e.target.value })} />
                  </div>
                )}
                {spec.needs.includes("value") && (
                  <div className="flex-1">
                    <Label className="text-xs">{t.kind === "replace" || t.kind === "regex_replace" ? "Replace with" : "Value"}</Label>
                    <Input className="h-8 font-mono text-xs" value={t.value ?? ""} onChange={(e) => set(i, { value: e.target.value })} />
                  </div>
                )}
                {spec.needs.includes("chars") && (
                  <div className="w-28">
                    <Label className="text-xs">Characters</Label>
                    <Input className="h-8 font-mono text-xs" placeholder="whitespace" value={t.chars ?? ""} onChange={(e) => set(i, { chars: e.target.value })} />
                  </div>
                )}
                {spec.needs.includes("index") && (
                  <div className="w-24">
                    <Label className="text-xs">Keep #</Label>
                    <Input className="h-8" type="number" placeholder="all" value={t.index ?? ""} onChange={(e) => set(i, { index: e.target.value === "" ? null : +e.target.value })} />
                  </div>
                )}
                <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => onChange(list.filter((_, n) => n !== i))}>
                  <Trash2 className="size-3.5" />
                </Button>
              </div>
            );
          })}
          {list.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nothing yet. A common pair: <b>Strip prefix</b> “Price: ” then <b>European decimals</b>.
            </p>
          )}
          <div className="flex items-center gap-2">
            <Button size="sm" variant="secondary" onClick={() => onChange([...list, blank("trim")])}>
              <Plus className="size-3.5" /> Add a step
            </Button>
            <span className="text-xs text-muted-foreground">
              {(KINDS.find((k) => k.kind === list[list.length - 1]?.kind)?.hint) || ""}
            </span>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

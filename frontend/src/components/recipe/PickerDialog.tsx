import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { buildContainerSelector, buildRelativeSelector, buildSelector, suggestAttr } from "@/lib/picker";
import type { Sample } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";

export type PickTarget = { kind: "container" | "detail" | "next" | "field"; index?: number };

export function PickerDialog({
  open,
  onOpenChange,
  sample,
  container,
  target,
  onPicked,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  sample: Sample | null;
  container: string | null; // current list container (for relative selectors)
  target: PickTarget | null;
  onPicked: (r: { selector: string; attr: string | null; matches: number; relative: boolean }) => void;
}) {
  const iframe = useRef<HTMLIFrameElement>(null);
  const [candidate, setCandidate] = useState<{ selector: string; attr: string | null; matches: number; relative: boolean; text: string } | null>(null);
  const [check, setCheck] = useState<string>("");

  useEffect(() => {
    if (!open) {
      setCandidate(null);
      setCheck("");
    }
  }, [open]);

  const wire = () => {
    const doc = iframe.current?.contentDocument;
    if (!doc || !target) return;
    let hovered: Element | null = null;
    const root = doc.documentElement;
    const clear = () => {
      doc.querySelectorAll(".sa-hover").forEach((e) => e.classList.remove("sa-hover"));
    };
    doc.addEventListener("mouseover", (e) => {
      const el = e.target as Element;
      if (!el || el === hovered) return;
      clear();
      hovered = el;
      el.classList.add("sa-hover");
    });
    doc.addEventListener(
      "click",
      async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const el = e.target as Element;
        if (!el || el.tagName === "HTML" || el.tagName === "BODY") return;
        doc.querySelectorAll(".sa-picked,.sa-sibling").forEach((x) => x.classList.remove("sa-picked", "sa-sibling"));
        let selector: string;
        let relative = false;
        let attr: string | null = null;
        if (target.kind === "container") {
          const c = buildContainerSelector(el, root);
          selector = c.selector;
          doc.querySelectorAll(selector).forEach((x) => x.classList.add("sa-sibling"));
        } else {
          // relative to the container item when the click is inside one
          let item: Element | null = null;
          if (container && !container.startsWith("json:")) {
            try {
              item = el.closest(container);
            } catch {
              item = null;
            }
          }
          if (item && target.kind !== "next") {
            selector = buildRelativeSelector(el, item) || el.tagName.toLowerCase();
            relative = true;
          } else {
            selector = buildSelector(el, root);
          }
          attr = target.kind === "detail" || target.kind === "next" ? "href" : suggestAttr(el);
          if (target.kind === "field" && el.tagName.toLowerCase() === "a") attr = null; // text of the link by default
        }
        el.classList.add("sa-picked");
        const text = (el.textContent || "").trim().slice(0, 80);
        setCandidate({ selector, attr, matches: 0, relative, text });
        setCheck("checking…");
        try {
          const res = await api.testSelector(sample!.id, {
            selector,
            attr: attr ?? undefined,
            container: relative ? container : undefined,
          });
          const matches = relative ? (res.filled ?? 0) : (res.matches ?? 0);
          setCandidate((c) => (c ? { ...c, matches } : c));
          setCheck(
            relative
              ? `${res.filled}/${res.container_matches} items filled · e.g. ${(res.values ?? []).slice(0, 2).map(String).join(" · ")}`
              : `${res.matches} matches · e.g. ${(res.values ?? []).slice(0, 2).map(String).join(" · ")}`,
          );
        } catch (err) {
          setCheck(`could not test: ${(err as Error).message}`);
        }
      },
      true,
    );
  };

  const title =
    target?.kind === "container" ? "Click one item — its siblings become the list container"
    : target?.kind === "detail" ? "Click the link that opens an item's detail page"
    : target?.kind === "next" ? "Click the “next page” link"
    : "Click the element holding this field's value";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[96vw] w-[1200px] h-[88vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-4 py-3 border-b">
          <DialogTitle className="text-base">{title}</DialogTitle>
          <DialogDescription className="flex items-center gap-2 flex-wrap">
            <span>Cached page ({sample?.tier} tier), scripts removed. Selectors are generated in a CSS subset the crawler supports.</span>
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 min-h-0 bg-muted">
          {sample && (
            <iframe
              ref={iframe}
              title="page"
              src={api.renderUrl(sample.id)}
              className="w-full h-full bg-white"
              onLoad={wire}
            />
          )}
        </div>
        <div className="border-t px-4 py-3 flex items-center gap-3">
          {candidate ? (
            <>
              <div className="min-w-0 flex-1 text-sm">
                <code className="text-xs break-all">{candidate.selector}</code>
                {candidate.attr && <Badge variant="outline" className="ml-2">@{candidate.attr}</Badge>}
                {candidate.relative && <Badge variant="secondary" className="ml-2">relative to item</Badge>}
                <div className="text-xs text-muted-foreground truncate">{check} {candidate.text && `· “${candidate.text}”`}</div>
              </div>
              <Button onClick={() => { onPicked({ selector: candidate.selector, attr: candidate.attr, matches: candidate.matches, relative: candidate.relative }); onOpenChange(false); }}>
                Use this
              </Button>
            </>
          ) : (
            <div className="text-sm text-muted-foreground">Hover to highlight, click to select.</div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

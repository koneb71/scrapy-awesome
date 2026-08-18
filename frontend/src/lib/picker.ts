/** Client-side selector generation for the element picker (cssselect-safe grammar:
 * tag, #id, .class, [attr="v"], `>` and `:nth-of-type(n)` only). */

const UNSTABLE = /\d{2,}|^(css|sc|jsx|_)-|[A-Za-z0-9]{6,}[-_][A-Za-z0-9]{4,}$|^[a-z]{1,2}\d|__|hash|--\w+$/;

function stableClasses(el: Element): string[] {
  return Array.from(el.classList)
    .filter((c) => c.length >= 3 && c.length <= 40 && !UNSTABLE.test(c) && !c.startsWith("sa-"))
    .slice(0, 2);
}

function cssEscape(s: string): string {
  return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^\w-])/g, "\\$1");
}

function step(el: Element, withNth: boolean): string {
  const tag = el.tagName.toLowerCase();
  const id = el.getAttribute("id");
  if (id && /^[A-Za-z_][\w-]*$/.test(id) && !/\d{3,}/.test(id)) return `${tag}#${cssEscape(id)}`;
  let s = tag;
  const cls = stableClasses(el);
  if (cls.length) s += cls.map((c) => `.${cssEscape(c)}`).join("");
  else {
    for (const attr of ["data-testid", "data-test", "itemprop", "role", "name"]) {
      const v = el.getAttribute(attr);
      if (v && /^[\w.-]+$/.test(v)) {
        s += `[${attr}="${v}"]`;
        break;
      }
    }
  }
  if (withNth && el.parentElement) {
    const same = Array.from(el.parentElement.children).filter((c) => c.tagName === el.tagName);
    if (same.length > 1) s += `:nth-of-type(${same.indexOf(el) + 1})`;
  }
  return s;
}

function count(root: ParentNode, sel: string): number {
  try {
    return root.querySelectorAll(sel).length;
  } catch {
    return -1;
  }
}

/** Selector for `el` that is unique within `root` when possible; prefers stable classes over position. */
export function buildSelector(el: Element, root: ParentNode, opts: { unique?: boolean } = {}): string {
  const unique = opts.unique ?? true;
  const parts: string[] = [];
  let cur: Element | null = el;
  let depth = 0;
  while (cur && cur !== root && depth < 6) {
    parts.unshift(step(cur, false));
    const sel = parts.join(" > ");
    if (!unique || count(root, sel) === 1) return sel;
    // try positional at the leaf
    const withNth = [...parts.slice(0, -1), step(cur === el ? el : cur, true)];
    if (cur === el && count(root, withNth.join(" > ")) === 1) return withNth.join(" > ");
    cur = cur.parentElement;
    depth += 1;
  }
  return parts.join(" > ");
}

/** A selector meant to match *all* siblings that look like `el` (for list containers). */
export function buildContainerSelector(el: Element, root: ParentNode): { selector: string; count: number } {
  let cur: Element | null = el;
  let best = { selector: step(el, false), count: count(root, step(el, false)) };
  let depth = 0;
  while (cur && cur !== root && depth < 3) {
    const parent: Element | null = cur.parentElement;
    if (!parent) break;
    const s = step(cur, false);
    const scoped = parent === root ? s : `${step(parent, false)} > ${s}`;
    const n = count(root, scoped);
    if (n >= 2 && (best.count < 2 || n <= best.count)) best = { selector: scoped, count: n };
    if (n >= 2) break;
    cur = parent;
    depth += 1;
  }
  return best;
}

/** Relative selector of `el` inside its container item `item`. */
export function buildRelativeSelector(el: Element, item: Element): string {
  if (el === item) return "";
  return buildSelector(el, item, { unique: false });
}

export function suggestAttr(el: Element): string | null {
  const tag = el.tagName.toLowerCase();
  if (tag === "img") return "src";
  if (tag === "a") return "href";
  if (tag === "time" && el.getAttribute("datetime")) return "datetime";
  return null;
}

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Check, Copy, Plug } from "lucide-react";

const DEFAULT_MODEL: Record<string, string> = { anthropic: "claude-opus-5", gemini: "gemini-3.7-flash", claude_code: "claude-opus-5" };
const POLICY_QUOTE = "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK.";

export default function SettingsPage() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const doctor = useQuery({ queryKey: ["doctor"], queryFn: api.doctor });
  const tiers = useQuery({ queryKey: ["tier-memory"], queryFn: api.tierMemory });
  const connect = useQuery({ queryKey: ["connect"], queryFn: api.connect });
  const anthropicModels = useQuery({ queryKey: ["llm-models", "anthropic"], queryFn: () => api.llmModels("anthropic") });
  const geminiModels = useQuery({ queryKey: ["llm-models", "gemini"], queryFn: () => api.llmModels("gemini") });
  const modelsFor = (p: string) => (p === "gemini" ? geminiModels : anthropicModels);
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storage });
  const prune = useMutation({
    mutationFn: api.prune,
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["storage"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
      toast.success(`Pruned ${r.runs} runs and ${r.samples} cached pages`);
    },
  });
  const [notifPerm, setNotifPerm] = useState<string>(typeof Notification !== "undefined" ? Notification.permission : "unsupported");
  const [keys, setKeys] = useState<Record<string, string>>({});
  const update = useMutation({
    mutationFn: (patch: unknown) => api.updateSettings(patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      toast.success("Saved");
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const setSecret = useMutation({
    mutationFn: ({ name, value }: { name: string; value: string }) => api.setSecret(name, value),
    onSuccess: (_, v) => {
      setKeys((k) => ({ ...k, [v.name]: "" }));
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["doctor"] });
      qc.invalidateQueries({ queryKey: ["llm-models"] });
      toast.success("Key stored");
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const delSecret = useMutation({
    mutationFn: (name: string) => api.deleteSecret(name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["doctor"] });
    },
  });
  const forget = useMutation({ mutationFn: (d: string) => api.forgetTier(d), onSuccess: () => qc.invalidateQueries({ queryKey: ["tier-memory"] }) });

  if (!s.data) return <div className="p-6 text-muted-foreground">Loading…</div>;
  const st = s.data.settings;
  const role = (r: "designer" | "fallback") => (
    <div className="grid grid-cols-3 gap-2">
      <div>
        <Label>Provider</Label>
        <Select value={st.llm[r].provider} onValueChange={(v) => update.mutate({ llm: { [r]: { provider: v, model: DEFAULT_MODEL[v] ?? st.llm[r].model } } })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="anthropic">Claude (Anthropic API key)</SelectItem>
            <SelectItem value="gemini">Gemini (Google API key)</SelectItem>
            {st.llm.cli_login_enabled && r === "designer" && <SelectItem value="claude_code">Claude Code login (advanced, gray zone)</SelectItem>}
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label>Model {modelsFor(st.llm[r].provider).data?.source === "live" ? <span className="text-[10px] text-emerald-600">live list</span> : <span className="text-[10px] text-muted-foreground">defaults</span>}</Label>
        <Select value={st.llm[r].model} onValueChange={(v) => update.mutate({ llm: { [r]: { model: v } } })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            {(() => {
              const ids = (modelsFor(st.llm[r].provider).data?.models ?? []).map((m) => m.id);
              if (!ids.includes(st.llm[r].model)) ids.unshift(st.llm[r].model);
              return ids.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>);
            })()}
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label>Effort</Label>
        <Select value={st.llm[r].effort} onValueChange={(v) => update.mutate({ llm: { [r]: { effort: v } } })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>{["low", "medium", "high", "xhigh", "max"].map((e) => <SelectItem key={e} value={e}>{e}</SelectItem>)}</SelectContent>
        </Select>
      </div>
    </div>
  );

  return (
    <div className="p-6 space-y-4 max-w-4xl">
      <div><h1 className="text-xl font-semibold">Settings</h1><p className="text-sm text-muted-foreground">Data dir: <code>{s.data.data_dir}</code></p></div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2"><Plug className="size-4" /> Connect your agent (use your Claude / Gemini subscription)</CardTitle>
          <CardDescription>
            Your own Claude Code, Claude Desktop or Gemini CLI drives this app through its MCP server — no API key needed here, and the app never sees your login. Then say e.g. <code>/scrape https://books.toscrape.com/ title, price</code>.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {connect.data ? (
            <>
              <Snippet label="Claude Code" hint={connect.data.claude_code.auth ? `claude CLI: ${connect.data.claude_code.auth.loggedIn ? `logged in (${connect.data.claude_code.auth.subscriptionType ?? connect.data.claude_code.auth.authMethod})` : "not logged in"}` : "claude CLI not found on PATH"} code={connect.data.claude_code.add} />
              {connect.data.claude_code.plugin_add && <Snippet label="Claude Code plugin (adds the /scrape skill)" code={connect.data.claude_code.plugin_add} />}
              <Snippet label="Gemini CLI" code={connect.data.gemini_cli.add} />
              <Snippet label={`Claude Desktop — ${connect.data.claude_desktop.file}`} code={connect.data.claude_desktop.json} multiline />
            </>
          ) : (
            <div className="text-muted-foreground">Loading…</div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">AI providers (in-app, API keys)</CardTitle><CardDescription>Optional. Used only to design and repair recipes (and, if enabled, per-page fallback extraction). Crawls themselves never call an AI.</CardDescription></CardHeader>
        <CardContent className="space-y-4 text-sm">
          {(["anthropic_api_key", "gemini_api_key"] as const).map((name) => {
            const sec = s.data!.secrets[name];
            return (
              <div key={name} className="flex items-center gap-2">
                <Label className="w-40">{name === "anthropic_api_key" ? "Anthropic API key" : "Google (Gemini) key"}</Label>
                {sec.set ? (
                  <>
                    <Badge variant="secondary">{sec.masked} · {sec.source}</Badge>
                    <Button size="sm" variant="ghost" onClick={() => delSecret.mutate(name)}>Remove</Button>
                  </>
                ) : (
                  <>
                    <Input type="password" placeholder={`or set env ${sec.env}`} value={keys[name] ?? ""} onChange={(e) => setKeys((k) => ({ ...k, [name]: e.target.value }))} className="w-80" />
                    <Button size="sm" onClick={() => setSecret.mutate({ name, value: keys[name] })} disabled={!keys[name]}>Save</Button>
                  </>
                )}
              </div>
            );
          })}
          <Separator />
          <div><div className="font-medium mb-1">Designer (builds recipes)</div>{role("designer")}</div>
          <div><div className="font-medium mb-1">Fallback (per-page extraction when selectors fail)</div>{role("fallback")}</div>
          <div className="grid grid-cols-2 gap-3">
            <div><Label>Session budget (USD)</Label><Input type="number" step={0.5} defaultValue={st.llm.session_budget_usd} onBlur={(e) => update.mutate({ llm: { session_budget_usd: +e.target.value } })} /></div>
            <div><Label>Default per-run fallback budget (USD)</Label><Input type="number" step={0.5} defaultValue={st.llm.default_run_llm_budget_usd} onBlur={(e) => update.mutate({ llm: { default_run_llm_budget_usd: +e.target.value } })} /></div>
          </div>
          <p className="text-xs text-muted-foreground">In-app designer chat and per-page fallback use these keys (next phase). Subscription users: use the “Connect your agent” card above instead.</p>
        </CardContent>
      </Card>

      <LoginCard />

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Crawling</CardTitle></CardHeader>
        <CardContent className="grid grid-cols-2 gap-3 text-sm">
          <div className="flex items-center gap-2 col-span-2"><Switch checked={st.crawl.obey_robots} onCheckedChange={(v) => update.mutate({ crawl: { obey_robots: v } })} /><Label>Respect robots.txt</Label></div>
          <div><Label>Chrome executable (stealth browser tier)</Label><Input defaultValue={st.crawl.chrome_executable_path ?? ""} placeholder="auto-detect" onBlur={(e) => update.mutate({ crawl: { chrome_executable_path: e.target.value || null } })} /></div>
          <div><Label>Max concurrent runs</Label><Input type="number" min={1} max={8} defaultValue={st.max_concurrent_runs} onBlur={(e) => update.mutate({ max_concurrent_runs: +e.target.value || 1 })} /></div>
          <div className="col-span-2"><Label>Proxies (one per line, http/socks5, rotated)</Label><textarea className="w-full rounded-md border bg-background p-2 font-mono text-xs h-20" defaultValue={st.crawl.proxies.join("\n")} onBlur={(e) => update.mutate({ crawl: { proxies: e.target.value.split("\n").map((x) => x.trim()).filter(Boolean) } })} /></div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Notifications & storage</CardTitle><CardDescription>Scheduled runs notify you when they finish (with the diff). Retention caps keep the data dir small; the prune runs daily.</CardDescription></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="flex items-center gap-2">
            <Switch
              checked={st.retention.notifications}
              onCheckedChange={(v) => {
                update.mutate({ retention: { notifications: v } });
                if (v && typeof Notification !== "undefined" && Notification.permission === "default") Notification.requestPermission().then(setNotifPerm);
              }}
            />
            <Label>Desktop notifications</Label>
            <span className="text-xs text-muted-foreground">browser permission: {notifPerm}{notifPerm === "denied" && " (allow it in your browser's site settings)"}</span>
            {notifPerm === "default" && <Button size="sm" variant="ghost" onClick={() => Notification.requestPermission().then(setNotifPerm)}>Allow</Button>}
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div><Label>Keep runs per recipe</Label><Input type="number" min={1} defaultValue={st.retention.keep_runs_per_recipe} onBlur={(e) => update.mutate({ retention: { keep_runs_per_recipe: +e.target.value || 1 } })} /></div>
            <div><Label>Keep cached pages per recipe</Label><Input type="number" min={1} defaultValue={st.retention.keep_samples_per_recipe} onBlur={(e) => update.mutate({ retention: { keep_samples_per_recipe: +e.target.value || 1 } })} /></div>
            <div><Label>Keep days</Label><Input type="number" min={1} defaultValue={st.retention.keep_days} onBlur={(e) => update.mutate({ retention: { keep_days: +e.target.value || 1 } })} /></div>
          </div>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            {storage.data && <span>{(storage.data.data_size_bytes / 1_048_576).toFixed(1)} MB · {storage.data.runs} runs · {storage.data.samples} cached pages</span>}
            <Button size="sm" variant="outline" onClick={() => prune.mutate()} disabled={prune.isPending}>Prune now</Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Domain tier memory</CardTitle><CardDescription>Which fetch tier worked last per domain. Forget one to retry from plain HTTP.</CardDescription></CardHeader>
        <CardContent className="text-sm space-y-1">
          {Object.entries(tiers.data ?? {}).length === 0 && <div className="text-muted-foreground">Empty.</div>}
          {Object.entries(tiers.data ?? {}).map(([d, t]) => (
            <div key={d} className="flex items-center justify-between rounded border px-2 py-1"><span><code>{d}</code> → <Badge variant="outline">{t}</Badge></span><Button size="sm" variant="ghost" onClick={() => forget.mutate(d)}>Forget</Button></div>
          ))}
        </CardContent>
      </Card>

      <Card className="border-dashed">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Advanced: use my Claude Code login inside the app</CardTitle>
          <CardDescription>
            Reuses the <code>claude</code> CLI's login through the Claude Agent SDK so the in-app designer runs on your subscription — instead of the compliant path (your own Claude Code / Claude Desktop / Gemini CLI driving the app via MCP, see “Connect your agent”). Off by default.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <blockquote className="border-l-2 pl-3 text-xs text-muted-foreground italic">“{POLICY_QUOTE}” — Anthropic, Claude Agent SDK documentation.</blockquote>
          <p className="text-xs text-muted-foreground">Enabling this may violate Anthropic's terms for third-party products; you accept that risk. Requires <code>pip install 'scrapy-awesome[claude-code]'</code> and a logged-in <code>claude</code> CLI. If the SDK stops inheriting the CLI login, this simply fails and you can switch back.</p>
          <div className="flex items-center gap-2">
            <Switch
              checked={st.llm.cli_login_enabled}
              onCheckedChange={(v) => {
                if (v && !window.confirm(`Enable “use my Claude Code login”?\n\n${POLICY_QUOTE}\n\nI understand this is a gray zone and I accept the risk.`)) return;
                update.mutate({ llm: { cli_login_enabled: v, ...(v ? {} : st.llm.designer.provider === "claude_code" ? { designer: { provider: "anthropic", model: "claude-opus-5" } } : {}) } });
              }}
            />
            <Label>Enable (I have read the note above)</Label>
            {st.llm.cli_login_enabled && <span className="text-xs text-muted-foreground">Then pick “Claude Code login” as the designer provider above.</span>}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Doctor</CardTitle></CardHeader>
        <CardContent className="text-sm space-y-1">
          {(doctor.data ?? []).map((c) => (
            <div key={c.name} className="flex gap-2 items-start"><Badge className={c.status === "ok" ? "bg-emerald-600" : c.status === "warn" ? "bg-amber-500" : "bg-red-600"}>{c.status}</Badge><span className="w-56 shrink-0">{c.name}</span><span className="text-muted-foreground break-all">{c.detail}</span></div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}


function Snippet({ label, code, hint, multiline }: { label: string; code: string; hint?: string; multiline?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <Label className="text-xs">{label}{hint && <span className="ml-2 text-muted-foreground font-normal">· {hint}</span>}</Label>
        <Button size="sm" variant="ghost" onClick={copy}>{copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />} {copied ? "Copied" : "Copy"}</Button>
      </div>
      <pre className={`rounded-md border bg-muted/40 p-2 font-mono text-xs overflow-x-auto ${multiline ? "" : "whitespace-pre-wrap break-all"}`}>{code}</pre>
    </div>
  );
}


function LoginCard() {
  const auth = useQuery({ queryKey: ["auth", "status"], queryFn: api.authStatus });
  const [username, setUsername] = useState("");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState("");
  useEffect(() => { if (auth.data?.username) setUsername(auth.data.username); }, [auth.data?.username]);

  const save = useMutation({
    mutationFn: () => api.authChangePassword(current, next, username),
    onSuccess: (r) => {
      setCurrent(""); setNext(""); setMsg("");
      toast.success(`Password changed for ${r.username}. Other browsers were signed out.`);
      auth.refetch();
    },
    onError: (e: Error) => setMsg(((e as ApiError).body as { detail?: string })?.detail ?? e.message),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Login</CardTitle>
        <CardDescription>
          The username and password this app signs in with. Forgotten it? <code>scrapy-awesome passwd</code>{" "}
          sets a new one from the terminal (and <code>--reset</code> clears it back to first-run setup).
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-3 text-sm">
        <div><Label>Username</Label><Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></div>
        <div />
        <div><Label>Current password</Label><Input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} /></div>
        <div><Label>New password</Label><Input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} /></div>
        {msg && <p className="col-span-2 text-destructive">{msg}</p>}
        <div className="col-span-2">
          <Button size="sm" disabled={!current || !next || save.isPending} onClick={() => save.mutate()}>
            Change password
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

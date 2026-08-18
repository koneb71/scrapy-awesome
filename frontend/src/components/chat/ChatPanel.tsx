import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, Bot, Check, ChevronDown, Loader2, Plus, Send, Sparkles, Square, Wrench, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useChatEvents } from "@/lib/ws";
import type { Chat, ChatMessage, ChatToolCall } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

const PROVIDER_LABEL: Record<string, string> = { anthropic: "Claude", gemini: "Gemini", claude_code: "Claude (Claude Code login)" };
const DEFAULT_MODEL: Record<string, string> = { anthropic: "claude-opus-5", gemini: "gemini-3.7-flash" };

/**
 * In-app designer chat (Claude / Gemini via the user's API key). One chat per recipe by default;
 * everything the assistant does through tools lands in the recipe/preview live.
 */
export function ChatPanel({
  recipeId,
  onClose,
  initialPrompt,
  onConsumedInitialPrompt,
  onTurnEnd,
}: {
  recipeId: string | null;
  onClose?: () => void;
  /** sent automatically once (e.g. from New → "Design with AI", or "Fix this column") */
  initialPrompt?: string | null;
  onConsumedInitialPrompt?: () => void;
  /** called when an assistant turn finishes (the editor re-validates the preview) */
  onTurnEnd?: () => void;
}) {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const chats = useQuery({ queryKey: ["chats", recipeId], queryFn: () => api.chats(recipeId ?? undefined) });
  const [chatId, setChatId] = useState<string | null>(null);
  const [autoPick, setAutoPick] = useState(true); // false after "New chat" so we don't re-select the old one
  const [draft, setDraft] = useState("");
  const [live, setLive] = useState<{ text: string; tools: ChatToolCall[]; running: boolean; usage?: Chat["usage"] }>({ text: "", tools: [], running: false });
  const bottom = useRef<HTMLDivElement>(null);

  // pick the latest chat for this recipe once loaded
  useEffect(() => {
    if (chatId || !chats.data || !autoPick) return;
    if (chats.data.length) setChatId(chats.data[0].id);
  }, [chats.data, chatId, autoPick]);
  const chat = useQuery({ queryKey: ["chat", chatId], queryFn: () => api.chat(chatId!), enabled: !!chatId });

  const provider = chat.data?.provider ?? settings.data?.settings.llm.designer.provider ?? "anthropic";
  const model = chat.data?.model ?? settings.data?.settings.llm.designer.model ?? "";
  const keySet = !!settings.data?.fake_llm || provider === "claude_code" || !!settings.data?.secrets[provider === "gemini" ? "gemini_api_key" : "anthropic_api_key"]?.set;

  const onEvent = useCallback(
    (ev: Record<string, unknown> & { t: string }) => {
      if (ev.t === "turn_start") setLive({ text: "", tools: [], running: true });
      else if (ev.t === "snapshot") setLive({ text: String(ev.content ?? ""), tools: (ev.tool_calls as ChatToolCall[]) ?? [], running: true, usage: ev.usage as Chat["usage"] });
      else if (ev.t === "text_delta") setLive((l) => ({ ...l, text: l.text + String(ev.text ?? "") }));
      else if (ev.t === "tool_call") setLive((l) => ({ ...l, tools: [...l.tools, { name: String(ev.name), input: (ev.input as Record<string, unknown>) ?? null, ok: null, summary: "" }] }));
      else if (ev.t === "tool_result")
        setLive((l) => {
          const tools = [...l.tools];
          const i = tools.findIndex((t) => t.name === ev.name && t.ok === null);
          if (i >= 0) tools[i] = { ...tools[i], ok: Boolean(ev.ok), summary: String(ev.summary ?? "") };
          else tools.push({ name: String(ev.name), input: null, ok: Boolean(ev.ok), summary: String(ev.summary ?? "") });
          return { ...l, tools };
        });
      else if (ev.t === "usage") setLive((l) => ({ ...l, usage: ev as Chat["usage"] }));
      else if (ev.t === "error") toast.error(String(ev.message ?? "error"));
      else if (ev.t === "turn_end") {
        setLive((l) => ({ ...l, running: false }));
        qc.invalidateQueries({ queryKey: ["chat", chatId] });
        qc.invalidateQueries({ queryKey: ["chats", recipeId] });
        // the assistant may have saved/validated the recipe or fetched pages
        if (recipeId) {
          qc.invalidateQueries({ queryKey: ["recipe", recipeId] });
          qc.invalidateQueries({ queryKey: ["samples", recipeId] });
        }
        onTurnEnd?.();
      }
    },
    [chatId, qc, recipeId, onTurnEnd],
  );
  useChatEvents(chatId, onEvent);

  const send = useMutation({
    mutationFn: async (content: string) => {
      let id = chatId;
      if (!id) {
        const c = await api.createChat({ recipe_id: recipeId });
        id = c.id;
        setChatId(id);
        qc.invalidateQueries({ queryKey: ["chats", recipeId] });
      }
      // optimistic: show the user's message immediately
      setLive({ text: "", tools: [], running: true });
      return api.sendChat(id, content);
    },
    onSuccess: (c) => {
      qc.setQueryData(["chat", c.id], c);
      setDraft("");
    },
    onError: (e: Error) => {
      setLive((l) => ({ ...l, running: false }));
      const msg = e instanceof ApiError && e.status === 400 ? e.message : e.message;
      toast.error(msg);
    },
  });
  const cancel = useMutation({ mutationFn: () => api.cancelChat(chatId!), onSuccess: () => toast.info("Cancelled") });

  // auto-send an initial prompt (once)
  const sentInitial = useRef(false);
  useEffect(() => {
    if (!initialPrompt || sentInitial.current || send.isPending || settings.isLoading) return;
    if (!keySet) return; // the empty state explains what to do
    sentInitial.current = true;
    send.mutate(initialPrompt);
    onConsumedInitialPrompt?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPrompt, keySet, settings.isLoading]);

  const messages: ChatMessage[] = useMemo(() => chat.data?.messages ?? [], [chat.data]);
  const running = live.running || chat.data?.status === "running";
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length, live.text, live.tools.length]);

  const usage = live.usage ?? chat.data?.usage;
  const budget = settings.data?.settings.llm.session_budget_usd;

  const submit = () => {
    const t = draft.trim();
    if (!t || running) return;
    send.mutate(t);
  };

  return (
    <div className="flex h-full flex-col border-l bg-background">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <Sparkles className="size-4 text-primary" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium leading-tight">AI designer</div>
          <div className="text-[11px] text-muted-foreground truncate">
            {settings.data?.fake_llm ? <span className="text-amber-600">offline fake designer (SA_FAKE_LLM)</span> : <>Powered by {PROVIDER_LABEL[provider] ?? provider} · <code>{model}</code></>}
            {usage?.calls ? <> · {usage.calls} calls · {provider === "claude_code" ? "subscription" : `$${(usage.cost_usd ?? 0).toFixed(3)}${budget ? ` / $${budget}` : ""}`}</> : null}
          </div>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button size="icon" variant="ghost" onClick={() => { setAutoPick(false); setChatId(null); setLive({ text: "", tools: [], running: false }); }} disabled={running} aria-label="New chat"><Plus className="size-4" /></Button>
          </TooltipTrigger>
          <TooltipContent>New chat</TooltipContent>
        </Tooltip>
        {onClose && <Button size="icon" variant="ghost" onClick={onClose} aria-label="Close"><X className="size-4" /></Button>}
      </div>

      <ScrollArea className="flex-1 min-h-0">
        <div className="p-3 space-y-3 text-sm">
          {!keySet && !settings.isLoading && (
            <div className="rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-950/30 p-3 text-xs space-y-1">
              <div className="flex items-center gap-1 font-medium"><AlertTriangle className="size-3.5" /> No {PROVIDER_LABEL[provider] ?? provider} API key</div>
              <div>Add one in <a className="underline" href="/settings">Settings → AI providers</a>, or use your Claude Code / Gemini CLI subscription through <a className="underline" href="/settings">Connect your agent</a>. You can also keep editing fields manually.</div>
            </div>
          )}
          {messages.length === 0 && !live.running && keySet && (
            <div className="text-xs text-muted-foreground space-y-2">
              <p>Describe what you want from this site. The assistant fetches the page, tests selectors, saves the recipe and validates it — you'll see every step here and in the editor.</p>
              <div className="flex flex-wrap gap-1">
                {["Build the recipe from my intent", "Follow detail pages for the description", "Add pagination", "Which fields are unreliable?"].map((q) => (
                  <button key={q} className="rounded-full border px-2 py-0.5 hover:bg-accent" onClick={() => setDraft(q)}>{q}</button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => <Message key={i} m={m} />)}
          {live.running && (
            <div className="space-y-2">
              {live.tools.length > 0 && <ToolChips tools={live.tools} />}
              {live.text ? <AssistantText text={live.text} /> : <div className="flex items-center gap-2 text-muted-foreground text-xs"><Loader2 className="size-3.5 animate-spin" /> thinking…</div>}
            </div>
          )}
          {chat.data?.error && !running && <div className="text-xs text-red-600">{chat.data.error}</div>}
          <div ref={bottom} />
        </div>
      </ScrollArea>

      <div className="border-t p-2 space-y-1">
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={keySet ? "e.g. all products with name, price and rating; open each for the description" : "Add an API key in Settings to chat here"}
          rows={2}
          disabled={!keySet}
          className="text-sm"
        />
        <div className="flex items-center justify-between">
          <ModelPicker chat={chat.data ?? null} provider={provider} model={model} disabled={running || !!chatId} cliLogin={!!settings.data?.settings.llm.cli_login_enabled} onChange={(p, m) => { qc.setQueryData(["settings"], undefined); api.updateSettings({ llm: { designer: { provider: p, model: m } } }).then(() => qc.invalidateQueries({ queryKey: ["settings"] })); }} />
          {running ? (
            <Button size="sm" variant="secondary" onClick={() => cancel.mutate()}><Square className="size-3.5" /> Stop</Button>
          ) : (
            <Button size="sm" onClick={submit} disabled={!draft.trim() || !keySet || send.isPending}>{send.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />} Send</Button>
          )}
        </div>
      </div>
    </div>
  );
}

function ModelPicker({ chat, provider, model, disabled, cliLogin, onChange }: { chat: Chat | null; provider: string; model: string; disabled: boolean; cliLogin?: boolean; onChange: (p: string, m: string) => void }) {
  const models = useQuery({ queryKey: ["llm-models", provider], queryFn: () => api.llmModels(provider), enabled: !chat });
  const [open, setOpen] = useState(false);
  if (chat) return <div className="text-[11px] text-muted-foreground">Model is fixed per chat — start a new chat to switch.</div>;
  return (
    <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
      <button className="flex items-center gap-1 rounded px-1 hover:bg-accent" onClick={() => setOpen((o) => !o)}>
        {PROVIDER_LABEL[provider] ?? provider} · {model || "…"} <ChevronDown className="size-3" />
      </button>
      {open && (
        <div className="flex items-center gap-1">
          <Select value={provider} onValueChange={(p) => onChange(p, DEFAULT_MODEL[p] ?? "")}>
            <SelectTrigger className="h-7 w-28 text-xs" disabled={disabled}><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="anthropic">Claude (API key)</SelectItem><SelectItem value="gemini">Gemini (API key)</SelectItem>{cliLogin && <SelectItem value="claude_code">Claude Code login</SelectItem>}</SelectContent>
          </Select>
          <Select value={model} onValueChange={(m) => onChange(provider, m)}>
            <SelectTrigger className="h-7 w-52 text-xs" disabled={disabled}><SelectValue placeholder="model" /></SelectTrigger>
            <SelectContent>{(models.data?.models ?? []).map((m) => <SelectItem key={m.id} value={m.id}>{m.display_name || m.id}</SelectItem>)}</SelectContent>
          </Select>
          {models.data?.source === "fallback" && <span title={models.data.error}>(defaults)</span>}
        </div>
      )}
    </div>
  );
}

function Message({ m }: { m: ChatMessage }) {
  if (m.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[92%] rounded-2xl rounded-br-sm bg-primary text-primary-foreground px-3 py-2 whitespace-pre-wrap break-words">{m.content}</div>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {m.tool_calls && m.tool_calls.length > 0 && <ToolChips tools={m.tool_calls} />}
      {m.content && <AssistantText text={m.content} />}
      {m.stop_reason && m.stop_reason !== "end_turn" && <div className="text-[11px] text-muted-foreground">stopped: {m.stop_reason}</div>}
    </div>
  );
}

function AssistantText({ text }: { text: string }) {
  return (
    <div className="flex gap-2">
      <Bot className="size-4 shrink-0 mt-1 text-muted-foreground" />
      <div className="min-w-0 flex-1 whitespace-pre-wrap break-words leading-relaxed">{renderInline(text)}</div>
    </div>
  );
}

/** Tiny inline markdown: `code`, **bold**, and bullet lines — enough for terse assistant replies. */
function renderInline(text: string) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => {
    if (p.startsWith("`") && p.endsWith("`")) return <code key={i} className="rounded bg-muted px-1 py-0.5 text-[12px]">{p.slice(1, -1)}</code>;
    if (p.startsWith("**") && p.endsWith("**")) return <strong key={i}>{p.slice(2, -2)}</strong>;
    return <span key={i}>{p}</span>;
  });
}

function ToolChips({ tools }: { tools: ChatToolCall[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {tools.map((t, i) => (
        <Tooltip key={i}>
          <TooltipTrigger asChild>
            <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-mono", t.ok === false ? "border-red-300 text-red-700 dark:text-red-300" : t.ok === true ? "border-emerald-300 text-emerald-700 dark:text-emerald-300" : "text-muted-foreground")}>
              {t.ok === null ? <Loader2 className="size-3 animate-spin" /> : t.ok ? <Check className="size-3" /> : <AlertTriangle className="size-3" />}
              <Wrench className="size-3" /> {t.name}
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-sm">
            <div className="text-xs font-mono whitespace-pre-wrap break-all">{t.input ? JSON.stringify(t.input).slice(0, 300) : ""}</div>
            {t.summary && <div className="text-xs mt-1">{t.summary}</div>}
          </TooltipContent>
        </Tooltip>
      ))}
    </div>
  );
}

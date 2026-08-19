import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Activity, BookOpen, Bot, CalendarClock, KeyRound, Plus, Settings, Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import { useAppEvents } from "@/lib/ws";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

const NAV = [
  { to: "/", label: "New", icon: Plus, end: true },
  { to: "/recipes", label: "Recipes", icon: BookOpen },
  { to: "/runs", label: "Runs", icon: Activity },
  { to: "/schedules", label: "Schedules", icon: CalendarClock },
  { to: "/sessions", label: "Sessions", icon: KeyRound },
  { to: "/settings", label: "Settings", icon: Settings },
];

export default function App() {
  const [unauth, setUnauth] = useState(false);
  const nav = useNavigate();
  const qc = useQueryClient();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 15_000 });
  const pending = useQuery({ queryKey: ["picks", "pending"], queryFn: api.pendingPicks, refetchInterval: 30_000 });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 60_000 });
  const notificationsOn = settings.data?.settings.retention?.notifications ?? true;
  useEffect(() => {
    const h = () => setUnauth(true);
    window.addEventListener("sa:unauthorized", h);
    return () => window.removeEventListener("sa:unauthorized", h);
  }, []);

  // Agent hand-offs (Claude Code / Gemini CLI via the MCP server): pick requests + navigation.
  useAppEvents((ev) => {
    window.dispatchEvent(new CustomEvent("sa:event", { detail: ev }));
    if (ev.t === "pick_request") {
      qc.invalidateQueries({ queryKey: ["picks"] });
      const id = String(ev.id);
      toast(String(ev.prompt), {
        id: `pick-${id}`,
        description: "Your agent asks you to click an element.",
        icon: <Bot className="size-4" />,
        duration: 60_000,
        action: { label: "Pick", onClick: () => nav(`/pick/${id}`) },
      });
      if (!location.pathname.startsWith("/pick/")) nav(`/pick/${id}`);
    } else if (ev.t === "pick_answered" || ev.t === "pick_cancelled") {
      qc.invalidateQueries({ queryKey: ["picks"] });
      qc.invalidateQueries({ queryKey: ["pick", String(ev.id)] });
      toast.dismiss(`pick-${String(ev.id)}`);
    } else if (ev.t === "navigate" && typeof ev.route === "string") {
      nav(ev.route);
    } else if (ev.t === "notify") {
      const title = String(ev.title ?? "scrapy-awesome");
      const body = String(ev.body ?? "");
      const route = typeof ev.route === "string" ? ev.route : undefined;
      const fn = ev.level === "error" ? toast.error : ev.level === "warning" ? toast.warning : toast.success;
      fn(title, { description: body, duration: 12_000, action: route ? { label: "Open", onClick: () => nav(route) } : undefined });
      // OS notification: Tauri plugin inside the desktop app, browser Notification API on the web
      const tauriNotif = (window as unknown as { __TAURI__?: { notification?: { isPermissionGranted: () => Promise<boolean>; requestPermission: () => Promise<string>; sendNotification: (o: { title: string; body: string }) => void } } }).__TAURI__?.notification;
      if (notificationsOn && tauriNotif) {
        tauriNotif.isPermissionGranted().then(async (ok) => {
          const granted = ok || (await tauriNotif.requestPermission()) === "granted";
          if (granted) tauriNotif.sendNotification({ title, body });
        }).catch(() => undefined);
      } else if (notificationsOn && typeof Notification !== "undefined" && Notification.permission === "granted" && document.hidden) {
        try {
          const n = new Notification(title, { body, tag: String(ev.run_id ?? title) });
          n.onclick = () => { window.focus(); if (route) nav(route); };
        } catch { /* ignore */ }
      }
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["schedules"] });
    } else if (ev.t === "schedule_started") {
      qc.invalidateQueries({ queryKey: ["schedules"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
    } else if (ev.t === "done" || ev.t === "status") {
      qc.invalidateQueries({ queryKey: ["runs"] });
    }
  });
  const pendingPick = (pending.data ?? [])[0];

  return (
    <div className="min-h-screen bg-background text-foreground flex">
      <aside className="w-56 shrink-0 border-r bg-muted/30 flex flex-col">
        <div className="px-4 py-4 flex items-center gap-2 border-b">
          <Sparkles className="size-5 text-primary" />
          <div>
            <div className="font-semibold leading-tight">scrapy-awesome</div>
            <div className="text-[11px] text-muted-foreground">
              {health.data ? `v${health.data.version} · ${health.data.active_runs} active` : "connecting…"}
            </div>
          </div>
        </div>
        <nav className="p-2 flex flex-col gap-1">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm hover:bg-accent",
                  isActive && "bg-accent font-medium",
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        {pendingPick && (
          <button
            onClick={() => nav(`/pick/${pendingPick.id}`)}
            className="mx-2 mb-2 rounded-md border border-blue-300 bg-blue-50 dark:bg-blue-950/40 px-3 py-2 text-left text-xs hover:bg-blue-100 dark:hover:bg-blue-900/40"
          >
            <div className="flex items-center gap-1 font-medium text-blue-700 dark:text-blue-300"><Bot className="size-3.5" /> Agent is waiting</div>
            <div className="text-muted-foreground line-clamp-2">{pendingPick.prompt}</div>
          </button>
        )}
        <div className="mt-auto p-3 text-[11px] text-muted-foreground leading-relaxed">
          Local-first. Everything stays on this machine.
        </div>
      </aside>
      <main className="flex-1 min-w-0 overflow-auto">
        {unauth && (
          <div className="p-4">
            <Alert variant="destructive">
              <AlertTitle>Not signed in to the local server</AlertTitle>
              <AlertDescription>
                This page needs the sign-in link, which carries the local server's token. Run{" "}
                <code>scrapy-awesome open</code> in a terminal — it prints the link for the server
                already running and opens it here.
              </AlertDescription>
            </Alert>
          </div>
        )}
        <Outlet />
      </main>
    </div>
  );
}

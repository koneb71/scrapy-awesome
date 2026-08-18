import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { KeyRound, RefreshCw, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export default function SessionsPage() {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions, refetchInterval: (q) => ((q.state.data ?? []).some((s) => s.status === "pending") ? 2000 : false) });
  const create = useMutation({
    mutationFn: () => api.createSession({ name, url: url.match(/^https?:\/\//) ? url : `https://${url}` }),
    onSuccess: () => {
      toast.success("A browser window opened — log in there, then click “Done — save session”.");
      setUrl("");
      setName("");
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const browsers = useQuery({ queryKey: ["import-browsers"], queryFn: api.importBrowsers });
  const [impBrowser, setImpBrowser] = useState("chrome");
  const [impDomain, setImpDomain] = useState("");
  const importCookies = useMutation({
    mutationFn: () => api.importSession({ browser: impBrowser, domain: impDomain }),
    onSuccess: (s) => {
      toast.success(`Imported ${s.cookies} cookies for ${s.domain}`);
      setImpDomain("");
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const refresh = useMutation({ mutationFn: (id: string) => api.refreshSession(id), onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }) });
  const del = useMutation({ mutationFn: (id: string) => api.deleteSession(id), onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }) });

  return (
    <div className="p-6 space-y-4 max-w-4xl">
      <div>
        <h1 className="text-xl font-semibold">Login sessions</h1>
        <p className="text-sm text-muted-foreground">
          For sites that need you to be signed in. A real browser window opens; you log in yourself; only the resulting cookies/local storage are saved — locally, and never shown to any AI.
        </p>
      </div>
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Log in once</CardTitle><CardDescription>Then pick this session in a recipe's Plan tab. Runs use the interactive tier with it.</CardDescription></CardHeader>
        <CardContent className="flex gap-2 flex-wrap">
          <Input placeholder="Name (optional)" value={name} onChange={(e) => setName(e.target.value)} className="w-48" />
          <Input placeholder="https://site.example/login" value={url} onChange={(e) => setUrl(e.target.value)} className="flex-1 min-w-[280px]" />
          <Button onClick={() => create.mutate()} disabled={!url || create.isPending}><KeyRound className="size-4" /> Open login window</Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-base">Import from a browser</CardTitle><CardDescription>Copy the cookies you already have for a site from your installed browser (optional extra: <code>pip install 'scrapy-awesome[cookies]'</code>). Chromium browsers may ask for your keychain password; on Windows close the browser first.</CardDescription></CardHeader>
        <CardContent className="flex gap-2 flex-wrap items-center">
          <Select value={impBrowser} onValueChange={setImpBrowser}>
            <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent>{(browsers.data?.browsers ?? ["chrome", "firefox", "safari"]).map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}</SelectContent>
          </Select>
          <Input placeholder="example.com" value={impDomain} onChange={(e) => setImpDomain(e.target.value)} className="w-64" />
          <Button variant="secondary" onClick={() => importCookies.mutate()} disabled={!impDomain || importCookies.isPending}>Import cookies</Button>
          {browsers.data && !browsers.data.available && <span className="text-xs text-muted-foreground">extra not installed</span>}
        </CardContent>
      </Card>
      <Card>
        <Table>
          <TableHeader><TableRow><TableHead>Name</TableHead><TableHead>Domain</TableHead><TableHead>Status</TableHead><TableHead>Cookies</TableHead><TableHead>Last used</TableHead><TableHead /></TableRow></TableHeader>
          <TableBody>
            {(sessions.data ?? []).map((s) => (
              <TableRow key={s.id}>
                <TableCell className="font-medium">{s.name}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{s.domain}</TableCell>
                <TableCell><Badge variant={s.status === "ready" ? "default" : s.status === "pending" ? "secondary" : "destructive"}>{s.status}</Badge>{s.error && <span className="ml-2 text-xs text-red-600">{s.error}</span>}</TableCell>
                <TableCell>{s.cookies}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{s.last_used_at ? new Date(s.last_used_at).toLocaleString() : "—"}</TableCell>
                <TableCell><div className="flex gap-1 justify-end"><Button size="sm" variant="outline" onClick={() => refresh.mutate(s.id)}><RefreshCw className="size-3.5" /> Renew</Button><Button size="icon" variant="ghost" onClick={() => del.mutate(s.id)}><Trash2 className="size-3.5" /></Button></div></TableCell>
              </TableRow>
            ))}
            {sessions.data?.length === 0 && <TableRow><TableCell colSpan={6} className="text-center text-muted-foreground py-8">No sessions yet.</TableCell></TableRow>}
          </TableBody>
        </Table>
      </Card>
    </div>
  );
}

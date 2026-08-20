import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Loader2, Lock } from "lucide-react";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * The one page that works without a session. On a machine with no login yet it creates one
 * instead of asking for it — a fresh install should not need a terminal to get in.
 */
export default function LoginPage() {
  const nav = useNavigate();
  const status = useQuery({ queryKey: ["auth", "status"], queryFn: api.authStatus });
  const configured = status.data?.configured ?? true;
  const minPassword = status.data?.min_password ?? 8;

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (status.data?.authenticated) nav("/", { replace: true });
    if (status.data?.username) setUsername(status.data.username);
  }, [status.data, nav]);

  const submit = useMutation({
    mutationFn: async () => {
      if (!configured) {
        if (password !== confirm) throw new Error("the two passwords do not match");
        if (password.length < minPassword) throw new Error(`password must be at least ${minPassword} characters`);
        return api.authSetup(username, password);
      }
      return api.authLogin(username, password);
    },
    onSuccess: () => {
      setError("");
      window.location.assign("/"); // full load: every query refetches with the new session
    },
    onError: (e: Error) => setError(((e as ApiError).body as { detail?: string })?.detail ?? e.message),
  });

  return (
    <div className="min-h-dvh flex items-center justify-center bg-muted/30 p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="flex items-center gap-2">
            <Lock className="size-4" />
            <CardTitle className="text-base">
              {configured ? "Sign in to scrapy-awesome" : "Create your login"}
            </CardTitle>
          </div>
          <CardDescription>
            {configured
              ? "This app runs on your machine; the login keeps anything else on it out of your scrapes and sessions."
              : "Pick a username and password for this machine. Forgot it later? Run scrapy-awesome passwd."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              submit.mutate();
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                autoFocus
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete={configured ? "current-password" : "new-password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            {!configured && (
              <div className="space-y-1.5">
                <Label htmlFor="confirm">Confirm password</Label>
                <Input
                  id="confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                />
              </div>
            )}
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Button type="submit" className="w-full" disabled={submit.isPending || !username || !password}>
              {submit.isPending && <Loader2 className="size-4 animate-spin" />}
              {configured ? "Sign in" : "Create login"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

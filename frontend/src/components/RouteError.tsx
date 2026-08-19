import { useEffect, useState } from "react";
import { useRouteError } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

/**
 * What a route shows when it throws.
 *
 * The common case here is not a bug: this app is rebuilt while its tabs stay open (a run page
 * left up for hours, a schedule watch), and `pnpm build` gives every route chunk a new hash. The
 * old tab then asks for a chunk that no longer exists, gets an honest 404, and React Router's
 * default screen offers no way out but a manual reload. One reload fixes it, so do that — once,
 * so a genuinely broken build cannot spin.
 */
const STALE = /dynamically imported module|Importing a module script failed|error loading dynamically imported module/i;
const RELOAD_KEY = "sa:chunk-reload-at";
const RELOAD_WINDOW_MS = 30_000;

export function RouteError() {
  const error = useRouteError() as Error | undefined;
  const message = String(error?.message ?? error ?? "unknown error");
  const stale = STALE.test(message);
  const [reloading, setReloading] = useState(false);

  useEffect(() => {
    if (!stale) return;
    const last = Number(sessionStorage.getItem(RELOAD_KEY) ?? 0);
    if (Date.now() - last < RELOAD_WINDOW_MS) return; // already tried: show the message instead
    sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
    setReloading(true);
    location.reload();
  }, [stale]);

  if (reloading) return <div className="p-6 text-sm text-muted-foreground">Updating…</div>;

  return (
    <div className="p-4">
      <Alert variant="destructive">
        <AlertTitle>{stale ? "This tab is running an older build" : "This page hit an error"}</AlertTitle>
        <AlertDescription className="space-y-2">
          <p>
            {stale
              ? "The app was rebuilt while this tab was open, so part of it is no longer on the server. Reloading picks up the new build — your recipes and runs are untouched."
              : message}
          </p>
          <Button size="sm" variant="secondary" onClick={() => location.reload()}>
            Reload
          </Button>
        </AlertDescription>
      </Alert>
    </div>
  );
}

export default RouteError;

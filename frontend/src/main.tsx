import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./index.css";
import App from "./App";
import NewPage from "./routes/NewPage";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

// Route-level code splitting: the editor (picker, preview) and run page are the heavy ones.
const RecipesPage = lazy(() => import("./routes/RecipesPage"));
const RecipeEditor = lazy(() => import("./routes/RecipeEditor"));
const RunsPage = lazy(() => import("./routes/RunsPage"));
const RunPage = lazy(() => import("./routes/RunPage"));
const SessionsPage = lazy(() => import("./routes/SessionsPage"));
const SettingsPage = lazy(() => import("./routes/SettingsPage"));
const PickPage = lazy(() => import("./routes/PickPage"));
const SchedulesPage = lazy(() => import("./routes/SchedulesPage"));

const qc = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 } },
});

const fallback = <div className="p-6 text-sm text-muted-foreground">Loading…</div>;
const lazyEl = (el: React.ReactNode) => <Suspense fallback={fallback}>{el}</Suspense>;

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <NewPage /> },
      { path: "recipes", element: lazyEl(<RecipesPage />) },
      { path: "recipes/:id", element: lazyEl(<RecipeEditor />) },
      { path: "runs", element: lazyEl(<RunsPage />) },
      { path: "runs/:id", element: lazyEl(<RunPage />) },
      { path: "sessions", element: lazyEl(<SessionsPage />) },
      { path: "settings", element: lazyEl(<SettingsPage />) },
      { path: "pick/:id", element: lazyEl(<PickPage />) },
      { path: "schedules", element: lazyEl(<SchedulesPage />) },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <TooltipProvider delayDuration={300}>
        <RouterProvider router={router} />
        <Toaster richColors position="bottom-right" />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);

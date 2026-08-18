import type {
  Analysis,
  Chat,
  ConnectInfo,
  DiffSummary,
  FailedPage,
  ModelList,
  Schedule,
  ScheduleIn,
  PickRequest,
  Recipe,
  RecipeRow,
  RunRow,
  Sample,
  SessionRow,
  SettingsResponse,
  ValidationReport,
} from "./types";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown) {
    super(typeof body === "string" ? body : (body as { detail?: unknown })?.detail ? JSON.stringify((body as { detail: unknown }).detail) : `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(method: string, path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "content-type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    ...init,
  });
  const text = await res.text();
  let data: unknown = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* plain text */
  }
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent("sa:unauthorized"));
    throw new ApiError(res.status, data);
  }
  return data as T;
}

const get = <T,>(p: string) => request<T>("GET", p);
const post = <T,>(p: string, b?: unknown) => request<T>("POST", p, b);
const put = <T,>(p: string, b?: unknown) => request<T>("PUT", p, b);
const del = <T,>(p: string) => request<T>("DELETE", p);

export const api = {
  health: () => get<{ ok: boolean; version: string; active_runs: number }>("/health"),

  // settings
  settings: () => get<SettingsResponse>("/api/settings"),
  updateSettings: (patch: unknown) => put<{ settings: SettingsResponse["settings"] }>("/api/settings", patch),
  setSecret: (name: string, value: string) => put<{ name: string; source: string; masked: string }>(`/api/settings/secrets/${name}`, { value }),
  deleteSecret: (name: string) => del(`/api/settings/secrets/${name}`),
  doctor: () => get<{ name: string; status: "ok" | "warn" | "fail"; detail: string }[]>("/api/settings/doctor"),
  tierMemory: () => get<Record<string, string>>("/api/settings/tier-memory"),
  connect: () => get<ConnectInfo>("/api/settings/connect"),

  // in-app designer (Claude / Gemini via API key)
  llmModels: (provider: string, refresh = false) => get<ModelList>(`/api/llm/models?provider=${provider}${refresh ? "&refresh=1" : ""}`),
  chats: (recipeId?: string) => get<Chat[]>(`/api/chats${recipeId ? `?recipe_id=${recipeId}` : ""}`),
  chat: (id: string) => get<Chat>(`/api/chats/${id}`),
  createChat: (body: { recipe_id?: string | null; provider?: string; model?: string; effort?: string; title?: string }) => post<Chat>("/api/chats", body),
  sendChat: (id: string, content: string) => post<Chat>(`/api/chats/${id}/messages`, { content }),
  cancelChat: (id: string) => post<{ id: string; cancelled: boolean }>(`/api/chats/${id}/cancel`),
  deleteChat: (id: string) => del<{ id: string; deleted: boolean }>(`/api/chats/${id}`),

  importBrowsers: () => get<{ available: boolean; browsers: string[] }>("/api/sessions/import/browsers"),
  importSession: (body: { browser: string; domain: string; name?: string }) => post<SessionRow>("/api/sessions/import", body),

  // schedules + diffs
  schedules: (recipeId?: string) => get<Schedule[]>(`/api/schedules${recipeId ? `?recipe_id=${recipeId}` : ""}`),
  createSchedule: (body: ScheduleIn) => post<Schedule>("/api/schedules", body),
  patchSchedule: (id: string, body: Partial<ScheduleIn>) => request<Schedule>("PATCH", `/api/schedules/${id}`, body),
  deleteSchedule: (id: string) => del<{ id: string; deleted: boolean }>(`/api/schedules/${id}`),
  runScheduleNow: (id: string) => post<RunRow>(`/api/schedules/${id}/run`),
  runDiff: (runId: string, against?: string) => get<{ run_id: string; against_run_id: string | null; against_finished_at?: string | null; diff: DiffSummary | null }>(`/api/runs/${runId}/diff${against ? `?against=${against}` : ""}`),
  storage: () => get<{ data_size_bytes: number; runs: number; samples: number }>("/api/settings/storage"),
  runFailed: (runId: string) => get<{ pages: FailedPage[]; counts: Record<string, number> }>(`/api/runs/${runId}/failed`),
  runFallbackNow: (runId: string) => post<Record<string, number>>(`/api/runs/${runId}/fallback`),
  prune: () => post<{ runs: number; samples: number; data_size_bytes: number }>("/api/settings/prune"),

  // agent hand-offs
  pick: (id: string) => get<PickRequest>(`/api/picks/${id}`),
  pendingPicks: () => get<PickRequest[]>("/api/picks?status=pending"),
  answerPick: (id: string, body: NonNullable<PickRequest["answer"]> | { cancelled: true }) => post<PickRequest>(`/api/picks/${id}/answer`, body),
  forgetTier: (domain: string) => del(`/api/settings/tier-memory/${encodeURIComponent(domain)}`),

  // recipes
  recipes: () => get<RecipeRow[]>("/api/recipes"),
  recipe: (id: string) => get<RecipeRow>(`/api/recipes/${id}`),
  createRecipe: (recipe: Recipe) => post<RecipeRow>("/api/recipes", recipe),
  updateRecipe: (id: string, recipe: Recipe, note = "") =>
    put<RecipeRow>(`/api/recipes/${id}?note=${encodeURIComponent(note)}`, recipe),
  deleteRecipe: (id: string) => del(`/api/recipes/${id}`),
  validateRecipe: (recipe: Recipe) => post<{ ok: boolean; errors: { loc: string; msg: string }[]; recipe?: Recipe }>("/api/recipes/validate", recipe),
  recipeVersions: (id: string) => get<{ version: number; note: string; created_at: string; recipe: Recipe }[]>(`/api/recipes/${id}/versions`),
  rollback: (id: string, version: number) => post<RecipeRow>(`/api/recipes/${id}/rollback/${version}`),
  exportRecipeUrl: (id: string, fmt: "yaml" | "json" | "scrapy") => `/api/recipes/${id}/export?fmt=${fmt}`,

  // pages / samples
  snapshot: (body: { urls: string[]; recipe?: Recipe; recipe_id?: string; kind?: string; tier?: string | null; headed?: boolean }) =>
    post<Sample[]>("/api/pages/snapshot", body),
  pages: (recipeId?: string) => get<Sample[]>(`/api/pages${recipeId ? `?recipe_id=${recipeId}` : ""}`),
  page: (id: string) => get<Sample>(`/api/pages/${id}`),
  analyzePage: (id: string) => post<Analysis>(`/api/pages/${id}/analyze`),
  renderUrl: (id: string) => `/api/pages/${id}/render`,
  testSelector: (id: string, body: { selector: string; attr?: string | null; regex?: string | null; container?: string | null }) =>
    post<{ matches?: number; values: unknown[]; snippets?: string[]; container_matches?: number; filled?: number; fill_rate?: number }>(
      `/api/pages/${id}/selector`,
      body,
    ),

  // preview
  preview: (recipe: Recipe, sampleIds?: string[]) =>
    post<{ report: ValidationReport; samples: Sample[] }>("/api/preview", { recipe, sample_ids: sampleIds }),
  fetchSamples: (recipe: Recipe, opts?: { with_page2?: boolean; detail_pages?: number; tier?: string | null }) =>
    post<{ report: ValidationReport; samples: Sample[] }>("/api/preview/samples", { recipe, ...opts }),

  // runs
  runs: (recipeId?: string) => get<RunRow[]>(`/api/runs${recipeId ? `?recipe_id=${recipeId}` : ""}`),
  run: (id: string) => get<RunRow>(`/api/runs/${id}`),
  startRun: (body: { recipe_id?: string; recipe?: Recipe; max_pages?: number | null; max_items?: number | null; tier?: string | null; headed?: boolean }) =>
    post<RunRow>("/api/runs", body),
  stopRun: (id: string) => post(`/api/runs/${id}/stop`),
  cancelRun: (id: string) => post(`/api/runs/${id}/cancel`),
  resumeRun: (id: string) => post<RunRow>(`/api/runs/${id}/resume`),
  runItems: (id: string, offset = 0, limit = 100) =>
    get<{ total: number; offset: number; items: Record<string, unknown>[] }>(`/api/runs/${id}/items?offset=${offset}&limit=${limit}`),
  runEvents: (id: string, types?: string, tail = 200) =>
    get<Record<string, unknown>[]>(`/api/runs/${id}/events?tail=${tail}${types ? `&types=${types}` : ""}`),
  runLog: (id: string, tail = 200) => get<{ lines: string[] }>(`/api/runs/${id}/log?tail=${tail}`),
  exportRun: (id: string, fmt: string, include_meta = true) => post<{ path: string; rows: number; download: string }>(`/api/runs/${id}/export`, { fmt, include_meta }),

  // sessions
  sessions: () => get<SessionRow[]>("/api/sessions"),
  createSession: (body: { name?: string; url: string }) => post<SessionRow>("/api/sessions", body),
  session: (id: string) => get<SessionRow>(`/api/sessions/${id}`),
  refreshSession: (id: string) => post<SessionRow>(`/api/sessions/${id}/refresh`),
  deleteSession: (id: string) => del(`/api/sessions/${id}`),
};

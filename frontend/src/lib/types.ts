// Mirrors backend/src/scrapy_awesome/recipe/models.py (kept small; the server validates).

export type Tier = "auto" | "http" | "browser" | "interactive";
export type FieldType =
  | "text" | "number" | "price" | "date" | "url" | "image" | "bool" | "enum" | "list" | "json";
export type Scope = "list" | "detail" | "page";
export type PaginationKind =
  | "none" | "next_link" | "url_template" | "load_more" | "infinite_scroll" | "xhr_json";

export interface Extractor {
  css?: string | null;
  xpath?: string | null;
  json_path?: string | null;
  llm?: string | null;
  attr?: string | null;
  regex?: string | null;
  all?: boolean;
}

export interface Field {
  name: string;
  type: FieldType;
  description?: string;
  required?: boolean;
  sparse?: boolean;   // usually empty by nature — an empty column is a note, not an error
  scope: Scope;
  extract: Extractor;
  alternates?: Extractor[];
  enum?: string[] | null;
  examples?: string[];
  default?: unknown;
}

export interface Action {
  kind: "wait_for" | "wait_ms" | "scroll_until_stable" | "scroll" | "click" | "fill" | "press" | "evaluate";
  selector?: string | null;
  ms?: number | null;
  js?: string | null;
  value?: string | null;
  times?: number | null;
  max_rounds?: number | null;
  optional?: boolean;
}

export interface FetchConfig {
  tier: Tier;
  profile?: string;
  proxy?: string | null;
  session?: string | null;
  actions?: Action[];
  wait_for?: string | null;
  settle_seconds?: number | null;
  timeout_seconds?: number;
  headers?: Record<string, string>;
  block_static_assets?: boolean;
}

export interface ApiPaging {
  kind: "none" | "page" | "cursor";
  start: number;
  step: number;
  page_size?: number | null;
  cursor_path?: string | null;
  has_more_path?: string | null;
  stop_on_empty: boolean;
}
export interface ApiConfig {
  url_template: string;
  method: "GET" | "POST";
  body_template?: string | null;
  headers: Record<string, string>;
  paging: ApiPaging;
  explode?: string | null;
  on_error: "html" | "stop";
  platform?: string | null;
  note: string;
}

export interface Recipe {
  api?: ApiConfig | null;
  version: 1;
  id?: string;
  name: string;
  created_at?: string;
  seeds: string[];
  intent?: string;
  page_type: "list" | "single";
  allowed_domains?: string[];
  fetch: FetchConfig;
  list?: { container: string; alternates?: string[]; min_items?: number } | null;
  detail: { enabled: boolean; link?: Extractor | null; max_concurrency?: number; fetch?: FetchConfig | null };
  pagination: {
    kind: PaginationKind;
    selector?: string | null;
    url_template?: string | null;
    start?: number;
    step?: number;
    max_pages?: number;
    stop_when_no_new_items?: boolean;
  };
  fields: Field[];
  dedupe_key?: string[];
  limits: {
    max_pages: number;
    max_items: number;
    max_detail_pages?: number | null;
    download_delay: number;
    concurrency_per_domain: number;
    per_run_llm_budget_usd?: number;
    request_timeout_seconds?: number;
  };
  fallback?: { llm_enabled: boolean; only_missing_fields?: boolean };
  fingerprints?: Record<string, unknown>;
  notes?: string;
}

export interface RecipeRow {
  id: string;
  name: string;
  version: number;
  recipe: Recipe;
  created_at: string;
  updated_at: string;
  last_run_id?: string | null;
  archived: boolean;
  incompatible_with_resume?: string[];
}

export interface RunRow {
  id: string;
  recipe_id?: string | null;
  recipe_version?: number | null;
  recipe_name: string;
  kind: string;
  status: "queued" | "running" | "stopping" | "stopped" | "finished" | "failed" | "cancelled";
  reason?: string | null;
  items: number;
  pages: number;
  blocked: number;
  escalations: number;
  limits: Record<string, unknown>;
  stats: Record<string, unknown> & { diff?: DiffSummary; healed?: HealedSelector[]; fill_history?: Record<string, number[]>; llm?: { pages: number; rows: number; cost_usd: number; skipped: number } };
  schedule_id?: string | null;
  error?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  active: boolean;
  run_dir: string;
}

export interface Sample {
  id: string;
  recipe_id?: string | null;
  url: string;
  final_url: string;
  status: number;
  tier?: string | null;
  kind: string;
  bytes: number;
  title: string;
  blobs: string[];
  verdict?: { blocked: boolean; needs_js: boolean; reason?: string | null; detail?: string } | null;
  analysis?: Analysis | null;
  created_at: string;
}

export interface PlatformSignal { name: string; weight: number; detail: string }
export interface PlatformApi {
  platform: string;
  label: string;
  endpoint: string;
  reason: string;
  evidence: string[];
  currency: string | null;
  granularity: "product" | "variant";
  patch_origin?: string;
  robots_note?: string;   // a candidate endpoint robots.txt put out of bounds
}
export interface PlatformBlock {
  detected: boolean;
  platform: string | null;
  label: string | null;
  score: number;
  signals: string[];
  extras: Record<string, string>;
  api: PlatformApi | null;
  reason: string;
  probed: boolean;
  cached?: boolean;
  candidates?: { platform: string; label: string; score: number; detected: boolean }[];
}

export interface Analysis {
  platform?: PlatformBlock | null;
  url: string;
  title: string;
  page_type: "list" | "single";
  text_length: number;
  script_count: number;
  containers: { selector: string; count: number; avg_text: number; with_links: number; score: number; sample: string[] }[];
  fields: { name: string; type: FieldType; selector: string; attr: string | null; examples: string[]; fill: number }[];
  detail_link?: { selector: string; sample: string[]; same_host: number } | null;
  pagination: { kind: PaginationKind; selector?: string | null; url_template?: string | null; evidence: string }[];
  json_blobs: Record<string, unknown>;
  json_list_paths: { container: string; count: number; keys: string[] }[];
  login_hint: boolean;
  notes: string[];
}

export interface FieldStats {
  name: string;
  scope: string;
  n_total: number;
  n_filled: number;
  distinct: number;
  examples: unknown[];
  provenance: Record<string, number>;
  selector: string;
  fill_rate: number;
}

export interface Issue {
  level: "error" | "warn" | "info";
  code: string;
  message: string;
  field?: string | null;
}

export interface ValidationReport {
  ok: boolean;
  containers: { url: string; matched: number; provenance: string }[];
  fields: Record<string, FieldStats>;
  rows: Record<string, unknown>[];
  issues: Issue[];
  pagination: Record<string, unknown>;
  detail: Record<string, unknown>;
}

export interface SessionRow {
  id: string;
  name: string;
  start_url: string;
  domain: string;
  status: "pending" | "ready" | "failed" | "expired";
  cookies: number;
  error?: string | null;
  created_at: string;
  updated_at: string;
  last_used_at?: string | null;
}

export interface Settings {
  llm: {
    designer: { provider: string; model: string; effort: string };
    fallback: { provider: string; model: string; effort: string };
    session_budget_usd: number;
    default_run_llm_budget_usd: number;
    cli_login_enabled: boolean;
  };
  crawl: {
    obey_robots: boolean;
    default_download_delay: number;
    default_concurrency_per_domain: number;
    autothrottle: boolean;
    httpcache_ttl_seconds: number;
    chrome_executable_path?: string | null;
    proxies: string[];
  };
  server: { host: string; port: number; open_browser: boolean; idle_exit_seconds?: number | null };
  retention: { keep_runs_per_recipe: number; keep_samples_per_recipe: number; keep_days: number; notifications: boolean };
  max_concurrent_runs: number;
}

export interface SettingsResponse {
  fake_llm?: boolean;
  settings: Settings;
  secrets: Record<string, { set: boolean; masked?: string | null; source: string; env: string }>;
  data_dir: string;
}

export interface RunEvent {
  t: string;
  ts?: string;
  run_id?: string;
  [k: string]: unknown;
}

export interface PickRequest {
  id: string;
  status: "pending" | "answered" | "cancelled" | "expired";
  prompt: string;
  kind: "field" | "container" | "link" | "pagination" | "any";
  recipe_id: string | null;
  sample_id: string | null;
  field_name: string | null;
  hint: string | null;
  created_at: number;
  answer?: {
    selector: string | null;
    relative_selector: string | null;
    container: string | null;
    attr: string | null;
    examples: string[];
    matches: number | null;
    note: string | null;
  };
}

export interface ConnectInfo {
  mcp_command: string[];
  claude_code: {
    add: string;
    plugin_dir: string | null;
    plugin_add: string | null;
    auth: { loggedIn?: boolean; authMethod?: string; subscriptionType?: string } | null;
  };
  claude_desktop: { file: string; json: string };
  gemini_cli: { file: string; json: string; add: string };
  note: string;
}

export interface ChatToolCall {
  name: string;
  input: Record<string, unknown> | null;
  ok: boolean | null;
  summary: string;
}
export interface ChatUsage {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  cost_usd?: number;
  calls?: number;
}
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  ts?: number;
  tool_calls?: ChatToolCall[];
  usage?: ChatUsage;
  stop_reason?: string | null;
}
export interface Chat {
  id: string;
  recipe_id: string | null;
  provider: "anthropic" | "gemini" | string;
  model: string;
  effort: string;
  title: string;
  status: "idle" | "running" | "error";
  messages: ChatMessage[];
  usage: ChatUsage;
  error: string | null;
  created_at: string;
  updated_at: string;
}
export interface ChatEvent {
  t: string;
  chat_id?: string;
  text?: string;
  name?: string;
  input?: Record<string, unknown>;
  ok?: boolean;
  summary?: string;
  message?: string;
  stop_reason?: string;
  usage?: ChatUsage;
  error?: string | null;
  [k: string]: unknown;
}
export interface ModelList {
  provider: string;
  models: { id: string; display_name: string }[];
  source: "live" | "fallback";
  error?: string;
  cached?: boolean;
}

export interface DiffSummary {
  keys: string[];
  old_count: number;
  new_count: number;
  added: number;
  removed: number;
  changed: number;
  unchanged: number;
  against_run_id?: string;
  against_finished_at?: string | null;
  samples?: {
    added: Record<string, unknown>[];
    removed: Record<string, unknown>[];
    changed: { key: Record<string, unknown>; fields: Record<string, { old: unknown; new: unknown }> }[];
  };
}
export interface FailedPage {
  id: string;
  run_id: string;
  url: string;
  kind: "list" | "detail";
  reason: string;
  tier: string | null;
  status: "pending" | "recovered" | "skipped" | "failed";
  rows_added: number;
  provider: string | null;
  cost_usd: number;
  error: string | null;
  created_at: string;
}
export interface HealedSelector {
  field: string;
  url: string;
  old: { css?: string | null; xpath?: string | null; attr?: string | null };
  new: { css: string; attr?: string | null };
  score: number;
  fill: number;
  examples?: string[];
}
export interface Schedule {
  id: string;
  recipe_id: string;
  name: string;
  kind: "cron" | "interval";
  cron: string | null;
  every_seconds: number | null;
  timezone: string | null;
  describe: string;
  enabled: boolean;
  max_pages: number | null;
  max_items: number | null;
  notify: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  last_status: string | null;
  last_diff: DiffSummary | null;
  created_at: string;
  updated_at: string;
}
export interface ScheduleIn {
  recipe_id: string;
  name?: string;
  kind: "cron" | "interval";
  cron?: string | null;
  every_seconds?: number | null;
  timezone?: string | null;
  enabled?: boolean;
  max_pages?: number | null;
  max_items?: number | null;
  notify?: boolean;
}

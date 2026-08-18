# In-app AI providers (Claude & Gemini via API keys)

The in-app **AI designer** is a chat panel in the recipe editor. The assistant drives the *same
tools* the MCP server exposes (`fetch_page`, `search_page`, `test_selector`, `list_json_blobs`,
`save_recipe`, `validate_recipe`, `request_pick`, `start_run`, …) through a loopback client, so
everything it does appears live in the editor, preview and runs. See `docs/auth-modes.md` for
how this relates to the subscription (MCP) path.

## Providers

| Provider | SDK | Default model | Notes |
|---|---|---|---|
| `anthropic` ("Powered by Claude") | `anthropic` (async, beta tool runner, streaming) | `claude-opus-5` | adaptive thinking, `output_config.effort` = the role's effort, prompt caching on system prompt + tool defs, refusal-terminal |
| `gemini` | `google-genai` (manual function-calling loop, streaming) | `gemini-3.7-flash` | `thinking_level` from effort, raw JSON-schema function declarations, thought signatures preserved |
| `claude_code` ("Claude Code login") | `claude-agent-sdk` (Claude Code harness, our tools as an SDK MCP server) | `claude-opus-5` | your **subscription**, no key; opt-in in Settings → Advanced (policy note); designer only — see `auth-modes.md` |
| `fake` (dev/test) | none | — | `SA_FAKE_LLM=1` — an offline scripted designer that fetches, saves and validates from the heuristic analysis; used by tests and for UI work without a key |

Keys: Settings → *AI providers* (stored in the OS keychain, else a 0600 file in the data dir), or
`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` in the environment. Model lists come live from each API
(`GET /api/llm/models?provider=…`, cached 10 min; falls back to a built-in list without a key).

Roles (Settings): **designer** (chat; default effort `high`) and **fallback** (per-page extraction
when selectors fail — Phase 6; default effort `low`). Each role picks provider + model + effort.

## What the model sees

* A stable system prompt (`llm/designer.py::SYSTEM_PROMPT`) — the workflow, rules and the recipe
  JSON keys. Cached (Anthropic `cache_control`).
* Per turn, a small `[context]` block prepended to the user's message: robots setting, the current
  recipe JSON (≤ 6 kB), the ids/kinds/URLs of cached pages.
* Tool results: folded page outlines, selector test results, validation reports — never raw HTML
  by default, never login cookies/storage state, never API keys.

## Cost & budgets

Usage is accounted per model call (input/output/cache tokens) and priced with an approximate
table (`llm/pricing.py`) for **estimates**; a per-chat **session budget** (Settings, default $2)
stops the turn with a clear message when exceeded. The header of the chat panel shows calls and
spend; transcripts and provider-native history are persisted in SQLite (`chats` table).

## Events (WebSocket `/ws/chats/{id}`)

`turn_start` · `snapshot` (late-join replay of the partial assistant message) · `text_delta` ·
`thinking_delta` · `tool_call {name,input}` · `tool_result {name,ok,summary}` · `usage` · `error` ·
`done` · `turn_end {stop_reason, usage, error}`. Recipe saves by any agent also emit
`recipe_saved` on the firehose (`/ws/events`) so open editors refresh.

## Testing

* `tests/test_llm_providers.py` — scripted fake SDK clients: streaming, tool execution, history
  mirroring, cost/budget, error mapping (no network).
* `tests/test_chat_api.py` — chat sessions, WebSocket stream, cancel, persistence, loopback tool
  calls; `SA_FAKE_LLM` end-to-end (fetch → save → validate) against the fixture site.
* `tests/test_llm_live.py` — opt-in: `SA_LIVE=1 ANTHROPIC_API_KEY=… uv run pytest -m live`.

# AI access modes

scrapy-awesome works with **no AI at all** (manual fields + the visual picker). When you want an
assistant, there are three ways to bring one, and one deliberately gray one.

| Mode | Who pays / logs in | Where the model runs | How |
|---|---|---|---|
| **MCP plugin** (recommended for subscribers) | your Claude Pro/Max or Gemini plan, in *your own* Claude Code / Claude Desktop / Gemini CLI | in that client | `scrapy-awesome mcp` — see `plugin/README.md`; Settings → *Connect your agent* prints the exact commands |
| **Anthropic API key** | your Anthropic Console key (pay-as-you-go) | inside the app (designer chat, "fix this column", per-page fallback) | Settings → AI providers |
| **Google (Gemini) API key** | your Google AI Studio key | inside the app | Settings → AI providers |
| *cli_login* (opt-in, gray zone) | your Claude Code login, reused by the app through the Agent SDK | inside the app | Settings → Advanced, off by default; see below |

## Why the MCP path is the subscription path

Anthropic's Agent SDK documentation says (verbatim): *"Unless previously approved, Anthropic does
not allow third party developers to offer claude.ai login or rate limits for their products,
including agents built on the Claude Agent SDK."* An app therefore must not log you into
claude.ai or spend your subscription on its own behalf. What it **can** do is be a tool that
your own Claude client calls — which is exactly what an MCP server is. The same server works for
Gemini CLI, so Gemini subscribers get the identical flow.

Consequences you will notice:

- In MCP mode the app has **no chat panel**: you talk to Claude Code / Claude Desktop / Gemini
  CLI, and the app is the workbench that fills in live (recipe, preview, run). `open_ui` and
  `request_pick` bring the app to the front when the agent needs you.
- The app never sees your login; only loopback tool calls with a per-process token.

## In-app API keys

With a key the app runs the designer itself: streaming chat, structured recipe proposals,
"fix this column", and (if enabled) per-page LLM fallback extraction with provenance and a
budget. Keys live in your OS keychain (fallback: a 0600 file in the data dir), never in recipes
or exports. Model lists are fetched live from each provider; defaults are `claude-opus-5` and
`gemini-3.7-flash`.

## `cli_login` — the in-app designer on your Claude Code subscription (Advanced, off by default)

Reuses the Claude Code CLI's OAuth login inside the app via `claude-agent-sdk`
(`llm/claude_code_provider.py`, extra `scrapy-awesome[claude-code]`; also included in the frozen
sidecar). This is against the policy quoted above unless Anthropic has approved it for you.
Settings → *Advanced* shows the quote and asks for an explicit acknowledgement before enabling
`llm.cli_login_enabled`; then pick "Claude Code login" as the designer provider (or set
`llm.designer.provider = claude_code` in `settings.json`). Once on, "Design with AI" on the New
page and the editor's AI designer panel run on your subscription with no API key: token
streaming, tool chips, session resume between turns, `$0` shown as *subscription*.

How it is locked down: `system_prompt` replaces the Claude Code prompt entirely; only our tools
are exposed (in-process SDK MCP server, `strict_mcp_config`, built-in tools disallowed, no
settings sources); the CLI is located on PATH or in the usual install dirs (`~/.local/bin`, …) and
given `USER`/`LOGNAME`/`TMPDIR` so the Keychain login is found even from a Finder-launched app. It
cannot serve the per-page *fallback* role or AI fields (those need an API key). If the SDK stops
inheriting the CLI login it fails with the CLI's auth error and you can switch provider.

## Claude Desktop extension (MCPB)

`mcpb/manifest.json` (+ `npx @anthropic-ai/mcpb pack mcpb/`) installs the MCP server into Claude
Desktop with one click; it points at the desktop app's bundled binary (configurable path).

## What never happens

- No CAPTCHA solving; the headed "log in once" window is the human pass-through.
- Credentials / cookies from login sessions are never sent to any model.
- Crawls never call a model unless per-page fallback is explicitly enabled and budgeted.

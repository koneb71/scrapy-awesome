# scrapy-awesome — Claude Desktop extension (MCPB)

A one-click way to give Claude Desktop the scrapy-awesome tools. The bundle contains only the
manifest: it points at the binary of the installed **scrapy-awesome desktop app** (or a Python
install's `scrapy-awesome` script), which starts the local server on first use.

```bash
npx @anthropic-ai/mcpb pack mcpb/            # → scrapy-awesome.mcpb
```

Then double-click the `.mcpb` (or Claude Desktop → Settings → Extensions → Install) and confirm
the binary path when asked. Details on the tools and the compliant subscription flow:
`plugin/README.md`, `docs/auth-modes.md`.

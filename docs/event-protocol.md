# Worker ↔ parent protocol

One worker process per crawl or snapshot job:

```
python -m scrapy_awesome.crawl.worker crawl    --recipe recipe.json --run-dir DIR --run-id ID [--tier T] [--max-pages N] [--max-items N] [--resume] [--events-url URL --events-token T] [--control-url URL]
python -m scrapy_awesome.crawl.worker snapshot --urls '["…"]' --kind list|detail --run-dir DIR --run-id ID [--recipe recipe.json]
```

The parent (CLI / server) uses `scrapy_awesome.crawl.runner` (`run_crawl`, `fetch_snapshots`,
`stop_run`). Frozen builds re-exec `sys.executable --worker`.

## Run directory

```
<run_dir>/
  recipe.json      canonical recipe used for this run
  items.jsonl      one row per item (append-only; reloaded for dedupe on resume)
  events.jsonl     every event (see below)
  worker.log       Scrapy/Playwright/Chrome logs (stdout+stderr of the worker)
  stats.json       final summary (reason, counts, escalations, tier_memory)
  control.json     parent → worker command file ({"cmd": "stop"})
  jobdir/          Scrapy JOBDIR when started with --resume (pause/resume)
  snapshots/NNN.json   snapshot mode: {url, final_url, status, tier, verdict, headers, html, blobs}
```

## Events (`{"t": type, "ts": iso, "run_id": id, ...}`)

| t | fields | when |
|---|---|---|
| `started` | recipe_id, recipe_name, seeds, max_pages, max_items, tier | spider start |
| `progress` | pages, items, requests, errors, blocked, escalations, tiers{tier: n}, final, reason | every 2 s + on close |
| `page` | url, status, tier, ok, kind(list/detail), page_no, items, container, reason, detail, filled | each fetched page |
| `fill` | url, page_no, rates{field: 0..1} | each list page with items (drift detection) |
| `item` | row, n | each stored item (after dedupe) |
| `blocked` | url, status, tier, reason, detail, escalated_to | each block / JS-only verdict |
| `log` | level, msg | notable messages |
| `snapshot` | index, url, status, tier, path, bytes, blobs[], verdict | snapshot mode |
| `done` | reason, items, duplicates, pages, requests, errors, blocked{}, escalations{}, tier_memory{} | spider closed |

Sinks: `events.jsonl` in the run dir (always) and, when `--events-url` is given, batched
`POST {url}` with `{"events": [...]}` and `Authorization: Bearer <token>` (Phase 2 server).

## Control

* `control.json` → `{"cmd": "stop"}` — checked every second by `ControlExtension`; triggers a
  graceful `close_spider(reason="stopped")` (JOBDIR persisted). Cross-platform (no signals).
* `--control-url` (Phase 2) polls the server for the same command.
* Hard cancel: terminate the process (`RunResult`/server keep the pid).

## Row shape

Recipe fields plus implicit columns: `_url` (detail URL or `page#item-N`), `_page_url`,
`_fetched_at`, `_tier` (http|browser|interactive), `_provenance` ({field: primary|alt:N|missing|llm}).

## Server handoff (`serve`)

`scrapy-awesome serve` binds and **listens** on `127.0.0.1:<port>` first, then:

1. writes `<data_dir>/server.json` — `{pid, port, host, url, token, started_at, version}` (tmp+rename,
   mode 0600); consumers must `GET /health` before trusting it;
2. prints one JSON **ready line** on stdout — `{"port", "token", "url", "pid"}` — the Tauri sidecar /
   MCP auto-start handoff. Because the socket is already listening, connecting right after the ready
   line never gets "connection refused" (requests queue in the backlog until uvicorn starts serving).

The UI is authenticated by opening `GET /auth?token=<token>&next=/`, which sets the HttpOnly session
cookie. On SIGTERM (or `--idle-exit`, or `--ppid-watch` losing its parent) the server terminates
active workers, flushes their events, and exits within ~3 s even if browser WebSockets are open.

Frozen builds (PyInstaller) re-execute the same binary as `scrapy-awesome --worker …` and
`scrapy-awesome --login-window …` (dev installs use `python -m scrapy_awesome.crawl.worker` /
`…browser_session.profile`).

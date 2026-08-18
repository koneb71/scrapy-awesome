"""Parent-side orchestration: spawn worker subprocesses for crawls and snapshot jobs, wait, collect.

The server (Phase 2) uses the async variants; the CLI uses the blocking ones. Both share `worker_cmd`
so frozen builds re-exec themselves (`sys.executable --worker …`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scrapy_awesome.config import UserSettings, get_paths
from scrapy_awesome.recipe.io import load_recipe, save_recipe
from scrapy_awesome.recipe.models import Recipe


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def worker_cmd() -> list[str]:
    if getattr(sys, "frozen", False):  # PyInstaller
        return [sys.executable, "--worker"]
    return [sys.executable, "-m", "scrapy_awesome.crawl.worker"]


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    exit_code: int
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def items_path(self) -> Path:
        return self.run_dir / "items.jsonl"

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    def items(self) -> list[dict[str, Any]]:
        if not self.items_path.exists():
            return []
        out = []
        with self.items_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        out = []
        with self.events_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


def common_worker_args(
    *,
    run_id: str,
    run_dir: Path,
    tier: str | None,
    headed: bool,
    storage_state: str | None,
    events_url: str | None,
    events_token: str | None,
    control_url: str | None,
    obey_robots: bool,
    httpcache: bool,
    chrome: str | None,
    proxies: list[str] | None,
    tier_memory: dict[str, str] | None,
    log_level: str,
) -> list[str]:
    args = ["--run-id", run_id, "--run-dir", str(run_dir), "--log-level", log_level]
    if tier:
        args += ["--tier", tier]
    if headed:
        args.append("--headed")
    if storage_state:
        args += ["--storage-state", storage_state]
    if events_url:
        args += ["--events-url", events_url]
    if events_token:
        args += ["--events-token", events_token]
    if control_url:
        args += ["--control-url", control_url]
    if not obey_robots:
        args.append("--no-robots")
    if httpcache:
        args.append("--httpcache")
    if chrome:
        args += ["--chrome", chrome]
    for p in proxies or []:
        args += ["--proxy", p]
    if tier_memory:
        args += ["--tier-memory", json.dumps(tier_memory)]
    return args


def spawn_worker(
    args: list[str], *, run_dir: Path, env: dict[str, str] | None = None
) -> subprocess.Popen:
    run_dir.mkdir(parents=True, exist_ok=True)
    log = (run_dir / "worker.log").open("ab")
    full_env = dict(os.environ)
    full_env.setdefault("PYTHONUNBUFFERED", "1")
    if env:
        full_env.update(env)
    creation = 0
    if sys.platform == "win32":  # new process group so terminate() is clean
        creation = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(
        [*worker_cmd(), *args],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=full_env,
        cwd=str(run_dir),
        creationflags=creation,
    )


def prepare_run_dir(recipe: Recipe, run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    return save_recipe(recipe, run_dir / "recipe.json")


def run_crawl(
    recipe: Recipe,
    *,
    run_dir: Path,
    run_id: str | None = None,
    tier: str | None = None,
    max_pages: int | None = None,
    max_items: int | None = None,
    resume: bool = False,
    headed: bool = False,
    storage_state: str | None = None,
    events_url: str | None = None,
    events_token: str | None = None,
    control_url: str | None = None,
    obey_robots: bool | None = None,
    httpcache: bool = False,
    chrome: str | None = None,
    proxies: list[str] | None = None,
    tier_memory: dict[str, str] | None = None,
    log_level: str = "INFO",
    timeout: float | None = None,
) -> RunResult:
    """Blocking: spawn a crawl worker and wait for it."""
    settings = UserSettings.load()
    run_id = run_id or new_run_id()
    recipe_path = prepare_run_dir(recipe, run_dir)
    args = [
        "crawl",
        "--recipe",
        str(recipe_path),
        *common_worker_args(
            run_id=run_id,
            run_dir=run_dir,
            tier=tier,
            headed=headed,
            storage_state=storage_state,
            events_url=events_url,
            events_token=events_token,
            control_url=control_url,
            obey_robots=settings.crawl.obey_robots if obey_robots is None else obey_robots,
            httpcache=httpcache,
            chrome=chrome or settings.crawl.chrome_executable_path,
            proxies=proxies if proxies is not None else settings.crawl.proxies,
            tier_memory=tier_memory,
            log_level=log_level,
        ),
    ]
    if max_pages is not None:
        args += ["--max-pages", str(max_pages)]
    if max_items is not None:
        args += ["--max-items", str(max_items)]
    if resume:
        args.append("--resume")
    proc = spawn_worker(args, run_dir=run_dir)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_run(run_dir)
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
    stats = {}
    sp = run_dir / "stats.json"
    if sp.exists():
        stats = json.loads(sp.read_text())
    return RunResult(run_id=run_id, run_dir=run_dir, exit_code=code, stats=stats)


def stop_run(run_dir: Path) -> None:
    """Ask a running worker to stop gracefully (JOBDIR-friendly)."""
    (run_dir / "control.json").write_text(json.dumps({"cmd": "stop"}))


def fetch_snapshots(
    urls: list[str],
    *,
    run_dir: Path,
    recipe: Recipe | None = None,
    kind: str = "list",
    tier: str | None = None,
    headed: bool = False,
    storage_state: str | None = None,
    obey_robots: bool | None = None,
    chrome: str | None = None,
    proxies: list[str] | None = None,
    tier_memory: dict[str, str] | None = None,
    log_level: str = "WARNING",
    timeout: float | None = 300,
) -> list[dict[str, Any]]:
    """Blocking: fetch URLs through the engine stack; returns snapshot dicts (html, status, tier, blobs)."""
    settings = UserSettings.load()
    run_dir.mkdir(parents=True, exist_ok=True)
    args = ["snapshot", "--urls", json.dumps(urls), "--kind", kind]
    if recipe is not None:
        args += ["--recipe", str(save_recipe(recipe, run_dir / "recipe.json"))]
    args += common_worker_args(
        run_id=f"snap-{uuid.uuid4().hex[:8]}",
        run_dir=run_dir,
        tier=tier,
        headed=headed,
        storage_state=storage_state,
        events_url=None,
        events_token=None,
        control_url=None,
        obey_robots=settings.crawl.obey_robots if obey_robots is None else obey_robots,
        httpcache=False,
        chrome=chrome or settings.crawl.chrome_executable_path,
        proxies=proxies if proxies is not None else settings.crawl.proxies,
        tier_memory=tier_memory,
        log_level=log_level,
    )
    proc = spawn_worker(args, run_dir=run_dir)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    snaps: list[dict[str, Any]] = []
    snap_dir = run_dir / "snapshots"
    if snap_dir.exists():
        for p in sorted(snap_dir.glob("*.json")):
            snaps.append(json.loads(p.read_text(encoding="utf-8")))
    return snaps


# ---------------------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------------------
def run_recipe_file(
    path: Path,
    *,
    out_dir: Path,
    formats: list[str],
    max_pages: int | None,
    max_items: int | None,
    tier: str | None,
) -> dict[str, Any]:
    from scrapy_awesome.export.writers import export_jsonl_file

    recipe = load_recipe(path)
    if not recipe.ready:
        raise SystemExit("recipe is not ready to run: " + "; ".join(recipe.readiness_errors()))
    run_id = new_run_id()
    run_dir = Path(out_dir) / run_id
    res = run_crawl(
        recipe, run_dir=run_dir, run_id=run_id, tier=tier, max_pages=max_pages, max_items=max_items
    )
    outputs: dict[str, str] = {}
    if res.items_path.exists():
        for fmt in formats:
            if fmt == "jsonl":
                outputs["jsonl"] = str(res.items_path)
                continue
            outputs[fmt] = str(export_jsonl_file(res.items_path, fmt=fmt))
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "exit_code": res.exit_code,
        "stats": res.stats,
        "outputs": outputs,
    }


def preview_recipe_file(path: Path, *, rows: int = 20, console: Any = None) -> dict[str, Any]:
    """Fetch page 1 (+ page 2 via next link, + up to two detail pages), validate in-process, print."""
    from rich.table import Table

    from scrapy_awesome.extract.engine import extract_list_items, next_page_url
    from scrapy_awesome.extract.validate import Sample, validate_on_samples

    recipe = load_recipe(path)
    if not recipe.ready:
        raise SystemExit("recipe is not ready: " + "; ".join(recipe.readiness_errors()))
    paths = get_paths().ensure()
    run_dir = paths.cache / "previews" / new_run_id()
    snaps = fetch_snapshots([recipe.seeds[0]], run_dir=run_dir, recipe=recipe, kind="list")
    if not snaps:
        raise SystemExit("preview: could not fetch the seed page (see worker.log)")
    first = snaps[0]
    samples = [Sample(first["final_url"], first["html"], "list", first.get("blobs") or None)]

    more_urls: list[tuple[str, str]] = []
    nxt = next_page_url(recipe, first["html"], first["final_url"])
    if nxt:
        more_urls.append((nxt, "list"))
    if recipe.detail.enabled:
        items, _ = extract_list_items(
            recipe, first["html"], first["final_url"], json_blobs=first.get("blobs") or None
        )
        links = [it.detail_url for it in items if it.detail_url]
        if links:
            more_urls.append((links[0], "detail"))
            if len(links) > 2:
                more_urls.append((links[len(links) // 2], "detail"))
    for i, (u, kind) in enumerate(more_urls):
        for s in fetch_snapshots([u], run_dir=run_dir / f"more{i}", recipe=recipe, kind=kind):
            samples.append(Sample(s["final_url"], s["html"], kind, s.get("blobs") or None))  # type: ignore[arg-type]

    report = validate_on_samples(recipe, samples, max_rows=rows)
    if console is not None:
        t = Table(title=f"preview: {recipe.name} — {report.summary()}", show_lines=False)
        cols = [f.name for f in recipe.fields]
        for c in cols:
            t.add_column(c, overflow="fold", max_width=32)
        for row in report.rows[:rows]:
            t.add_row(*[str(row.get(c, "")) if row.get(c) is not None else "" for c in cols])
        console.print(t)
        for i in report.issues:
            style = {"error": "red", "warn": "yellow", "info": "dim"}[i.level]
            console.print(f"[{style}]{i.level:5}[/] {i.code}: {i.message}")
        console.print(f"[dim]samples: {len(samples)} · tiers: {[s.get('tier') for s in snaps]}[/]")
    return report.to_dict()

"""Command-line entry point: `scrapy-awesome`.

Subcommands are added phase by phase. Heavy imports (Scrapy, FastAPI) happen inside commands so
`--help` and `doctor` stay fast and importable everywhere (including frozen builds).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="scrapy-awesome",
    help="Local-first, AI-assisted interactive web scraper built on Scrapy.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", "-V", help="Show version and exit.", is_eager=True)
    ] = False,
) -> None:
    if version:
        from scrapy_awesome import __version__

        console.print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def doctor(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Check the local toolchain, browsers, secrets and provider configuration."""
    from scrapy_awesome.doctor import run_checks

    checks = run_checks()
    if json_out:
        import json as _json

        console.print_json(_json.dumps([c.__dict__ for c in checks]))
        raise typer.Exit(code=1 if any(c.status == "fail" for c in checks) else 0)

    table = Table(title="scrapy-awesome doctor", show_lines=False)
    table.add_column("check", style="bold")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    style = {"ok": "green", "warn": "yellow", "fail": "red"}
    for c in checks:
        table.add_row(c.name, f"[{style[c.status]}]{c.status}[/]", c.detail)
    console.print(table)
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(code=1)


@app.command()
def run(
    recipe: Annotated[Path, typer.Argument(help="Recipe file (.yaml/.json)")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory")] = Path("out"),
    fmt: Annotated[
        list[str], typer.Option("--format", "-f", help="jsonl|json|csv|xlsx (repeatable)")
    ] = ["jsonl"],  # noqa: B006
    max_pages: Annotated[int | None, typer.Option(help="Override recipe max_pages")] = None,
    max_items: Annotated[int | None, typer.Option(help="Override recipe max_items")] = None,
    tier: Annotated[
        str | None, typer.Option(help="Force fetch tier: http|browser|interactive")
    ] = None,
) -> None:
    """Run a recipe headlessly and export the results."""
    from scrapy_awesome.crawl.runner import run_recipe_file

    stats = run_recipe_file(
        recipe, out_dir=out, formats=fmt, max_pages=max_pages, max_items=max_items, tier=tier
    )
    console.print(stats)


@app.command()
def preview(
    recipe: Annotated[Path, typer.Argument(help="Recipe file (.yaml/.json)")],
    rows: Annotated[int, typer.Option(help="Rows to show")] = 20,
) -> None:
    """Fetch sample pages, run the recipe on them in-process and print a preview table + diagnostics."""
    from scrapy_awesome.crawl.runner import preview_recipe_file

    preview_recipe_file(recipe, rows=rows, console=console)


@app.command()
def export(
    run_dir: Annotated[Path, typer.Argument(help="Run directory containing items.jsonl")],
    fmt: Annotated[str, typer.Option("--format", "-f")] = "xlsx",
    out: Annotated[Path | None, typer.Option("--out", "-o")] = None,
) -> None:
    """Re-export an existing run's items.jsonl to another format."""
    from scrapy_awesome.export.writers import export_jsonl_file

    dest = export_jsonl_file(run_dir / "items.jsonl", fmt=fmt, out=out)
    console.print(f"wrote {dest}")


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port (0 = random free port)")] = 0,
    no_open: Annotated[bool, typer.Option("--no-open", help="Don't open the browser")] = False,
    idle_exit: Annotated[
        int | None, typer.Option(help="Exit after N idle seconds with no active runs")
    ] = None,
    ppid_watch: Annotated[
        bool, typer.Option("--ppid-watch", help="Exit when the parent process exits (sidecar)")
    ] = False,
    dev: Annotated[
        bool, typer.Option("--dev", help="Allow the Vite dev server origin (CORS)")
    ] = False,
    log_level: Annotated[str, typer.Option()] = "info",
) -> None:
    """Start the local server + UI (writes server.json, prints a ready line, opens the browser)."""
    from scrapy_awesome.api.server import serve as _serve

    raise typer.Exit(
        code=_serve(
            port=port,
            open_browser=not no_open,
            idle_exit=idle_exit,
            ppid_watch=ppid_watch,
            dev=dev,
            log_level=log_level,
        )
    )


@app.command()
def passwd(
    username: Annotated[
        str, typer.Option(help="Username to set (default: keep the current one)")
    ] = "",
    reset: Annotated[
        bool, typer.Option("--reset", help="Remove the login, back to the first-run setup")
    ] = False,
) -> None:
    """Set (or reset) the username and password the UI signs in with."""
    import httpx

    from scrapy_awesome.api import credentials
    from scrapy_awesome.config import get_paths
    from scrapy_awesome.tools.client import running_server

    def _revoke() -> None:
        """A password change should not leave an open browser signed in."""
        info = running_server(get_paths())
        if not info:
            return
        with contextlib.suppress(Exception):
            httpx.post(
                f"{info['url']}/api/auth/revoke-sessions",
                headers={"Authorization": f"Bearer {info['token']}"},
                timeout=5,
            )

    if reset:
        removed = credentials.clear()
        _revoke()
        console.print(
            "[green]Login removed.[/green] The UI will ask you to create one."
            if removed
            else "No login was set."
        )
        return

    current = credentials.load()
    name = username.strip() or (current.username if current else "")
    if not name:
        name = typer.prompt("Username", default="admin")
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        creds = credentials.save(name, password)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    _revoke()
    console.print(
        f"[green]Login set for {creds.username}.[/green] Sign in at the app's login page."
    )


@app.command("open")
def open_ui(
    route: Annotated[str, typer.Argument(help="Where to land, e.g. /recipes")] = "/",
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Print the link, don't open it")
    ] = False,
) -> None:
    """Open the UI of the server that is already running (mints a fresh sign-in link).

    `serve` prints a sign-in link once, on the terminal it was started from — which is no help
    when the server runs in the background (`service install`, the desktop app, `nohup`). This
    reads server.json, checks the server is actually answering, and hands you the link again.
    """
    import webbrowser

    from scrapy_awesome.api import credentials
    from scrapy_awesome.config import get_paths
    from scrapy_awesome.tools.client import running_server

    info = running_server(get_paths())
    if not info:
        console.print("[red]No server is running.[/red] Start one with: scrapy-awesome serve")
        raise typer.Exit(code=1)
    target = route if route.startswith("/") else f"/{route}"
    # With a login configured the UI asks for it; a token in the URL would be a way around the
    # password, so `open` just points at the page.
    url = (
        f"{info['url']}{target}"
        if credentials.configured()
        else f"{info['url']}/auth?token={info['token']}&next={target}"
    )
    typer.echo(url)  # plain: a wrapped or marked-up URL is not copy-pasteable
    if not no_open:
        webbrowser.open(url)


@app.command()
def mcp() -> None:
    """Start the stdio MCP server (for Claude Code / Claude Desktop / Gemini CLI).

    Starts the local app server on first use and exposes its tools; logs go to stderr only.
    """
    from scrapy_awesome.mcp_server import main as _mcp_main

    raise typer.Exit(code=_mcp_main())


@app.command()
def service(
    action: Annotated[str, typer.Argument(help="install | uninstall | status")],
    port: Annotated[
        int | None, typer.Option(help="Fixed port for the background server (default: random)")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would be written; change nothing")
    ] = False,
) -> None:
    """Keep the server running in the background (launchd / systemd --user / Task Scheduler) so
    schedules fire without a terminal or the desktop app open."""
    from scrapy_awesome import service as _svc

    if action == "install":
        console.print(_svc.install(port, dry_run=dry_run))
    elif action == "uninstall":
        console.print(_svc.uninstall(dry_run=dry_run))
    elif action == "status":
        console.print(_svc.status())
    else:
        console.print("[red]action must be install | uninstall | status[/]")
        raise typer.Exit(code=2)


# Sub-process modes. The server re-executes *this same program* for crawl workers and the
# headed login window; in a frozen (PyInstaller) build there is no `python -m …`, so these
# flags are dispatched before Typer sees the argv.
_MODES = {
    "--worker": ("scrapy_awesome.crawl.worker", "main"),
    "--login-window": ("scrapy_awesome.browser_session.profile", "main"),
}


def main(argv: list[str] | None = None) -> int:
    """Process entry point (console script + frozen binary)."""
    import multiprocessing
    import sys

    multiprocessing.freeze_support()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _MODES:
        import importlib

        mod, fn = _MODES[argv[0]]
        return int(getattr(importlib.import_module(mod), fn)(argv[1:]) or 0)
    app(args=argv, prog_name="scrapy-awesome")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

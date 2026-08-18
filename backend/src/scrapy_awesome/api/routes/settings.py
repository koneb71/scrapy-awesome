"""Settings, secrets (masked), doctor, tier memory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scrapy_awesome.config import SECRET_ENV, SecretStore, UserSettings

router = APIRouter(tags=["settings"])


class SecretIn(BaseModel):
    value: str


def _mask(v: str) -> str:
    return v[:6] + "…" + v[-4:] if len(v) > 12 else "set"


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    settings: UserSettings = request.app.state.settings
    store = SecretStore(request.app.state.paths)
    secrets = {}
    for name in ("anthropic_api_key", "gemini_api_key"):
        value, source = store.get(name)  # type: ignore[arg-type]
        secrets[name] = {
            "set": bool(value),
            "masked": _mask(value) if value else None,
            "source": source,
            "env": SECRET_ENV[name],
        }
    import os

    return {
        "settings": settings.model_dump(mode="json"),
        "secrets": secrets,
        "data_dir": str(request.app.state.paths.root),
        # dev/test: SA_FAKE_LLM=1 runs an offline scripted designer (no key needed)
        "fake_llm": bool(os.environ.get("SA_FAKE_LLM")),
    }


@router.put("/settings")
def put_settings(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    current: UserSettings = request.app.state.settings
    merged = current.model_dump(mode="json")

    def deep(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep(dst[k], v)
            else:
                dst[k] = v

    deep(merged, body)
    try:
        new = UserSettings(**merged)
    except Exception as exc:  # pydantic validation
        raise HTTPException(422, str(exc)) from exc
    new.save(request.app.state.paths)
    st = request.app.state
    st.settings = new
    # every long-lived component keeps its own reference → re-point them all
    for name in ("manager", "chats", "scheduler", "fallback"):
        comp = getattr(st, name, None)
        if comp is not None:
            comp.settings = new
    return {"settings": new.model_dump(mode="json")}


@router.put("/settings/secrets/{name}")
def put_secret(request: Request, name: str, body: SecretIn) -> dict[str, Any]:
    if name not in SECRET_ENV:
        raise HTTPException(404, "unknown secret")
    store = SecretStore(request.app.state.paths)
    source = store.set(name, body.value.strip())  # type: ignore[arg-type]
    return {"name": name, "source": source, "masked": _mask(body.value.strip())}


@router.delete("/settings/secrets/{name}")
def delete_secret(request: Request, name: str) -> dict[str, Any]:
    if name not in SECRET_ENV:
        raise HTTPException(404, "unknown secret")
    SecretStore(request.app.state.paths).delete(name)  # type: ignore[arg-type]
    return {"name": name, "set": False}


@router.get("/settings/doctor")
def doctor(request: Request) -> list[dict[str, Any]]:
    from scrapy_awesome.doctor import run_checks

    return [c.__dict__ for c in run_checks()]


@router.get("/settings/tier-memory")
def tier_memory(request: Request) -> dict[str, str]:
    return request.app.state.store.tier_memory()


@router.delete("/settings/tier-memory/{domain}")
def forget_tier(request: Request, domain: str) -> dict[str, Any]:
    request.app.state.store.forget_tier(domain)
    return {"domain": domain, "forgotten": True}


@router.post("/settings/prune")
def prune_now(request: Request) -> dict[str, Any]:
    """Apply retention caps immediately (Settings → Storage → Prune now)."""
    out = request.app.state.scheduler.prune()
    return {**out, "data_size_bytes": request.app.state.store.data_size_bytes()}


@router.get("/settings/storage")
def storage(request: Request) -> dict[str, Any]:
    store = request.app.state.store
    return {
        "data_size_bytes": store.data_size_bytes(),
        "runs": len(store.list_runs(limit=100000)),
        "samples": len(store.list_samples(limit=100000)),
    }


# ---------------------------------------------------------------------- connect your agent
def mcp_command() -> list[str]:
    """The exact command an MCP client should run to start `scrapy-awesome mcp` *for this install*.

    Frozen (desktop) build → the binary itself; dev/uv checkout → `uv run --project <backend>`;
    otherwise the console script next to the current interpreter.
    """
    import shutil
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        return [sys.executable, "mcp"]
    here = Path(__file__).resolve()
    # <backend>/src/scrapy_awesome/api/routes/settings.py → <backend>
    backend = here.parents[4]
    if (backend / "pyproject.toml").exists() and shutil.which("uv"):
        return ["uv", "run", "--project", str(backend), "scrapy-awesome", "mcp"]
    exe = Path(sys.executable).with_name(
        "scrapy-awesome" + (".exe" if sys.platform == "win32" else "")
    )
    if exe.exists():
        return [str(exe), "mcp"]
    return [sys.executable, "-m", "scrapy_awesome", "mcp"]


def _trim_auth(a: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only what onboarding needs — never the email/org ids the CLI also reports."""
    if not a:
        return None
    return {k: a.get(k) for k in ("loggedIn", "authMethod", "subscriptionType")}


def _shell_join(cmd: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(c) for c in cmd)


@router.get("/settings/connect")
def connect_snippets(request: Request) -> dict[str, Any]:
    """Copy-paste snippets that connect the user's own Claude Code / Claude Desktop / Gemini CLI
    (their subscription, no API key) to this app's MCP server."""
    import json

    from scrapy_awesome.doctor import claude_auth_status

    cmd = mcp_command()
    server = {"command": cmd[0], "args": cmd[1:]}
    plugin_dir = None
    try:
        from pathlib import Path

        cand = Path(__file__).resolve().parents[5] / "plugin"
        if (cand / ".claude-plugin" / "plugin.json").exists():
            plugin_dir = str(cand)
    except Exception:
        plugin_dir = None
    return {
        "mcp_command": cmd,
        "claude_code": {
            "add": f"claude mcp add --scope user scrapy-awesome -- {_shell_join(cmd)}",
            "plugin_dir": plugin_dir,
            "plugin_add": f"claude plugin add {plugin_dir}" if plugin_dir else None,
            "auth": _trim_auth(claude_auth_status()),
        },
        "claude_desktop": {
            "file": "claude_desktop_config.json (Settings → Developer → Edit Config)",
            "json": json.dumps({"mcpServers": {"scrapy-awesome": server}}, indent=2),
        },
        "gemini_cli": {
            "file": "~/.gemini/settings.json",
            "json": json.dumps({"mcpServers": {"scrapy-awesome": server}}, indent=2),
            "add": f"gemini mcp add scrapy-awesome {_shell_join(cmd)}",
        },
        "note": (
            "These use *your* Claude / Gemini subscription through your own client; the app never "
            "sees the login. In-app API-key mode is separate (below)."
        ),
    }

"""Smoke-test a frozen (PyInstaller) build of scrapy-awesome.

Usage (from backend/, after building):
    uv run python packaging/smoke_test.py [dist/scrapy-awesome/scrapy-awesome]

Checks, against the black-box binary only:
  1. `doctor --json` runs and reports the core stack importable (scrapy, scrapy-stealth,
     scrapy-playwright, patchright, curl_cffi) — browsers may legitimately be missing on CI.
  2. `run examples/fixture-static.yaml` crawls the local fixture site over the http tier
     (the frozen binary re-executes itself with `--worker`) and writes JSONL + CSV.
  3. `serve --no-open --port 0 --idle-exit ...` prints the ready line, answers /health, serves the
     bundled UI, and exits promptly on SIGTERM.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))  # tests.fixtures.server (dev venv)

from tests.fixtures.server import FixtureServer  # noqa: E402

CORE = ("scrapy", "scrapy-stealth", "scrapy-playwright", "patchright", "curl_cffi", "python")


def _bin(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1]).resolve()
    name = "scrapy-awesome.exe" if os.name == "nt" else "scrapy-awesome"
    return BACKEND / "dist" / "scrapy-awesome" / name


def _run(
    cmd: list[str], env: dict[str, str], timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=timeout)


def check_doctor(exe: Path, env: dict[str, str]) -> None:
    p = _run([str(exe), "doctor", "--json"], env, timeout=120)
    print(p.stdout[-3000:], p.stderr[-2000:])
    checks = json.loads(p.stdout)
    by_name = {c["name"]: c for c in checks}
    for name in CORE:
        assert by_name.get(name, {}).get("status") == "ok", (
            f"doctor: {name} not ok: {by_name.get(name)}"
        )
    print("doctor OK:", ", ".join(f"{n}={by_name[n]['detail'].split(' ')[0]}" for n in CORE))


def check_crawl(exe: Path, env: dict[str, str], srv: FixtureServer, tmp: Path) -> None:
    recipe = tmp / "fixture-static.yaml"
    text = (BACKEND / "examples" / "fixture-static.yaml").read_text()
    recipe.write_text(text.replace("http://127.0.0.1:8765", srv.base_url))
    out = tmp / "out"
    p = _run(
        [
            str(exe),
            "run",
            str(recipe),
            "--out",
            str(out),
            "-f",
            "jsonl",
            "-f",
            "csv",
            "--max-pages",
            "2",
        ],
        env,
        timeout=600,
    )
    print(p.stdout[-3000:], p.stderr[-3000:])
    assert p.returncode == 0, f"run failed with {p.returncode}"
    jsonl = next(out.rglob("items.jsonl"), None)
    assert jsonl, f"no items.jsonl under {out}"
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) >= 5, f"expected ≥5 rows, got {len(rows)}"
    assert all(r.get("title") for r in rows), "rows without title"
    assert next(out.rglob("*.csv"), None), "no CSV export"
    print(
        f"crawl OK: {len(rows)} rows, first: {rows[0].get('title')!r} price={rows[0].get('price')!r}"
    )


def check_serve(exe: Path, env: dict[str, str]) -> None:
    # Windows: CTRL_BREAK_EVENT only reaches a process in its own group.
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        [str(exe), "serve", "--no-open", "--port", "0", "--idle-exit", "120"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    ready = None
    deadline = time.time() + 90
    assert proc.stdout is not None
    while time.time() < deadline and ready is None:
        line = proc.stdout.readline()
        if not line:
            break
        print("serve>", line.rstrip())
        if line.startswith("{") and '"port"' in line:
            ready = json.loads(line)
    assert ready, "no ready line from serve"
    base = ready["url"]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=10) as r:
            health = json.loads(r.read())
        assert health["ok"] is True, health
        with urllib.request.urlopen(f"{base}/auth?token={ready['token']}&next=/", timeout=10) as r:
            body = r.read().decode()
        assert '<div id="root">' in body or "scrapy-awesome" in body, "UI index not served"
        print("serve OK:", base, "version", health["version"])
    finally:
        t0 = time.time()
        sig = signal.SIGTERM if os.name != "nt" else signal.CTRL_BREAK_EVENT  # type: ignore[attr-defined]
        proc.send_signal(sig)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError(f"serve did not exit on {sig!r} within 15s") from None
        print(f"serve exited in {time.time() - t0:.1f}s")


def main(argv: list[str]) -> int:
    exe = _bin(argv)
    assert exe.exists(), f"frozen binary not found: {exe}"
    tmp = Path(tempfile.mkdtemp(prefix="sa-smoke-"))
    env = dict(os.environ)
    env["SCRAPY_AWESOME_HOME"] = str(tmp / "home")
    env.pop("SCRAPY_AWESOME_UI_DIR", None)
    try:
        check_doctor(exe, env)
        with FixtureServer() as srv:
            check_crawl(exe, env, srv, tmp)
        check_serve(exe, env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

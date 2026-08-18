"""`scrapy-awesome service install|uninstall|status` — keep the server running in the background so
schedules fire without a terminal or the desktop app open.

* macOS: a launchd *user agent* (`~/Library/LaunchAgents/com.scrapy-awesome.server.plist`).
* Linux: a systemd *user* unit (`~/.config/systemd/user/scrapy-awesome.service`); enable
  `loginctl enable-linger $USER` if it should run while you're logged out.
* Windows: printed `schtasks` command (Task Scheduler, run at logon).

The service runs `<this program> serve --no-open`; the UI is still reachable through
`server.json` (Settings → data dir) or `scrapy-awesome serve` (which detects the running server).
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path

from scrapy_awesome.config import get_paths
from scrapy_awesome.tools.client import serve_cmd

LABEL = "com.scrapy-awesome.server"
UNIT = "scrapy-awesome.service"


def _cmd(port: int | None) -> list[str]:
    cmd = [*serve_cmd(), "serve", "--no-open"]
    if port:
        cmd += ["--port", str(port)]
    return cmd


def _plist(port: int | None) -> tuple[Path, str]:
    paths = get_paths().ensure()
    args = "".join(f"\n        <string>{a}</string>" for a in _cmd(port))
    env = "".join(
        f"\n        <key>{k}</key><string>{v}</string>"
        for k, v in os.environ.items()
        if k in ("SCRAPY_AWESOME_HOME", "PATH")
    )
    text = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>{env}
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>{paths.logs / "service.log"}</string>
    <key>StandardErrorPath</key><string>{paths.logs / "service.log"}</string>
</dict>
</plist>
"""
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist", text


def _unit(port: int | None) -> tuple[Path, str]:
    paths = get_paths().ensure()
    exec_start = " ".join(shlex.quote(a) for a in _cmd(port))
    env = "\n".join(
        f"Environment={k}={shlex.quote(v)}"
        for k, v in os.environ.items()
        if k in ("SCRAPY_AWESOME_HOME", "PATH")
    )
    text = f"""[Unit]
Description=scrapy-awesome local server (schedules + UI)
After=network.target

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
{env}
StandardOutput=append:{paths.logs / "service.log"}
StandardError=append:{paths.logs / "service.log"}

[Install]
WantedBy=default.target
"""
    return Path.home() / ".config" / "systemd" / "user" / UNIT, text


def render(port: int | None = None) -> tuple[Path | None, str]:
    """(target path, file contents or command) for this OS."""
    sysname = platform.system()
    if sysname == "Darwin":
        return _plist(port)
    if sysname == "Linux":
        return _unit(port)
    if sysname == "Windows":
        tr = " ".join(f'"{a}"' if " " in a else a for a in _cmd(port))
        return None, f'schtasks /Create /F /SC ONLOGON /TN "scrapy-awesome" /TR "{tr}"'
    return None, f"unsupported platform {sysname}"


def install(port: int | None = None, *, dry_run: bool = False) -> str:
    target, text = render(port)
    sysname = platform.system()
    if target is None:
        return (
            f"Run this in an elevated PowerShell to install:\n  {text}\n"
            if sysname == "Windows"
            else text
        )
    if dry_run:
        return f"# would write {target}\n{text}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if sysname == "Darwin":
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True, check=False
        )
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return f"wrote {target} but launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}"
        return f"installed launchd agent {LABEL} ({target}); it starts now and at login."
    # Linux
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    r = subprocess.run(
        ["systemctl", "--user", "enable", "--now", UNIT],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return f"wrote {target} but systemctl failed: {r.stderr.strip() or r.stdout.strip()}"
    return (
        f"installed systemd user unit {UNIT} ({target}); it starts now and at login. "
        f"To keep it running while logged out: loginctl enable-linger {os.environ.get('USER', '$USER')}"
    )


def uninstall(*, dry_run: bool = False) -> str:
    sysname = platform.system()
    if sysname == "Darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if dry_run:
            return f"# would bootout {LABEL} and remove {target}"
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, check=False
        )
        target.unlink(missing_ok=True)
        return f"removed {LABEL}"
    if sysname == "Linux":
        target = Path.home() / ".config" / "systemd" / "user" / UNIT
        if dry_run:
            return f"# would disable {UNIT} and remove {target}"
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", UNIT], capture_output=True, check=False
        )
        target.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return f"removed {UNIT}"
    if sysname == "Windows":
        return 'Run: schtasks /Delete /F /TN "scrapy-awesome"'
    return f"unsupported platform {sysname}"


def status() -> str:
    sysname = platform.system()
    if sysname == "Darwin":
        r = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return "not installed"
        state = next((ln.strip() for ln in r.stdout.splitlines() if "state =" in ln), "installed")
        return f"{LABEL}: {state}"
    if sysname == "Linux":
        r = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True, check=False
        )
        return f"{UNIT}: {r.stdout.strip() or 'not installed'}"
    if sysname == "Windows":
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", "scrapy-awesome"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "installed" if r.returncode == 0 else "not installed"
    return f"unsupported platform {sysname}"


__all__ = ["install", "render", "status", "uninstall"]

if __name__ == "__main__":  # pragma: no cover
    print(install(dry_run=True) if "--dry-run" in sys.argv else status())

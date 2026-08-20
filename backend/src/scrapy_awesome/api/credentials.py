"""Username + password for the local UI.

Stored as a scrypt hash in `credentials.json` next to the database, 0600, never in the recipe
store or any export. The KDF is `hashlib.scrypt` — stdlib, memory-hard, no new dependency — with
parameters recorded alongside the hash so they can be raised later without invalidating anyone's
password.

This is the *human* door. Machine clients (the MCP server, the CLI, the crawl worker posting
events back) keep using the per-process bearer token in `server.json`: there is nobody there to
type a password, and both files are readable only by the account that runs the app.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, replace
from hashlib import scrypt
from pathlib import Path
from typing import Any

from scrapy_awesome.config import Paths, get_paths

FILENAME = "credentials.json"

# ~64 MB, ~100 ms on a 2026 laptop: slow enough to make a stolen file useless, fast enough that
# signing in feels instant.
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32

MIN_PASSWORD = 8


@dataclass(frozen=True)
class Credentials:
    username: str
    salt: str  # hex
    hash: str  # hex
    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "salt": self.salt,
            "hash": self.hash,
            "kdf": "scrypt",
            "n": self.n,
            "r": self.r,
            "p": self.p,
            "updated_at": self.updated_at,
        }


def _path(paths: Paths | None = None) -> Path:
    return (paths or get_paths()).root / FILENAME


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> str:
    # OpenSSL refuses anything over 32 MB unless the caller says how much it means to use, and
    # these parameters need 128*n*r = 64 MB. Ask for the exact amount, with room for the p copies.
    maxmem = 128 * n * r * (p + 1) + (1 << 20)
    return scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=maxmem
    ).hex()


def load(paths: Paths | None = None) -> Credentials | None:
    """The configured login, or None when nobody has set one yet (first run)."""
    p = _path(paths)
    try:
        raw = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return Credentials(
            username=str(raw["username"]),
            salt=str(raw["salt"]),
            hash=str(raw["hash"]),
            n=int(raw.get("n", SCRYPT_N)),
            r=int(raw.get("r", SCRYPT_R)),
            p=int(raw.get("p", SCRYPT_P)),
            updated_at=float(raw.get("updated_at", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def configured(paths: Paths | None = None) -> bool:
    return load(paths) is not None


def validate(username: str, password: str) -> str:
    """Empty string when the pair is usable, else why not (shown to the person setting it)."""
    if not username.strip():
        return "username cannot be empty"
    if len(username) > 64:
        return "username is too long (64 characters max)"
    if len(password) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if len(password.encode("utf-8")) > 1024:
        return "password is too long"
    return ""


def save(username: str, password: str, paths: Paths | None = None) -> Credentials:
    """Write (or replace) the login. Raises ValueError if the pair is unusable."""
    problem = validate(username, password)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(16)
    creds = Credentials(
        username=username.strip(),
        salt=salt.hex(),
        hash=_derive(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P),
        updated_at=time.time(),
    )
    p = _path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds.to_dict(), indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)  # atomic: a half-written file would lock everyone out
    return creds


def verify(username: str, password: str, paths: Paths | None = None) -> bool:
    """Constant-time check against the stored hash. False when nothing is configured."""
    creds = load(paths)
    if creds is None:
        return False
    # Derive regardless of whether the username matched, so a wrong username is not measurably
    # faster to reject than a wrong password.
    candidate = _derive(password, bytes.fromhex(creds.salt), n=creds.n, r=creds.r, p=creds.p)
    user_ok = hmac.compare_digest(username.strip(), creds.username)
    hash_ok = hmac.compare_digest(candidate, creds.hash)
    return user_ok and hash_ok


def clear(paths: Paths | None = None) -> bool:
    """Remove the login (back to the first-run state). True when a file was there."""
    p = _path(paths)
    try:
        p.unlink()
        return True
    except OSError:
        return False


def rename(username: str, paths: Paths | None = None) -> Credentials | None:
    """Change the username without touching the password."""
    creds = load(paths)
    if creds is None:
        return None
    updated = replace(creds, username=username.strip(), updated_at=time.time())
    p = _path(paths)
    p.write_text(json.dumps(updated.to_dict(), indent=2), "utf-8")
    os.chmod(p, 0o600)
    return updated


__all__ = [
    "MIN_PASSWORD",
    "Credentials",
    "clear",
    "configured",
    "load",
    "rename",
    "save",
    "validate",
    "verify",
]

"""`qis worker join / leave`: switch worker mode on or off in config.toml and store the join token (MESH.md 8.1)."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from ..models import write_config
from .state import normalize_hub_url

TOKEN_FILE = "worker.token"


def write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(token.strip() + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)   # same protection as data/secret.key
    os.replace(tmp, path)


def join(cfg_path: Path, hub_url: str, token: str, name: str | None = None) -> str:
    """Write [worker] (enabled = true) and config/worker.token; returns the normalized ws(s):// URL."""
    token = token.strip()
    if not token:
        raise ValueError("the join token is empty")
    url = normalize_hub_url(hub_url)
    write_token(cfg_path.parent / TOKEN_FILE, token)
    values: dict = {"enabled": True, "hub_url": url, "token_file": TOKEN_FILE}
    if name:
        values["name"] = name
    write_config(cfg_path, {"worker": values})
    return url


def leave(cfg_path: Path) -> None:
    """Worker mode off; the token file stays so a later `join` without --token is not needed to remember it."""
    write_config(cfg_path, {"worker": {"enabled": False}})

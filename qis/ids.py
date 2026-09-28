"""Prefixed ULIDs: sortable by time and unique across nodes (no autoincrement ids anywhere)."""
from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last = [0, 0]  # ms, random part: monotonic within one millisecond


def ulid() -> str:
    with _lock:
        ms = int(time.time() * 1000)
        if ms == _last[0]:
            _last[1] += 1
        else:
            _last[0], _last[1] = ms, int.from_bytes(os.urandom(10), "big")
        n = (ms << 80) | (_last[1] & ((1 << 80) - 1))
    return "".join(_ALPHABET[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"

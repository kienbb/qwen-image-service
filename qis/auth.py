"""API keys (Bearer sk-qis-...), dashboard users (argon2id) and sessions (cookie + CSRF), behind small services.

An AuthProvider interface keeps room for OIDC / Cloudflare Access later (DESIGN §10).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .db import DB
from .ids import new_id

SCOPES = ["images:generate", "images:edit", "tools:*", "files:*", "jobs:read", "admin:*"]
DEFAULT_SCOPES = ["images:generate", "images:edit", "tools:*", "files:*", "jobs:read"]
ROLES = {"admin": 3, "operator": 2, "viewer": 1}
SESSION_IDLE_S = 12 * 3600
SESSION_MAX_S = 7 * 86400
LOCK_AFTER = 5
LOCK_S = 15 * 60
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def has_scope(scopes: list[str], need: str) -> bool:
    if "admin:*" in scopes or need in scopes:
        return True
    group = need.split(":")[0] + ":*"
    return group in scopes


# ---------------------------------------------------------------- API keys
class ApiKeys:
    def __init__(self, db: DB, secret: bytes):
        self.db = db
        self.pepper = hmac.new(secret, b"api-key-pepper", hashlib.sha256).digest()
        self._last_used: dict[str, float] = {}

    def _hash(self, key: str) -> str:
        return hmac.new(self.pepper, key.encode(), hashlib.sha256).hexdigest()

    def create(self, name: str, *, scopes: list[str] | None = None, owner_user_id: str | None = None,
               priority: str = "interactive", max_pending: int | None = None, rpm: int | None = None,
               daily_images: int | None = None, allowed_models: list[str] | None = None,
               expires_at: float | None = None) -> tuple[str, dict]:
        scopes = scopes or list(DEFAULT_SCOPES)
        bad = [s for s in scopes if s not in SCOPES]
        if bad:
            raise AuthError(400, "invalid_scope", f"unknown scopes {bad}; valid: {SCOPES}")
        if priority not in ("interactive", "batch"):
            raise AuthError(400, "invalid_priority", "priority must be interactive or batch")
        key = "sk-qis-" + "".join(secrets.choice(_B62) for _ in range(40))
        row = {"id": new_id("key"), "name": name.strip()[:100] or "key", "prefix": key[:14], "hash": self._hash(key),
               "owner_user_id": owner_user_id, "scopes": scopes, "priority": priority, "max_pending": max_pending,
               "rpm": rpm, "daily_images": daily_images, "allowed_models": allowed_models, "created_at": time.time(),
               "expires_at": expires_at}
        self.db.insert("api_keys", row)
        return key, self.view(row)

    @staticmethod
    def view(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "hash"}

    def verify(self, key: str) -> dict:
        if not key or not key.startswith("sk-qis-"):
            raise AuthError(401, "invalid_api_key", "Incorrect API key provided.")
        row = self.db.one("SELECT * FROM api_keys WHERE hash=?", (self._hash(key),))
        if not row or row["revoked_at"]:
            raise AuthError(401, "invalid_api_key", "Incorrect API key provided.")
        if row["expires_at"] and row["expires_at"] < time.time():
            raise AuthError(401, "expired_api_key", "This API key has expired.")
        now = time.time()
        if now - self._last_used.get(row["id"], 0) > 60:  # avoid a write per request
            self._last_used[row["id"]] = now
            self.db.update("api_keys", "id", row["id"], {"last_used_at": now})
        return row

    def list(self, owner_user_id: str | None = None) -> list[dict]:
        if owner_user_id:
            rows = self.db.all("SELECT * FROM api_keys WHERE owner_user_id=? ORDER BY created_at DESC", (owner_user_id,))
        else:
            rows = self.db.all("SELECT * FROM api_keys ORDER BY created_at DESC")
        return [self.view(r) for r in rows]

    def get(self, key_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM api_keys WHERE id=?", (key_id,))
        return self.view(row) if row else None

    def update(self, key_id: str, fields: dict) -> dict | None:
        allowed = {"name", "scopes", "priority", "max_pending", "rpm", "daily_images", "allowed_models", "expires_at"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "scopes" in fields and any(s not in SCOPES for s in fields["scopes"]):
            raise AuthError(400, "invalid_scope", f"valid scopes: {SCOPES}")
        self.db.update("api_keys", "id", key_id, fields)
        return self.get(key_id)

    def revoke(self, key_id: str) -> bool:
        return self.db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                               (time.time(), key_id)).rowcount > 0


# ---------------------------------------------------------------- users + sessions
class AuthProvider(Protocol):
    def authenticate(self, login: str, password: str, ip: str = "") -> dict: ...


USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")


class Users:
    def __init__(self, db: DB):
        self.db = db

    @staticmethod
    def view(row: dict | None) -> dict | None:
        if not row:
            return None
        return {k: v for k, v in row.items() if k not in ("pw_hash",)}

    @staticmethod
    def check_password(pw: str) -> None:
        if len(pw) < 10:
            raise AuthError(400, "weak_password", "password must be at least 10 characters")

    def create(self, username: str, password: str, *, email: str | None = None, role: str = "viewer",
               must_change_pw: bool = False) -> dict:
        if not USERNAME.match(username):
            raise AuthError(400, "invalid_username", "username: 2-64 letters, digits, _ . -")
        if role not in ROLES:
            raise AuthError(400, "invalid_role", f"role must be one of {list(ROLES)}")
        if email and "@" not in email:
            raise AuthError(400, "invalid_email", "invalid email")
        self.check_password(password)
        if self.db.one("SELECT id FROM users WHERE username=? OR (email IS NOT NULL AND email=?)", (username, email)):
            raise AuthError(409, "user_exists", "a user with this username or email already exists")
        row = {"id": new_id("usr"), "username": username, "email": email or None, "pw_hash": _ph.hash(password),
               "role": role, "must_change_pw": int(must_change_pw), "created_at": time.time()}
        self.db.insert("users", row)
        return self.view(row)

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM users") or 0)

    def get(self, user_id: str) -> dict | None:
        return self.view(self.db.one("SELECT * FROM users WHERE id=?", (user_id,)))

    def find(self, login: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE username=? OR email=?", (login, login.lower()))

    def list(self) -> list[dict]:
        return [self.view(r) for r in self.db.all("SELECT * FROM users ORDER BY created_at")]

    def authenticate(self, login: str, password: str, ip: str = "") -> dict:
        row = self.find(login.strip())
        generic = AuthError(401, "invalid_credentials", "wrong username/email or password")
        if not row:
            _ph.hash(password)  # same cost as a real check: no user enumeration by timing
            raise generic
        now = time.time()
        if row["locked_until"] and row["locked_until"] > now:
            raise AuthError(423, "locked", f"too many failed logins; try again in {int(row['locked_until'] - now) // 60 + 1} min")
        if row["disabled"]:
            raise AuthError(403, "disabled", "this account is disabled")
        try:
            _ph.verify(row["pw_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            fails = row["failed_logins"] + 1
            fields = {"failed_logins": fails}
            if fails >= LOCK_AFTER:
                fields = {"failed_logins": 0, "locked_until": now + LOCK_S}
            self.db.update("users", "id", row["id"], fields)
            self.db.audit(row["username"], "login.failed", row["id"], ip=ip)
            raise generic from None
        fields = {"failed_logins": 0, "locked_until": None, "last_login_at": now}
        if _ph.check_needs_rehash(row["pw_hash"]):
            fields["pw_hash"] = _ph.hash(password)
        self.db.update("users", "id", row["id"], fields)
        return self.view({**row, **fields})

    def set_password(self, user_id: str, password: str, must_change: bool = False) -> None:
        self.check_password(password)
        self.db.update("users", "id", user_id, {"pw_hash": _ph.hash(password), "must_change_pw": int(must_change),
                                                "failed_logins": 0, "locked_until": None})
        self.db.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))

    def verify_password(self, user_id: str, password: str) -> bool:
        row = self.db.one("SELECT pw_hash FROM users WHERE id=?", (user_id,))
        try:
            return bool(row) and _ph.verify(row["pw_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def update(self, user_id: str, fields: dict) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in ("email", "role", "disabled")}
        if "role" in fields and fields["role"] not in ROLES:
            raise AuthError(400, "invalid_role", f"role must be one of {list(ROLES)}")
        if "disabled" in fields:
            fields["disabled"] = int(bool(fields["disabled"]))
            if fields["disabled"]:
                self.db.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))
        self.db.update("users", "id", user_id, fields)
        return self.get(user_id)

    def unlock(self, user_id: str) -> None:
        self.db.update("users", "id", user_id, {"failed_logins": 0, "locked_until": None})

    def delete(self, user_id: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.db.execute("DELETE FROM users WHERE id=?", (user_id,))


class Sessions:
    def __init__(self, db: DB):
        self.db = db

    @staticmethod
    def _sid(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, user_id: str, ip: str = "", ua: str = "") -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        self.db.insert("sessions", {"id": self._sid(token), "user_id": user_id, "csrf": csrf, "ip": ip, "ua": ua[:300],
                                    "created_at": now, "last_seen": now, "expires_at": now + SESSION_MAX_S})
        return token, csrf

    def verify(self, token: str) -> tuple[dict, dict] | None:
        if not token:
            return None
        s = self.db.one("SELECT * FROM sessions WHERE id=?", (self._sid(token),))
        now = time.time()
        if not s or s["revoked"] or s["expires_at"] < now or now - s["last_seen"] > SESSION_IDLE_S:
            return None
        user = self.db.one("SELECT * FROM users WHERE id=?", (s["user_id"],))
        if not user or user["disabled"]:
            return None
        if now - s["last_seen"] > 60:
            self.db.update("sessions", "id", s["id"], {"last_seen": now})
        return s, Users.view(user)

    def revoke(self, token: str) -> None:
        self.db.update("sessions", "id", self._sid(token), {"revoked": 1})

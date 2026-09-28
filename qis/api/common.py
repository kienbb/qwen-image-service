"""Shared API plumbing: OpenAI-style errors, auth dependencies, client IP, image input parsing."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..auth import ROLES, AuthError, has_scope
from ..engine.comfy.client import ComfyUnavailable
from ..engine.ops import OpError
from ..node import Node
from ..scheduler import AdmissionError
from ..settings import SettingsError
from ..storage import InputError, normalize_image

log = logging.getLogger("qis.api")

ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
               404: "not_found_error", 409: "conflict_error", 413: "invalid_request_error", 423: "authentication_error",
               429: "rate_limit_error", 503: "server_error", 507: "server_error"}


def error_body(status: int, message: str, code: str | None = None, param: str | None = None) -> dict:
    typ = ERROR_TYPES.get(status, "server_error" if status >= 500 else "invalid_request_error")
    return {"error": {"message": message, "type": typ, "param": param, "code": code}}


def err(status: int, message: str, code: str | None = None, param: str | None = None, headers: dict | None = None):
    return JSONResponse(error_body(status, message, code, param), status_code=status, headers=headers)


def install_error_handlers(app) -> None:
    @app.exception_handler(AuthError)
    async def _auth(request, e: AuthError):
        return err(e.status, e.message, e.code)

    @app.exception_handler(AdmissionError)
    async def _adm(request, e: AdmissionError):
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
        if e.status == 429:
            node: Node = request.app.state.node
            headers["X-Qis-Queue-Depth"] = str(node.jobs.queue_stats()["queued"])
        return err(e.status, e.message, e.code, e.param, headers or None)

    @app.exception_handler(OpError)
    async def _op(request, e: OpError):
        return err(400, str(e), "invalid_request")

    @app.exception_handler(InputError)
    async def _inp(request, e: InputError):
        return err(400, str(e), "invalid_image")

    @app.exception_handler(SettingsError)
    async def _set(request, e: SettingsError):
        return err(400, str(e), "invalid_settings")

    @app.exception_handler(ComfyUnavailable)
    async def _cu(request, e: ComfyUnavailable):
        return err(503, str(e), "engine_unavailable")

    @app.exception_handler(RequestValidationError)
    async def _val(request, e: RequestValidationError):
        errors = e.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query"))
        msg = first.get("msg", "invalid request")
        return err(400, f"{loc}: {msg}" if loc else msg, "invalid_request", loc or None)

    @app.exception_handler(ValidationError)
    async def _pyd(request, e: ValidationError):
        errors = e.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = first.get("msg", "invalid request")
        return err(400, f"{loc}: {msg}" if loc else msg, "invalid_request", loc or None)

    @app.exception_handler(HTTPException)
    async def _http(request, e: HTTPException):
        if isinstance(e.detail, dict) and "error" in e.detail:
            return JSONResponse(e.detail, status_code=e.status_code, headers=getattr(e, "headers", None))
        return err(e.status_code, str(e.detail), None, None, getattr(e, "headers", None))


def get_node(request: Request) -> Node:
    return request.app.state.node


def client_ip(request: Request) -> str:
    node: Node = request.app.state.node
    peer = request.client.host if request.client else ""
    if peer in node.cfg.security.trust_proxy_from:
        fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return fwd or peer
    return peer


def api_key(request: Request, node: Node = Depends(get_node)) -> dict:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not token:
        raise AuthError(401, "missing_api_key", "You did not provide an API key. Use 'Authorization: Bearer sk-qis-...'.")
    key = node.keys.verify(token)
    request.state.key = key
    return key


def need(scope: str):
    def dep(key: dict = Depends(api_key)) -> dict:
        if not has_scope(key.get("scopes") or [], scope):
            raise AuthError(403, "insufficient_scope", f"this API key lacks the '{scope}' scope")
        return key
    return dep


def check_model_allowed(key: dict, model: str) -> None:
    allowed = key.get("allowed_models")
    if allowed and model not in allowed and "admin:*" not in (key.get("scopes") or []):
        raise AuthError(403, "model_not_allowed", f"this API key may not use model {model!r}")


# ---------------------------------------------------------------- admin session
def session_user(request: Request, node: Node = Depends(get_node)) -> dict:
    token = request.cookies.get("qis_session", "")
    res = node.sessions.verify(token)
    if not res:
        raise AuthError(401, "not_logged_in", "login required")
    sess, user = res
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-csrf-token") != sess["csrf"]:
            raise AuthError(403, "csrf", "missing or invalid X-CSRF-Token")
        origin = request.headers.get("origin")
        if origin:
            host = urlparse(origin).netloc
            allowed = {request.url.netloc, urlparse(node.public_url).netloc}
            allowed |= {urlparse(o).netloc for o in node.cfg.security.cors_origins}
            if host not in allowed:
                raise AuthError(403, "bad_origin", f"origin {origin} not allowed")
    request.state.user = user
    request.state.session = sess
    return user


def role(min_role: str):
    def dep(user: dict = Depends(session_user)) -> dict:
        if user.get("must_change_pw"):
            raise AuthError(403, "must_change_password", "change your password first")
        if ROLES.get(user["role"], 0) < ROLES[min_role]:
            raise AuthError(403, "forbidden", f"requires role {min_role}")
        return user
    return dep


# ---------------------------------------------------------------- image inputs
def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def fetch_url(node: Node, url: str) -> bytes:
    st = node.settings.current.inputs
    if not st.allow_url:
        raise InputError("image URLs are disabled on this node; upload the file or send base64")
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or not _is_public_host(u.hostname):
        raise InputError("only public https URLs are accepted")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c, c.stream("GET", url) as r:
        if r.status_code != 200:
            raise InputError(f"fetching {url} returned {r.status_code}")
        data = b""
        async for chunk in r.aiter_bytes():
            data += chunk
            if len(data) > st.max_bytes:
                raise InputError("image at URL is too large")
        return data


def store_input(node: Node, key: dict | None, data: bytes, name: str = "input.png") -> str:
    st = node.settings.current
    info = normalize_image(data, st.inputs.max_bytes, st.inputs.max_pixels)
    row = node.storage.put_image(info, purpose="input", hours=st.storage.input_hours,
                                 owner_key_id=key["id"] if key else None, name=name[:200])
    return row["id"]


async def resolve_image_value(node: Node, key: dict | None, value: str) -> str:
    """file id, $reference, data: URL, raw base64 or https URL -> file id (or the reference unchanged)."""
    if value.startswith(("file_", "$")):
        return value
    if value.startswith("data:"):
        try:
            data = base64.b64decode(value.split(",", 1)[1], validate=False)
        except (IndexError, binascii.Error) as e:
            raise InputError("bad data URL") from e
        return store_input(node, key, data)
    if value.startswith(("http://", "https://")):
        return store_input(node, key, await fetch_url(node, value))
    try:
        data = base64.b64decode(value, validate=True)
    except binascii.Error as e:
        raise InputError("image must be a file id, data URL, base64 or https URL") from e
    return store_input(node, key, data)

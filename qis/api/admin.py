"""Admin API used by the dashboard (session cookie + CSRF) and by automation (API key with the admin:* scope)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..auth import ROLES, SCOPES, AuthError
from ..bench import run_internal
from ..engine import hardware
from ..models import Manifest, installed, models_root, profile_config, write_config
from ..node import Node
from ..pipeline import JobSpec
from .common import client_ip, get_node, session_user, store_input
from .jobs import JobCreate, build_spec
from .present import job_public

router = APIRouter(prefix="/admin/api", tags=["admin"])
_login_hits: dict[str, deque] = defaultdict(deque)
COOKIE = "qis_session"


# ---------------------------------------------------------------- auth plumbing
def actor(min_role: str = "viewer", allow_must_change: bool = False):
    """Session user (dashboard) or an admin:* API key (automation, acts as role admin)."""
    def dep(request: Request, node: Node = Depends(get_node)) -> dict:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = node.keys.verify(auth[7:].strip())
            if "admin:*" not in (key.get("scopes") or []):
                raise AuthError(403, "insufficient_scope", "admin API needs a key with the admin:* scope")
            return {"id": None, "username": f"key:{key['name']}", "role": "admin", "key_id": key["id"]}
        user = session_user(request, node)
        if user.get("must_change_pw") and not allow_must_change:
            raise AuthError(403, "must_change_password", "change your password first")
        if ROLES.get(user["role"], 0) < ROLES[min_role]:
            raise AuthError(403, "forbidden", f"requires role {min_role}")
        return user
    return dep


def _is_local(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    return peer in ("127.0.0.1", "::1") and not request.headers.get("cf-connecting-ip") \
        and not request.headers.get("x-forwarded-for")


def _set_cookie(request: Request, response: Response, token: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" \
        or bool(request.headers.get("cf-visitor", "").count("https"))
    response.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite="lax", path="/", max_age=7 * 86400)


class LoginBody(BaseModel):
    login: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


class SetupBody(BaseModel):
    username: str
    password: str
    email: str | None = None


class PasswordBody(BaseModel):
    current: str
    new: str


@router.get("/auth/bootstrap")
async def bootstrap(request: Request, node: Node = Depends(get_node)):
    return {"needs_setup": node.users.count() == 0, "setup_allowed_here": _is_local(request), "version": __version__,
            "node": node.name}


@router.post("/auth/setup")
async def setup(body: SetupBody, request: Request, response: Response, node: Node = Depends(get_node)):
    """First admin, only when there is no user yet and only from the machine itself (not through the tunnel)."""
    if node.users.count() > 0:
        raise AuthError(409, "already_setup", "an admin already exists")
    if not _is_local(request):
        raise AuthError(403, "setup_local_only", "create the first admin on the node itself (qis admin create) or from localhost")
    user = node.users.create(body.username, body.password, email=body.email, role="admin")
    token, csrf = node.sessions.create(user["id"], client_ip(request), request.headers.get("user-agent", ""))
    _set_cookie(request, response, token)
    node.db.audit(user["username"], "user.setup", user["id"], ip=client_ip(request))
    return {"user": user, "csrf": csrf}


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response, node: Node = Depends(get_node)):
    ip = client_ip(request)
    hits = _login_hits[ip]
    now = time.time()
    while hits and hits[0] < now - 60:
        hits.popleft()
    if len(hits) >= 20:
        raise AuthError(429, "too_many_attempts", "too many login attempts from this address; wait a minute")
    hits.append(now)
    user = await asyncio.to_thread(node.users.authenticate, body.login, body.password, ip)
    token, csrf = node.sessions.create(user["id"], ip, request.headers.get("user-agent", ""))
    _set_cookie(request, response, token)
    node.db.audit(user["username"], "login", user["id"], ip=ip)
    return {"user": user, "csrf": csrf}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, node: Node = Depends(get_node)):
    token = request.cookies.get(COOKIE, "")
    if token:
        node.sessions.revoke(token)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request, node: Node = Depends(get_node)):
    res = node.sessions.verify(request.cookies.get(COOKIE, ""))
    if not res:
        raise AuthError(401, "not_logged_in", "login required")
    sess, user = res
    return {"user": user, "csrf": sess["csrf"]}


@router.post("/auth/password")
async def change_password(body: PasswordBody, request: Request, response: Response,
                          user: dict = Depends(actor("viewer", allow_must_change=True)), node: Node = Depends(get_node)):
    if not user.get("id"):
        raise AuthError(400, "not_a_user", "API keys have no password")
    if not await asyncio.to_thread(node.users.verify_password, user["id"], body.current):
        raise AuthError(400, "wrong_password", "current password is wrong")
    await asyncio.to_thread(node.users.set_password, user["id"], body.new)
    token, csrf = node.sessions.create(user["id"], client_ip(request), request.headers.get("user-agent", ""))
    _set_cookie(request, response, token)
    node.db.audit(user["username"], "user.password", user["id"], ip=client_ip(request))
    return {"ok": True, "csrf": csrf}


# ---------------------------------------------------------------- overview
@router.get("/status")
async def status(user: dict = Depends(actor()), node: Node = Depends(get_node)):
    st = node.status()
    since = time.time() - 86400
    agg = node.db.one("SELECT COALESCE(SUM(jobs_done),0) done, COALESCE(SUM(jobs_failed),0) failed, "
                      "COALESCE(SUM(gpu_s),0) gpu_s, COALESCE(SUM(images),0) images FROM stats_minute WHERE ts>=?", (since,))
    running = [node.jobs.get(j) for j in list(node.jobs.running)]
    st.update({"last_24h": agg, "storage_bytes": node.storage.usage_bytes(), "public_url": node.public_url,
               "profile": node.cfg.profile.tier, "running_jobs": [j for j in running if j],
               "preview": {j: node.events.last_preview.get(j, {}).get("data") for j in node.jobs.running}})
    return st


@router.get("/stats")
async def stats(hours: int = Query(24, ge=1, le=720), bucket_min: int = Query(10, ge=1, le=1440),
                user: dict = Depends(actor()), node: Node = Depends(get_node)):
    since = int(time.time() - hours * 3600)
    rows = node.db.all("SELECT ts, jobs_done, jobs_failed, gpu_s, queue_depth, images FROM stats_minute WHERE ts>=? ORDER BY ts",
                       (since,))
    buckets: dict[int, dict] = {}
    for r in rows:
        b = r["ts"] // (bucket_min * 60) * bucket_min * 60
        cur = buckets.setdefault(b, {"ts": b, "jobs_done": 0, "jobs_failed": 0, "gpu_s": 0.0, "queue_depth": 0, "images": 0})
        for k in ("jobs_done", "jobs_failed", "images"):
            cur[k] += r[k]
        cur["gpu_s"] += r["gpu_s"]
        cur["queue_depth"] = max(cur["queue_depth"], r["queue_depth"])
    return {"bucket_s": bucket_min * 60, "data": list(buckets.values())}


@router.get("/events")
async def admin_events(request: Request, user: dict = Depends(actor()), node: Node = Depends(get_node)):
    q = node.events.subscribe_admin()

    async def gen():
        try:
            yield f"event: node.status\ndata: {json.dumps(node.status(), default=str)}\n\n".encode()
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield b": ping\n\n"
                    continue
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n".encode()
        finally:
            node.events.unsubscribe_admin(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- jobs
@router.get("/jobs")
async def jobs(request: Request, status: str | None = None, key_id: str | None = None, limit: int = Query(50, ge=1, le=500),
               before: str | None = None, user: dict = Depends(actor()), node: Node = Depends(get_node)):
    rows = node.jobs.list(key_id=key_id, status=status, limit=limit, before=before, all_keys=True)
    names = {k["id"]: k["name"] for k in node.keys.list()}
    data = []
    for j in rows:
        pub = job_public(node, request, j)
        pub["key_name"] = names.get(j["api_key_id"]) if j["api_key_id"] else "dashboard"
        pub["api_key_id"] = j["api_key_id"]
        data.append(pub)
    return {"data": data, "has_more": len(rows) == limit, "queue": node.jobs.queue_stats()}


@router.get("/jobs/{job_id}")
async def job(job_id: str, request: Request, user: dict = Depends(actor()), node: Node = Depends(get_node)):
    j = node.jobs.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    pub = job_public(node, request, j)
    pub["events"] = node.events.history(job_id)
    pub["api_key_id"] = j["api_key_id"]
    if j.get("result"):
        steps = []
        for s in j["result"].get("steps", []):
            steps.append({**s, "images": [{"file_id": f, "url": _url(node, request, f)} for f in s.get("images", [])]})
        pub["result"]["steps"] = steps
    return pub


def _url(node, request, fid):
    from .present import file_url
    return file_url(node, request, fid)


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str, request: Request, user: dict = Depends(actor("operator")), node: Node = Depends(get_node)):
    j = await node.jobs.cancel(job_id, actor=user["username"])
    if not j:
        raise HTTPException(404, "job not found")
    node.db.audit(user["username"], "job.cancel", job_id, ip=client_ip(request))
    return job_public(node, request, j)


@router.post("/jobs/{job_id}/retry")
async def retry(job_id: str, request: Request, user: dict = Depends(actor("operator")), node: Node = Depends(get_node)):
    j = node.jobs.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    key = node.keys.get(j["api_key_id"]) if j["api_key_id"] else None
    new = node.jobs.submit(JobSpec.model_validate(j["spec"]), key=key, user_id=user.get("id"), kind="pipeline")
    return job_public(node, request, new)


@router.post("/queue/{action}")
async def queue_action(action: str, request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    if action not in ("pause", "resume"):
        raise HTTPException(404, "unknown action")
    node.jobs.paused = action == "pause"
    node.jobs._wake.set()
    node.db.audit(user["username"], f"queue.{action}", ip=client_ip(request))
    return node.jobs.queue_stats()


# ---------------------------------------------------------------- playground
@router.post("/playground/files")
async def pg_upload(file: UploadFile = File(...), user: dict = Depends(actor("operator")), node: Node = Depends(get_node)):
    fid = store_input(node, None, await file.read(), file.filename or "upload.png")
    row = node.storage.get(fid)
    return {"id": fid, "width": row["width"], "height": row["height"]}


@router.post("/playground/jobs")
async def pg_job(body: JobCreate, request: Request, user: dict = Depends(actor("operator")), node: Node = Depends(get_node)):
    spec = await build_spec(node, None, body)
    job = node.jobs.submit(spec, key=None, user_id=user.get("id"), kind="playground")
    return job_public(node, request, job)


@router.get("/playground/jobs/{job_id}/events")
async def pg_events(job_id: str, request: Request, user: dict = Depends(actor()), node: Node = Depends(get_node)):
    from .present import job_sse
    return StreamingResponse(job_sse(node, job_id, request, 0), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/presets")
async def presets(user: dict = Depends(actor()), node: Node = Depends(get_node)):
    avail = node.presets.available(node.cfg.models.model_set())
    return {"presets": node.presets.data, "available": sorted(avail), "path": str(node.presets.path or "")}


@router.put("/presets")
async def put_presets(body: dict[str, Any], request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    try:
        node.presets.save(body)
    except ValueError as e:
        raise AuthError(400, "invalid_presets", str(e)) from e
    node.db.audit(user["username"], "presets.update", "", {"names": sorted(body)}, ip=client_ip(request))
    return {"presets": node.presets.data}


# ---------------------------------------------------------------- settings
@router.get("/settings")
async def get_settings(user: dict = Depends(actor()), node: Node = Depends(get_node)):
    cfg = node.cfg
    return {"values": node.settings.current.model_dump(), "schema": node.settings.schema(),
            "config": {"source": cfg.source, "server": cfg.server.model_dump(), "comfy": cfg.comfy.model_dump(),
                       "profile": cfg.profile.model_dump(), "data_dir": str(cfg.data_dir), "comfyui": cfg.paths.comfyui}}


@router.put("/settings/{group}")
async def put_settings(group: str, body: dict[str, Any], request: Request, user: dict = Depends(actor("admin")),
                       node: Node = Depends(get_node)):
    before = getattr(node.settings.current, group, None)
    node.settings.update(group, body, actor=user["username"])
    node.db.audit(user["username"], "settings.update", group, {"values": body,
                  "before": before.model_dump() if before is not None else None}, ip=client_ip(request))
    return {"values": node.settings.current.model_dump()}


# ---------------------------------------------------------------- keys
class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] | None = None
    priority: str = "interactive"
    max_pending: int | None = Field(None, ge=1)
    rpm: int | None = Field(None, ge=1)
    daily_images: int | None = Field(None, ge=1)
    allowed_models: list[str] | None = None
    expires_in_days: float | None = Field(None, gt=0)


@router.get("/keys")
async def keys(user: dict = Depends(actor("operator")), node: Node = Depends(get_node)):
    rows = node.keys.list(None if user["role"] == "admin" else user["id"])
    since = time.time() - 86400
    usage = {r["api_key_id"]: r for r in node.db.all(
        "SELECT api_key_id, COUNT(*) jobs, SUM(status='failed') failed FROM jobs WHERE created_at>=? GROUP BY api_key_id", (since,))}
    for k in rows:
        u = usage.get(k["id"]) or {}
        k["jobs_24h"], k["failed_24h"] = u.get("jobs", 0), u.get("failed", 0) or 0
    return {"data": rows, "scopes": SCOPES}


@router.post("/keys")
async def create_key(body: KeyBody, request: Request, user: dict = Depends(actor("operator")), node: Node = Depends(get_node)):
    scopes = body.scopes
    if user["role"] != "admin" and scopes and "admin:*" in scopes:
        raise AuthError(403, "forbidden", "only admins can create admin keys")
    exp = time.time() + body.expires_in_days * 86400 if body.expires_in_days else None
    key, row = node.keys.create(body.name, scopes=scopes, owner_user_id=user.get("id"), priority=body.priority,
                                max_pending=body.max_pending, rpm=body.rpm, daily_images=body.daily_images,
                                allowed_models=body.allowed_models, expires_at=exp)
    node.db.audit(user["username"], "key.create", row["id"], {"name": body.name, "scopes": row["scopes"]}, ip=client_ip(request))
    return {"key": key, **row}


def _own_key(node: Node, user: dict, key_id: str) -> dict:
    k = node.keys.get(key_id)
    if not k or (user["role"] != "admin" and k["owner_user_id"] != user["id"]):
        raise HTTPException(404, "key not found")
    return k


@router.patch("/keys/{key_id}")
async def patch_key(key_id: str, body: dict[str, Any], request: Request, user: dict = Depends(actor("operator")),
                    node: Node = Depends(get_node)):
    _own_key(node, user, key_id)
    if user["role"] != "admin" and "admin:*" in (body.get("scopes") or []):
        raise AuthError(403, "forbidden", "only admins can grant admin:*")
    k = node.keys.update(key_id, body)
    node.db.audit(user["username"], "key.update", key_id, body, ip=client_ip(request))
    return k


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, request: Request, user: dict = Depends(actor("operator")), node: Node = Depends(get_node)):
    _own_key(node, user, key_id)
    node.keys.revoke(key_id)
    node.db.audit(user["username"], "key.revoke", key_id, ip=client_ip(request))
    return {"id": key_id, "revoked": True}


# ---------------------------------------------------------------- users
class UserBody(BaseModel):
    username: str
    password: str
    email: str | None = None
    role: str = "viewer"


def _admins(node: Node) -> int:
    return int(node.db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0") or 0)


@router.get("/users")
async def users(user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    return {"data": node.users.list(), "roles": list(ROLES)}


@router.post("/users")
async def create_user(body: UserBody, request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    u = node.users.create(body.username, body.password, email=body.email, role=body.role, must_change_pw=True)
    node.db.audit(user["username"], "user.create", u["id"], {"username": body.username, "role": body.role}, ip=client_ip(request))
    return u


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, body: dict[str, Any], request: Request, user: dict = Depends(actor("admin")),
                     node: Node = Depends(get_node)):
    target = node.users.get(user_id)
    if not target:
        raise HTTPException(404, "user not found")
    demote = target["role"] == "admin" and (body.get("role", "admin") != "admin" or body.get("disabled"))
    if demote and _admins(node) <= 1:
        raise AuthError(400, "last_admin", "cannot demote or disable the last admin")
    u = node.users.update(user_id, body)
    node.db.audit(user["username"], "user.update", user_id, body, ip=client_ip(request))
    return u


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: str, request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    import secrets
    if not node.users.get(user_id):
        raise HTTPException(404, "user not found")
    pw = secrets.token_urlsafe(12)
    await asyncio.to_thread(node.users.set_password, user_id, pw, True)
    node.users.unlock(user_id)
    node.db.audit(user["username"], "user.reset_password", user_id, ip=client_ip(request))
    return {"password": pw, "must_change": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    target = node.users.get(user_id)
    if not target:
        raise HTTPException(404, "user not found")
    if user_id == user.get("id"):
        raise AuthError(400, "self_delete", "you cannot delete yourself")
    if target["role"] == "admin" and _admins(node) <= 1:
        raise AuthError(400, "last_admin", "cannot delete the last admin")
    node.users.delete(user_id)
    node.db.audit(user["username"], "user.delete", user_id, ip=client_ip(request))
    return {"id": user_id, "deleted": True}


# ---------------------------------------------------------------- models, profile, benchmark
_downloads: dict[str, dict] = {}
_bench: dict = {"status": "idle"}


def _manifest() -> Manifest:
    return Manifest.load()


@router.get("/models")
async def models(user: dict = Depends(actor()), node: Node = Depends(get_node)):
    man = _manifest()
    hw = await asyncio.to_thread(hardware.detect, str(models_root(node.cfg)))
    vram = hw.gpus[0].vram_gb if hw.gpus else 0
    profiles = {t: {"title": p["title"], "models": man.profile_models(t), "bytes": man.total_bytes(man.profile_models(t))}
                for t, p in man.profiles.items()}
    return {"models": installed(node.cfg, man), "profiles": profiles, "current": node.cfg.profile.model_dump(),
            "recommended": hardware.choose_tier(vram), "hardware": hw.to_dict(), "downloads": _downloads,
            "root": str(models_root(node.cfg))}


@router.post("/models/{model_id}/download")
async def download_model(model_id: str, request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    from ..models import download
    man = _manifest()
    if model_id not in man.models:
        raise HTTPException(404, "unknown model")
    if _downloads.get(model_id, {}).get("status") == "running":
        return _downloads[model_id]
    m = man.models[model_id]
    state = _downloads[model_id] = {"status": "running", "done": 0, "total": m.bytes, "error": None, "started_at": time.time()}

    def progress(done, total):
        state["done"], state["total"] = done, total
        node.events.admin({"type": "model.download", "data": {"id": model_id, **state}})

    async def run():
        try:
            await asyncio.to_thread(download, m, models_root(node.cfg), progress)
            state["status"] = "done"
        except Exception as e:
            state.update({"status": "failed", "error": str(e)})
        node.events.admin({"type": "model.download", "data": {"id": model_id, **state}})
    asyncio.create_task(run())
    node.db.audit(user["username"], "model.download", model_id, ip=client_ip(request))
    return state


class ProfileBody(BaseModel):
    tier: str
    model_ids: list[str] | None = None


@router.post("/models/profile")
async def set_profile(body: ProfileBody, request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    man = _manifest()
    if body.tier not in man.profiles:
        raise HTTPException(400, f"unknown tier {body.tier}")
    ids = body.model_ids or man.profile_models(body.tier)
    missing = [i for i in ids if i not in man.models]
    if missing:
        raise HTTPException(400, f"unknown models {missing}")
    not_present = [m["id"] for m in installed(node.cfg, man) if m["id"] in ids and not m["present"]]
    if not node.cfg.source:
        raise HTTPException(400, "this node runs without a config file; set the profile with qis setup")
    write_config(Path(node.cfg.source), profile_config(man, ids, body.tier))
    node.db.audit(user["username"], "profile.set", body.tier, {"models": ids}, ip=client_ip(request))
    return {"ok": True, "restart_required": True, "not_downloaded": not_present}


class BenchBody(BaseModel):
    images: int = Field(3, ge=1, le=8)
    apply: bool = True


@router.post("/benchmark")
async def benchmark(body: BenchBody, request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    if _bench.get("status") == "running":
        return _bench
    _bench.clear()
    _bench.update({"status": "running"})
    asyncio.create_task(_bench_task(node, body))
    node.db.audit(user["username"], "benchmark.start", ip=client_ip(request))
    return _bench


async def _bench_task(node, body):
    with contextlib.suppress(Exception):
        await run_internal(node, body.images, body.apply, _bench)


@router.get("/benchmark")
async def benchmark_state(user: dict = Depends(actor()), node: Node = Depends(get_node)):
    return _bench


# ---------------------------------------------------------------- system
@router.post("/system/restart-comfy")
async def restart_comfy(request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    await node.supervisor.restart()
    node.db.audit(user["username"], "comfy.restart", ip=client_ip(request))
    return node.supervisor.status()


@router.get("/system/logs")
async def logs(source: str = "gateway", lines: int = Query(300, ge=10, le=5000), level: str | None = None,
               user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    name = {"gateway": "gateway.log", "comfyui": "comfyui.log"}.get(source)
    if not name:
        raise HTTPException(400, "source must be gateway or comfyui")
    path = node.cfg.data_dir / "logs" / name
    if not path.exists():
        return {"lines": [], "path": str(path)}
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - lines * 400))
        text = f.read().decode("utf-8", errors="replace").splitlines()[-lines:]
    if level:
        text = [t for t in text if f" {level.upper()} " in t]
    return {"lines": text, "path": str(path)}


@router.post("/system/backup")
async def backup(request: Request, user: dict = Depends(actor("admin")), node: Node = Depends(get_node)):
    path = await asyncio.to_thread(node.backup)
    node.db.audit(user["username"], "db.backup", str(path), ip=client_ip(request))
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.get("/system/info")
async def system_info(user: dict = Depends(actor()), node: Node = Depends(get_node)):
    return {"version": __version__, "node_id": node.node_id, "name": node.name, "config": node.cfg.source,
            "data_dir": str(node.cfg.data_dir), "comfy": node.supervisor.status(), "uptime_s": round(time.time() - node.started_at)}


@router.get("/audit")
async def audit(limit: int = Query(200, ge=1, le=2000), actor_name: str | None = None, user: dict = Depends(actor("admin")),
                node: Node = Depends(get_node)):
    if actor_name:
        rows = node.db.all("SELECT * FROM audit WHERE actor=? ORDER BY ts DESC LIMIT ?", (actor_name, limit))
    else:
        rows = node.db.all("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,))
    return {"data": rows}

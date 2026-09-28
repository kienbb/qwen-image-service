"""M4: dashboard auth (sessions, CSRF, lockout, roles), live settings, keys, users, playground, admin API keys."""
from __future__ import annotations

import time

import httpx

from .test_api_core import wait_job


class Dash:
    """A browser-like client: cookie jar + X-CSRF-Token."""

    def __init__(self, gw):
        self.c = httpx.Client(base_url=gw.url, timeout=60)
        self.csrf = ""

    def login(self, login, password):
        r = self.c.post("/admin/api/auth/login", json={"login": login, "password": password})
        if r.status_code == 200:
            self.csrf = r.json()["csrf"]
        return r

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def send(self, method, path, **kw):
        return self.c.request(method, path, headers={"X-CSRF-Token": self.csrf}, **kw)


def admin_dash(gw):
    gw.node.users.create("admin", "correct horse battery", email="admin@example.com", role="admin")
    d = Dash(gw)
    assert d.login("admin", "correct horse battery").status_code == 200
    return d


def test_setup_first_admin_local_only(gw):
    c = httpx.Client(base_url=gw.url)
    assert c.get("/admin/api/auth/bootstrap").json()["needs_setup"] is True
    r = c.post("/admin/api/auth/setup", json={"username": "boss", "password": "long enough pw"},
               headers={"CF-Connecting-IP": "8.8.8.8"})
    assert r.status_code == 403
    r = c.post("/admin/api/auth/setup", json={"username": "boss", "password": "long enough pw"})
    assert r.status_code == 200 and r.cookies.get("qis_session")
    assert c.post("/admin/api/auth/setup", json={"username": "x", "password": "long enough pw"}).status_code == 409


def test_login_email_csrf_logout(gw):
    d = admin_dash(gw)
    assert d.get("/admin/api/status").status_code == 200
    # email login works too
    d2 = Dash(gw)
    assert d2.login("admin@example.com", "correct horse battery").status_code == 200
    # writes need the CSRF header
    r = d.c.put("/admin/api/settings/queue", json={"max_pending": 7})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    r = d.send("PUT", "/admin/api/settings/queue", json={"max_pending": 7}, )
    assert r.status_code == 200
    r = d.c.put("/admin/api/settings/queue", json={"max_pending": 8},
                headers={"X-CSRF-Token": d.csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403
    d.send("POST", "/admin/api/auth/logout")
    assert d.get("/admin/api/status").status_code == 401


def test_lockout_after_failures(gw):
    gw.node.users.create("bob", "bob password 1", role="viewer")
    d = Dash(gw)
    for _ in range(5):
        assert d.login("bob", "nope").status_code == 401
    r = d.login("bob", "bob password 1")
    assert r.status_code == 423


def test_live_settings_apply_to_admission(gw, fake):
    d = admin_dash(gw)
    fake.run_s = 1.0
    key = gw.key(max_pending=100)
    c = gw.client(key)
    assert d.send("PUT", "/admin/api/settings/queue", json={"max_pending": 1}).status_code == 200
    c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "a", "size": 256}]})
    c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "b", "size": 256}]})
    r = c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "c", "size": 256}]})
    assert r.status_code == 429
    assert d.send("PUT", "/admin/api/settings/queue", json={"max_pending": 20}).status_code == 200
    assert c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "d", "size": 256}]}).status_code == 202
    bad = d.send("PUT", "/admin/api/settings/queue", json={"max_pending": -1})
    assert bad.status_code == 400
    schema = d.get("/admin/api/settings").json()["schema"]
    assert "QueueS" in schema["$defs"]


def test_roles(gw):
    admin_dash(gw)
    gw.node.users.create("viewer1", "viewer password", role="viewer")
    gw.node.users.create("op1", "operator password", role="operator")
    v = Dash(gw)
    v.login("viewer1", "viewer password")
    assert v.get("/admin/api/status").status_code == 200
    assert v.send("PUT", "/admin/api/settings/queue", json={"max_pending": 3}).status_code == 403
    assert v.send("POST", "/admin/api/keys", json={"name": "x"}).status_code == 403
    o = Dash(gw)
    o.login("op1", "operator password")
    assert o.send("POST", "/admin/api/keys", json={"name": "mine"}).status_code == 200
    assert o.send("POST", "/admin/api/keys", json={"name": "adm", "scopes": ["admin:*"]}).status_code == 403
    assert o.get("/admin/api/users").status_code == 403


def test_keys_lifecycle(gw):
    d = admin_dash(gw)
    r = d.send("POST", "/admin/api/keys", json={"name": "unity", "scopes": ["images:generate", "jobs:read"], "rpm": 5})
    assert r.status_code == 200
    key, kid = r.json()["key"], r.json()["id"]
    c = gw.client(key)
    assert c.get("/v1/models").status_code == 200
    listed = d.get("/admin/api/keys").json()["data"]
    assert any(k["id"] == kid and "hash" not in k for k in listed)
    assert d.send("DELETE", f"/admin/api/keys/{kid}").status_code == 200
    assert c.get("/v1/models").status_code == 401


def test_users_last_admin_guard(gw):
    d = admin_dash(gw)
    me = d.get("/admin/api/auth/me").json()["user"]
    r = d.send("PATCH", f"/admin/api/users/{me['id']}", json={"role": "viewer"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "last_admin"
    r = d.send("POST", "/admin/api/users", json={"username": "newbie", "password": "a good password", "role": "operator"})
    uid = r.json()["id"]
    pw = d.send("POST", f"/admin/api/users/{uid}/reset-password").json()["password"]
    n = Dash(gw)
    assert n.login("newbie", pw).status_code == 200
    # must change the password before anything else
    assert n.get("/admin/api/status").status_code == 403
    r = n.send("POST", "/admin/api/auth/password", json={"current": pw, "new": "my own password"})
    assert r.status_code == 200
    n.csrf = r.json()["csrf"]
    assert n.get("/admin/api/status").status_code == 200


def test_playground_and_jobs_view(gw):
    d = admin_dash(gw)
    r = d.send("POST", "/admin/api/playground/jobs", json={"preset": "pixelart-icon-32", "prompt": "a shield"})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    for _ in range(100):
        j = d.get(f"/admin/api/jobs/{jid}").json()
        if j["status"] == "succeeded":
            break
        time.sleep(0.1)
    assert j["status"] == "succeeded" and j["events"]
    lst = d.get("/admin/api/jobs").json()
    assert lst["data"][0]["key_name"] == "dashboard"
    assert d.get("/admin/api/stats").json()["data"]


def test_admin_api_key_automation(gw):
    key = gw.key("ops", scopes=["admin:*"])
    c = gw.client(key)
    assert c.put("/admin/api/settings/queue", json={"max_pending": 9}).status_code == 200
    assert gw.node.settings.current.queue.max_pending == 9
    assert gw.client(gw.key("normal")).get("/admin/api/status").status_code == 403


def test_logs_audit_models(gw):
    d = admin_dash(gw)
    d.send("PUT", "/admin/api/settings/api", json={"sync_max_s": 60})
    audit = d.get("/admin/api/audit").json()["data"]
    assert any(a["action"] == "settings.update" for a in audit)
    assert d.get("/admin/api/system/logs?source=gateway").status_code == 200
    m = d.get("/admin/api/models").json()
    assert "T1" in m["profiles"] and m["models"]


def test_queue_pause_resume(gw):
    d = admin_dash(gw)
    c = gw.client(gw.key())
    assert d.send("POST", "/admin/api/queue/pause").json()["paused"] is True
    jid = c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "p", "size": 256}]}).json()["id"]
    time.sleep(0.5)
    assert c.get(f"/v1/jobs/{jid}").json()["status"] == "queued"
    d.send("POST", "/admin/api/queue/resume")
    assert wait_job(c, jid)["status"] == "succeeded"

"""Worker mode seen from the node's own HTTP API and CLI: 409 worker_mode, health / status blocks, drain / resume,
`qis worker join|leave`, config keys."""
from __future__ import annotations

import asyncio
import json
import time
import tomllib

import httpx
import pytest

from qis.app import create_app
from qis.auth import ApiKeys
from qis.cli import main as qis_main
from qis.config import Config, load_config
from qis.mesh.join import join, leave

from .fake_comfy import free_port

TURBO = "qwen-image-2.1-turbo"


@pytest.fixture
async def api(make_worker):
    """(node, admin client, user client) for a worker node that is connected to a hub."""
    async def make(hub=None, **worker):
        node = await make_worker(**worker)
        keys = ApiKeys(node.db, node.secret)
        admin, _ = keys.create("admin-test", scopes=["admin:*"])
        user, _ = keys.create("user-test")
        app = create_app(node.cfg, node)

        def client(key):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"Authorization": f"Bearer {key}"})
        return node, client(admin), client(user), app
    return make


# ------------------------------------------------------------------ 409 worker_mode
async def test_job_routes_answer_409_worker_mode(hub, api):
    node, admin, user, _ = await api()
    await hub.wait_connected()
    for method, path in [("POST", "/v1/images/generations"), ("POST", "/v1/images/edits"), ("POST", "/v1/jobs"),
                         ("POST", "/v1/jobs/job_x/retry"), ("POST", "/v1/tools/pixelize"), ("POST", "/mcp")]:
        r = await user.request(method, path, json={"model": TURBO, "prompt": "x"})
        assert r.status_code == 409, path
        err = r.json()["error"]
        assert err["code"] == "worker_mode" and err["type"] == "conflict_error" and "hub" in err["message"]
    assert node.jobs.queue_stats()["queued"] == 0 and node.jobs.queue_stats()["running"] == 0
    # reading is still fine: models, job lookups, health
    assert (await user.get("/v1/models")).status_code == 200
    assert (await user.get("/v1/jobs")).status_code == 200
    assert (await user.delete("/v1/jobs/job_nope")).status_code == 404
    assert (await user.get("/healthz")).status_code == 200


async def test_accept_local_jobs_keeps_the_routes_open(hub, api):
    node, admin, user, _ = await api(accept_local_jobs=True)
    r = await user.post("/v1/images/generations", json={"model": TURBO, "prompt": "a knight", "response_format": "url"},
                        timeout=30)
    assert r.status_code == 200, r.text


async def test_worker_mode_off_changes_nothing(hub, make_worker):
    node = await make_worker(enabled=False)
    assert node.worker is None and not node.worker_blocks_local_jobs
    app = create_app(node.cfg, node)
    key, _ = ApiKeys(node.db, node.secret).create("k", scopes=["admin:*"])
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"Authorization": f"Bearer {key}"})
    assert (await c.get("/healthz")).json()["worker"] == {"enabled": False}
    st = (await c.get("/admin/api/status")).json()
    assert st["worker"] == {"enabled": False, "link": "disabled"}
    r = await c.post("/admin/api/worker/drain")
    assert r.status_code == 409 and r.json()["error"]["code"] == "worker_mode_off"
    assert hub.sessions == []


# ------------------------------------------------------------------ status blocks
async def test_healthz_and_admin_status_report_the_hub_link(hub, api, fake):
    node, admin, user, _ = await api()
    await hub.wait_connected()
    h = (await user.get("/healthz")).json()
    assert h["worker"] == {"enabled": True, "link": "connected", "hub": f"127.0.0.1:{hub.port}"}
    st = (await admin.get("/admin/api/status")).json()["worker"]
    assert st["link"] == "connected" and st["hub_url"] == hub.ws_url and st["worker_id"] == node.node_id
    assert st["session_id"] and st["connected_since"] <= time.time() and st["last_ack_age_s"] < 5
    assert st["retry_in_s"] is None and st["running"] == [] and st["state"] == "ready"

    fake.slow_s = 2
    a = await hub.assign(request={"model": TURBO, "prompt": "SLOW"})
    await hub.expect("progress", attempt_id=a.attempt_id, where=lambda m: m.phase == "running")
    st = (await admin.get("/admin/api/status")).json()
    run = st["worker"]["running"]
    assert len(run) == 1 and run[0]["attempt_id"] == a.attempt_id and run[0]["job_id"] == a.job_id
    assert run[0]["phase"] == "running" and 0 <= run[0]["pct"] < 1
    assert [j["kind"] for j in st["running_jobs"]] == ["hub"]          # the tray's "Queue" shows hub jobs too
    await hub.expect("result", attempt_id=a.attempt_id, timeout=20)
    await asyncio.sleep(0.2)
    assert (await admin.get("/admin/api/worker")).json()["session_done"] == 1


async def test_drain_and_resume_endpoints(hub, api):
    node, admin, user, _ = await api()
    await hub.wait_connected()
    assert (await user.post("/admin/api/worker/drain")).status_code == 403       # needs an admin / operator
    r = await admin.post("/admin/api/worker/drain", params={"wait": "true", "timeout": 5})
    assert r.status_code == 200 and r.json()["link"] == "draining" and r.json()["draining"] is True
    assert (await hub.expect("bye")).reason == "drained"
    assert (await admin.get("/healthz")).json()["worker"]["link"] == "draining"
    r = await admin.post("/admin/api/worker/resume")
    assert r.status_code == 200 and r.json()["draining"] is False
    await hub.wait_connected(sessions=2)
    await hub.wait_for(lambda: node.worker.status()["link"] == "connected", what="connected")


async def test_leave_endpoint_says_bye_leave(hub, api):
    node, admin, _, _ = await api()
    await hub.wait_connected()
    r = await admin.post("/admin/api/worker/leave", params={"timeout": 5})
    assert r.status_code == 200
    assert (await hub.expect("bye")).reason == "leave"


# ------------------------------------------------------------------ config
def test_worker_config_defaults_and_token_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("QIS_JOIN_TOKEN", raising=False)
    cfg = Config()
    w = cfg.worker
    assert (w.enabled, w.hub_url, w.token_file, w.name, w.prefetch, w.cpu_slots, w.heartbeat_s, w.accept_local_jobs) == \
        (False, "wss://qis.runnow.cc/worker/ws", "worker.token", "", 1, 2, 10, False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text('[worker]\nenabled = true\ntoken_file = "t.txt"\n', encoding="utf-8")
    cfg = load_config(tmp_path / "config" / "config.toml")
    assert cfg.worker.enabled and cfg.worker_token() == ""
    (tmp_path / "config" / "t.txt").write_text("  qisw_fromfile\n", encoding="utf-8")
    assert cfg.worker_token() == "qisw_fromfile"
    monkeypatch.setenv("QIS_JOIN_TOKEN", "qisw_fromenv")
    assert cfg.worker_token() == "qisw_fromenv"
    cfg.worker.token = "qisw_inline"
    assert cfg.worker_token() == "qisw_inline"


def test_cli_join_and_leave_write_the_config(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("QIS_JOIN_TOKEN", raising=False)
    cfg_path = tmp_path / "config" / "config.toml"
    cfg_path.parent.mkdir()
    cfg_path.write_text(f'[server]\nport = {free_port()}\n[comfy]\nport = 8999\n', encoding="utf-8")
    qis_main(["worker", "join", "https://hub.example.test", "--token", "qisw_abc123", "--name", "BENCH-01",
              "--config", str(cfg_path), "--no-restart"])
    out = capsys.readouterr().out
    assert "wss://hub.example.test/worker/ws" in out and "not restarting" in out
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["worker"] == {"enabled": True, "hub_url": "wss://hub.example.test/worker/ws", "token_file": "worker.token",
                              "name": "BENCH-01"}
    assert data["comfy"]["port"] == 8999 and "port" in data["server"]            # the rest of the config is kept
    assert (cfg_path.parent / "worker.token").read_text(encoding="utf-8").strip() == "qisw_abc123"
    cfg = load_config(cfg_path)
    assert cfg.worker.enabled and cfg.worker_token() == "qisw_abc123"

    # leave: the gateway is not running here, so it just turns worker mode off (and says so)
    qis_main(["worker", "leave", "--config", str(cfg_path), "--no-restart", "--timeout", "1"])
    out = capsys.readouterr().out
    assert "worker mode off" in out
    assert load_config(cfg_path).worker.enabled is False
    assert load_config(cfg_path).worker.hub_url == "wss://hub.example.test/worker/ws"      # settings survive for a re-join


def test_cli_join_needs_a_token_and_a_valid_url(tmp_path, monkeypatch):
    monkeypatch.delenv("QIS_JOIN_TOKEN", raising=False)
    cfg_path = tmp_path / "config" / "config.toml"
    with pytest.raises(SystemExit) as e:
        qis_main(["worker", "join", "https://hub.example.test", "--config", str(cfg_path), "--no-restart"])
    assert "token" in str(e.value)
    with pytest.raises(ValueError):
        join(cfg_path, "gopher://x", "qisw_t")
    join(cfg_path, "http://10.0.0.5:8101", "qisw_t")
    assert load_config(cfg_path).worker.hub_url == "ws://10.0.0.5:8101/worker/ws"
    leave(cfg_path)
    assert json.dumps(load_config(cfg_path).worker.enabled) == "false"


def test_cli_exposes_all_worker_commands(capsys):
    for cmd in ("join", "leave", "status", "drain", "resume", "run"):
        with pytest.raises(SystemExit) as e:
            qis_main(["worker", cmd, "--help"])
        assert e.value.code == 0
    capsys.readouterr()


def test_cli_run_turns_worker_mode_on_for_this_process_only(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config" / "config.toml"
    cfg_path.parent.mkdir()
    cfg_path.write_text(chr(10).join(["[worker]", "enabled = false"]), encoding="utf-8")
    seen = {}
    monkeypatch.setattr("qis.cli.cmd_serve", lambda a, cfg=None: seen.update(cfg=cfg, a=a))
    monkeypatch.delenv("QIS_HUB_URL", raising=False)
    qis_main(["worker", "run", "--config", str(cfg_path), "--hub", "ws://127.0.0.1:9/worker/ws", "--token", "qisw_dev",
              "--name", "dev-box", "--attach", "--port", "9123"])
    cfg = seen["cfg"]
    assert cfg.worker.enabled and cfg.worker.hub_url == "ws://127.0.0.1:9/worker/ws" and cfg.worker_token() == "qisw_dev"
    assert cfg.worker.name == "dev-box" and seen["a"].attach and seen["a"].port == 9123
    assert load_config(cfg_path).worker.enabled is False            # nothing was written to config.toml
    monkeypatch.setenv("QIS_HUB_URL", "ws://127.0.0.1:8/worker/ws")
    qis_main(["worker", "run", "--config", str(cfg_path)])
    assert seen["cfg"].worker.hub_url == "ws://127.0.0.1:8/worker/ws"

"""M3: GPU tools on the fake engine, pipeline validation, MCP, webhooks, retention, gen.py as an API client."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .fake_comfy import free_port
from .test_api_core import png, wait_job

ROOT = Path(__file__).resolve().parents[1]


def test_refine_outpaint_upscale_esrgan(gw, fake):
    c = gw.client(gw.key())
    fid = c.post("/v1/files", files={"file": ("a.png", png(256, 256), "image/png")}).json()["id"]
    r = c.post("/v1/jobs", json={"inputs": {"src": fid}, "ops": [
        {"op": "refine", "image": "$inputs.src", "prompt": "a room", "width": 512, "height": 512},
        {"op": "upscale", "image": "$prev", "method": "esrgan", "width": 300, "height": 200},
        {"op": "outpaint", "image": "$inputs.src", "width": 540, "height": 960, "prompt": "more sky"}],
        "outputs": ["$step[0]", "$step[1]", "$step[2]"]})
    job = wait_job(c, r.json()["id"])
    assert job["status"] == "succeeded", job
    kinds = [p["workflow"] for p in fake.prompts[-5:]]
    assert any("ViggleTurboSigmas" in json.dumps(w) and "VAEEncode" in json.dumps(w) for w in kinds)
    esr = [w for w in kinds if "UpscaleModelLoader" in json.dumps(w)]
    assert esr[0]["model"]["inputs"]["model_name"] in ("RealESRGAN_x2plus.pth", "RealESRGAN_x4plus.pth")
    out = job["result"]["outputs"]
    assert out[2]["images"][0]["width"] == 540 and out[2]["images"][0]["height"] == 960  # outpaint pastes back at final size


def test_pipeline_validation(gw):
    c = gw.client(gw.key())
    bad = [
        {"ops": [{"op": "pixelize"}]},                                   # $prev at step 0
        {"ops": [{"op": "generate", "prompt": "x"}, {"op": "pixelize", "image": "$step[5]"}]},
        {"ops": [{"op": "generate", "prompt": "$inputs.x"}]},             # text refs only $prev.text
        {"ops": [{"op": "nope"}]},
        {"ops": [{"op": "generate", "prompt": "x", "mode": "warp"}]},
        {"ops": [{"op": "generate", "prompt": "x", "steps": 3}]},         # turbo supports 4-8 (fails at run)
        {"ops": [{"op": "pixelize", "image": "file_01ARZ3NDEKTSV4RRFFQ69G5FAV"}]},  # unknown file
    ]
    for spec in bad[:5] + bad[6:]:
        r = c.post("/v1/jobs", json=spec)
        assert r.status_code in (400, 404), (spec, r.text)
    r = c.post("/v1/jobs", json=bad[5])
    assert wait_job(c, r.json()["id"])["status"] == "failed"


def test_preset_shortcut_job(gw):
    c = gw.client(gw.key())
    r = c.post("/v1/jobs", json={"preset": "pixelart-icon-32", "prompt": "a sword", "params": {"n": 2, "palette": "db32"}})
    assert r.status_code == 202, r.text
    job = wait_job(c, r.json()["id"])
    assert job["status"] == "succeeded" and len(job["result"]["outputs"][0]["images"]) == 2


def _rpc(c, method, params=None, id_=1, headers=None):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers=headers)


def test_mcp(gw):
    c = gw.client(gw.key())
    init = _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}).json()
    assert init["result"]["serverInfo"]["name"] == "qwen-image-service"
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = {t["name"] for t in _rpc(c, "tools/list").json()["result"]["tools"]}
    assert {"generate_image", "edit_image", "pixelize", "upscale", "get_job"} <= tools
    res = _rpc(c, "tools/call", {"name": "generate_image", "arguments": {"prompt": "a gem", "model": "pixelart-icon-32"}}).json()
    content = res["result"]["content"]
    assert content[0]["type"] == "text" and any(x["type"] == "image" for x in content)
    summary = json.loads(content[0]["text"])
    assert summary["images"][0]["width"] == 32
    # SSE transport with progress notifications
    with c.stream("POST", "/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                        "params": {"name": "generate_image", "arguments": {"prompt": "slow SLOW"},
                                                   "_meta": {"progressToken": "p1"}}},
                  headers={"Accept": "application/json, text/event-stream"}) as s:
        body = "".join(s.iter_text())
    assert "notifications/progress" in body and '"id": 7' in body
    err = _rpc(c, "tools/call", {"name": "generate_image", "arguments": {"prompt": "x", "model": "nope"}}).json()
    assert err["result"]["isError"] is True
    assert _rpc(c, "bogus").json()["error"]["code"] == -32601
    assert gw.client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_webhook_signed(gw):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.headers["X-Qis-Signature"], body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = gw.client(gw.key())
        r = c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "hook", "size": 256}],
                                     "webhook_url": f"http://127.0.0.1:{port}/hook"})
        wait_job(c, r.json()["id"])
        for _ in range(50):
            if got:
                break
            time.sleep(0.1)
        sig, body = got[0]
        expect = hmac.new(gw.node.secret, body, hashlib.sha256).hexdigest()
        assert sig == f"sha256={expect}" and json.loads(body)["type"] == "job.succeeded"
    finally:
        srv.shutdown()


def test_retention_cleanup(gw):
    c = gw.client(gw.key())
    fid = c.post("/v1/files", files={"file": ("a.png", png(), "image/png")}).json()["id"]
    gw.node.db.execute("UPDATE files SET expires_at=0 WHERE id=?", (fid,))
    res = gw.node.storage.cleanup()
    assert res["expired"] >= 1
    assert c.get(f"/v1/files/{fid}").status_code == 404


def test_gen_py_client(gw, tmp_path):
    key = gw.key("cli")
    env = {**os.environ, "QIS_URL": gw.url, "QIS_API_KEY": key}
    name = f"pytest_{int(time.time())}"
    r = subprocess.run([sys.executable, str(ROOT / "gen.py"), "a slime", "--grid", "32", "--count", "2", "--name", name],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    out = ROOT / "output" / name
    try:
        files = sorted(p.name for p in out.iterdir())
        assert files == ["preview_0.png", "preview_1.png", "raw_0.png", "raw_1.png", "sprite_32px_0.png", "sprite_32px_1.png"]
    finally:
        for p in out.glob("*"):
            p.unlink()
        out.rmdir()
    wf = tmp_path / "wf.json"
    subprocess.run([sys.executable, str(ROOT / "gen.py"), "--dump-workflow", str(wf)], check=True, timeout=60)
    assert "UnetLoaderGGUF" in wf.read_text()

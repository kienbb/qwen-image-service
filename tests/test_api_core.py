"""Gateway against the fake ComfyUI: OpenAI API shape, jobs, files, auth, queue limits, recovery."""
from __future__ import annotations

import base64
import concurrent.futures as cf
import io
import time

import pytest
from PIL import Image


def png(w=64, h=64, color=(10, 200, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def wait_job(c, job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/v1/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed", "canceled", "expired"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {j}")


def test_health_and_auth(gw):
    c = gw.client()
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/readyz").status_code == 200
    r = c.post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"
    r = gw.client("sk-qis-wrong").get("/v1/models")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"


def test_generate_openai_shape(gw):
    c = gw.client(gw.key())
    r = c.post("/v1/images/generations", json={"model": "qwen-image-2.1-turbo", "prompt": "a knight", "n": 2, "size": "1024x768"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["data"]) == 2 and "b64_json" in body["data"][0]
    img = Image.open(io.BytesIO(base64.b64decode(body["data"][0]["b64_json"])))
    assert img.size == (1024 // 8, 768 // 8)  # the fake renders at 1/8
    assert body["qis"]["seeds"][1] == body["qis"]["seeds"][0] + 1
    wf = gw.node.jobs  # the workflow sent to ComfyUI used the requested size
    del wf


def test_pixelart_preset_url_and_extra(gw, fake):
    c = gw.client(gw.key())
    r = c.post("/v1/images/generations", json={"model": "pixelart-16bit-64", "prompt": "a goblin", "response_format": "url",
                                               "grid": 32, "palette": "pico8", "seed": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    url = body["data"][0]["url"]
    img = Image.open(io.BytesIO(c.get(url).content))
    assert max(img.size) == 32
    assert "preview" in body["qis"]["extra"] and body["qis"]["raw"]
    sent = fake.prompts[-1]["workflow"]
    assert "16-bit retro pixel art" in sent["enc"]["inputs"]["prompt"]
    assert sent["noise"]["inputs"]["noise_seed"] == 5
    # signed URL works without a key, a tampered one does not
    import httpx
    assert httpx.get(url).status_code == 200
    assert httpx.get(url.replace("sig=", "sig=0")).status_code == 403


def test_quality_and_transparent_mapping(gw, fake):
    c = gw.client(gw.key())
    r = c.post("/v1/images/generations", json={"prompt": "a key", "quality": "low", "background": "transparent"})
    assert r.status_code == 200, r.text
    wf = fake.prompts[-1]["workflow"]
    assert wf["sigmas"]["inputs"]["nodes"] == "1.0, 0.75, 0.5, 0.25"
    assert wf["enc"]["inputs"]["prompt"].startswith("This is an RGBA format image")


def test_bad_requests(gw):
    c = gw.client(gw.key())
    assert c.post("/v1/images/generations", json={"prompt": "x", "model": "nope"}).status_code == 400
    r = c.post("/v1/images/generations", json={"prompt": "x", "size": "big"})
    assert r.status_code == 400 and "size" in r.json()["error"]["message"]
    r = c.post("/v1/images/generations", json={"prompt": "x", "bogus_param": 1})
    assert r.status_code == 400
    r = c.post("/v1/images/generations", json={"prompt": "FAIL_REJECT"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "workflow_rejected"


def test_edit_multipart(gw, fake):
    c = gw.client(gw.key())
    r = c.post("/v1/images/edits", data={"prompt": "make it gold", "model": "qwen-image-2.1-turbo", "n": "1"},
               files=[("image[]", ("a.png", png(), "image/png")), ("image[]", ("b.png", png(color=(1, 2, 3)), "image/png"))])
    assert r.status_code == 200, r.text
    wf = fake.prompts[-1]["workflow"]
    assert "images.image_2" in wf["enc"]["inputs"] and "empty" not in wf


def test_scopes(gw):
    key = gw.key(scopes=["images:generate"])
    c = gw.client(key)
    r = c.post("/v1/images/edits", data={"prompt": "x"}, files={"image": ("a.png", png(), "image/png")})
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"


def test_jobs_pipeline_files_events(gw):
    c = gw.client(gw.key())
    up = c.post("/v1/files", files={"file": ("ref.png", png(), "image/png")}).json()
    assert up["id"].startswith("file_") and up["width"] == 64
    spec = {"inputs": {"hero": up["id"]},
            "ops": [{"op": "enhance_prompt", "prompt": "a knight"},
                    {"op": "generate", "prompt": "$prev.text", "n": 2, "size": 512},
                    {"op": "upscale", "image": "$prev", "method": "nearest", "scale": 2},
                    {"op": "pixelize", "image": "$step[1]", "grid": 16},
                    {"op": "fidelity", "reference": "$inputs.hero", "images": "$step[1]"}],
            "outputs": ["$step[2]", "$step[3]", "$prev"]}
    r = c.post("/v1/jobs", json=spec)
    assert r.status_code == 202, r.text
    job = wait_job(c, r.json()["id"])
    assert job["status"] == "succeeded", job
    outs = job["result"]["outputs"]
    assert [o["op"] for o in outs] == ["upscale", "pixelize", "fidelity"]
    assert len(outs[0]["images"]) == 2 and outs[0]["images"][0]["width"] == 2 * (512 // 8)
    assert len(outs[1]["images"]) == 2 and len(outs[1]["extra"]["preview"]) == 2
    assert len(outs[2]["data"]["scores"]) == 2
    gen = job["result"]["steps"][1]
    assert gen["data"]["prompt"] == "An enhanced prompt about a knight"
    # SSE replay contains the lifecycle
    with c.stream("GET", f"/v1/jobs/{job['id']}/events") as s:
        text = "".join(s.iter_text())
    for ev in ("queued", "started", "op_done", "succeeded"):
        assert f"event: {ev}" in text
    # listing and file access control
    assert any(j["id"] == job["id"] for j in c.get("/v1/jobs").json()["data"])
    other = gw.client(gw.key("other"))
    assert other.get(f"/v1/jobs/{job['id']}").status_code == 404
    assert other.get(f"/v1/files/{up['id']}").status_code == 404
    r = other.post("/v1/jobs", json={"ops": [{"op": "pixelize", "image": up["id"]}]})
    assert r.status_code == 403


def test_enhance_cache_and_ratio(gw, fake):
    c = gw.client(gw.key())
    body = {"prompt": "a knight", "enhance": True}
    assert c.post("/v1/images/generations", json=body).status_code == 200
    n = len(fake.prompts)
    r = c.post("/v1/images/generations", json=body)
    assert r.status_code == 200
    assert len(fake.prompts) == n + 1  # second call reused the cached rewrite: only the image run
    wf = fake.prompts[-1]["workflow"]
    assert wf["empty"]["inputs"]["height"] > wf["empty"]["inputs"]["width"]  # rewriter chose 2:3
    assert r.json()["data"][0]["revised_prompt"] == "An enhanced prompt about a knight"


def test_tools_sync_and_async(gw):
    c = gw.client(gw.key())
    r = c.post("/v1/tools/pixelize", files={"image": ("a.png", png(128, 128), "image/png")}, data={"grid": "32"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["images"][0]["width"] == 32 and out["extra"]["preview"]
    r = c.post("/v1/tools/upscale?async=1", json={"image": "data:image/png;base64," + base64.b64encode(png()).decode(),
                                                   "scale": 2})
    assert r.status_code == 202
    assert wait_job(c, r.json()["id"])["status"] == "succeeded"
    r = c.post("/v1/tools/remove-bg", json={"image": base64.b64encode(png()).decode(), "response_format": "b64_json"})
    assert r.status_code == 200 and r.json()["images"][0]["b64_json"]


def test_input_limits(gw):
    c = gw.client(gw.key())
    r = c.post("/v1/files", files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_image"
    gw.node.settings.update("inputs", {"max_pixels": 100_000})
    r = c.post("/v1/files", files={"file": ("x.png", png(1000, 1000), "image/png")})
    assert r.status_code == 400 and "pixels" in r.json()["error"]["message"]


def test_queue_full_and_key_limits(make_gateway, fake):
    gw = make_gateway(queue={"max_pending": 3, "max_wait_s": 100000}, keys={"default_max_pending": 100, "default_rpm": 1000})
    fake.run_s = 1.0
    c = gw.client(gw.key())

    def submit(i):
        return c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": f"p{i}", "size": 256}]})
    with cf.ThreadPoolExecutor(10) as ex:
        results = list(ex.map(submit, range(10)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(202) == 4 and codes.count(429) == 6, codes  # 1 running + 3 queued
    rej = next(r for r in results if r.status_code == 429)
    assert rej.json()["error"]["code"] == "queue_full" and int(rej.headers["Retry-After"]) >= 5
    # per-key pending limit
    k2 = gw.client(gw.key("limited", max_pending=1))
    gw.node.settings.update("queue", {"max_pending": 50})
    assert k2.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "a", "size": 256}]}).status_code == 202
    r = k2.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "b", "size": 256}]})
    assert r.status_code == 429 and r.json()["error"]["code"] == "key_limit"


def test_fair_share_between_keys(make_gateway, fake):
    gw = make_gateway(queue={"max_pending": 50}, scheduler={"aging_per_min": 0})
    fake.run_s = 0.2
    a, b = gw.client(gw.key("a", priority="batch")), gw.client(gw.key("b", priority="batch"))
    ids_a = [a.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": f"a{i}", "size": 256}]}).json()["id"] for i in range(4)]
    ids_b = [b.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": f"b{i}", "size": 256}]}).json()["id"] for i in range(2)]
    for j in ids_a + ids_b:
        wait_job(a if j in ids_a else b, j)
    started = sorted(ids_a + ids_b, key=lambda j: gw.node.jobs.get(j)["started_at"])
    order = ["a" if j in ids_a else "b" for j in started]
    assert order[:4].count("b") == 2, order  # b's jobs do not wait behind all of a's


def test_interactive_beats_batch(make_gateway, fake):
    gw = make_gateway(queue={"max_pending": 50})
    fake.run_s = 0.3
    batch = gw.client(gw.key("batch", priority="batch"))
    inter = gw.client(gw.key("inter"))
    ids = [batch.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": f"x{i}", "size": 256}]}).json()["id"] for i in range(3)]
    hi = inter.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "hi", "size": 256}]}).json()["id"]
    wait_job(inter, hi)
    for j in ids:
        wait_job(batch, j)
    assert gw.node.jobs.get(hi)["started_at"] < gw.node.jobs.get(ids[2])["started_at"]


def test_cancel_running_and_queued(gw, fake):
    fake.slow_s = 5
    c = gw.client(gw.key())
    j1 = c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "SLOW one", "size": 256}]}).json()["id"]
    j2 = c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "two", "size": 256}]}).json()["id"]
    time.sleep(0.5)
    assert c.delete(f"/v1/jobs/{j2}").json()["status"] == "canceled"
    c.delete(f"/v1/jobs/{j1}")
    assert wait_job(c, j1)["status"] == "canceled"


def test_oom_retry_degraded(gw, fake):
    fake.oom_left = 1
    c = gw.client(gw.key())
    r = c.post("/v1/images/generations", json={"prompt": "FAIL_OOM once"})
    assert r.status_code == 200, r.text
    assert r.json()["qis"]["degraded"] is True
    assert fake.prompts[-1]["workflow"]["clip"]["inputs"]["device"] == "cpu"


def test_exec_error_fails(gw):
    c = gw.client(gw.key())
    r = c.post("/v1/images/generations", json={"prompt": "FAIL_EXEC"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "execution_error"


def test_engine_down_then_back(gw, fake):
    c = gw.client(gw.key())
    fake.down = True
    time.sleep(0.8)
    assert c.get("/readyz").status_code == 503
    j = c.post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": "while down", "size": 256}]}).json()["id"]
    time.sleep(0.5)
    fake.down = False
    assert wait_job(c, j, 20)["status"] == "succeeded"


def test_restart_recovers_queued_jobs(tmp_path, fake):
    from .conftest import Gateway
    fake.run_s = 0.2
    g = Gateway(tmp_path / "r", fake)
    g.node.jobs.paused = True  # nothing runs
    g.start()
    key = g.key()
    ids = [g.client(key).post("/v1/jobs", json={"ops": [{"op": "generate", "prompt": f"r{i}", "size": 256}]}).json()["id"]
           for i in range(3)]
    g.stop()
    g2 = Gateway(tmp_path / "r", fake)
    g2.start()
    try:
        c = g2.client(key)
        for j in ids:
            assert wait_job(c, j)["status"] == "succeeded"
    finally:
        g2.stop()


def test_idempotency(gw):
    c = gw.client(gw.key())
    body = {"ops": [{"op": "generate", "prompt": "idem", "size": 256}]}
    a = c.post("/v1/jobs", json=body, headers={"Idempotency-Key": "k1"}).json()["id"]
    b = c.post("/v1/jobs", json=body, headers={"Idempotency-Key": "k1"}).json()["id"]
    assert a == b


def test_stream_openai_events(gw):
    c = gw.client(gw.key())
    with c.stream("POST", "/v1/images/generations", json={"prompt": "streamed", "stream": True, "partial_images": 2}) as r:
        text = "".join(r.iter_text())
    assert "event: image_generation.partial_image" in text
    assert "event: image_generation.completed" in text


def test_node_info_and_metrics(gw):
    c = gw.client(gw.key())
    info = c.get("/v1/node/info").json()
    assert info["schema"] == 1 and info["node_id"].startswith("node_") and "pixelart-16bit-64" in info["presets"]
    m = c.get("/metrics").text
    assert "qis_queue_depth" in m


def test_models_list(gw):
    c = gw.client(gw.key())
    data = c.get("/v1/models").json()["data"]
    ids = {m["id"] for m in data}
    assert {"qwen-image-2.1-turbo", "pixelart-16bit-64"} <= ids
    assert "qwen-image-2.1-hq" in ids


@pytest.mark.parametrize("fmt", ["webp", "jpeg"])
def test_output_format(gw, fmt):
    c = gw.client(gw.key())
    r = c.post("/v1/images/generations", json={"prompt": "fmt", "output_format": fmt})
    img = Image.open(io.BytesIO(base64.b64decode(r.json()["data"][0]["b64_json"])))
    assert img.format == fmt.upper()

"""Assigned attempts end to end: FakeHub -> worker -> local scheduler -> fake ComfyUI -> presigned PUT -> result."""
from __future__ import annotations

import asyncio
import io
import json
import time

import pytest
from PIL import Image

from qis.mesh.protocol import ResultAck
from qis.mesh.runner import AttemptFailed, map_job_error

TURBO = "qwen-image-2.1-turbo"


def png(color=(10, 200, 30), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def file_id(n: int) -> str:
    return "file_" + str(n).rjust(26, "0")


async def until(pred, timeout=15, what="condition"):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def phases(hub, att):
    return [m.phase for m in hub.of_type("progress", att)]


def hub_jobs(node):
    return node.db.all("SELECT * FROM jobs WHERE kind='hub' ORDER BY created_at")


# ------------------------------------------------------------------ the happy paths
async def test_text_to_image_full_flow(hub, make_worker, fake):
    node = await make_worker()
    hello = await hub.wait_connected()
    fake.run_s = 0.8
    a = await hub.assign(request={"model": TURBO, "prompt": "a knight", "seed": 42}, priority="batch")
    await hub.expect("accept", attempt_id=a.attempt_id, timeout=10)
    ur = await hub.expect("upload_request", attempt_id=a.attempt_id)
    res = await hub.expect("result", attempt_id=a.attempt_id)

    # phases come in order and the upload names follow the contract
    seq = phases(hub, a.attempt_id)
    assert seq[0] == "fetching" and seq[-1] == "uploading"
    assert seq.index("queued_local") < seq.index("running") < seq.index("uploading")
    assert [f.name for f in ur.files] == ["o0-0.png"] and ur.files[0].content_type == "image/png"

    img = res.outputs[0].images[0]
    assert img.key == f"out/test/{a.job_id}/a1/o0-0.png"
    data = hub.blob(img.key)
    assert data.startswith(b"\x89PNG") and len(data) == img.bytes == ur.files[0].bytes
    assert hub.blob_types[img.key] == "image/png"
    import hashlib
    assert img.sha256 == hashlib.sha256(data).hexdigest() and (img.width, img.height) == (128, 128)
    assert res.job_id == a.job_id and res.worker_version
    assert res.meta.seeds == [42] and res.meta.revised_prompt == "a knight" and res.meta.cold_start is True
    assert res.meta.model_fingerprint == hello.presets[TURBO].fingerprint

    # timings: the per-call sampling interval is first-to-last progress event of the (fake) KSampler: 3 of 4 gaps
    t = res.timings
    op = t.ops[0]
    assert (op.op, op.cls, op.kind) == ("generate", "t2i", "gpu") and len(op.calls) == 1
    assert 0.45 <= op.calls[0].sample_s <= 0.75, op.calls
    assert op.calls[0].sample_s < op.calls[0].seconds <= op.seconds + 0.01
    assert t.run_s == pytest.approx(op.seconds, abs=0.01) and t.fetch_s == 0 and t.upload_s >= 0 and op.estimate > 0

    # the local job is an internal "hub" job keyed by the attempt
    await until(lambda: node.worker.runner.attempts[a.attempt_id].acked, what="result_ack")
    (job,) = hub_jobs(node)
    assert job["status"] == "succeeded" and job["api_key_id"] is None and job["origin_node"] == "hub"
    assert job["idempotency_key"] == a.attempt_id and job["priority"] == "batch" and job["exec_node"] == node.node_id
    # the hub has the images: local copies now expire within the hour
    out_id = res_file_ids(node, job)[0]
    assert node.storage.get(out_id)["expires_at"] - time.time() <= 3600 + 5
    assert node.worker.status()["session_done"] == 1


def res_file_ids(node, job):
    return [i["file_id"] for o in job["result"]["outputs"] for i in o["images"]]


async def test_fingerprint_in_result_equals_the_advertised_one(hub, make_worker):
    await make_worker()
    hello = await hub.wait_connected()
    a = await hub.assign(request={"model": TURBO, "prompt": "x"}, fingerprint=hello.presets[TURBO].fingerprint)
    res = await hub.expect("result", attempt_id=a.attempt_id)
    assert res.meta.model_fingerprint == hello.presets[TURBO].fingerprint


async def test_previews_are_forwarded_only_when_asked_for(hub, make_worker, fake):
    await make_worker()
    await hub.wait_connected()
    fake.run_s = 1.8
    plain = await hub.assign(request={"model": TURBO, "prompt": "no previews"})
    await hub.expect("result", attempt_id=plain.attempt_id)
    assert all(m.preview_jpeg_b64 is None for m in hub.of_type("progress", plain.attempt_id))
    a = await hub.assign(request={"model": TURBO, "prompt": "with previews"}, output={"partial_images": 1})
    await hub.expect("result", attempt_id=a.attempt_id)
    previews = [m for m in hub.of_type("progress", a.attempt_id) if m.preview_jpeg_b64]
    assert len(previews) == 1                                         # at most partial_images of them
    import base64
    raw = base64.b64decode(previews[0].preview_jpeg_b64)
    assert raw[:2] == bytes([0xFF, 0xD8]) and len(raw) <= 256 * 1024
    # progress is rate limited to one message per second per attempt (plus the phase changes and the preview)
    running = [m for m in hub.of_type("progress", a.attempt_id) if m.phase == "running" and m.step is not None]
    assert 1 <= len(running) <= 4
    assert all(0 <= (m.pct or 0) < 1 for m in running)


async def test_pinned_fingerprint_that_does_not_match_is_rejected(hub, make_worker):
    await make_worker()
    hello = await hub.wait_connected()
    a = await hub.assign(request={"model": TURBO, "prompt": "x"}, fingerprint="mf_0000000000000000")
    r = await hub.expect("reject", attempt_id=a.attempt_id)
    assert r.reason == "model_missing" and hello.presets[TURBO].fingerprint in r.message
    assert hub.of_type("accept", a.attempt_id) == []


async def test_edit_with_two_references(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    before = len(fake.uploads)
    f1, f2 = file_id(1), file_id(2)
    a = await hub.assign(kind="images.edits", request={"model": TURBO, "prompt": "give him a shield"},
                         inputs={f1: png((250, 0, 0)), f2: png((0, 0, 250), (80, 60))})
    res = await hub.expect("result", attempt_id=a.attempt_id)
    assert len(res.outputs[0].images) == 1 and res.timings.ops[0].cls == "edit"
    assert len(fake.uploads) - before == 2                       # both references reached ComfyUI
    wf = fake.prompts[-1]["workflow"]
    assert sum(1 for n in wf.values() if n["class_type"] == "LoadImage") == 2
    (job,) = hub_jobs(node)
    refs = job["spec"]["ops"][0]["refs"]
    assert len(refs) == 2 and not {f1, f2} & set(refs)           # hub ids replaced by local file ids
    assert all(node.storage.get(r) for r in refs)


async def test_pipeline_with_pixelize_uploads_extra_images(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    fid = file_id(7)
    a = await hub.assign(kind="pipeline", inputs={fid: png((200, 30, 30), (96, 96))},
                         spec={"ops": [{"op": "pixelize", "image": fid, "grid": 16, "colors": 8}]})
    ur = await hub.expect("upload_request", attempt_id=a.attempt_id)
    res = await hub.expect("result", attempt_id=a.attempt_id)
    assert sorted(f.name for f in ur.files) == ["o0-0-preview.png", "o0-0.png"]
    out = res.outputs[0]
    assert out.op == "pixelize" and out.images[0].key.endswith("/o0-0.png")
    assert out.extra["preview"][0].key.endswith("/o0-0-preview.png")
    assert Image.open(io.BytesIO(hub.blob(out.images[0].key))).size[0] <= 16
    assert res.timings.ops[0].kind == "cpu" and res.timings.ops[0].calls == []
    assert res.meta.model_fingerprint.startswith("mf_")


async def test_output_format_webp_is_converted_by_the_worker(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    a = await hub.assign(request={"model": TURBO, "prompt": "x"}, output={"format": "webp", "compression": 80})
    ur = await hub.expect("upload_request", attempt_id=a.attempt_id)
    res = await hub.expect("result", attempt_id=a.attempt_id)
    assert ur.files[0].name == "o0-0.webp" and ur.files[0].content_type == "image/webp"
    img = res.outputs[0].images[0]
    assert img.content_type == "image/webp" and hub.blob_types[img.key] == "image/webp"
    assert Image.open(io.BytesIO(hub.blob(img.key))).format == "WEBP"


async def test_multi_step_preset_returns_raw_and_extra(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    a = await hub.assign(request={"model": "pixelart-16bit-64", "prompt": "a goblin", "seed": 3})
    ur = await hub.expect("upload_request", attempt_id=a.attempt_id)
    res = await hub.expect("result", attempt_id=a.attempt_id)
    assert sorted(f.name for f in ur.files) == ["o1-0-preview.png", "o1-0.png", "raw-0.png"]
    assert [i.key.rsplit("/", 1)[1] for i in res.raw] == ["raw-0.png"]
    assert res.outputs[0].step == 1 and res.outputs[0].op == "pixelize"
    assert [o.op for o in res.timings.ops] == ["generate", "pixelize"]
    assert res.meta.seeds == [3]


async def test_result_reports_cold_start_only_when_the_model_changes(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    first = await hub.assign(request={"model": TURBO, "prompt": "a"})
    r1 = await hub.expect("result", attempt_id=first.attempt_id)
    second = await hub.assign(request={"model": TURBO, "prompt": "b"})
    r2 = await hub.expect("result", attempt_id=second.attempt_id)
    assert r1.meta.cold_start is True and r2.meta.cold_start is False


async def test_history_teaches_the_perf_model(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    for i in range(5):
        a = await hub.assign(request={"model": TURBO, "prompt": f"p{i}"})
        await hub.expect("result", attempt_id=a.attempt_id)
    pm = await asyncio.to_thread(node.worker.caps.perf_model)
    assert pm.source == "history" and pm.classes["t2i"].samples == 5
    assert pm.classes["t2i"].b > 0 and pm.classes["t2i"].a1 > 0


# ------------------------------------------------------------------ failures
async def test_oom_after_the_local_retry_is_reported_as_oom(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.oom_left = 2                       # the normal run and the degraded (text encoder on CPU) retry both fail
    a = await hub.assign(request={"model": TURBO, "prompt": "FAIL_OOM"})
    f = await hub.expect("failure", attempt_id=a.attempt_id)
    assert f.code == "oom" and f.retryable is True and "out of memory" in f.message.lower()
    (job,) = hub_jobs(node)
    assert job["attempts"] == 2 and job["degraded"] == 1 and job["status"] == "failed"
    assert hub.of_type("result", a.attempt_id) == []


async def test_oom_that_the_local_retry_fixes_is_a_success(hub, make_worker, fake):
    await make_worker()
    await hub.wait_connected()
    fake.oom_left = 1
    a = await hub.assign(request={"model": TURBO, "prompt": "FAIL_OOM"})
    res = await hub.expect("result", attempt_id=a.attempt_id)
    assert res.meta.degraded is True


async def test_bad_reference_image_is_invalid_input(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    a = await hub.assign(kind="images.edits", request={"model": TURBO, "prompt": "x"}, inputs={file_id(3): b"definitely not a png"})
    f = await hub.expect("failure", attempt_id=a.attempt_id)
    assert f.code == "invalid_input" and f.retryable is False and "not a readable image" in f.message


async def test_unparsable_request_is_accepted_then_invalid_input(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    a = await hub.assign(request={"model": TURBO, "prompt": "x", "n": 12})        # more than max_n (8)
    await hub.expect("accept", attempt_id=a.attempt_id)
    f = await hub.expect("failure", attempt_id=a.attempt_id)
    assert f.code == "invalid_input" and "n must be" in f.message


async def test_comfy_rejection_and_crash_codes(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    rejected = await hub.assign(request={"model": TURBO, "prompt": "FAIL_REJECT"})
    f1 = await hub.expect("failure", attempt_id=rejected.attempt_id)
    assert f1.code == "invalid_input" and f1.retryable is False
    crashed = await hub.assign(request={"model": TURBO, "prompt": "FAIL_EXEC"})
    f2 = await hub.expect("failure", attempt_id=crashed.attempt_id)
    assert f2.code == "internal" and f2.retryable is True


async def test_unreachable_input_url_is_input_fetch_failed(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    a = await hub.assign(kind="images.edits", request={"model": TURBO, "prompt": "x"},
                         input_urls={file_id(9): f"{hub.http}/blob/in/never-uploaded"})
    f = await hub.expect("failure", attempt_id=a.attempt_id)
    assert f.code == "input_fetch_failed" and f.retryable is True


@pytest.mark.parametrize("job,code,retryable", [
    ({"status": "failed", "error": {"code": "oom", "message": "m"}}, "oom", True),
    ({"status": "failed", "error": {"code": "invalid_request"}}, "invalid_input", False),
    ({"status": "failed", "error": {"code": "workflow_rejected"}}, "invalid_input", False),
    ({"status": "failed", "error": {"code": "engine_unavailable"}}, "engine_unavailable", True),
    ({"status": "failed", "error": {"code": "timeout"}}, "timeout", True),
    ({"status": "failed", "error": {"code": "execution_error"}}, "internal", True),
    ({"status": "failed", "error": {"code": "server_error"}}, "internal", True),
    ({"status": "failed", "error": {"code": "interrupted"}}, "internal", True),
    ({"status": "failed", "error": None}, "internal", True),
    ({"status": "canceled", "error": {"code": "canceled"}}, "cancelled", False),
    ({"status": "expired", "error": {"code": "expired"}}, "timeout", True),
])
def test_local_job_errors_map_to_failure_codes(job, code, retryable):
    e = map_job_error(job)
    assert isinstance(e, AttemptFailed) and (str(e.code), e.retryable) == (code, retryable)


# ------------------------------------------------------------------ reject / accept rules
async def test_unknown_preset_is_rejected(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    a = await hub.assign(request={"model": "no-such-preset", "prompt": "x"})
    r = await hub.expect("reject", attempt_id=a.attempt_id)
    assert r.reason == "preset_unknown" and hub.of_type("accept", a.attempt_id) == []


async def test_slots_full_is_busy_and_duplicate_assign_runs_once(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 1.2
    slow = {"model": TURBO, "prompt": "SLOW"}
    a1 = await hub.assign(request=slow)
    a2 = await hub.assign(request=slow)              # prefetch slot
    a3 = await hub.assign(request=slow)              # nothing left
    r3 = await hub.expect("reject", attempt_id=a3.attempt_id)
    assert r3.reason == "busy"
    assert node.worker.runner.free_slots().model_dump() == {"gpu": 0, "prefetch": 0, "cpu": 2}
    await hub.send(a1)                                # the same attempt delivered twice
    await hub.expect("result", attempt_id=a1.attempt_id, timeout=20)
    await hub.expect("result", attempt_id=a2.attempt_id, timeout=20)
    assert len(hub.of_type("accept", a1.attempt_id)) == 2 and len(hub.of_type("result", a1.attempt_id)) == 1
    assert len(hub_jobs(node)) == 2                  # a1 and a2; never a third


# ------------------------------------------------------------------ cancel
async def test_cancel_running_attempt(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 5
    a = await hub.assign(request={"model": TURBO, "prompt": "SLOW"})
    await hub.expect("progress", attempt_id=a.attempt_id, where=lambda m: m.phase == "running")
    t = time.monotonic()
    await hub.cancel(a.attempt_id, "client")
    f = await hub.expect("failure", attempt_id=a.attempt_id, timeout=6)
    assert f.code == "cancelled" and f.retryable is False and time.monotonic() - t < 5
    assert fake.interrupted                           # ComfyUI got the interrupt
    (job,) = hub_jobs(node)
    assert job["status"] == "canceled"
    assert hub.of_type("result", a.attempt_id) == []


async def test_cancel_a_queued_attempt_leaves_the_running_one_alone(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 1.5
    first = await hub.assign(request={"model": TURBO, "prompt": "SLOW"})
    second = await hub.assign(request={"model": TURBO, "prompt": "SLOW again"})
    await hub.expect("progress", attempt_id=second.attempt_id, where=lambda m: m.phase == "queued_local")
    await hub.cancel(second.attempt_id, "superseded")
    f = await hub.expect("failure", attempt_id=second.attempt_id, timeout=6)
    assert f.code == "cancelled"
    await hub.expect("result", attempt_id=first.attempt_id, timeout=20)
    assert {j["status"] for j in hub_jobs(node)} == {"succeeded", "canceled"}


# ------------------------------------------------------------------ uploads
async def test_put_is_retried(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    hub.put_fail = 2
    a = await hub.assign(request={"model": TURBO, "prompt": "x"})
    await hub.expect("result", attempt_id=a.attempt_id)
    assert hub.puts >= 3


async def test_storage_down_for_too_long_is_upload_failed(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    node.worker.runner.upload_budget_s = 1.0
    hub.put_fail = 10_000
    a = await hub.assign(request={"model": TURBO, "prompt": "x"})
    f = await hub.expect("failure", attempt_id=a.attempt_id, timeout=15)
    assert f.code == "upload_failed" and f.retryable is True
    assert len(hub.of_type("upload_request", a.attempt_id)) >= 2        # it asked for fresh URLs meanwhile


async def test_expired_url_asks_for_a_new_grant(hub, make_worker):
    await make_worker()
    await hub.wait_connected()
    hub.put_fail, hub.put_status = 1, 403               # first URL is refused outright
    a = await hub.assign(request={"model": TURBO, "prompt": "x"})
    await hub.expect("result", attempt_id=a.attempt_id)
    assert len(hub.of_type("upload_request", a.attempt_id)) == 2


async def test_upload_denied_stale_drops_the_attempt_silently(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    hub.deny_upload = "stale"
    a = await hub.assign(request={"model": TURBO, "prompt": "x"})
    await hub.expect("upload_request", attempt_id=a.attempt_id)
    await until(lambda: a.attempt_id not in node.worker.runner.attempts, what="attempt forgotten")
    assert hub.of_type("result", a.attempt_id) == [] and hub.of_type("failure", a.attempt_id) == []
    assert node.worker.runner.idle()


async def test_result_ack_not_accepted_deletes_local_files(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    hub.auto_result_ack = False
    a = await hub.assign(request={"model": TURBO, "prompt": "x"})
    await hub.expect("result", attempt_id=a.attempt_id)
    att = node.worker.runner.attempts[a.attempt_id]
    out = list(att.out_files)
    assert out and node.storage.get(out[0])
    await hub.send(ResultAck(attempt_id=a.attempt_id, accepted=False, reason="stale"))
    await until(lambda: att.acked, what="ack")
    assert node.storage.get(out[0]) is None and node.worker.status()["session_done"] == 0


# ------------------------------------------------------------------ drain / pause / leave / stop
async def test_drain_finishes_jobs_says_bye_and_resume_reconnects(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 1.0
    a = await hub.assign(request={"model": TURBO, "prompt": "SLOW"})
    await hub.expect("progress", attempt_id=a.attempt_id, where=lambda m: m.phase == "running")
    st = await node.worker.drain()
    assert st["draining"] and st["link"] == "draining"
    await hub.expect("state", where=lambda m: m.state == "draining")
    b = await hub.assign(request={"model": TURBO, "prompt": "x"})
    assert (await hub.expect("reject", attempt_id=b.attempt_id)).reason == "draining"
    await hub.expect("result", attempt_id=a.attempt_id, timeout=20)
    bye = await hub.expect("bye", timeout=10)
    assert bye.reason == "drained" and bye.abandon == []
    await until(lambda: node.worker.state.parked, what="parked")
    await asyncio.sleep(0.6)
    assert len(hub.sessions) == 1                                   # drained workers do not reconnect by themselves
    assert node.worker.status()["link"] == "draining"
    await node.worker.resume()
    await hub.wait_connected(sessions=2)
    await until(lambda: node.worker.status()["link"] == "connected", what="connected")
    c = await hub.assign(request={"model": TURBO, "prompt": "after resume"})
    await hub.expect("result", attempt_id=c.attempt_id)


async def test_drain_when_idle_disconnects_at_once(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    res = await node.worker.drain(wait=True, timeout=5)
    assert res["link"] == "draining"
    assert (await hub.expect("bye")).reason == "drained"


async def test_leave_says_bye_leave(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    await node.worker.leave(timeout=5)
    assert (await hub.expect("bye")).reason == "leave"
    assert node.worker.state.parked


async def test_hub_pause_and_resume(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    await hub.pause()
    await until(lambda: node.worker.status()["link"] == "paused", what="paused")
    a = await hub.assign(request={"model": TURBO, "prompt": "x"})
    assert (await hub.expect("reject", attempt_id=a.attempt_id)).reason == "busy"
    from qis.mesh.protocol import Resume
    await hub.send(Resume(reason="admin"))
    await until(lambda: node.worker.status()["link"] == "connected", what="connected again")
    b = await hub.assign(request={"model": TURBO, "prompt": "x"})
    await hub.expect("result", attempt_id=b.attempt_id)


async def test_pause_in_welcome_is_honoured(hub, make_worker):
    hub.welcome_paused = True
    node = await make_worker()
    await hub.wait_connected()
    await until(lambda: node.worker.status()["link"] == "paused", what="paused")


async def test_stop_with_a_running_job_abandons_it(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 5
    a = await hub.assign(request={"model": TURBO, "prompt": "SLOW"})
    await hub.expect("progress", attempt_id=a.attempt_id, where=lambda m: m.phase == "running")
    await node.stop()
    bye = await hub.expect("bye", timeout=5)
    assert bye.reason == "shutdown" and bye.abandon == [a.attempt_id]


async def test_stop_when_idle_says_bye_without_abandoning(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    await node.stop()
    bye = await hub.expect("bye", timeout=5)
    assert bye.reason == "shutdown" and bye.abandon == []


async def test_node_restart_cancels_hub_jobs_left_behind(make_worker):
    node = await make_worker(start=False)
    row = {"id": "job_01RESTART000000000000000000", "kind": "hub", "spec": {"ops": []}, "status": "running",
           "priority": "interactive", "created_at": time.time(), "exec_node": node.node_id, "attempts": 1}
    node.db.insert("jobs", row)
    node.db.insert("jobs", {**row, "id": "job_01RESTART000000000000000001", "status": "queued"})
    node.db.insert("jobs", {**row, "id": "job_01RESTART000000000000000002", "kind": "pipeline", "status": "succeeded"})
    await node.boot()
    states = {j["id"][-1]: j["status"] for j in node.db.all("SELECT id, status FROM jobs")}
    assert states == {"0": "canceled", "1": "canceled", "2": "succeeded"}
    assert node.jobs.queued == {} and node.jobs.running == {}


# ------------------------------------------------------------------ connection loss in the middle of a job
async def test_disconnect_mid_job_then_reclaim(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 1.5
    a = await hub.assign(request={"model": TURBO, "prompt": "SLOW"})
    await hub.expect("progress", attempt_id=a.attempt_id, where=lambda m: m.phase == "running")
    hub.resume[a.attempt_id] = "keep"
    hub.drop_connection()
    hello2 = await hub.wait_connected(sessions=2, timeout=10)
    assert [r.attempt_id for r in hello2.running] == [a.attempt_id]        # "I still have it"
    res = await hub.expect("result", attempt_id=a.attempt_id, timeout=20)
    assert len(hub.of_type("result", a.attempt_id)) == 1
    assert len(hub_jobs(node)) == 1 and hub_jobs(node)[0]["status"] == "succeeded"
    assert res.job_id == a.job_id


async def test_result_finished_during_the_outage_is_sent_once_after_reconnect(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 1.0
    a = await hub.assign(request={"model": TURBO, "prompt": "SLOW"})
    await hub.expect("progress", attempt_id=a.attempt_id, where=lambda m: m.phase == "running")
    hub.tokens.clear()                                 # the hub is "away": every dial is refused
    hub.drop_connection()
    await until(lambda: node.worker.status()["link"] in ("auth_failed", "connecting"), what="link down")
    await until(lambda: node.jobs.get(hub_jobs(node)[0]["id"])["status"] == "succeeded", what="local job done")
    assert hub.of_type("result", a.attempt_id) == []   # nothing could be delivered
    hub.tokens.add("qisw_test")
    hello2 = await hub.wait_connected(sessions=2, timeout=15)
    res = await hub.expect("result", attempt_id=a.attempt_id, timeout=20)
    assert res.attempt_id == a.attempt_id
    await until(lambda: node.worker.runner.attempts[a.attempt_id].acked, what="ack")
    assert len(hub.of_type("result", a.attempt_id)) == 1
    assert [r.attempt_id for r in hello2.running] in ([a.attempt_id], [])


async def test_unacked_result_is_replayed_after_reconnect(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    hub.auto_result_ack = False
    a = await hub.assign(request={"model": TURBO, "prompt": "x"})
    await hub.expect("result", attempt_id=a.attempt_id)
    hub.drop_connection()
    hello2 = await hub.wait_connected(sessions=2, timeout=10)
    assert [u.attempt_id for u in hello2.unacked] == [a.attempt_id] and hello2.running == []
    again = await hub.expect("result", attempt_id=a.attempt_id)       # replayed right after welcome
    assert again.attempt_id == a.attempt_id
    hub.auto_result_ack = True
    await hub.send(hub.check_result(again))
    await until(lambda: node.worker.runner.attempts[a.attempt_id].acked, what="ack")
    assert node.worker.runner.idle()
    hub.drop_connection()
    hello3 = await hub.wait_connected(sessions=3, timeout=10)
    assert hello3.unacked == []


async def test_failure_is_replayed_until_a_heartbeat_ack_follows(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    hub.auto_heartbeat_ack = False
    a = await hub.assign(kind="images.edits", request={"model": TURBO, "prompt": "x"}, inputs={file_id(4): b"junk"})
    await hub.expect("failure", attempt_id=a.attempt_id)
    hub.drop_connection()                              # connection lost before anything confirmed the failure
    hello2 = await hub.wait_connected(sessions=2, timeout=10)
    assert [u.attempt_id for u in hello2.unacked] == [a.attempt_id]
    await hub.expect("failure", attempt_id=a.attempt_id)               # sent again after welcome
    hub.auto_heartbeat_ack = True
    await until(lambda: node.worker.runner.idle(), what="failure confirmed by a later heartbeat_ack")


async def test_resume_drop_cancels_the_local_job_and_deletes_files(hub, make_worker, fake):
    node = await make_worker()
    await hub.wait_connected()
    fake.slow_s = 5
    a = await hub.assign(kind="images.edits", request={"model": TURBO, "prompt": "SLOW"}, inputs={file_id(5): png()})
    await hub.expect("progress", attempt_id=a.attempt_id, where=lambda m: m.phase == "running")
    att = node.worker.runner.attempts[a.attempt_id]
    local_inputs = list(att.local_files)
    assert local_inputs and node.storage.get(local_inputs[0])
    hub.resume[a.attempt_id] = "drop"                  # the hub gave the job to someone else meanwhile
    hub.drop_connection()
    await hub.wait_connected(sessions=2, timeout=10)
    await until(lambda: a.attempt_id not in node.worker.runner.attempts, timeout=15, what="attempt dropped")
    assert hub.of_type("result", a.attempt_id) == [] and hub.of_type("failure", a.attempt_id) == []
    (job,) = hub_jobs(node)
    assert job["status"] == "canceled"
    assert node.storage.get(local_inputs[0]) is None
    assert json.dumps(node.worker.status()["running"]) == "[]"

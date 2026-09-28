"""Benchmark: measure seconds/step on this GPU and derive the queue defaults (DESIGN §12.2 step 10).

Runs through the node's own queue so it measures exactly what clients get. Also usable from the CLI against a
running gateway (`qis benchmark --key <admin key>`), which calls POST /admin/api/benchmark.
"""
from __future__ import annotations

import logging
import time

from .engine.constants import MODES
from .pipeline import JobSpec

log = logging.getLogger("qis.bench")


async def _run(node, spec: dict, state: dict, label: str) -> dict:
    state["step"] = label
    job = node.jobs.submit(JobSpec.model_validate(spec), key=None, kind="benchmark")
    done = await node.jobs.wait(job["id"], 3600)
    if not done or done["status"] != "succeeded":
        raise RuntimeError(f"{label}: job {job['id']} {done and done['status']}: {(done or {}).get('error')}")
    return done["result"]


async def run_internal(node, images: int = 3, apply: bool = True, state: dict | None = None) -> dict:
    state = state if state is not None else {}
    state.update({"status": "running", "started_at": time.time(), "result": None, "error": None})
    try:
        models = node.cfg.models.model_set()
        perf = node.settings.current.perf
        size = 768  # the reference resolution of perf.ref_mp
        res: dict = {"s_per_step": {}, "per_image_s": {}}
        if "turbo" in models.unets:
            await _run(node, {"ops": [{"op": "generate", "prompt": "benchmark warm up", "mode": "turbo", "size": 256,
                                       "steps": 4, "seed": 1}]}, state, "warm-up")
            r = await _run(node, {"ops": [{"op": "generate", "prompt": "a knight with a blue cape, game asset",
                                           "mode": "turbo", "size": size, "n": images, "seed": 7}]}, state, f"turbo x{images}")
            per = r["timings"][0]["seconds"] / images
            steps = MODES["turbo"]["steps"]
            res["per_image_s"]["turbo"] = round(per, 2)
            res["s_per_step"]["turbo"] = round(max(per - perf.overhead_s, 0.5) / steps, 3)
        if "base" in models.unets:
            r = await _run(node, {"ops": [{"op": "generate", "prompt": "a knight with a blue cape, game asset",
                                           "mode": "base", "size": size, "n": 1, "seed": 7}]}, state, "base x1")
            per = r["timings"][0]["seconds"]
            res["per_image_s"]["base"] = round(per, 2)
            res["s_per_step"]["base"] = round(max(per - perf.overhead_s, 0.5) / MODES["base"]["steps"], 3)
        avg = res["per_image_s"].get("turbo") or res["per_image_s"].get("base") or perf.avg_job_s
        q = node.settings.current.queue
        res["avg_job_s"] = avg
        res["max_pending"] = max(4, min(200, round(q.target_wait_s / max(avg, 1))))
        res["gpu"] = node.supervisor.gpu().get("name")
        if apply:
            sps = {**perf.s_per_step, **res["s_per_step"]}
            if "base" in res["s_per_step"] and "base_q8" not in res["s_per_step"]:
                sps["base_q8"] = round(res["s_per_step"]["base"] * 1.4, 3)
            calib = {k: v for k, v in perf.calib.items() if k != "generate"}
            node.settings.update("perf", {"s_per_step": sps, "avg_job_s": avg, "calib": calib}, actor="benchmark")
            node.settings.update("queue", {"max_pending": res["max_pending"]}, actor="benchmark")
            node.db.audit("benchmark", "benchmark.apply", "", res)
        state.update({"status": "done", "result": res, "finished_at": time.time(), "step": ""})
        log.info("benchmark: %s", res)
        return res
    except Exception as e:
        state.update({"status": "failed", "error": str(e), "finished_at": time.time()})
        log.exception("benchmark failed")
        raise


def run_benchmark(cfg, url: str | None, key: str | None, images: int, apply: bool) -> None:
    """CLI: trigger the benchmark on a running gateway and wait for the result."""
    import json
    import os
    import sys

    import httpx
    key = key or os.environ.get("QIS_API_KEY")
    if not key:
        sys.exit("needs an API key with the admin:* scope (--key or QIS_API_KEY)")
    base = (url or f"http://127.0.0.1:{cfg.server.port}").rstrip("/")
    h = {"Authorization": f"Bearer {key}"}
    r = httpx.post(f"{base}/admin/api/benchmark", json={"images": images, "apply": apply}, headers=h, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"benchmark request failed: {r.status_code} {r.text}")
    last = ""
    while True:
        time.sleep(3)
        st = httpx.get(f"{base}/admin/api/benchmark", headers=h, timeout=30).json()
        line = f"{st.get('status')} {st.get('step') or ''}"
        if line != last:
            print(line, file=sys.stderr)
            last = line
        if st.get("status") in ("done", "failed"):
            break
    if st["status"] == "failed":
        sys.exit(f"benchmark failed: {st.get('error')}")
    print(json.dumps(st["result"], indent=1))

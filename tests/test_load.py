"""Load: many concurrent clients against one node. Every request gets a clean answer (202 or 429 + Retry-After),
every accepted job finishes, and the queue drains back to zero."""
from __future__ import annotations

import concurrent.futures as cf
import time

from .test_api_core import wait_job


def test_hundred_concurrent_clients(make_gateway, fake):
    gw = make_gateway(queue={"max_pending": 40, "max_wait_s": 100000},
                      keys={"default_max_pending": 1000, "default_rpm": 100000})
    fake.run_s = 0.05
    keys = [gw.key(f"k{i}") for i in range(5)]
    clients = [gw.client(k) for k in keys]

    def submit(i):
        c = clients[i % len(clients)]
        body = {"ops": [{"op": "generate", "prompt": f"load {i}", "size": 256}, {"op": "pixelize", "image": "$prev", "grid": 16}]}
        return i % len(clients), c.post("/v1/jobs", json=body)

    t = time.time()
    with cf.ThreadPoolExecutor(32) as ex:
        results = list(ex.map(submit, range(100)))
    codes = [r.status_code for _, r in results]
    assert set(codes) <= {202, 429}, codes
    assert all("Retry-After" in r.headers for _, r in results if r.status_code == 429)
    accepted = [(k, r.json()["id"]) for k, r in results if r.status_code == 202]
    assert len(accepted) >= 41
    for k, jid in accepted:
        assert wait_job(clients[k], jid, 120)["status"] == "succeeded"
    q = gw.node.jobs.queue_stats()
    assert q["queued"] == 0 and q["running"] == 0
    print(f"{len(accepted)} accepted, {codes.count(429)} rejected, drained in {time.time() - t:.1f}s")

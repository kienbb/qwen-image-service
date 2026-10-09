"""Worker side of the hub link against FakeHub: auth, hello, heartbeats, close codes, reconnects, warm-up state."""
from __future__ import annotations

import asyncio
import re
import time

import pytest

from qis.mesh.link import Timing
from qis.mesh.protocol import PROTO, Close, parse_message
from qis.mesh.state import WorkerState, hub_host, hub_http_url, normalize_hub_url


async def link_is(node, want: str, timeout: float = 10):
    end = time.monotonic() + timeout
    while node.worker.state.link() != want:
        if time.monotonic() > end:
            raise AssertionError(f"link is {node.worker.state.link()!r}, wanted {want!r}: {node.worker.status()}")
        await asyncio.sleep(0.05)


# ------------------------------------------------------------------ hello
async def test_hello_matches_schema_and_carries_capabilities(hub, make_worker):
    node = await make_worker(name="PIXON-TEST-01")
    hello = await hub.wait_connected()
    # the upgrade request authenticates with the join token and names the worker
    assert hub.upgrade_headers["authorization"] == "Bearer qisw_test"
    assert hub.upgrade_headers["x-qis-worker-id"] == node.node_id
    assert parse_message(hello.to_wire()).to_wire() == hello.to_wire()
    assert (hello.worker_id, hello.name, hello.proto, hello.state) == (node.node_id, "PIXON-TEST-01", PROTO, "ready")
    assert hello.version and hello.gpu.name and hello.tier == node.cfg.profile.tier
    assert hello.running == [] and hello.unacked == []
    assert hello.slots.model_dump() == {"gpu": 1, "prefetch": 1, "cpu": 2}
    assert hello.limits.max_refs == 3 and hello.limits.max_input_bytes == 20 * 1024 * 1024
    assert set(hello.ops) >= {"generate", "refine", "upscale", "pixelize", "compose", "fidelity"}
    assert set(hello.modes) == {"turbo", "base"} and set(hello.upscalers) == {"realesrgan-x2", "realesrgan-x4"}
    # presets are the ones this node can run, with the definition from presets.yaml and a fingerprint
    assert "qwen-image-2.1-turbo" in hello.presets and "pixelart-16bit-64" in hello.presets
    turbo = hello.presets["qwen-image-2.1-turbo"]
    assert re.fullmatch(r"mf_[0-9a-f]{16}", turbo.fingerprint) and turbo.ops == [{"op": "generate", "mode": "turbo", "size": 1024}]
    # the time model: nothing learnt yet, so the calibration fallback is what the hub starts from
    assert hello.perf_model.source == "calibration" and hello.perf_model.classes == {}
    assert hello.perf_model.fallback["s_per_step"]["turbo"] > 0 and "overhead_s" in hello.perf_model.fallback


async def test_heartbeat_content(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    hb = await hub.expect("heartbeat")
    assert hb.state == "ready" and hb.comfy == "ready" and hb.running == []
    assert hb.free_slots.model_dump() == {"gpu": 1, "prefetch": 1, "cpu": 2}
    assert hb.gpu is not None
    await hub.expect("heartbeat")      # and they keep coming at the interval the hub chose in `welcome`
    assert node.worker.status()["last_ack_age_s"] < 2


# ------------------------------------------------------------------ authentication and close codes
async def test_wrong_join_token_is_auth_failed_then_recovers(hub, make_worker):
    node = await make_worker(token="qisw_wrong")
    await link_is(node, "auth_failed")
    st = node.worker.status()
    assert st["link"] == "auth_failed" and "token" in st["last_error"] and st["last_close_code"] == Close.AUTH
    assert node.worker.health() == {"enabled": True, "link": "auth_failed", "hub": f"127.0.0.1:{hub.port}"}
    assert hub.sessions == []
    # a corrected hub / new token: the worker keeps trying (every auth_retry_s) and clears the state on welcome
    hub.tokens.add("qisw_wrong")
    await hub.wait_connected()
    await link_is(node, "connected")
    assert node.worker.state.sticky == ""


async def test_no_token_configured_never_dials(hub, make_worker, monkeypatch):
    monkeypatch.delenv("QIS_JOIN_TOKEN", raising=False)
    node = await make_worker(token="")
    assert node.worker.status()["link"] == "auth_failed" and "qis worker join" in node.worker.status()["last_error"]
    await asyncio.sleep(0.4)
    assert hub.sessions == []


async def test_outdated_protocol_then_upgrade(hub, make_worker):
    hub.proto_range = (2, 3)
    node = await make_worker()
    await link_is(node, "outdated")
    st = node.worker.status()
    assert st["last_close_code"] == Close.PROTOCOL and "qis update" in st["last_error"]
    hub.proto_range = (1, 1)
    await hub.wait_connected()
    await link_is(node, "connected")


@pytest.mark.parametrize("code", [Close.NORMAL, Close.GOING_AWAY, Close.RESTART])
async def test_hub_restart_reconnects_within_three_seconds(hub, make_worker, code):
    node = await make_worker(timing=Timing(heartbeat_s=0.3))      # default 1-3 s jitter after a clean close
    await hub.wait_connected()
    t = time.monotonic()
    await hub.close(code)
    await hub.wait_connected(sessions=2, timeout=5)
    assert time.monotonic() - t <= 3.5
    await link_is(node, "connected")


async def test_replaced_session_waits_then_comes_back(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    await hub.close(Close.REPLACED)
    await asyncio.sleep(0.15)
    st = node.worker.status()
    assert st["replaced_count"] == 1 and "worker_id" in st["last_error"] and st["retry_in_s"] is not None
    assert len(hub.sessions) == 1            # still waiting out replaced_wait_s
    await hub.wait_connected(sessions=2)


async def test_silent_hub_triggers_reconnect(hub, make_worker):
    hub.auto_heartbeat_ack = False
    node = await make_worker(timing=Timing(heartbeat_s=0.2, rx_timeout_s=0.8, restart_delay=(0.05, 0.1), backoff_cap_s=0.2))
    await hub.wait_connected()
    await hub.wait_connected(sessions=2, timeout=6)
    assert "no frame from the hub" in node.worker.status()["last_error"]


async def test_abrupt_disconnect_backs_off_and_reconnects(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    hub.drop_connection()
    await hub.wait_connected(sessions=2, timeout=8)
    await link_is(node, "connected")


async def test_unreachable_hub_is_connecting_then_disconnected(hub, make_worker, monkeypatch):
    await hub.stop()
    node = await make_worker()
    for _ in range(100):      # Windows takes ~2 s to report a refused connection
        if node.worker.state.last_error:
            break
        await asyncio.sleep(0.1)
    st = node.worker.status()
    assert st["link"] == "connecting" and "cannot reach the hub" in st["last_error"] and st["connected_since"] is None
    # "disconnected" is time based (60 s without a session): shrink the clock instead of waiting
    monkeypatch.setattr("qis.mesh.state.DISCONNECTED_AFTER_S", 0.2)
    assert node.worker.status()["link"] == "disconnected"


# ------------------------------------------------------------------ warm-up
async def test_warming_state_is_reported_and_blocks_jobs(hub, make_worker):
    node = await make_worker()
    hello = await hub.wait_connected()
    assert hello.state == "ready"
    node.supervisor.warm = False                       # ComfyUI restarted: model warm-up running
    st = await hub.expect("state", where=lambda m: m.state == "warming")
    assert st.reason == "engine"
    a = await hub.assign(request={"model": "qwen-image-2.1-turbo", "prompt": "x"})
    rej = await hub.expect("reject", attempt_id=a.attempt_id)
    assert rej.reason == "busy" and "warm" in rej.message
    node.supervisor.warm = True
    await hub.expect("state", where=lambda m: m.state == "ready")
    a2 = await hub.assign(request={"model": "qwen-image-2.1-turbo", "prompt": "x"})
    await hub.expect("accept", attempt_id=a2.attempt_id)
    await hub.expect("result", attempt_id=a2.attempt_id)


async def test_hello_after_reconnect_reports_current_state(hub, make_worker):
    node = await make_worker()
    await hub.wait_connected()
    node.supervisor.warm = False
    await hub.close(Close.NORMAL)
    hello2 = await hub.wait_connected(sessions=2)
    assert hello2.state == "warming" and hello2.warm_eta_s is None


# ------------------------------------------------------------------ pure pieces
def test_hub_url_normalisation():
    assert normalize_hub_url("https://qis.runnow.cc") == "wss://qis.runnow.cc/worker/ws"
    assert normalize_hub_url("https://qis.runnow.cc/") == "wss://qis.runnow.cc/worker/ws"
    assert normalize_hub_url("http://10.0.0.5:8101") == "ws://10.0.0.5:8101/worker/ws"
    assert normalize_hub_url("wss://qis.runnow.cc/worker/ws") == "wss://qis.runnow.cc/worker/ws"
    assert normalize_hub_url("ws://localhost:9/custom") == "ws://localhost:9/custom"
    assert normalize_hub_url("qis.runnow.cc") == "wss://qis.runnow.cc/worker/ws"
    with pytest.raises(ValueError):
        normalize_hub_url("ftp://x")
    assert hub_http_url("wss://qis.runnow.cc/worker/ws") == "https://qis.runnow.cc"
    assert hub_http_url("ws://10.0.0.5:8101/worker/ws") == "http://10.0.0.5:8101"
    assert hub_host("wss://qis.runnow.cc/worker/ws") == "qis.runnow.cc"


def test_link_state_derivation_covers_every_state():
    s = WorkerState(enabled=False)
    assert s.link(100) == "disabled"
    s = WorkerState(enabled=True, started_mono=0.0)
    assert s.link(1) == "connecting"                              # dialing, never connected
    assert s.link(61) == "disconnected"                           # no session for > 60 s
    s.connected, s.last_rx_mono, s.last_ack_mono = True, 100.0, 100.0
    assert s.link(101) == "connected"
    assert s.link(131) == "connecting"                            # connected but the hub went quiet > 30 s
    s.last_ack_mono = 130.0
    s.paused = True
    assert s.link(131) == "paused"
    s.draining = True
    assert s.link(131) == "draining"                              # draining wins over paused
    s.sticky = "auth_failed"
    assert s.link(131) == "auth_failed"                           # a sticky error wins over everything
    s.sticky = "outdated"
    assert s.link(131) == "outdated"
    s.sticky, s.connected, s.parked = "", False, True             # drained and disconnected on purpose
    assert s.link(500) == "draining"
    s.parked, s.draining, s.disconnected_mono = False, False, 140.0
    assert s.link(150) == "connecting" and s.link(201) == "disconnected"
    snap = s.snapshot()
    assert snap["link"] in ("connecting", "disconnected") and snap["running"] == []

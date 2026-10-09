"""Tray view model in worker mode (docs/MESH.md 8.3): one distinct colour and headline per hub connection state,
tooltip with hub / current job / last error, and the engine states winning over the link. Pure functions, no display."""
from __future__ import annotations

import pytest

from qis.tray import HUB_ROWS, TOOLTIP_MAX, WORKER_COLOURS, Snapshot, view_model, worker_view

NOW = 1_000_000.0
RUNNING = [{"attempt_id": "att_01ABC", "job_id": "job_01XYZ", "phase": "running", "pct": 0.62}]


def worker_block(link="connected", **kw) -> dict:
    base = {"enabled": True, "link": link, "hub_url": "wss://qis.runnow.cc/worker/ws", "hub": "qis.runnow.cc",
            "worker_id": "node_01TEST", "name": "PIXON-3060-01", "state": "ready", "session_id": "ses_1",
            "connected_since": NOW - 3725, "last_ack_age_s": 4.2, "retry_in_s": None, "last_error": "", "running": [],
            "unacked": 0, "draining": False, "paused": False, "session_done": 12, "session_failed": 1}
    return {**base, **kw}


def snap(worker=None, *, health_only=False, **kw) -> Snapshot:
    status = {"name": "node-a", "version": "0.1.0", "uptime_s": 100, "queue": {"queued": 0, "running": 0},
              "running_jobs": [], "worker": worker}
    health = {"status": "ok", "version": "0.1.0", "worker": ({"enabled": worker["enabled"], "link": worker.get("link"),
                                                              "hub": worker.get("hub")} if worker else {"enabled": False})}
    base = {"service": "RUNNING", "health": health, "ready": {"ready": True, "comfy": "ready", "warm": True},
            "status": None if health_only else status, "ts": NOW}
    return Snapshot(**{**base, **kw})


CASES = {   # link -> (worker block overrides, headline)
    "connected": ({}, "Worker: connected to qis.runnow.cc"),
    "connecting": ({"retry_in_s": 12}, "Connecting to hub… retry in 12 s"),
    "disconnected": ({"last_error": "cannot reach the hub: ConnectionRefusedError", "retry_in_s": 30},
                     "Hub unreachable: cannot reach the hub: ConnectionRefusedError"),
    "auth_failed": ({"last_error": "join token rejected by the hub (close 4401)"},
                    "Join token rejected: run qis worker join"),
    "outdated": ({"last_error": "the hub does not support this protocol version"},
                 "Hub needs a newer QIS: run qis update"),
    "draining": ({"draining": True, "running": RUNNING}, "Draining: 1 job left"),
    "paused": ({"paused": True}, "Paused by hub"),
}


@pytest.mark.parametrize("link", sorted(CASES))
def test_every_link_state_has_its_headline_colour_and_state(link):
    extra, headline = CASES[link]
    vm = view_model(snap(worker_block(link, **extra)))
    assert (vm["state"], vm["headline"], vm["colour"]) == (link, headline, WORKER_COLOURS[link])
    assert vm["tooltip"].startswith(f"QIS: {headline}"[:50])
    assert len(vm["tooltip"]) <= TOOLTIP_MAX
    assert vm["worker"]["enabled"] and vm["worker"]["link"] == link
    assert list(vm["hub"]) == list(HUB_ROWS) and vm["hub"]["State"].startswith(link)


def test_colours_are_all_distinct_and_follow_the_documented_families():
    assert len(set(WORKER_COLOURS.values())) == len(WORKER_COLOURS) == 7
    assert WORKER_COLOURS["connected"] == "#16a34a"            # green
    assert WORKER_COLOURS["connecting"] == "#ca8a04"           # yellow, same as "starting"
    assert WORKER_COLOURS["disconnected"] == "#ea580c"         # orange
    assert WORKER_COLOURS["auth_failed"] == "#dc2626"          # red
    assert WORKER_COLOURS["draining"] == "#2563eb"             # blue


def test_connected_with_a_running_job_shows_progress():
    vm = view_model(snap(worker_block(running=RUNNING)))
    assert vm["headline"] == "Worker: connected · 1 running (62%)"
    assert "Hub: qis.runnow.cc" in vm["tooltip"] and "job_01XYZ" in vm["tooltip"] and "running" in vm["tooltip"]
    two = view_model(snap(worker_block(running=RUNNING + [{**RUNNING[0], "attempt_id": "att_2", "phase": "fetching", "pct": 0}])))
    assert two["headline"] == "Worker: connected · 2 running"


def test_tooltip_carries_hub_job_and_last_error_within_the_windows_limit():
    err = "cannot reach the hub: " + "x" * 300
    vm = view_model(snap(worker_block("disconnected", last_error=err, running=RUNNING)))
    lines = vm["tooltip"].split("\n")
    assert len(vm["tooltip"]) <= TOOLTIP_MAX
    assert lines[0].startswith("QIS: Hub unreachable") and lines[1] == "Hub: qis.runnow.cc"
    # the headline already holds the error: the tooltip is never longer than the OS allows, whatever the error says
    vm = view_model(snap(worker_block("connecting", last_error="timed out", retry_in_s=5, running=RUNNING)))
    assert "Hub: qis.runnow.cc" in vm["tooltip"] and "job_01XYZ" in vm["tooltip"] and "Error: timed out" in vm["tooltip"]
    assert len(vm["tooltip"]) <= TOOLTIP_MAX


def test_hub_frame_rows():
    vm = view_model(snap(worker_block(running=RUNNING)))
    assert vm["hub"] == {"State": "connected", "Hub": "wss://qis.runnow.cc/worker/ws", "Worker ID": "node_01TEST",
                         "Connected for": "1h 02m", "Last heartbeat": "4s ago", "Jobs this session": "12 done, 1 failed",
                         "Last error": "-"}
    vm = view_model(snap(worker_block("connecting", retry_in_s=12, last_error="boom", connected_since=None, last_ack_age_s=None)))
    assert vm["hub"]["State"] == "connecting (retry in 12 s)" and vm["hub"]["Connected for"] == "-"
    assert vm["hub"]["Last heartbeat"] == "-" and vm["hub"]["Last error"] == "boom"


def test_without_an_admin_key_healthz_alone_is_enough():
    vm = view_model(snap(worker_block("disconnected", last_error="ignored: not in /healthz"), health_only=True))
    assert vm["state"] == "disconnected" and vm["colour"] == WORKER_COLOURS["disconnected"]
    assert vm["headline"] == "Hub unreachable" and vm["hub"]["State"] == "disconnected"
    vm = view_model(snap(worker_block("auth_failed"), health_only=True))
    assert vm["headline"] == "Join token rejected: run qis worker join"
    assert vm["worker"]["hub_http"] == ""        # the hub address comes from the status block only


def test_engine_down_or_starting_wins_over_the_link():
    w = worker_block("connected")
    down = view_model(snap(w, health=None, ready=None, status=None, service="STOPPED", error="refused"))
    assert (down["state"], down["headline"], down["colour"]) == ("down", "Down", "#dc2626")
    assert down["tooltip"] == "QIS: Down" and down["hub"] == {}
    starting = view_model(snap(w, ready={"ready": False, "comfy": "starting", "warm": False}))
    assert (starting["state"], starting["colour"], starting["headline"]) == ("starting", "#ca8a04", "Starting / warming up")
    not_warm = view_model(snap(w, ready={"ready": False, "comfy": "ready", "warm": False}))
    assert not_warm["state"] == "starting"


def test_menu_flags_for_drain_resume_and_hub_dashboard():
    vm = view_model(snap(worker_block()))
    assert vm["worker"] == {"enabled": True, "link": "connected", "draining": False, "paused": False,
                            "hub_http": "https://qis.runnow.cc"}
    vm = view_model(snap(worker_block("draining", draining=True)))
    assert vm["worker"]["draining"] is True
    vm = view_model(snap(worker_block("paused", paused=True)))
    assert vm["worker"]["paused"] is True and vm["worker"]["draining"] is False


def test_worker_mode_off_looks_like_before():
    s = snap(None)
    assert worker_view(s) is None
    vm = view_model(s)
    assert (vm["state"], vm["headline"], vm["colour"]) == ("ready", "Ready", "#16a34a")
    assert vm["hub"] == {} and vm["worker"]["enabled"] is False and vm["tooltip"] == "QIS: Ready - 0 running, 0 queued"
    # a gateway that does not know about workers at all (older build) is fine too
    old = Snapshot(service="RUNNING", health={"status": "ok", "version": "0.1.0"},
                   ready={"ready": True, "comfy": "ready", "warm": True}, ts=NOW)
    assert view_model(old)["state"] == "ready"


def test_unknown_link_value_degrades_to_connecting():
    vm = view_model(snap(worker_block("something_new")))
    assert vm["state"] == "connecting"


def test_drained_worker_with_nothing_left():
    vm = view_model(snap(worker_block("draining", draining=True, running=[])))
    assert vm["headline"] == "Drained: no jobs left"
    vm = view_model(snap(worker_block("draining", draining=True, running=[], unacked=2)))
    assert vm["headline"] == "Draining: 2 jobs left"


def test_repeated_session_replacement_names_the_cause():
    once = view_model(snap(worker_block("connecting", replaced_count=1, retry_in_s=30)))
    assert once["headline"].startswith("Connecting to hub")
    twice = view_model(snap(worker_block("connecting", replaced_count=2, retry_in_s=30,
                                         last_error="replaced by another session with the same worker_id")))
    assert twice["headline"].startswith("worker_id is in use elsewhere") and twice["colour"] == WORKER_COLOURS["connecting"]

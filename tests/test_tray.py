"""`qis tray` pure parts: service ACE merge, sc.exe parsing, status payload -> view model, the admin key file."""
from __future__ import annotations

import pytest

from qis.auth import ApiKeys, AuthError
from qis.config import Config
from qis.db import DB
from qis.setup import merge_service_ace
from qis.tray import (
    Snapshot,
    create_key,
    ensure_key,
    fmt_dur,
    key_path,
    overall,
    parse_sc_state,
    read_key,
    view_model,
    write_key,
)

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
DEFAULT_SD = ("D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)(A;;CCLCSWLOCRRC;;;IU)"
              "(A;;CCLCSWLOCRRC;;;SU)S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)")


# ------------------------------------------------------------------ ACE merge
def test_ace_appended_to_dacl_before_sacl():
    out = merge_service_ace(DEFAULT_SD, SID)
    assert out == DEFAULT_SD.replace("S:(AU", f"(A;;RPWPLCRC;;;{SID})S:(AU")


def test_ace_merge_is_idempotent_and_strips_sdshow_whitespace():
    once = merge_service_ace("\r\n" + DEFAULT_SD + "\r\n", SID)
    assert merge_service_ace(once, SID) == once
    assert once.count(SID) == 1


def test_ace_existing_superset_or_case_differences_keep_sddl():
    sd = f"D:(A;;CCLCSWRPWPDTLOCRRC;;;{SID.lower()})"
    assert merge_service_ace(sd, SID) == sd


def test_ace_weaker_existing_grant_gets_a_new_ace():
    sd = f"D:(A;;LCRC;;;{SID})(D;;RP;;;BG)"
    out = merge_service_ace(sd, SID)
    assert out == f"D:(A;;LCRC;;;{SID})(D;;RP;;;BG)(A;;RPWPLCRC;;;{SID})"


def test_ace_dacl_flags_owner_group_and_no_sacl():
    sd = "O:SYG:SYD:PAI(A;;CCLCSWRPWPDTLOCRRC;;;SY)"
    assert merge_service_ace(sd, "AU") == "O:SYG:SYD:PAI(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;RPWPLCRC;;;AU)"


def test_ace_deny_ace_for_sid_does_not_count():
    sd = f"D:(D;;RPWPLCRC;;;{SID})"
    assert merge_service_ace(sd, SID).endswith(f"(A;;RPWPLCRC;;;{SID})")


def test_ace_without_dacl_raises():
    with pytest.raises(ValueError):
        merge_service_ace("S:(AU;FA;CCDC;;;WD)", SID)


# ------------------------------------------------------------------ sc.exe
SC_RUNNING = """
SERVICE_NAME: qis
        TYPE               : 10  WIN32_OWN_PROCESS
        STATE              : 4  RUNNING
                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)
        WIN32_EXIT_CODE    : 0  (0x0)
"""


def test_parse_sc_state():
    assert parse_sc_state(SC_RUNNING, 0) == "RUNNING"
    assert parse_sc_state(SC_RUNNING.replace("4  RUNNING", "2  START_PENDING"), 0) == "START_PENDING"
    assert parse_sc_state(SC_RUNNING.replace("STATE   ", "ZUSTAND ").replace("4  RUNNING", "1  STOPPED"), 0) == "STOPPED"
    assert parse_sc_state("[SC] OpenService FAILED 1060:\n\nThe specified service does not exist", 1060) == "NOT_INSTALLED"
    assert parse_sc_state("garbage", 1) == "UNKNOWN"


# ------------------------------------------------------------------ view model
STATUS = {
    "name": "node-a", "version": "0.1.0", "uptime_s": 3725,
    "gpu": {"name": "RTX 4080", "vram_total": 17171480576, "vram_free": 6000000000, "memory_total": 17171480576,
            "memory_used": 10737418240, "util_gpu": 87, "temperature": 71, "power_w": 221.4},
    "queue": {"queued": 3, "queued_cpu": 1, "running": 1, "running_cpu": 0, "max_pending": 20, "eta_s": 130,
              "paused": False},
    "last_24h": {"done": 120, "failed": 3, "gpu_s": 3900.5, "images": 130},
    "running_jobs": [{"id": "job_abc", "status": "running", "kind": "pipeline", "preset": "pixelart-16bit-64",
                      "eta_s": 12, "progress": {"type": "progress", "pct": 0.4567, "step": 9, "steps": 20}}],
}
KEYS = [
    {"name": "idle", "prefix": "sk-qis-aaaaaaa", "jobs_24h": 0, "failed_24h": 0, "revoked_at": None},
    {"name": "busy", "prefix": "sk-qis-bbbbbbb", "jobs_24h": 42, "failed_24h": 2, "revoked_at": None},
    {"name": "old", "prefix": "sk-qis-ccccccc", "jobs_24h": 0, "failed_24h": 0, "revoked_at": 1.0},
]


def snap(**kw) -> Snapshot:
    base = {"service": "RUNNING", "health": {"status": "ok", "version": "0.1.0"},
            "ready": {"ready": True, "comfy": "running", "warm": True, "missing_nodes": []},
            "status": STATUS, "keys": KEYS, "system": {"cpu_pct": 12.34, "ram_total": 64 * 1024 ** 3,
                                                       "ram_used": 18 * 1024 ** 3}, "ts": 1.0}
    return Snapshot(**{**base, **kw})


def test_view_model_full_payload():
    vm = view_model(snap())
    assert (vm["state"], vm["headline"]) == ("ready", "Ready")
    assert vm["subtitle"] == "node-a  v0.1.0  up 1h 02m"
    assert vm["service"] == {"Service": "running", "Gateway": "ok", "Engine": "running, warm"}
    assert vm["queue"] == {"Running": "1 GPU, 0 CPU", "Queued": "3 GPU, 1 CPU  (max pending 20)", "ETA": "2m 10s",
                           "Paused": "no"}
    assert vm["gpu"] == {"GPU": "RTX 4080", "Utilisation": "87 %", "VRAM": "10.0 GB / 16.0 GB",
                         "Temperature": "71 °C", "Power": "221 W"}
    assert vm["system"] == {"CPU": "12 %", "RAM": "18.0 GB / 64.0 GB"}
    assert vm["last_24h"] == {"Done": "120", "Failed": "3", "Images": "130", "GPU time": "1h 05m"}
    assert vm["jobs"] == [("job_abc", "pixelart-16bit-64", "46%  step 9/20", "12s")]
    assert [k[0] for k in vm["keys"]] == ["busy", "idle"]  # most used first, revoked + unused hidden
    assert vm["keys"][0] == ("busy", "sk-qis-bbbbbbb...", "42", "2", "active")
    assert vm["note"] == ""
    assert vm["tooltip"] == "QIS: Ready - 1 running, 3 queued"


def test_view_model_vram_from_comfy_when_no_nvml():
    st = {**STATUS, "gpu": {"name": "x", "vram_total": 8 * 1024 ** 3, "vram_free": 2 * 1024 ** 3}}
    vm = view_model(snap(status=st))
    assert vm["gpu"]["VRAM"] == "6.0 GB / 8.0 GB"
    assert vm["gpu"]["Utilisation"] == "-" and vm["gpu"]["Power"] == "-"


def test_view_model_gateway_unreachable_is_down():
    vm = view_model(Snapshot(service="STOPPED", error="All connection attempts failed"))
    assert vm["state"] == "down" and vm["colour"] == "#dc2626"
    assert vm["service"]["Gateway"] == "unreachable (All connection attempts failed)"
    assert vm["queue"]["Running"] == "-" and vm["jobs"] == [] and vm["keys"] == []
    assert vm["tooltip"] == "QIS: Down"


def test_overall_states():
    assert overall(Snapshot(service="NOT_INSTALLED")) == "not_installed"
    assert overall(Snapshot(service="START_PENDING")) == "starting"
    assert overall(Snapshot(service="RUNNING")) == "starting"  # gateway still booting
    assert overall(Snapshot(service="UNKNOWN")) == "down"
    assert overall(snap(ready={"ready": False, "comfy": "starting", "warm": False})) == "starting"
    # a gateway run by hand (no service) still counts when it answers
    assert overall(snap(service="NOT_INSTALLED")) == "ready"


def test_view_model_not_warm_and_missing_nodes():
    vm = view_model(snap(ready={"ready": False, "comfy": "running", "warm": False, "missing_nodes": ["QwenX"]}))
    assert vm["state"] == "starting"
    assert vm["service"]["Engine"] == "running, not warm, missing nodes: QwenX"


def test_view_model_without_admin_access():
    vm = view_model(snap(status=None, keys=None))
    assert "no admin key" in vm["note"]
    assert vm["subtitle"] == "v0.1.0" and vm["queue"]["Queued"] == "-"
    vm = view_model(snap(status=None, keys=None, auth_error="rejected (401)"))
    assert vm["note"] == "rejected (401)"


def test_fmt_dur():
    assert [fmt_dur(x) for x in (None, 5, 65, 3600, 7322)] == ["0s", "5s", "1m 05s", "1h 00m", "2h 02m"]


# ------------------------------------------------------------------ key file
KEY = "sk-qis-" + "A1b2C3" * 7


def test_key_file_roundtrip_and_validation(tmp_path):
    p = key_path(tmp_path)
    assert p == tmp_path / "config" / "tray.key"
    assert read_key(p) is None
    write_key(p, KEY)
    assert read_key(p) == KEY
    p.write_text(f"  {KEY}\r\n", encoding="utf-8")
    assert read_key(p) == KEY
    for junk in ("", "not a key", "sk-qis-short", KEY + " extra"):
        p.write_text(junk, encoding="utf-8")
        assert read_key(p) is None
    assert not list(p.parent.glob("*.tmp"))


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.paths.data = str(tmp_path / "data")
    return cfg


def test_create_and_ensure_key(tmp_path):
    cfg = _cfg(tmp_path)
    keys = ApiKeys(DB(cfg.data_dir / "qis.db"), cfg.secret())
    first = create_key(cfg, key_path(tmp_path))
    assert read_key(key_path(tmp_path)) == first
    assert keys.verify(first)["scopes"] == ["admin:*"]
    assert ensure_key(tmp_path, cfg) == first  # valid key kept
    keys.revoke(keys.verify(first)["id"])
    second = ensure_key(tmp_path, cfg)  # revoked -> replaced
    assert second != first and read_key(key_path(tmp_path)) == second
    third = create_key(cfg, key_path(tmp_path))  # a new tray key revokes the previous one
    with pytest.raises(AuthError):
        keys.verify(second)
    assert [k["name"] for k in keys.list() if not k["revoked_at"]] == ["tray"]
    assert keys.verify(third)

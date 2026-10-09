"""The wire protocol shared with the hub repo: every fixture parses and round-trips, bad ones are refused, and the
protocol file itself has not drifted from the hash recorded next to the fixtures (docs/MESH.md section 10.1)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from qis.mesh import protocol
from qis.mesh.protocol import MESSAGES, Close, ProtocolError, parse_message

FIXTURES = Path(__file__).parent / "fixtures" / "mesh"
META = FIXTURES / "_meta.json"
NAMES = sorted(p.stem for p in FIXTURES.glob("*.json") if not p.stem.startswith("_"))


def protocol_sha() -> str:
    """sha256 of qis/mesh/protocol.py with LF line endings (the hub repo hashes its copy the same way)."""
    text = Path(protocol.__file__).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(text).hexdigest()


def test_every_message_type_has_a_fixture():
    assert set(NAMES) == set(MESSAGES)


@pytest.mark.parametrize("name", NAMES)
def test_valid_fixtures_parse_and_roundtrip(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert data["valid"], f"{name}: needs at least one valid example"
    for raw in data["valid"]:
        msg = parse_message(raw)
        assert type(msg) is MESSAGES[name] and msg.type == name
        assert msg.to_wire() == raw                       # nothing added, nothing lost
        assert parse_message(msg.to_json()).to_wire() == raw   # and through real JSON text


@pytest.mark.parametrize("name", NAMES)
def test_invalid_fixtures_are_refused(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    for case in data["invalid"]:
        with pytest.raises(ProtocolError) as e:
            parse_message(case["msg"])
        assert e.value.code == "bad_message", case["why"]


def test_unknown_type_garbage_and_extra_fields():
    with pytest.raises(ProtocolError) as e:
        parse_message({"v": 1, "type": "teleport", "id": "m_1"})
    assert e.value.code == "unknown_type" and e.value.reply_to == "m_1"
    for junk in ("not json", "[1, 2]", "42", b"\xff"):
        with pytest.raises(ProtocolError) as e:
            parse_message(junk)
        assert e.value.code in ("bad_json", "bad_message")
    # unknown fields are ignored, known ones kept (forward compatible)
    msg = parse_message({"v": 1, "type": "accept", "id": "m_1", "ts": 1.0, "attempt_id": "att_1", "from_the_future": [1]})
    assert msg.attempt_id == "att_1" and "from_the_future" not in msg.to_wire()


def test_message_ids_and_defaults():
    a, b = protocol.Accept(attempt_id="att_1"), protocol.Accept(attempt_id="att_1")
    assert a.id.startswith("m_") and len(a.id) == 28 and a.id != b.id
    wire = a.to_wire()
    assert list(wire)[:4] == ["v", "type", "id", "ts"] and wire["v"] == protocol.PROTO == 1
    assert (protocol.PROTO_MIN, protocol.PROTO_MAX) == (1, 1)
    assert protocol.MAX_FRAME_BYTES == 2 * 1024 * 1024
    assert (Close.AUTH, Close.REPLACED, Close.PROTOCOL, Close.BAD_HELLO) == (4401, 4409, 4426, 4400)


def test_output_file_names_follow_the_contract():
    ok = ["o0-0.png", "o12-3-preview.png", "raw-0.png", "o1-0.webp", "o1-0.jpg"]
    bad = ["O0.png", "o0-0.gif", "-o.png", "a/b.png", "o0-0.PNG", "x" * 41 + ".png", ".png"]
    assert all(protocol.OUTPUT_NAME_RE.match(n) for n in ok)
    assert not any(protocol.OUTPUT_NAME_RE.match(n) for n in bad)


def test_protocol_file_matches_recorded_sha():
    """The hub keeps a verbatim copy; a changed protocol.py must update the recorded hash (UPDATE_MESH_SHA=1 pytest ...)
    and be copied to the hub repo."""
    sha = protocol_sha()
    if os.environ.get("UPDATE_MESH_SHA"):
        META.write_text(json.dumps({"proto": protocol.PROTO, "protocol_sha256": sha,
                                    "note": "sha256 of qis/mesh/protocol.py with LF line endings; "
                                            "the hub keeps a verbatim copy in app/hub/protocol.py"},
                                   indent=1) + "\n", encoding="utf-8")
    meta = json.loads(META.read_text(encoding="utf-8"))
    assert meta["proto"] == protocol.PROTO
    assert meta["protocol_sha256"] == sha, "protocol.py changed: rerun with UPDATE_MESH_SHA=1 and sync the hub copy"

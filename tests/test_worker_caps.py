"""What the worker tells the hub about itself: model identity, fingerprints, preset availability, learnt time model."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from qis.config import Config, WorkerCfg
from qis.mesh.caps import Capabilities
from qis.mesh.perf import fit
from qis.models import Manifest, ModelEntry
from qis.node import Node

TURBO_FILE, BASE_FILE, CLIP, VAE = "turbo.gguf", "base.gguf", "enc.safetensors", "vae.safetensors"


def entry(mid: str, role: str, file: str, dir_: str, data: bytes) -> ModelEntry:
    return ModelEntry(id=mid, role=role, file=file, dir=dir_, url="", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def put(root: Path, sub: str, name: str, data: bytes) -> bytes:
    (root / sub).mkdir(parents=True, exist_ok=True)
    (root / sub / name).write_bytes(data)
    return data


def make_caps(tmp_path: Path, name: str, files: dict[str, bytes], manifest: Manifest | None = None, *, with_root: bool = True,
              base: str | None = BASE_FILE, base_q8: str | None = None) -> Capabilities:
    cfg = Config()
    cfg.paths.data = str(tmp_path / name / "data")
    if with_root:
        cfg.paths.models = str(tmp_path / name / "models")
        root = Path(cfg.paths.models)
        root.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            sub, fname = rel.split("/")
            put(root, sub, fname, data)
    cfg.models.turbo, cfg.models.base, cfg.models.base_q8 = TURBO_FILE, base, base_q8
    cfg.models.clip, cfg.models.vae = CLIP, VAE
    cfg.models.upscalers = {"realesrgan-x2": "x2.pth", "realesrgan-x4": "x4.pth"}
    cfg.worker = WorkerCfg(enabled=False)
    node = Node(cfg, cpu_pool=ThreadPoolExecutor(1), start_comfy=False)
    return Capabilities(node, manifest or Manifest())


def full_set(turbo: bytes) -> dict[str, bytes]:
    return {f"diffusion_models/{TURBO_FILE}": turbo, f"diffusion_models/{BASE_FILE}": b"base" * 10,
            f"text_encoders/{CLIP}": b"clip" * 10, f"vae/{VAE}": b"vae" * 10, "upscale_models/x2.pth": b"u2" * 5,
            "upscale_models/x4.pth": b"u4" * 5}


def manifest_for(files: dict[str, bytes]) -> Manifest:
    ids = {"diffusion_models/" + TURBO_FILE: ("turbo-q8", "turbo"), "diffusion_models/" + BASE_FILE: ("base-q4", "base"),
           "text_encoders/" + CLIP: ("enc", "clip"), "vae/" + VAE: ("vae", "vae"),
           "upscale_models/x2.pth": ("x2", "upscaler"), "upscale_models/x4.pth": ("x4", "upscaler")}
    models = {}
    for rel, (mid, role) in ids.items():
        sub, fname = rel.split("/")
        models[mid] = entry(mid, role, fname, sub, files[rel])
    return Manifest(models=models)


# ------------------------------------------------------------------ identity and fingerprints
def test_manifest_match_gives_id_and_sha_and_a_self_merged_file_does_not(tmp_path):
    official = full_set(b"T" * 100)
    man = manifest_for(official)
    a = make_caps(tmp_path, "a", official, man)
    files = {f.file: f for f in a.model_files()}
    turbo = files[TURBO_FILE]
    assert (turbo.role, turbo.mode, turbo.bytes, turbo.manifest_id) == ("diffusion", "turbo", 100, "turbo-q8")
    assert turbo.sha256 == hashlib.sha256(b"T" * 100).hexdigest()
    assert files[CLIP].role == "text_encoder" and files[VAE].role == "vae"
    assert (files["x2.pth"].role, files["x2.pth"].key) == ("upscaler", "realesrgan-x2")

    merged = dict(official)
    merged[f"diffusion_models/{TURBO_FILE}"] = b"M" * 120              # same name, a locally merged build
    b = make_caps(tmp_path, "b", merged, man)
    t2 = next(f for f in b.model_files() if f.file == TURBO_FILE)
    assert t2.manifest_id is None and t2.sha256 is None and t2.bytes == 120
    # different file -> different fingerprint for every preset that uses it, same for the ones that do not
    pa, pb = a.presets(), b.presets()
    assert pa["qwen-image-2.1-turbo"].fingerprint != pb["qwen-image-2.1-turbo"].fingerprint
    assert pa["qwen-image-2.1-base"].fingerprint == pb["qwen-image-2.1-base"].fingerprint
    # and the fingerprint is stable between calls and between two identical machines
    c = make_caps(tmp_path, "c", official, man)
    assert c.presets()["qwen-image-2.1-turbo"].fingerprint == pa["qwen-image-2.1-turbo"].fingerprint
    assert pa["qwen-image-2.1-turbo"].files == [TURBO_FILE, CLIP, VAE]


async def test_background_hash_replaces_name_and_size_and_is_cached(tmp_path):
    files = full_set(b"M" * 120)
    caps = make_caps(tmp_path, "a", files, manifest_for(full_set(b"T" * 100)))
    before = caps.presets()["qwen-image-2.1-turbo"].fingerprint
    assert [f.file for _, f in caps.pending_hashes()] == [TURBO_FILE]
    fired = []
    caps.on_hashes_ready = lambda: fired.append(1)
    caps.start_hashing()
    await caps._hash_task
    assert fired == [1] and caps.pending_hashes() == []
    turbo = next(f for f in caps.model_files() if f.file == TURBO_FILE)
    assert turbo.sha256 == hashlib.sha256(b"M" * 120).hexdigest() and turbo.manifest_id is None
    assert caps.presets()["qwen-image-2.1-turbo"].fingerprint != before       # identity upgraded name:bytes -> sha
    assert (tmp_path / "a" / "data" / "model_hashes.json").exists()
    # a restart reads the cache instead of hashing again; touching the file invalidates it
    again = make_caps(tmp_path, "a", {}, manifest_for(full_set(b"T" * 100)))
    assert again.pending_hashes() == []
    p = tmp_path / "a" / "models" / "diffusion_models" / TURBO_FILE
    p.write_bytes(b"N" * 130)
    assert [f.file for _, f in make_caps(tmp_path, "a", {}, manifest_for(full_set(b"T" * 100))).pending_hashes()] == [TURBO_FILE]


# ------------------------------------------------------------------ presets
def test_preset_needing_an_unconfigured_mode_is_not_advertised(tmp_path):
    files = full_set(b"T" * 100)
    caps = make_caps(tmp_path, "a", files, manifest_for(files), base_q8=None)
    caps.node.presets.data["needs-q8"] = {"description": "", "ops": [{"op": "generate", "mode": "base_q8", "size": 1024}]}
    assert "needs-q8" not in caps.presets() and "qwen-image-2.1-turbo" in caps.presets()
    # configured and on disk -> advertised
    files["diffusion_models/q8.gguf"] = b"q8" * 20
    q8 = make_caps(tmp_path, "b", files, manifest_for(full_set(b"T" * 100)), base_q8="q8.gguf")
    q8.node.presets.data["needs-q8"] = {"description": "", "ops": [{"op": "generate", "mode": "base_q8", "size": 1024}]}
    assert "needs-q8" in q8.presets()
    # configured but the file is not on disk -> not advertised (and not in model_files)
    gone = make_caps(tmp_path, "c", full_set(b"T" * 100), manifest_for(full_set(b"T" * 100)), base_q8="q8.gguf")
    gone.node.presets.data["needs-q8"] = {"description": "", "ops": [{"op": "generate", "mode": "base_q8", "size": 1024}]}
    assert "needs-q8" not in gone.presets() and "q8.gguf" not in {f.file for f in gone.model_files()}


def test_missing_file_on_disk_removes_the_presets_that_use_it(tmp_path):
    files = full_set(b"T" * 100)
    del files["upscale_models/x2.pth"], files["upscale_models/x4.pth"], files["diffusion_models/" + BASE_FILE]
    caps = make_caps(tmp_path, "a", files, manifest_for(full_set(b"T" * 100)))
    names = set(caps.presets())
    assert "qwen-image-2.1-turbo" in names and "pixelart-16bit-64" in names       # turbo + encoder + vae only
    assert "qwen-image-2.1-base" not in names                                    # base gguf missing
    assert "qwen-image-2.1-hq" not in names                                      # needs the x2 upscaler


def test_without_a_model_folder_the_configuration_is_trusted(tmp_path):
    caps = make_caps(tmp_path, "a", {}, with_root=False)
    names = set(caps.presets())
    assert {"qwen-image-2.1-turbo", "qwen-image-2.1-base", "qwen-image-2.1-hq"} <= names
    assert all(f.bytes == 0 for f in caps.model_files())


def test_ops_without_any_diffusion_model_are_cpu_only(tmp_path):
    caps = make_caps(tmp_path, "a", {}, with_root=False)
    caps.cfg.models.turbo = caps.cfg.models.base = caps.cfg.models.base_q8 = None
    assert "generate" not in caps.op_names() and "pixelize" in caps.op_names()


def test_spec_fingerprint_follows_the_files_the_ops_use(tmp_path):
    files = full_set(b"T" * 100)
    caps = make_caps(tmp_path, "a", files, manifest_for(files))
    from qis.pipeline import JobSpec
    px = JobSpec.model_validate({"ops": [{"op": "pixelize", "image": "file_" + "0" * 26}]})
    turbo = JobSpec.model_validate({"ops": [{"op": "generate", "prompt": "x", "mode": "turbo"}]})
    base = JobSpec.model_validate({"ops": [{"op": "generate", "prompt": "x", "mode": "base"}]})
    assert len({caps.spec_fingerprint(px), caps.spec_fingerprint(turbo), caps.spec_fingerprint(base)}) == 3
    assert caps.spec_fingerprint(turbo) == caps.presets()["qwen-image-2.1-turbo"].fingerprint
    assert caps.missing_for(turbo) == []
    caps.cfg.models.base = None
    assert caps.missing_for(base) == ["diffusion:base"]


# ------------------------------------------------------------------ learnt time model
def job(a1: float, b: float, *, steps=5, size=1024, n=1, refs=False, cls=None, jitter=1.0) -> dict:
    x = steps * (size * size / 1e6)
    calls = [{"seconds": round((a1 + b * x) * jitter, 3), "sample_s": round(b * x * jitter, 3)} for _ in range(n)]
    op = {"op": "generate", "mode": "turbo", "steps": steps, "size": size, "n": n, "prompt": "x"}
    if refs:
        op["refs"] = ["file_" + "0" * 26]
    return {"spec": {"ops": [op]},
            "result": {"timings": [{"op": "generate", "class": cls or ("edit" if refs else "t2i"), "kind": "gpu",
                                    "seconds": sum(c["seconds"] for c in calls), "estimate": 1, "calls": calls}]}}


def test_fit_recovers_fixed_and_per_step_cost():
    rows = [job(50.0, 2.5, jitter=1 + 0.02 * ((-1) ** i)) for i in range(12)]
    out = fit(rows)
    t2i = out["t2i"]
    assert t2i["samples"] == 12
    assert t2i["a1"] == pytest.approx(50.0, rel=0.2) and t2i["b"] == pytest.approx(2.5, rel=0.2)
    assert t2i["aN"] == pytest.approx(0.5 * t2i["a1"], rel=0.01)             # no multi-image job seen: aN defaults to a1/2


def test_fit_separates_first_and_later_calls_and_classes():
    rows = []
    for _ in range(6):
        j = job(40.0, 2.0, n=3)
        j["result"]["timings"][0]["calls"][1:] = [{"seconds": 15.0 + 2.0 * 5.24, "sample_s": 2.0 * 5.24}] * 2   # later calls: a(N)=15
        rows.append(j)
    rows += [job(79.0, 2.5, refs=True) for _ in range(5)]
    out = fit(rows)
    assert out["t2i"]["a1"] == pytest.approx(40.0, rel=0.05) and out["t2i"]["aN"] == pytest.approx(15.0, rel=0.05)
    assert out["edit"]["a1"] == pytest.approx(79.0, rel=0.05) and out["edit"]["samples"] == 5


def test_fit_needs_five_samples_and_ignores_old_or_odd_rows():
    assert fit([job(50, 2.5) for _ in range(4)]) == {}
    rows = [job(50, 2.5) for _ in range(4)]
    legacy = job(50, 2.5)
    del legacy["result"]["timings"][0]["calls"]                                   # recorded before worker mode
    enhance = job(50, 2.5)
    enhance["spec"]["ops"][0]["enhance"] = True                                   # extra call would pollute a(1)
    assert fit(rows + [legacy, enhance]) == {}
    assert "t2i" in fit(rows + [job(50, 2.5)])


def test_fit_cpu_and_other_gpu_classes_use_seconds_per_call():
    rows = [{"spec": {"ops": [{"op": "upscale"}]},
             "result": {"timings": [{"op": "upscale", "class": "upscale", "kind": "gpu", "seconds": 8.0, "estimate": 5,
                                     "calls": [{"seconds": 4.0, "sample_s": 0.0}] * 2}]}} for _ in range(5)]
    assert fit(rows)["upscale"] == {"a1": 4.0, "aN": 4.0, "b": 0.0, "samples": 5}

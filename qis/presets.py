"""Presets: named pipelines exposed as OpenAI "models", and the mapping of OpenAI image requests onto them."""
from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path

import yaml

from .engine.constants import MODES, ModelSet
from .engine.ops import OP_TYPES, OpError
from .pipeline import JobSpec

QUALITY = {  # OpenAI quality -> generate overrides (turbo presets)
    "low": {"steps": 4},
    "medium": {},
    "standard": {},
    "auto": {},
    "high": {"steps": 8},
    "hd": {"mode": "base", "steps": 10},
}


class Presets:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.reload()

    def reload(self) -> None:
        if self.path and self.path.exists():
            text = self.path.read_text(encoding="utf-8")
        else:
            text = resources.files("qis.data").joinpath("presets.yaml").read_text(encoding="utf-8")
        self.data: dict[str, dict] = yaml.safe_load(text) or {}
        for name, p in self.data.items():
            self.validate(name, p)

    @staticmethod
    def validate(name: str, p: dict) -> None:
        if not isinstance(p, dict) or not p.get("ops"):
            raise ValueError(f"preset {name}: needs a non-empty ops list")
        ops = copy.deepcopy(p["ops"])
        for op in ops:
            if op.get("op") not in OP_TYPES:
                raise ValueError(f"preset {name}: unknown op {op.get('op')!r}")
            if op["op"] == "generate":
                op.setdefault("prompt", "x")
            if op["op"] in ("compose",):
                op.setdefault("ui", "$prev")
            if op["op"] == "fidelity":
                op.setdefault("reference", "$prev")
        JobSpec.model_validate({"ops": ops})

    def save(self, data: dict) -> None:
        for name, p in data.items():
            self.validate(name, p)
        if not self.path:
            raise OpError("presets file is not configured")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.data = data

    def available(self, models: ModelSet) -> dict[str, dict]:
        """Presets whose models are installed on this node."""
        out = {}
        for name, p in self.data.items():
            ok = True
            for op in p["ops"]:
                mode = op.get("mode") or op.get("engine")
                if op["op"] in ("generate", "refine", "outpaint", "enhance_prompt"):
                    mode = mode or "turbo"
                    if op["op"] != "enhance_prompt" and MODES.get(mode, {}).get("unet") not in models.unets:
                        ok = False
                if op["op"] == "upscale" and op.get("method", "esrgan") == "esrgan" and not models.upscalers:
                    ok = False
            if ok:
                out[name] = p
        return out

    def get(self, name: str) -> dict | None:
        return self.data.get(name)

    def build(self, name: str, *, prompt: str, n: int = 1, size: str | None = None, quality: str | None = None,
              transparent: bool | None = None, refs: list[str] | None = None, extra: dict | None = None,
              models: ModelSet | None = None) -> JobSpec:
        p = self.get(name)
        if p is None:
            raise OpError(f"unknown model {name!r}")
        ops = copy.deepcopy(p["ops"])
        gen = next((o for o in ops if o["op"] == "generate"), None)
        if gen is None:
            raise OpError(f"preset {name} has no generate step")
        gen["prompt"] = prompt
        gen["n"] = n
        if refs:
            gen["refs"] = refs
        if size and size != "auto":
            try:
                w, h = (int(x) for x in size.lower().split("x"))
            except ValueError as e:
                raise OpError(f"size must be WIDTHxHEIGHT or auto, got {size!r}") from e
            gen["width"], gen["height"] = w, h
        if quality:
            if quality not in QUALITY:
                raise OpError(f"quality must be one of {list(QUALITY)}")
            q = dict(QUALITY[quality])
            if q.get("mode") and models and MODES[q["mode"]]["unet"] not in models.unets:
                q = {"steps": 8}
            if gen.get("mode", "turbo") == "turbo" or q.get("mode"):
                if q.get("mode") == "base":
                    gen.pop("steps", None)
                gen.update(q)
        if transparent is not None:
            gen["transparent"] = transparent
        for key, value in (extra or {}).items():
            aliases = {"keep_bg": ("remove_bg", lambda v: not v), "no_crop": ("crop", lambda v: not v),
                       "preview": ("preview_size", lambda v: v)}
            if key in aliases:
                key, conv = aliases[key]
                value = conv(value)
            target = next((o for o in ops if key in OP_TYPES[o["op"]].model_fields and key not in ("op", "image")), None)
            if target is None:
                raise OpError(f"unknown parameter {key!r} for model {name}")
            target[key] = value
        return JobSpec.model_validate({"ops": ops, "preset": name})

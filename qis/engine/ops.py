"""Pipeline ops: one validated parameter model + estimate + run per op.

A job is a list of ops. Image fields take a reference ("$inputs.name", "$prev", "$step[i]", "$step[i].preview",
a file id) or a list of them; they are resolved to file ids before run(). Image -> image ops map over every input
image, so `generate n=4 -> refine -> pixelize` yields four finished sprites.
"""
from __future__ import annotations

import asyncio
import io
import math
import random
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal, Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import imaging
from . import workflows as W
from .constants import MODES, STYLES, ModelSet

ImageRef = str | list[str]


class OpError(ValueError):
    """Bad op parameters or references (400 at submission, 'failed' at run time)."""


@dataclass
class StepResult:
    images: list[str] = field(default_factory=list)            # file ids (main output of the step)
    extra: dict[str, list[str]] = field(default_factory=dict)  # named secondary images, e.g. {"preview": [...]}
    text: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class OpContext(Protocol):
    """What an op needs from the executor (implemented in qis.executor)."""
    models: ModelSet
    job_id: str
    step: int
    prev_text: str | None

    def read(self, file_id: str) -> bytes: ...
    def size(self, file_id: str) -> tuple[int, int]: ...
    def store(self, data: bytes, name: str) -> str: ...
    async def upload(self, file_id_or_bytes: str | bytes) -> str: ...
    async def comfy(self, workflow: dict, node: str, label: str = "", progress_part: tuple[int, int] = (0, 1)) -> list[bytes]: ...
    async def comfy_text(self, workflow: dict, node: str) -> str: ...
    async def cpu(self, fn, *args) -> Any: ...
    def cached_prompt(self, key: str) -> tuple[str, str] | None: ...
    def cache_prompt(self, key: str, request: str, prompt: str, ratio: str) -> None: ...
    def check_cancel(self) -> None: ...


def _dims_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def _prompt_key(request: str, ref_shas: list[str]) -> str:
    import hashlib
    import json
    return hashlib.sha1(json.dumps([request] + ref_shas).encode()).hexdigest()


class BaseOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ClassVar[str] = "gpu"
    image_fields: ClassVar[tuple[str, ...]] = ()
    maps: ClassVar[bool] = False  # image -> image over each input

    def model_key(self, models: ModelSet) -> str | None:
        return None

    def estimate(self, perf, n_in: int, mp_in: float) -> float:
        return perf.cpu_op_s * max(1, n_in)

    def outputs_per_input(self) -> int:
        return 1

    async def run(self, ctx: OpContext, refs: dict[str, list[str]]) -> StepResult:  # pragma: no cover
        raise NotImplementedError


def _sps(perf, mode: str) -> float:
    return perf.s_per_step.get(mode, 4.0)


def _calib(perf, op: str) -> float:
    return perf.calib.get(op, 1.0)


# ---------------------------------------------------------------- generate / edit
class GenerateOp(BaseOp):
    op: Literal["generate"] = "generate"
    image_fields: ClassVar = ("refs",)
    prompt: str = Field(min_length=1, max_length=8000)
    refs: ImageRef = Field(default_factory=list, description="1-3 reference images -> edit / multi-reference")
    mode: str = "turbo"
    steps: int = Field(0, ge=0, le=100)
    n: int = Field(1, ge=1, le=16)
    seed: int | None = Field(None, ge=0, lt=2**48)
    width: int | None = Field(None, ge=256, le=2048)
    height: int | None = Field(None, ge=256, le=2048)
    size: int = Field(1024, ge=256, le=2048, description="about size x size pixels when width/height are not given")
    aspect: str | None = Field(None, pattern=r"^\d+(\.\d+)?:\d+(\.\d+)?$")
    negative: str = ""
    cfg: float = Field(1.0, ge=0, le=20)
    template: Literal["raw", "asset"] = "raw"
    style: str = "none"
    transparent: bool = False
    enhance: bool = False
    clip_device: Literal["default", "cpu"] = "default"

    @field_validator("mode")
    @classmethod
    def _mode(cls, v):
        if v not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}")
        return v

    @field_validator("style")
    @classmethod
    def _style(cls, v):
        if v not in STYLES:
            raise ValueError(f"style must be one of {list(STYLES)}")
        return v

    def model_key(self, models):
        return models.unets.get(MODES[self.mode]["unet"])

    def outputs_per_input(self) -> int:
        return self.n

    def _mp(self) -> float:
        if self.width and self.height:
            return self.width * self.height / 1e6
        return self.size * self.size / 1e6

    def estimate(self, perf, n_in, mp_in) -> float:
        steps = self.steps or MODES[self.mode]["steps"]
        one = perf.overhead_s + steps * _sps(perf, self.mode) * self._mp() / perf.ref_mp
        return self.n * one * _calib(perf, "generate") + (perf.enhance_s if self.enhance else 0)

    def gen_params(self, aspect: str | None) -> W.GenParams:
        return W.GenParams(mode=self.mode, steps=self.steps, size=self.size, aspect=aspect or "1:1", width=self.width,
                           height=self.height, negative=self.negative, cfg=self.cfg, clip_device=self.clip_device)

    async def run(self, ctx, refs):
        ref_ids = refs.get("refs", [])
        if len(ref_ids) > 3:
            raise OpError("generate takes at most 3 reference images")
        edit = bool(ref_ids)
        params = self.gen_params(self.aspect)
        params.resolved_steps()  # validate before any upload
        names = [await ctx.upload(r) for r in ref_ids]
        revised = None
        if self.enhance:
            request = self.prompt if self.template == "raw" else W.build_prompt(self.prompt, self.style, False, edit)
            prompt, ratio = await enhance_prompt(ctx, request, ref_ids, names, self.clip_device)
            revised = prompt
            if self.transparent:
                prompt = W.wrap_transparent(prompt)
            if ratio and not self.aspect and not edit and not (self.width and self.height):
                params.aspect = ratio if _valid_ratio(ratio) else "1:1"
        elif self.template == "raw":
            prompt = W.wrap_transparent(self.prompt) if self.transparent else self.prompt
        else:
            prompt = W.build_prompt(self.prompt, self.style, self.transparent, edit)
        seed0 = self.seed if self.seed is not None else random.randrange(2**48 - self.n)
        images, seeds = [], []
        for i in range(self.n):
            ctx.check_cancel()
            wf = W.generate(params, prompt, seed0 + i, names, ctx.models, prefix=f"qis/{ctx.job_id}/{ctx.step}")
            for data in await ctx.comfy(wf, "save_raw", label="generate", progress_part=(i, self.n)):
                images.append(ctx.store(data, f"generate_{i}.png"))
            seeds.append(seed0 + i)
        return StepResult(images=images, text=prompt,
                          data={"seeds": seeds, "prompt": prompt, "revised_prompt": revised, "mode": self.mode,
                                "aspect": params.aspect if not (edit or (self.width and self.height)) else None})


def _valid_ratio(r: str) -> bool:
    try:
        a, b = (float(x) for x in r.split(":"))
        return a > 0 and b > 0 and 0.2 <= a / b <= 5
    except ValueError:
        return False


async def enhance_prompt(ctx, request: str, ref_ids: list[str], names: list[str], clip_device: str) -> tuple[str, str]:
    """Official rewriter on the text encoder; cached per request + reference contents."""
    import hashlib
    key = _prompt_key(request, [hashlib.sha256(ctx.read(r)).hexdigest() for r in ref_ids])
    if hit := ctx.cached_prompt(key):
        return hit
    text = await ctx.comfy_text(W.enhance(request, names, clip_device, ctx.models), "show")
    parsed = W.parse_enhanced(text)
    if parsed is None:  # rewriter did not return JSON: keep the request as written
        return request, ""
    ctx.cache_prompt(key, request, *parsed)
    return parsed


class EnhancePromptOp(BaseOp):
    op: Literal["enhance_prompt"] = "enhance_prompt"
    image_fields: ClassVar = ("refs",)
    prompt: str = Field(min_length=1, max_length=8000)
    refs: ImageRef = Field(default_factory=list)
    template: Literal["raw", "asset"] = "raw"
    style: str = "none"
    clip_device: Literal["default", "cpu"] = "default"

    def model_key(self, models):
        return models.clip

    def estimate(self, perf, n_in, mp_in):
        return perf.enhance_s * _calib(perf, "enhance_prompt")

    async def run(self, ctx, refs):
        ref_ids = refs.get("refs", [])
        names = [await ctx.upload(r) for r in ref_ids]
        request = self.prompt if self.template == "raw" else W.build_prompt(self.prompt, self.style, False, bool(ref_ids))
        prompt, ratio = await enhance_prompt(ctx, request, ref_ids, names, self.clip_device)
        return StepResult(text=prompt, data={"prompt": prompt, "ratio": ratio, "request": request})


# ---------------------------------------------------------------- refine / upscale / outpaint
class _SizeMixin(BaseModel):
    width: int | None = Field(None, ge=16, le=8192)
    height: int | None = Field(None, ge=16, le=8192)
    scale: float | None = Field(None, gt=0, le=8)

    def target(self, w: int, h: int, default_scale: float, multiple32: bool) -> tuple[int, int]:
        if self.width and self.height:
            tw, th = self.width, self.height
        elif self.width:
            tw, th = self.width, self.width * h / w
        elif self.height:
            tw, th = self.height * w / h, self.height
        else:
            s = self.scale or default_scale
            tw, th = w * s, h * s
        if multiple32:
            return W.round32(tw), W.round32(th)
        return max(1, round(tw)), max(1, round(th))


class RefineOp(BaseOp, _SizeMixin):
    op: Literal["refine"] = "refine"
    image_fields: ClassVar = ("image",)
    maps: ClassVar = True
    image: ImageRef = "$prev"
    prompt: str = Field("", max_length=8000, description="empty = the previous step's prompt")
    engine: str = "turbo"
    nodes: str = "0.25, 0.12"
    denoise: float = Field(0.45, gt=0, le=1)
    steps: int = Field(24, ge=1, le=100)
    seed: int = Field(1, ge=0)

    def model_key(self, models):
        return models.unets.get(MODES.get(self.engine, {}).get("unet", ""))

    def estimate(self, perf, n_in, mp_in):
        mp = (self.width * self.height / 1e6) if self.width and self.height else mp_in * (self.scale or 1.5) ** 2
        steps = len(self.nodes.split(",")) if self.engine == "turbo" else max(1, round(self.steps * self.denoise))
        return max(1, n_in) * (perf.overhead_s + steps * _sps(perf, self.engine) * mp / perf.ref_mp) * _calib(perf, "refine")

    async def run(self, ctx, refs):
        prompt = self.prompt or ctx.prev_text or ""
        if not prompt:
            raise OpError("refine needs a prompt (or a previous step that produced one)")
        out = []
        for i, fid in enumerate(refs["image"]):
            ctx.check_cancel()
            w, h = ctx.size(fid)
            tw, th = self.target(w, h, 1.5, multiple32=True)
            name = await ctx.upload(fid)
            wf = W.refine(name, prompt, tw, th, engine=self.engine, nodes=self.nodes, denoise=self.denoise,
                          steps=self.steps, seed=self.seed, models=ctx.models, prefix=f"qis/{ctx.job_id}/{ctx.step}_{i}")
            for data in await ctx.comfy(wf, "save", label="refine", progress_part=(i, len(refs["image"]))):
                out.append(ctx.store(data, f"refine_{i}.png"))
        return StepResult(images=out, text=prompt)


class UpscaleOp(BaseOp, _SizeMixin):
    op: Literal["upscale"] = "upscale"
    image_fields: ClassVar = ("image",)
    maps: ClassVar = True
    image: ImageRef = "$prev"
    method: Literal["esrgan", "nearest", "lanczos"] = "esrgan"
    model: str | None = Field(None, description="upscaler key (realesrgan-x2 / realesrgan-x4); default by scale")

    @property
    def kind(self) -> str:  # type: ignore[override]
        return "gpu" if self.method == "esrgan" else "cpu"

    def _model(self, models: ModelSet, factor: float) -> str:
        if self.model:
            if self.model not in models.upscalers:
                raise OpError(f"unknown upscaler {self.model!r}; installed: {list(models.upscalers)}")
            return models.upscalers[self.model]
        want = "realesrgan-x4" if factor > 2.01 else "realesrgan-x2"
        if want in models.upscalers:
            return models.upscalers[want]
        if not models.upscalers:
            raise OpError("no ESRGAN upscaler installed; use method nearest or lanczos")
        return next(iter(models.upscalers.values()))

    def model_key(self, models):
        return None  # small, loads in a second; no affinity

    def estimate(self, perf, n_in, mp_in):
        if self.method != "esrgan":
            return perf.cpu_op_s * max(1, n_in)
        mp = (self.width * self.height / 1e6) if self.width and self.height else mp_in * (self.scale or 2) ** 2
        return max(1, n_in) * (2 + perf.upscale_s_per_mp * mp) * _calib(perf, "upscale")

    async def run(self, ctx, refs):
        out = []
        for i, fid in enumerate(refs["image"]):
            ctx.check_cancel()
            w, h = ctx.size(fid)
            tw, th = self.target(w, h, 2.0, multiple32=False)
            if self.method == "esrgan":
                model = self._model(ctx.models, max(tw / w, th / h))
                wf = W.upscale(await ctx.upload(fid), model, tw, th, prefix=f"qis/{ctx.job_id}/{ctx.step}_{i}")
                datas = await ctx.comfy(wf, "save", label="upscale", progress_part=(i, len(refs["image"])))
            else:
                datas = [await ctx.cpu(imaging.resize_bytes, ctx.read(fid), tw, th, self.method)]
            out += [ctx.store(d, f"upscale_{i}.png") for d in datas]
        return StepResult(images=out)


class OutpaintOp(BaseOp):
    op: Literal["outpaint"] = "outpaint"
    image_fields: ClassVar = ("image",)
    maps: ClassVar = True
    image: ImageRef = "$prev"
    width: int = Field(ge=64, le=8192)
    height: int = Field(ge=64, le=8192)
    prompt: str = Field(min_length=1, max_length=4000, description="what the extended area should contain")
    anchor_y: float = Field(0.5, ge=0, le=1)
    draft: int = Field(768, ge=256, le=1536)
    steps: int = Field(6, ge=4, le=8)
    seed: int = Field(1, ge=0)
    feather: int = Field(24, ge=0, le=256)
    scene: str = Field("", max_length=4000, description="short description of the whole image for the refine pass")
    refine_nodes: str = Field("0.5, 0.25", description="empty = ESRGAN only")
    upscaler: str = "realesrgan-x4"

    def model_key(self, models):
        return models.unets.get("turbo")

    def estimate(self, perf, n_in, mp_in):
        mp = self.width * self.height / 1e6
        draft = perf.overhead_s + self.steps * _sps(perf, "turbo") * (self.draft ** 2 / 1e6) / perf.ref_mp
        refine = (perf.overhead_s + len(self.refine_nodes.split(",")) * _sps(perf, "turbo") * mp / perf.ref_mp) if self.refine_nodes else 0
        return max(1, n_in) * (draft + 2 + perf.upscale_s_per_mp * mp + refine) * _calib(perf, "outpaint")

    async def run(self, ctx, refs):
        if "turbo" not in ctx.models.unets:
            raise OpError("outpaint needs the turbo model")
        W_, H_ = self.width, self.height
        out = []
        for i, fid in enumerate(refs["image"]):
            ctx.check_cancel()
            src = Image.open(io.BytesIO(ctx.read(fid))).convert("RGB")
            dw, dh = W.dims(self.draft, f"{W_}:{H_}")
            canvas = await asyncio.to_thread(imaging.hint_canvas, src, dw, dh, self.anchor_y)
            prompt = ("Image 1 shows a scene in its middle; the blurry areas are empty and must be painted. "
                      f"Extend the scene naturally into the blurry areas: {self.prompt} "
                      "Keep the existing scene exactly unchanged, same art style, colours, lighting and perspective, "
                      "seamless continuation with no borders or frames.")
            params = W.GenParams(mode="turbo", steps=self.steps, size=self.draft)
            wf = W.generate(params, prompt, self.seed, [await ctx.upload(imaging._png(canvas))], ctx.models,
                            prefix=f"qis/{ctx.job_id}/{ctx.step}_{i}_draft")
            draft = Image.open(io.BytesIO((await ctx.comfy(wf, "save_raw", label="outpaint draft"))[0])).convert("RGB")
            ctx.check_cancel()
            if self.upscaler not in ctx.models.upscalers:
                raise OpError(f"upscaler {self.upscaler!r} is not installed")
            wf = W.upscale(await ctx.upload(imaging._png(draft)), ctx.models.upscalers[self.upscaler], W_, H_,
                           prefix=f"qis/{ctx.job_id}/{ctx.step}_{i}_up")
            up = Image.open(io.BytesIO((await ctx.comfy(wf, "save", label="outpaint upscale"))[0])).convert("RGB")
            # the sharp original goes in first so the refine pass matches its detail level across the seam
            up = await asyncio.to_thread(imaging.paste_back, up, src, self.anchor_y, self.feather)
            if self.refine_nodes:
                rw, rh = W.round32(W_), W.round32(H_)
                wf = W.refine(await ctx.upload(imaging._png(up)), f"{self.scene} {self.prompt}".strip(), rw, rh,
                              nodes=self.refine_nodes, seed=self.seed, models=ctx.models,
                              prefix=f"qis/{ctx.job_id}/{ctx.step}_{i}_refine")
                ref = Image.open(io.BytesIO((await ctx.comfy(wf, "save", label="outpaint refine"))[0])).convert("RGB")
                up = ref.resize((W_, H_), Image.LANCZOS)
            final = await asyncio.to_thread(imaging.paste_back, up, src, self.anchor_y, self.feather)
            out.append(ctx.store(imaging._png(final), f"outpaint_{i}.png"))
        return StepResult(images=out)


# ---------------------------------------------------------------- CPU ops
class PixelizeOp(BaseOp):
    op: Literal["pixelize"] = "pixelize"
    kind: ClassVar = "cpu"
    image_fields: ClassVar = ("image",)
    maps: ClassVar = True
    image: ImageRef = "$prev"
    grid: int = Field(64, ge=8, le=512)
    colors: int = Field(24, ge=2, le=256)
    palette: str = "adaptive"
    downscale: Literal["box", "nearest"] = "box"
    dither: bool = False
    crop: bool = True
    remove_bg: bool = True
    preview_size: int = Field(256, ge=0, le=2048)

    @field_validator("palette")
    @classmethod
    def _pal(cls, v):
        if v not in imaging.PALETTE_CHOICES:
            raise ValueError(f"palette must be one of {imaging.PALETTE_CHOICES}")
        return v

    async def run(self, ctx, refs):
        sprites, previews = [], []
        for i, fid in enumerate(refs["image"]):
            s, p = await ctx.cpu(imaging.pixelize_bytes, ctx.read(fid), self.grid, self.colors, self.palette,
                                 self.downscale, self.dither, self.preview_size, self.crop, self.remove_bg)
            sprites.append(ctx.store(s, f"sprite_{self.grid}px_{i}.png"))
            previews.append(ctx.store(p, f"preview_{i}.png"))
        return StepResult(images=sprites, extra={"preview": previews})


class RemoveBgOp(BaseOp):
    op: Literal["remove_bg"] = "remove_bg"
    kind: ClassVar = "cpu"
    image_fields: ClassVar = ("image",)
    maps: ClassVar = True
    image: ImageRef = "$prev"
    tolerance: float = Field(40, ge=1, le=255)

    async def run(self, ctx, refs):
        return StepResult(images=[ctx.store(await ctx.cpu(imaging.remove_bg_bytes, ctx.read(f), self.tolerance), f"nobg_{i}.png")
                                  for i, f in enumerate(refs["image"])])


class ComposeOp(BaseOp):
    op: Literal["compose"] = "compose"
    kind: ClassVar = "cpu"
    image_fields: ClassVar = ("ui", "model")
    ui: ImageRef
    model: ImageRef = "$prev"
    top: float = Field(0.26, ge=0, le=1)
    bottom: float = Field(0.84, ge=0, le=1)
    max_width: float = Field(0.9, gt=0, le=1)

    async def run(self, ctx, refs):
        if len(refs["ui"]) != 1:
            raise OpError("compose takes exactly one ui image")
        ui = ctx.read(refs["ui"][0])
        return StepResult(images=[ctx.store(await ctx.cpu(imaging.compose_bytes, ui, ctx.read(m), self.top, self.bottom,
                                                          self.max_width), f"compose_{i}.png")
                                  for i, m in enumerate(refs["model"])])


class FidelityOp(BaseOp):
    op: Literal["fidelity"] = "fidelity"
    kind: ClassVar = "cpu"
    image_fields: ClassVar = ("reference", "images")
    reference: ImageRef
    images: ImageRef = "$prev"

    async def run(self, ctx, refs):
        if len(refs["reference"]) != 1:
            raise OpError("fidelity takes exactly one reference image")
        scores = await ctx.cpu(imaging.fidelity_bytes, ctx.read(refs["reference"][0]), [ctx.read(f) for f in refs["images"]])
        return StepResult(data={"scores": [{"file_id": f, **s} for f, s in zip(refs["images"], scores, strict=True)]})


AnyOp = Annotated[GenerateOp | EnhancePromptOp | RefineOp | UpscaleOp | OutpaintOp | PixelizeOp | RemoveBgOp
                  | ComposeOp | FidelityOp, Field(discriminator="op")]
OP_TYPES: dict[str, type[BaseOp]] = {c.model_fields["op"].default: c for c in (
    GenerateOp, EnhancePromptOp, RefineOp, UpscaleOp, OutpaintOp, PixelizeOp, RemoveBgOp, ComposeOp, FidelityOp)}


def op_kind(op: BaseOp) -> str:
    return op.kind if isinstance(op.kind, str) else "gpu"


def mp_of(w: int | None, h: int | None) -> float:
    return (w or 1024) * (h or 1024) / 1e6


__all__ = ["AnyOp", "OP_TYPES", "BaseOp", "OpError", "StepResult", "OpContext", "op_kind", "mp_of", "math"]

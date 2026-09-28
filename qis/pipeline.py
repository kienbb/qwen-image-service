"""JobSpec: a validated list of ops plus named inputs, and the reference resolution between steps."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .engine.ops import AnyOp, BaseOp, OpError, StepResult, op_kind

REF = re.compile(r"^\$(?:(inputs)\.([A-Za-z0-9_\-]+)|(prev)|step\[(\d+)\])(?:\.(preview|text|[A-Za-z0-9_]+))?$")
FILE_ID = re.compile(r"^file_[0-9A-Z]{26}$")


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[AnyOp] = Field(min_length=1, max_length=16)
    inputs: dict[str, str | list[str]] = Field(default_factory=dict, description="name -> file id(s)")
    outputs: list[str] | None = Field(None, description='steps to return, e.g. ["$step[2]", "$prev"]; default: last step')
    priority: Literal["interactive", "batch"] | None = None
    webhook_url: str | None = Field(None, pattern=r"^https?://", max_length=2000)
    retention_hours: float | None = Field(None, gt=0, le=24 * 30)
    preset: str | None = None

    def gpu(self) -> bool:
        return any(op_kind(o) == "gpu" for o in self.ops)

    def file_ids(self) -> list[str]:
        ids = []
        for v in self.inputs.values():
            ids += v if isinstance(v, list) else [v]
        for op in self.ops:
            for f in op.image_fields:
                for r in _as_list(getattr(op, f)):
                    if FILE_ID.match(r):
                        ids.append(r)
        return ids


def _as_list(v) -> list[str]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _parse(ref: str) -> tuple[str, str | int | None, str | None]:
    m = REF.match(ref)
    if not m:
        raise OpError(f"bad reference {ref!r} (use $inputs.name, $prev, $step[i], optionally .preview / .text, or a file id)")
    if m.group(1):
        return "inputs", m.group(2), m.group(5)
    if m.group(3):
        return "prev", None, m.group(5)
    return "step", int(m.group(4)), m.group(5)


def validate_refs(spec: JobSpec) -> list[int]:
    """Static checks before queueing; returns the image count each step will produce (for the cost model)."""
    counts: list[int] = []
    for i, op in enumerate(spec.ops):
        n_in = 0
        for f in op.image_fields:
            for r in _as_list(getattr(op, f)):
                if FILE_ID.match(r):
                    n_in += 1
                    continue
                kind, target, attr = _parse(r)
                if kind == "inputs":
                    if target not in spec.inputs:
                        raise OpError(f"step {i}: {r} is not in inputs")
                    n_in += len(_as_list(spec.inputs[target]))
                else:
                    k = i - 1 if kind == "prev" else target
                    if k < 0 or k >= i:
                        raise OpError(f"step {i} ({op.op}): {r} must point to an earlier step")
                    n_in += counts[k]
        if op.image_fields and op.maps and n_in == 0:
            raise OpError(f"step {i} ({op.op}) has no input image")
        if op.op == "generate":
            counts.append(op.n)
        elif op.op in ("enhance_prompt", "fidelity"):
            counts.append(0)
        elif op.op == "compose":
            counts.append(_count(spec, counts, i, op.model))
        else:
            counts.append(n_in)
        for name, value in op.model_dump().items():
            if isinstance(value, str) and value.startswith("$") and name not in op.image_fields:
                kind, target, attr = _parse(value)
                if attr != "text" or kind == "inputs":
                    raise OpError(f"step {i}: text field {name} can only reference $prev.text or $step[k].text")
                k = i - 1 if kind == "prev" else target
                if k < 0 or k >= i:
                    raise OpError(f"step {i}: {value} must point to an earlier step")
    for r in spec.outputs or []:
        kind, target, _ = _parse(r)
        if kind == "inputs" or (kind == "step" and target >= len(spec.ops)):
            raise OpError(f"output {r} must be $prev or $step[i] with i < {len(spec.ops)}")
    return counts


def _count(spec, counts, i, ref) -> int:
    n = 0
    for r in _as_list(ref):
        if FILE_ID.match(r):
            n += 1
            continue
        kind, target, _ = _parse(r)
        n += len(_as_list(spec.inputs.get(target, []))) if kind == "inputs" else counts[i - 1 if kind == "prev" else target]
    return n


def resolve_images(spec: JobSpec, results: list[StepResult], i: int, value) -> list[str]:
    out: list[str] = []
    for r in _as_list(value):
        if FILE_ID.match(r):
            out.append(r)
            continue
        kind, target, attr = _parse(r)
        if kind == "inputs":
            out += _as_list(spec.inputs[target])
            continue
        res = results[i - 1 if kind == "prev" else target]
        if attr and attr != "text":
            if attr not in res.extra:
                raise OpError(f"{r}: step has no '{attr}' images (has {list(res.extra) or 'none'})")
            out += res.extra[attr]
        else:
            out += res.images
    return out


def bind(op: BaseOp, spec: JobSpec, results: list[StepResult], i: int) -> tuple[BaseOp, dict[str, list[str]]]:
    """Resolve image references to file ids and $..text references to strings for step i."""
    refs = {f: resolve_images(spec, results, i, getattr(op, f)) for f in op.image_fields}
    updates = {}
    for name in type(op).model_fields:
        v = getattr(op, name)
        if name not in op.image_fields and isinstance(v, str) and v.startswith("$"):
            kind, target, _ = _parse(v)
            updates[name] = results[i - 1 if kind == "prev" else target].text or ""
    return (op.model_copy(update=updates) if updates else op), refs


def selected_outputs(spec: JobSpec, results: list[StepResult]) -> list[int]:
    if not spec.outputs:
        return [len(results) - 1]
    idx = []
    for r in spec.outputs:
        kind, target, _ = _parse(r)
        idx.append(len(results) - 1 if kind == "prev" else target)
    return idx

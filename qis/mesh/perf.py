"""Learn this machine's time model from its own job history (docs/MESH.md section 3.1).

For each timing class the hub keeps a(1), a(N) and b: est = a1 + (calls-1)*aN + calls*b*x, where x = steps * MP * mode
factor is the sampling volume of one call. The worker sends its own estimates in `hello.perf_model` so the hub starts
from real numbers instead of a tier table. Needs `timings[].calls[]` (written since worker mode); older jobs lack it
and are skipped.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from ..engine.constants import MODES
from ..executor import op_class
from ..pipeline import FILE_ID

ALPHA = 0.2
MIN_SAMPLES = 5
MODE_F = {"turbo": 1.0, "base": 1.05, "base_q8": 1.45}
SAMPLING = ("t2i", "edit")           # classes whose b is learnt from sample_s


def _ewma(prev: float | None, new: float) -> float:
    return new if prev is None else (1 - ALPHA) * prev + ALPHA * new


def sampling_units(op: dict, size_of: Callable[[str], tuple[int, int] | None] | None = None) -> float:
    """x for one call of a generate op: steps * megapixels * mode factor (edit: size of the first reference)."""
    mode = op.get("mode", "turbo")
    steps = op.get("steps") or MODES.get(mode, {}).get("steps", 5)
    mp = None
    refs = op.get("refs")
    first = (refs if isinstance(refs, list) else [refs])[0] if refs else None
    if first and size_of and isinstance(first, str) and FILE_ID.match(first):
        dims = size_of(first)
        if dims:
            mp = dims[0] * dims[1] / 1e6
    if mp is None:
        w, h = op.get("width"), op.get("height")
        mp = (w * h / 1e6) if w and h else (op.get("size", 1024) ** 2) / 1e6
    return steps * mp * MODE_F.get(mode, 1.0)


def fit(rows: Iterable[dict], size_of: Callable[[str], tuple[int, int] | None] | None = None) -> dict[str, dict]:
    """rows: {"spec", "result"} oldest first. -> {class: {a1, aN, b, samples}} for classes with >= MIN_SAMPLES jobs."""
    acc: dict[str, dict] = {}
    for row in rows:
        spec, result = row.get("spec") or {}, row.get("result") or {}
        ops, timings = spec.get("ops") or [], result.get("timings") or []
        for op, t in zip(ops, timings, strict=False):
            calls = t.get("calls")
            if t.get("kind") != "gpu" or not calls:
                continue
            cls = t.get("class") or op_class(op.get("op", ""), bool(op.get("refs")))
            a = acc.setdefault(cls, {"a1": None, "aN": None, "b": None, "samples": 0})
            if cls in SAMPLING:
                if op.get("enhance") or len(calls) != op.get("n", 1):
                    continue      # the enhance pass is an extra call: it would pollute the fixed cost
                x = sampling_units(op, size_of)
                for i, c in enumerate(calls):
                    fixed = max(c["seconds"] - c["sample_s"], 0.0)
                    key = "a1" if i == 0 else "aN"
                    a[key] = _ewma(a[key], fixed)
                    if x > 0 and c["sample_s"] > 0:
                        a["b"] = _ewma(a["b"], c["sample_s"] / x)
            else:
                per_call = t["seconds"] / max(1, len(calls))
                a["a1"] = _ewma(a["a1"], per_call)
                a["aN"] = a["a1"]
                a["b"] = 0.0
            a["samples"] += 1
    out = {}
    for cls, a in acc.items():
        if a["samples"] >= MIN_SAMPLES and a["a1"] is not None:
            a1 = a["a1"]
            out[cls] = {"a1": round(a1, 2), "aN": round(a["aN"] if a["aN"] is not None else 0.5 * a1, 2),
                        "b": round(a["b"] or 0.0, 3), "samples": a["samples"]}
    return out

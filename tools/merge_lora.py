"""Bake a diffusers-format Qwen-Image 2.1 LoRA into the bf16 base weights -> f32 safetensors for sd.cpp quantization.

The update is added in fp32 and written as fp32 (no bf16 round-trip), so the quantizer sees the exact merged weight.
The fused ComfyUI `img_mlp.gate_up` is split back into diffusers `gate_layer` / `proj` so the result uses the same
tensor names as the published GGUFs.

  python_embeded/python.exe tools/merge_lora.py base_bf16.safetensors lora.safetensors out_f32.safetensors
"""
import json
import struct
import sys

import torch
from safetensors import safe_open
from safetensors.torch import load_file


def load_lora(path):
    with safe_open(path, "pt") as f:
        meta = json.loads((f.metadata() or {}).get("lora_adapter_metadata", "{}"))
    sd = load_file(path)
    scale = meta.get("transformer.lora_alpha", 1) / meta.get("transformer.r", 1)
    lora = {}
    for k in sd:
        if k.endswith(".lora_A.weight"):
            name = k.removeprefix("transformer.").removesuffix(".lora_A.weight") + ".weight"
            lora[name] = (sd[k].float(), sd[k.replace("lora_A", "lora_B")].float() * scale)
    print(f"lora: {len(lora)} layers, scale {scale}")
    return lora


def plan(f):
    """(out name, source name, row slice, shape) in diffusers naming; gate_up is split into gate_layer / proj."""
    for name in f.keys():
        shape = f.get_slice(name).get_shape()
        if name.endswith("img_mlp.gate_up.weight"):
            half = shape[0] // 2
            prefix = name.removesuffix("gate_up.weight")
            yield prefix + "gate_layer.weight", name, slice(0, half), [half] + shape[1:]
            yield prefix + "proj.weight", name, slice(half, None), [half] + shape[1:]
        else:
            yield name, name, slice(None), shape


def main(base, lora_path, out):
    lora = load_lora(lora_path)
    with safe_open(base, "pt") as f:
        items = list(plan(f))
        missing = set(lora) - {o for o, *_ in items}
        if missing:
            sys.exit(f"LoRA layers without a matching base tensor: {sorted(missing)[:10]}")
        header, offset = {}, 0
        for out_name, _, _, shape in items:
            dtype, size = ("F32", 4) if out_name in lora else ("BF16", 2)
            n = size
            for d in shape:
                n *= d
            header[out_name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + n]}
            offset += n
        h = json.dumps(header, separators=(",", ":")).encode()
        h += b" " * (-len(h) % 8)
        with open(out, "wb") as o:
            o.write(struct.pack("<Q", len(h)) + h)
            for out_name, src, rows, _ in items:
                t = f.get_tensor(src)[rows]
                if out_name in lora:
                    a, b = lora[out_name]
                    t = t.float() + b @ a
                o.write(t.contiguous().view(torch.uint8).numpy().tobytes())
    print(f"merged {len(lora)} tensors -> {out} ({offset / 2**30:.1f} GiB)")


if __name__ == "__main__":
    main(*sys.argv[1:4])

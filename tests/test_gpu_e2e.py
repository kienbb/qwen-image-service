"""End-to-end checks against a real node with the models installed (skipped by default).

  QIS_URL=http://127.0.0.1:8080 QIS_API_KEY=sk-qis-... uv run pytest -m gpu -q
"""
from __future__ import annotations

import base64
import io
import os
import time

import pytest
from PIL import Image

pytestmark = pytest.mark.gpu
URL = os.environ.get("QIS_URL", "http://127.0.0.1:8080")
KEY = os.environ.get("QIS_API_KEY", "")


@pytest.fixture(scope="module")
def client():
    if not KEY:
        pytest.skip("set QIS_API_KEY")
    from openai import OpenAI
    return OpenAI(base_url=URL + "/v1", api_key=KEY, timeout=1800)


def _img(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def test_pixelart_sprite(client):
    t = time.time()
    r = client.images.generate(model="pixelart-16bit-64", prompt="a red health potion in a glass flask",
                               extra_body={"seed": 3})
    sprite = _img(r.data[0].b64_json)
    assert max(sprite.size) == 64 and sprite.mode == "RGBA"
    assert sprite.getchannel("A").getextrema()[0] == 0  # has transparent background
    print(f"pixelart {time.time() - t:.1f}s")


def test_generate_size_and_seed_repeatable(client):
    a = client.images.generate(model="qwen-image-2.1-turbo", prompt="a wooden treasure chest", size="768x512",
                               extra_body={"seed": 11, "steps": 4})
    b = client.images.generate(model="qwen-image-2.1-turbo", prompt="a wooden treasure chest", size="768x512",
                               extra_body={"seed": 11, "steps": 4})
    ia, ib = _img(a.data[0].b64_json), _img(b.data[0].b64_json)
    assert ia.size == (768, 512)
    assert ia.tobytes() == ib.tobytes()  # same seed, same image


def test_edit(client, tmp_path):
    base = client.images.generate(model="qwen-image-2.1-turbo", prompt="a knight standing, plain background", size="512x512",
                                  extra_body={"seed": 5})
    p = tmp_path / "k.png"
    p.write_bytes(base64.b64decode(base.data[0].b64_json))
    r = client.images.edit(model="qwen-image-2.1-turbo", image=open(p, "rb"), prompt="Make his cape red.")
    assert _img(r.data[0].b64_json).size[0] > 0

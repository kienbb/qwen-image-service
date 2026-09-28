"""A fake ComfyUI speaking the HTTP + WebSocket protocol the service uses, for tests without a GPU.

Prompts run one at a time. Markers in any string input of a workflow change the behaviour:
  FAIL_OOM      execution_error "CUDA out of memory" (only while fake.oom_left > 0)
  FAIL_EXEC     execution_error (always)
  FAIL_REJECT   /prompt answers 400 (validation error)
  SLOW          run takes fake.slow_s instead of fake.run_s
"""
from __future__ import annotations

import asyncio
import io
import json
import socket
import struct
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from PIL import Image

NODES = ["UnetLoaderGGUF", "TextEncodeQwenImage21", "QwenImage21Cache", "ViggleTurboSigmas", "PixelArtRetro",
         "UpscaleModelLoader", "TextGenerate", "CLIPLoader", "VAELoader", "SaveImage", "LoadImage"]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _png(w: int, h: int, alpha: bool = False) -> bytes:
    im = Image.new("RGBA" if alpha else "RGB", (w, h), (200, 60, 60, 255) if alpha else (200, 60, 60))
    for x in range(w // 4, 3 * w // 4):
        for y in range(h // 4, 3 * h // 4):
            im.putpixel((x, y), (30, 90, 200, 255) if alpha else (30, 90, 200))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class FakeComfy:
    def __init__(self):
        self.port = free_port()
        self.run_s = 0.3
        self.slow_s = 3.0
        self.oom_left = 0
        self.down = False
        self.history: dict[str, dict] = {}
        self.prompts: list[dict] = []
        self.queue: asyncio.Queue | None = None
        self.sockets: dict[str, WebSocket] = {}
        self.uploads: dict[str, bytes] = {}
        self.outputs: dict[str, bytes] = {}
        self.interrupted: set[str] = set()
        self.running: str | None = None
        self.app = self._build()
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    # ---------------------------------------------------------------- app
    def _build(self) -> FastAPI:
        app = FastAPI()
        fake = self

        @app.middleware("http")
        async def down(request: Request, call_next):
            if fake.down:
                return JSONResponse({"error": "down"}, status_code=503)
            return await call_next(request)

        @app.on_event("startup")
        async def start():
            fake.queue = asyncio.Queue()
            asyncio.create_task(fake._worker())

        @app.get("/system_stats")
        async def stats():
            return {"system": {"comfyui_version": "0.37.0-fake"},
                    "devices": [{"name": "cuda:0 Fake GPU", "vram_total": 6 * 1024 ** 3, "vram_free": 4 * 1024 ** 3}]}

        @app.get("/object_info")
        async def info():
            return {n: {} for n in NODES}

        @app.post("/upload/image")
        async def upload(request: Request):
            form = await request.form()
            f = form["image"]
            data = await f.read()
            sub = form.get("subfolder") or ""
            fake.uploads[f"{sub}/{f.filename}" if sub else f.filename] = data
            return {"name": f.filename, "subfolder": sub, "type": "input"}

        @app.post("/prompt")
        async def prompt(request: Request):
            body = await request.json()
            wf = body["prompt"]
            text = json.dumps(wf)
            if "FAIL_REJECT" in text:
                return JSONResponse({"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"},
                                     "node_errors": {}}, status_code=400)
            pid = uuid.uuid4().hex
            fake.prompts.append({"id": pid, "workflow": wf, "client_id": body.get("client_id"), "extra": body.get("extra_data")})
            await fake.queue.put((pid, wf, body.get("client_id"), body.get("extra_data") or {}))
            return {"prompt_id": pid, "number": len(fake.prompts), "node_errors": {}}

        @app.get("/history/{pid}")
        async def history(pid: str):
            return {pid: fake.history[pid]} if pid in fake.history else {}

        @app.get("/view")
        async def view(filename: str, subfolder: str = "", type: str = "output"):
            key = f"{subfolder}/{filename}"
            if key not in fake.outputs:
                return Response(status_code=404)
            return Response(fake.outputs[key], media_type="image/png")

        @app.post("/interrupt")
        async def interrupt(request: Request):
            try:
                body = await request.json()
            except ValueError:
                body = {}
            pid = body.get("prompt_id") or fake.running
            if pid:
                fake.interrupted.add(pid)
            return {}

        @app.post("/queue")
        async def queue(request: Request):
            body = await request.json()
            for pid in body.get("delete", []):
                fake.interrupted.add(pid)
            return {}

        @app.post("/free")
        async def free():
            return {}

        @app.websocket("/ws")
        async def ws(websocket: WebSocket, clientId: str = ""):
            await websocket.accept()
            fake.sockets[clientId] = websocket
            try:
                await websocket.send_json({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}, "sid": clientId}})
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                fake.sockets.pop(clientId, None)

        return app

    async def _send(self, cid, msg):
        ws = self.sockets.get(cid)
        if ws:
            try:
                if isinstance(msg, bytes):
                    await ws.send_bytes(msg)
                else:
                    await ws.send_json(msg)
            except Exception:
                pass

    async def _worker(self):
        while True:
            pid, wf, cid, extra = await self.queue.get()
            if pid in self.interrupted:
                self.history[pid] = {"outputs": {}, "status": {"status_str": "error", "completed": False,
                                                               "messages": [["execution_interrupted", {"prompt_id": pid}]]}}
                continue
            self.running = pid
            text = json.dumps(wf)
            await self._send(cid, {"type": "execution_start", "data": {"prompt_id": pid}})
            steps = 4
            dur = self.slow_s if "SLOW" in text else self.run_s
            interrupted = False
            for i in range(1, steps + 1):
                await asyncio.sleep(dur / steps)
                if pid in self.interrupted:
                    interrupted = True
                    break
                await self._send(cid, {"type": "progress", "data": {"value": i, "max": steps, "prompt_id": pid, "node": "sample"}})
                if extra.get("preview_method"):
                    buf = io.BytesIO()
                    Image.new("RGB", (32, 32), (i * 40, 0, 0)).save(buf, "JPEG")
                    await self._send(cid, struct.pack(">II", 1, 1) + buf.getvalue())
            self.running = None
            if interrupted:
                self.history[pid] = {"outputs": {}, "status": {"status_str": "error", "completed": False,
                                                               "messages": [["execution_interrupted", {"prompt_id": pid}]]}}
                await self._send(cid, {"type": "execution_interrupted", "data": {"prompt_id": pid}})
                continue
            if "FAIL_EXEC" in text or ("FAIL_OOM" in text and self.oom_left > 0):
                oom = "FAIL_OOM" in text
                if oom:
                    self.oom_left -= 1
                msg = "CUDA out of memory. Tried to allocate 2.00 GiB" if oom else "boom"
                err = {"prompt_id": pid, "node_id": "sample", "exception_message": msg, "exception_type": "RuntimeError"}
                self.history[pid] = {"outputs": {}, "status": {"status_str": "error", "completed": False,
                                                               "messages": [["execution_error", err]]}}
                await self._send(cid, {"type": "execution_error", "data": err})
                continue
            outputs = self._outputs(pid, wf)
            for node, out in outputs.items():
                await self._send(cid, {"type": "executed", "data": {"node": node, "output": out, "prompt_id": pid}})
            self.history[pid] = {"outputs": outputs, "status": {"status_str": "success", "completed": True,
                                                                "messages": [["execution_success", {"prompt_id": pid}]]}}
            await self._send(cid, {"type": "executing", "data": {"node": None, "prompt_id": pid}})

    def _outputs(self, pid, wf):
        w = h = 64
        for n in wf.values():
            if n["class_type"] in ("EmptyLatentImage", "ImageScale"):
                w, h = n["inputs"]["width"], n["inputs"]["height"]
        w, h = max(8, w // 8), max(8, h // 8)  # small but proportional
        alpha = "RGBA format" in json.dumps(wf)
        outputs = {}
        for nid, n in wf.items():
            if n["class_type"] == "SaveImage":
                prefix = n["inputs"]["filename_prefix"]
                sub, _, base = prefix.rpartition("/")
                fname = f"{base}_{pid[:6]}_.png"
                self.outputs[f"{sub}/{fname}"] = _png(w, h, alpha)
                outputs[nid] = {"images": [{"filename": fname, "subfolder": sub, "type": "output"}]}
            elif n["class_type"] == "PreviewAny":
                outputs[nid] = {"text": ['<think>x</think>{"rewritten_prompt": "An enhanced prompt about a knight", "wh_ratio": "2:3"}']}
        return outputs

    # ---------------------------------------------------------------- run
    def start(self):
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", ws="websockets")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fake comfy did not start")

    def stop(self):
        if self.server:
            self.server.should_exit = True
            self.thread.join(5)

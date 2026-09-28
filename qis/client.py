"""Tiny stdlib-only client for the service API (used by gen.py and tools/*.py; no third-party packages needed).

  QIS_URL      default http://127.0.0.1:8080
  QIS_API_KEY  sk-qis-...
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class ApiError(RuntimeError):
    def __init__(self, status: int, body: dict | str):
        self.status, self.body = status, body
        msg = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"HTTP {status}: {msg}")


class Client:
    def __init__(self, url: str | None = None, key: str | None = None, timeout: float = 1800):
        self.url = (url or os.environ.get("QIS_URL") or "http://127.0.0.1:8080").rstrip("/")
        self.key = key or os.environ.get("QIS_API_KEY") or ""
        self.timeout = timeout
        if not self.key:
            sys.exit("set QIS_API_KEY (create one with: qis key create --name cli) or pass --api-key")

    def _req(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None, raw: bool = False):
        headers = {"Authorization": f"Bearer {self.key}"}
        if ctype:
            headers["Content-Type"] = ctype
        url = path if path.startswith("http") else self.url + path
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                raise ApiError(e.code, json.loads(text)) from None
            except ValueError:
                raise ApiError(e.code, text) from None
        except urllib.error.URLError as e:
            sys.exit(f"cannot reach the service at {self.url}: {e.reason} (is `qis serve` running?)")
        if raw:
            return data
        parsed = json.loads(data.decode().strip() or "{}")
        if isinstance(parsed, dict) and "error" in parsed and "id" not in parsed:
            raise ApiError(200, parsed)  # failure reported after keep-alive
        return parsed

    def get(self, path: str):
        return self._req("GET", path)

    def post(self, path: str, payload: dict):
        return self._req("POST", path, json.dumps(payload).encode(), "application/json")

    def upload(self, path: str | Path) -> str:
        p = Path(path)
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{p.name}\"\r\n"
                f"Content-Type: {mimetypes.guess_type(p.name)[0] or 'image/png'}\r\n\r\n").encode()
        body += p.read_bytes() + f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\ninput\r\n--{boundary}--\r\n".encode()
        return self._req("POST", "/v1/files", body, f"multipart/form-data; boundary={boundary}")["id"]

    def download(self, url: str) -> bytes:
        return self._req("GET", url, raw=True)

    def run_job(self, spec: dict, poll_s: float = 1.0, progress: bool = True) -> dict:
        job = self.post("/v1/jobs", spec)
        last = ""
        while job["status"] not in ("succeeded", "failed", "canceled", "expired"):
            time.sleep(poll_s)
            job = self.get(f"/v1/jobs/{job['id']}")
            if progress:
                p = job.get("progress") or {}
                line = f"{job['status']}"
                if job["status"] == "queued":
                    line += f" position={job.get('position')} eta={job.get('eta_s')}s"
                elif p:
                    line += f" step {p.get('op_index', 0) + 1}/{len(spec['ops'])} {p.get('op', '')} {p.get('step', '')}/{p.get('steps', '')}"
                if line != last:
                    print("  " + line, file=sys.stderr)
                    last = line
        if job["status"] != "succeeded":
            err = job.get("error") or {}
            raise ApiError(0, {"error": {"message": f"job {job['status']}: {err.get('message', '')}"}})
        return job


def b64(path: str | Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()

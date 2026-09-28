"""Fixtures: a fake ComfyUI and a real gateway (uvicorn in a thread) talking to it over HTTP."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn

from qis.app import create_app
from qis.auth import ApiKeys
from qis.config import Config
from qis.node import Node

from .fake_comfy import FakeComfy, free_port


@pytest.fixture(scope="session")
def fake():
    f = FakeComfy().start()
    yield f
    f.stop()


class Gateway:
    def __init__(self, tmp_path, fake: FakeComfy, **settings):
        self.cfg = Config()
        self.cfg.paths.data = str(tmp_path / "data")
        self.cfg.comfy.manage = False
        self.cfg.comfy.port = fake.port
        self.cfg.comfy.warmup = False
        self.cfg.comfy.health_interval_s = 0.2
        self.cfg.server.port = free_port()
        self.node = Node(self.cfg, cpu_pool=ThreadPoolExecutor(2))
        self.node.executor.ready_timeout_s = 5
        for group, values in settings.items():
            self.node.settings.update(group, values)
        self.app = create_app(self.cfg, self.node)
        self.url = f"http://127.0.0.1:{self.cfg.server.port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.cfg.server.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        for _ in range(100):  # supervisor sees the fake as ready
            if self.node.supervisor.ready.is_set():
                break
            time.sleep(0.05)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)

    def key(self, name="test", **kw) -> str:
        k, _ = ApiKeys(self.node.db, self.node.secret).create(name, **kw)
        return k

    def client(self, key: str | None = None, timeout=60) -> httpx.Client:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return httpx.Client(base_url=self.url, headers=headers, timeout=timeout)


@pytest.fixture
def make_gateway(tmp_path, fake):
    made = []

    def make(**settings) -> Gateway:
        fake.run_s, fake.slow_s, fake.oom_left, fake.down = 0.3, 3.0, 0, False
        g = Gateway(tmp_path / f"g{len(made)}", fake, **settings).start()
        made.append(g)
        return g
    yield make
    for g in made:
        g.stop()


@pytest.fixture
def gw(make_gateway):
    return make_gateway()

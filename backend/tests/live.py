"""Helpers for tests that need the demonstration services or a real TCP listener."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import uvicorn

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "demo"
SIMULATOR = DEMO / "novapay" / "simulator" / "app.py"
CLIENT = DEMO / "legacy-client" / "gravepay_client.py"
NOVAPAY_TOKEN = "replacement-demo-token"
LEGACY_KEY = "legacy-demo-key"


def load_simulator() -> ModuleType:
    """A fresh copy of the simulator module, with empty sandboxes."""
    spec = importlib.util.spec_from_file_location("novapay_simulator", SIMULATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def serve(app: Any) -> Iterator[str]:
    """Serve an ASGI app on a free local port. Yields its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False,
                            lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def run_client(base_url: str, key: str = LEGACY_KEY) -> subprocess.CompletedProcess[str]:
    """Run the unchanged legacy client as its own process."""
    env = {**os.environ, "GRAVEPAY_BASE_URL": base_url, "GRAVEPAY_API_KEY": key,
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, str(CLIENT)], capture_output=True, text=True, timeout=120, env=env)

"""Fixtures for the Playwright end-to-end tests (marker ``e2e``; run with ``uv run pytest -m e2e``).

- ``live_server``: the real app (fresh data dir, scheduler off) served by Uvicorn on 127.0.0.1 in a background
  thread; yields an object with ``url`` (``http://localhost:<port>``) and ``app``. ``live_server_sched`` is
  the same with the real scheduler running (tests/e2e/test_flows.py).
- Browser: pytest-playwright's ``browser`` fixture (Chromium). Set ``PLAYWRIGHT_CHROMIUM_EXECUTABLE`` to use
  an already installed Chromium instead of ``playwright install chromium``.
- ``PLANIFICADOR_E2E_SHOTS=<dir>`` saves screenshots there (light + dark).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/e2e/" in str(item.path).replace(os.sep, "/"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if executable:
        return {**browser_type_launch_args, "executable_path": executable}
    return browser_type_launch_args


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class LiveServer:
    url: str
    app: FastAPI
    shots_dir: Path | None

    def shot(self, page: Any, name: str) -> None:
        if self.shots_dir is not None:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(self.shots_dir / f"{name}.png"))


@pytest.fixture
def live_server(data_dir: Path) -> Iterator[LiveServer]:
    yield from _serve(data_dir, scheduler=False)


@pytest.fixture
def live_server_sched(data_dir: Path) -> Iterator[LiveServer]:
    """Like ``live_server`` but with the real background scheduler running (publishing engine + jobs)."""
    yield from _serve(data_dir, scheduler=True)


def _serve(data_dir: Path, *, scheduler: bool) -> Iterator[LiveServer]:
    from planificador.app import create_app
    from planificador.config import load_app_settings

    port = _free_port()
    app = create_app(load_app_settings(data_dir=data_dir, port=port, scheduler_enabled=scheduler))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, name="e2e-uvicorn", daemon=True)
    thread.start()
    url = f"http://localhost:{port}"
    deadline = time.monotonic() + 15
    while True:
        try:
            if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:  # pragma: no cover - startup failure
            raise RuntimeError("Live server did not start")
        time.sleep(0.05)
    shots = os.environ.get("PLANIFICADOR_E2E_SHOTS")
    try:
        yield LiveServer(url=url, app=app, shots_dir=Path(shots) if shots else None)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.state.db.dispose()

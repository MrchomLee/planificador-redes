"""Shared pytest fixtures.

Every test runs with an isolated environment (autouse ``_isolated_env``):

- ``PLANIFICADOR_DATA_DIR`` → a fresh temp dir (``data_dir`` fixture),
- ``PLANIFICADOR_NO_SCHEDULER=1`` (the background scheduler never starts in tests),
- ``PLANIFICADOR_NO_KEYRING=1`` (never touch the developer's real OS keyring; key file fallback is used),
- ``PLANIFICADOR_NO_DESKTOP_NOTIFY=1`` (notifications are stored but never shown as desktop toasts),
- ``PLANIFICADOR_TIMEZONE=off`` (the first brand never takes the PC's time zone; Europe/Madrid as in SPEC),
- module singletons (crypto box, current settings/database, provider registry) reset afterwards.

Main fixtures: ``app`` (``create_app`` with a :class:`ManualClock`), ``client`` (TestClient on
``http://localhost:8765`` so the Host check passes), ``db`` (session on the app database), ``clock``,
``csrf_token``, ``csrf_client`` (adds ``X-CSRF-Token`` to unsafe requests), ``post_csrf``,
``demo_mode`` / ``demo_mode_off``, ``brand`` (active brand) and ``demo_account``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy.orm import Session

BASE_URL = "http://localhost:8765"
DEFAULT_NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)  # Tuesday 10:00 in Europe/Madrid


def _install_templating_fallback() -> None:
    """If the frontend's ``planificador.templating`` is not available yet, install a tiny stand-in."""
    try:
        import planificador.templating  # noqa: F401
    except ImportError:
        module = types.ModuleType("planificador.templating")

        def render(request: Any, name: str, **ctx: Any) -> HTMLResponse:
            title = escape(str(ctx.get("title") or ctx.get("page_title") or name))
            status = int(ctx.get("status_code") or 200)
            return HTMLResponse(f"<!doctype html><title>{title}</title><h1>{title}</h1>", status_code=status)

        module.render = render  # type: ignore[attr-defined]
        module.templates = None  # type: ignore[attr-defined]
        sys.modules["planificador.templating"] = module
        import planificador

        planificador.templating = module  # type: ignore[attr-defined]


_install_templating_fallback()


# ------------------------------------------------------------------------------------------- isolation


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture(autouse=True)
def _isolated_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PLANIFICADOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PLANIFICADOR_NO_SCHEDULER", "1")
    monkeypatch.setenv("PLANIFICADOR_NO_KEYRING", "1")
    monkeypatch.setenv("PLANIFICADOR_NO_BROWSER", "1")
    monkeypatch.setenv("PLANIFICADOR_NO_DESKTOP_NOTIFY", "1")
    # Never adopt the developer's PC time zone for the first brand (tests assume Europe/Madrid).
    monkeypatch.setenv("PLANIFICADOR_TIMEZONE", "off")
    monkeypatch.delenv("PLANIFICADOR_PORT", raising=False)
    yield
    from planificador import config, crypto, db
    from planificador.providers import registry

    crypto.reset()
    config.set_app_settings(None)
    db.set_current_database(None)
    registry.reset()


# ------------------------------------------------------------------------------------------------ app


@pytest.fixture
def clock():
    from planificador.timeutil import ManualClock

    return ManualClock(DEFAULT_NOW)


@pytest.fixture
def app_settings(data_dir: Path):
    from planificador.config import load_app_settings

    return load_app_settings(data_dir=data_dir, scheduler_enabled=False)


@pytest.fixture
def app(app_settings, clock) -> Iterator[FastAPI]:
    from planificador.app import create_app

    application = create_app(app_settings, clock=clock)
    yield application
    application.state.db.dispose()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def db(app: FastAPI) -> Iterator[Session]:
    session = app.state.db.session()
    try:
        yield session
    finally:
        session.close()


# ----------------------------------------------------------------------------------------------- CSRF


def fetch_csrf_token(client: TestClient) -> str:
    """The session's CSRF token (the client keeps the session cookie)."""
    response = client.get("/api/csrf")
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


class CSRFClient:
    """Wraps a TestClient and adds ``X-CSRF-Token`` to POST/PUT/PATCH/DELETE requests."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.token = fetch_csrf_token(client)

    def refresh(self) -> str:
        self.token = fetch_csrf_token(self.client)
        return self.token

    def _headers(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("X-CSRF-Token", self.token)
        return headers

    def get(self, url: str, **kwargs: Any):
        return self.client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.client.post(url, headers=self._headers(kwargs), **kwargs)

    def put(self, url: str, **kwargs: Any):
        return self.client.put(url, headers=self._headers(kwargs), **kwargs)

    def patch(self, url: str, **kwargs: Any):
        return self.client.patch(url, headers=self._headers(kwargs), **kwargs)

    def delete(self, url: str, **kwargs: Any):
        return self.client.delete(url, headers=self._headers(kwargs), **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


@pytest.fixture
def csrf_token(client: TestClient) -> str:
    return fetch_csrf_token(client)


@pytest.fixture
def csrf_client(client: TestClient) -> CSRFClient:
    return CSRFClient(client)


@pytest.fixture
def post_csrf(csrf_client: CSRFClient) -> Callable[..., Any]:
    """``post_csrf(url, data=..., files=..., json=...)`` → response (CSRF header added)."""
    return csrf_client.post


# ----------------------------------------------------------------------------------------- domain data


@pytest.fixture
def demo_mode(db: Session) -> bool:
    from planificador.config import set_setting

    set_setting(db, "demo_mode", True)
    return True


@pytest.fixture
def demo_mode_off(db: Session) -> bool:
    from planificador.config import set_setting

    set_setting(db, "demo_mode", False)
    return False


@pytest.fixture
def brand(db: Session):
    from planificador.deps import ensure_active_brand

    return ensure_active_brand(db)


@pytest.fixture
def demo_account(db: Session, brand):
    from planificador.models import Account, AccountStatus, Network

    account = Account(
        brand_id=brand.id,
        network=Network.DEMO,
        external_id="demo-account-1",
        name="Cuenta Demo",
        username="demo",
        account_type="demo",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens("demo-access-token", "demo-refresh-token")
    db.add(account)
    db.commit()
    return account

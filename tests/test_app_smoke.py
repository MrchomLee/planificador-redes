"""Smoke tests: every page renders through the real app with the shared layout (TestClient, no browser).

These guard the integration contract between the backend core (routes, security, deps) and the frontend shell
(``templating.render``, ``base.html``): pages must answer 200 with the top bar, a CSRF meta tag that matches
the session token, the active brand in the switcher and only static files that exist. Section owners replace
the placeholder pages; these tests must keep passing for the real pages too.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from planificador import config, security
from planificador.deps import ensure_active_brand
from planificador.models import Brand, Notification, utcnow
from planificador.routes import ROUTER_MODULES, render_placeholder

BASE_URL = "http://localhost:8765"

#: Top-level pages (top-bar nav + ☰ menu + notification center) and their nav key.
PAGES: dict[str, str] = {
    "/planificacion": "planificacion",
    "/estadisticas": "estadisticas",
    "/comentarios": "comentarios",
    "/biblioteca": "biblioteca",
    "/conexiones": "conexiones",
    "/marcas": "marcas",
    "/importar": "importar",
    "/ajustes": "ajustes",
    "/notificaciones": "notificaciones",
}

#: Parameterless GET routes that are not full HTML pages (JSON, redirects, fragments, files).
NON_PAGE_ROUTES = {
    "/",
    "/login",
    "/healthz",
    "/favicon.ico",
    "/api/csrf",
    "/api/notificaciones/resumen",
}

STATIC_REF_RE = re.compile(r"""(?:src|href|data-sprite|data-favicon)="(/static/[^"?#]+)""")
CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]*)">')
FLASH_RE = re.compile(r'<script type="application/json" id="flash-data">(.*?)</script>', re.S)


def fetch_csrf_token(client: TestClient) -> str:
    return client.get("/api/csrf").json()["csrf_token"]


def _csrf_meta(html: str) -> str:
    match = CSRF_META_RE.search(html)
    assert match, "csrf-token meta tag missing"
    return match.group(1)


def _assert_full_page(html: str, path: str) -> None:
    assert html.lstrip().lower().startswith("<!doctype html>"), path
    assert '<html lang="es">' in html, path
    assert 'class="topbar"' in html, f"{path}: top bar missing"
    assert 'id="modal-root"' in html and 'id="drawer-root"' in html and 'id="toast-root"' in html, path
    assert "Nueva publicación" in html, path


def _parameterless_get_routes() -> list[str]:
    """GET paths without parameters from every router module (``routes.ROUTER_MODULES`` + auth router).

    Read from the routers themselves: FastAPI >= 0.14x wraps included routers in a private type, so
    ``app.routes`` no longer lists their routes. Our routers use no prefixes.
    """
    routers = [importlib.import_module(f"planificador.routes.{name}").router for name in ROUTER_MODULES]
    routers.append(security.router)
    paths = set()
    for router in routers:
        for route in router.routes:
            if isinstance(route, APIRoute) and "GET" in route.methods and "{" not in route.path:
                paths.add(route.path)
    return sorted(paths)


def _flash_messages(html: str) -> list[str]:
    match = FLASH_RE.search(html)
    assert match, "flash-data script missing"
    return [f["message"] for f in json.loads(match.group(1))]


# ------------------------------------------------------------------------------------------------ pages


@pytest.mark.parametrize("path", list(PAGES))
def test_top_level_page_renders_with_layout(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    _assert_full_page(html, path)
    # CSRF token exposed to htmx/app.js and equal to the session token used by the middleware.
    assert _csrf_meta(html) == fetch_csrf_token(client)
    # Navigation highlights the current section (main nav or ☰ menu).
    nav_key = PAGES[path]
    assert f'href="{path}" class="mainnav__link is-active"' in html or (
        f'class="dropdown__item is-active" role="menuitem" href="{path}"' in html
    ), f"{path}: nav item for {nav_key!r} not active"
    # The active brand (auto-created on first run) appears in the switcher.
    assert 'class="brand-switcher__name">Mi marca<' in html
    assert "Sin marca" not in html


def test_every_parameterless_get_route_answers(client: TestClient) -> None:
    routes = _parameterless_get_routes()
    assert set(PAGES) <= set(routes), "a top-level page route is missing"
    for path in routes:
        r = client.get(path, follow_redirects=False)
        assert r.status_code < 500, f"{path}: {r.status_code}"
        if path in NON_PAGE_ROUTES or r.status_code != 200:
            continue
        if r.headers.get("content-type", "").startswith("text/html") and "<html" in r.text[:500].lower():
            _assert_full_page(r.text, path)
            assert _csrf_meta(r.text), path


def test_home_redirects_to_planner(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"].endswith("/planificacion")


def test_not_found_page(client: TestClient) -> None:
    r = client.get("/no-existe", headers={"accept": "text/html"})
    assert r.status_code == 404
    _assert_full_page(r.text, "/no-existe")
    assert "404" in r.text
    # API-style clients get JSON, not the HTML page.
    r = client.get("/no-existe", headers={"accept": "application/json"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_static_files_referenced_by_layout_exist(client: TestClient) -> None:
    html = client.get("/planificacion").text
    refs = set(STATIC_REF_RE.findall(html))
    expected = {
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/vendor/htmx.org/htmx.min.js",
        "/static/vendor/alpinejs/cdn.min.js",
        "/static/vendor/lucide-static/sprite.svg",
        "/static/img/favicon.svg",
    }
    assert expected <= refs, expected - refs
    for ref in sorted(refs):
        r = client.get(ref)
        assert r.status_code == 200, ref
        assert r.content, ref


@pytest.mark.parametrize(
    "path",
    [
        "/static/vendor/fullcalendar/index.global.min.js",
        "/static/vendor/fullcalendar/locales/es.global.min.js",
        "/static/vendor/chart.js/chart.umd.min.js",
        "/static/vendor/simple-icons/facebook.svg",
        "/static/vendor/simple-icons/instagram.svg",
        "/static/vendor/simple-icons/tiktok.svg",
        "/static/img/logo.svg",
    ],
)
def test_vendored_assets_served(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200 and len(r.content) > 100, path


def test_page_uses_active_brand_timezone(client: TestClient, db: Session) -> None:
    brand = ensure_active_brand(db)
    brand.timezone = "America/Mexico_City"
    db.commit()
    html = client.get("/estadisticas").text
    assert f'data-brand-id="{brand.id}"' in html
    assert 'data-brand-tz="America/Mexico_City"' in html


def test_demo_mode_ribbon(client: TestClient, db: Session) -> None:
    assert "Modo prueba activo" not in client.get("/planificacion").text
    config.set_setting(db, "demo_mode", True)
    assert "Modo prueba activo" in client.get("/planificacion").text


# ----------------------------------------------------------------------------------------- brand switch


def _second_brand(db: Session) -> tuple[Brand, Brand]:
    first = ensure_active_brand(db)
    other = Brand(name="Cafetería Aurora", color="#e8590c", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    return first, other


def test_brand_switcher_lists_brands_and_switches(csrf_client: Any, db: Session) -> None:
    first, other = _second_brand(db)
    html = csrf_client.get("/biblioteca").text
    assert f'action="/marcas/{first.id}/activar"' in html
    assert f'action="/marcas/{other.id}/activar"' in html

    r = csrf_client.post(f"/marcas/{other.id}/activar", data={"next": "/biblioteca"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/biblioteca"
    db.expire_all()
    assert config.get_setting(db, "active_brand_id") == other.id

    html = csrf_client.get("/biblioteca").text
    assert 'class="brand-switcher__name">Cafetería Aurora<' in html
    assert _flash_messages(html) == ["Ahora trabajas con la marca «Cafetería Aurora»."]  # → toast
    # The flash is shown once.
    assert _flash_messages(csrf_client.get("/biblioteca").text) == []


def test_brand_switch_via_form_field_token(client: TestClient, db: Session) -> None:
    _, other = _second_brand(db)
    token = _csrf_meta(client.get("/planificacion").text)
    r = client.post(
        f"/marcas/{other.id}/activar",
        data={"csrf_token": token, "next": "/estadisticas"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/estadisticas"


def test_brand_switch_security_and_errors(client: TestClient, csrf_client: Any, db: Session) -> None:
    first, other = _second_brand(db)
    # No CSRF token → 403, nothing changes.
    r = client.post(f"/marcas/{other.id}/activar", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 403
    db.expire_all()
    assert config.get_setting(db, "active_brand_id") == first.id
    # Open redirects are neutralised.
    for evil in ("//evil.example/x", "https://evil.example", "/\\evil.example", "/ok\r\nSet-Cookie: x=1"):
        r = csrf_client.post(f"/marcas/{other.id}/activar", data={"next": evil}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/planificacion", evil
    # Unknown or archived brand → 404.
    assert csrf_client.post("/marcas/9999/activar", data={"next": "/"}).status_code == 404
    other.archived_at = utcnow()
    db.commit()
    assert csrf_client.post(f"/marcas/{other.id}/activar", data={"next": "/"}).status_code == 404
    # htmx callers get HX-Redirect instead of a 303.
    r = csrf_client.post(
        f"/marcas/{first.id}/activar", data={"next": "/comentarios"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/comentarios"


# -------------------------------------------------------------------------------------- notifications


def test_notification_summary_empty(client: TestClient) -> None:
    r = client.get("/api/notificaciones/resumen", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()
    assert 'id="notif-list" hx-swap-oob="innerHTML"' in r.text
    assert "notif__badge" not in r.text
    assert "No tienes avisos pendientes." in r.text
    assert r.headers.get("cache-control") == "no-store"


def test_notification_summary_lists_latest(client: TestClient, db: Session) -> None:
    now = utcnow()
    db.add_all(
        [
            Notification(
                kind="info",
                title="Aviso antiguo",
                body="Leído",
                created_at=now - timedelta(days=2),
                read_at=now - timedelta(days=1),
            ),
            Notification(
                kind="error",
                title="Error al publicar",
                body="Detalle <b>sin HTML</b>",
                created_at=now - timedelta(minutes=5),
            ),
            Notification(kind="manual_publish", title="Publicación asistida", created_at=now),
        ]
    )
    db.commit()
    r = client.get("/api/notificaciones/resumen", headers={"HX-Request": "true"})
    assert r.status_code == 200
    html = r.text
    # Visible number hidden from assistive tech; the bell button is described by the sr-only text.
    badge = '<span class="notif__badge" aria-hidden="true">2</span><span class="sr-only">2 sin leer</span>'
    assert badge in html
    listing = html[html.index('id="notif-list"') :]
    assert listing.index("Publicación asistida") < listing.index("Error al publicar") < listing.index("Aviso")
    assert listing.count("notif-item is-unread") == 2
    assert "&lt;b&gt;sin HTML&lt;/b&gt;" in html  # user text is escaped
    assert "data-notify-items=" in html
    # The page badge uses the same unread count.
    page = client.get("/planificacion").text
    assert '<span class="sr-only">2 sin leer</span>' in page
    assert 'aria-describedby="notif-badge"' in page


# ---------------------------------------------------------------------------------------------- layers


def test_new_post_button_opens_modal_fragment(client: TestClient) -> None:
    page = client.get("/planificacion").text
    assert 'hx-get="/publicaciones/nueva" hx-target="#modal-root"' in page
    r = client.get("/publicaciones/nueva", headers={"HX-Request": "true", "HX-Target": "modal-root"})
    assert r.status_code == 200
    body = r.text.lstrip().lower()
    assert not body.startswith("<!doctype") and "<html" not in body, "full page returned into the modal"
    assert 'role="dialog"' in r.text
    # Without htmx it is a normal page.
    _assert_full_page(client.get("/publicaciones/nueva").text, "/publicaciones/nueva")


def test_placeholder_layers(app: FastAPI, client: TestClient) -> None:
    """``render_placeholder`` answers htmx requests aimed at a layer root with a modal/drawer fragment."""

    @app.get("/__test/placeholder")
    def placeholder(request: Request) -> Response:
        return render_placeholder(request, "Sección <de prueba>")

    def get(target: str | None) -> str:
        headers = {"HX-Request": "true", "HX-Target": target} if target else {}
        r = client.get("/__test/placeholder", headers=headers)
        assert r.status_code == 200
        return r.text

    modal = get("modal-root")
    assert 'class="modal modal--md"' in modal and "<html" not in modal.lower()
    assert "Sección &lt;de prueba&gt;" in modal and "data-close" in modal
    drawer = get("drawer-root")
    assert 'class="drawer drawer--md"' in drawer and "<html" not in drawer.lower()
    # Other htmx targets and plain navigation get the full page.
    _assert_full_page(get("main"), "/__test/placeholder")
    _assert_full_page(get(None), "/__test/placeholder")


# ----------------------------------------------------------------------------------------------- login


def test_login_page_when_password_set(client: TestClient, db: Session) -> None:
    assert client.get("/login", follow_redirects=False).status_code == 303  # no password → no login page
    security.set_password(db, "clave-de-prueba")
    for path in PAGES:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), path
    r = client.get("/login?next=/biblioteca")
    assert r.status_code == 200
    html = r.text
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert 'class="topbar"' not in html  # no app chrome before logging in
    token = _csrf_meta(html)
    assert token and f'name="csrf_token" value="{token}"' in html
    assert 'name="next" value="/biblioteca"' in html

    r = client.post(
        "/login",
        data={"password": "clave-de-prueba", "next": "/biblioteca", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/biblioteca"
    html = client.get("/biblioteca").text
    assert "Cerrar sesión" in html
    security.set_password(db, None)


def test_pages_render_on_second_app_instance(app_settings, tmp_path) -> None:
    """Template globals read the database of the app serving the request, not a module-level one."""
    from planificador.app import create_app
    from planificador.config import load_app_settings

    first = create_app(app_settings)
    second = create_app(load_app_settings(data_dir=tmp_path / "otra", scheduler_enabled=False))
    try:
        with first.state.db.session_scope() as s:
            ensure_active_brand(s).name = "Marca uno"
        with TestClient(first, base_url=BASE_URL) as c:
            assert 'class="brand-switcher__name">Marca uno<' in c.get("/planificacion").text
    finally:
        first.state.db.dispose()
        second.state.db.dispose()


def test_default_brand_created_once_under_concurrency(app: FastAPI) -> None:
    """First run: concurrent requests (page + bell, two tabs) must not create two default brands."""
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import func, select

    def work(_: int) -> int:
        with app.state.db.session() as s:
            return ensure_active_brand(s).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = set(pool.map(work, range(16)))
    with app.state.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Brand)) == 1
    assert len(ids) == 1


def test_lifespan_wires_the_public_media_service(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan creates ``app.state.public_media`` (used by the engine for Instagram photos) and stops it
    at shutdown; with public media off nothing is started."""
    from planificador import publicmedia

    stopped: list[bool] = []
    monkeypatch.setattr(publicmedia.PublicMediaService, "stop", lambda self: stopped.append(True))
    assert app.state.public_media is None
    with TestClient(app, base_url="http://localhost:8765") as c:
        assert c.get("/healthz").status_code == 200
        service = app.state.public_media
        assert isinstance(service, publicmedia.PublicMediaService)
        assert service.mode() == "off" and not service.server.running
    assert stopped == [True]

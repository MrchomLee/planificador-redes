"""Browser smoke test of the application shell (layout, top bar, layers, security) against a live server.

Run: ``uv run pytest -m e2e`` (needs Chromium: ``uv run playwright install chromium`` or
``PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome``).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from planificador import security
from planificador.deps import ensure_active_brand
from planificador.models import Brand, Notification

pytestmark = pytest.mark.e2e
LiveServer = Any  # tests/e2e/conftest.py::LiveServer (tests/ is not a package)

PAGES = [
    "/planificacion",
    "/estadisticas",
    "/comentarios",
    "/biblioteca",
    "/conexiones",
    "/marcas",
    "/importar",
    "/ajustes",
    "/notificaciones",
]
SCHEMES = ["light", "dark"]


class PageLog:
    """Console errors, uncaught exceptions and failed/4xx static requests of one page."""

    def __init__(self, page: Any) -> None:
        self.errors: list[str] = []
        self.failed: list[str] = []
        page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("requestfailed", lambda r: self.failed.append(f"{r.url} {r.failure}"))
        page.on(
            "response",
            lambda r: r.status >= 400 and "/static/" in r.url and self.failed.append(f"{r.status} {r.url}"),
        )

    def assert_clean(self, where: str) -> None:
        assert not self.errors, f"{where}: console errors {self.errors}"
        assert not self.failed, f"{where}: failed requests {self.failed}"


def _new_page(browser: Any, scheme: str, width: int = 1440, height: int = 900) -> Any:
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    return context.new_page()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_pages_load_cleanly(browser: Any, live_server: LiveServer, scheme: str) -> None:
    page = _new_page(browser, scheme)
    log = PageLog(page)
    for path in PAGES:
        with page.expect_response(lambda r: "/api/notificaciones/resumen" in r.url) as bell:
            response = page.goto(live_server.url + path, wait_until="networkidle")
        assert response is not None and response.status == 200, path
        assert bell.value.status == 200, f"{path}: bell endpoint {bell.value.status}"
        token = page.get_attribute('meta[name="csrf-token"]', "content")
        assert token and len(token) > 20, f"{path}: csrf meta"
        assert page.evaluate("() => !!(window.Alpine && window.htmx && window.App)"), path
        assert page.text_content(".brand-switcher__name").strip() == "Mi marca", path
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        ), f"{path}: horizontal scroll"
        log.assert_clean(path)
    live_server.shot(page, f"e2e-ajustes-{scheme}")
    page.context.close()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_brand_switcher_bell_and_modal(browser: Any, live_server: LiveServer, scheme: str) -> None:
    with live_server.app.state.db.session_scope() as db:
        ensure_active_brand(db)
        db.add(Brand(name="Cafetería Aurora", color="#e8590c", timezone="America/Mexico_City"))
        db.add(Notification(kind="error", title="Error al publicar en Facebook", body="Detalle"))
        db.add(Notification(kind="info", title="Cuenta conectada", body=""))

    page = _new_page(browser, scheme)
    log = PageLog(page)
    page.goto(live_server.url + "/estadisticas", wait_until="networkidle")

    # Brand switcher: open, pick the other brand, come back to the same page with a toast.
    page.click(".brand-switcher__btn")
    menu = page.locator(".brand-switcher .dropdown__menu")
    menu.wait_for(state="visible")
    assert menu.locator("form button").count() == 2
    with page.expect_navigation(wait_until="networkidle"):
        menu.locator('form button[aria-checked="false"]').first.click()
    assert page.url.endswith("/estadisticas")
    assert page.text_content(".brand-switcher__name").strip() == "Cafetería Aurora"
    assert page.get_attribute("body", "data-brand-tz") == "America/Mexico_City"
    assert "Cafetería Aurora" in page.locator("#toast-root .toast").first.text_content()

    # Notifications bell: badge + list loaded on open.
    page.locator("#notif-badge .notif__badge").wait_for()
    assert page.text_content("#notif-badge .notif__badge").strip() == "2"
    with page.expect_response(lambda r: "/api/notificaciones/resumen" in r.url) as bell:
        page.click(".notif__btn")
    assert bell.value.status == 200
    page.locator("#notif-list .notif-item").first.wait_for()
    assert page.locator("#notif-list .notif-item").count() == 2
    live_server.shot(page, f"e2e-bell-{scheme}")
    page.keyboard.press("Escape")

    # "Nueva publicación" opens the composer as a full-screen overlay in #modal-root (doc 05 §3; button and
    # shortcut N); Esc and "Cancelar" close it.
    composer = page.locator("#modal-root .composer--overlay")
    page.click("#btn-new-post")
    composer.wait_for(state="visible")
    assert page.url.endswith("/estadisticas")
    live_server.shot(page, f"e2e-modal-{scheme}")
    page.keyboard.press("Escape")
    composer.wait_for(state="detached")
    page.keyboard.press("n")
    composer.wait_for(state="visible")
    composer.locator(".cmp-cancel").click()
    composer.wait_for(state="detached")
    log.assert_clean("interactions")

    # POST without the CSRF token is refused even from our own origin; with it, it passes.
    statuses = page.evaluate(
        """async () => {
          const opts = (token) => ({method: "POST", redirect: "manual",
            headers: Object.assign({"Content-Type": "application/x-www-form-urlencoded"},
                                   token ? {"X-CSRF-Token": token} : {}),
            body: "next=/estadisticas"});
          const token = document.querySelector('meta[name="csrf-token"]').content;
          const a = await fetch("/marcas/1/activar", opts(null));
          const b = await fetch("/marcas/1/activar", opts(token));
          return [a.status, b.type];
        }"""
    )
    assert statuses == [403, "opaqueredirect"]
    page.context.close()


def test_host_header_and_csrf_over_real_http(live_server: LiveServer) -> None:
    port = live_server.url.rsplit(":", 1)[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as c:
        assert c.get("/planificacion", headers={"Host": "evil.example"}).status_code == 400
        assert c.get("/planificacion", headers={"Host": f"localhost:{port}.evil.example"}).status_code == 400
        r = c.get("/planificacion", headers={"Host": f"127.0.0.1:{port}"})
        assert r.status_code == 307 and r.headers["location"].startswith(live_server.url)
        r = c.post("/marcas/1/activar", headers={"Host": f"localhost:{port}"}, data={"next": "/"})
        assert r.status_code == 403
        r = c.post(
            "/marcas/1/activar",
            headers={"Host": f"localhost:{port}", "Origin": "http://evil.example", "X-CSRF-Token": "x"},
        )
        assert r.status_code == 403


@pytest.mark.parametrize("scheme", SCHEMES)
def test_login_flow(browser: Any, live_server: LiveServer, scheme: str) -> None:
    with live_server.app.state.db.session_scope() as db:
        security.set_password(db, "secreta-123", commit=False)
    page = _new_page(browser, scheme)
    log = PageLog(page)
    response = page.goto(live_server.url + "/biblioteca", wait_until="networkidle")
    assert response is not None and response.status == 200
    assert "/login?next=%2Fbiblioteca" in page.url
    live_server.shot(page, f"e2e-login-{scheme}")
    page.fill('input[name="password"]', "incorrecta")
    with page.expect_navigation():
        page.click('button[type="submit"]')
    assert "incorrecta" in page.text_content(".form-field__error")
    page.fill('input[name="password"]', "secreta-123")
    with page.expect_navigation(url="**/biblioteca"):
        page.click('button[type="submit"]')
    page.click('button[aria-label="Menú"]')
    with page.expect_navigation(url="**/login"):
        page.click("text=Cerrar sesión")
    # The wrong password answers 401 (the browser logs that as a failed resource).
    log.errors = [e for e in log.errors if "401" not in e]
    log.assert_clean("login")
    page.context.close()

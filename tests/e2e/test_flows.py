"""End-to-end user flows in a real browser against the real app **with the scheduler running** (SPEC §16).

Each test starts a fresh app (``live_server_sched``: Uvicorn + lifespan + background scheduler, temp data dir)
and drives Chromium through a complete flow, failing on any console error, uncaught exception, failed request
or 4xx/5xx response (static files, htmx and fetch calls alike):

1. Ajustes → modo prueba; Conexiones → conectar Demo; Biblioteca → subir foto + vídeo; composer (Demo + the
   Facebook/Instagram sections) → programar a 2 minutos; semana → arrastrar; ficha → «Publicar ahora» →
   «Publicado» through the real scheduler.
2. Publicación asistida: Autopublicar off + «Publicar ahora» → aviso → modal (copiar, .zip, marcar publicado).
3. Ideas: crear, mover en el tablero, papelera, restaurar.
4. Estadísticas con datos demo y gráficos; Comentarios con datos demo y una respuesta.
5. Ajustes (guardar) + CSV: plantilla y importación de 2 filas.

Run: ``PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome uv run pytest -m e2e tests/e2e/test_flows.py``.
No real network: Facebook/Instagram accounts are seeded with offline providers (only their UI is exercised).
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import av
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from planificador import config, media
from planificador.deps import ensure_active_brand
from planificador.models import Account, AccountStatus, Network, Post, PostTarget, TargetStatus
from planificador.providers import registry
from planificador.providers.base import AccountSnapshot, ErrorKind, ProviderError
from planificador.providers.facebook import FacebookProvider
from planificador.providers.instagram import InstagramProvider

pytestmark = pytest.mark.e2e
LiveServer = Any  # tests/e2e/conftest.py::LiveServer

PHOTO = "foto-prueba.jpg"
VIDEO = "video-prueba.mp4"


# ------------------------------------------------------------------------------------------ helpers


class PageLog:
    """Console errors, uncaught exceptions, failed requests and every 4xx/5xx response of one page."""

    def __init__(self, page: Any, *, allow: tuple[str, ...] = ()) -> None:
        self.errors: list[str] = []
        self.bad: list[str] = []
        self.allow = allow
        page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("requestfailed", self._failed)
        page.on("response", self._response)

    def _failed(self, request: Any) -> None:
        failure = str(request.failure or "")
        # Downloads and navigations aborted on purpose by the browser are not errors.
        if "ERR_ABORTED" in failure and (request.resource_type == "document" or "download" in request.url):
            return
        self.bad.append(f"FAILED {request.method} {request.url} {failure}")

    def _response(self, response: Any) -> None:
        if response.status >= 400 and not any(a in response.url for a in self.allow):
            self.bad.append(f"{response.status} {response.request.method} {response.url}")

    def assert_clean(self, where: str) -> None:
        assert not self.errors, f"{where}: console errors {self.errors}"
        assert not self.bad, f"{where}: bad requests {self.bad}"


def new_page(browser: Any, live: LiveServer, scheme: str = "light") -> Any:
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, color_scheme=scheme, accept_downloads=True
    )
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live.url)
    context.set_default_timeout(15_000)
    return context.new_page()


def wait_until(predicate: Callable[[], bool], timeout: float = 30.0, every: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(every)
    return predicate()


def make_media(folder: Path) -> tuple[Path, Path]:
    """A 1080×1350 JPEG and a 2 s 540×960 H.264 MP4 (generated, no fixtures on disk)."""
    folder.mkdir(parents=True, exist_ok=True)
    photo = folder / PHOTO
    img = Image.new("RGB", (1080, 1350), (91, 76, 240))
    draw = ImageDraw.Draw(img)
    for x in range(0, 1080, 60):
        draw.rectangle([x, 0, x + 30, 1350], fill=(150, 90, 200))
    draw.ellipse([340, 475, 740, 875], fill=(255, 210, 80))
    img.save(photo, quality=88)
    video = folder / VIDEO
    with av.open(str(video), mode="w") as container:
        stream = container.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 540, 960, "yuv420p"
        for i in range(60):
            frame_img = Image.new("RGB", (540, 960), ((i * 4) % 255, 80, 200))
            ImageDraw.Draw(frame_img).rectangle([200, 400 + i * 3, 340, 480 + i * 3], fill=(255, 255, 255))
            for packet in stream.encode(av.VideoFrame.from_image(frame_img)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return photo, video


class _Offline:
    """Mixin: the network-facing parts of a real provider, answered offline (the UI is what is tested)."""

    def check_account(self, account, settings, http) -> None:
        return None

    def fetch_account_snapshot(self, account, settings, http, day):
        return AccountSnapshot(day=day, metrics={})

    def fetch_recent_posts(self, account, settings, http, since):
        return []

    def fetch_comments(self, account, settings, http, since):
        return []

    def publish_step(self, ctx):
        raise ProviderError(ErrorKind.PERMANENT, "offline", "Sin red en las pruebas end-to-end.")


class OfflineFacebook(_Offline, FacebookProvider):
    pass


class OfflineInstagram(_Offline, InstagramProvider):
    pass


def seed_meta_accounts(live: LiveServer) -> None:
    """Facebook Page + Instagram business account on the active brand (fake tokens, offline providers)."""
    registry.register(OfflineFacebook())
    registry.register(OfflineInstagram())
    with live.app.state.db.session_scope() as db:
        brand = ensure_active_brand(db)
        for network, external_id, username, account_type in (
            (Network.FACEBOOK, "111", None, "page"),
            (Network.INSTAGRAM, "17841", "cafe.aurora", "business"),
        ):
            account = Account(
                brand_id=brand.id,
                network=network,
                external_id=external_id,
                name="Cafetería Aurora",
                username=username,
                account_type=account_type,
                status=AccountStatus.CONNECTED,
                meta={"page_id": "111"},
            )
            account.set_tokens("fake-page-token", None)
            db.add(account)


def setup_demo(live: LiveServer, folder: Path | None = None) -> dict[str, int]:
    """Demo mode + Demo account (+ the generated media in the library) straight in the database."""
    ids: dict[str, int] = {}
    with live.app.state.db.session_scope() as db:
        brand = ensure_active_brand(db)
        config.set_setting(db, "demo_mode", True, commit=False)
        account = Account(
            brand_id=brand.id,
            network=Network.DEMO,
            external_id=f"demo-{brand.id}",
            name=f"{brand.name} (Demo)",
            username="demo",
            account_type="demo",
            status=AccountStatus.CONNECTED,
            meta={"demo": True},
        )
        account.set_tokens("demo-access", "demo-refresh")
        db.add(account)
        db.flush()
        ids["account"] = account.id
        ids["brand"] = brand.id
    if folder is not None:
        for path in make_media(folder):
            with live.app.state.db.session_scope() as db, path.open("rb") as fh:
                ids[path.name] = media.ingest(db, fh, path.name, ids["brand"]).id
    return ids


def brand_tz(page: Any) -> ZoneInfo:
    return ZoneInfo(page.get_attribute("body", "data-brand-tz"))


def open_composer(page: Any, opener: str = "#btn-new-post") -> Any:
    page.locator(opener).first.click()
    return composer_ready(page)


def composer_ready(page: Any) -> Any:
    """The composer overlay once its Alpine component is initialised (clicks before that are lost)."""
    page.locator("#modal-root .composer--overlay.is-ready").wait_for(state="visible")
    return page.locator("#modal-root .composer--overlay")


def select_network(composer: Any, network: str, on: bool = True) -> None:
    """Select (or deselect) a network in the composer's logo row. Idempotent: with a single connected account
    the composer preselects it."""
    logo = composer.locator(f".cmp-net--{network} .cmp-net__logo")
    expected = "true" if on else "false"
    if logo.get_attribute("aria-pressed") != expected:
        logo.click()
    composer.locator(f".cmp-net--{network} .cmp-net__logo[aria-pressed='{expected}']").wait_for()


def pick_media(page: Any, composer: Any, *names: str) -> None:
    composer.locator(".cmp-tool[aria-label='Añadir fotos o vídeos']").click()
    picker = page.locator("#media-picker")
    picker.wait_for(state="visible")
    for name in names:
        picker.locator(".media-card--pick").filter(has_text=name).click()
    picker.locator("[data-picker-confirm]").click()
    picker.wait_for(state="detached")
    composer.locator(".cmp-thumb:not(.cmp-thumb--add)").nth(len(names) - 1).wait_for()


def set_date(composer: Any, when: datetime) -> None:
    composer.locator(".cmp-date__btn").click()
    composer.locator(".cmp-pop--date input[type=date]").fill(when.strftime("%Y-%m-%d"))
    composer.locator(".cmp-pop--date input[type=time]").fill(when.strftime("%H:%M"))
    composer.locator(".cmp-pop--date button", has_text="Listo").click()


def save_composer(page: Any, composer: Any, *, publish_now: bool = False) -> int:
    """Primary action (Programar / Guardar como idea) or «Publicar ahora»; returns the post id."""

    def is_save(r: Any) -> bool:
        return r.request.method == "POST" and r.url.split("?")[0].rstrip("/").endswith("/publicaciones")

    with page.expect_response(is_save) as saved:
        if publish_now:
            composer.locator(".cmp-split__caret").click()
            composer.locator(".cmp-split__menu button", has_text="Publicar ahora").click()
            page.locator("#confirm-root button", has_text="Publicar ahora").click()
        else:
            composer.locator(".cmp-split__main").click()
    assert saved.value.status == 200, saved.value.text()
    composer.wait_for(state="detached")
    return int(json.loads(saved.value.headers["hx-trigger"])["posts:changed"]["post_id"])


def target_status(live: LiveServer, post_id: int) -> list[str]:
    with live.app.state.db.session_scope() as db:
        rows = db.scalars(select(PostTarget.status).where(PostTarget.post_id == post_id)).all()
        return [str(s) for s in rows]


def refresh_bell(page: Any) -> None:
    page.evaluate("() => window.htmx.trigger(document.body, 'notifications:refresh')")


# ------------------------------------------------------------------------------------------- flows


def test_setup_library_composer_drag_and_publish_now(browser: Any, live_server_sched: LiveServer, tmp_path):
    live = live_server_sched
    seed_meta_accounts(live)
    photo, video = make_media(tmp_path / "media")
    page = new_page(browser, live)
    log = PageLog(page)

    # Ajustes → modo prueba.
    page.goto(live.url + "/ajustes", wait_until="networkidle")
    assert not page.is_checked("#sw-demo-mode")
    page.locator("label:has(#sw-demo-mode)").first.click()
    with page.expect_response(lambda r: "/ajustes/publicacion" in r.url) as saved:
        page.locator("#publicacion button[type=submit]").click()
    assert saved.value.ok
    page.reload(wait_until="networkidle")
    assert page.is_checked("#sw-demo-mode")
    page.locator(".demo-ribbon").wait_for()

    # Conexiones → conectar la cuenta Demo.
    page.goto(live.url + "/conexiones", wait_until="networkidle")
    with page.expect_navigation(wait_until="networkidle"):
        page.locator("form[action='/conexiones/demo/conectar'] button").first.click()
    assert "Cuenta Demo conectada" in page.locator("#toast-root").inner_text()
    log.assert_clean("ajustes + conexiones")

    # Biblioteca → subir una foto y un vídeo.
    page.goto(live.url + "/biblioteca", wait_until="networkidle")
    with page.expect_response(lambda r: "/biblioteca/subir" in r.url) as uploaded:
        page.set_input_files("[data-upload-input]", [str(photo), str(video)])
    assert uploaded.value.ok
    cards = page.locator("#media-results .media-card")
    assert wait_until(lambda: cards.count() == 2, 20)
    assert page.locator("#media-results").get_by_text("0:02").count() == 1  # video duration badge
    live.shot(page, "flow-biblioteca")
    log.assert_clean("biblioteca")

    # Composer: Demo + the Facebook and Instagram sections; schedule for Demo 2 minutes ahead.
    page.goto(live.url + "/planificacion", wait_until="networkidle")
    tz = brand_tz(page)
    composer = open_composer(page)
    for net in ("demo", "facebook", "instagram"):
        select_network(composer, net)
    for net, label in (("facebook", "Facebook"), ("instagram", "Instagram")):
        section = composer.locator(f"section.cmp-sec--{net}")
        section.wait_for(state="visible")
        assert f"Configuración de {label}" in section.locator(".cmp-sec__toggle").inner_text()
    composer.locator(".cmp-pernet__btn").wait_for(state="visible")  # "Editar por red social" (≥ 2 networks)
    live.shot(page, "flow-composer-three-networks")
    for net in ("facebook", "instagram"):
        select_network(composer, net, on=False)
        composer.locator(f"section.cmp-sec--{net}").wait_for(state="hidden")
    composer.locator("#cmp-text").fill("Primera publicación de prueba desde el planificador 🎉 #demo")
    pick_media(page, composer, PHOTO)
    when = (datetime.now(tz) + timedelta(minutes=2)).replace(second=0, microsecond=0)
    set_date(composer, when)
    assert composer.locator(".cmp-split__main").is_enabled()
    post_id = save_composer(page, composer)
    assert target_status(live, post_id) == ["scheduled"]
    log.assert_clean("composer")

    # Week view shows it; drag it to the same time on the next day.
    day_param = when.date().isoformat()
    page.goto(f"{live.url}/planificacion?vista=semana&fecha={day_param}", wait_until="networkidle")
    event = page.locator(".fc-timegrid-event", has_text="Primera publicación de prueba")
    event.wait_for()
    tomorrow = when.date() + timedelta(days=1)
    column = page.locator(f".fc-timegrid-col[data-date='{tomorrow.isoformat()}']")
    month = column.count() == 0
    if month:  # Sunday: the week ends here → drag in the month grid instead
        page.goto(f"{live.url}/planificacion?vista=mes&fecha={day_param}", wait_until="networkidle")
        event = page.locator(".fc-daygrid-event", has_text="Primera publicación de prueba")
        column = page.locator(f".fc-daygrid-day[data-date='{tomorrow.isoformat()}']")
    event.scroll_into_view_if_needed()
    start, target = event.bounding_box(), column.bounding_box()
    x0, y0 = start["x"] + start["width"] / 2, start["y"] + min(10, start["height"] / 2)
    # Drop in the middle of the *visible* part of the next day's column/cell: any time tomorrow is valid, and
    # staying away from the viewport edges avoids FullCalendar's auto-scroll (late-evening posts sit at the
    # bottom of the grid).
    viewport = page.viewport_size
    top, bottom = max(target["y"], 0), min(target["y"] + target["height"], viewport["height"])
    x1, y1 = target["x"] + target["width"] / 2, (top + bottom) / 2
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x0 + 10, y0 + 2, steps=4)
    with page.expect_response(lambda r: "/reprogramar" in r.url) as moved:
        page.mouse.move(x1, y1, steps=15)
        page.mouse.up()
    assert moved.value.ok, moved.value.text()
    assert moved.value.json()["event"]["start"].startswith(tomorrow.isoformat())
    live.shot(page, "flow-calendar-dragged")
    with live.app.state.db.session_scope() as db:
        assert db.get(Post, post_id).scheduled_at.astimezone(tz).date() == tomorrow

    # Drawer → «Publicar ahora» → the real scheduler publishes it; the open drawer follows the status.
    page.locator(".fc-event", has_text="Primera publicación de prueba").first.click()
    drawer = page.locator("#drawer-root #post-drawer")
    drawer.wait_for(state="visible")
    assert "Programado" in drawer.locator(".pd-header__meta").inner_text()
    drawer.locator("button", has_text="Publicar ahora").click()
    page.locator("#confirm-root button", has_text="Publicar ahora").click()
    assert wait_until(lambda: target_status(live, post_id) == ["published"], 30), target_status(live, post_id)
    page.locator("#post-drawer .pd-header__meta .badge", has_text="Publicado").wait_for(timeout=20_000)
    live.shot(page, "flow-drawer-published")
    log.assert_clean("publish now")
    page.context.close()


def test_assisted_publication_notification_and_modal(browser: Any, live_server_sched: LiveServer, tmp_path):
    live = live_server_sched
    setup_demo(live, tmp_path / "media")
    page = new_page(browser, live)
    log = PageLog(page)
    page.goto(live.url + "/planificacion", wait_until="networkidle")

    composer = open_composer(page)
    select_network(composer, "demo")
    text = "Publicación asistida de prueba: copia este texto"
    composer.locator("#cmp-text").fill(text)
    pick_media(page, composer, PHOTO, VIDEO)
    auto = composer.locator(".cmp-auto input[type=checkbox]")
    assert auto.is_checked()
    composer.locator(".cmp-auto .switch__track").click()
    assert not auto.is_checked()
    post_id = save_composer(page, composer, publish_now=True)
    assert "Publicación asistida" in page.locator("#toast-root").inner_text()

    # The scheduler turns it into "manual_pending" and notifies; the bell shows it.
    assert wait_until(lambda: target_status(live, post_id) == ["manual_pending"], 30)
    badge = page.locator("#notif-badge .notif__badge")
    assert wait_until(lambda: (refresh_bell(page), page.wait_for_timeout(400), badge.is_visible())[-1], 20)
    assert badge.inner_text().strip() == "1"
    page.click(".notif__btn")
    item = page.locator("#notif-list .notif-item").first
    item.wait_for()
    assert "Es hora de publicar en Demo" in item.inner_text()
    item.click()

    modal = page.locator("#modal-root #assist-modal")
    modal.wait_for(state="visible")
    live.shot(page, "flow-assisted-modal")
    modal.locator("button", has_text="Copiar texto").click()
    assert wait_until(lambda: page.evaluate("() => navigator.clipboard.readText()") == text, 5)
    with page.expect_download() as download:
        modal.locator("a.assist-zip").click()
    names = zipfile.ZipFile(download.value.path()).namelist()
    assert names == [f"01-{PHOTO}", f"02-{VIDEO}"]
    with page.expect_response(lambda r: "/marcar-publicado" in r.url) as marked:
        modal.locator("footer button", has_text="Marcar como publicado").click()
    assert marked.value.ok
    modal.wait_for(state="detached")
    assert target_status(live, post_id) == ["manual_done"]
    refresh_bell(page)
    page.locator("#notif-badge .notif__badge").wait_for(state="detached")
    log.assert_clean("assisted")
    page.context.close()


def test_ideas_board_trash_and_restore(browser: Any, live_server_sched: LiveServer):
    live = live_server_sched
    setup_demo(live)
    page = new_page(browser, live)
    log = PageLog(page)
    page.goto(live.url + "/ideas", wait_until="networkidle")

    composer = open_composer(page, ".idea-col--idea .idea-col__add")
    composer.locator("#cmp-text").fill("Idea: vídeo del nuevo menú de otoño")
    assert composer.locator(".cmp-split__main").inner_text().strip() == "Guardar como idea"
    post_id = save_composer(page, composer)
    card = page.locator(".idea-col--idea .idea-card", has_text="nuevo menú de otoño")
    card.wait_for()

    with page.expect_response(lambda r: f"/ideas/{post_id}/etapa" in r.url) as moved:
        card.drag_to(page.locator(".idea-col--en_progreso"))
    assert moved.value.ok
    moved_card = page.locator(".idea-col--en_progreso .idea-card", has_text="nuevo menú de otoño")
    moved_card.wait_for()
    with live.app.state.db.session_scope() as db:
        assert str(db.get(Post, post_id).idea_stage) == "en_progreso"
    live.shot(page, "flow-ideas")

    moved_card.locator(".idea-card__menu > button").click()
    moved_card.locator("button", has_text="Mover a la papelera").click()
    page.locator("#confirm-root button", has_text="Mover a la papelera").click()
    page.locator(".idea-card", has_text="nuevo menú de otoño").wait_for(state="detached")

    page.goto(live.url + "/papelera", wait_until="networkidle")
    row = page.locator("#trash-list").get_by_text("nuevo menú de otoño")
    row.wait_for()
    live.shot(page, "flow-papelera")
    with page.expect_response(lambda r: f"/papelera/{post_id}/restaurar" in r.url) as restored:
        page.locator("#trash-list button", has_text="Restaurar").first.click()
    assert restored.value.ok
    row.wait_for(state="detached")
    page.goto(live.url + "/ideas", wait_until="networkidle")
    page.locator(".idea-col--en_progreso .idea-card", has_text="nuevo menú de otoño").wait_for()
    log.assert_clean("ideas")
    page.context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_analytics_and_comments_with_demo_data(browser: Any, live_server_sched: LiveServer, scheme: str):
    live = live_server_sched
    setup_demo(live)
    page = new_page(browser, live, scheme)
    log = PageLog(page)

    # Estadísticas: "Actualizar ahora" collects the demo history; charts render on every tab.
    page.goto(live.url + "/estadisticas/demo", wait_until="networkidle")
    refresh = page.locator("[data-an-refresh]").first
    with page.expect_navigation(wait_until="networkidle", timeout=30_000):
        refresh.click()
    page.locator(".an-tabs").wait_for()
    assert page.locator("canvas").count() >= 2
    assert "Seguidores" in page.locator("main").inner_text()
    live.shot(page, f"flow-estadisticas-{scheme}")
    page.locator(".an-tabs .subtabs__list a", has_text="Cuenta").click()
    page.wait_for_load_state("networkidle")
    assert page.locator("canvas").count() >= 2
    page.locator(".an-tabs .subtabs__list a", has_text="Posts").click()
    page.wait_for_load_state("networkidle")
    assert page.locator("main table tbody tr").count() >= 1
    page.goto(live.url + "/estadisticas", wait_until="networkidle")
    assert page.locator("canvas").count() >= 1
    log.assert_clean("estadísticas")

    # Comentarios: "Actualizar" polls the demo comments; open one and reply.
    page.goto(live.url + "/comentarios", wait_until="networkidle")
    with page.expect_response(lambda r: "/comentarios/actualizar" in r.url) as polled:
        page.locator("#inbox-refresh").click()
    assert polled.value.ok
    items = page.locator(".ib-item")
    assert wait_until(lambda: items.count() > 0, 15)
    items.first.locator(".ib-item__link").click()
    thread = page.locator("#inbox-thread .ib-thread")
    thread.wait_for()
    reply = f"¡Gracias por tu comentario! ({scheme})"
    thread.locator("#ib-reply-text").fill(reply)
    with page.expect_response(lambda r: "/responder" in r.url) as answered:
        thread.locator("button[type=submit]", has_text="Responder").click()
    assert answered.value.ok
    page.locator("#inbox-thread").get_by_text(reply).wait_for()
    live.shot(page, f"flow-comentarios-{scheme}")
    log.assert_clean("comentarios")
    page.context.close()


def test_settings_save_and_csv_import(browser: Any, live_server_sched: LiveServer, tmp_path):
    live = live_server_sched
    setup_demo(live, tmp_path / "media")
    page = new_page(browser, live)
    log = PageLog(page)

    # Ajustes: save the grace window.
    page.goto(live.url + "/ajustes", wait_until="networkidle")
    page.fill("#f-grace", "90")
    with page.expect_response(lambda r: "/ajustes/publicacion" in r.url) as saved:
        page.locator("#publicacion button[type=submit]").click()
    assert saved.value.ok
    page.locator("#toast-root .toast", has_text="guardados").wait_for()
    page.reload(wait_until="networkidle")
    assert page.input_value("#f-grace") == "90"
    log.assert_clean("ajustes")

    # CSV: template download, then import two rows (Demo column, demo mode).
    page.goto(live.url + "/importar", wait_until="networkidle")
    with page.expect_download() as download:
        page.locator("a[href='/importar/plantilla.csv']").first.click()
    raw = Path(download.value.path()).read_bytes()
    assert download.value.suggested_filename == "plantilla.csv" and raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    header = [*rows[0], "demo"]
    assert header[:3] == ["fecha", "hora", "texto"] and len(rows) == 3  # header + 2 examples
    day = (date.today() + timedelta(days=2)).isoformat()
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(header)
    first = {"fecha": day, "hora": "10:30", "texto": "Importada desde CSV (1)", "demo": "si", "medios": PHOTO}
    second = {
        "fecha": day,
        "hora": "18:00",
        "texto": "Importada desde CSV (2)",
        "demo": "si",
        "autopublicar": "no",
    }
    for values in (first, second):
        writer.writerow([values.get(c, "") for c in header])
    upload = tmp_path / "importar.csv"
    upload.write_text(out.getvalue(), encoding="utf-8-sig")
    page.set_input_files("input[name=archivo]", str(upload))
    with page.expect_navigation(wait_until="networkidle"):
        page.locator("button[type=submit]", has_text="Revisar el archivo").click()
    assert "/importar/revisar/" in page.url
    assert page.locator("main table tbody tr", has_text="Se programará").count() == 2
    live.shot(page, "flow-csv-preview")
    with page.expect_navigation(wait_until="networkidle"):
        page.locator("button[type=submit]", has_text="Importar 2 publicaciones").click()
    assert "programadas=2" in page.url
    assert "Se han importado 2 publicaciones" in page.locator("main").inner_text()
    with live.app.state.db.session_scope() as db:
        posts = db.scalars(select(Post).where(Post.source == "csv").order_by(Post.id)).all()
        assert [p.text for p in posts] == ["Importada desde CSV (1)", "Importada desde CSV (2)"]
        assert all([t.status for t in p.targets] == [TargetStatus.SCHEDULED] for p in posts)
        assert [t.auto_publish for p in posts for t in p.targets] == [True, False]
    log.assert_clean("csv")
    page.context.close()

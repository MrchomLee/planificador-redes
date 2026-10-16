"""End-to-end "Carga masiva" in a real browser (marker ``e2e``): upload 3 tiny generated videos at once, keep
the Demo account, put the three on one day (3 times), fill the captions with «Pegar textos» (one of them with
a hashtags paragraph), reorder (the dates stay in their positions), schedule everything (confirm dialog) and
find the posts on the calendar. Fails on any console error or 4xx/5xx response.

Also (review regressions): the paste splitter and caption composer, the page being inert while "Programar
todo" runs and autosaving again for the next batch, a brand switch in another tab stopping the page, the
"interrupted" banner after a crash mid-submit, a new batch not keeping TikTok/YouTube per-batch choices, and
200 videos staying responsive.

Run: ``PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome uv run pytest -m e2e tests/e2e/test_bulk_e2e.py``.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import av
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from planificador import bulk, config
from planificador.deps import ensure_active_brand, set_active_brand
from planificador.models import Account, AccountStatus, Brand, Network, Post, Setting, TargetStatus

pytestmark = pytest.mark.e2e

TEXTS = [
    "Receta exprés de tacos 🌮\n\n#receta #tacos",  # text, blank line, hashtags: still one caption
    "Tres trucos para ahorrar\ncon una segunda línea",
    "Detrás de cámaras de nuestro equipo",
]
COMPONENT = "Alpine.$data(document.querySelector('.bk'))"


def make_videos(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, color in enumerate([(91, 76, 240), (233, 30, 99), (0, 150, 136)]):
        path = folder / f"video-{i + 1}.mp4"
        with av.open(str(path), mode="w") as out:
            stream = out.add_stream("libx264", rate=24)
            stream.width, stream.height, stream.pix_fmt = 360, 640, "yuv420p"
            for f in range(24):
                img = Image.new("RGB", (360, 640), color)
                ImageDraw.Draw(img).ellipse([130, 260 + f * 4, 230, 360 + f * 4], fill=(255, 255, 255))
                for packet in stream.encode(av.VideoFrame.from_image(img)):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
        paths.append(path)
    return paths


def seed(live: Any) -> tuple[int, str]:
    with live.app.state.db.session() as db:
        config.set_setting(db, "demo_mode", True)
        brand = ensure_active_brand(db)
        account = Account(
            brand_id=brand.id,
            network=Network.DEMO,
            external_id="demo-e2e",
            name="Cuenta Demo",
            username="demo",
            account_type="demo",
            status=AccountStatus.CONNECTED,
        )
        db.add(account)
        db.commit()
        return brand.id, brand.timezone or "Europe/Madrid"


def test_bulk_upload_paste_distribute_schedule_and_calendar(browser, live_server, tmp_path):
    brand_id, tz = seed(live_server)
    videos = make_videos(tmp_path / "videos")
    errors: list[str] = []
    bad: list[str] = []
    context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="es-ES")
    context.set_default_timeout(20_000)
    page = context.new_page()
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("response", lambda r: r.status >= 400 and bad.append(f"{r.status} {r.request.method} {r.url}"))

    page.goto(live_server.url + "/carga-masiva")
    page.wait_for_selector(".bk.is-ready")

    # 1 · Vídeos: three files at once (uploaded 2 at a time).
    page.set_input_files("[data-bulk-file]", [str(v) for v in videos])
    page.wait_for_function("document.querySelectorAll('.bk-vid').length === 3")
    names = page.locator(".bk-vid__name").all_inner_texts()
    assert names == ["video-1.mp4", "video-2.mp4", "video-3.mp4"]  # chosen order, whatever finishes first

    # 2 · Redes: the only account (Demo) is preselected.
    page.click("#bk-tab-2")
    pick = page.locator(".bk-acc__pick").first
    if pick.get_attribute("aria-pressed") != "true":
        pick.click()
    assert pick.get_attribute("aria-pressed") == "true"

    # 3 · Calendario: tomorrow, 3 a day at 10:00 + the suggested times.
    page.click("#bk-tab-3")
    tomorrow = (datetime.now(ZoneInfo(tz)) + timedelta(days=1)).date()
    page.fill("#bk-start", tomorrow.isoformat())
    page.fill('input[aria-label="Nueva hora"]', "10:00")
    page.click("text=Añadir hora")
    page.fill('input[aria-label="Publicaciones por día"]', "3")
    page.click("button:has-text('Repartir fechas')")
    page.wait_for_function("document.querySelectorAll('.bk-date-row.is-empty').length === 0")
    page.wait_for_selector(".bk-day")
    assert page.locator(".bk-day").count() == 1

    # 4 · Textos: paste the three captions (separated by a blank line; the hashtags paragraph of the first one
    # stays with it).
    page.click("#bk-tab-4")
    page.click("button:has-text('Pegar textos')")
    page.fill("#bk-paste-text", "\n\n".join(TEXTS))
    assert "3 textos (separados por líneas en blanco) para 3 vídeos" in page.inner_text(".bk-paste__count")
    page.click("button:has-text('Rellenar textos')")
    values = [page.locator(".bk-row__text").nth(i).input_value() for i in range(3)]
    assert values == TEXTS

    # Reorder: the list order is the publishing order, so the dates stay in their positions.
    def rows() -> list[list[str]]:
        return page.evaluate(f"{COMPONENT}.batch.rows.map(r => [r.text, r.date + ' ' + r.time])")

    before = rows()
    assert page.locator(".bk-order-hint").is_visible()
    page.locator("[data-move=up]").nth(2).click()
    page.locator("[data-move=up]").nth(1).click()
    after = rows()
    assert [r[0] for r in after] == [TEXTS[2], TEXTS[0], TEXTS[1]]
    assert [r[1] for r in after] == [r[1] for r in before]
    page.wait_for_function(
        "Array.from(document.querySelectorAll('.bk-badge')).every(b => /Listo|aviso/.test(b.textContent))"
    )

    # Schedule everything (styled confirm dialog), once.
    page.click("button.bk-go")
    page.click("[data-confirm-accept]")
    page.wait_for_selector("#bk-result-title")
    assert "3 publicaciones programadas" in page.inner_text("#bk-result-title")
    assert page.locator(".bk-result__item").count() == 3

    with live_server.app.state.db.session() as db:
        posts = list(db.scalars(select(Post).where(Post.brand_id == brand_id, Post.source == "bulk")))
        assert sorted(p.text for p in posts) == sorted(TEXTS)
        assert {t.status for p in posts for t in p.targets} == {TargetStatus.SCHEDULED}

    # The calendar opens on their day and shows the three posts.
    page.click("a:has-text('Ver en el calendario')")
    page.wait_for_url("**/planificacion?fecha=*")
    for text in ("Receta exprés de tacos", "Tres trucos para ahorrar", "Detrás de cámaras"):
        page.locator(".fc-event", has_text=text).first.wait_for()

    # Back on the page: the batch is empty and the last result is offered.
    page.goto(live_server.url + "/carga-masiva")
    page.wait_for_selector(".bk.is-ready")
    assert page.locator(".bk-vid").count() == 0
    assert "Tu última carga masiva" in page.inner_text(".bk-last")

    context.close()
    assert not errors, errors
    assert not bad, bad


# ======================================================================================= review fixes


def open_bulk(browser: Any, live_server: Any, errors: list[str] | None = None) -> tuple[Any, Any]:
    context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="es-ES")
    context.set_default_timeout(20_000)
    page = context.new_page()
    if errors is not None:
        # A 409 the test provokes on purpose is logged by Chromium as "Failed to load resource".
        page.on("console", lambda m: m.type == "error" and "409" not in m.text and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(live_server.url + "/carga-masiva")
    page.wait_for_selector(".bk.is-ready")
    return context, page


def stored_batch(live_server: Any, brand_id: int) -> dict | None:
    with live_server.app.state.db.session() as db:
        return bulk.load_batch(db, brand_id)


def upload(page: Any, files: list[Path], total: int) -> None:
    page.set_input_files("[data-bulk-file]", [str(f) for f in files])
    page.wait_for_function(f"document.querySelectorAll('.bk-vid').length === {total}")


def fill_rows(page: Any, tz: str) -> None:
    """Date (tomorrow, one hour apart) and text for every row."""
    day = (datetime.now(ZoneInfo(tz)) + timedelta(days=1)).date().isoformat()
    page.evaluate(
        f"""(day) => {COMPONENT}.batch.rows.forEach((r, i) => {{
            r.date = day;
            r.time = String(10 + i).padStart(2, '0') + ':00';
            r.text = 'Vídeo número ' + (i + 1);
        }})""",
        day,
    )


def wait_checked(page: Any) -> None:
    badges = "Array.from(document.querySelectorAll('.bk-row .bk-badge'))"
    page.wait_for_function(f"{badges}.every(b => /Listo|aviso/.test(b.textContent))", timeout=60_000)


def wait_saved(page: Any) -> None:
    page.wait_for_function("document.querySelector('.bk-save').textContent.includes('Guardado a las')")


def test_bulk_paste_splitter_and_caption_composer(browser, live_server):
    seed(live_server)
    context, page = open_bulk(browser, live_server)

    def split(text: str, mode: str = "auto") -> list[str]:
        return page.evaluate("([t, m]) => App.bulk.splitPasted(t, m)", [text, mode])

    # Review: captions written as text + blank line + hashtags were split in two (6 texts for 3 videos).
    usual = "\n\n".join(f"Receta {i}: tacos al pastor\n\n#tacos #receta{i}" for i in range(1, 4))
    assert split(usual) == [f"Receta {i}: tacos al pastor\n\n#tacos #receta{i}" for i in range(1, 4)]
    assert len(split(usual, "blank")) == 6  # "Por una línea en blanco" is literal
    # Captions with several paragraphs: a line with only "---" separates them (detected in "auto").
    multi = "Primero\n\nSegundo párrafo\n#a\n---\nOtro texto\n\n@cuenta #b\n  ---  \nTercero\n"
    expected = ["Primero\n\nSegundo párrafo\n#a", "Otro texto\n\n@cuenta #b", "Tercero"]
    assert split(multi) == expected and split(multi, "sep") == expected
    method = [page.evaluate("(t) => App.bulk.pasteMethod(t, 'auto')", t) for t in (multi, usual, "a\nb")]
    assert method == ["sep", "blank", "line"]
    assert split("Uno\nDos\nTres") == ["Uno", "Dos", "Tres"]
    assert split("Uno\n\nDos", "line") == ["Uno", "Dos"]
    assert split("#solo #hashtags\n\nTexto") == ["#solo #hashtags", "Texto"]  # nothing before it

    # The live counters use the same caption as the server (one-line text: hashtags on the same line).
    for args in [
        ("Hola mundo\n", ["#viaje"], ["verano"]),
        ("Línea 1\nLínea 2", ["x"], []),
        ("", ["a"], ["b"]),
        ("Tacos #Receta", ["receta", "tacos"], "#mexico"),
    ]:
        assert page.evaluate("([t, h, c]) => App.bulk.composeCaption(t, h, c)", list(args)) == (
            bulk.compose_caption(*args)
        )
    context.close()


def test_bulk_inert_while_scheduling_then_saves_the_next_batch_and_stops_on_brand_switch(
    browser, live_server, tmp_path
):
    brand_id, tz = seed(live_server)
    videos = make_videos(tmp_path / "videos")
    errors: list[str] = []
    context, page = open_bulk(browser, live_server, errors)
    saves: list[str] = []
    page.on(
        "request",
        lambda r: r.method == "POST" and r.url.endswith("/api/carga-masiva/lote") and saves.append(r.url),
    )
    upload(page, videos[:2], 2)
    fill_rows(page, tz)
    page.click("#bk-tab-4")
    wait_checked(page)
    wait_saved(page)
    first_token = page.evaluate(f"{COMPONENT}.batch.token")

    # "Guardar todo como borradores" with the response held: the steps are inert and nothing autosaves.
    held: list[Any] = []
    page.route("**/api/carga-masiva/programar", lambda route: held.append(route))
    page.click("button:has-text('Guardar todo como borradores')")
    page.click("[data-confirm-accept]")
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    assert held and page.evaluate(f"{COMPONENT}.submitting")
    assert page.evaluate(
        "['#bk-panel-4', '.bk-steps', '.bk-foot'].every(s => document.querySelector(s).inert)"
    )
    count = len(saves)
    # Review: an edit while "Programando…" ran got 409 and stopped autosave for good.
    page.evaluate(f"{COMPONENT}.batch.rows[0].text += ' (cambio mientras se guarda)'")
    page.wait_for_timeout(1500)
    assert len(saves) == count
    held[0].continue_()
    page.wait_for_selector("#bk-result-title")
    assert "2 borradores guardados" in page.inner_text("#bk-result-title")
    page.unroute("**/api/carga-masiva/programar")

    # A new batch does not keep the choices TikTok/YouTube want made for each batch.
    fresh = page.evaluate(
        f"""() => {{
        const c = {COMPONENT};
        c.acc.t9 = {{ network: 'tiktok' }};
        c.acc.y9 = {{ network: 'youtube' }};
        c.batch.accounts.t9 = {{ selected: false, format: 'video', auto_publish: true,
            options: {{ privacy_level: 'PUBLIC_TO_EVERYONE', disable_comment: true }} }};
        c.batch.accounts.y9 = {{ selected: false, format: 'short', auto_publish: true,
            options: {{ made_for_kids: false, privacy_status: 'unlisted' }} }};
        const b = c.freshBatch();
        delete c.batch.accounts.t9; delete c.batch.accounts.y9; delete c.acc.t9; delete c.acc.y9;
        return JSON.parse(JSON.stringify(b.accounts));
    }}"""
    )
    assert fresh["t9"]["options"] == {"privacy_level": None, "disable_comment": True}
    assert fresh["y9"]["options"] == {"made_for_kids": None, "privacy_status": "unlisted"}

    # The next batch autosaves again (it used to stay "Todo guardado" with nothing on the server).
    page.click("button:has-text('Nueva carga masiva')")
    upload(page, videos[2:], 1)
    wait_saved(page)
    stored = stored_batch(live_server, brand_id)
    assert stored is not None and stored["token"] != first_token and len(stored["rows"]) == 1

    # Discarding also clears those choices (spy on what it keeps).
    page.evaluate(
        f"""() => {{
        const c = {COMPONENT};
        const real = c.resetBatchChoices;
        c.acc.t9 = {{ network: 'tiktok' }};
        c.batch.accounts.t9 = {{ selected: false, format: 'video', auto_publish: true,
            options: {{ privacy_level: 'FOLLOWER_OF_CREATOR' }} }};
        c.resetBatchChoices = (accounts) => {{
            const out = real.call(c, accounts);
            window.__kept = JSON.parse(JSON.stringify(out.t9 || null));
            return out;
        }};
    }}"""
    )
    page.click(".bk-discard")
    page.click("[data-confirm-accept]")
    page.wait_for_function("window.__kept !== undefined")
    assert page.evaluate("window.__kept.options.privacy_level") is None
    page.evaluate(f"delete {COMPONENT}.acc.t9")
    upload(page, videos[:1], 1)
    wait_saved(page)

    # Brand switched in another tab: this tab stops (inert, "No se guarda") instead of writing into brand B.
    with live_server.app.state.db.session() as db:
        other = Brand(name="Marca B", color="#e91e63", timezone="Europe/Madrid")
        db.add(other)
        db.commit()
        set_active_brand(db, other.id)
        other_id = other.id
    page.click("#bk-tab-4")
    page.locator(".bk-row__text").first.fill("Escrito después de cambiar de marca")
    page.wait_for_selector(".bk-stopped", state="visible")
    assert "No se guarda" in page.inner_text(".bk-save")
    assert "«Marca B»" in page.inner_text(".bk-stopped")
    assert "no se han guardado" in page.inner_text(".bk-stopped")
    assert page.evaluate("document.querySelector('#bk-panel-4').inert")
    assert stored_batch(live_server, other_id) is None
    assert stored_batch(live_server, brand_id)["rows"][0]["text"] != "Escrito después de cambiar de marca"
    context.close()
    assert not errors, errors


def test_bulk_interrupted_submit_shows_how_to_finish(browser, live_server, tmp_path):
    brand_id, tz = seed(live_server)
    videos = make_videos(tmp_path / "videos")
    errors: list[str] = []
    context, page = open_bulk(browser, live_server, errors)
    upload(page, videos[:2], 2)
    fill_rows(page, tz)
    page.click("#bk-tab-4")
    wait_checked(page)
    wait_saved(page)
    batch = page.evaluate(f"JSON.parse(JSON.stringify({COMPONENT}.batch))")

    # "Programar todo" created the first post, then the app died (claim left by an earlier process).
    first = page.evaluate(
        "(b) => App.fetchJSON('/api/carga-masiva/programar', {body: {batch: b, action: 'schedule'}})",
        {**batch, "rows": batch["rows"][:1]},
    )
    assert first["scheduled"] == 1
    with live_server.app.state.db.session() as db:
        setting = db.get(Setting, bulk.submit_key(batch["token"]))
        setting.value = {
            "status": "running",
            "brand_id": brand_id,
            "boot": "earlier-process",
            "done": {p["row_id"]: p for p in first["posts"]},
        }
        db.commit()
    page.evaluate("(b) => App.fetchJSON('/api/carga-masiva/lote', {body: {batch: b}})", batch)

    page.reload()
    page.wait_for_selector(".bk.is-ready")
    banner = page.locator(".bk-interrupted")
    banner.wait_for(state="visible")
    assert "Se interrumpió" in banner.inner_text()
    assert "1 vídeo ya se había guardado" in banner.inner_text()
    assert page.evaluate(f"{COMPONENT}.batch.rows.length") == 1  # only the one left to create
    page.locator(".bk-row__text").first.fill("Texto cambiado tras el cierre")
    wait_saved(page)  # autosave works (it used to answer "ya se ha programado")
    wait_checked(page)
    page.click("button.bk-go")
    page.click("[data-confirm-accept]")
    page.wait_for_selector("#bk-result-title")
    assert "2 publicaciones programadas" in page.inner_text("#bk-result-title")
    with live_server.app.state.db.session() as db:
        posts = list(db.scalars(select(Post).where(Post.brand_id == brand_id, Post.source == "bulk")))
        assert sorted(p.text for p in posts) == ["Texto cambiado tras el cierre", "Vídeo número 1"]
    context.close()
    assert not errors, errors


def test_bulk_200_videos_stay_responsive(browser, live_server, tmp_path):
    # Review: shared warnings and row statuses were recomputed for every row on every render (quadratic):
    # ~600 ms per keystroke and 3 s to apply a check result with 200 videos.
    _, tz = seed(live_server)
    videos = make_videos(tmp_path / "videos")
    context, page = open_bulk(browser, live_server)
    upload(page, videos[:1], 1)
    day = (datetime.now(ZoneInfo(tz)) + timedelta(days=1)).date().isoformat()
    page.evaluate(
        f"""(day) => {{
        const c = {COMPONENT};
        const media = c.batch.rows[0].media_id;
        c.batch.rows = Array.from({{ length: 200 }}, (_, i) => ({{
            id: 'p' + i, media_id: media, text: 'Texto del vídeo ' + i, hashtags: ['comun'], yt_title: '',
            date: day, time: String(8 + (i % 12)).padStart(2, '0') + ':' + String(i % 60).padStart(2, '0'),
            networks: {{}} }}));
        c.go(4);
    }}""",
        day,
    )
    wait_checked(page)
    page.wait_for_function(f"!{COMPONENT}.checking && {COMPONENT}.saveState !== 'dirty'", timeout=30_000)
    page.wait_for_timeout(1000)

    # A check result where every video has one warning in common and half of them one of their own.
    applied_ms = page.evaluate(
        f"""async () => {{
        const c = {COMPONENT};
        const w = (code) => ({{ level: 'warning', code, message: 'Aviso ' + code, field: null, network: null,
                               network_label: null, account_key: null }});
        const checks = {{}};
        c.batch.rows.forEach((r, i) => (checks[r.id] = {{ status: 'warning', errors: [],
            warnings: [w('comun')].concat(i % 2 ? [] : [w('propio' + i)]) }}));
        const t = performance.now();
        c.checks = checks;
        await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));
        return performance.now() - t;
    }}"""
    )
    assert "Un aviso en todos los vídeos" in page.inner_text(".bk-shared summary")
    assert page.locator(".bk-row .bk-badge.is-warning").count() == 100
    assert page.locator(".bk-row .bk-badge.is-ok").count() == 100
    assert page.evaluate(f"{COMPONENT}.stepHint(4)") == "200 de 200 con texto"

    page.click(".bk-row__text >> nth=0")
    keys = page.evaluate(
        """async () => {
        const ta = document.querySelector('.bk-row__text'); const out = [];
        for (let i = 0; i < 15; i++) {
            const t = performance.now();
            ta.value += 'x'; ta.dispatchEvent(new Event('input', { bubbles: true }));
            await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0)));
            out.push(performance.now() - t);
        }
        return out;
    }"""
    )
    assert applied_ms < 1000, applied_ms
    assert statistics.median(keys) < 120, keys
    context.close()

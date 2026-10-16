"""Browser checks for TikTok apps that lack some scopes (sandbox apps without ``video.publish``).

Run: ``PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome uv run pytest -m e2e
tests/e2e/test_tiktok_scopes_e2e.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from planificador import config
from planificador.deps import ensure_active_brand
from planificador.models import Account, AccountStatus, Network

pytestmark = pytest.mark.e2e
LiveServer = Any  # tests/e2e/conftest.py::LiveServer


def seed_tiktok(live: LiveServer, scopes: list[str]) -> int:
    with live.app.state.db.session_scope() as db:
        brand = ensure_active_brand(db)
        config.set_setting(db, "demo_mode", True, commit=False)
        account = Account(
            brand_id=brand.id,
            network=Network.TIKTOK,
            external_id="tt-sandbox-1",
            name="Café Aurora",
            username="cafeaurora",
            status=AccountStatus.CONNECTED,
            scopes=scopes,
            meta={"demo": True, "granted_scopes": scopes},
        )
        db.add(account)
        db.flush()
        return account.id


def new_page(browser: Any) -> Any:
    context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="es-ES")
    context.set_default_timeout(15_000)
    page = context.new_page()
    page.errors = []  # type: ignore[attr-defined]
    page.on("pageerror", lambda e: page.errors.append(str(e)))  # type: ignore[attr-defined]
    return page


def open_composer_with_tiktok(page: Any, live: LiveServer) -> Any:
    page.goto(live.url + "/planificacion", wait_until="networkidle")
    page.locator("#btn-new-post").first.click()
    composer = page.locator("#modal-root .composer--overlay")
    composer.locator(".cmp-net--tiktok .cmp-net__logo").wait_for()
    page.locator("#modal-root .composer--overlay.is-ready").wait_for(state="visible")
    logo = composer.locator(".cmp-net--tiktok .cmp-net__logo")
    if logo.get_attribute("aria-pressed") != "true":
        logo.click()
    composer.locator(".cmp-net--tiktok .cmp-net__logo[aria-pressed='true']").wait_for()
    return composer


def data(page: Any, expr: str) -> Any:
    return page.evaluate(
        f"() => {{ const d = Alpine.$data(document.querySelector('.composer')); return {expr}; }}"
    )


def test_composer_forces_the_draft_mode_without_video_publish(browser: Any, live_server: LiveServer) -> None:
    key = str(seed_tiktok(live_server, ["user.info.basic", "video.upload", "video.list"]))
    page = new_page(browser)
    creator_calls: list[str] = []
    page.on("request", lambda r: creator_calls.append(r.url) if "creator-info" in r.url else None)
    composer = open_composer_with_tiktok(page, live_server)
    section = composer.locator(".cmp-tt")
    note = section.locator(".callout", has_text="video.publish")
    note.wait_for()
    assert "borrador" in note.inner_text()
    switch = section.locator("[data-field=mode] input[role=switch]")
    assert switch.is_checked() and switch.is_disabled()
    assert data(page, f"d.targets['{key}'].options.mode") == "inbox"
    # Direct Post only fields are hidden and not required; creator_info (Direct Post) is never asked.
    assert not composer.locator(f"#tt-privacy-{key}").is_visible()
    page.wait_for_function(
        "k => { const d = Alpine.$data(document.querySelector('.composer')); "
        "return !!(d.result && (d.result.errors || []).some(e => e.account_key === k)); }",
        arg=key,
    )  # the server validation of the TikTok target came back (the video is still missing)
    codes = data(page, f"d.result.errors.filter(e => e.account_key === '{key}').map(e => e.code)")
    assert codes  # e.g. the missing video
    assert "tiktok_privacy_required" not in codes and "tiktok_consent" not in codes
    assert "tiktok_mode_unavailable" not in codes
    assert composer.locator(".cmp-issue", has_text="Elige quién puede ver").count() == 0
    assert composer.locator(".cmp-issue", has_text="Confirmación de uso de música").count() == 0
    assert not creator_calls
    assert not page.errors
    page.context.close()


def test_composer_forces_direct_post_without_video_upload(browser: Any, live_server: LiveServer) -> None:
    key = str(seed_tiktok(live_server, ["user.info.basic", "video.publish"]))
    page = new_page(browser)
    composer = open_composer_with_tiktok(page, live_server)
    section = composer.locator(".cmp-tt")
    section.locator(".callout", has_text="video.upload").wait_for()
    switch = section.locator("[data-field=mode] input[role=switch]")
    assert not switch.is_checked() and switch.is_disabled()
    assert data(page, f"d.targets['{key}'].options.mode") == "direct"
    composer.locator(f"#tt-privacy-{key}").wait_for(state="visible")
    assert not page.errors
    page.context.close()


def test_settings_scope_checkboxes_save(browser: Any, live_server: LiveServer) -> None:
    page = new_page(browser)
    page.goto(live_server.url + "/ajustes#credenciales-tiktok", wait_until="networkidle")
    card = page.locator("#cred-tiktok")
    card.get_by_text("Marca exactamente los permisos (scopes)").wait_for()
    basic = card.locator("#ck-tt-scope-user-info-basic")
    assert basic.is_checked() and basic.is_disabled()
    card.locator("#ck-tt-scope-video-publish").uncheck()
    card.locator("#ck-tt-scope-video-list").check()
    with page.expect_response(lambda r: "/ajustes/credenciales/tiktok" in r.url) as saved:
        card.locator("button[type=submit]").click()
    assert saved.value.status == 200
    page.locator("#cred-tiktok #ck-tt-scope-video-list:checked").wait_for()
    with live_server.app.state.db.session_scope() as db:
        assert config.tiktok_scopes(db) == ["user.info.basic", "video.upload", "video.list"]
    # Unticking both publishing scopes is refused with an inline error.
    card = page.locator("#cred-tiktok")
    card.locator("#ck-tt-scope-video-upload").uncheck()
    with page.expect_response(lambda r: "/ajustes/credenciales/tiktok" in r.url) as refused:
        card.locator("button[type=submit]").click()
    assert refused.value.status == 422
    page.locator("#tiktok-scopes-error", has_text="Marca al menos").wait_for()
    with live_server.app.state.db.session_scope() as db:
        assert "video.upload" in config.tiktok_scopes(db)
    assert not page.errors
    page.context.close()

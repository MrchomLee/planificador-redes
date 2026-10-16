"""The composer's editor card must never scroll as a whole.

Regression for a bug seen on Windows: the visually hidden inputs of the switches ("Permitir comentarios", ...)
were positioned against the editor card instead of their own control, so they escaped the scrolling area and
made the clipped card scrollable. Clicking a switch (or tabbing to it) focused the hidden input and Chrome
scrolled the card to reveal it, leaving the footer floating over an empty area.

Run: ``PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome uv run pytest -m e2e tests/e2e/test_composer_layout_e2e.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from planificador import config
from planificador.deps import ensure_active_brand
from planificador.models import Account, AccountStatus, Network

pytestmark = pytest.mark.e2e
LiveServer = Any  # tests/e2e/conftest.py::LiveServer

SCOPES = ["user.info.basic", "video.publish", "video.upload"]
CARD_STATE = """() => {
  const c = document.querySelector('.composer__editor');
  return {scrollTop: c.scrollTop, scrollHeight: c.scrollHeight, clientHeight: c.clientHeight};
}"""


def seed_tiktok(live: LiveServer) -> None:
    with live.app.state.db.session_scope() as db:
        brand = ensure_active_brand(db)
        config.set_setting(db, "demo_mode", True, commit=False)
        db.add(
            Account(
                brand_id=brand.id,
                network=Network.TIKTOK,
                external_id="tt-layout-1",
                name="Ascendia",
                username="ascendiamx",
                status=AccountStatus.CONNECTED,
                scopes=SCOPES,
                meta={"demo": True, "granted_scopes": SCOPES},
            )
        )


def open_composer(browser: Any, live: LiveServer) -> tuple[Any, Any]:
    # A laptop-sized window: the editor content is taller than the card, so a scrollable card would show.
    context = browser.new_context(viewport={"width": 1366, "height": 657}, locale="es-ES", color_scheme="dark")
    context.set_default_timeout(15_000)
    page = context.new_page()
    page.goto(live.url + "/planificacion", wait_until="networkidle")
    page.locator("#btn-new-post").first.click()
    page.locator("#modal-root .composer--overlay.is-ready").wait_for(state="visible")
    composer = page.locator("#modal-root .composer--overlay")
    logo = composer.locator(".cmp-net--tiktok .cmp-net__logo")
    if logo.get_attribute("aria-pressed") != "true":
        logo.click()
    composer.locator(".cmp-tt").wait_for()
    return page, composer


def test_editor_card_has_no_scrollable_overflow(browser: Any, live_server: LiveServer) -> None:
    seed_tiktok(live_server)
    page, _ = open_composer(browser, live_server)
    state = page.evaluate(CARD_STATE)
    assert state["scrollHeight"] <= state["clientHeight"] + 1, state


def test_toggling_tiktok_switches_keeps_the_card_in_place(browser: Any, live_server: LiveServer) -> None:
    seed_tiktok(live_server)
    page, composer = open_composer(browser, live_server)
    switches = composer.locator(".cmp-tt label.switch")
    assert switches.count() >= 3
    for i in range(switches.count()):
        switch = switches.nth(i)
        if not switch.is_visible() or switch.locator("input").is_disabled():
            continue
        switch.scroll_into_view_if_needed()
        switch.click()
        assert page.evaluate(CARD_STATE)["scrollTop"] == 0
    # Keyboard users tab through every control of the editor: the card itself must never move.
    composer.locator(".cmp-scroll").first.click(position={"x": 5, "y": 5})
    for _ in range(60):
        page.keyboard.press("Tab")
        assert page.evaluate(CARD_STATE)["scrollTop"] == 0
    footer = composer.locator(".cmp-footer")
    assert footer.is_visible()
    box = footer.bounding_box()
    assert box is not None and box["y"] > 300  # still at the bottom of the card, not floating at the top

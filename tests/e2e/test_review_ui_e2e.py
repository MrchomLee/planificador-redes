"""Browser regressions for the UI review fixes (composer, planner, CSV preview).

Run: ``PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome uv run pytest -m e2e tests/e2e/test_review_ui_e2e.py``.
The app runs in demo mode with Facebook / Instagram / TikTok / YouTube accounts flagged ``demo`` (no tokens),
so every network call is answered by the app's offline stand-ins; ``creator_info`` is intercepted where a
test needs specific TikTok privacy options.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from planificador import config, csvio
from planificador.deps import ensure_active_brand
from planificador.models import (
    Account,
    AccountStatus,
    IdeaStage,
    Network,
    Post,
    PostKind,
    PostTarget,
    TargetStatus,
)

pytestmark = pytest.mark.e2e
LiveServer = Any  # tests/e2e/conftest.py::LiveServer

NETWORKS = ("facebook", "instagram", "tiktok", "youtube")


# ------------------------------------------------------------------------------------------ helpers


def wait_until(predicate: Callable[[], bool], timeout: float = 8.0, every: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(every)
    return predicate()


def seed(live: LiveServer) -> dict[str, int]:
    """Demo mode + one demo-flagged account per network on the active brand → {network: account id}."""
    ids: dict[str, int] = {}
    with live.app.state.db.session_scope() as db:
        brand = ensure_active_brand(db)
        config.set_setting(db, "demo_mode", True, commit=False)
        for network, ext, name, user, kind in (
            (Network.FACEBOOK, "fb-demo-1", "Café Aurora", None, "page"),
            (Network.INSTAGRAM, "ig-demo-1", "Café Aurora", "cafe.aurora", "business"),
            (Network.TIKTOK, "tt-demo-1", "Café Aurora", "cafeaurora", "creator"),
            (Network.YOUTUBE, "yt-demo-1", "Café Aurora TV", "@cafeaurora", "channel"),
        ):
            account = Account(
                brand_id=brand.id,
                network=network,
                external_id=ext,
                name=name,
                username=user,
                account_type=kind,
                status=AccountStatus.CONNECTED,
                meta={"demo": True},
            )
            db.add(account)
            db.flush()
            ids[str(network)] = account.id
        ids["brand"] = brand.id
    return ids


def add_post(
    live: LiveServer,
    brand_id: int,
    account_id: int,
    network: str,
    *,
    at: datetime | None,
    text: str,
    kind: PostKind = PostKind.SCHEDULED,
    stage: IdeaStage | None = None,
    fmt: str = "post",
    options: dict | None = None,
    status: TargetStatus = TargetStatus.SCHEDULED,
) -> int:
    with live.app.state.db.session_scope() as db:
        post = Post(brand_id=brand_id, kind=kind, idea_stage=stage, text=text, scheduled_at=at)
        post.targets.append(
            PostTarget(
                account_id=account_id,
                network=network,
                format=fmt,
                options=options or {},
                status=status,
                scheduled_at=at,
            )
        )
        db.add(post)
        db.flush()
        return post.id


def new_page(browser: Any, live: LiveServer, width: int = 1440, height: int = 900) -> Any:
    context = browser.new_context(viewport={"width": width, "height": height}, locale="es-ES")
    context.set_default_timeout(15_000)
    page = context.new_page()
    page.errors = []  # type: ignore[attr-defined]
    page.on("pageerror", lambda e: page.errors.append(str(e)))  # type: ignore[attr-defined]
    return page


def open_composer(page: Any, live: LiveServer) -> Any:
    page.goto(live.url + "/planificacion", wait_until="networkidle")
    page.locator("#btn-new-post").first.click()
    page.locator("#modal-root .composer--overlay.is-ready").wait_for(state="visible")
    return page.locator("#modal-root .composer--overlay")


def select_networks(composer: Any, *networks: str) -> None:
    for network in networks:
        logo = composer.locator(f".cmp-net--{network} .cmp-net__logo")
        if logo.get_attribute("aria-pressed") != "true":
            logo.click()
        composer.locator(f".cmp-net--{network} .cmp-net__logo[aria-pressed='true']").wait_for()


def composer_data(page: Any, expr: str) -> Any:
    return page.evaluate(
        f"() => {{ const d = Alpine.$data(document.querySelector('.composer')); return {expr}; }}"
    )


def creator_info_route(page: Any, options: list[str]) -> None:
    info = {
        "nickname": "Café Aurora",
        "username": "cafeaurora",
        "avatar_url": None,
        "privacy_level_options": options,
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 600,
    }
    body = json.dumps({"ok": True, "demo": False, "creator_info": info, "privacy_options": []})
    page.route(
        "**/api/tiktok/*/creator-info",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def wait_creator_loaded(page: Any, key: str) -> None:
    page.wait_for_function(
        "k => { const d = Alpine.$data(document.querySelector('.composer')); "
        "return d.creator[k] && d.creator[k].loading === false; }",
        arg=key,
    )


# ------------------------------------------------------------------- [0][1] TikTok privacy: no default


def test_single_privacy_option_is_not_preselected(browser: Any, live_server: LiveServer) -> None:
    ids = seed(live_server)
    key = str(ids["tiktok"])
    page = new_page(browser, live_server)
    creator_info_route(page, ["SELF_ONLY"])  # unaudited / sandbox client
    composer = open_composer(page, live_server)
    select_networks(composer, "tiktok")
    wait_creator_loaded(page, key)
    select = composer.locator(f"#tt-privacy-{key}")
    select.wait_for(state="visible")
    composer.locator(".cmp-tt .callout", has_text="no está auditada").wait_for()
    assert composer_data(page, f"d.targets['{key}'].options.privacy_level") is None
    assert select.evaluate("e => e.value") == ""
    assert select.evaluate("e => e.selectedOptions[0].textContent.trim()") == "Selecciona quién puede verla"
    # The user picks «Solo yo» explicitly.
    select.select_option("SELF_ONLY")
    assert composer_data(page, f"d.targets['{key}'].options.privacy_level") == "SELF_ONLY"
    assert not page.errors
    page.context.close()


def test_stale_privacy_is_cleared_with_a_notice(browser: Any, live_server: LiveServer) -> None:
    ids = seed(live_server)
    key = str(ids["tiktok"])
    post_id = add_post(
        live_server,
        ids["brand"],
        ids["tiktok"],
        "tiktok",
        at=datetime.now(UTC) + timedelta(days=3),
        text="Vídeo de otoño",
        fmt="video",
        options={"privacy_level": "FOLLOWER_OF_CREATOR", "consent": True, "mode": "direct"},
        status=TargetStatus.DRAFT,
    )
    page = new_page(browser, live_server)
    # The account turned public: «Seguidores» is no longer offered.
    creator_info_route(page, ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"])
    page.goto(f"{live_server.url}/publicaciones/{post_id}/editar", wait_until="networkidle")
    page.locator(".composer.is-ready").wait_for()
    wait_creator_loaded(page, key)
    select = page.locator(f"#tt-privacy-{key}")
    select.wait_for(state="visible")
    assert composer_data(page, f"d.targets['{key}'].options.privacy_level") is None
    assert select.evaluate("e => e.value") == ""  # the placeholder, not «Público»
    notice = page.locator(".cmp-tt .cmp-error", has_text="ya no está disponible")
    notice.wait_for()
    assert "Seguidores" in notice.inner_text()
    select.select_option("PUBLIC_TO_EVERYONE")
    assert composer_data(page, f"d.targets['{key}'].options.privacy_level") == "PUBLIC_TO_EVERYONE"
    notice.wait_for(state="hidden")
    assert not page.errors
    page.context.close()


# ------------------------------------------------------------- [2] clicking an issue goes to the field


def test_clicking_a_section_issue_scrolls_and_focuses_the_field(
    browser: Any, live_server: LiveServer
) -> None:
    ids = seed(live_server)
    key = str(ids["tiktok"])
    page = new_page(browser, live_server)
    composer = open_composer(page, live_server)
    select_networks(composer, "tiktok", "youtube")
    wait_creator_loaded(page, key)
    issue = composer.locator(".cmp-issues--error .cmp-issue", has_text="Elige quién puede ver")
    issue.wait_for()  # 1440×900: the error list is open by default
    assert composer.locator(".cmp-scroll").evaluate("e => e.scrollTop") == 0
    issue.click()
    assert wait_until(
        lambda: (
            page.evaluate("() => document.activeElement && document.activeElement.id") == f"tt-privacy-{key}"
        )
    )
    assert composer.locator(".cmp-scroll").evaluate("e => e.scrollTop") > 0
    assert not page.errors
    page.context.close()


# ------------------------------------------------------------ [4] error panel on laptop-height windows


def test_short_window_keeps_editor_and_toolbar_in_view(browser: Any, live_server: LiveServer) -> None:
    seed(live_server)
    page = new_page(browser, live_server, 1366, 657)
    composer = open_composer(page, live_server)
    select_networks(composer, *NETWORKS)
    panel = composer.locator(".cmp-issues--error")
    panel.wait_for(state="visible")
    head = panel.locator(".cmp-issues__head")
    assert head.get_attribute("aria-expanded") == "false"
    assert not panel.locator(".cmp-issues__list").is_visible()
    preview = panel.locator(".cmp-issues__preview")
    assert preview.is_visible() and preview.inner_text().strip()  # the first error, on one line
    tool = composer.locator(".cmp-tool[aria-label='Añadir fotos o vídeos']")
    inside = tool.evaluate(
        "b => { const s = b.closest('.cmp-scroll').getBoundingClientRect();"
        " const r = b.getBoundingClientRect();"
        " return r.top >= s.top && r.bottom <= s.bottom; }"
    )
    assert inside, "the media toolbar must be visible without scrolling"
    head.click()
    panel.locator(".cmp-issues__list").wait_for(state="visible")
    assert not preview.is_visible()
    assert not page.errors
    page.context.close()


def test_tall_window_opens_the_error_list(browser: Any, live_server: LiveServer) -> None:
    seed(live_server)
    page = new_page(browser, live_server, 1440, 900)
    composer = open_composer(page, live_server)
    select_networks(composer, *NETWORKS)
    composer.locator(".cmp-issues--error .cmp-issues__list").wait_for(state="visible")
    page.context.close()


# ------------------------------------------------------------------------------- [3] Ideas board search


def test_ideas_search_and_optimistic_counts(browser: Any, live_server: LiveServer) -> None:
    ids = seed(live_server)
    for text, stage in (
        ("Receta de tamales de Juárez", IdeaStage.IDEA),
        ("Detrás de cámaras en la cocina", IdeaStage.IDEA),
        ("Sorteo de aniversario", IdeaStage.EN_PROGRESO),
    ):
        add_post(
            live_server, ids["brand"], ids["facebook"], "facebook", at=None, text=text,
            kind=PostKind.IDEA, stage=stage, status=TargetStatus.DRAFT,
        )  # fmt: skip
    page = new_page(browser, live_server)
    page.goto(live_server.url + "/ideas", wait_until="networkidle")
    cards = page.locator(".idea-card")
    assert cards.count() == 3
    search = page.get_by_placeholder("Buscar ideas")

    def visible() -> list[str]:
        return [c.inner_text() for c in cards.all() if c.is_visible()]

    search.fill("zzzz")
    assert wait_until(lambda: visible() == [])
    search.fill("juarez")  # accent-insensitive
    assert wait_until(lambda: len(visible()) == 1 and "Juárez" in visible()[0])
    search.fill("")
    assert wait_until(lambda: len(visible()) == 3)

    # Drop a card on another column: the counters change at once (before the server answers).
    held: list[Any] = []
    page.route("**/ideas/*/etapa", lambda route: held.append(route))
    count = page.locator(".idea-col--lista [data-count]")
    assert count.inner_text().strip() == "0"
    page.locator(".idea-card", has_text="Sorteo").drag_to(page.locator(".idea-col--lista"))
    assert wait_until(lambda: bool(held))
    assert count.inner_text().strip() == "1"
    assert page.locator(".idea-col--en_progreso [data-count]").inner_text().strip() == "0"
    held[0].continue_()
    page.wait_for_load_state("networkidle")
    assert not page.errors
    page.context.close()


# ------------------------------------------------------------- [6] focus survives the planner refresh


def test_closing_the_drawer_returns_focus_to_the_row_and_event(browser: Any, live_server: LiveServer) -> None:
    ids = seed(live_server)
    at = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    first = add_post(live_server, ids["brand"], ids["facebook"], "facebook", at=at, text="Primera")
    add_post(
        live_server, ids["brand"], ids["facebook"], "facebook", at=at + timedelta(days=1), text="Segunda"
    )
    page = new_page(browser, live_server)

    def active() -> str:
        return page.evaluate(
            "() => { const e = document.activeElement; return e ? e.tagName + '|' + e.className + '|' +"
            " (e.getAttribute('hx-get') || e.dataset.postId || '') : ''; }"
        )

    page.goto(live_server.url + "/planificacion/lista", wait_until="networkidle")
    opener = page.locator(f'.plan-row__open[hx-get="/publicaciones/{first}"]')
    opener.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelector('#drawer-root').contains(document.activeElement)")
    with page.expect_response(lambda r: "/planificacion/lista" in r.url and r.request.method == "GET"):
        page.keyboard.press("Escape")
    assert wait_until(lambda: active() == f"BUTTON|plan-row__open|/publicaciones/{first}"), active()
    page.wait_for_timeout(400)
    assert active() == f"BUTTON|plan-row__open|/publicaciones/{first}"

    page.goto(live_server.url + "/planificacion?vista=mes", wait_until="networkidle")
    event = page.locator(f'a.fc-event[data-post-id="{first}"]')
    event.wait_for()
    event.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelector('#drawer-root').contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert wait_until(lambda: active().endswith(f"|{first}") and active().startswith("A|")), active()
    page.wait_for_timeout(600)
    assert active().endswith(f"|{first}")
    assert not page.errors
    page.context.close()


# ---------------------------------------------------------------------------- [8] bulk «Reprogramar»


def test_bulk_reprogramar_from_the_list(browser: Any, live_server: LiveServer) -> None:
    ids = seed(live_server)
    at = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(days=2)
    post_id = add_post(live_server, ids["brand"], ids["facebook"], "facebook", at=at, text="Mover")
    page = new_page(browser, live_server)
    page.goto(live_server.url + "/planificacion/lista", wait_until="networkidle")
    page.locator(f'#plan-list input[name="ids"][value="{post_id}"]').check()
    page.locator(".bulk-btn").click()
    page.locator("[data-bulk-shift]").click()
    days = page.locator("#bulk-shift-dias")
    days.wait_for(state="visible")
    assert page.evaluate("() => document.activeElement.id") == "bulk-shift-dias"
    url_before = page.url
    days.fill("3")
    page.wait_for_timeout(300)
    assert page.url == url_before  # the filters form ignores the shift inputs
    with page.expect_response(lambda r: r.url.endswith("/planificacion/lista/acciones")) as answer:
        page.locator(".bulk-shift__actions .btn--primary").click()
    assert answer.value.ok
    page.locator(".toast", has_text="Publicación reprogramada.").wait_for()
    with live_server.app.state.db.session_scope() as db:
        assert db.get(Post, post_id).scheduled_at.replace(tzinfo=UTC) == at + timedelta(days=3)
    assert "dias" not in page.url
    assert not page.errors
    page.context.close()


# ------------------------------------------------------------- [9] CSV preview follows the consent box


def test_csv_preview_tiles_follow_the_tiktok_consent(
    browser: Any, live_server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(live_server)
    when = datetime.now(UTC) + timedelta(days=5)
    preview = csvio.ImportPreview(
        rows=[
            csvio.ImportRow(line=2, values={}, scheduled_at=when, text="Facebook", networks=["facebook"]),
            csvio.ImportRow(
                line=3, values={}, scheduled_at=when, text="TikTok", networks=["tiktok"], tiktok_direct=True
            ),
        ]
    )
    monkeypatch.setattr(csvio, "build_preview", lambda *a, **k: preview)
    page = new_page(browser, live_server)
    page.goto(live_server.url + "/importar", wait_until="networkidle")
    page.set_input_files(
        "input[name=archivo]",
        files=[{"name": "x.csv", "mimeType": "text/csv", "buffer": b"fecha;hora;texto\n"}],
    )
    page.locator("button[type=submit]", has_text="Revisar el archivo").click()
    page.wait_for_url("**/importar/revisar/*")

    def tiles() -> list[str]:
        return [
            t.inner_text().replace("\n", " ")
            for t in page.locator(".cv-summary .kpi-tile").all()
            if t.is_visible()
        ]

    assert wait_until(lambda: tiles() == ["1 se programarán", "1 se guardarán como borrador"]), tiles()
    pending = page.locator("[data-tiktok-pending]")
    assert pending.is_visible() and "Se guardará como borrador" in pending.inner_text()
    page.locator("input[name=consentimiento_tiktok]").check()
    assert wait_until(lambda: tiles() == ["2 se programarán", "0 se guardarán como borrador"]), tiles()
    assert not pending.is_visible()
    assert page.locator("tr", has_text="TikTok").locator(".badge", has_text="Se programará").is_visible()
    assert not page.errors
    page.context.close()

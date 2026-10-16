"""Regression tests for the UI review fixes (routes / templates / static).

Browser behaviour (composer, planner JS) is covered by ``tests/e2e/test_review_ui_e2e.py``; these tests pin
the server side and the markup those scripts rely on.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import analytics, config, csvio, inbox, security
from planificador.models import (
    Account,
    AccountStatus,
    AnalyticsSnapshot,
    InboxComment,
    Post,
    PostKind,
    PostMetric,
    PostTarget,
    TargetStatus,
)
from planificador.templating import pct

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)  # conftest clock: Tuesday 10:00 in Europe/Madrid
HTMX = {"HX-Request": "true"}
STATIC = Path(__file__).resolve().parents[1] / "src" / "planificador" / "static"


def toast_of(response) -> dict:
    return json.loads(response.headers["hx-trigger"])["toast"]


def account_for(db: Session, brand, network: str, external_id: str | None = None, **kw) -> Account:
    acc = db.scalar(select(Account).where(Account.brand_id == brand.id, Account.network == network))
    if acc is None:
        acc = Account(
            brand_id=brand.id,
            network=network,
            external_id=external_id or f"{network}-1",
            name=kw.pop("name", f"Cuenta {network}"),
            status=AccountStatus.CONNECTED,
            **kw,
        )
        db.add(acc)
        db.commit()
    return acc


def make_post(
    db: Session,
    brand,
    *,
    at: datetime | None,
    text: str = "Publicación de prueba",
    network: str = "facebook",
    status: TargetStatus = TargetStatus.SCHEDULED,
    auto_publish: bool = True,
    kind: PostKind = PostKind.SCHEDULED,
) -> Post:
    acc = account_for(db, brand, network)
    post = Post(brand_id=brand.id, kind=kind, text=text, scheduled_at=at)
    post.targets.append(
        PostTarget(
            account_id=acc.id,
            network=network,
            format="post",
            status=status,
            scheduled_at=at,
            auto_publish=auto_publish,
        )
    )
    db.add(post)
    db.commit()
    return post


# ------------------------------------------------------------------- [0][1] TikTok privacy has no default


def test_composer_never_preselects_tiktok_privacy() -> None:
    """[0] creator_info offering a single option (unaudited / sandbox apps: SELF_ONLY) must not pick it; [1] a
    saved value the account no longer offers is cleared with a notice (behaviour: e2e)."""
    js = (STATIC / "js" / "composer.js").read_text(encoding="utf-8")
    assert "choices.length === 1" not in js
    assert "opts.privacy_level = choices[0]" not in js
    assert "!choices.includes(opts.privacy_level)" in js
    assert "ya no está disponible en tu cuenta de TikTok" in js


def test_tiktok_section_documents_no_default(client, brand) -> None:
    tpl = (
        Path(__file__).resolve().parents[1] / "src/planificador/templates/composer/_net_tiktok.html"
    ).read_text(encoding="utf-8")
    assert "unless only one option" not in tpl
    assert 'disabled :selected="!' in tpl  # the placeholder is selected while privacy_level is empty


# --------------------------------------------------------------------------- [2][3] $el scope in handlers


def test_handlers_called_from_inner_elements_do_not_query_this_el() -> None:
    """focusIssue (issue buttons) and ideasBoard.filter/recount (search input, column drop) run with $el set
    to the inner element: they must query from the component root."""
    composer = (STATIC / "js" / "composer.js").read_text(encoding="utf-8")
    body = composer[
        composer.index("focusIssue(it) {") : composer.index(
            "// ---------------------------------------------------------------- accounts & formats"
        )
    ]
    assert "this.$el" not in body.replace("// Not this.$el", "")
    planner = (STATIC / "js" / "planner.js").read_text(encoding="utf-8")
    board = planner[planner.index('Alpine.data("ideasBoard"') : planner.index('Alpine.data("tzPicker"')]
    assert '$$(".idea-card", this.$root)' in board and '$$("[data-stage]", this.$root)' in board
    assert re.search(r"\$\$?\([^)]*this\.\$el\)", board) is None


# ------------------------------------------------------------------------ [4] composer issue panel height


def test_error_list_starts_collapsed_on_short_windows() -> None:
    js = (STATIC / "js" / "composer.js").read_text(encoding="utf-8")
    assert "this.issuesOpen.error = (window.innerHeight || 0) >= ISSUES_OPEN_MIN_HEIGHT;" in js
    css = (STATIC / "css" / "composer.css").read_text(encoding="utf-8")
    assert "@media (max-height: 720px) and (min-width: 1001px)" in css


def test_collapsed_error_panel_previews_first_error(client, brand) -> None:
    html = client.get("/publicaciones/nueva").text
    assert 'class="cmp-issues__preview" x-show="!issuesOpen.error"' in html


# ---------------------------------------------------------------------- [5] assisted modal, scheduled target


def test_assisted_modal_for_scheduled_target_is_consistent(client, db, brand, demo_account) -> None:
    post = make_post(
        db,
        brand,
        at=NOW + timedelta(days=2),
        network="demo",
        status=TargetStatus.SCHEDULED,
        auto_publish=False,
    )
    target = post.targets[0]
    html = client.get(f"/objetivos/{target.id}/asistida", headers=HTMX).text
    assert "Puedes prepararla ya" not in html
    assert "publícala a la hora prevista" in html
    assert "Podrás marcarla como publicada cuando llegue la hora programada" in html
    assert "Marcar como publicado" not in html  # no button that would be refused
    assert "está «" not in html  # no "publicación está «Programado»" gender clash


def test_assisted_modal_blocked_state_wording(client, db, brand, demo_account) -> None:
    post = make_post(
        db,
        brand,
        at=NOW + timedelta(days=2),
        network="demo",
        status=TargetStatus.CANCELLED,
        auto_publish=False,
    )
    html = client.get(f"/objetivos/{post.targets[0].id}/asistida", headers=HTMX).text
    assert "No se puede marcar como publicada en su estado actual:" in html
    assert "está «" not in html


# ------------------------------------------------------------------------- [7] forgotten password recovery


def test_quitar_contrasena_cli_removes_the_password(app, db, data_dir, capsys) -> None:
    from planificador import __main__ as cli

    security.set_password(db, "secreto-123")
    assert security.password_enabled(db)
    assert cli.main(["--data-dir", str(data_dir), "--quitar-contrasena"]) == 0
    assert "Contraseña quitada" in capsys.readouterr().out
    db.expire_all()
    assert not security.password_enabled(db)
    # Running it again is harmless.
    assert cli.main(["--data-dir", str(data_dir), "--quitar-contrasena"]) == 0
    assert "no tenía contraseña" in capsys.readouterr().out


def test_quitar_contrasena_without_data(tmp_path, capsys) -> None:
    from planificador import __main__ as cli

    assert cli.main(["--data-dir", str(tmp_path / "vacio"), "--quitar-contrasena"]) == 0
    assert "no hay ninguna contraseña" in capsys.readouterr().out


def test_login_and_security_explain_the_reset(client, db) -> None:
    security.set_password(db, "secreto-123")
    html = client.get("/login").text
    assert "¿Has olvidado la contraseña?" in html and "quitar-contrasena.bat" in html
    assert "--quitar-contrasena" in html
    tpl = (
        Path(__file__).resolve().parents[1] / "src/planificador/templates/settings/_security.html"
    ).read_text(encoding="utf-8")
    assert "password_hash" not in tpl and "quitar-contrasena.bat" in tpl
    bat = Path(__file__).resolve().parents[1] / "quitar-contrasena.bat"
    assert bat.is_file() and b"--quitar-contrasena" in bat.read_bytes() and b"\r\n" in bat.read_bytes()


# ------------------------------------------------------------------------------ [8] bulk «Reprogramar»


def test_list_offers_bulk_reprogramar(client, brand) -> None:
    html = client.get("/planificacion/lista").text
    assert "Reprogramar…" in html and "data-bulk-shift" in html
    assert 'hx-vals=\'{"accion": "reprogramar"}\'' in html
    # The day/hour inputs are kept out of the filters form (else they would end up in the list URL).
    assert 'id="bulk-shift-dias" name="dias" form="bulk-shift"' in html
    assert '<form id="bulk-shift" hidden' in html


def test_bulk_reprogramar_moves_selected_posts(csrf_client, db, brand) -> None:
    a = make_post(db, brand, at=NOW + timedelta(days=1), text="Uno")
    b = make_post(db, brand, at=NOW + timedelta(days=2), text="Dos", status=TargetStatus.DRAFT)
    r = csrf_client.post(
        "/planificacion/lista/acciones",
        data={"accion": "reprogramar", "ids": [str(a.id), str(b.id)], "dias": "1", "horas": "2"},
        headers=HTMX,
    )
    assert r.status_code == 200, r.text
    assert toast_of(r) == {"message": "2 publicaciones reprogramadas.", "kind": "success"}
    db.expire_all()
    shift = timedelta(days=1, hours=2)
    assert db.get(Post, a.id).scheduled_at.replace(tzinfo=UTC) == NOW + timedelta(days=1) + shift
    assert db.get(Post, b.id).scheduled_at.replace(tzinfo=UTC) == NOW + timedelta(days=2) + shift
    target = db.get(Post, a.id).targets[0]
    assert target.scheduled_at.replace(tzinfo=UTC) == NOW + timedelta(days=1) + shift


def test_bulk_reprogramar_keeps_wall_time_across_dst(csrf_client, db, brand) -> None:
    # Saturday 27 March 2027 10:00 CET (09:00 UTC); Sunday 28 March is already CEST (UTC+2).
    post = make_post(db, brand, at=datetime(2027, 3, 27, 9, 0, tzinfo=UTC), text="Cambio de hora")
    r = csrf_client.post(
        "/planificacion/lista/acciones",
        data={"accion": "reprogramar", "ids": str(post.id), "dias": "1"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["ok"] is True
    db.expire_all()
    assert db.get(Post, post.id).scheduled_at.replace(tzinfo=UTC) == datetime(2027, 3, 28, 8, 0, tzinfo=UTC)


def test_bulk_reprogramar_skips_what_cannot_move(csrf_client, db, brand) -> None:
    ok = make_post(db, brand, at=NOW + timedelta(days=3), text="Se mueve")
    done = make_post(
        db, brand, at=NOW - timedelta(days=1), text="Ya publicada", status=TargetStatus.PUBLISHED
    )
    soon = make_post(db, brand, at=NOW + timedelta(hours=1), text="Casi ya")
    r = csrf_client.post(
        "/planificacion/lista/acciones",
        data={"accion": "reprogramar", "ids": [str(ok.id), str(done.id), str(soon.id)], "dias": "-1"},
        headers={"Accept": "application/json"},
    )
    body = r.json()
    assert body["done"] == 1
    assert body["ok"] is False and len(body["errors"]) == 2
    assert any("«Ya publicada» no se puede mover" in e for e in body["errors"])
    assert any("«Casi ya» quedaría en una fecha pasada" in e for e in body["errors"])
    assert body["message"].startswith("Publicación reprogramada. 2 no se han podido procesar.")
    db.expire_all()
    assert db.get(Post, ok.id).scheduled_at.replace(tzinfo=UTC) == NOW + timedelta(days=2)
    assert db.get(Post, soon.id).scheduled_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    ("dias", "horas", "message"),
    [
        ("0", "0", "Indica cuántos días u horas"),
        ("", "", "Indica cuántos días u horas"),
        ("uno", "0", "números enteros"),
        ("400", "0", "Como máximo"),
        ("1", "30", "Como máximo"),
    ],
)
def test_bulk_reprogramar_validation(csrf_client, db, brand, dias, horas, message) -> None:
    post = make_post(db, brand, at=NOW + timedelta(days=3))
    r = csrf_client.post(
        "/planificacion/lista/acciones",
        data={"accion": "reprogramar", "ids": str(post.id), "dias": dias, "horas": horas},
        headers=HTMX,
    )
    # 400, not 422: htmx swaps 422 answers into the target (the list itself).
    assert r.status_code == 400
    assert message in r.text


def test_ideas_card_menu_still_rejects_reprogramar(csrf_client, db, brand) -> None:
    idea = make_post(db, brand, at=None, kind=PostKind.IDEA, status=TargetStatus.DRAFT)
    r = csrf_client.post(f"/ideas/{idea.id}/acciones", data={"accion": "reprogramar"}, headers=HTMX)
    assert r.status_code == 422


# ------------------------------------------------------------------- [9] CSV preview vs TikTok consent


def test_csv_preview_counts_follow_the_tiktok_consent(csrf_client, brand, monkeypatch) -> None:
    when = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
    preview = csvio.ImportPreview(
        rows=[
            csvio.ImportRow(line=2, values={}, scheduled_at=when, text="Facebook", networks=["facebook"]),
            csvio.ImportRow(
                line=3, values={}, scheduled_at=when, text="TikTok", networks=["tiktok"], tiktok_direct=True
            ),
            csvio.ImportRow(
                line=4,
                values={},
                scheduled_at=when,
                text="Con error",
                networks=["facebook"],
                issues=[csvio.RowIssue("error", "Falta algo")],
            ),
        ]
    )
    monkeypatch.setattr(csvio, "build_preview", lambda *a, **k: preview)
    location = csrf_client.post(
        "/importar/revisar",
        data={"csrf_token": csrf_client.token},
        files={"archivo": ("x.csv", b"fecha;hora;texto\n", "text/csv")},
        follow_redirects=False,
    ).headers["location"]
    html = csrf_client.get(location).text
    tiles = html[html.index('class="cv-summary"') : html.index("</table>")]

    def tile(show: str, label: str) -> str:
        m = re.search(
            r'<div role="listitem" x-show="'
            + re.escape(show)
            + r'"[^>]*>\s*<div class="kpi-tile[^"]*"[^>]*>\s*'
            r'<span class="kpi-tile__value">([^<]*)</span>\s*<span class="kpi-tile__label">'
            + re.escape(label),
            tiles,
        )
        assert m, (show, label)
        return m.group(1).strip()

    # Without the consent the TikTok row becomes a draft (as csv_confirm does); with it, it is scheduled.
    assert tile("!consent", "se programarán") == "1" and tile("consent", "se programarán") == "2"
    assert tile("!consent", "se guardarán como borrador") == "2"
    assert tile("consent", "se guardarán como borrador") == "1"
    row = html[html.index('value="3"') : html.index('value="4"')]
    assert 'x-show="!consent" data-tiktok-pending' in row and "Se guardará como borrador" in row
    assert "Falta aceptar la Confirmación de uso de música" in row
    assert 'name="consentimiento_tiktok" value="1" x-model="consent"' in html


def test_csv_preview_without_tiktok_keeps_plain_tiles(csrf_client, brand, monkeypatch) -> None:
    when = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
    preview = csvio.ImportPreview(
        rows=[csvio.ImportRow(line=2, values={}, scheduled_at=when, text="Hola", networks=["facebook"])]
    )
    monkeypatch.setattr(csvio, "build_preview", lambda *a, **k: preview)
    location = csrf_client.post(
        "/importar/revisar",
        data={"csrf_token": csrf_client.token},
        files={"archivo": ("x.csv", b"fecha;hora;texto\n", "text/csv")},
        follow_redirects=False,
    ).headers["location"]
    html = csrf_client.get(location).text
    assert 'x-show="!consent"' not in html and "data-tiktok-pending" not in html
    assert "Se programará" in html


# ------------------------------------------------------- [10] another page/IG/channel replaces the account


def _seed_history(db: Session, account: Account) -> None:
    db.add(AnalyticsSnapshot(account_id=account.id, day=date(2026, 10, 31), metrics={"followers": 10000}))
    db.add(
        PostMetric(
            account_id=account.id,
            external_id="old-media-1",
            published_at=NOW - timedelta(days=1),
            metrics={"likes": 50},
        )
    )
    db.add(
        InboxComment(
            account_id=account.id,
            external_id="c1",
            object_external_id="old-media-1",
            author_name="Ana",
            text="Hola",
            created_time=NOW - timedelta(days=1),
        )
    )
    db.commit()
    config.set_setting(db, inbox.STATE_KEY, {str(account.id): {"last_ok": NOW.isoformat()}, "999": {"x": 1}})


def _counts(db: Session, account_id: int) -> tuple[int, int, int]:
    return (
        len(db.scalars(select(AnalyticsSnapshot).where(AnalyticsSnapshot.account_id == account_id)).all()),
        len(db.scalars(select(PostMetric).where(PostMetric.account_id == account_id)).all()),
        len(db.scalars(select(InboxComment).where(InboxComment.account_id == account_id)).all()),
    )


def test_reconnecting_another_account_resets_its_history(db, brand, clock) -> None:
    from planificador.routes.connections import upsert_account

    acc = account_for(db, brand, "instagram", external_id="IG_OLD")
    _seed_history(db, acc)
    upsert_account(db, brand.id, "instagram", "IG_NEW", clock.now(), name="Nueva", access_token="t")
    db.commit()
    db.expire_all()
    same = db.scalars(
        select(Account).where(Account.brand_id == brand.id, Account.network == "instagram")
    ).one()
    assert same.id == acc.id and same.external_id == "IG_NEW"  # scheduled posts keep following the row
    assert _counts(db, acc.id) == (0, 0, 0)
    state = inbox.poll_state(db)
    assert str(acc.id) not in state and "999" in state  # next poll imports the full window again
    assert analytics.posts_in(db, same, date(2026, 10, 27), date(2026, 11, 3)) == []


def test_reconnecting_the_same_account_keeps_its_history(db, brand, clock) -> None:
    from planificador.routes.connections import upsert_account

    acc = account_for(db, brand, "instagram", external_id="IG_SAME")
    _seed_history(db, acc)
    upsert_account(db, brand.id, "instagram", "IG_SAME", clock.now(), name="Misma", access_token="t2")
    db.commit()
    db.expire_all()
    assert _counts(db, acc.id) == (1, 1, 1)
    assert str(acc.id) in inbox.poll_state(db)


def test_choosers_warn_that_history_is_reset() -> None:
    root = Path(__file__).resolve().parents[1] / "src/planificador/templates/connections"
    meta = (root / "meta_chooser.html").read_text(encoding="utf-8")
    yt = (root / "youtube_chooser.html").read_text(encoding="utf-8")
    for tpl in (meta, yt):
        assert "se borrarán las" in tpl and "comentarios guardados" in tpl


# ---------------------------------------------------- [11] Resumen % change over comparable accounts only


def test_summary_change_only_compares_accounts_with_a_baseline(client, db, brand) -> None:
    fb = account_for(db, brand, "facebook", external_id="page-1")
    tt = account_for(db, brand, "tiktok", external_id="tt-1")
    # Period 7–28 Oct 2026 vs 15 Sep–6 Oct; TikTok was connected 5 days before the end.
    db.add(AnalyticsSnapshot(account_id=fb.id, day=date(2026, 10, 5), metrics={"followers": 990}))
    db.add(AnalyticsSnapshot(account_id=fb.id, day=date(2026, 10, 27), metrics={"followers": 1000}))
    db.add(AnalyticsSnapshot(account_id=tt.id, day=date(2026, 10, 25), metrics={"followers": 5000}))
    db.commit()
    html = client.get("/estadisticas?periodo=personalizado&desde=2026-10-07&hasta=2026-10-28").text
    start = html.index('data-tile="followers"')
    tile = html[start : html.index("</div>", start)]
    assert '<span class="kpi-tile__value">6.000</span>' in tile  # the value still adds every account
    assert pct(analytics.change_pct(1000, 990), 1, signed=True) in tile  # +1 %, FB only
    assert pct(analytics.change_pct(6000, 990), 1, signed=True) not in tile  # not the fake +506 %
    assert "solo tiene en cuenta las redes con datos de los dos periodos" in tile


def test_summary_change_without_any_baseline(client, db, brand) -> None:
    tt = account_for(db, brand, "tiktok", external_id="tt-1")
    db.add(AnalyticsSnapshot(account_id=tt.id, day=date(2026, 10, 25), metrics={"followers": 5000}))
    db.commit()
    html = client.get("/estadisticas?periodo=personalizado&desde=2026-10-07&hasta=2026-10-28").text
    start = html.index('data-tile="followers"')
    tile = html[start : html.index("</div>", start)]
    assert "sin datos previos" in tile and 'an-tile__delta">' not in tile

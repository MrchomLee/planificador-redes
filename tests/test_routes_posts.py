"""Post detail drawer/page, target actions, "Publicación asistida" modal and media ZIP (routes/posts.py)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from planificador.models import (
    Account,
    AccountStatus,
    CommitPhase,
    MediaAsset,
    MediaKind,
    Network,
    Notification,
    Post,
    PostEvent,
    PostTarget,
    TargetStatus,
)
from planificador.routes import posts as posts_routes

HX = {"HX-Request": "true"}
HX_DRAWER = {"HX-Request": "true", "HX-Target": "drawer-root"}


# ------------------------------------------------------------------------------------------ fixtures


def _account(db: Session, brand, network: Network, **kw: Any) -> Account:
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=kw.pop("external_id", f"{network}-ext"),
        name=kw.pop("name", f"Cuenta {network}"),
        username=kw.pop("username", "cafeteria"),
        account_type=kw.pop("account_type", "business"),
        status=kw.pop("status", AccountStatus.CONNECTED),
        **kw,
    )
    db.add(account)
    db.commit()
    return account


def _media(
    app, db: Session, name: str, data: bytes, kind: MediaKind = MediaKind.IMAGE, **kw: Any
) -> MediaAsset:
    media_root = Path(app.state.settings.paths.media_dir)
    rel = f"originals/{name[:2]}/{name}"
    path = media_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    asset = MediaAsset(
        kind=kind,
        original_name=kw.pop("original_name", name),
        rel_path=rel,
        sha256=(name * 64)[:64],
        size_bytes=len(data),
        mime="video/mp4" if kind == MediaKind.VIDEO else "image/jpeg",
        width=1080,
        height=1350,
        **kw,
    )
    db.add(asset)
    db.commit()
    return asset


def _post(db: Session, brand, clock, *, text: str = "Nuevo menú de otoño 🍂", **kw: Any) -> Post:
    post = Post(
        brand_id=brand.id,
        text=text,
        scheduled_at=kw.pop("scheduled_at", clock.now() + timedelta(days=1)),
        **kw,
    )
    db.add(post)
    db.commit()
    return post


def _target(db: Session, post: Post, account: Account, status: TargetStatus, **kw: Any) -> PostTarget:
    target = PostTarget(
        post_id=post.id,
        account_id=account.id,
        network=account.network,
        format=kw.pop("format", "post"),
        status=status,
        scheduled_at=post.scheduled_at,
        **kw,
    )
    db.add(target)
    db.commit()
    return target


@pytest.fixture
def ig(db: Session, brand) -> Account:
    return _account(db, brand, Network.INSTAGRAM, external_id="1784", username="cafeteria.aurora")


@pytest.fixture
def fb(db: Session, brand) -> Account:
    return _account(
        db, brand, Network.FACEBOOK, external_id="1234567890", username=None, name="Cafetería Aurora"
    )


@pytest.fixture
def yt(db: Session, brand) -> Account:
    return _account(db, brand, Network.YOUTUBE, external_id="UCabc123", username=None, name="Aurora TV")


def _trigger(response: Any) -> dict:
    return json.loads(response.headers["HX-Trigger"])


def _reload(db: Session, obj: Any) -> Any:
    db.expire_all()
    return db.get(type(obj), obj.id)


# ------------------------------------------------------------------------------------------ detail


def test_detail_drawer_fragment(
    client: TestClient, db: Session, brand, clock, ig: Account, fb: Account
) -> None:
    post = _post(db, brand, clock, text="Texto común <b>no HTML</b>", notes="Revisar precios")
    t_ig = _target(
        db,
        post,
        ig,
        TargetStatus.ERROR,
        last_error="Instagram ha rechazado el vídeo.",
        last_error_detail={"http_status": 400, "code": "2207026"},
        options={"first_comment": "#otoño", "collaborators": ["ana", "luis"]},
    )
    t_fb = _target(
        db,
        post,
        fb,
        TargetStatus.PUBLISHED,
        external_url="https://facebook.com/p/1",
        published_at=clock.now(),
    )
    db.add(
        PostEvent(target_id=t_ig.id, ts=clock.now(), level="error", event="error", message="Error permanente")
    )
    db.add(
        PostEvent(
            target_id=t_ig.id, ts=clock.now() - timedelta(minutes=5), event="claimed", message="Enviando"
        )
    )
    db.commit()

    r = client.get(f"/publicaciones/{post.id}", headers=HX_DRAWER)
    assert r.status_code == 200
    html = r.text
    assert "<html" not in html.lower()
    assert 'class="drawer drawer--lg post-drawer"' in html and 'id="post-detail"' in html
    assert "Texto común &lt;b&gt;no HTML&lt;/b&gt;" in html
    assert "Revisar precios" in html
    # Per-target cards: account, format, status, error, external link, timeline, actions.
    assert f'id="ptarget-{t_ig.id}"' in html and f'id="ptarget-{t_fb.id}"' in html
    assert "@cafeteria.aurora" in html and "Instagram ha rechazado el vídeo." in html
    assert "2207026" in html  # technical details
    assert 'href="https://facebook.com/p/1"' in html and "Ver en Facebook" in html
    assert "Error permanente" in html and "Enviando" in html
    assert f'hx-post="/objetivos/{t_ig.id}/reintentar"' in html
    assert f'hx-post="/objetivos/{t_ig.id}/cancelar"' in html
    assert f'hx-post="/objetivos/{t_fb.id}/reintentar"' not in html
    assert "@ana, @luis" in html
    # Brand time zone (Europe/Madrid): DEFAULT_NOW 09:00Z + 1 day → miércoles 4 de noviembre, 10:00.
    assert "4 de noviembre, 10:00" in html
    # Post-level footer.
    assert f'hx-post="/publicaciones/{post.id}/duplicar"' in html
    assert f'hx-post="/publicaciones/{post.id}/papelera"' in html
    # Already published somewhere → editing is locked (postsvc.edit_lock_reason), duplicating is not.
    assert f'hx-get="/publicaciones/{post.id}/editar"' not in html and "no se puede editar" in html
    assert f'hx-post="/publicaciones/{post.id}/publicar-ahora"' in html  # error target can be published now
    # Self refresh on changes.
    assert 'hx-trigger="posts:changed from:body, post:saved from:body"' in html


def test_detail_full_page_and_partial(client: TestClient, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock)
    _target(db, post, ig, TargetStatus.SCHEDULED)
    page = client.get(f"/publicaciones/{post.id}")
    assert page.status_code == 200
    assert page.text.lstrip().lower().startswith("<!doctype html>")
    assert 'class="topbar"' in page.text and "post-detail--pagina" in page.text
    assert 'class="mainnav__link is-active"' in page.text  # Planificación

    assert f'hx-get="/publicaciones/{post.id}/editar"' in page.text

    partial = client.get(f"/publicaciones/{post.id}?parcial=1&modo=pagina", headers=HX)
    assert partial.status_code == 200
    assert partial.text.lstrip().startswith('<div id="post-detail" class="post-detail post-detail--pagina"')
    assert "drawer-backdrop" not in partial.text


def test_detail_missing_post_is_404(client: TestClient) -> None:
    assert client.get("/publicaciones/999", headers=HX_DRAWER).status_code == 404
    assert client.get("/objetivos/999", headers=HX_DRAWER).status_code == 404


def test_target_deep_link_highlights_card(
    client: TestClient, db: Session, brand, clock, ig: Account, fb: Account
) -> None:
    post = _post(db, brand, clock)
    _target(db, post, fb, TargetStatus.SCHEDULED)
    t_ig = _target(db, post, ig, TargetStatus.UNKNOWN, commit_phase=CommitPhase.COMMIT_SENT)
    r = client.get(f"/objetivos/{t_ig.id}", headers=HX_DRAWER)
    assert r.status_code == 200
    assert f'class="ptarget st-unknown is-focus" id="ptarget-{t_ig.id}"' in r.text
    # Unknown: retry needs the explicit "did NOT publish" confirmation.
    assert "¿Seguro que NO se publicó?" in r.text
    assert 'hx-vals=\'{"confirmado": "1"}\'' in r.text
    assert "data-confirm-danger" in r.text
    # Full page fallback too.
    assert "is-focus" in client.get(f"/objetivos/{t_ig.id}").text


@pytest.mark.parametrize(
    ("status", "auto", "expected", "absent"),
    [
        (TargetStatus.SCHEDULED, True, ["cancelar"], ["reintentar", "marcar-publicado", "asistida"]),
        (TargetStatus.SCHEDULED, False, ["asistida", "cancelar"], ["reintentar"]),
        (TargetStatus.MANUAL_PENDING, False, ["asistida", "marcar-publicado", "cancelar"], ["reintentar"]),
        (TargetStatus.ERROR, True, ["reintentar", "marcar-publicado", "cancelar"], ["asistida"]),
        (TargetStatus.MISSED, True, ["reintentar", "marcar-publicado", "cancelar"], []),
        (TargetStatus.SENT_TO_APP, True, ["marcar-publicado"], ["reintentar", "cancelar"]),
        (TargetStatus.PUBLISHED, True, [], ["reintentar", "cancelar", "marcar-publicado"]),
        (TargetStatus.CANCELLED, True, [], ["reintentar", "cancelar", "marcar-publicado"]),
    ],
)
def test_actions_per_status(
    client: TestClient, db: Session, brand, clock, ig: Account, status, auto, expected, absent
) -> None:
    post = _post(db, brand, clock)
    target = _target(db, post, ig, status, auto_publish=auto)
    html = client.get(f"/publicaciones/{post.id}", headers=HX_DRAWER).text
    for name in expected:
        assert f"/objetivos/{target.id}/{name}" in html, name
    for name in absent:
        assert f"/objetivos/{target.id}/{name}" not in html, name


def test_trashed_post_offers_restore_only(client: TestClient, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock, deleted_at=clock.now())
    target = _target(db, post, ig, TargetStatus.CANCELLED)
    html = client.get(f"/publicaciones/{post.id}", headers=HX_DRAWER).text
    assert "En la papelera" in html and f'hx-post="/publicaciones/{post.id}/restaurar"' in html
    assert f'hx-post="/publicaciones/{post.id}/papelera"' not in html
    assert f"/objetivos/{target.id}/cancelar" not in html


# ------------------------------------------------------------------------------------------ actions


def test_retry_error_target(csrf_client, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock, scheduled_at=clock.now() - timedelta(hours=1))
    target = _target(db, post, ig, TargetStatus.ERROR, last_error="Fallo", attempts=4)
    r = csrf_client.post(f"/objetivos/{target.id}/reintentar", headers=HX)
    assert r.status_code == 200, r.text
    trigger = _trigger(r)
    assert trigger["posts:changed"] and trigger["notifications:refresh"]
    assert trigger["toast"] == {"message": "Reintentando la publicación en Instagram.", "kind": "success"}
    target = _reload(db, target)
    assert target.status == TargetStatus.SCHEDULED and target.last_error is None


def test_retry_unknown_requires_confirmation(csrf_client, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.UNKNOWN, commit_phase=CommitPhase.COMMIT_SENT)
    r = csrf_client.post(f"/objetivos/{target.id}/reintentar", headers=HX)
    assert r.status_code == 409
    assert "X-Error-Message" in r.headers
    assert _reload(db, target).status == TargetStatus.UNKNOWN
    r = csrf_client.post(f"/objetivos/{target.id}/reintentar", data={"confirmado": "1"}, headers=HX)
    assert r.status_code == 200, r.text
    assert _reload(db, target).status == TargetStatus.SCHEDULED


def test_cancel_and_invalid_cancel(csrf_client, db: Session, brand, clock, ig: Account, fb: Account) -> None:
    post = _post(db, brand, clock)
    scheduled = _target(db, post, ig, TargetStatus.SCHEDULED)
    published = _target(db, post, fb, TargetStatus.PUBLISHED, published_at=clock.now())
    r = csrf_client.post(f"/objetivos/{scheduled.id}/cancelar", headers=HX)
    assert r.status_code == 200 and _trigger(r)["toast"]["message"] == "Publicación cancelada en Instagram."
    assert _reload(db, scheduled).status == TargetStatus.CANCELLED
    r = csrf_client.post(f"/objetivos/{published.id}/cancelar", headers=HX)
    assert r.status_code == 409
    assert _reload(db, published).status == TargetStatus.PUBLISHED


def test_mark_published_with_url_from_assisted_modal(
    csrf_client, db: Session, brand, clock, ig: Account
) -> None:
    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.MANUAL_PENDING, auto_publish=False)
    db.add(Notification(kind="manual_publish", title="Toca publicar", target_id=target.id))
    db.commit()
    r = csrf_client.post(
        f"/objetivos/{target.id}/marcar-publicado",
        data={"url": "https://www.instagram.com/p/abc/", "origen": "asistida"},
        headers=HX,
    )
    assert r.status_code == 200, r.text
    assert _trigger(r)["closeModal"] is True
    target = _reload(db, target)
    assert target.status == TargetStatus.MANUAL_DONE
    assert target.external_url == "https://www.instagram.com/p/abc/"
    # The assisted reminder is resolved.
    note = db.query(Notification).filter_by(target_id=target.id).one()
    assert note.read_at is not None


def test_mark_published_rejects_bad_url(csrf_client, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.MANUAL_PENDING, auto_publish=False)
    r = csrf_client.post(
        f"/objetivos/{target.id}/marcar-publicado", data={"url": "javascript:alert(1)"}, headers=HX
    )
    assert r.status_code == 422
    assert _reload(db, target).status == TargetStatus.MANUAL_PENDING


def test_plain_form_post_redirects_with_flash(csrf_client, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.MANUAL_PENDING, auto_publish=False)
    r = csrf_client.post(
        f"/objetivos/{target.id}/marcar-publicado",
        data={"next": f"/publicaciones/{post.id}", "origen": "asistida"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/publicaciones/{post.id}"
    page = csrf_client.get(r.headers["location"]).text
    assert "Marcada como publicada en Instagram" in page
    # Open redirects are ignored.
    target2 = _target(db, _post(db, brand, clock), ig, TargetStatus.ERROR)
    r = csrf_client.post(
        f"/objetivos/{target2.id}/cancelar", data={"next": "https://evil.example/"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == f"/publicaciones/{target2.post_id}"


def test_publish_now(csrf_client, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock, scheduled_at=clock.now() + timedelta(days=2))
    target = _target(db, post, ig, TargetStatus.SCHEDULED)
    r = csrf_client.post(f"/publicaciones/{post.id}/publicar-ahora", headers=HX)
    assert r.status_code == 200, r.text
    target = _reload(db, target)
    assert target.status == TargetStatus.SCHEDULED and target.scheduled_at == clock.now()
    # Nothing left to publish → user-facing error.
    target.status = TargetStatus.PUBLISHED
    db.commit()
    r = csrf_client.post(f"/publicaciones/{post.id}/publicar-ahora", headers=HX)
    assert r.status_code == 409 and r.headers.get("X-Error-Message")


def test_duplicate_trash_restore(csrf_client, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock)
    _target(db, post, ig, TargetStatus.SCHEDULED)
    r = csrf_client.post(f"/publicaciones/{post.id}/duplicar", headers=HX)
    assert r.status_code == 200, r.text
    assert _trigger(r)["post:open"]["id"] > post.id
    copies = db.query(Post).filter(Post.id != post.id).all()
    assert len(copies) == 1 and copies[0].text == post.text

    r = csrf_client.post(f"/publicaciones/{post.id}/duplicar", data={"modo": "pagina"}, headers=HX)
    assert r.status_code == 200 and r.headers["HX-Redirect"].startswith("/publicaciones/")

    r = csrf_client.post(f"/publicaciones/{post.id}/papelera", headers=HX)
    assert r.status_code == 200 and _trigger(r)["closeDrawer"] is True
    assert _reload(db, post).deleted_at is not None

    r = csrf_client.post(f"/publicaciones/{post.id}/restaurar", headers=HX)
    assert r.status_code == 200
    assert _reload(db, post).deleted_at is None


def test_actions_require_csrf(client: TestClient, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.ERROR)
    assert client.post(f"/objetivos/{target.id}/reintentar", headers=HX).status_code == 403
    assert _reload(db, target).status == TargetStatus.ERROR


def test_engine_contract_is_used(monkeypatch, csrf_client, db: Session, brand, clock, ig: Account) -> None:
    """Routes call the engine contract functions with the documented signatures."""
    from planificador import engine

    calls: list[tuple] = []
    monkeypatch.setattr(
        engine,
        "retry_target",
        lambda db_, tid, clk, *, confirmed_not_published=False: calls.append(
            ("retry", tid, confirmed_not_published)
        ),
    )
    monkeypatch.setattr(engine, "cancel_target", lambda db_, tid, clk: calls.append(("cancel", tid)))
    monkeypatch.setattr(
        engine,
        "mark_done_manually",
        lambda db_, tid, clk, external_url=None: calls.append(("done", tid, external_url)),
    )
    monkeypatch.setattr(engine, "publish_now", lambda db_, pid, clk: calls.append(("now", pid)))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise engine.EngineError("La publicación ha cambiado de estado; recarga la página.")

    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.UNKNOWN)
    assert (
        csrf_client.post(
            f"/objetivos/{target.id}/reintentar", data={"confirmado": "1"}, headers=HX
        ).status_code
        == 200
    )
    assert csrf_client.post(f"/objetivos/{target.id}/cancelar", headers=HX).status_code == 200
    csrf_client.post(f"/objetivos/{target.id}/marcar-publicado", data={"url": " "}, headers=HX)
    csrf_client.post(f"/publicaciones/{post.id}/publicar-ahora", headers=HX)
    assert calls == [
        ("retry", target.id, True),
        ("cancel", target.id),
        ("done", target.id, None),
        ("now", post.id),
    ]

    monkeypatch.setattr(engine, "cancel_target", boom)
    r = csrf_client.post(f"/objetivos/{target.id}/cancelar", headers=HX)
    assert r.status_code == 409
    assert "recarga" in r.text
    r = csrf_client.post(f"/objetivos/{target.id}/cancelar", headers={"Accept": "application/json"})
    assert r.status_code == 409 and r.json()["ok"] is False


# ------------------------------------------------------------------------------------------ assisted


def test_assisted_modal(app, client: TestClient, db: Session, brand, clock, ig: Account) -> None:
    a1 = _media(app, db, "aa11.jpg", b"\xff\xd8first", original_name="portada.jpg")
    a2 = _media(
        app, db, "bb22.mp4", b"video-bytes", kind=MediaKind.VIDEO, duration_s=75.0, original_name="reel.mp4"
    )
    post = _post(db, brand, clock, text="Texto común")
    post.set_media([a1, a2])
    db.commit()
    target = _target(
        db,
        post,
        ig,
        TargetStatus.MANUAL_PENDING,
        auto_publish=False,
        format="reel",
        text_override="Texto solo para Instagram",
        options={"first_comment": "#cafe", "collaborators": ["ana"], "share_to_feed": False},
    )
    r = client.get(f"/objetivos/{target.id}/asistida", headers={**HX, "HX-Target": "modal-root"})
    assert r.status_code == 200
    html = r.text
    assert "<html" not in html.lower() and 'class="modal modal--lg assist-modal"' in html
    assert "Texto solo para Instagram" in html and "Texto común" not in html
    assert "Copiar texto" in html and f'data-copy-target="#assist-text-{target.id}"' in html
    assert (
        f'href="/medios/{a1.id}/archivo?descargar=1"' in html
        and f'href="/medios/{a2.id}/archivo?descargar=1"' in html
    )
    assert f'href="/objetivos/{target.id}/medios.zip"' in html and "Descargar todo (.zip)" in html
    assert "1:15" in html  # video duration
    assert 'href="https://www.instagram.com/"' in html and "Abrir Instagram" in html
    assert (
        "Desactiva «Compartir también en el feed»" in html and "@ana" in html and 'data-copy="#cafe"' in html
    )
    assert (
        f'hx-post="/objetivos/{target.id}/marcar-publicado"' in html
        and 'name="origen" value="asistida"' in html
    )
    assert f'form="assist-done-{target.id}"' in html

    page = client.get(f"/objetivos/{target.id}/asistida")
    assert page.status_code == 200 and 'class="topbar"' in page.text
    assert (
        f'action="/objetivos/{target.id}/marcar-publicado"' in page.text
        and "hx-post=" not in page.text.split("assist-done")[1][:200]
    )


def test_assisted_youtube_fields(client: TestClient, db: Session, brand, clock, yt: Account) -> None:
    post = _post(db, brand, clock, text="Primera línea del vídeo\nDescripción larga")
    target = _target(
        db,
        post,
        yt,
        TargetStatus.MANUAL_PENDING,
        auto_publish=False,
        format="video",
        options={
            "tags": ["café", "otoño"],
            "made_for_kids": False,
            "privacy_status": "unlisted",
            "category_id": "26",
        },
    )
    html = client.get(f"/objetivos/{target.id}/asistida", headers=HX).text
    assert "Copiar título" in html and "Copiar descripción" in html and "Copiar etiquetas" in html
    assert 'value="Primera línea del vídeo"' in html  # title prefilled from the first line
    assert 'value="café, otoño"' in html
    assert (
        "https://studio.youtube.com/channel/UCabc123/videos/upload?d=ud" in html
        and "Abrir YouTube Studio" in html
    )
    assert "No, no es contenido creado para niños" in html and "Oculto (no listado)" in html
    assert "Consejos y estilo" in html


def test_assisted_done_and_missing(client: TestClient, db: Session, brand, clock, fb: Account) -> None:
    post = _post(db, brand, clock)
    target = _target(
        db,
        post,
        fb,
        TargetStatus.MANUAL_DONE,
        external_url="https://facebook.com/x",
        published_at=clock.now(),
    )
    html = client.get(f"/objetivos/{target.id}/asistida", headers=HX).text
    assert "Ya está marcada como publicada" in html and "Guardar enlace" in html
    assert "https://www.facebook.com/1234567890" in html
    assert client.get("/objetivos/999/asistida", headers=HX).status_code == 404


def test_checklist_tiktok() -> None:
    target = PostTarget(
        network="tiktok",
        format="video",
        options={
            "privacy_level": "SELF_ONLY",
            "disable_duet": True,
            "is_aigc": True,
            "brand_content_toggle": True,
        },
    )
    texts = [item.text for item in posts_routes.assisted_checklist(target, 1)]
    assert "«¿Quién puede ver este vídeo?»: Solo yo." in texts
    assert "Permitir comentarios: Sí · Dúo: No · Stitch: Sí." in texts
    assert "Activa «Contenido generado por IA»." in texts
    assert "Activa «Divulgación de contenido comercial»: Contenido de marca." in texts


# ------------------------------------------------------------------------------------------ media ZIP


def test_media_zip_streams_files_in_order(
    app, client: TestClient, db: Session, brand, clock, ig: Account
) -> None:
    a1 = _media(app, db, "cc33.jpg", b"A" * 3000, original_name="foto: 1?.jpg")
    a2 = _media(
        app, db, "dd44.mp4", b"B" * (posts_routes.ZIP_CHUNK + 10), kind=MediaKind.VIDEO, original_name="clip"
    )
    missing = _media(app, db, "ee55.jpg", b"C", original_name="borrada.jpg")
    Path(app.state.settings.paths.media_dir, missing.rel_path).unlink()
    post = _post(db, brand, clock)
    post.set_media([a1, a2, missing])
    db.commit()
    target = _target(db, post, ig, TargetStatus.MANUAL_PENDING, auto_publish=False)

    r = client.get(f"/objetivos/{target.id}/medios.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert f'filename="medios-instagram-{post.id}.zip"' in r.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    assert archive.testzip() is None
    names = archive.namelist()
    assert names == ["01-foto_ 1_.jpg", "02-clip.mp4"]
    assert archive.read(names[0]) == b"A" * 3000
    assert len(archive.read(names[1])) == posts_routes.ZIP_CHUNK + 10


def test_media_zip_without_media_is_404(client: TestClient, db: Session, brand, clock, ig: Account) -> None:
    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.MANUAL_PENDING)
    assert client.get(f"/objetivos/{target.id}/medios.zip").status_code == 404
    assert client.get("/objetivos/999/medios.zip").status_code == 404


def test_zip_stream_helper(tmp_path: Path) -> None:
    files = []
    for i in range(3):
        p = tmp_path / f"f{i}.bin"
        p.write_bytes(bytes([i]) * (1000 * (i + 1)))
        files.append((f"0{i + 1}-f{i}.bin", p))
    chunks = list(posts_routes.zip_stream(files))
    assert len(chunks) >= 3
    archive = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert [archive.read(n)[:1] for n in archive.namelist()] == [b"\x00", b"\x01", b"\x02"]


def test_open_network_links() -> None:
    def link(network: str, external: str) -> str | None:
        target = PostTarget(network=network, format="post")
        target.account = Account(network=network, external_id=external, name="x")
        return posts_routes.open_network_link(target)[0]

    assert link("tiktok", "open") == "https://www.tiktok.com/tiktokstudio/upload"
    assert link("youtube", "nochannel") == "https://studio.youtube.com/"
    assert link("facebook", "page-slug") == "https://www.facebook.com/"
    assert link("demo", "d") is None


def test_detail_polls_while_a_target_is_in_flight(
    client: TestClient, db: Session, brand, clock, ig: Account
) -> None:
    """After "Publicar ahora" the scheduler changes the status in the background: the open detail refreshes
    itself while a target is publishing or due within two minutes, and stops once everything settled."""
    post = _post(db, brand, clock)
    target = _target(db, post, ig, TargetStatus.PUBLISHING)
    url = f"/publicaciones/{post.id}?parcial=1&modo=drawer"
    assert "every 4s" in client.get(url).text
    target.status = TargetStatus.SCHEDULED
    target.scheduled_at = clock.now() + timedelta(minutes=1)
    db.commit()
    assert "every 4s" in client.get(url).text
    target.scheduled_at = clock.now() + timedelta(hours=3)
    db.commit()
    assert "every 4s" not in client.get(url).text
    target.status = TargetStatus.PUBLISHED
    db.commit()
    assert "every 4s" not in client.get(url).text

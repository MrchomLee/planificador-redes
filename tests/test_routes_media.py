"""Library routes (SPEC §8): page, streaming upload (CSRF), files, API JSON, detail panel, delete, picker."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planificador import media
from planificador.models import Brand, MediaAsset, Post, PostTarget, TargetStatus
from test_media import make_image, make_video

JSON = {"Accept": "application/json"}
ITEM_KEYS = {"id", "kind", "name", "thumb_url", "url", "width", "height", "duration_s"}


def upload(client: Any, *files: tuple[str, bytes, str], headers: dict | None = None) -> Any:
    payload = [("files", (name, data, mime)) for name, data, mime in files]
    return client.post("/biblioteca/subir", files=payload, headers={**JSON, **(headers or {})})


def png(size=(64, 48), color=(10, 120, 200)) -> bytes:
    return make_image("PNG", size, color)


@pytest.fixture
def clip(tmp_path: Path) -> bytes:
    return make_video(tmp_path / "clip.mp4", size=(48, 64), seconds=1.0).read_bytes()


def tmp_files(app: Any) -> list[Path]:
    tmp = Path(app.state.paths.tmp_dir)
    return list(tmp.iterdir()) if tmp.exists() else []


def count_assets(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(MediaAsset)) or 0)


# ----------------------------------------------------------------------------------------------- page


def test_empty_library_page(client: TestClient) -> None:
    r = client.get("/biblioteca")
    assert r.status_code == 200
    text = r.text
    assert text.lstrip().lower().startswith("<!doctype html>")
    assert 'class="mainnav__link is-active"' in text and 'href="/biblioteca"' in text
    assert "Tu biblioteca está vacía" in text
    assert "/static/css/media.css" in text and "/static/js/media.js" in text
    assert "data-upload-trigger" in text and 'name="q"' in text
    for path in ("/static/css/media.css", "/static/js/media.js"):
        assert client.get(path).status_code == 200


# --------------------------------------------------------------------------------------------- upload


def test_upload_multiple_files_returns_json_list(
    app, csrf_client, db: Session, brand: Brand, clip: bytes
) -> None:
    r = upload(csrf_client, ("Cartel.png", png(), "image/png"), ("reel.mp4", clip, "video/mp4"))
    assert r.status_code == 200, r.text
    items = r.json()
    assert [i["ok"] for i in items] == [True, True]
    image, video = items
    assert ITEM_KEYS <= set(image) and image["kind"] == "image" and image["name"] == "Cartel.png"
    assert (
        image["thumb_url"] == f"/medios/{image['id']}/miniatura"
        and image["url"] == f"/medios/{image['id']}/archivo"
    )
    assert (image["width"], image["height"]) == (64, 48)
    assert video["kind"] == "video" and video["duration_s"] == pytest.approx(1.0, abs=0.1)
    assert set(image["compat"]) == {"facebook", "instagram", "tiktok", "youtube"}
    rows = db.scalars(select(MediaAsset).order_by(MediaAsset.id)).all()
    assert [a.brand_id for a in rows] == [brand.id, brand.id]
    assert tmp_files(app) == []


def test_upload_requires_csrf(app, client: TestClient, db: Session) -> None:
    r = upload(client, ("a.png", png(), "image/png"))
    assert r.status_code == 403
    assert count_assets(db) == 0 and tmp_files(app) == []


def test_upload_rejects_cross_origin(csrf_client, db: Session) -> None:
    r = upload(csrf_client, ("a.png", png(), "image/png"), headers={"Origin": "https://malo.example.com"})
    assert r.status_code == 403
    assert count_assets(db) == 0


def test_upload_plain_form_with_token_field_redirects(
    client: TestClient, csrf_token: str, db: Session
) -> None:
    r = client.post(
        "/biblioteca/subir",
        data={"csrf_token": csrf_token},
        files=[("files", ("form.png", png(), "image/png"))],
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/biblioteca"
    assert count_assets(db) == 1
    page = client.get("/biblioteca").text
    flashes = json.loads(re.search(r'id="flash-data">(.*?)</script>', page, re.S).group(1))
    assert flashes == [{"kind": "success", "message": "1 archivo añadido a la biblioteca."}]


def test_upload_reports_per_file_errors(app, csrf_client, db: Session) -> None:
    r = upload(csrf_client, ("ok.png", png(), "image/png"), ("notas.jpg", b"texto plano" * 20, "image/jpeg"))
    assert r.status_code == 200
    ok, bad = r.json()
    assert ok["ok"] is True
    assert bad == {
        "ok": False,
        "name": "notas.jpg",
        "error": bad["error"],
        "code": "unsupported",
        "status": 415,
    }
    assert "Tipo de archivo no admitido" in bad["error"]
    only_bad = upload(csrf_client, ("notas.jpg", b"texto plano" * 20, "image/jpeg"))
    assert only_bad.status_code == 415 and only_bad.json()[0]["ok"] is False
    assert count_assets(db) == 1 and tmp_files(app) == []


def test_upload_size_limit(app, csrf_client, db: Session, monkeypatch) -> None:
    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", 1000)
    r = upload(csrf_client, ("grande.png", make_image("PNG", (300, 300), noise=True), "image/png"))
    assert r.status_code == 413
    assert r.json()[0]["code"] == "too_large"
    assert count_assets(db) == 0 and tmp_files(app) == []


def test_upload_needs_multipart_and_files(csrf_client) -> None:
    assert csrf_client.post("/biblioteca/subir", json={"x": 1}, headers=JSON).status_code == 415
    r = csrf_client.post("/biblioteca/subir", files=[("otro", ("a.png", png(), "image/png"))], headers=JSON)
    assert r.status_code == 400 and r.json()[0]["code"] == "no_files"


def test_upload_dedupes(csrf_client, db: Session) -> None:
    data = png((20, 20))
    first = upload(csrf_client, ("a.png", data, "image/png")).json()[0]
    second = upload(csrf_client, ("copia.png", data, "image/png")).json()[0]
    assert first["id"] == second["id"] and count_assets(db) == 1


# ------------------------------------------------------------------------------------------- browsing


def _seed(db: Session, brand_id: int, tmp_path: Path) -> dict[str, MediaAsset]:
    import io

    assets = {
        "playa": media.ingest(db, io.BytesIO(png((80, 100), (1, 2, 3))), "Playa de verano.png", brand_id),
        "monte": media.ingest(db, io.BytesIO(png((100, 80), (4, 5, 6))), "Montaña.png", brand_id),
        "video": media.ingest_path(
            db, make_video(tmp_path / "v.mp4", size=(64, 48), seconds=4.0), "Promo.mp4", brand_id
        ),
    }
    return assets


def test_library_grid_search_and_filter(
    client: TestClient, db: Session, brand: Brand, tmp_path: Path
) -> None:
    assets = _seed(db, brand.id, tmp_path)
    other = Brand(name="Otra", color="#e8590c", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    import io

    hidden = media.ingest(db, io.BytesIO(png((10, 10), (9, 9, 9))), "secreto.png", other.id)
    text = client.get("/biblioteca").text
    for asset in assets.values():
        assert f'data-media-id="{asset.id}"' in text
    assert f'data-media-id="{hidden.id}"' not in text
    assert "3 archivos" in text and "compat-dot" in text
    assert 'id="media-count-video">1<' in text

    hx = {"HX-Request": "true", "HX-Target": "media-results"}
    frag = client.get("/biblioteca", params={"q": "playa"}, headers=hx).text
    assert "<html" not in frag.lower()
    assert (
        f'data-media-id="{assets["playa"].id}"' in frag
        and f'data-media-id="{assets["monte"].id}"' not in frag
    )
    assert 'hx-swap-oob="true"' in frag and "1 coincide" in frag
    videos = client.get("/biblioteca", params={"tipo": "video"}, headers=hx).text
    assert (
        f'data-media-id="{assets["video"].id}"' in videos
        and f'data-media-id="{assets["playa"].id}"' not in videos
    )
    none = client.get("/biblioteca", params={"q": "zzz"}, headers=hx).text
    assert "No hay resultados" in none


def test_library_pagination(client: TestClient, db: Session, brand: Brand) -> None:
    import io

    from planificador.routes import media as media_routes

    for i in range(media_routes.PAGE_SIZE + 2):
        media.ingest(db, io.BytesIO(png((8, 8), (i, 0, 0))), f"img-{i:02d}.png", brand.id)
    text = client.get("/biblioteca").text
    assert "Cargar más" in text and text.count('class="media-card"') == media_routes.PAGE_SIZE
    more = client.get(
        "/biblioteca", params={"pagina": 2}, headers={"HX-Request": "true", "HX-Target": "media-more"}
    )
    assert 'hx-swap-oob="beforeend:#media-grid"' in more.text
    assert more.text.count('class="media-card"') == 2 and "Cargar más" not in more.text


def test_detail_panel(client: TestClient, db: Session, brand: Brand, tmp_path: Path) -> None:
    assets = _seed(db, brand.id, tmp_path)
    video = assets["video"]
    hx = {"HX-Request": "true", "HX-Target": "media-detail"}
    frag = client.get(f"/medios/{video.id}", headers=hx).text
    assert "<html" not in frag.lower() and "data-media-detail" in frag
    for label in (
        "Facebook",
        "Instagram",
        "TikTok",
        "YouTube",
        "Compatibilidad por red",
        "Duración",
        "Códecs",
    ):
        assert label in frag, label
    assert "H.264" in frag and f'src="/medios/{video.id}/archivo"' in frag
    assert "Vertical" not in frag or "Horizontal" in frag
    redirect = client.get(f"/medios/{video.id}", follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == f"/biblioteca?medio={video.id}"
    page = client.get(f"/biblioteca?medio={video.id}").text
    assert "has-detail" in page and "data-media-detail" in page
    assert client.get("/medios/999999", headers=hx).status_code == 404


def test_detail_hints_public_media_for_photos(
    client: TestClient, db: Session, brand: Brand, tmp_path: Path
) -> None:
    assets = _seed(db, brand.id, tmp_path)
    hx = {"HX-Request": "true", "HX-Target": "media-detail"}
    frag = client.get(f"/medios/{assets['playa'].id}", headers=hx).text
    assert "activa la URL pública" in frag
    from planificador import config

    config.set_setting(db, "public_media_mode", "tunnel")
    assert "activa la URL pública" not in client.get(f"/medios/{assets['playa'].id}", headers=hx).text


# ---------------------------------------------------------------------------------------------- files


def test_file_and_thumbnail_routes(client: TestClient, db: Session, brand: Brand, tmp_path: Path) -> None:
    assets = _seed(db, brand.id, tmp_path)
    video = assets["video"]
    r = client.get(f"/medios/{video.id}/archivo")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.content == media.asset_path(video).read_bytes()
    ranged = client.get(f"/medios/{video.id}/archivo", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100
    download = client.get(f"/medios/{video.id}/archivo?descargar=1")
    assert download.headers["content-disposition"].startswith("attachment")
    assert "Promo.mp4" in unquote(download.headers["content-disposition"])
    thumb = client.get(f"/medios/{video.id}/miniatura")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    media.thumb_path(video).unlink()
    assert client.get(f"/medios/{video.id}/miniatura").status_code == 200  # regenerated
    assert client.get("/medios/424242/archivo").status_code == 404
    assert client.get("/medios/424242/miniatura").status_code == 404
    assert client.get("/medios/abc/archivo").status_code in (404, 422)


def test_api_json(client: TestClient, db: Session, brand: Brand, tmp_path: Path) -> None:
    assets = _seed(db, brand.id, tmp_path)
    data = client.get(f"/api/medios/{assets['video'].id}").json()
    assert ITEM_KEYS <= set(data)
    assert data["mime"] == "video/mp4" and data["video_codec"] == "h264" and data["aspect"] == "4:3"
    assert data["usage"] == 0 and data["deleted"] is False
    compat = data["compat"]
    assert set(compat) == {"facebook", "instagram", "tiktok", "youtube"}
    assert compat["instagram"]["status"] in ("ok", "warning", "error")
    assert {"label", "status", "formats", "issues"} <= set(compat["tiktok"])
    listing = client.get("/api/medios", params={"tipo": "image"}).json()
    assert listing["total"] == 2 and {i["kind"] for i in listing["items"]} == {"image"}
    assert client.get("/api/medios/999999").status_code == 404


# -------------------------------------------------------------------------------------------- actions


def test_rename(csrf_client, db: Session, brand: Brand, tmp_path: Path) -> None:
    assets = _seed(db, brand.id, tmp_path)
    asset = assets["monte"]
    hx = {"HX-Request": "true", "HX-Target": "media-detail"}
    r = csrf_client.post(f"/medios/{asset.id}/renombrar", data={"name": "Pico nevado.png"}, headers=hx)
    assert r.status_code == 200 and 'value="Pico nevado.png"' in r.text
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["toast"]["message"] == "Nombre actualizado" and trigger["media:changed"]["id"] == asset.id
    db.expire_all()
    assert db.get(MediaAsset, asset.id).original_name == "Pico nevado.png"
    empty = csrf_client.post(f"/medios/{asset.id}/renombrar", data={"name": "  "}, headers=hx)
    assert empty.status_code == 422 and unquote(empty.headers["X-Error-Message"]) == "Escribe un nombre."


def test_delete_blocked_while_pending_then_allowed(
    csrf_client, client: TestClient, db: Session, brand: Brand, demo_account, tmp_path: Path
) -> None:
    assets = _seed(db, brand.id, tmp_path)
    asset = assets["playa"]
    post = Post(brand_id=brand.id, text="Hola")
    post.set_media([asset])
    target = PostTarget(
        account_id=demo_account.id, network="demo", format="post", status=TargetStatus.SCHEDULED
    )
    post.targets.append(target)
    db.add(post)
    db.commit()

    assert client.post(f"/medios/{asset.id}/borrar").status_code == 403  # no CSRF token
    hx = {"HX-Request": "true", "HX-Target": "media-detail"}
    detail = client.get(f"/medios/{asset.id}", headers=hx).text
    assert "publicación pendiente" in detail and "disabled" in detail
    blocked = csrf_client.post(f"/medios/{asset.id}/borrar", headers=hx)
    assert blocked.status_code == 409
    assert "1 publicación pendiente" in unquote(blocked.headers["X-Error-Message"])

    target.status = TargetStatus.PUBLISHED
    db.commit()
    ok = csrf_client.post(f"/medios/{asset.id}/borrar", headers=hx)
    assert ok.status_code == 200 and ok.text == ""
    trigger = json.loads(ok.headers["HX-Trigger"])
    assert (
        trigger["media:deleted"] == {"id": asset.id}
        and "quitado de la biblioteca" in trigger["toast"]["message"]
    )
    db.expire_all()
    assert db.get(MediaAsset, asset.id).deleted_at is not None
    assert f'data-media-id="{asset.id}"' not in client.get("/biblioteca").text
    # Files of deleted assets stay reachable (published posts and history still show them).
    assert client.get(f"/medios/{asset.id}/miniatura").status_code == 200
    again = csrf_client.delete(f"/medios/{asset.id}", headers=JSON)
    assert again.status_code == 404 and again.json()["detail"]


def test_delete_json(csrf_client, db: Session, brand: Brand, tmp_path: Path) -> None:
    assets = _seed(db, brand.id, tmp_path)
    r = csrf_client.delete(f"/medios/{assets['monte'].id}", headers=JSON)
    assert r.status_code == 200 and r.json()["ok"] is True


# --------------------------------------------------------------------------------------------- picker


def test_picker_fragment_contract(client: TestClient, db: Session, brand: Brand, tmp_path: Path) -> None:
    assets = _seed(db, brand.id, tmp_path)
    r = client.get(
        "/biblioteca/selector",
        params={"multiple": 0, "kind": "video"},
        headers={"HX-Request": "true", "HX-Target": "modal-root"},
    )
    assert r.status_code == 200
    text = r.text
    assert "<html" not in text.lower() and 'role="dialog"' in text and 'id="media-picker"' in text
    assert "data-media-picker" in text and 'data-multiple="0"' in text and "media:selected" in text
    assert 'type="radio"' in text and 'type="checkbox"' not in text
    assert f'value="{assets["video"].id}"' in text and f'value="{assets["playa"].id}"' not in text
    raw = re.findall(r"data-item='([^']+)'", text)
    items = [json.loads(html.unescape(x)) for x in raw]
    assert len(items) == 1 and set(items[0]) == ITEM_KEYS
    assert items[0]["kind"] == "video" and items[0]["thumb_url"].endswith("/miniatura")

    multi = client.get("/biblioteca/selector", params={"multiple": 1, "kind": "any"}).text
    assert multi.count('type="checkbox"') == 3 and 'name="tipo"' in multi
    results = client.get(
        "/biblioteca/selector",
        params={"multiple": 1, "kind": "any", "q": "playa"},
        headers={"HX-Request": "true", "HX-Target": "picker-results"},
    ).text
    assert 'id="media-picker"' not in results and results.count('type="checkbox"') == 1


def test_picker_empty_state(client: TestClient) -> None:
    text = client.get("/biblioteca/selector").text
    assert "Todavía no hay archivos" in text and "data-upload-trigger" in text

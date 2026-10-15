"""Composer routes: overlay fragment vs full page, query presets, edit/read-only, save API (CSRF, JSON and
form payloads, HX-Trigger), validation API, TikTok creator info and YouTube categories/playlists (respx)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from planificador import config, postsvc
from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    MediaAsset,
    MediaKind,
    Post,
    PostKind,
    TargetStatus,
)
from planificador.postsvc import PostInput, TargetInput

HX = {"HX-Request": "true", "HX-Target": "modal-root"}


# --------------------------------------------------------------------------------------------- helpers


def state_of(html: str) -> dict:
    m = re.search(r'<script type="application/json" data-composer-state>(.*?)</script>', html, re.S)
    assert m, "composer state not found"
    return json.loads(m.group(1))


def account(db, brand, network, **kw) -> Account:
    acc = Account(
        brand_id=brand.id,
        network=network,
        external_id=kw.pop("external_id", f"{network}-ext"),
        name=kw.pop("name", f"Cuenta {network}"),
        username=kw.pop("username", None),
        account_type=kw.pop("account_type", None),
        status=kw.pop("status", AccountStatus.CONNECTED),
        meta=kw.pop("meta", {}),
    )
    token = kw.pop("token", None)
    if token:
        acc.set_tokens(token, "refresh-token")
    db.add(acc)
    db.commit()
    return acc


def asset(db, brand, name="foto.jpg", kind="image", **kw) -> MediaAsset:
    sha = name.encode().hex().ljust(64, "0")[:64]
    a = MediaAsset(
        brand_id=brand.id if brand else None,
        kind=MediaKind(kind),
        original_name=name,
        rel_path=f"originals/{sha[:2]}/{sha}.bin",
        sha256=sha,
        size_bytes=1000,
        mime="image/jpeg" if kind == "image" else "video/mp4",
        width=1080,
        height=1350 if kind == "image" else 1920,
        duration_s=None if kind == "image" else 10.0,
        fps=None if kind == "image" else 30.0,
        video_codec=None if kind == "image" else "h264",
        **kw,
    )
    db.add(a)
    db.commit()
    return a


def wall(dt: datetime, tz: str = "Europe/Madrid") -> str:
    from planificador.timeutil import local_wall_iso

    return local_wall_iso(dt, tz)[:16]


def payload(**kw) -> dict:
    body = {
        "action": "schedule",
        "layer": "overlay",
        "text": "Hola",
        "media_ids": [],
        "scheduled_at": None,
        "targets": [],
    }
    body.update(kw)
    return body


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def demo_on(db):
    config.set_setting(db, "demo_mode", True)


# ----------------------------------------------------------------------------------------------- pages


def test_new_full_page_and_fragment(client, brand):
    page = client.get("/publicaciones/nueva")
    assert page.status_code == 200
    assert page.text.lstrip().lower().startswith("<!doctype html")
    assert "Crear nueva publicación" in page.text
    assert "css/composer.css" in page.text
    assert state_of(page.text)["layer"] == "page"

    frag = client.get("/publicaciones/nueva", headers=HX)
    assert frag.status_code == 200
    assert "<html" not in frag.text.lower()
    assert 'class="composer composer--overlay"' in frag.text
    assert 'role="dialog"' in frag.text
    st = state_of(frag.text)
    assert st["layer"] == "overlay" and st["mode"] == "create" and st["primary"] == "schedule"
    assert st["urls"]["save"] == "/publicaciones"


def test_default_slot_is_next_full_hour(client, brand, clock):
    # 2026-11-03 09:00 UTC = 10:00 Madrid → +30 min → next full hour 11:00.
    st = state_of(client.get("/publicaciones/nueva", headers=HX).text)
    assert st["scheduled_local"] == "2026-11-03T11:00"
    assert st["now_local"] == "2026-11-03T10:00"


def test_fecha_presets(client, brand):
    st = state_of(client.get("/publicaciones/nueva?fecha=2026-11-05T18:30", headers=HX).text)
    assert st["scheduled_local"] == "2026-11-05T18:30"
    st = state_of(client.get("/publicaciones/nueva?fecha=2026-11-06", headers=HX).text)
    assert st["scheduled_local"] == "2026-11-06T10:00"
    st = state_of(client.get("/publicaciones/nueva?fecha=nope", headers=HX).text)
    assert st["scheduled_local"] == "2026-11-03T11:00"


def test_idea_presets(client, brand):
    st = state_of(client.get("/publicaciones/nueva?idea=1", headers=HX).text)
    assert st["primary"] == "idea" and st["idea_stage"] == "idea" and st["scheduled_local"] is None
    st = state_of(client.get("/publicaciones/nueva?tipo=idea&etapa=lista", headers=HX).text)
    assert st["idea_stage"] == "lista" and st["urls"]["back"] == "/ideas"
    st = state_of(client.get("/publicaciones/nueva?tipo=idea&etapa=rara", headers=HX).text)
    assert st["idea_stage"] == "idea"


def test_accounts_offered(client, db, brand, demo_on):
    fb = account(db, brand, "facebook")
    account(db, brand, "instagram", status=AccountStatus.DISCONNECTED)
    tt = account(
        db, brand, "tiktok", status=AccountStatus.NEEDS_RECONNECT, meta={"creator_info": {"nickname": "Ana"}}
    )
    yt = account(db, brand, "youtube", meta={"youtube_lock_status": "unlocked"})
    st = state_of(client.get("/publicaciones/nueva?red=tiktok", headers=HX).text)
    keys = [a["key"] for a in st["accounts"]]
    assert keys == [str(fb.id), str(tt.id), str(yt.id), "demo"]  # disconnected IG hidden; pseudo demo
    by_key = {a["key"]: a for a in st["accounts"]}
    assert by_key[str(tt.id)]["status"] == "needs_reconnect"
    assert by_key[str(tt.id)]["creator_info"] == {"nickname": "Ana"}
    assert (
        by_key[str(yt.id)]["auto_publish_default"] is True and by_key[str(yt.id)]["youtube_unlocked"] is True
    )
    assert by_key["demo"]["pseudo"] is True and by_key["demo"]["id"] is None
    fmt_keys = [f["key"] for f in by_key[str(fb.id)]["formats"]]
    assert fmt_keys == ["post", "reel", "story"]
    tt_formats = {f["key"]: f for f in by_key[str(tt.id)]["formats"]}
    assert tt_formats["photo"]["disabled"] and tt_formats["video"]["limit"] == 2200
    assert by_key[str(tt.id)]["unit"] == "utf16"
    assert st["selected"] == [str(tt.id)]


def test_demo_hidden_when_demo_mode_off(client, db, brand, demo_mode_off):
    account(db, brand, "demo")
    st = state_of(client.get("/publicaciones/nueva", headers=HX).text)
    assert st["accounts"] == []
    assert "No hay cuentas conectadas" in client.get("/publicaciones/nueva", headers=HX).text


def test_medios_preset(client, db, brand):
    a = asset(db, brand, "a.jpg")
    other = Brand(name="Otra", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    b = asset(db, other, "b.jpg")
    st = state_of(client.get(f"/publicaciones/nueva?medios={a.id},{b.id},x", headers=HX).text)
    assert [m["id"] for m in st["media"]] == [a.id]
    assert st["media"][0]["thumb_url"] == f"/medios/{a.id}/miniatura"


def test_recent_hashtags(client, db, brand, clock):
    for text in ("Hola #Verano #playa", "Otra #verano", "#cafe"):
        postsvc.create_post(db, PostInput(brand.id, text, [], None), "draft", clock)
    st = state_of(client.get("/publicaciones/nueva", headers=HX).text)
    assert st["recent_hashtags"][0] == "#verano"
    assert set(st["recent_hashtags"]) == {"#verano", "#playa", "#cafe"}


def test_edit_page(client, db, brand, clock):
    fb = account(db, brand, "facebook")
    img = asset(db, brand)
    post = postsvc.create_post(
        db,
        PostInput(
            brand.id,
            "Texto",
            [img.id],
            clock.now() + timedelta(days=1),
            notes="nota",
            targets=[TargetInput(fb.id, "post", text_override="Solo FB", options={"link": ""})],
        ),
        "draft",
        clock,
    )
    page = client.get(f"/publicaciones/{post.id}/editar")
    assert page.status_code == 200 and "Editar publicación" in page.text
    st = state_of(page.text)
    assert st["mode"] == "edit" and st["post_id"] == post.id and not st["readonly"]
    assert st["selected"] == [str(fb.id)]
    assert st["targets"][str(fb.id)]["text_override"] == "Solo FB"
    assert st["text"] == "Texto" and st["notes"] == "nota" and [m["id"] for m in st["media"]] == [img.id]
    assert st["urls"]["save"] == f"/publicaciones/{post.id}"
    assert st["scheduled_local"] == wall(post.scheduled_at)

    frag = client.get(f"/publicaciones/{post.id}/editar", headers=HX)
    assert "<html" not in frag.text.lower()


def test_edit_readonly_and_missing(client, db, brand, clock):
    fb = account(db, brand, "facebook")
    post = postsvc.create_post(
        db, PostInput(brand.id, "x", [], None, targets=[TargetInput(fb.id, "post")]), "draft", clock
    )
    post.targets[0].status = TargetStatus.PUBLISHED
    db.commit()
    page = client.get(f"/publicaciones/{post.id}/editar", headers=HX)
    st = state_of(page.text)
    assert st["readonly"] is True and "ya se ha publicado" in st["readonly_reason"]
    assert "Solo lectura" in page.text and "Ver publicación" in page.text
    assert client.get("/publicaciones/9999/editar").status_code == 404
    postsvc.delete_post(db, post.id, clock)
    assert client.get(f"/publicaciones/{post.id}/editar").status_code == 404


# ------------------------------------------------------------------------------------------------ save


def test_save_requires_csrf(client, brand):
    r = client.post("/publicaciones", json=payload())
    assert r.status_code == 403


def test_save_schedule(csrf_client, db, brand, clock, demo_on):
    demo = account(db, brand, "demo")
    when = clock.now() + timedelta(days=1)
    body = payload(
        text="Hola #mundo",
        scheduled_at=wall(when),
        targets=[
            {"account_id": demo.id, "format": "post", "options": {"demo_steps": 2}, "auto_publish": True}
        ],
    )
    r = csrf_client.post("/publicaciones", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and data["action"] == "schedule" and data["status"] == "scheduled"
    assert data["redirect"].startswith("/planificacion?fecha=")
    assert "Publicación programada para el" in data["message"]
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["closeModal"] is True and trigger["planner:refresh"] is True
    assert trigger["toast"]["kind"] == "success" and trigger["posts:changed"]["post_id"] == data["post_id"]
    db.expire_all()
    post = db.get(Post, data["post_id"])
    assert post.scheduled_at == when.replace(second=0, microsecond=0)
    assert post.targets[0].status == TargetStatus.SCHEDULED
    assert post.targets[0].options == {"demo_steps": 2}


def test_save_validation_error_and_draft(csrf_client, db, brand, clock, demo_on):
    demo = account(db, brand, "demo")
    past = wall(clock.now() - timedelta(hours=1))
    body = payload(text="", scheduled_at=past, targets=[{"account_id": demo.id, "format": "post"}])
    r = csrf_client.post("/publicaciones", json=body)
    assert r.status_code == 422
    data = r.json()
    assert not data["ok"] and data["code"] == "validation"
    codes = {e["code"] for e in data["validation"]["errors"]}
    assert {"date_past", "demo_empty"} <= codes
    assert data["validation"]["can_schedule"] is False
    assert db.scalar(select(Post.id)) is None

    body["action"] = "draft"
    r = csrf_client.post("/publicaciones", json=body)
    assert r.status_code == 200 and r.json()["message"] == "Borrador guardado."
    assert "planner:refresh" in r.headers["HX-Trigger"]


def test_save_idea_and_demo_pseudo_account(csrf_client, db, brand, clock, demo_on):
    body = payload(
        action="idea",
        text="Idea",
        idea_stage="en_progreso",
        targets=[{"account_id": "demo", "format": "post"}],
    )
    r = csrf_client.post("/publicaciones", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["redirect"] == "/ideas" and data["message"] == "Idea guardada."
    demo_id = data["account_map"]["demo"]
    assert db.get(Account, demo_id).network == "demo"
    post = db.get(Post, data["post_id"])
    assert post.kind == PostKind.IDEA and post.idea_stage == "en_progreso"


def test_pseudo_demo_refused_without_demo_mode(csrf_client, brand, demo_mode_off):
    r = csrf_client.post(
        "/publicaciones", json=payload(action="draft", targets=[{"account_id": "demo", "format": "post"}])
    )
    assert r.status_code == 422 and "modo prueba" in r.json()["message"]


def test_save_publish_now(csrf_client, db, brand, clock, demo_on):
    demo = account(db, brand, "demo")
    r = csrf_client.post(
        "/publicaciones",
        json=payload(action="publish_now", targets=[{"account_id": demo.id, "format": "post"}]),
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    target = db.get(Post, r.json()["post_id"]).targets[0]
    assert target.status == TargetStatus.SCHEDULED and target.scheduled_at == clock.now()


@pytest.mark.parametrize(
    "body",
    [
        {"action": "nope"},
        payload(targets="x"),
        payload(targets=[{"account_id": "abc", "format": "post"}]),
        payload(targets=[{"account_id": 1, "format": "post", "options": []}]),
        payload(media_ids="1"),
        payload(scheduled_at="mañana"),
        payload(text={"a": 1}),
    ],
)
def test_save_bad_payloads(csrf_client, brand, body):
    r = csrf_client.post("/publicaciones", json=body)
    assert r.status_code == 422
    assert r.json()["ok"] is False


def test_save_rejects_non_object_json(csrf_client, brand):
    r = csrf_client.post("/publicaciones", content=b"[1,2]", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    r = csrf_client.post("/publicaciones", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_save_form_payload_and_page_flash(csrf_client, db, brand, clock):
    body = payload(action="draft", layer="page", text="Desde formulario")
    r = csrf_client.post("/publicaciones", data={"payload": json.dumps(body)})
    assert r.status_code == 200, r.text
    assert db.get(Post, r.json()["post_id"]).text == "Desde formulario"
    page = csrf_client.get("/publicaciones/nueva")
    assert "Borrador guardado." in page.text  # flashed for the next full page


def test_update_post(csrf_client, db, brand, clock, demo_on):
    demo = account(db, brand, "demo")
    post = postsvc.create_post(
        db, PostInput(brand.id, "Antes", [], None, targets=[TargetInput(demo.id, "post")]), "draft", clock
    )
    when = wall(clock.now() + timedelta(days=2))
    body = payload(text="Después", scheduled_at=when, targets=[{"account_id": demo.id, "format": "post"}])
    r = csrf_client.post(f"/publicaciones/{post.id}", json=body)
    assert r.status_code == 200, r.text
    db.expire_all()
    post = db.get(Post, post.id)
    assert post.text == "Después" and post.targets[0].status == TargetStatus.SCHEDULED

    post.targets[0].status = TargetStatus.PUBLISHED
    db.commit()
    r = csrf_client.post(f"/publicaciones/{post.id}", json=body)
    assert r.status_code == 409 and r.json()["code"] == "locked"
    assert csrf_client.post("/publicaciones/4040", json=body).status_code == 404


def test_update_other_brand_post_is_404(csrf_client, db, brand, clock):
    other = Brand(name="Otra", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    post = postsvc.create_post(db, PostInput(other.id, "x", [], None), "draft", clock)
    r = csrf_client.post(f"/publicaciones/{post.id}", json=payload(action="draft"))
    assert r.status_code == 404


# ------------------------------------------------------------------------------------------ validation


def test_validate_api(csrf_client, db, brand, clock):
    tt = account(db, brand, "tiktok", meta={"creator_info": {"privacy_level_options": ["SELF_ONLY"]}})
    ig = account(db, brand, "instagram", account_type="business")
    video = asset(db, brand, "v.mp4", "video")
    text = "Hola 😀 #uno #dos"
    body = payload(
        action=None,
        text=text,
        media_ids=[video.id],
        scheduled_at=wall(clock.now() + timedelta(hours=3)),
        targets=[
            {"account_id": tt.id, "format": "video", "options": {"consent": True}},
            {"account_id": ig.id, "format": "reel", "options": {}},
        ],
    )
    r = csrf_client.post("/api/composer/validar", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    err = next(e for e in data["errors"] if e["code"] == "tiktok_privacy_required")
    assert err["account_key"] == str(tt.id) and err["network"] == "tiktok" and err["field"] == "privacy_level"
    assert any(w["code"] == "tiktok_unaudited" for w in data["warnings"])
    assert data["can_schedule"] is False and data["can_publish_now"] is False
    counters = data["counters"]
    assert counters[str(tt.id)]["length"] == len(text.encode("utf-16-le")) // 2  # emoji = 2 UTF-16 units
    assert counters[str(ig.id)]["length"] == len(text)
    assert counters[str(ig.id)]["hashtags"] == 2 and counters[str(ig.id)]["limit"] == 2200
    assert data["scheduled_label"].endswith("2026, 13:00")

    body["targets"][0]["options"]["privacy_level"] = "SELF_ONLY"
    data = csrf_client.post("/api/composer/validar", json=body).json()
    assert data["can_schedule"] is True, data["errors"]


def test_validate_api_general_and_idea(csrf_client, brand, clock):
    data = csrf_client.post("/api/composer/validar", json=payload(action=None)).json()
    assert {e["code"] for e in data["errors"]} == {"no_targets", "date_required"}
    assert all(e["account_id"] is None for e in data["errors"])
    data = csrf_client.post("/api/composer/validar", json=payload(action="idea")).json()
    assert data["errors"] == []
    data = csrf_client.post(
        "/api/composer/validar",
        json=payload(action="publish_now", scheduled_at=wall(clock.now() - timedelta(days=1))),
    ).json()
    assert {e["code"] for e in data["errors"]} == {"no_targets"}


def test_validate_api_demo_pseudo(csrf_client, db, brand, demo_on):
    data = csrf_client.post(
        "/api/composer/validar", json=payload(targets=[{"account_id": "demo", "format": "post"}])
    ).json()
    demo_id = data["account_map"]["demo"]
    assert "demo" in data["counters"]
    assert db.get(Account, demo_id).brand_id == brand.id


def test_validate_requires_csrf(client, brand):
    assert client.post("/api/composer/validar", json=payload()).status_code == 403


# -------------------------------------------------------------------------------------- TikTok creator


def test_creator_info_fake_in_demo_mode(client, db, brand, demo_on):
    tt = account(db, brand, "tiktok", external_id="demo-tt", name="Ana")
    r = client.get(f"/api/tiktok/{tt.id}/creator-info")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and data["demo"] is True and data["creator_info"]["nickname"] == "Ana"
    assert {o["label"] for o in data["privacy_options"]} >= {"Público", "Solo yo"}
    db.expire_all()
    cached = db.get(Account, tt.id).meta["creator_info"]
    assert cached["max_video_post_duration_sec"] == 600 and "fetched_at" in cached


def test_creator_info_real(client, db, brand, demo_mode_off, router):
    from planificador.providers import tiktok

    tt = account(db, brand, "tiktok", token="tok-123")
    route = router.post(tiktok.CREATOR_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "creator_nickname": "Ana TikTok",
                    "creator_username": "ana",
                    "creator_avatar_url": "https://p16.example/avatar.jpg",
                    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                    "comment_disabled": True,
                    "duet_disabled": False,
                    "stitch_disabled": False,
                    "max_video_post_duration_sec": 300,
                },
                "error": {"code": "ok", "message": "", "log_id": "x"},
            },
        )
    )
    data = client.get(f"/api/tiktok/{tt.id}/creator-info").json()
    assert route.called
    assert data["ok"] and not data["demo"]
    assert (
        data["creator_info"]["nickname"] == "Ana TikTok" and data["creator_info"]["comment_disabled"] is True
    )
    assert [o["value"] for o in data["privacy_options"]] == ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]
    db.expire_all()
    assert db.get(Account, tt.id).meta["creator_info"]["max_video_post_duration_sec"] == 300


def test_creator_info_error_returns_cached(client, db, brand, demo_mode_off, router):
    from planificador.providers import tiktok

    cached = {"nickname": "Antes", "privacy_level_options": ["SELF_ONLY"]}
    tt = account(db, brand, "tiktok", token="tok-123", meta={"creator_info": cached})
    router.post(tiktok.CREATOR_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {},
                "error": {"code": "spam_risk_too_many_posts", "message": "too many", "log_id": "x"},
            },
        )
    )
    data = client.get(f"/api/tiktok/{tt.id}/creator-info").json()
    assert data["ok"] is False and data["message"]
    assert data["creator_info"] == cached
    assert data["privacy_options"] == [{"value": "SELF_ONLY", "label": "Solo yo"}]


def test_creator_info_not_found(client, db, brand):
    fb = account(db, brand, "facebook")
    assert client.get(f"/api/tiktok/{fb.id}/creator-info").status_code == 404
    assert client.get("/api/tiktok/999/creator-info").status_code == 404


# -------------------------------------------------------------------------------------------- YouTube


def test_youtube_lists_fake(client, db, brand, demo_on):
    yt = account(db, brand, "youtube", external_id="demo-yt")
    cats = client.get(f"/api/youtube/{yt.id}/categorias").json()
    assert cats["demo"] and {"id": "22", "title": "Gente y blogs"} in cats["items"]
    lists = client.get(f"/api/youtube/{yt.id}/listas").json()
    assert lists["demo"] and lists["items"][0]["title"] == "Tutoriales"


def test_youtube_categories_real_and_cached(client, db, brand, demo_mode_off, router):
    from planificador.providers import youtube

    yt = account(db, brand, "youtube", token="ya29.token")
    route = router.get(f"{youtube.API_BASE}/videoCategories").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": "22", "snippet": {"title": "Gente y blogs", "assignable": True}},
                    {"id": "18", "snippet": {"title": "Cortos", "assignable": False}},
                    {"id": "27", "snippet": {"title": "Educación", "assignable": True}},
                ]
            },
        )
    )
    first = client.get(f"/api/youtube/{yt.id}/categorias").json()
    assert first["ok"] and [c["id"] for c in first["items"]] == ["22", "27"]
    second = client.get(f"/api/youtube/{yt.id}/categorias").json()
    assert second["cached"] is True and route.call_count == 1
    client.get(f"/api/youtube/{yt.id}/categorias?refrescar=1")
    assert route.call_count == 2


def test_youtube_playlists_real_and_error(client, db, brand, demo_mode_off, router):
    from planificador.providers import youtube

    yt = account(db, brand, "youtube", token="ya29.token")
    route = router.get(f"{youtube.API_BASE}/playlists").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "PL1", "snippet": {"title": "Recetas"}}]})
    )
    data = client.get(f"/api/youtube/{yt.id}/listas").json()
    assert data["items"] == [{"id": "PL1", "title": "Recetas"}]
    route.mock(
        return_value=httpx.Response(
            403, json={"error": {"code": 403, "message": "forbidden", "errors": [{"reason": "forbidden"}]}}
        )
    )
    data = client.get(f"/api/youtube/{yt.id}/listas?refrescar=1").json()
    assert data["ok"] is False and data["items"] == [{"id": "PL1", "title": "Recetas"}]

    cats = router.get(f"{youtube.API_BASE}/videoCategories").mock(return_value=httpx.Response(500, json={}))
    data = client.get(f"/api/youtube/{yt.id}/categorias").json()
    assert cats.called and data["ok"] is False and len(data["items"]) >= 10  # built-in fallback


def test_youtube_lists_not_found(client, db, brand):
    tt = account(db, brand, "tiktok")
    assert client.get(f"/api/youtube/{tt.id}/categorias").status_code == 404
    assert client.get(f"/api/youtube/{tt.id}/listas").status_code == 404


def test_edit_shows_status_and_last_errors(client, db, brand, clock):
    fb = account(db, brand, "facebook")
    post = postsvc.create_post(
        db, PostInput(brand.id, "x", [], None, targets=[TargetInput(fb.id, "post")]), "draft", clock
    )
    post.targets[0].status = TargetStatus.ERROR
    post.targets[0].last_error = "Facebook rechazó la publicación (simulado)."
    db.commit()
    html = client.get(f"/publicaciones/{post.id}/editar", headers=HX).text
    assert "Hubo un problema al publicar" in html
    assert "Facebook rechazó la publicación (simulado)." in html
    assert "st-error" in html  # status badge in the header
    assert state_of(html)["readonly"] is False


def test_youtube_lookup_keeps_a_refreshed_token(client, db, brand, demo_mode_off, router):
    """Listing categories with an expired access token refreshes it; the new token is saved (encrypted)."""
    from datetime import UTC, datetime

    from planificador import config
    from planificador.providers import youtube

    long_ago = datetime(2020, 1, 1, tzinfo=UTC)  # the YouTube helpers use the real clock
    config.set_setting(db, "google_client_id", "client-id.apps.googleusercontent.com")
    config.set_setting(db, "google_client_secret", "client-secret")
    yt = account(db, brand, "youtube", token="ya29.old")
    yt.token_expires_at = long_ago
    db.commit()
    router.post(youtube.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ya29.new", "expires_in": 3599, "token_type": "Bearer"}
        )
    )
    router.get(f"{youtube.API_BASE}/videoCategories").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "22", "snippet": {"title": "Gente", "assignable": True}}]}
        )
    )
    assert client.get(f"/api/youtube/{yt.id}/categorias").json()["ok"]
    db.expire_all()
    saved = db.get(Account, yt.id)
    assert saved.access_token == "ya29.new" and saved.refresh_token == "refresh-token"
    assert saved.token_expires_at is not None and saved.token_expires_at > long_ago

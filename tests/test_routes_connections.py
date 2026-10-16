"""Cuentas conectadas: OAuth orchestration (Meta, TikTok, YouTube), demo, disconnect, lock test, guides.

Network calls are mocked with respx (full provider code paths) or by monkeypatching provider functions.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config, crypto
from planificador.models import Account, AccountStatus, Brand, OAuthState, Post, PostTarget, TargetStatus
from planificador.providers.base import ErrorKind, ProviderError
from planificador.routes import connections as conn

BASE_URL = "http://localhost:8765"
GRAPH = "https://graph.facebook.com/v25.0"
TT_API = "https://open.tiktokapis.com"
FLASH_RE = '<script type="application/json" id="flash-data">'


# ------------------------------------------------------------------------------------------- helpers


def png_bytes(color: tuple[int, int, int] = (200, 30, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def flashes(html: str) -> list[str]:
    start = html.index(FLASH_RE) + len(FLASH_RE)
    end = html.index("</script>", start)
    return [f["message"] for f in json.loads(html[start:end])]


def location(response: httpx.Response) -> str:
    assert response.status_code in (302, 303, 307), (response.status_code, response.text[:300])
    return response.headers["location"]


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def set_meta(db: Session) -> None:
    config.set_setting(db, "meta_app_id", "1234567890")
    config.set_setting(db, "meta_app_secret", "meta-secret-xyz", secret=True)


def set_tiktok(db: Session) -> None:
    config.set_setting(db, "tiktok_client_key", "ttkey123456")
    config.set_setting(db, "tiktok_client_secret", "tt-secret-abc", secret=True)


def set_google(db: Session) -> None:
    config.set_setting(db, "google_client_id", "123-abc.apps.googleusercontent.com")
    config.set_setting(db, "google_client_secret", "GOCSPX-secret", secret=True)


def start(client: TestClient, path: str) -> tuple[str, dict[str, str]]:
    r = client.get(path, follow_redirects=False)
    url = location(r)
    return url, query_of(url)


def account(db: Session, brand_id: int, network: str) -> Account | None:
    db.expire_all()
    return db.scalar(select(Account).where(Account.brand_id == brand_id, Account.network == network))


@pytest.fixture
def inline_background(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    def run(fn: Any, name: str) -> None:
        started.append(name)
        fn()

    monkeypatch.setattr(conn, "_start_background", run)
    return started


# ------------------------------------------------------------------------------------------- page


def test_page_lists_networks_with_missing_credentials(
    client: TestClient, brand: Brand, demo_mode_off: bool
) -> None:
    r = client.get("/conexiones")
    assert r.status_code == 200
    html = r.text
    for label in ("Facebook", "Instagram", "TikTok", "YouTube"):
        assert f'id="conn-{label.lower()}"' in html
    assert 'id="conn-demo"' not in html  # demo mode off
    assert "Sin conectar" in html
    assert "Falta configurar tu app de Meta" in html and 'href="/ajustes#credenciales-meta"' in html
    assert 'href="/ajustes#credenciales-tiktok"' in html and 'href="/ajustes#credenciales-google"' in html
    # Exact redirect URIs to register + guides.
    assert "http://localhost:8765/conexiones/callback/meta" in html
    assert "http://localhost:8765/conexiones/callback/social1" in html
    assert "http://localhost:8765/conexiones/callback/google" in html
    assert 'href="/ayuda/meta"' in html and 'href="/ayuda/tiktok"' in html and 'href="/ayuda/youtube"' in html
    assert "0 de 4" in html


def test_page_shows_connected_cards(
    client: TestClient, db: Session, brand: Brand, demo_account: Account, demo_mode: bool
) -> None:
    set_meta(db)
    fb = Account(
        brand_id=brand.id,
        network="facebook",
        external_id="111",
        name="Mi Página",
        account_type="page",
        status=AccountStatus.NEEDS_RECONNECT,
        last_error="El token ya no es válido.",
    )
    fb.set_tokens("tok")
    db.add(fb)
    db.commit()
    html = client.get("/conexiones").text
    assert 'id="conn-demo"' in html and "Cuenta Demo" in html
    assert "Reconexión necesaria" in html and "El token ya no es válido." in html
    assert "Facebook necesita que la vuelvas a conectar" in html
    assert f'action="/conexiones/{fb.id}/desconectar"' in html
    assert 'href="/conexiones/meta/iniciar"' in html  # Reconectar


def test_card_fragment(client: TestClient, brand: Brand) -> None:
    r = client.get("/conexiones/tarjeta/tiktok")
    assert r.status_code == 200 and 'id="conn-tiktok"' in r.text and "<html" not in r.text
    assert client.get("/conexiones/tarjeta/myspace").status_code == 404


# ------------------------------------------------------------------------------------------- Meta


def mock_meta(router: respx.MockRouter, *, pages: list[dict] | None = None) -> dict[str, respx.Route]:
    def token(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if q.get("grant_type") == "fb_exchange_token":
            assert q["fb_exchange_token"] == "short-token"
            return httpx.Response(200, json={"access_token": "long-user-token", "expires_in": 5184000})
        assert q["code"] == "the-code"
        assert q["redirect_uri"] == "http://localhost:8765/conexiones/callback/meta"
        assert q["client_secret"] == "meta-secret-xyz"
        return httpx.Response(200, json={"access_token": "short-token", "expires_in": 3600})

    if pages is None:
        pages = [
            {
                "id": "page-1",
                "name": "Panadería Lucía",
                "access_token": "page-token-1",
                "category": "Panadería",
                "tasks": ["ANALYZE", "ADVERTISE", "MODERATE", "CREATE_CONTENT", "MANAGE"],
                "picture": {"data": {"url": "https://cdn.example.com/page1.png"}},
                "instagram_business_account": {
                    "id": "ig-1",
                    "username": "panaderia.lucia",
                    "name": "Panadería Lucía",
                    "profile_picture_url": "https://cdn.example.com/ig1.png",
                },
            },
            {
                "id": "page-2",
                "name": "Página Ajena",
                "access_token": "page-token-2",
                "tasks": ["ANALYZE"],
            },
        ]
    return {
        "token": router.get(f"{GRAPH}/oauth/access_token").mock(side_effect=token),
        "accounts": router.get(f"{GRAPH}/me/accounts").mock(
            return_value=httpx.Response(200, json={"data": pages})
        ),
        "businesses": router.get(f"{GRAPH}/me/businesses").mock(
            return_value=httpx.Response(200, json={"data": []})
        ),
        "permissions": router.get(f"{GRAPH}/me/permissions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"permission": p, "status": "granted"}
                        for p in (
                            "pages_show_list",
                            "pages_manage_posts",
                            "pages_read_engagement",
                            "read_insights",
                            "pages_manage_engagement",
                            "pages_read_user_content",
                            "instagram_basic",
                            "instagram_content_publish",
                            "instagram_manage_insights",
                            "instagram_manage_comments",
                        )
                    ]
                    + [{"permission": "business_management", "status": "declined"}]
                },
            )
        ),
        "pic1": router.get("https://cdn.example.com/page1.png").mock(
            return_value=httpx.Response(200, content=png_bytes())
        ),
        "pic2": router.get("https://cdn.example.com/ig1.png").mock(
            return_value=httpx.Response(200, content=png_bytes((10, 200, 10)))
        ),
    }


def test_meta_full_flow(client: TestClient, db: Session, brand: Brand, data_dir: Path) -> None:
    set_meta(db)
    url, q = start(client, "/conexiones/meta/iniciar")
    assert url.startswith("https://www.facebook.com/v25.0/dialog/oauth?")
    assert (
        q["client_id"] == "1234567890"
        and q["redirect_uri"] == "http://localhost:8765/conexiones/callback/meta"
    )
    assert "pages_manage_posts" in q["scope"] and q["response_type"] == "code"
    row = db.get(OAuthState, q["state"])
    assert row is not None and row.network == "meta" and row.brand_id == brand.id

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        routes = mock_meta(router)
        r = client.get(f"/conexiones/callback/meta?code=the-code&state={q['state']}", follow_redirects=False)
        chooser_url = location(r)
        assert chooser_url.startswith("/conexiones/meta/elegir?pendiente=")
        assert routes["token"].call_count == 2 and routes["accounts"].called
        db.expire_all()
        assert db.get(OAuthState, q["state"]) is None  # single use

        page = client.get(chooser_url)
        assert page.status_code == 200
        html = page.text
        assert "Panadería Lucía" in html and "@panaderia.lucia" in html and "Página Ajena" in html
        assert "CREATE_CONTENT" in html  # warning on the page without content permissions
        assert 'value="page-2"' in html and "disabled" in html
        assert "business_management" in html  # declined scope warning
        assert "long-user-token" not in html and "page-token-1" not in html  # tokens never reach the browser

        pending = query_of(chooser_url)["pendiente"]
        r = client.post(
            "/conexiones/meta/seleccionar",
            data={
                "csrf_token": conn_csrf(client),
                "pendiente": pending,
                "page_id": "page-1",
                "facebook": "1",
                "instagram": "1",
                "ig_type": "business",
            },
            follow_redirects=False,
        )
        assert location(r) == "/conexiones"
        assert routes["pic1"].called and routes["pic2"].called

    fb = account(db, brand.id, "facebook")
    ig = account(db, brand.id, "instagram")
    assert fb is not None and ig is not None
    assert fb.status == AccountStatus.CONNECTED and fb.external_id == "page-1" and fb.account_type == "page"
    assert fb.access_token == "page-token-1" and fb.access_token_enc.startswith("v1:")
    assert "page-token-1" not in fb.access_token_enc
    assert fb.meta["page_id"] == "page-1" and "CREATE_CONTENT" in fb.meta["tasks"]
    assert crypto.decrypt(fb.meta["user_token_enc"]) == "long-user-token"
    assert "pages_manage_posts" in fb.scopes
    assert ig.external_id == "ig-1" and ig.username == "panaderia.lucia" and ig.account_type == "business"
    assert ig.access_token == "page-token-1" and ig.meta["page_id"] == "page-1"
    # Avatars cached locally and served by id.
    assert fb.avatar_path and (data_dir / "media" / "avatars" / fb.avatar_path).is_file()
    avatar = client.get(f"/conexiones/avatar/{fb.id}")
    assert avatar.status_code == 200 and avatar.headers["content-type"] == "image/jpeg"
    html = client.get("/conexiones").text
    assert any("Conectado: Facebook" in m for m in flashes(html))
    assert any("business_management" in m for m in flashes(html))
    assert f"/conexiones/avatar/{fb.id}?v=" in html
    assert "2 de 4" in html


def conn_csrf(client: TestClient) -> str:
    return client.get("/api/csrf").json()["csrf_token"]


def _meta_pending(client: TestClient, db: Session, router: respx.MockRouter, **kw: Any) -> str:
    set_meta(db)
    _, q = start(client, "/conexiones/meta/iniciar")
    mock_meta(router, **kw)
    r = client.get(f"/conexiones/callback/meta?code=the-code&state={q['state']}", follow_redirects=False)
    return query_of(location(r))["pendiente"]


def test_meta_select_rejects_page_without_create_content(
    client: TestClient, db: Session, brand: Brand
) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        pending = _meta_pending(client, db, router)
        r = client.post(
            "/conexiones/meta/seleccionar",
            data={
                "csrf_token": conn_csrf(client),
                "pendiente": pending,
                "page_id": "page-2",
                "facebook": "1",
            },
            follow_redirects=False,
        )
        assert location(r).startswith("/conexiones/meta/elegir")
        # Instagram requested on a page without Instagram.
        r = client.post(
            "/conexiones/meta/seleccionar",
            data={"csrf_token": conn_csrf(client), "pendiente": pending, "page_id": "page-1"},
            follow_redirects=False,
        )
        assert location(r).startswith("/conexiones/meta/elegir")  # nothing ticked
    assert account(db, brand.id, "facebook") is None
    html = client.get("/conexiones").text
    msgs = flashes(html)
    assert any("CREATE_CONTENT" in m for m in msgs) and any("al menos una red" in m for m in msgs)


def test_meta_select_only_instagram_and_replaces_previous(
    client: TestClient, db: Session, brand: Brand
) -> None:
    old = Account(
        brand_id=brand.id,
        network="instagram",
        external_id="ig-old",
        name="Vieja",
        status=AccountStatus.CONNECTED,
    )
    old.set_tokens("old")
    old.meta = {"refresh_expiry_warned": "x"}
    db.add(old)
    db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        pending = _meta_pending(client, db, router)
        html = client.get(f"/conexiones/meta/elegir?pendiente={pending}").text
        assert "se sustituirá" in html
        r = client.post(
            "/conexiones/meta/seleccionar",
            data={
                "csrf_token": conn_csrf(client),
                "pendiente": pending,
                "page_id": "page-1",
                "instagram": "1",
            },
            follow_redirects=False,
        )
        assert location(r) == "/conexiones"
    ig = account(db, brand.id, "instagram")
    assert ig is not None and ig.id == old.id and ig.external_id == "ig-1"
    assert "refresh_expiry_warned" not in ig.meta and ig.account_type is None
    assert account(db, brand.id, "facebook") is None
    # The pending choice is single use.
    r = client.get(f"/conexiones/meta/elegir?pendiente={pending}", follow_redirects=False)
    assert location(r) == "/conexiones"


def test_meta_chooser_without_pages(client: TestClient, db: Session, brand: Brand) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        pending = _meta_pending(client, db, router, pages=[])
        html = client.get(f"/conexiones/meta/elegir?pendiente={pending}").text
    assert "No hemos encontrado ninguna página" in html


def test_meta_exchange_error_is_reported(client: TestClient, db: Session, brand: Brand) -> None:
    set_meta(db)
    _, q = start(client, "/conexiones/meta/iniciar")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(f"{GRAPH}/oauth/access_token").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Invalid verification code format.",
                        "type": "OAuthException",
                        "code": 100,
                    }
                },
            )
        )
        r = client.get(f"/conexiones/callback/meta?code=bad&state={q['state']}", follow_redirects=False)
    assert location(r) == "/conexiones"
    assert any("No se pudo conectar Facebook" in m for m in flashes(client.get("/conexiones").text))
    assert account(db, brand.id, "facebook") is None


def test_meta_start_without_credentials(client: TestClient, db: Session, brand: Brand) -> None:
    r = client.get("/conexiones/meta/iniciar", follow_redirects=False)
    assert location(r) == "/ajustes#credenciales-meta"
    assert db.scalar(select(OAuthState)) is None
    assert any("Falta configurar tu app de Meta" in m for m in flashes(client.get("/ajustes").text))


def test_meta_uses_config_id_when_set(client: TestClient, db: Session, brand: Brand) -> None:
    set_meta(db)
    config.set_setting(db, "meta_config_id", "998877665544")
    _, q = start(client, "/conexiones/meta/iniciar")
    assert q["config_id"] == "998877665544" and "scope" not in q


# ------------------------------------------------------------------------------------------- state checks


def test_callback_unknown_state(client: TestClient, db: Session, brand: Brand) -> None:
    set_meta(db)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        token = router.get(f"{GRAPH}/oauth/access_token")
        r = client.get("/conexiones/callback/meta?code=x&state=forged", follow_redirects=False)
        assert not token.called
    assert location(r) == "/conexiones"
    assert any("no es válida" in m for m in flashes(client.get("/conexiones").text))
    # Without any parameter (e.g. opened by hand) it never crashes.
    for path in ("/conexiones/callback/meta", "/conexiones/callback/social1", "/conexiones/callback/google"):
        assert location(client.get(path, follow_redirects=False)) == "/conexiones"


def test_callback_expired_state(client: TestClient, db: Session, brand: Brand, clock: Any) -> None:
    set_meta(db)
    _, q = start(client, "/conexiones/meta/iniciar")
    clock.advance(minutes=11)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        token = router.get(f"{GRAPH}/oauth/access_token")
        r = client.get(f"/conexiones/callback/meta?code=x&state={q['state']}", follow_redirects=False)
        assert not token.called
    assert location(r) == "/conexiones"
    db.expire_all()
    assert db.get(OAuthState, q["state"]) is None
    assert any("caducado" in m for m in flashes(client.get("/conexiones").text))


def test_callback_state_for_another_network(client: TestClient, db: Session, brand: Brand) -> None:
    set_tiktok(db)
    _, q = start(client, "/conexiones/tiktok/iniciar")
    r = client.get(f"/conexiones/callback/meta?code=x&state={q['state']}", follow_redirects=False)
    assert location(r) == "/conexiones"
    assert any("no es válida" in m for m in flashes(client.get("/conexiones").text))


def test_callback_from_another_browser_session(
    app: Any, client: TestClient, db: Session, brand: Brand
) -> None:
    set_meta(db)
    _, q = start(client, "/conexiones/meta/iniciar")
    with (
        TestClient(app, base_url=BASE_URL) as other,
        respx.mock(assert_all_mocked=True, assert_all_called=False) as router,
    ):
        token = router.get(f"{GRAPH}/oauth/access_token")
        r = other.get(f"/conexiones/callback/meta?code=x&state={q['state']}", follow_redirects=False)
        assert location(r) == "/conexiones" and not token.called


def test_callback_user_cancelled(client: TestClient, db: Session, brand: Brand) -> None:
    set_meta(db)
    _, q = start(client, "/conexiones/meta/iniciar")
    r = client.get(
        f"/conexiones/callback/meta?error=access_denied&error_reason=user_denied&state={q['state']}",
        follow_redirects=False,
    )
    assert location(r) == "/conexiones"
    assert any("Has cancelado la conexión con Meta" in m for m in flashes(client.get("/conexiones").text))


def test_callbacks_are_exempt_from_password(client: TestClient, db: Session, brand: Brand) -> None:
    from planificador import security

    security.set_password(db, "secreta-123")
    r = client.get("/conexiones/callback/google?state=x", follow_redirects=False)
    assert not location(r).startswith("/login")
    security.set_password(db, None)


# ------------------------------------------------------------------------------------------- TikTok


def mock_tiktok(
    router: respx.MockRouter,
    verifier_holder: dict[str, str],
    scope: str = "user.info.basic,video.publish,video.upload",
) -> dict[str, respx.Route]:
    def token(request: httpx.Request) -> httpx.Response:
        form = dict(parse_qs(request.content.decode()))
        form = {k: v[0] for k, v in form.items()}
        assert form["grant_type"] == "authorization_code" and form["code"] == "tt-code"
        assert form["redirect_uri"] == "http://localhost:8765/conexiones/callback/social1"
        assert form["client_key"] == "ttkey123456" and form["client_secret"] == "tt-secret-abc"
        verifier_holder["verifier"] = form["code_verifier"]
        return httpx.Response(
            200,
            json={
                "access_token": "act.tt",
                "expires_in": 86400,
                "refresh_token": "rft.tt",
                "refresh_expires_in": 31536000,
                "open_id": "open-123",
                "scope": scope,
                "token_type": "Bearer",
            },
        )

    user = {
        "data": {
            "user": {
                "open_id": "open-123",
                "union_id": "union-1",
                "display_name": "Lucía hornea",
                "username": "luciahornea",
                "avatar_url": "https://cdn.example.com/tt.png",
            }
        },
        "error": {"code": "ok", "message": "", "log_id": "1"},
    }
    creator = {
        "data": {
            "creator_nickname": "Lucía hornea",
            "creator_username": "luciahornea",
            "privacy_level_options": ["SELF_ONLY"],
            "comment_disabled": False,
            "duet_disabled": True,
            "stitch_disabled": False,
            "max_video_post_duration_sec": 600,
        },
        "error": {"code": "ok"},
    }
    return {
        "token": router.post(f"{TT_API}/v2/oauth/token/").mock(side_effect=token),
        "user": router.get(f"{TT_API}/v2/user/info/").mock(return_value=httpx.Response(200, json=user)),
        "creator": router.post(f"{TT_API}/v2/post/publish/creator_info/query/").mock(
            return_value=httpx.Response(200, json=creator)
        ),
        "avatar": router.get("https://cdn.example.com/tt.png").mock(
            return_value=httpx.Response(200, content=png_bytes())
        ),
    }


def test_tiktok_full_flow(client: TestClient, db: Session, brand: Brand, clock: Any) -> None:
    set_tiktok(db)
    url, q = start(client, "/conexiones/tiktok/iniciar")
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert q["client_key"] == "ttkey123456" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "http://localhost:8765/conexiones/callback/social1"
    row = db.get(OAuthState, q["state"])
    assert row is not None and row.code_verifier
    # TikTok PKCE: hex(sha256(verifier)) by default.
    assert q["code_challenge"] == hashlib.sha256(row.code_verifier.encode()).hexdigest()
    verifier = row.code_verifier

    seen: dict[str, str] = {}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        routes = mock_tiktok(router, seen)
        r = client.get(
            f"/conexiones/callback/social1?code=tt-code&scopes=x&state={q['state']}", follow_redirects=False
        )
        assert location(r) == "/conexiones"
        assert routes["token"].called and routes["user"].called and routes["creator"].called
    assert seen["verifier"] == verifier
    acc = account(db, brand.id, "tiktok")
    assert acc is not None and acc.status == AccountStatus.CONNECTED
    assert acc.external_id == "open-123" and acc.name == "Lucía hornea" and acc.username == "luciahornea"
    assert acc.access_token == "act.tt" and acc.refresh_token == "rft.tt"
    assert acc.token_expires_at == clock.now() + timedelta(seconds=86400)
    assert acc.refresh_expires_at == clock.now() + timedelta(seconds=31536000)
    assert acc.scopes == ["user.info.basic", "video.publish", "video.upload"]
    assert acc.meta["creator_info"]["privacy_level_options"] == ["SELF_ONLY"]
    assert acc.avatar_path
    html = client.get("/conexiones").text
    assert any("Conectado: TikTok (@luciahornea)" in m for m in flashes(html))
    assert "aún no está auditada" in html


def test_tiktok_base64url_pkce_setting(client: TestClient, db: Session, brand: Brand) -> None:
    import base64

    set_tiktok(db)
    config.set_setting(db, "tiktok_pkce_encoding", "base64url")
    _, q = start(client, "/conexiones/tiktok/iniciar")
    verifier = db.get(OAuthState, q["state"]).code_verifier
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected


def test_tiktok_start_requests_only_the_ticked_scopes(client: TestClient, db: Session, brand: Brand) -> None:
    set_tiktok(db)
    _, q = start(client, "/conexiones/tiktok/iniciar")
    assert q["scope"].split(",") == ["user.info.basic", "video.publish", "video.upload"]  # defaults
    # A sandbox app with user.info.basic, video.upload and video.list only (no Direct Post).
    config.set_setting(db, "tiktok_scopes", ["user.info.basic", "video.upload", "video.list"])
    _, q = start(client, "/conexiones/tiktok/iniciar")
    assert q["scope"] == "user.info.basic,video.upload,video.list"


def test_tiktok_card_lists_the_scopes_it_will_request(client: TestClient, db: Session, brand: Brand) -> None:
    set_tiktok(db)
    config.set_setting(db, "tiktok_scopes", ["user.info.basic", "video.upload"])
    html = client.get("/conexiones").text
    card = html[html.index('id="conn-tiktok"') :]
    card = card[: card.index("</article>")]
    assert "Permisos que se pedirán a TikTok" in card
    assert "<code>user.info.basic</code>, <code>video.upload</code>." in card
    assert "<code>video.publish</code>" not in card
    assert 'href="/ajustes#credenciales-tiktok"' in card and "error «scope»" in card


def test_tiktok_legacy_analytics_flag_still_requests_stats(
    client: TestClient, db: Session, brand: Brand
) -> None:
    set_tiktok(db)
    config.set_setting(db, "tiktok_analytics_scopes", True)  # installs from before tiktok_scopes
    _, q = start(client, "/conexiones/tiktok/iniciar")
    assert "user.info.stats" in q["scope"] and "video.publish" in q["scope"]


def test_tiktok_connect_inbox_only_app(client: TestClient, db: Session, brand: Brand) -> None:
    """A sandbox app without video.publish connects fine: no warning, no creator_info (Direct Post), the card
    says only the draft mode is available."""
    set_tiktok(db)
    config.set_setting(db, "tiktok_scopes", ["user.info.basic", "video.upload", "video.list"])
    _, q = start(client, "/conexiones/tiktok/iniciar")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        routes = mock_tiktok(router, {}, scope="user.info.basic,video.upload,video.list")
        r = client.get(
            f"/conexiones/callback/social1?code=tt-code&state={q['state']}", follow_redirects=False
        )
        assert location(r) == "/conexiones"
        assert routes["token"].called and not routes["creator"].called
    acc = account(db, brand.id, "tiktok")
    assert acc is not None and acc.status == AccountStatus.CONNECTED
    assert acc.scopes == ["user.info.basic", "video.upload", "video.list"]
    assert acc.meta["granted_scopes"] == acc.scopes and acc.meta.get("creator_info") is None
    html = client.get("/conexiones").text
    messages = flashes(html)
    assert any("Conectado: TikTok" in m for m in messages)
    assert not any("permiso" in m for m in messages)
    card = html[html.index('id="conn-tiktok"') :]
    card = card[: card.index("</article>")]
    assert "Modos de publicación disponibles" in card
    assert re.search(r'conn-tt__mode is-off">.*?Publicación directa', card, re.S)
    assert re.search(r'conn-tt__mode">.*?Borrador en la app', card, re.S)
    assert "video.publish" in card  # why direct posting is missing


def test_tiktok_card_modes(client: TestClient, db: Session, brand: Brand) -> None:
    set_tiktok(db)
    acc = _tiktok_account(db, brand)

    def card() -> str:
        html = client.get("/conexiones").text
        part = html[html.index('id="conn-tiktok"') :]
        return part[: part.index("</article>")]

    # Unknown grants (older connection): both offered, checked when publishing.
    assert "is-off" not in card() and "se comprobarán al publicar" in card()
    acc.scopes = ["user.info.basic", "video.publish", "video.upload"]
    db.commit()
    assert "is-off" not in card() and "se comprobarán" not in card()
    acc.scopes = ["user.info.basic", "video.publish"]
    db.commit()
    html = card()
    assert re.search(r'conn-tt__mode is-off">.*?Borrador en la app', html, re.S) and "video.upload" in html
    acc.scopes = ["user.info.basic"]
    db.commit()
    assert "ningún permiso de publicación" in card()


def test_tiktok_flow_with_monkeypatched_provider(
    client: TestClient, db: Session, brand: Brand, monkeypatch: pytest.MonkeyPatch
) -> None:
    from planificador.providers import tiktok

    set_tiktok(db)
    calls: dict[str, Any] = {}

    def fake_exchange(settings: Any, http: Any, code: str, redirect_uri: str, code_verifier: str, **kw: Any):
        calls["exchange"] = (code, redirect_uri, code_verifier)
        return tiktok.TikTokTokens(
            open_id="open-9",
            access_token="A",
            expires_at=datetime(2026, 11, 4, tzinfo=UTC),
            refresh_token="R",
            refresh_expires_at=None,
            scopes=["user.info.basic"],  # publishing scopes declined
        )

    def no_creator(*a: Any, **k: Any):
        raise ProviderError(ErrorKind.PERMANENT, "scope_not_authorized", "Falta permiso")

    monkeypatch.setattr(tiktok, "exchange_code", fake_exchange)
    monkeypatch.setattr(
        tiktok, "get_user_info", lambda s, h, t: {"open_id": "open-9", "display_name": "Otra"}
    )
    monkeypatch.setattr(tiktok, "query_creator_info", no_creator)
    _, q = start(client, "/conexiones/tiktok/iniciar")
    r = client.get(f"/conexiones/callback/social1?code=c9&state={q['state']}", follow_redirects=False)
    assert location(r) == "/conexiones"
    assert calls["exchange"][0] == "c9"
    acc = account(db, brand.id, "tiktok")
    assert acc is not None and acc.name == "Otra" and "creator_info" not in acc.meta
    html = client.get("/conexiones").text
    assert any("ningún permiso de publicación" in m for m in flashes(html))


def test_tiktok_exchange_error(client: TestClient, db: Session, brand: Brand) -> None:
    set_tiktok(db)
    _, q = start(client, "/conexiones/tiktok/iniciar")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(f"{TT_API}/v2/oauth/token/").mock(
            return_value=httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Code expired"}
            )
        )
        r = client.get(f"/conexiones/callback/social1?code=old&state={q['state']}", follow_redirects=False)
    assert location(r) == "/conexiones"
    assert account(db, brand.id, "tiktok") is None
    assert any("No se pudo conectar TikTok" in m for m in flashes(client.get("/conexiones").text))


# ------------------------------------------------------------------------------------------- YouTube

CHANNEL = {
    "id": "UC111",
    "snippet": {
        "title": "Canal Uno",
        "customUrl": "@canaluno",
        "thumbnails": {"default": {"url": "https://cdn.example.com/yt1.png"}},
    },
    "contentDetails": {"relatedPlaylists": {"uploads": "UU111"}},
    "statistics": {"subscriberCount": "1200", "videoCount": "33"},
    "status": {"longUploadsStatus": "allowed"},
}
CHANNEL_2 = {**CHANNEL, "id": "UC222", "snippet": {"title": "Canal Dos", "customUrl": "@canaldos"}}
YT_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
    "https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/yt-analytics.readonly"
)


def mock_google(
    router: respx.MockRouter, channels: list[dict], *, testing: bool = True
) -> dict[str, respx.Route]:
    def token(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["grant_type"] == "authorization_code" and form["code"] == "g-code"
        assert form["redirect_uri"] == "http://localhost:8765/conexiones/callback/google"
        assert form["client_secret"] == "GOCSPX-secret" and form["code_verifier"]
        body = {
            "access_token": "ya29.a",
            "expires_in": 3599,
            "refresh_token": "1//refresh",
            "scope": YT_SCOPES,
        }
        if testing:
            body["refresh_token_expires_in"] = 604799
        return httpx.Response(200, json=body)

    return {
        "token": router.post("https://oauth2.googleapis.com/token").mock(side_effect=token),
        "channels": router.get("https://www.googleapis.com/youtube/v3/channels").mock(
            return_value=httpx.Response(200, json={"items": channels})
        ),
        "avatar": router.get("https://cdn.example.com/yt1.png").mock(
            return_value=httpx.Response(200, content=png_bytes())
        ),
    }


def test_youtube_full_flow_single_channel(client: TestClient, db: Session, brand: Brand, clock: Any) -> None:
    set_google(db)
    url, q = start(client, "/conexiones/youtube/iniciar")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["access_type"] == "offline" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "http://localhost:8765/conexiones/callback/google"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        routes = mock_google(router, [CHANNEL])
        r = client.get(f"/conexiones/callback/google?code=g-code&state={q['state']}", follow_redirects=False)
        assert location(r) == "/conexiones"
        assert routes["channels"].called
    acc = account(db, brand.id, "youtube")
    assert (
        acc is not None
        and acc.external_id == "UC111"
        and acc.name == "Canal Uno"
        and acc.username == "canaluno"
    )
    assert acc.access_token == "ya29.a" and acc.refresh_token == "1//refresh"
    assert acc.refresh_expires_at == clock.now() + timedelta(seconds=604799)  # "Testing" project
    assert acc.meta["youtube_lock_status"] == "unknown" and acc.meta["youtube_publish_mode"] == "assisted"
    assert acc.meta["uploads_playlist_id"] == "UU111"
    html = client.get("/conexiones").text
    msgs = flashes(html)
    assert any("Conectado: YouTube (Canal Uno)" in m for m in msgs)
    assert any("7 días" in m for m in msgs)  # Testing warning
    assert "Prueba de bloqueo" in html and "modo «Prueba»" in html


def test_youtube_reconnect_keeps_lock_status(client: TestClient, db: Session, brand: Brand) -> None:
    set_google(db)
    old = Account(
        brand_id=brand.id,
        network="youtube",
        external_id="UC111",
        name="Canal Uno",
        status=AccountStatus.NEEDS_RECONNECT,
    )
    old.meta = {"youtube_lock_status": "unlocked", "youtube_publish_mode": "api"}
    db.add(old)
    db.commit()
    _, q = start(client, "/conexiones/youtube/iniciar")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        mock_google(router, [CHANNEL], testing=False)
        client.get(f"/conexiones/callback/google?code=g-code&state={q['state']}", follow_redirects=False)
    acc = account(db, brand.id, "youtube")
    assert acc.status == AccountStatus.CONNECTED and acc.refresh_expires_at is None
    assert acc.meta["youtube_lock_status"] == "unlocked" and acc.meta["youtube_publish_mode"] == "api"


def test_youtube_channel_chooser(client: TestClient, db: Session, brand: Brand) -> None:
    set_google(db)
    _, q = start(client, "/conexiones/youtube/iniciar")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        mock_google(router, [CHANNEL, CHANNEL_2])
        r = client.get(f"/conexiones/callback/google?code=g-code&state={q['state']}", follow_redirects=False)
        chooser = location(r)
        assert chooser.startswith("/conexiones/youtube/elegir?pendiente=")
        assert account(db, brand.id, "youtube") is None
        html = client.get(chooser).text
        assert "Canal Uno" in html and "Canal Dos" in html and "ya29.a" not in html
        r = client.post(
            "/conexiones/youtube/seleccionar",
            data={
                "csrf_token": conn_csrf(client),
                "pendiente": query_of(chooser)["pendiente"],
                "channel_id": "UC222",
            },
            follow_redirects=False,
        )
        assert location(r) == "/conexiones"
    acc = account(db, brand.id, "youtube")
    assert acc is not None and acc.external_id == "UC222" and acc.name == "Canal Dos"


def test_youtube_without_channel(client: TestClient, db: Session, brand: Brand) -> None:
    set_google(db)
    _, q = start(client, "/conexiones/youtube/iniciar")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        mock_google(router, [])
        client.get(f"/conexiones/callback/google?code=g-code&state={q['state']}", follow_redirects=False)
    assert account(db, brand.id, "youtube") is None
    assert any("no tiene canal de YouTube" in m for m in flashes(client.get("/conexiones").text))


def test_pending_choice_bound_to_session(app: Any, client: TestClient, db: Session, brand: Brand) -> None:
    set_google(db)
    _, q = start(client, "/conexiones/youtube/iniciar")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        mock_google(router, [CHANNEL, CHANNEL_2])
        chooser = location(
            client.get(f"/conexiones/callback/google?code=g-code&state={q['state']}", follow_redirects=False)
        )
    with TestClient(app, base_url=BASE_URL) as other:
        assert location(other.get(chooser, follow_redirects=False)) == "/conexiones"


# --------------------------------------------------------------------------------------- demo / disconnect


def test_demo_connect(csrf_client: Any, db: Session, brand: Brand, demo_mode: bool) -> None:
    r = csrf_client.post("/conexiones/demo/conectar", follow_redirects=False)
    assert location(r) == "/conexiones"
    acc = account(db, brand.id, "demo")
    assert acc is not None and acc.status == AccountStatus.CONNECTED and acc.access_token
    html = csrf_client.get("/conexiones").text
    assert "Cuenta Demo conectada" in " ".join(flashes(html))
    # htmx variant answers with HX-Redirect.
    r = csrf_client.post("/conexiones/demo/conectar", headers={"HX-Request": "true"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/conexiones"
    assert len(db.scalars(select(Account).where(Account.network == "demo")).all()) == 1


def test_demo_connect_requires_demo_mode(
    csrf_client: Any, db: Session, brand: Brand, demo_mode_off: bool
) -> None:
    csrf_client.post("/conexiones/demo/conectar", follow_redirects=False)
    assert account(db, brand.id, "demo") is None


def test_post_requires_csrf(client: TestClient, brand: Brand, demo_mode: bool) -> None:
    assert client.post("/conexiones/demo/conectar").status_code == 403


def _tiktok_account(db: Session, brand: Brand) -> Account:
    acc = Account(
        brand_id=brand.id,
        network="tiktok",
        external_id="open-1",
        name="TT",
        username="tt",
        status=AccountStatus.CONNECTED,
    )
    acc.set_tokens("tt-access", "tt-refresh", refresh_expires_at=datetime(2027, 1, 1, tzinfo=UTC))
    db.add(acc)
    db.commit()
    return acc


def test_disconnect_tiktok_revokes_and_wipes(csrf_client: Any, db: Session, brand: Brand) -> None:
    set_tiktok(db)
    acc = _tiktok_account(db, brand)
    post = Post(brand_id=brand.id, text="x", scheduled_at=datetime(2026, 12, 1, tzinfo=UTC))
    db.add(post)
    db.flush()
    db.add(
        PostTarget(
            post_id=post.id,
            account_id=acc.id,
            network="tiktok",
            format="video",
            status=TargetStatus.SCHEDULED,
        )
    )
    db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        revoke = router.post(f"{TT_API}/v2/oauth/revoke/").mock(return_value=httpx.Response(200, json={}))
        r = csrf_client.post(f"/conexiones/{acc.id}/desconectar", follow_redirects=False)
        assert location(r) == "/conexiones"
        assert revoke.called
        assert "token=tt-access" in revoke.calls.last.request.content.decode()
    db.expire_all()
    acc = db.get(Account, acc.id)
    assert acc.status == AccountStatus.DISCONNECTED
    assert acc.access_token_enc is None and acc.refresh_token_enc is None and acc.refresh_expires_at is None
    html = csrf_client.get("/conexiones").text
    assert any("1 publicación pendiente" in m for m in flashes(html))
    assert f'action="/conexiones/{acc.id}/desconectar"' not in html


def test_disconnect_youtube_revoke_failure_still_disconnects(
    csrf_client: Any, db: Session, brand: Brand
) -> None:
    set_google(db)
    acc = Account(
        brand_id=brand.id, network="youtube", external_id="UC1", name="C", status=AccountStatus.CONNECTED
    )
    acc.set_tokens("a", "r")
    db.add(acc)
    db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        revoke = router.post("https://oauth2.googleapis.com/revoke").mock(return_value=httpx.Response(500))
        csrf_client.post(f"/conexiones/{acc.id}/desconectar", follow_redirects=False)
        assert revoke.called and "token=r" in revoke.calls.last.request.content.decode()
    db.expire_all()
    assert db.get(Account, acc.id).status == AccountStatus.DISCONNECTED


def test_disconnect_meta_without_network(csrf_client: Any, db: Session, brand: Brand) -> None:
    acc = Account(
        brand_id=brand.id, network="facebook", external_id="p", name="P", status=AccountStatus.CONNECTED
    )
    acc.set_tokens("t")
    acc.meta = {"user_token_enc": crypto.encrypt("u"), "page_id": "p"}
    db.add(acc)
    db.commit()
    with respx.mock(assert_all_mocked=True, assert_all_called=False):
        csrf_client.post(f"/conexiones/{acc.id}/desconectar", follow_redirects=False)
    db.expire_all()
    acc = db.get(Account, acc.id)
    assert acc.status == AccountStatus.DISCONNECTED and "user_token_enc" not in acc.meta
    assert csrf_client.post("/conexiones/9999/desconectar").status_code == 404


def test_check_account(
    csrf_client: Any, db: Session, brand: Brand, demo_account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    from planificador.providers import registry
    from planificador.providers.demo import DemoProvider

    r = csrf_client.post(f"/conexiones/{demo_account.id}/comprobar", follow_redirects=False)
    assert location(r) == "/conexiones"
    assert any("funciona correctamente" in m for m in flashes(csrf_client.get("/conexiones").text))

    class Broken(DemoProvider):
        def check_account(self, account: Any, settings: Any, http: Any) -> None:
            raise ProviderError(ErrorKind.AUTH, "expired", "La sesión ha caducado.")

    registry.register(Broken())
    csrf_client.post(f"/conexiones/{demo_account.id}/comprobar", follow_redirects=False)
    db.expire_all()
    acc = db.get(Account, demo_account.id)
    assert acc.status == AccountStatus.NEEDS_RECONNECT and acc.last_error == "La sesión ha caducado."


# --------------------------------------------------------------------------------------- YouTube lock test


def _yt_account(db: Session, brand: Brand) -> Account:
    acc = Account(
        brand_id=brand.id, network="youtube", external_id="UC1", name="Canal", status=AccountStatus.CONNECTED
    )
    acc.set_tokens("old-access", "refresh-1", token_expires_at=datetime(2026, 11, 3, 9, 30, tzinfo=UTC))
    acc.meta = {"youtube_lock_status": "unknown", "youtube_publish_mode": "assisted"}
    db.add(acc)
    db.commit()
    return acc


def test_lock_test_runs_in_background_and_stores_result(
    csrf_client: Any, db: Session, brand: Brand, inline_background: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from planificador.providers import youtube

    set_google(db)
    acc = _yt_account(db, brand)

    def fake_lock_test(creds: Any, settings: Any, http: Any, clock: Any, **kw: Any):
        creds.access_token = "refreshed-access"  # the provider refreshed the token while testing
        creds.token_expires_at = datetime(2026, 11, 3, 10, 30, tzinfo=UTC)
        return youtube.LockTestResult(
            "unlocked", "YouTube respetó la visibilidad.", video_id="v1", deleted=True
        )

    monkeypatch.setattr(youtube, "lock_test", fake_lock_test)
    r = csrf_client.post(f"/conexiones/{acc.id}/youtube/prueba-bloqueo", headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="conn-youtube"' in r.text
    assert inline_background == [f"yt-lock-test-{acc.id}"]
    db.expire_all()
    acc = db.get(Account, acc.id)
    assert acc.meta["youtube_lock_status"] == "unlocked" and acc.meta["youtube_publish_mode"] == "api"
    assert acc.meta["youtube_lock_test"]["state"] == "done"
    assert acc.meta["youtube_lock_test"]["result"] == "unlocked"
    assert acc.access_token == "refreshed-access" and acc.refresh_token == "refresh-1"
    assert "YouTube respetó la visibilidad." in r.text and "Subidas automáticas permitidas" in r.text


def test_lock_test_keeps_the_clip_id_for_the_24h_recheck(
    csrf_client: Any, db: Session, brand: Brand, inline_background: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``unlocked`` clip kept for the re-check is stored by id, so the follow-up reads that video instead
    of looking for it by title among the latest uploads."""
    from planificador.providers import youtube

    set_google(db)
    acc = _yt_account(db, brand)
    monkeypatch.setattr(
        youtube,
        "lock_test",
        lambda *a, **k: youtube.LockTestResult("unlocked", "Sin bloqueo.", video_id="v9", deleted=False),
    )
    csrf_client.post(f"/conexiones/{acc.id}/youtube/prueba-bloqueo", headers={"HX-Request": "true"})
    db.expire_all()
    assert db.get(Account, acc.id).meta["youtube_lock_test"]["video_id"] == "v9"


def test_lock_test_locked_and_error(
    csrf_client: Any, db: Session, brand: Brand, inline_background: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from planificador.providers import youtube

    set_google(db)
    acc = _yt_account(db, brand)
    monkeypatch.setattr(youtube, "lock_test", lambda *a, **k: youtube.LockTestResult("locked", "Privado."))
    csrf_client.post(f"/conexiones/{acc.id}/youtube/prueba-bloqueo", follow_redirects=False)
    db.expire_all()
    assert db.get(Account, acc.id).meta["youtube_lock_status"] == "locked"

    def boom(*a: Any, **k: Any):
        raise ProviderError(ErrorKind.TRANSIENT, "quota", "Cuota agotada.")

    monkeypatch.setattr(youtube, "lock_test", boom)
    csrf_client.post(f"/conexiones/{acc.id}/youtube/prueba-bloqueo", follow_redirects=False)
    db.expire_all()
    meta = db.get(Account, acc.id).meta
    assert meta["youtube_lock_status"] == "locked"  # an error never changes the stored status
    assert (
        meta["youtube_lock_test"]["result"] == "error"
        and "Cuota agotada." in meta["youtube_lock_test"]["message"]
    )


def test_lock_test_running_card_polls(
    csrf_client: Any, db: Session, brand: Brand, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_google(db)
    acc = _yt_account(db, brand)
    started: list[str] = []
    monkeypatch.setattr(conn, "_start_background", lambda fn, name: started.append(name))
    r = csrf_client.post(f"/conexiones/{acc.id}/youtube/prueba-bloqueo", headers={"HX-Request": "true"})
    assert 'hx-trigger="every 3s"' in r.text and "Probando" in r.text
    r = csrf_client.post(f"/conexiones/{acc.id}/youtube/prueba-bloqueo", headers={"HX-Request": "true"})
    assert started == [f"yt-lock-test-{acc.id}"]  # never two tests at once
    # After a restart the running marker is stale: the card says it was interrupted (no polling).
    csrf_client.client.app.state.yt_lock_tests.clear()
    html = csrf_client.get("/conexiones/tarjeta/youtube").text
    assert "se interrumpió" in html and "every 3s" not in html


def test_lock_test_only_for_youtube(csrf_client: Any, demo_account: Account) -> None:
    assert csrf_client.post(f"/conexiones/{demo_account.id}/youtube/prueba-bloqueo").status_code == 400


def test_youtube_mode(csrf_client: Any, db: Session, brand: Brand) -> None:
    acc = _yt_account(db, brand)
    r = csrf_client.post(
        f"/conexiones/{acc.id}/youtube/modo",
        data={"modo": "api", "auditado": "1"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "toast" in r.headers.get("HX-Trigger", "")
    db.expire_all()
    meta = db.get(Account, acc.id).meta
    assert meta["youtube_publish_mode"] == "api" and meta["youtube_project_audited"] is True
    csrf_client.post(f"/conexiones/{acc.id}/youtube/modo", data={"modo": "assisted"}, follow_redirects=False)
    db.expire_all()
    meta = db.get(Account, acc.id).meta
    assert meta["youtube_publish_mode"] == "assisted" and meta["youtube_project_audited"] is False


# --------------------------------------------------------------------------------------- guides + markdown


def test_guide_missing_file(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(conn, "GUIDE_DIRS", (tmp_path,))
    r = client.get("/ayuda/meta")
    assert r.status_code == 200 and "todavía no está disponible" in r.text
    assert "http://localhost:8765/conexiones/callback/meta" in r.text
    assert client.get("/ayuda/otra").status_code == 404
    assert client.get("/ayuda/..%2Fsecret").status_code == 404
    assert client.get("/ayuda/META").status_code == 404


def test_guide_renders_markdown_safely(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "guia-tiktok.md").write_text(
        "# Guía de TikTok\n\nPaso **uno** con `código` y <script>alert(1)</script>.\n\n"
        "## Crear la app\n\n1. Entra en [developers](https://developers.tiktok.com).\n"
        "2. Lee [la otra guía](guia-meta.md#permisos).\n   - sub punto\n3. [malo](javascript:alert(1))\n\n"
        "| Campo | Valor |\n|---|---|\n| Redirect | `http://localhost` |\n\n> Nota importante\n\n"
        "```\n<b>no html</b>\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(conn, "GUIDE_DIRS", (tmp_path,))
    html = client.get("/ayuda/tiktok").text
    assert "<title>Guía de TikTok · Planificador</title>" in html
    assert "<strong>uno</strong>" in html and "<code>código</code>" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert (
        '<a href="https://developers.tiktok.com" target="_blank" rel="noopener noreferrer">developers</a>'
        in html
    )
    assert 'href="/ayuda/meta#permisos"' in html
    assert "javascript:" not in html
    assert '<h2 id="crear-la-app">' in html and 'href="#crear-la-app"' in html  # table of contents
    assert "<table" in html and "<blockquote>" in html and "&lt;b&gt;no html&lt;/b&gt;" in html


def test_tiktok_guide_follows_the_sandbox_flow(client: TestClient) -> None:
    """The shipped docs/guia-tiktok.md: Sandbox first, Desktop URL verification, scopes, error screens."""
    html = client.get("/ayuda/tiktok").text
    for needle in (
        "Production | Sandbox",
        "Create Sandbox",
        "Apply changes",
        "App review",
        "URL prefix",
        "public_html",
        ".github.io",
        "Login Kit",
        "Direct Post",
        "Target users",
        "error_type=client_key",
        "unauthorized_client",
        "error_type=scope",
        "redirect_uri",
        "code_challenge",
        "Permisos (scopes) de tu app",
        "http://localhost:8765/conexiones/callback/social1",
    ):
        assert needle in html, needle
    assert "Pedir permisos de estadísticas" not in html
    # The composer's "Ver la guía" link points at an existing section.
    composer_tpl = (conn._PACKAGE_DIR / "templates" / "composer" / "_net_tiktok.html").read_text(
        encoding="utf-8"
    )
    anchor = re.search(r'href="/ayuda/tiktok#([\w-]+)"', composer_tpl).group(1)
    assert f'id="{anchor}"' in html


def test_markdown_lists_and_paragraphs() -> None:
    out = str(
        conn.render_markdown(
            "Texto\nseguido.\n\n- a\n- b\n  - b1\n  - b2\n- c\n\n3. tres\n4. cuatro\n\n---\n"
        )
    )
    assert out.startswith("<p>Texto seguido.</p>")
    assert "<ul><li>a</li><li>b<ul><li>b1</li><li>b2</li></ul></li><li>c</li></ul>" in out
    assert '<ol start="3"><li>tres</li><li>cuatro</li></ol>' in out
    assert out.endswith("<hr>")


def test_markdown_does_not_trust_html() -> None:
    out = str(
        conn.render_markdown("[x](https://ok.example/?a=1&b=2) ![img](a.png) <img src=x onerror=alert(1)>")
    )
    assert 'href="https://ok.example/?a=1&amp;b=2"' in out
    assert "<img" not in out and "&lt;img" in out and "[img]" in out


# ------------------------------------------------------------------------------------------- helpers (unit)


def test_upsert_account_resets_avatar_for_other_external_account(db: Session, brand: Brand) -> None:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    acc = conn.upsert_account(
        db, brand.id, "tiktok", "a", now, name="A", access_token="t1", meta={"x": 1, "keep": 2}
    )
    acc.avatar_path = "account-1.jpg"
    db.commit()
    same = conn.upsert_account(
        db, brand.id, "tiktok", "a", now, name="A", access_token="t2", keep_meta=("keep",)
    )
    assert same.id == acc.id and same.avatar_path == "account-1.jpg" and same.meta == {"keep": 2}
    other = conn.upsert_account(
        db, brand.id, "tiktok", "b", now, name="B", access_token="t3", keep_meta=("keep",)
    )
    assert other.id == acc.id and other.avatar_path is None and other.meta == {}
    assert other.access_token == "t3"


def test_cache_avatar_rejects_non_https_and_garbage(db: Session, brand: Brand, tmp_path: Path) -> None:
    acc = conn.upsert_account(db, brand.id, "tiktok", "a", datetime(2026, 11, 3, tzinfo=UTC), name="A")
    db.commit()
    with httpx.Client() as http, respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        assert not conn.cache_avatar(http, "http://insecure.example/a.png", acc, tmp_path)
        router.get("https://cdn.example.com/bad.png").mock(
            return_value=httpx.Response(200, content=b"not an image")
        )
        assert not conn.cache_avatar(http, "https://cdn.example.com/bad.png", acc, tmp_path)
        router.get("https://cdn.example.com/404.png").mock(return_value=httpx.Response(404))
        assert not conn.cache_avatar(http, "https://cdn.example.com/404.png", acc, tmp_path)
    assert acc.avatar_path is None

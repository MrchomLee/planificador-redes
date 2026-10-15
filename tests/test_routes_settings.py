"""Ajustes: credentials (write-only, encrypted at rest), publishing, public media, password, notifications,
about/logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config, crypto, security
from planificador.models import Account, AccountStatus, Brand, Notification, Setting
from planificador.routes import settings as settings_mod

FLASH_RE = '<script type="application/json" id="flash-data">'
HX = {"HX-Request": "true"}


def flashes(html: str) -> list[str]:
    start = html.index(FLASH_RE) + len(FLASH_RE)
    return [f["message"] for f in json.loads(html[start : html.index("</script>", start)])]


def toast(response: Any) -> dict:
    return json.loads(response.headers["HX-Trigger"])["toast"]


def raw_setting(db: Session, key: str) -> Any:
    db.expire_all()
    return db.scalar(select(Setting.value).where(Setting.key == key))


class FakePublicMedia:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.state = "stopped"
        self.test_result: dict[str, Any] = {
            "ok": True,
            "message": "La URL pública funciona.",
            "base": "https://abc.trycloudflare.com",
        }

    def status(self) -> dict[str, Any]:
        return {
            "mode": "tunnel",
            "mode_label": "Túnel temporal de Cloudflare",
            "enabled": True,
            "server": {"running": False, "port": 8766, "error": None},
            "tunnel": {
                "state": self.state,
                "url": None,
                "version": "2026.10.1",
                "asset": "cloudflared-linux-amd64",
                "binary_present": False,
                "error": None,
            },
            "base_url": None,
            "base_url_valid": False,
            "public_base": None,
            "active_tokens": 2,
            "idle_shutdown_s": 300,
            "last_error": None,
        }

    def prepare(self) -> dict:
        self.calls.append("prepare")
        return self.status()

    def stop(self) -> None:
        self.calls.append("stop")

    def test(self) -> dict:
        self.calls.append("test")
        return self.test_result


@pytest.fixture
def fake_pm(monkeypatch: pytest.MonkeyPatch) -> FakePublicMedia:
    fake = FakePublicMedia()
    monkeypatch.setattr(settings_mod, "_public_media_service", lambda request: fake)
    monkeypatch.setattr(settings_mod, "_start_background", lambda fn, name: fn())
    return fake


# ---------------------------------------------------------------------------------------------- page


def test_settings_page_sections(client: TestClient, db: Session, brand: Brand) -> None:
    config.set_setting(db, "meta_app_secret", "super-secret-value", secret=True)
    html = client.get("/ajustes").text
    for anchor in ("credenciales", "publicacion", "url-publica", "seguridad", "avisos", "acerca"):
        assert f'id="{anchor}"' in html and f'href="#{anchor}"' in html
    for anchor in ("credenciales-meta", "credenciales-tiktok", "credenciales-google"):
        assert f'id="{anchor}"' in html
    assert "super-secret-value" not in html
    assert "•••• guardado" in html and "Borrar el secreto guardado" in html
    assert "http://localhost:8765/conexiones/callback/meta" in html
    assert "http://localhost:8765/conexiones/callback/social1" in html
    assert "http://localhost:8765/conexiones/callback/google" in html
    assert "Túnel temporal de Cloudflare" in html and "URL pública propia" in html
    assert "Versión 0.1.0" in html and "planificador.sqlite3" in html
    assert '<option value="Europe/Madrid" selected>' in html
    assert "Sin contraseña" in html


# ---------------------------------------------------------------------------------------- credentials


def test_save_meta_credentials_encrypts_secret(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/ajustes/credenciales/meta",
        data={
            "client_id": " 1234567890 ",
            "client_secret": "abc123secretXYZ",
            "api_version": "v26.0",
            "config_id": "",
        },
        headers=HX,
    )
    assert r.status_code == 200 and 'id="cred-meta"' in r.text and "<html" not in r.text
    assert toast(r)["kind"] == "success"
    assert "abc123secretXYZ" not in r.text and "Configurado" in r.text
    assert config.get_setting(db, "meta_app_id") == "1234567890"
    assert config.get_setting(db, "meta_api_version") == "v26.0"
    stored = raw_setting(db, "meta_app_secret")
    assert (
        isinstance(stored, dict)
        and stored["enc"].startswith("v1:")
        and "abc123secretXYZ" not in json.dumps(stored)
    )
    assert crypto.decrypt(stored["enc"]) == "abc123secretXYZ"
    assert config.provider_settings(db).meta_app_secret == "abc123secretXYZ"

    # Empty secret keeps the stored one; "Borrar" removes it.
    csrf_client.post(
        "/ajustes/credenciales/meta",
        data={"client_id": "1234567890", "client_secret": "", "api_version": "v26.0"},
        headers=HX,
    )
    assert config.get_secret(db, "meta_app_secret") == "abc123secretXYZ"
    csrf_client.post(
        "/ajustes/credenciales/meta",
        data={"client_id": "1234567890", "clear_secret": "1", "api_version": ""},
        headers=HX,
    )
    db.expire_all()
    assert config.get_secret(db, "meta_app_secret") is None
    assert config.get_setting(db, "meta_api_version") == "v25.0"  # empty → default


def test_save_credentials_without_htmx_redirects(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/ajustes/credenciales/google",
        data={"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-zzz"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/ajustes#credenciales-google"
    assert config.get_secret(db, "google_client_secret") == "GOCSPX-zzz"
    assert any("Google (YouTube)" in m for m in flashes(csrf_client.get("/ajustes").text))


def test_save_tiktok_advanced(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/ajustes/credenciales/tiktok",
        data={
            "client_id": "awkey123",
            "client_secret": "s3cr3t-tt",
            "pkce_encoding": "base64url",
            "analytics_scopes": "1",
        },
        headers=HX,
    )
    assert r.status_code == 200
    ps = config.provider_settings(db)
    assert ps.tiktok_client_key == "awkey123" and ps.tiktok_client_secret == "s3cr3t-tt"
    assert ps.tiktok_pkce_encoding == "base64url"
    assert config.get_bool(db, "tiktok_analytics_scopes") is True
    csrf_client.post(
        "/ajustes/credenciales/tiktok", data={"client_id": "awkey123", "pkce_encoding": "hex"}, headers=HX
    )
    db.expire_all()
    assert config.get_bool(db, "tiktok_analytics_scopes") is False


@pytest.mark.parametrize(
    ("group", "data", "message"),
    [
        ("meta", {"client_id": "abc"}, "es un número"),
        ("meta", {"client_id": "123456", "api_version": "25"}, "v25.0"),
        ("meta", {"client_id": "123456", "config_id": "xx"}, "ID de configuración"),
        ("meta", {"client_id": "123456", "client_secret": "has spaces in it"}, "secreto no es válido"),
        ("tiktok", {"client_id": "with space", "pkce_encoding": "hex"}, "Client Key"),
        ("tiktok", {"client_id": "abcdef12", "pkce_encoding": "rot13"}, "hex o base64url"),
        ("google", {"client_id": "12345.example.com"}, "apps.googleusercontent.com"),
    ],
)
def test_credentials_validation(
    csrf_client: Any, db: Session, brand: Brand, group: str, data: dict, message: str
) -> None:
    r = csrf_client.post(f"/ajustes/credenciales/{group}", data=data, headers=HX)
    assert r.status_code == 422 and message in r.text and f'id="cred-{group}"' in r.text
    assert toast(r)["kind"] == "error"
    db.expire_all()
    assert config.get_setting(db, settings_mod.ID_FIELDS[group]) is None
    # Without htmx: full page with the error.
    r = csrf_client.post(f"/ajustes/credenciales/{group}", data=data)
    assert r.status_code == 422 and message in r.text and "<html" in r.text


def test_unknown_credentials_group(csrf_client: Any) -> None:
    assert csrf_client.post("/ajustes/credenciales/myspace", data={}).status_code == 404


def test_changing_app_warns_when_accounts_connected(csrf_client: Any, db: Session, brand: Brand) -> None:
    config.set_setting(db, "meta_app_id", "111111")
    acc = Account(
        brand_id=brand.id, network="facebook", external_id="p", name="P", status=AccountStatus.CONNECTED
    )
    db.add(acc)
    db.commit()
    r = csrf_client.post("/ajustes/credenciales/meta", data={"client_id": "222222"}, headers=HX)
    assert toast(r)["kind"] == "warning" and "reconectarse" in toast(r)["message"]


def test_settings_require_csrf(client: TestClient) -> None:
    assert client.post("/ajustes/credenciales/meta", data={"client_id": "1"}).status_code == 403


# ---------------------------------------------------------------------------------------- publishing


def test_save_publishing(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/ajustes/publicacion",
        data={"default_timezone": "America/Mexico_City", "grace_minutes": "60"},
        headers=HX,
    )
    assert r.status_code == 200 and 'id="publicacion"' in r.text
    assert config.get_setting(db, "default_timezone") == "America/Mexico_City"
    assert config.get_int(db, "grace_minutes") == 60
    assert config.get_bool(db, "demo_mode") is False


def test_toggle_demo_mode_reloads(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/ajustes/publicacion",
        data={"default_timezone": "Europe/Madrid", "grace_minutes": "120", "demo_mode": "1"},
        headers=HX,
    )
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/ajustes#publicacion"
    assert config.is_demo_mode(db)
    html = csrf_client.get("/ajustes").text
    assert "Modo prueba activo" in html and any("Modo prueba activado" in m for m in flashes(html))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"default_timezone": "Moon/Base", "grace_minutes": "120"}, "zona horaria"),
        ({"default_timezone": "Europe/Madrid", "grace_minutes": "-5"}, "entre 0 y 1440"),
        ({"default_timezone": "Europe/Madrid", "grace_minutes": "muchos"}, "entre 0 y 1440"),
        ({"default_timezone": "Europe/Madrid", "grace_minutes": "5000"}, "entre 0 y 1440"),
    ],
)
def test_publishing_validation(csrf_client: Any, db: Session, brand: Brand, data: dict, message: str) -> None:
    r = csrf_client.post("/ajustes/publicacion", data=data, headers=HX)
    assert r.status_code == 422 and message in r.text
    assert config.get_int(db, "grace_minutes") == 120


# ---------------------------------------------------------------------------------------- public media


def test_tunnel_requires_consent(
    csrf_client: Any, db: Session, brand: Brand, fake_pm: FakePublicMedia
) -> None:
    r = csrf_client.post("/ajustes/url-publica", data={"mode": "tunnel"}, headers=HX)
    assert r.status_code == 422 and "Marca la casilla" in r.text
    assert config.get_setting(db, "public_media_mode") == "off"
    assert fake_pm.calls == []
    r = csrf_client.post("/ajustes/url-publica", data={"mode": "tunnel", "consent": "1"}, headers=HX)
    assert r.status_code == 200 and "cloudflared" in toast(r)["message"]
    assert config.get_setting(db, "public_media_mode") == "tunnel"
    assert config.get_setting(db, "public_media_tunnel_consent_at")
    assert fake_pm.calls == ["prepare"]  # binary download only after consent
    assert "Aceptado el" in r.text and "Enlaces activos" in r.text
    # Saving again in tunnel mode does not ask for consent again nor re-download.
    r = csrf_client.post("/ajustes/url-publica", data={"mode": "tunnel"}, headers=HX)
    assert r.status_code == 200 and fake_pm.calls == ["prepare"]
    # Switching off stops the service.
    csrf_client.post("/ajustes/url-publica", data={"mode": "off"}, headers=HX)
    assert config.get_setting(db, "public_media_mode") == "off" and fake_pm.calls[-1] == "stop"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://fotos.midominio.com/", True),
        ("http://fotos.midominio.com", False),
        ("https://localhost:8766", False),
        ("https://fotos.midominio.com/?x=1", False),
        ("", False),
    ],
)
def test_base_url_mode(
    csrf_client: Any, db: Session, brand: Brand, fake_pm: FakePublicMedia, url: str, ok: bool
) -> None:
    r = csrf_client.post("/ajustes/url-publica", data={"mode": "base_url", "base_url": url}, headers=HX)
    if ok:
        assert r.status_code == 200
        assert config.get_setting(db, "public_media_mode") == "base_url"
        assert config.get_setting(db, "public_media_base_url") == "https://fotos.midominio.com"
    else:
        assert r.status_code == 422
        assert config.get_setting(db, "public_media_mode") == "off"


def test_public_media_status_and_test(
    csrf_client: Any, db: Session, brand: Brand, fake_pm: FakePublicMedia
) -> None:
    r = csrf_client.post("/ajustes/url-publica/probar", headers=HX)
    assert toast(r)["kind"] == "warning" and fake_pm.calls == []  # mode off
    config.set_setting(db, "public_media_mode", "tunnel")
    r = csrf_client.post("/ajustes/url-publica/probar", headers=HX)
    assert r.status_code == 200 and fake_pm.calls == ["test"]
    assert toast(r)["kind"] == "success" and "abc.trycloudflare.com" in r.text
    fake_pm.test_result = {"ok": False, "message": "La URL pública no responde."}
    r = csrf_client.post("/ajustes/url-publica/probar", headers=HX)
    assert toast(r)["kind"] == "error" and "no responde" in r.text
    # Status fragment polls while the tunnel is starting.
    fake_pm.state = "downloading"
    r = csrf_client.get("/ajustes/url-publica/estado")
    assert r.status_code == 200 and 'id="pm-status"' in r.text and 'hx-trigger="every 3s"' in r.text
    fake_pm.state = "stopped"
    assert 'hx-trigger="every 3s"' not in csrf_client.get("/ajustes/url-publica/estado").text


def test_page_renders_with_real_public_media_service(client: TestClient, db: Session, brand: Brand) -> None:
    config.set_setting(db, "public_media_mode", "base_url")
    config.set_setting(db, "public_media_base_url", "https://media.example.org")
    html = client.get("/ajustes").text
    assert "https://media.example.org" in html and "Servidor de medios" in html


# ------------------------------------------------------------------------------------------- security


def test_password_set_change_remove(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/ajustes/seguridad",
        data={"new_password": "clave-larga", "confirm_password": "clave-larga"},
        headers=HX,
    )
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/ajustes#seguridad"
    assert security.verify_password("clave-larga", security.get_password_hash(db))
    # This browser stays logged in (other sessions are logged out).
    page = csrf_client.get("/ajustes", follow_redirects=False)
    assert page.status_code == 200 and "Contraseña activada" in page.text
    csrf_client.refresh()  # the CSRF token was rotated

    # Changing needs the current password.
    r = csrf_client.post(
        "/ajustes/seguridad",
        data={"current_password": "mala", "new_password": "otra-clave", "confirm_password": "otra-clave"},
        headers=HX,
    )
    assert r.status_code == 422 and "no es correcta" in r.text
    r = csrf_client.post(
        "/ajustes/seguridad",
        data={
            "current_password": "clave-larga",
            "new_password": "otra-clave",
            "confirm_password": "otra-clave",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert security.verify_password("otra-clave", security.get_password_hash(db))
    csrf_client.refresh()

    # Removing it.
    r = csrf_client.post(
        "/ajustes/seguridad", data={"action": "remove", "current_password": "nope"}, headers=HX
    )
    assert r.status_code == 422 and security.password_enabled(db)
    r = csrf_client.post(
        "/ajustes/seguridad", data={"action": "remove", "current_password": "otra-clave"}, headers=HX
    )
    assert r.status_code == 200 and "eliminada" in toast(r)["message"]
    assert not security.password_enabled(db)


def test_other_sessions_are_logged_out(app: Any, csrf_client: Any, db: Session, brand: Brand) -> None:
    with TestClient(app, base_url="http://localhost:8765") as other:
        assert other.get("/ajustes", follow_redirects=False).status_code == 200
        csrf_client.post(
            "/ajustes/seguridad",
            data={"new_password": "secreta1", "confirm_password": "secreta1"},
            headers=HX,
        )
        assert other.get("/ajustes", follow_redirects=False).status_code == 303
    security.set_password(db, None)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"new_password": "123", "confirm_password": "123"}, "al menos 6"),
        ({"new_password": "123456", "confirm_password": "654321"}, "no coinciden"),
    ],
)
def test_password_validation(csrf_client: Any, db: Session, brand: Brand, data: dict, message: str) -> None:
    r = csrf_client.post("/ajustes/seguridad", data=data, headers=HX)
    assert r.status_code == 422 and message in r.text
    assert not security.password_enabled(db)


# ------------------------------------------------------------------------------------------- avisos


def test_notifications_settings_and_test(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post("/ajustes/avisos", data={"browser": "1"}, headers=HX)
    assert r.status_code == 200 and 'id="avisos"' in r.text
    assert config.get_bool(db, "notifications_desktop") is False
    assert config.get_bool(db, "notifications_browser") is True
    r = csrf_client.post("/ajustes/avisos/probar", headers=HX)
    assert r.status_code == 204
    trigger = json.loads(r.headers["HX-Trigger"])
    assert "notifications:refresh" in trigger and trigger["toast"]["kind"] == "success"
    db.expire_all()
    note = db.scalar(select(Notification))
    assert note is not None and note.kind == "info" and note.title == "Aviso de prueba"


# ---------------------------------------------------------------------------------------------- about


def test_logs_view_is_scrubbed(client: TestClient, data_dir: Path, brand: Brand) -> None:
    log_file = data_dir / "logs" / "app.log"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write("2026-11-03 10:00:00,000 ERROR   x: fallo con access_token=EAAB-secret-token\n")
    r = client.get("/ajustes/registros", headers=HX)
    assert r.status_code == 200 and 'class="modal' in r.text
    assert "EAAB-secret-token" not in r.text and "access_token=***" in r.text and "log-line--error" in r.text
    page = client.get("/ajustes/registros")
    assert page.status_code == 200 and "<html" in page.text


def test_read_log_tail(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    assert settings_mod.read_log_tail(path) == []
    path.write_text("\n".join(f"line {i}" for i in range(1000)), encoding="utf-8")
    lines = settings_mod.read_log_tail(path, max_lines=5)
    assert lines == [f"line {i}" for i in range(995, 1000)]


def test_open_folder(csrf_client: Any, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(settings_mod, "_open_in_file_manager", lambda p: opened.append(p))
    r = csrf_client.post("/ajustes/abrir-carpeta", data={"que": "registros"}, headers=HX)
    assert r.status_code == 204 and opened == [data_dir.resolve() / "logs"]
    r = csrf_client.post("/ajustes/abrir-carpeta", data={"que": "datos"}, follow_redirects=False)
    assert r.status_code == 303 and opened[-1] == data_dir.resolve()

    def fail(p: Path) -> None:
        raise OSError("no file manager")

    monkeypatch.setattr(settings_mod, "_open_in_file_manager", fail)
    r = csrf_client.post("/ajustes/abrir-carpeta", data={"que": "datos"}, headers=HX)
    assert toast(r)["kind"] == "warning" and str(data_dir.resolve()) in toast(r)["message"]

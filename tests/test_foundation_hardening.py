"""Regression tests for defects found in the foundation review (datetimes, DB transactions, CSRF on multipart
forms, log redaction, password cache, CLI startup failure)."""

from __future__ import annotations

import contextlib
import gc
import hashlib
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from planificador import db as dbmod
from planificador import security
from planificador.models import Account, Brand, Post, PostTarget, Setting
from planificador.providers.base import scrub_text

# ------------------------------------------------------------------------------------ aware datetimes


def test_assigned_datetimes_are_aware_utc_in_memory(db: Session) -> None:
    """Sessions use expire_on_commit=False: an assigned naive value used to stay naive in memory and break
    comparisons with clock.now() (TypeError) until the object was reloaded."""
    brand = Brand(name="Marca")
    db.add(brand)
    db.flush()
    post = Post(brand_id=brand.id, scheduled_at=datetime(2026, 11, 3, 9, 0))  # naive = UTC
    db.add(post)
    db.commit()
    assert post.scheduled_at == datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert post.scheduled_at.tzinfo is UTC
    assert post.scheduled_at <= datetime(2026, 11, 3, 10, 0, tzinfo=UTC)  # no TypeError

    madrid_summer = timezone(timedelta(hours=2))
    post.deleted_at = datetime(2026, 7, 1, 12, 0, tzinfo=madrid_summer)
    assert post.deleted_at == datetime(2026, 7, 1, 10, 0, tzinfo=UTC) and post.deleted_at.tzinfo is UTC
    post.deleted_at = None
    assert post.deleted_at is None

    account = Account(
        brand_id=brand.id, network="demo", external_id="d", token_expires_at=datetime(2026, 1, 1)
    )
    target = PostTarget(post=post, account=account, network="demo", next_attempt_at=datetime(2026, 1, 1, 8))
    db.add_all([account, target])
    db.commit()
    assert account.to_creds().token_expires_at.tzinfo is UTC
    assert target.next_attempt_at.tzinfo is UTC


# ------------------------------------------------------------------------------------- transactions


def test_begin_nested_first_write_is_rolled_back_with_outer_transaction(tmp_path: Path) -> None:
    """pysqlite only emits BEGIN before DML, so a SAVEPOINT issued first became the outermost transaction and
    RELEASE committed it immediately (surviving the outer rollback)."""
    database = dbmod.Database(tmp_path / "n.sqlite3")
    database.init()

    def brands() -> int:
        with database.session() as s:
            return s.scalar(select(func.count()).select_from(Brand))

    with database.session() as s:
        with s.begin_nested():
            s.add(Brand(name="dentro"))
        s.rollback()
    assert brands() == 0

    with database.session() as s:
        with s.begin_nested():
            s.add(Brand(name="dentro"))
        s.commit()
    assert brands() == 1

    # Typical use: swallow a unique violation inside a savepoint and keep the outer work.
    with database.session() as s:
        s.add(Setting(key="k", value=1))
        s.flush()
        with pytest.raises(IntegrityError), s.begin_nested():
            s.add(Setting(key="k", value=2))
            s.flush()
        s.add(Brand(name="después"))
        s.commit()
    assert brands() == 2
    database.dispose()


def test_failed_migration_is_rolled_back_and_can_be_retried(tmp_path: Path, monkeypatch: Any) -> None:
    """DDL used to autocommit statement by statement: a migration failing halfway left its ALTER TABLE applied
    with the old version stamp, and every later start failed with "duplicate column"."""
    database = dbmod.Database(tmp_path / "m.sqlite3")
    database.init()
    target = dbmod.SCHEMA_VERSION + 1

    def broken(conn: Any) -> None:
        conn.execute(text("ALTER TABLE brands ADD COLUMN slogan VARCHAR(80)"))
        raise RuntimeError("fallo a mitad de migración")

    monkeypatch.setitem(dbmod.MIGRATIONS, target, broken)
    with pytest.raises(RuntimeError):
        dbmod.init_db(database.engine, target=target)
    assert "slogan" not in {c["name"] for c in inspect(database.engine).get_columns("brands")}
    with database.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_version").scalar() == dbmod.SCHEMA_VERSION

    def fixed(conn: Any) -> None:
        conn.execute(text("ALTER TABLE brands ADD COLUMN slogan VARCHAR(80)"))

    monkeypatch.setitem(dbmod.MIGRATIONS, target, fixed)
    assert dbmod.init_db(database.engine, target=target) == target
    assert "slogan" in {c["name"] for c in inspect(database.engine).get_columns("brands")}
    database.dispose()


# ------------------------------------------------------------------------------ CSRF on multipart forms


def _add_upload_route(app: FastAPI) -> None:
    @app.post("/__test/subir")
    async def upload(request: Request) -> dict[str, Any]:
        form = await request.form()
        out: dict[str, Any] = {}
        for key, value in form.multi_items():
            out[key] = value if isinstance(value, str) else hashlib.sha256(await value.read()).hexdigest()
        return out


def test_csrf_token_as_multipart_form_field(app: FastAPI, client: TestClient, csrf_token: str) -> None:
    """SPEC §3 accepts the token as the ``csrf_token`` form field; plain multipart forms (no htmx header)
    used to be rejected unless the token was in the query string."""
    _add_upload_route(app)
    token = csrf_token
    video = bytes(range(256)) * 12_000  # ~3 MB: spans many receive() chunks; must reach the endpoint intact
    r = client.post(
        "/__test/subir",
        data={"csrf_token": token, "nombre": "Ana ñ"},
        files={"archivo": ("v.mp4", video, "video/mp4")},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "csrf_token": token,
        "nombre": "Ana ñ",
        "archivo": hashlib.sha256(video).hexdigest(),
    }

    files = {"archivo": ("a.txt", b"hola", "text/plain")}
    assert client.post("/__test/subir", data={"csrf_token": "otro"}, files=files).status_code == 403
    assert client.post("/__test/subir", data={"nombre": "x"}, files=files).status_code == 403
    assert client.post("/__test/subir", files=files).status_code == 403
    # Header and query parameter keep working.
    assert client.post("/__test/subir", files=files, headers={"X-CSRF-Token": token}).status_code == 200
    assert client.post(f"/__test/subir?csrf_token={token}", files=files).status_code == 200


def test_csrf_multipart_field_must_precede_files(app: FastAPI, client: TestClient, csrf_token: str) -> None:
    """Only the start of the body is scanned (uploads are never buffered): a token after a file is ignored."""
    _add_upload_route(app)
    token = csrf_token
    boundary = "----limite"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="archivo"; filename="a.txt"\r\n'
        "Content-Type: text/plain\r\n\r\nhola\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
        f"{token}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    r = client.post(
        "/__test/subir", content=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    assert r.status_code == 403


async def _run_asgi(app: Any, scope: dict[str, Any], chunks: list[bytes]) -> tuple[int, bytes]:
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 100_000])
def test_csrf_multipart_scan_replays_body_exactly(chunk_size: int) -> None:
    """The token may be split across receive() chunks and the endpoint must see the exact original body."""
    import asyncio

    token = "tok-123_ABC"
    boundary = "xyz"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="titulo"\r\n\r\nHola\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{token}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="x.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + bytes(range(256)) * 40
        + f"\r\n--{boundary}--\r\n".encode()
    )

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        received = b""
        while True:
            message = await receive()
            received += message.get("body", b"")
            if not message.get("more_body"):
                break
        payload = hashlib.sha256(received).hexdigest().encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": payload})

    middleware = security.SecurityMiddleware(inner, allowed_origins=["http://localhost:8765"])
    chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]

    def scope(session_token: str) -> dict[str, Any]:
        return {
            "type": "http",
            "method": "POST",
            "path": "/subir",
            "query_string": b"",
            "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
            "session": {security.CSRF_SESSION_KEY: session_token},
        }

    status, payload = asyncio.run(_run_asgi(middleware, scope(token), chunks))
    assert status == 200 and payload == hashlib.sha256(body).hexdigest().encode()
    status, _ = asyncio.run(_run_asgi(middleware, scope("different"), list(chunks)))
    assert status == 403


# ----------------------------------------------------------------------------------------- logging


def test_logging_redacts_secrets_in_tracebacks(app: FastAPI, data_dir: Path) -> None:
    """Exception text (e.g. httpx errors quoting the request URL) bypassed the redaction filter."""
    log = logging.getLogger("planificador.test.traceback")
    try:
        raise RuntimeError(
            "Client error for url 'https://graph.facebook.com/v25.0/oauth/access_token"
            "?client_id=1&client_secret=TOPSECRET&code=OAUTHCODE'"
        )
    except RuntimeError:
        log.exception("Fallo al canjear el código")
    log.warning("pila", stack_info=True, extra={})
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (data_dir / "logs" / "app.log").read_text(encoding="utf-8")
    assert "Fallo al canjear el código" in content and "RuntimeError" in content
    assert "TOPSECRET" not in content and "OAUTHCODE" not in content
    assert "client_secret=***" in content


def test_scrub_text_masks_csrf_token_in_query_strings() -> None:
    # Multipart uploads may carry the CSRF token in the query string, which ends up in access logs.
    assert (
        scrub_text("POST /medios?csrf_token=abc123&x=1 HTTP/1.1")
        == "POST /medios?csrf_token=***&x=1 HTTP/1.1"
    )


# ------------------------------------------------------------------------------------ password cache


def test_password_cache_is_keyed_by_database_object(monkeypatch: Any) -> None:
    """The middleware cached the hash under ``id(database)``: once a database was collected, a new one
    reusing its id (consecutive test apps) was served the old database's password hash for 5 s."""

    class FakeDatabase:
        def __init__(self, stored: str | None) -> None:
            self.stored = stored

        def session(self) -> Any:
            return contextlib.nullcontext(self)

    def scope_for(database: FakeDatabase) -> dict[str, Any]:
        return {"app": SimpleNamespace(state=SimpleNamespace(db=database))}

    monkeypatch.setattr(security, "get_password_hash", lambda s: s.stored)
    monkeypatch.setattr(security, "_password_cache", security._PasswordCache())

    first = FakeDatabase("hash-uno")
    assert security._default_password_loader(scope_for(first)) == "hash-uno"
    first.stored = "cambiada"
    assert security._default_password_loader(scope_for(first)) == "hash-uno"  # cached for a few seconds
    old_id = id(first)
    del first
    gc.collect()
    keep: list[FakeDatabase] = []
    second = FakeDatabase(None)
    for _ in range(10_000):  # CPython normally hands the freed slot to the next object of the same type
        if id(second) == old_id:
            break
        keep.append(second)
        second = FakeDatabase(None)
    assert security._default_password_loader(scope_for(second)) is None


# --------------------------------------------------------------------------------------------- CLI


def test_cli_explains_port_in_use(data_dir: Path, monkeypatch: Any, capsys: Any) -> None:
    """Uvicorn calls sys.exit(3) when the port cannot be bound; main() used to let it escape, so the Spanish
    hint was never printed."""
    import uvicorn

    from planificador import __main__ as cli

    class PortInUse:
        def __init__(self, config: Any) -> None:
            self.started = False

        def run(self) -> None:
            raise SystemExit(3)

    monkeypatch.setattr(uvicorn, "Server", PortInUse)
    assert cli.main(["--data-dir", str(data_dir), "--port", "8798", "--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "No se pudo iniciar el servidor en el puerto 8798" in err
    assert not (data_dir / "instance.json").exists()  # lock released

    class ExitAfterStart(PortInUse):
        def run(self) -> None:
            self.started = True
            raise SystemExit(5)

    monkeypatch.setattr(uvicorn, "Server", ExitAfterStart)
    with pytest.raises(SystemExit):
        cli.main(["--data-dir", str(data_dir), "--port", "8798", "--no-browser"])
    assert not (data_dir / "instance.json").exists()


# ------------------------------------------------------------------------- YouTube (SPEC update §4–§6, §15)


def test_youtube_network_contract(db: Session, app_settings: Any) -> None:
    """The SPEC now lists youtube as a fourth network: the strict enum used to reject it at flush."""
    from planificador import config
    from planificador.models import NETWORK_LABELS, Network
    from planificador.providers import registry
    from planificador.providers.youtube import YouTubeProvider, redirect_uri

    assert Network("youtube") is Network.YOUTUBE and NETWORK_LABELS["youtube"] == "YouTube"
    brand = Brand(name="Marca")
    db.add(brand)
    db.flush()
    db.add(Account(brand_id=brand.id, network="youtube", external_id="UC123", name="Mi canal"))
    db.commit()
    assert db.execute(text("SELECT network FROM accounts")).scalar() == "youtube"

    config.set_setting(db, "google_client_id", "123.apps.googleusercontent.com")
    config.set_setting(db, "google_client_secret", "GOCSPX-secreto")
    raw = db.execute(text("SELECT value FROM settings WHERE key = 'google_client_secret'")).scalar()
    assert "GOCSPX" not in raw  # always encrypted (SECRET_KEYS)
    ps = config.provider_settings(db)
    assert ps.google_client_id == "123.apps.googleusercontent.com"
    assert ps.google_client_secret == "GOCSPX-secreto"
    assert redirect_uri(ps) == "http://localhost:8765/conexiones/callback/google"
    assert config.public_settings(db)["google_client_secret"].startswith("****")

    assert isinstance(registry.get_provider("youtube"), YouTubeProvider)
    assert "youtube" in registry.available_networks(False)


def test_youtube_in_ui_maps() -> None:
    from planificador import templating

    assert templating.NETWORKS.index("youtube") == templating.NETWORKS.index("demo") - 1
    assert templating.network_label("youtube") == "YouTube"
    html = str(templating.net_icon("youtube", 24))
    assert "net-icon--youtube" in html and 'aria-label="YouTube"' in html
    assert templating._simple_icon_path("youtube")  # vendored simple-icons logo, not the globe fallback
    css = (templating.STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    assert "--yt: #ff0033" in css and ".net-icon--youtube" in css


def test_status_labels_have_one_source() -> None:
    """models and templating used to word the same statuses differently ("Comprobar en la red" vs "Sin
    confirmar"), so the drawer and the badges disagreed."""
    from planificador import templating
    from planificador.models import AccountStatus, TargetStatus

    for status in TargetStatus:
        assert PostTarget(status=status).status_label == templating.status_label(status)
    for status in AccountStatus:
        assert templating.ACCOUNT_STATUS_LABELS[str(status)]
    assert templating.status_label("unknown") == "Sin confirmar"


def test_cli_rejects_invalid_port(capsys: Any) -> None:
    """--port 70000 used to reach the socket layer and die with an OverflowError traceback."""
    from planificador.__main__ import build_parser

    for bad in ("70000", "0", "abc"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--port", bad])
        assert "puerto no válido" in capsys.readouterr().err
    assert build_parser().parse_args(["--port", "8799"]).port == 8799

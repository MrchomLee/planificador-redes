"""Foundation tests: models, settings/secrets, crypto, security middleware, demo provider, timeutil, CLI."""

from __future__ import annotations

import logging
import os
import stat
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, Form, Request
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from planificador import config, crypto, security
from planificador import db as dbmod
from planificador.models import (
    Account,
    AccountStatus,
    AnalyticsSnapshot,
    Brand,
    CommitPhase,
    InboxComment,
    MediaAsset,
    MediaKind,
    Network,
    Notification,
    NotificationKind,
    OAuthState,
    Post,
    PostEvent,
    PostKind,
    PostMetric,
    PostTarget,
    PublicMediaToken,
    Setting,
    TargetStatus,
    derive_post_status,
)
from planificador.providers import registry
from planificador.providers.base import (
    AccountCreds,
    ErrorKind,
    ProviderError,
    ProviderSettings,
    PublishContext,
    TargetSpec,
    scrub,
)
from planificador.providers.demo import DemoProvider
from planificador.timeutil import (
    FixedClock,
    ManualClock,
    format_date_es,
    format_dt_es,
    format_duration_es,
    format_month_year_es,
    format_relative_es,
    format_short_date_es,
    from_brand_local,
    local_day_bounds,
    local_wall_iso,
    parse_wall_iso,
    to_brand_local,
    wall_time_status,
)

BASE_URL = "http://localhost:8765"

MADRID = "Europe/Madrid"


def fetch_csrf_token(client: TestClient) -> str:
    return client.get("/api/csrf").json()["csrf_token"]


# ============================================================================================ models


def _brand(db: Session, name: str = "Marca") -> Brand:
    brand = Brand(name=name)
    db.add(brand)
    db.flush()
    return brand


def _asset(db: Session, sha: str, kind: MediaKind = MediaKind.IMAGE) -> MediaAsset:
    asset = MediaAsset(
        kind=kind,
        original_name=f"{sha}.jpg",
        rel_path=f"originals/{sha[:2]}/{sha}.jpg",
        sha256=sha,
        size_bytes=1234,
        mime="image/jpeg",
        width=1080,
        height=1350,
    )
    db.add(asset)
    db.flush()
    return asset


def test_models_relationships_and_defaults(db: Session) -> None:
    brand = _brand(db)
    account = Account(brand_id=brand.id, network="demo", external_id="d1", name="Demo")
    db.add(account)
    a1, a2 = _asset(db, "a" * 64), _asset(db, "b" * 64)
    post = Post(brand_id=brand.id, text="Hola mundo", scheduled_at=datetime(2026, 11, 3, 9, tzinfo=UTC))
    post.set_media([a1, a2])
    target = PostTarget(post=post, account=account, network=Network.DEMO, format="post", options={"x": 1})
    db.add_all([post, target])
    db.commit()

    db.expire_all()
    post = db.get(Post, post.id)
    assert post is not None
    assert post.kind == PostKind.SCHEDULED
    assert post.brand.name == "Marca"
    assert [m.sha256 for m in post.media] == ["a" * 64, "b" * 64]
    assert post.created_at.tzinfo is not None and post.created_at.utcoffset() == timedelta(0)
    t = post.targets[0]
    assert t.status == TargetStatus.DRAFT
    assert t.commit_phase == CommitPhase.NONE
    assert t.auto_publish is True and t.attempts == 0
    assert t.provider_state == {} and t.options == {"x": 1}
    assert t.effective_text == "Hola mundo"
    assert t.effective_media_ids == [a1.id, a2.id]
    t.text_override = "Solo demo"
    t.media_override = [a2.id]
    assert t.effective_text == "Solo demo" and t.effective_media_ids == [a2.id]
    assert t.account.brand_id == brand.id
    assert brand.account_for("demo") is account
    assert post.display_status == TargetStatus.DRAFT

    # reorder media (row switch on the composite PK)
    post.set_media([a2, a1])
    db.commit()
    db.expire_all()
    assert [m.id for m in db.get(Post, post.id).media] == [a2.id, a1.id]


def test_event_and_notification_rows(db: Session) -> None:
    brand = _brand(db)
    account = Account(brand_id=brand.id, network=Network.DEMO, external_id="d1", name="Demo")
    post = Post(brand_id=brand.id, text="x")
    target = PostTarget(post=post, account=account, network="demo")
    db.add_all([account, post, target])
    db.flush()
    db.add(PostEvent(target_id=target.id, event="claimed", message="Publicando…", data={"a": 1}))
    db.add(Notification(kind=NotificationKind.ERROR, title="Fallo", body="x", target_id=target.id))
    db.commit()
    db.expire_all()
    target = db.get(PostTarget, target.id)
    assert [e.event for e in target.events] == ["claimed"]
    assert target.events[0].level == "info"
    notif = db.scalars(select(Notification)).one()
    assert notif.target is target and not notif.is_read


def test_utc_datetime_type_roundtrip(db: Session) -> None:
    brand = _brand(db)
    madrid_time = datetime(2026, 7, 1, 12, 0, tzinfo=ZoneInfo(MADRID))  # 10:00 UTC
    post = Post(brand_id=brand.id, scheduled_at=madrid_time)
    naive = Post(brand_id=brand.id, scheduled_at=datetime(2026, 7, 1, 10, 0))  # naive = UTC
    db.add_all([post, naive])
    db.commit()
    raw = db.execute(text("SELECT scheduled_at FROM posts ORDER BY id")).scalars().all()
    assert all(str(v).startswith("2026-07-01 10:00:00") for v in raw)
    db.expire_all()
    for p in (db.get(Post, post.id), db.get(Post, naive.id)):
        assert p.scheduled_at == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        assert p.scheduled_at.tzinfo == UTC


def test_enums_stored_as_strings(db: Session) -> None:
    brand = _brand(db)
    account = Account(brand_id=brand.id, network="facebook", external_id="p1", name="Página")
    db.add(account)
    db.commit()
    assert db.execute(text("SELECT network, status FROM accounts")).one() == ("facebook", "connected")
    db.expire_all()
    loaded = db.get(Account, account.id)
    assert loaded.network is Network.FACEBOOK and loaded.network == "facebook"
    assert loaded.status is AccountStatus.CONNECTED

    bad = Account(brand_id=brand.id, network="myspace", external_id="x", name="x")
    db.add(bad)
    with pytest.raises(StatementError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda db, b, a: [
                Account(brand_id=b.id, network="instagram", external_id="1", name="a"),
                Account(brand_id=b.id, network="instagram", external_id="2", name="b"),
            ],
            id="account-per-brand-network",
        ),
        pytest.param(
            lambda db, b, a: [
                MediaAsset(kind="image", rel_path="x", sha256="c" * 64),
                MediaAsset(kind="image", rel_path="y", sha256="c" * 64),
            ],
            id="media-sha256",
        ),
        pytest.param(
            lambda db, b, a: [
                AnalyticsSnapshot(account_id=a.id, day=date(2026, 11, 1), metrics={"followers": 1}),
                AnalyticsSnapshot(account_id=a.id, day=date(2026, 11, 1), metrics={"followers": 2}),
            ],
            id="snapshot-per-day",
        ),
        pytest.param(
            lambda db, b, a: [
                PostMetric(account_id=a.id, external_id="m1"),
                PostMetric(account_id=a.id, external_id="m1"),
            ],
            id="post-metric",
        ),
        pytest.param(
            lambda db, b, a: [
                InboxComment(
                    account_id=a.id, external_id="c1", object_external_id="o", created_time=datetime.now(UTC)
                ),
                InboxComment(
                    account_id=a.id, external_id="c1", object_external_id="o", created_time=datetime.now(UTC)
                ),
            ],
            id="inbox-comment",
        ),
    ],
)
def test_unique_constraints(db: Session, factory: Any) -> None:
    brand = _brand(db)
    account = Account(brand_id=brand.id, network="demo", external_id="d", name="d")
    db.add(account)
    db.commit()
    first, second = factory(db, brand, account)
    db.add(first)
    db.commit()
    db.add(second)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_same_network_allowed_in_different_brands(db: Session) -> None:
    b1, b2 = _brand(db, "Uno"), _brand(db, "Dos")
    db.add_all(
        [
            Account(brand_id=b1.id, network="tiktok", external_id="t1", name="a"),
            Account(brand_id=b2.id, network="tiktok", external_id="t2", name="b"),
        ]
    )
    db.commit()
    assert db.scalar(select(Account).where(Account.brand_id == b2.id)).network == Network.TIKTOK


def test_brand_delete_cascades(db: Session) -> None:
    brand = _brand(db)
    account = Account(brand_id=brand.id, network="demo", external_id="d", name="d")
    post = Post(brand_id=brand.id, text="x")
    target = PostTarget(post=post, account=account, network="demo")
    db.add_all([account, post, target])
    db.commit()
    db.delete(brand)
    db.commit()
    for model in (Account, Post, PostTarget):
        assert db.scalar(select(model)) is None


def test_foreign_keys_enforced(db: Session) -> None:
    db.add(Account(brand_id=999, network="demo", external_id="d", name="d"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_mutable_json_columns_track_changes(db: Session) -> None:
    brand = _brand(db)
    account = Account(brand_id=brand.id, network="demo", external_id="d", name="d")
    post = Post(brand_id=brand.id)
    target = PostTarget(post=post, account=account, network="demo")
    db.add_all([account, post, target])
    db.commit()
    target.options["privacy_level"] = "SELF_ONLY"
    target.provider_state["step"] = 2
    account.scopes.append("video.publish")
    db.commit()
    db.expire_all()
    t = db.get(PostTarget, target.id)
    assert t.options == {"privacy_level": "SELF_ONLY"}
    assert t.provider_state == {"step": 2}
    assert t.account.scopes == ["video.publish"]


def test_account_tokens_encrypted_at_rest(db: Session) -> None:
    brand = _brand(db)
    account = Account(brand_id=brand.id, network="tiktok", external_id="open1", name="TT", meta={"k": "v"})
    expires = datetime(2026, 12, 1, tzinfo=UTC)
    account.set_tokens("access-123", "refresh-456", token_expires_at=expires)
    db.add(account)
    db.commit()
    raw = db.execute(text("SELECT access_token_enc, refresh_token_enc FROM accounts")).one()
    assert all(v.startswith("v1:") for v in raw)
    assert "access-123" not in raw[0] and "refresh-456" not in raw[1]
    creds = db.get(Account, account.id).to_creds()
    assert isinstance(creds, AccountCreds)
    assert (creds.access_token, creds.refresh_token) == ("access-123", "refresh-456")
    assert creds.token_expires_at == expires and creds.meta == {"k": "v"} and creds.network == "tiktok"
    account.wipe_tokens()
    assert account.access_token is None and account.refresh_token is None


def test_oauth_state_and_public_token_helpers() -> None:
    now = datetime(2026, 11, 3, 9, tzinfo=UTC)
    st = OAuthState(state="s", network="meta", created_at=now - timedelta(minutes=11))
    assert st.is_expired(now)
    tok = PublicMediaToken(token="t", rel_path="x", mime="image/jpeg", expires_at=now + timedelta(minutes=5))
    assert tok.is_valid(now)
    tok.revoked_at = now
    assert not tok.is_valid(now)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "draft"),
        (["published", "error", "scheduled"], "error"),
        (["unknown", "missed"], "unknown"),
        (["missed", "manual_pending"], "missed"),
        (["manual_pending", "publishing"], "manual_pending"),
        (["publishing", "scheduled"], "publishing"),
        (["scheduled", "sent_to_app", "draft"], "scheduled"),
        (["sent_to_app", "published"], "sent_to_app"),
        (["draft", "published"], "draft"),
        (["published", "manual_done"], "published"),
        (["manual_done"], "manual_done"),
        (["published", "cancelled"], "published"),
        (["cancelled", "cancelled"], "cancelled"),
    ],
)
def test_derive_post_status(statuses: list[str], expected: str) -> None:
    assert derive_post_status(statuses) == expected


def test_sqlite_pragmas(app: FastAPI) -> None:
    with app.state.db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL


def test_schema_version_and_migration_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = dbmod.Database(tmp_path / "m.sqlite3")
    assert database.init() == dbmod.SCHEMA_VERSION
    assert database.init() == dbmod.SCHEMA_VERSION  # idempotent
    tables = set(inspect(database.engine).get_table_names())
    expected = {
        "settings",
        "brands",
        "accounts",
        "media_assets",
        "posts",
        "post_media",
        "post_targets",
        "post_events",
        "notifications",
        "analytics_snapshots",
        "post_metrics",
        "inbox_comments",
        "oauth_states",
        "public_media_tokens",
        "schema_version",
    }
    assert expected <= tables

    calls: list[int] = []

    def to_v2(conn: Any) -> None:
        calls.append(2)
        conn.execute(text("ALTER TABLE brands ADD COLUMN slogan VARCHAR(80)"))

    monkeypatch.setitem(dbmod.MIGRATIONS, dbmod.SCHEMA_VERSION + 1, to_v2)
    assert dbmod.init_db(database.engine, target=dbmod.SCHEMA_VERSION + 1) == dbmod.SCHEMA_VERSION + 1
    assert dbmod.init_db(database.engine, target=dbmod.SCHEMA_VERSION + 1) == dbmod.SCHEMA_VERSION + 1
    assert calls == [2]
    assert "slogan" in {c["name"] for c in inspect(database.engine).get_columns("brands")}
    database.dispose()


def test_database_backup(app: FastAPI, tmp_path: Path) -> None:
    dest = app.state.db.backup(tmp_path / "backup" / "copia.sqlite3")
    assert dest.is_file() and dest.stat().st_size > 0


# ========================================================================================== settings


def test_setting_defaults(db: Session) -> None:
    assert config.get_setting(db, "meta_api_version") == "v25.0"
    assert config.get_setting(db, "tiktok_pkce_encoding") == "hex"
    assert config.get_setting(db, "grace_minutes") == 120
    assert config.get_setting(db, "public_media_mode") == "off"
    assert config.get_setting(db, "default_timezone") == "Europe/Madrid"
    assert config.get_setting(db, "demo_mode") is False
    assert config.get_setting(db, "nope") is None
    assert config.get_setting(db, "nope", "fallback") == "fallback"


@pytest.mark.parametrize("value", [42, "texto ñ", True, [1, 2], {"a": {"b": [1]}}, 1.5])
def test_setting_roundtrip(db: Session, value: Any) -> None:
    config.set_setting(db, "custom", value)
    assert config.get_setting(db, "custom") == value
    config.set_setting(db, "custom", "otro")  # upsert
    assert config.get_setting(db, "custom") == "otro"
    config.delete_setting(db, "custom")
    assert config.get_setting(db, "custom") is None


def test_secret_settings_encrypted_at_rest(db: Session) -> None:
    config.set_setting(db, "meta_app_secret", "super-secreto")  # always a secret key
    config.set_setting(db, "my_token", "tok-123", secret=True)
    rows = dict(db.execute(select(Setting.key, Setting.value)).all())
    for key, plain in (("meta_app_secret", "super-secreto"), ("my_token", "tok-123")):
        stored = rows[key]
        assert isinstance(stored, dict) and stored["enc"].startswith("v1:")
        assert plain not in str(stored)
        assert config.get_secret(db, key) == plain
        assert config.get_setting(db, key) == plain
    assert config.is_secret_set(db, "meta_app_secret")
    config.set_setting(db, "meta_app_secret", "")
    assert config.get_secret(db, "meta_app_secret") is None
    assert not config.is_secret_set(db, "meta_app_secret")


def test_set_setting_refreshes_loaded_orm_object(db: Session) -> None:
    config.set_setting(db, "grace_minutes", 30)
    obj = db.get(Setting, "grace_minutes")
    assert obj.value == 30
    config.set_setting(db, "grace_minutes", 45)
    assert obj.value == 45


def test_provider_settings(db: Session, app_settings) -> None:
    config.set_setting(db, "meta_app_id", "123")
    config.set_setting(db, "meta_app_secret", "abc")
    config.set_setting(db, "tiktok_client_key", "ck")
    config.set_setting(db, "tiktok_client_secret", "cs")
    config.set_setting(db, "demo_mode", True)
    ps = config.provider_settings(db)
    assert ps == ProviderSettings(
        meta_app_id="123",
        meta_app_secret="abc",
        meta_api_version="v25.0",
        tiktok_client_key="ck",
        tiktok_client_secret="cs",
        tiktok_pkce_encoding="hex",
        base_url="http://localhost:8765",
        demo_mode=True,
        tiktok_scopes=["user.info.basic", "video.publish", "video.upload"],  # defaults (config.tiktok_scopes)
    )
    config.set_setting(db, "tiktok_pkce_encoding", "weird")
    assert config.provider_settings(db, base_url="http://localhost:9000").tiktok_pkce_encoding == "hex"
    shown = config.public_settings(db)
    assert shown["meta_app_secret"] in ("****", "") or shown["meta_app_secret"].startswith("****")
    assert "abc" not in str(shown)


def test_app_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLANIFICADOR_PORT", "9100")
    monkeypatch.setenv("PLANIFICADOR_NO_SCHEDULER", "1")
    s = config.load_app_settings()
    assert s.port == 9100 and s.scheduler_enabled is False
    assert s.base_url == "http://localhost:9100"
    assert s.allowed_hosts == {"localhost:9100", "127.0.0.1:9100"}
    assert s.pubmedia_port == 9101
    monkeypatch.delenv("PLANIFICADOR_NO_SCHEDULER")
    assert config.load_app_settings(data_dir=tmp_path).scheduler_enabled is True
    assert config.load_app_settings(data_dir=tmp_path).data_dir == tmp_path.resolve()


def test_active_brand_autocreated(db: Session) -> None:
    from planificador.deps import ensure_active_brand, set_active_brand

    brand = ensure_active_brand(db)
    assert brand.name == "Mi marca" and brand.timezone == "Europe/Madrid"
    assert config.get_setting(db, "active_brand_id") == brand.id
    assert ensure_active_brand(db).id == brand.id
    assert len(db.scalars(select(Brand)).all()) == 1

    other = _brand(db, "Otra")
    db.commit()
    set_active_brand(db, other.id)
    assert ensure_active_brand(db).id == other.id
    other.archived_at = datetime.now(UTC)
    db.commit()
    assert ensure_active_brand(db).id == brand.id


def test_active_brand_uses_default_timezone_setting(db: Session) -> None:
    from planificador.deps import ensure_active_brand

    config.set_setting(db, "default_timezone", "America/Mexico_City")
    assert ensure_active_brand(db).timezone == "America/Mexico_City"


# ============================================================================================ crypto


def test_crypto_key_file_fallback(tmp_path: Path) -> None:
    key_file = tmp_path / "secret.key"
    box = crypto.init(key_file)  # PLANIFICADOR_NO_KEYRING=1 in tests
    assert box.source == "file" and key_file.is_file()
    if sys.platform != "win32":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    token = crypto.encrypt("hola ñ")
    assert token.startswith("v1:") and crypto.is_encrypted(token)
    assert crypto.decrypt(token) == "hola ñ"
    secret_1 = crypto.session_secret()
    crypto.reset()
    crypto.init(key_file)
    assert crypto.decrypt(token) == "hola ñ"  # same key read back from the file
    assert crypto.session_secret() == secret_1


def test_crypto_keyring_failure_falls_back_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.errors import NoKeyringError

    def boom(*_args: Any) -> None:
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(keyring, "set_password", boom)
    master = crypto.load_or_create_master_key(tmp_path / "secret.key", use_keyring=True)
    assert master.source == "file" and (tmp_path / "secret.key").exists()


def test_crypto_null_keyring_backend_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    monkeypatch.setattr(keyring, "set_password", lambda *a: None)  # silently drops the value
    master = crypto.load_or_create_master_key(tmp_path / "secret.key", use_keyring=True)
    assert master.source == "file"


def test_crypto_uses_keyring_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: store.__setitem__((s, u), v))
    first = crypto.load_or_create_master_key(tmp_path / "secret.key", use_keyring=True)
    assert first.source == "keyring"
    assert ("planificador-redes", "master-key") in store
    assert not (tmp_path / "secret.key").exists()
    second = crypto.load_or_create_master_key(tmp_path / "secret.key", use_keyring=True)
    assert second.key == first.key


def test_crypto_existing_key_file_wins_over_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from cryptography.fernet import Fernet

    file_master = crypto.load_or_create_master_key(tmp_path / "secret.key", use_keyring=False)
    monkeypatch.setattr(keyring, "get_password", lambda *a: Fernet.generate_key().decode())
    again = crypto.load_or_create_master_key(tmp_path / "secret.key", use_keyring=True)
    assert again.source == "file" and again.key == file_master.key


def test_crypto_wrong_key_and_bad_values(tmp_path: Path) -> None:
    token = crypto.init(tmp_path / "a.key").encrypt("x")
    other = crypto.init(tmp_path / "b.key")
    with pytest.raises(crypto.CryptoError):
        other.decrypt(token)
    with pytest.raises(crypto.CryptoError):
        other.decrypt("plain-text")
    assert crypto.decrypt_optional(token, on_error="none") is None
    assert crypto.decrypt_optional(None) is None and crypto.encrypt_optional("") is None
    assert crypto.mask_secret("abcdefghijkl") == "****ijkl"
    assert crypto.mask_secret("abc") == "****"
    assert crypto.mask_secret(None) == ""


# ========================================================================================== security


def _add_echo_route(app: FastAPI) -> None:
    @app.post("/__test/echo")
    async def echo(request: Request) -> dict[str, Any]:
        form = await request.form() if "form" in request.headers.get("content-type", "") else {}
        return {"ok": True, "fields": {k: v for k, v in form.items() if isinstance(v, str)}}

    @app.post("/__test/form")
    def form_route(nombre: str = Form(...)) -> dict[str, str]:
        return {"nombre": nombre}


def test_host_header_rejected(app: FastAPI) -> None:
    with TestClient(app) as c:  # Host: testserver
        assert c.get("/healthz").status_code == 400
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/healthz").status_code == 200
        r = c.get("/healthz", headers={"host": "evil.example:8765"})
        assert r.status_code == 400 and "Host" in r.text
        assert c.get("/healthz", headers={"host": "localhost:9999"}).status_code == 400
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.get("/planificacion?vista=mes", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "http://localhost:8765/planificacion?vista=mes"


def test_csrf_rejection_and_acceptance(app: FastAPI, client: TestClient) -> None:
    _add_echo_route(app)
    assert client.post("/__test/echo").status_code == 403
    assert client.post("/__test/echo", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    token = fetch_csrf_token(client)
    assert client.post("/__test/echo", headers={"X-CSRF-Token": token}).json()["ok"] is True
    assert client.put("/__test/echo", headers={"X-CSRF-Token": "x"}).status_code == 403
    assert client.delete("/__test/echo").status_code == 403

    # urlencoded form field (body is replayed to the endpoint)
    r = client.post("/__test/form", data={"nombre": "Ana ñ", "csrf_token": token})
    assert r.status_code == 200 and r.json() == {"nombre": "Ana ñ"}
    assert client.post("/__test/form", data={"nombre": "Ana"}).status_code == 403

    # multipart: header or query parameter
    files = {"archivo": ("a.txt", b"hola", "text/plain")}
    assert client.post("/__test/echo", files=files).status_code == 403
    assert client.post(f"/__test/echo?csrf_token={token}", files=files).status_code == 200
    assert client.post("/__test/echo", files=files, headers={"X-CSRF-Token": token}).status_code == 200

    # another session's token is not valid here
    with TestClient(app, base_url=BASE_URL) as other:
        other_token = fetch_csrf_token(other)
        assert other_token != token
        assert client.post("/__test/echo", headers={"X-CSRF-Token": other_token}).status_code == 403


def test_csrf_token_exposed_on_request_state(app: FastAPI, client: TestClient) -> None:
    @app.get("/__test/token")
    def token_view(request: Request) -> dict[str, str]:
        return {"state": request.state.csrf_token, "helper": security.csrf_token(request)}

    data = client.get("/__test/token").json()
    assert data["state"] == data["helper"] == fetch_csrf_token(client)


def test_origin_and_fetch_metadata_checks(app: FastAPI, client: TestClient) -> None:
    _add_echo_route(app)
    token = fetch_csrf_token(client)

    def post(**headers: str) -> int:
        return client.post("/__test/echo", headers={"X-CSRF-Token": token, **headers}).status_code

    assert post(Origin="http://localhost:8765") == 200
    assert post(Origin="http://127.0.0.1:8765") == 200
    assert post(Origin="http://evil.example") == 403
    assert post(Origin="null") == 403
    assert post(Origin="http://localhost:3000") == 403
    assert post(Referer="http://localhost:8765/planificacion") == 200
    assert post(Referer="http://evil.example/x") == 403
    assert post(**{"Sec-Fetch-Site": "same-origin"}) == 200
    assert post(**{"Sec-Fetch-Site": "cross-site"}) == 403
    assert post(**{"Sec-Fetch-Site": "same-site"}) == 403


def test_security_headers_and_session_cookie(client: TestClient) -> None:
    r = client.get("/api/csrf")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("planificador_session=")
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # plain http on localhost


def test_password_hashing() -> None:
    stored = security.hash_password("clave segura")
    assert stored.startswith("scrypt$") and "clave" not in stored
    assert security.verify_password("clave segura", stored)
    assert not security.verify_password("otra", stored)
    assert not security.verify_password("", stored)
    assert not security.verify_password("x", "garbage")
    assert security.hash_password("x") != security.hash_password("x")  # salted


def test_password_login_flow(app: FastAPI, client: TestClient, db: Session) -> None:
    # No password: everything open, /login redirects home.
    assert client.get("/planificacion").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303

    security.set_password(db, "clave-123")
    r = client.get("/planificacion?vista=semana", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fplanificacion%3Fvista%3Dsemana"
    r = client.get("/planificacion", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["hx-redirect"].startswith("/login")

    # Exempt paths
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/does-not-exist.css").status_code == 404
    callback = client.get("/conexiones/callback/meta", follow_redirects=False)
    assert not callback.headers.get("location", "").startswith("/login")

    login_page = client.get("/login?next=/biblioteca")
    assert login_page.status_code == 200 and 'name="password"' in login_page.text
    token = fetch_csrf_token(client)
    bad = client.post("/login", data={"password": "nope", "next": "/biblioteca", "csrf_token": token})
    assert bad.status_code == 401 and "Contraseña incorrecta" in bad.text
    assert client.post("/login", data={"password": "clave-123"}).status_code == 403  # no CSRF

    ok = client.post(
        "/login",
        data={"password": "clave-123", "next": "/biblioteca", "csrf_token": token},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/biblioteca"
    assert client.get("/planificacion", follow_redirects=False).status_code == 200

    # Changing the password logs existing sessions out.
    security.set_password(db, "nueva")
    assert client.get("/planificacion", follow_redirects=False).status_code == 303

    token = fetch_csrf_token(client)
    client.post("/login", data={"password": "nueva", "csrf_token": token}, follow_redirects=False)
    assert client.get("/planificacion", follow_redirects=False).status_code == 200
    token = fetch_csrf_token(client)
    out = client.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    assert client.get("/planificacion", follow_redirects=False).status_code == 303

    # Open redirect protection
    token = fetch_csrf_token(client)
    r = client.post(
        "/login",
        data={"password": "nueva", "next": "//evil.example/x", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"

    security.set_password(db, None)
    assert client.get("/planificacion", follow_redirects=False).status_code == 200


# ============================================================================================== app


def test_app_basics(app: FastAPI, client: TestClient, data_dir: Path, clock: ManualClock) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    assert app.state.clock is clock
    assert app.state.settings.scheduler_enabled is False
    assert (data_dir / "planificador.sqlite3").exists()
    assert (data_dir / "logs" / "app.log").exists()
    assert client.get("/", follow_redirects=False).status_code in (200, 303, 307)
    for path in ("/planificacion", "/biblioteca", "/estadisticas", "/comentarios", "/conexiones", "/ajustes"):
        assert client.get(path).status_code == 200, path
    r = client.get("/no-existe", headers={"accept": "text/html"})
    assert r.status_code == 404


def test_scheduler_start_hook(app_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    from planificador import scheduler
    from planificador.app import create_app

    calls: list[str] = []
    # The lifespan hands the FastAPI app itself to the scheduler (it accepts the app or its state).
    monkeypatch.setattr(scheduler, "start", lambda app: calls.append(f"start:{app.state.settings.port}"))
    monkeypatch.setattr(scheduler, "stop", lambda app=None: calls.append("stop"))
    application = create_app(dataclasses.replace(app_settings, scheduler_enabled=True))
    with TestClient(application, base_url=BASE_URL) as c:
        assert c.get("/healthz").status_code == 200
    assert calls == ["start:8765", "stop"]

    def not_ready(app: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(scheduler, "start", not_ready)
    application = create_app(dataclasses.replace(app_settings, scheduler_enabled=True))
    with TestClient(application, base_url=BASE_URL) as c:  # tolerated
        assert c.get("/healthz").status_code == 200
    application.state.db.dispose()


def test_unhandled_error_returns_500(app: FastAPI) -> None:
    @app.get("/__test/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as c:
        r = c.get("/__test/boom", headers={"accept": "text/html"})
    assert r.status_code == 500 and "kaboom" not in r.text


def test_logging_redacts_tokens(app: FastAPI, data_dir: Path) -> None:
    log = logging.getLogger("planificador.test")
    log.warning("GET /cb?code=SECRETCODE&state=abc access_token=TOKEN123")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (data_dir / "logs" / "app.log").read_text(encoding="utf-8")
    assert "SECRETCODE" not in content and "TOKEN123" not in content
    assert "state=abc" in content


def test_paths_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from planificador import paths

    monkeypatch.setenv("PLANIFICADOR_DATA_DIR", str(tmp_path / "x"))
    assert paths.resolve_data_dir() == (tmp_path / "x").resolve()
    assert paths.resolve_data_dir(tmp_path / "y") == (tmp_path / "y").resolve()
    monkeypatch.delenv("PLANIFICADOR_DATA_DIR")
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.resolve_data_dir() == tmp_path / "Local" / "PlanificadorRedes"
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert paths.resolve_data_dir() == Path.home() / ".local" / "share" / "planificador-redes"
    p = paths.get_paths(tmp_path / "z").ensure()
    assert p.originals_dir.is_dir() and p.thumbs_dir.is_dir() and p.derived_dir.is_dir()
    assert p.bin_dir.is_dir() and p.logs_dir.is_dir()
    assert p.lock_file.name == "instance.lock" and p.db_path.name == "planificador.sqlite3"


# ===================================================================================== demo provider


class Recorder:
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.commits: list[dict] = []
        self.logs: list[tuple[str, str]] = []

    def save_state(self, state: dict) -> None:
        self.saved.append(dict(state))

    def before_commit(self, state: dict) -> None:
        self.commits.append(dict(state))

    def log(self, event: str, message: str, data: dict | None) -> None:
        self.logs.append((event, message))


def _settings(demo_mode: bool = True) -> ProviderSettings:
    return ProviderSettings(None, None, "v25.0", None, None, "hex", "http://localhost:8765", demo_mode)


def _creds(account_id: int = 3, **kw: Any) -> AccountCreds:
    return AccountCreds(
        id=account_id,
        network="demo",
        external_id=f"demo-{account_id}",
        name="Demo",
        account_type="demo",
        access_token="t",
        refresh_token=None,
        token_expires_at=kw.get("token_expires_at"),
        refresh_expires_at=None,
        meta=kw.get("meta", {}),
    )


def _ctx(
    options: dict | None = None, state: dict | None = None, rec: Recorder | None = None
) -> PublishContext:
    import httpx

    rec = rec or Recorder()
    target = TargetSpec("demo", "post", "Hola", [], options or {}, None, True)
    return PublishContext(
        account=_creds(),
        target=target,
        target_id=7,
        state=state if state is not None else {},
        settings=_settings(),
        http=httpx.Client(),
        clock=FixedClock(datetime(2026, 11, 3, 9, tzinfo=UTC)),
        public_media=None,
        save_state=rec.save_state,
        before_commit=rec.before_commit,
        log=rec.log,
    )


def test_demo_default_two_steps() -> None:
    provider = DemoProvider()
    rec = Recorder()
    ctx = _ctx(rec=rec)
    first = provider.publish_step(ctx)
    assert first.status == "wait" and first.wait_seconds == 1
    assert rec.commits == [] and rec.saved
    second = provider.publish_step(ctx)
    assert second.status == "published"
    assert second.external_id == "demo-7" and second.external_url is None
    assert len(rec.commits) == 1


def test_demo_single_step_and_sent_to_app() -> None:
    provider = DemoProvider()
    assert provider.publish_step(_ctx({"demo_steps": 1})).status == "published"
    assert (
        provider.publish_step(_ctx({"demo_steps": 1, "demo_result": "sent_to_app"})).status == "sent_to_app"
    )
    rec = Recorder()
    ctx = _ctx({"demo_steps": 4}, rec=rec)
    statuses = [provider.publish_step(ctx).status for _ in range(4)]
    assert statuses == ["wait", "wait", "wait", "published"] and len(rec.commits) == 1


@pytest.mark.parametrize(
    ("fail", "kind"),
    [
        ("transient", ErrorKind.TRANSIENT),
        ("rate_limited", ErrorKind.RATE_LIMITED),
        ("auth", ErrorKind.AUTH),
        ("permanent", ErrorKind.PERMANENT),
    ],
)
def test_demo_pre_commit_failures(fail: str, kind: ErrorKind) -> None:
    provider = DemoProvider()
    rec = Recorder()
    ctx = _ctx({"demo_steps": 1, "demo_fail": fail, "demo_fail_times": 1}, rec=rec)
    with pytest.raises(ProviderError) as info:
        provider.publish_step(ctx)
    assert info.value.kind == kind and info.value.code == f"demo_{fail}"
    assert info.value.message  # Spanish, user facing
    assert rec.commits == []  # failed before the commit point
    if fail == "rate_limited":
        assert info.value.retry_after == 60
    # the retry (re-entrant from the saved state) succeeds after demo_fail_times failures
    assert provider.publish_step(ctx).status == "published"
    assert len(rec.commits) == 1


def test_demo_fails_every_time_without_fail_times() -> None:
    provider = DemoProvider()
    ctx = _ctx({"demo_steps": 1, "demo_fail": "transient"})
    for _ in range(5):
        with pytest.raises(ProviderError):
            provider.publish_step(ctx)
    assert ctx.state["failures"] == 5
    assert provider.publish_step(_ctx({"demo_steps": 1, "demo_fail": "none"})).status == "published"


def test_demo_ambiguous_and_reconcile() -> None:
    provider = DemoProvider()
    rec = Recorder()
    ctx = _ctx({"demo_steps": 1, "demo_fail": "ambiguous", "demo_fail_times": 1}, rec=rec)
    with pytest.raises(ProviderError) as info:
        provider.publish_step(ctx)
    assert info.value.kind == ErrorKind.AMBIGUOUS
    assert len(rec.commits) == 1  # before_commit happened before the failure
    assert provider.reconcile(ctx).outcome == "unknown"
    ctx.target.options["demo_reconcile"] = "published"
    res = provider.reconcile(ctx)
    assert res.outcome == "published" and res.external_id == "demo-7"
    ctx.target.options["demo_reconcile"] = "not_published"
    assert provider.reconcile(ctx).outcome == "not_published"
    # engine's retry path after not_published
    assert provider.publish_step(ctx).status == "published"


def test_demo_reentrant_after_crash_post_commit() -> None:
    provider = DemoProvider()
    rec = Recorder()
    ctx = _ctx({"demo_steps": 1}, rec=rec)
    assert provider.publish_step(ctx).status == "published"
    persisted = rec.saved[-1]
    rec2 = Recorder()
    resumed = _ctx({"demo_steps": 1}, state=dict(persisted), rec=rec2)
    assert provider.reconcile(resumed).outcome == "published"
    assert provider.publish_step(resumed).status == "published"
    assert rec2.commits == []  # never commits twice


def test_demo_refresh_and_check_account() -> None:
    provider = DemoProvider()
    clock = FixedClock(datetime(2026, 11, 3, 9, tzinfo=UTC))
    import httpx

    with httpx.Client() as http:
        fresh = _creds(token_expires_at=clock.now() + timedelta(days=10))
        assert provider.refresh_credentials(fresh, _settings(), http, clock) is None
        expiring = _creds(token_expires_at=clock.now() + timedelta(minutes=20))
        update = provider.refresh_credentials(expiring, _settings(), http, clock)
        assert update is not None and update.token_expires_at == clock.now() + timedelta(days=60)
        provider.check_account(_creds(), _settings(), http)
        with pytest.raises(ProviderError) as info:
            provider.check_account(_creds(meta={"demo_broken": True}), _settings(), http)
        assert info.value.kind == ErrorKind.AUTH


def test_demo_validation() -> None:
    provider = DemoProvider()
    empty = TargetSpec("demo", "post", "  ", [], {}, None, True)
    assert [i.code for i in provider.validate(empty)] == ["demo_empty"]
    assert provider.validate(TargetSpec("demo", "post", "Hola", [], {}, None, True)) == []


def test_demo_analytics_and_comments_deterministic() -> None:
    import httpx

    provider = DemoProvider()
    since = datetime(2026, 10, 4, 9, tzinfo=UTC)
    with httpx.Client() as http:
        off = _settings(demo_mode=False)
        assert provider.fetch_account_snapshot(_creds(), off, http, date(2026, 11, 3)).metrics == {}
        assert provider.fetch_recent_posts(_creds(), off, http, since) == []
        assert provider.fetch_comments(_creds(), off, http, since) == []

        on = _settings()
        snap1 = provider.fetch_account_snapshot(_creds(), on, http, date(2026, 11, 3))
        snap2 = provider.fetch_account_snapshot(_creds(), on, http, date(2026, 11, 3))
        assert snap1 == snap2
        assert {"followers", "views", "reach", "interactions"} <= set(snap1.metrics)
        assert all(v >= 0 for v in snap1.metrics.values())
        other = provider.fetch_account_snapshot(_creds(account_id=4), on, http, date(2026, 11, 3))
        assert other.metrics != snap1.metrics

        posts = provider.fetch_recent_posts(_creds(), on, http, since)
        assert posts and posts == provider.fetch_recent_posts(_creds(), on, http, since)
        assert all(p.caption.startswith("[Demo]") for p in posts)
        assert all(since <= p.published_at <= since + timedelta(days=30) for p in posts)
        assert len({p.external_id for p in posts}) == len(posts)
        # overlapping windows return the same ids for the same days (stable upserts)
        later = provider.fetch_recent_posts(_creds(), on, http, since + timedelta(days=3))
        assert {p.external_id for p in posts} & {p.external_id for p in later}

        comments = provider.fetch_comments(_creds(), on, http, since)
        assert comments == provider.fetch_comments(_creds(), on, http, since)
        assert all(c.text.startswith("[Demo]") and c.raw == {"demo": True} for c in comments)
        reply_id = provider.reply_comment(_creds(), on, http, "c1", "¡Gracias!")
        assert reply_id.startswith("demo-reply-")


def test_registry() -> None:
    demo = registry.get_provider("demo")
    assert isinstance(demo, DemoProvider) and registry.get_provider("demo") is demo
    networks = [p.network for p in registry.all_providers()]
    assert networks == ["facebook", "instagram", "tiktok", "youtube", "demo"]
    assert registry.available_networks(False) == ["facebook", "instagram", "tiktok", "youtube"]
    assert registry.available_networks(True)[-1] == "demo"
    with pytest.raises(KeyError):
        registry.get_provider("myspace")

    class Fake(DemoProvider):
        pass

    fake = registry.register(Fake())
    assert registry.get_provider("demo") is fake
    registry.reset()
    assert type(registry.get_provider("demo")) is DemoProvider


def test_provider_error_detail_is_scrubbed() -> None:
    err = ProviderError(
        ErrorKind.AUTH,
        "token_expired",
        "Reconecta tu cuenta.",
        detail={
            "url": "https://graph.facebook.com/v25.0/me?access_token=EAAB123&fields=id",
            "access_token": "EAAB123",
            "headers": {"Authorization": "Bearer abcdefghijkl"},
            "error": {"code": 190, "message": "Error validating access token"},
            "body": '{"refresh_token": "rt-999", "ok": 1}',
        },
    )
    flat = str(err.detail)
    assert "EAAB123" not in flat and "abcdefghijkl" not in flat and "rt-999" not in flat
    assert err.detail["error"]["code"] == 190  # error codes survive
    assert err.kind == ErrorKind.AUTH and err.to_dict()["kind"] == "auth"
    assert scrub({"code": "abc", "nested": ["x?code=SECRET&y=1"]}) == {
        "code": "abc",
        "nested": ["x?code=***&y=1"],
    }


# ========================================================================================== timeutil


def test_spanish_formatting_without_locale() -> None:
    dt = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)  # Monday 10:30 in Madrid (CET)
    assert format_dt_es(dt, MADRID) == "lunes 2 de noviembre, 10:30"
    assert format_dt_es(dt, MADRID, year=True) == "lunes 2 de noviembre de 2026, 10:30"
    assert format_date_es(date(2026, 3, 29)) == "domingo 29 de marzo"
    assert format_date_es(date(2026, 1, 1), weekday=False, year=True) == "1 de enero de 2026"
    assert format_date_es(datetime(2026, 8, 1, 23, 30, tzinfo=UTC), MADRID) == "domingo 2 de agosto"
    assert format_short_date_es(date(2026, 9, 7)) == "7 sept"
    assert format_month_year_es(date(2026, 2, 1)) == "febrero de 2026"
    assert format_dt_es(None, MADRID) == ""
    days = {format_date_es(date(2026, 11, 2) + timedelta(days=i)).split()[0] for i in range(7)}
    assert days == {"lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"}


def test_relative_and_duration_formatting() -> None:
    now = datetime(2026, 11, 3, 9, tzinfo=UTC)
    assert format_relative_es(now - timedelta(seconds=10), now) == "ahora mismo"
    assert format_relative_es(now - timedelta(minutes=5), now) == "hace 5 min"
    assert format_relative_es(now + timedelta(hours=2), now) == "dentro de 2 h"
    assert format_relative_es(now - timedelta(days=1), now) == "hace 1 día"
    assert format_relative_es(now - timedelta(days=3), now) == "hace 3 días"
    assert format_duration_es(45) == "45 s"
    assert format_duration_es(90) == "1 min 30 s"
    assert format_duration_es(3900) == "1 h 5 min"


def test_wall_time_conversion() -> None:
    summer = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    assert to_brand_local(summer, MADRID).hour == 12
    assert from_brand_local(datetime(2026, 7, 1, 12, 0), MADRID) == summer
    winter = datetime(2026, 12, 1, 10, 0, tzinfo=UTC)
    assert to_brand_local(winter, MADRID).hour == 11
    assert from_brand_local(datetime(2026, 12, 1, 11, 0), MADRID) == winter
    assert from_brand_local(datetime(2026, 12, 1, 11, 0, tzinfo=UTC), MADRID) == datetime(
        2026, 12, 1, 11, 0, tzinfo=UTC
    )
    assert to_brand_local(None, MADRID) is None
    assert from_brand_local(datetime(2026, 12, 1, 11, 0), "Invalid/Zone") == winter  # falls back to Madrid
    assert local_wall_iso(winter, MADRID) == "2026-12-01T11:00:00"


def test_dst_spring_forward_gap_madrid() -> None:
    # 2026-03-29: 02:00 CET jumps to 03:00 CEST; 02:30 does not exist.
    gap = datetime(2026, 3, 29, 2, 30)
    assert wall_time_status(gap, MADRID) == "nonexistent"
    utc = from_brand_local(gap, MADRID)
    assert utc == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert to_brand_local(utc, MADRID).replace(tzinfo=None) == datetime(2026, 3, 29, 3, 30)  # shifted forward
    assert from_brand_local(datetime(2026, 3, 29, 1, 59), MADRID) == datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
    assert from_brand_local(datetime(2026, 3, 29, 3, 0), MADRID) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    start, end = local_day_bounds(date(2026, 3, 29), MADRID)
    assert end - start == timedelta(hours=23)


def test_dst_fall_back_fold_madrid() -> None:
    # 2026-10-25: 03:00 CEST goes back to 02:00 CET; 02:30 happens twice → first occurrence (CEST).
    fold = datetime(2026, 10, 25, 2, 30)
    assert wall_time_status(fold, MADRID) == "ambiguous"
    assert from_brand_local(fold, MADRID) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert wall_time_status(datetime(2026, 10, 25, 12, 0), MADRID) == "ok"
    start, end = local_day_bounds(date(2026, 10, 25), MADRID)
    assert end - start == timedelta(hours=25)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # second 02:30 (CET)
    assert local_wall_iso(second, MADRID) == "2026-10-25T02:30:00"


def test_parse_wall_iso() -> None:
    assert parse_wall_iso("2026-11-03T10:30") == datetime(2026, 11, 3, 10, 30)
    assert parse_wall_iso("2026-11-03T10:30:00Z") == datetime(2026, 11, 3, 10, 30)
    assert parse_wall_iso("2026-11-03T10:30:00.000Z") == datetime(2026, 11, 3, 10, 30)
    assert parse_wall_iso("2026-11-03T10:30:00+01:00") == datetime(2026, 11, 3, 10, 30)
    assert parse_wall_iso("2026-11-03 10:30") == datetime(2026, 11, 3, 10, 30)
    assert parse_wall_iso("2026-11-03") == datetime(2026, 11, 3)
    with pytest.raises(ValueError):
        parse_wall_iso("")


def test_clocks() -> None:
    clock = ManualClock(datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo(MADRID)))
    assert clock.now() == datetime(2026, 11, 3, 9, 0, tzinfo=UTC) and clock.now().tzinfo == UTC
    clock.advance(minutes=5)
    assert clock.now() == datetime(2026, 11, 3, 9, 5, tzinfo=UTC)
    clock.advance(timedelta(hours=1))
    clock.set(datetime(2027, 1, 1))
    assert clock.now() == datetime(2027, 1, 1, tzinfo=UTC)
    assert FixedClock(datetime(2026, 1, 1)).now().tzinfo == UTC


# =============================================================================================== CLI


def test_instance_lock_is_exclusive(tmp_path: Path) -> None:
    from planificador.__main__ import InstanceLock

    first = InstanceLock(tmp_path / "instance.lock", tmp_path / "instance.json")
    second = InstanceLock(tmp_path / "instance.lock", tmp_path / "instance.json")
    assert first.acquire({"pid": os.getpid(), "port": 8765, "url": "http://localhost:8765/"})
    assert first.acquire() is True  # re-entrant for the holder
    assert second.acquire({"pid": 1, "port": 1}) is False
    assert second.read_info() == {"pid": os.getpid(), "port": 8765, "url": "http://localhost:8765/"}
    first.release()
    assert not (tmp_path / "instance.json").exists()
    assert second.acquire({"port": 9000})
    assert second.read_info()["port"] == 9000
    second.release()


def test_cli_second_instance_opens_browser(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from planificador import __main__ as cli
    from planificador.paths import get_paths

    paths = get_paths(data_dir).ensure()
    holder = cli.InstanceLock(paths.lock_file, paths.instance_info_file)
    assert holder.acquire({"pid": 1, "port": 9999, "url": "http://localhost:9999/"})
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.delenv("PLANIFICADOR_NO_BROWSER", raising=False)

    def must_not_start(*_a: Any, **_k: Any) -> None:
        raise AssertionError("server must not start when another instance runs")

    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", must_not_start)
    assert cli.main(["--data-dir", str(data_dir)]) == 0
    assert opened == ["http://localhost:9999/"]
    assert cli.main(["--data-dir", str(data_dir), "--no-browser"]) == 0
    assert opened == ["http://localhost:9999/"]
    holder.release()


def test_cli_starts_server_on_loopback(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from planificador import __main__ as cli

    seen: dict[str, Any] = {}

    class FakeServer:
        def __init__(self, config: Any) -> None:
            seen["host"], seen["port"] = config.host, config.port
            self.started = False

        def run(self) -> None:
            lock_info = cli.read_instance_info(data_dir / "instance.json")
            seen["lock_port"] = lock_info["port"] if lock_info else None
            self.started = True

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert cli.main(["--data-dir", str(data_dir), "--port", "8799", "--no-browser"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 8799, "lock_port": 8799}
    assert not (data_dir / "instance.json").exists()  # released on exit


def test_cli_parser_defaults() -> None:
    from planificador.__main__ import app_url, build_parser

    args = build_parser().parse_args([])
    assert args.port is None and args.no_browser is False and args.data_dir is None
    assert app_url(8765) == "http://localhost:8765/"

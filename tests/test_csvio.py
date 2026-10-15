"""CSV import/export service (SPEC §14): parsing, validation preview, import and export."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, time

import pytest

from planificador import config, csvio
from planificador.models import (
    Account,
    AccountStatus,
    MediaAsset,
    MediaKind,
    Network,
    Post,
    PostSource,
    PostTarget,
    TargetStatus,
)

HEADER = ";".join(csvio.TEMPLATE_COLUMNS)


def make_csv(*rows: dict[str, str], delimiter: str = ";", columns=csvio.TEMPLATE_COLUMNS) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(c, "") for c in columns])
    return buf.getvalue().encode("utf-8")


def add_account(db, brand, network, **kw) -> Account:
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=f"ext-{network}",
        name=kw.pop("name", f"Cuenta {network}"),
        status=kw.pop("status", AccountStatus.CONNECTED),
        **kw,
    )
    account.set_tokens("tok")
    db.add(account)
    db.commit()
    return account


def add_media(db, brand, name, kind="image", sha=None, brand_id="same", **kw) -> MediaAsset:
    sha = sha or (name.encode().hex() * 8)[:64].ljust(64, "0")
    defaults = {
        "image": {"mime": "image/jpeg", "width": 1080, "height": 1080, "size_bytes": 200_000},
        "video": {
            "mime": "video/mp4",
            "width": 1080,
            "height": 1920,
            "duration_s": 12.0,
            "fps": 30.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "bitrate": 4_000_000,
            "size_bytes": 6_000_000,
        },
    }[kind]
    defaults.update(kw)
    asset = MediaAsset(
        brand_id=brand.id if brand_id == "same" else brand_id,
        kind=MediaKind(kind),
        original_name=name,
        rel_path=f"originals/{sha[:2]}/{sha}.{'jpg' if kind == 'image' else 'mp4'}",
        sha256=sha,
        **defaults,
    )
    db.add(asset)
    db.commit()
    return asset


@pytest.fixture
def fb(db, brand):
    return add_account(db, brand, Network.FACEBOOK, account_type="page")


@pytest.fixture
def photo(db, brand):
    return add_media(db, brand, "Foto-Otoño.jpg")


def row(**kw) -> dict[str, str]:
    base = {"fecha": "2026-11-10", "hora": "10:30", "texto": "Hola"}
    base.update(kw)
    return base


# ----------------------------------------------------------------------------------------- parsing


def test_template_has_bom_semicolon_header_and_examples(brand, clock):
    raw = csvio.template_csv(brand, clock)
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    lines = text.split("\r\n")
    assert lines[0] == HEADER
    examples = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    assert len(examples) == 2
    assert all(date.fromisoformat(e["fecha"]) > clock.now().date() for e in examples)
    assert examples[1]["ninos_youtube"] == "no"


def test_decode_utf8_bom_and_windows_1252():
    assert csvio.decode_csv("﻿fecha;café".encode()) == ("fecha;café", "UTF-8")
    text, enc = csvio.decode_csv("fecha;café".encode("cp1252"))
    assert text == "fecha;café" and enc == "Windows-1252"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fecha;hora;texto\n", ";"),
        ("fecha,hora,texto\n", ","),
        ("fecha\thora\ttexto\n", "\t"),
        ('"a;b",hora,texto\n', ","),
        ("\n\nfecha;hora\n", ";"),
        ("fecha\n", ";"),
    ],
)
def test_detect_delimiter(text, expected):
    assert csvio.detect_delimiter(text) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("﻿FECHA", "fecha"),
        (" Título YouTube ", "titulo_youtube"),
        ("Niños_YouTube", "ninos_youtube"),
        ("Contenido", "texto"),
        ("formato-instagram", "formato_instagram"),
        ("Primer comentario", "primer_comentario"),
    ],
)
def test_normalize_header(raw, expected):
    assert csvio.normalize_header(raw) == expected


def test_parse_bool():
    for word in ("si", "Sí", "SI", "x", "1", "true", "Yes"):
        assert csvio.parse_bool(word) is True
    for word in ("no", "No", "0", "false", "n"):
        assert csvio.parse_bool(word) is False
    assert csvio.parse_bool("  ") is None
    with pytest.raises(ValueError):
        csvio.parse_bool("quizás")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-03", date(2026, 11, 3)),
        ("2026/11/3", date(2026, 11, 3)),
        ("03/11/2026", date(2026, 11, 3)),
        ("3-11-2026", date(2026, 11, 3)),
        ("03.11.26", date(2026, 11, 3)),
        ("2026-11-03 00:00:00", date(2026, 11, 3)),
    ],
)
def test_parse_date(raw, expected):
    assert csvio.parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "mañana", "2026-13-01", "31/02/2026", "11/2026"])
def test_parse_date_invalid(raw):
    with pytest.raises(ValueError):
        csvio.parse_date(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10:30", time(10, 30)),
        ("9:05", time(9, 5)),
        ("18:00:00", time(18)),
        ("9.30", time(9, 30)),
        ("17h", time(17)),
    ],
)
def test_parse_time(raw, expected):
    assert csvio.parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["25:00", "10:75", "diez", ""])
def test_parse_time_invalid(raw):
    with pytest.raises(ValueError):
        csvio.parse_time(raw)


# ----------------------------------------------------------------------------------------- preview


def test_valid_facebook_row(db, brand, clock, fb, photo):
    preview = csvio.build_preview(
        db, brand, make_csv(row(facebook="si", medios="foto-otoño.JPG", texto="Línea 1\nLínea 2")), clock
    )
    assert preview.file_errors == [] and preview.delimiter == ";"
    [r] = preview.data_rows
    assert r.errors == [], r.issues
    assert r.scheduled_at == datetime(2026, 11, 10, 9, 30, tzinfo=UTC)  # 10:30 Europe/Madrid (CET)
    assert r.networks == ["facebook"] and r.formats == {"facebook": "post"}
    assert [m.id for m in r.media] == [photo.id]
    assert r.data.source == "csv" and r.data.targets[0].account_id == fb.id
    assert r.action == "schedule" and preview.ok_count == 1 and preview.draft_count == 0


def test_comma_separated_file_with_spanish_headers(db, brand, clock, fb):
    raw = 'Fecha,Hora,Texto,Facebook\n10/11/2026,10:30,"Hola, mundo",Sí\n'.encode()
    preview = csvio.build_preview(db, brand, raw, clock)
    [r] = preview.data_rows
    assert preview.delimiter == "," and r.text == "Hola, mundo" and r.errors == []


def test_date_and_time_errors_are_reported_once(db, brand, clock, fb):
    preview = csvio.build_preview(
        db,
        brand,
        make_csv(
            row(facebook="si", fecha="", hora=""),
            row(facebook="si", hora=""),
            row(facebook="si", fecha="32/01/2026"),
            row(facebook="si", hora="25:99"),
            row(facebook="si", fecha="2026-11-01"),  # in the past (clock: 2026-11-03)
        ),
        clock,
    )
    messages = [[i.message for i in r.errors] for r in preview.data_rows]
    assert messages[0] == ["Falta la fecha."]
    assert messages[1] == ["Falta la hora."]
    assert preview.data_rows[1].day == date(2026, 11, 10)
    assert messages[2][0].startswith("Fecha no válida")
    assert messages[3][0].startswith("Hora no válida")
    assert messages[4] == ["La fecha de publicación está en el pasado."]
    assert all(len(m) == 1 for m in messages)


def test_network_without_account_and_invalid_values(db, brand, clock, fb):
    preview = csvio.build_preview(
        db,
        brand,
        make_csv(
            row(facebook="si", tiktok="si"),
            row(facebook="quizás"),
            row(facebook="si", formato_facebook="carrusel-raro"),
        ),
        clock,
    )
    r1, r2, r3 = preview.data_rows
    assert any("no tiene una cuenta de TikTok" in i.message and i.network == "tiktok" for i in r1.errors)
    assert r1.networks == ["facebook", "tiktok"] and len(r1.data.targets) == 1
    assert any("Valor no válido en «facebook»" in i.message for i in r2.errors)
    assert any("Selecciona al menos una red social" in i.message for i in r2.errors)
    assert any("Formato de Facebook no válido" in i.message for i in r3.errors)
    assert r3.formats["facebook"] == "post"


def test_formats_by_spanish_label(db, brand, clock):
    add_account(db, brand, Network.INSTAGRAM, account_type="business")
    preview = csvio.build_preview(
        db,
        brand,
        make_csv(
            row(instagram="si", formato_instagram="Historia"),
            row(instagram="si", formato_instagram="REEL"),
            row(instagram="si", formato_instagram="carrusel"),
        ),
        clock,
    )
    assert [r.formats["instagram"] for r in preview.data_rows] == ["story", "reel", "post"]


def test_media_lookup(db, brand, clock, fb, photo):
    other = add_media(db, brand, "logo.png", sha="c" * 64)
    shared = add_media(db, brand, "compartida.jpg", sha="d" * 64, brand_id=None)
    add_media(db, brand, "dup.jpg", sha="e" * 64)
    newest = add_media(db, brand, "dup.jpg", sha="f" * 64)
    preview = csvio.build_preview(
        db,
        brand,
        make_csv(
            row(facebook="si", medios="logo | compartida.jpg"),
            row(facebook="si", medios="no-existe.jpg"),
            row(facebook="si", medios="dup.jpg"),
        ),
        clock,
    )
    r1, r2, r3 = preview.data_rows
    assert [m.id for m in r1.media] == [other.id, shared.id]
    assert any("No se encuentra «no-existe.jpg»" in i.message for i in r2.errors)
    assert [m.id for m in r3.media] == [newest.id]
    assert any("Hay 2 archivos llamados «dup.jpg»" in i.message for i in r3.warnings)


def test_deleted_or_foreign_media_is_not_found(db, brand, clock, fb):
    from planificador.models import Brand

    other_brand = Brand(name="Otra")
    db.add(other_brand)
    db.commit()
    add_media(db, brand, "ajena.jpg", sha="a" * 64, brand_id=other_brand.id)
    gone = add_media(db, brand, "borrada.jpg", sha="b" * 64)
    gone.deleted_at = datetime(2026, 1, 1, tzinfo=UTC)
    db.commit()
    preview = csvio.build_preview(
        db, brand, make_csv(row(facebook="si", medios="ajena.jpg|borrada.jpg")), clock
    )
    assert len([i for i in preview.data_rows[0].errors if "No se encuentra" in i.message]) == 2


def test_tiktok_and_youtube_options(db, brand, clock):
    add_account(
        db,
        brand,
        Network.TIKTOK,
        account_type="creator",
        meta={"creator_info": {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 600}},
    )
    add_account(db, brand, Network.YOUTUBE, account_type="channel")
    add_media(db, brand, "clip.mp4", kind="video", sha="9" * 64)
    preview = csvio.build_preview(
        db,
        brand,
        make_csv(
            row(
                tiktok="si",
                youtube="si",
                medios="clip.mp4",
                formato_youtube="short",
                titulo_youtube="Mi short",
                privacidad_youtube="oculto",
                ninos_youtube="no",
                primer_comentario="¡Gracias!",
            ),
            row(tiktok="si", medios="clip.mp4", privacidad_tiktok="Seguidores"),
            row(youtube="si", medios="clip.mp4", privacidad_youtube="secreto"),
        ),
        clock,
    )
    r1, r2, r3 = preview.data_rows
    tt, yt = r1.data.targets
    assert tt.options == {"mode": "direct", "privacy_level": "SELF_ONLY"}  # single creator option
    assert yt.format == "short"
    assert yt.options["title"] == "Mi short" and yt.options["privacy_status"] == "unlisted"
    assert yt.options["made_for_kids"] is False and yt.options["first_comment"] == "¡Gracias!"
    assert yt.auto_publish is False  # YouTube defaults to assisted publishing until unlocked
    assert tt.auto_publish is True
    assert r1.tiktok_direct and preview.has_tiktok_direct
    assert r2.data.targets[0].options["privacy_level"] == "FOLLOWER_OF_CREATOR"
    assert any("no está disponible para esta cuenta" in i.message for i in r2.errors)
    assert any("Privacidad de YouTube no válida" in i.message for i in r3.errors)
    assert any("contenido creado para niños" in i.message for i in r3.errors)


def test_consent_is_applied_on_import_preview(db, brand, clock):
    add_account(db, brand, Network.TIKTOK, account_type="creator")
    raw = make_csv(row(tiktok="si", privacidad_tiktok="solo yo"))
    assert "consent" not in csvio.build_preview(db, brand, raw, clock).data_rows[0].data.targets[0].options
    refused = csvio.build_preview(db, brand, raw, clock, tiktok_consent=False).data_rows[0]
    assert refused.data.targets[0].options["consent"] is False
    assert any("Confirmación de uso de música" in i.message for i in refused.errors)


def test_first_comment_and_autopublish(db, brand, clock, fb):
    add_account(db, brand, Network.INSTAGRAM, account_type="business")
    preview = csvio.build_preview(
        db,
        brand,
        make_csv(
            row(facebook="si", instagram="si", primer_comentario="#hola", autopublicar="no"),
            row(facebook="si", primer_comentario="#hola"),
            row(instagram="si", formato_instagram="historia", primer_comentario="#hola"),
        ),
        clock,
    )
    r1, r2, r3 = preview.data_rows
    fb_t, ig_t = r1.data.targets
    assert ig_t.options["first_comment"] == "#hola" and "first_comment" not in fb_t.options
    assert fb_t.auto_publish is False and ig_t.auto_publish is False
    assert any("solo se usa en Instagram y YouTube" in i.message for i in r2.warnings)
    assert any("historias de Instagram no admiten primer comentario" in i.message for i in r3.warnings)


def test_file_level_problems(db, brand, clock):
    assert csvio.build_preview(db, brand, b"", clock).file_errors == ["El archivo está vacío."]
    assert (
        "No se reconocen las columnas" in csvio.build_preview(db, brand, b"a;b\n1;2\n", clock).file_errors[0]
    )
    only_header = csvio.build_preview(db, brand, make_csv(row(fecha="", hora="", texto="")), clock)
    assert only_header.file_errors == ["El archivo no tiene filas con publicaciones."]
    assert only_header.empty_count == 1
    big = csvio.build_preview(db, brand, b"x" * (csvio.MAX_BYTES + 1), clock)
    assert "demasiado grande" in big.file_errors[0]
    many = csvio.build_preview(db, brand, make_csv(*[row()] * (csvio.MAX_ROWS + 1)), clock)
    assert "Demasiadas filas" in many.file_errors[0]


def test_notices_for_missing_and_unknown_columns(db, brand, clock, fb):
    raw = make_csv({"texto": "Hola", "facebook": "si", "extra": "?"}, columns=("texto", "facebook", "extra"))
    preview = csvio.build_preview(db, brand, raw, clock)
    assert "Faltan las columnas: fecha, hora." in preview.notices
    assert "Se ignoran las columnas: extra." in preview.notices


def test_export_columns_are_not_reported_as_unknown(db, brand, clock, fb):
    columns = csvio.TEMPLATE_COLUMNS + csvio.EXPORT_EXTRA_COLUMNS
    raw = make_csv(dict(row(facebook="si"), estado="Programado", id="7"), columns=columns)
    assert csvio.build_preview(db, brand, raw, clock).notices == []


def test_formula_prefix_is_removed_on_import(db, brand, clock, fb):
    preview = csvio.build_preview(db, brand, make_csv(row(facebook="si", texto="'=SUMA(1)")), clock)
    assert preview.data_rows[0].text == "=SUMA(1)"


# ------------------------------------------------------------------------------------------ import


def test_import_creates_scheduled_and_draft_posts(db, brand, clock, fb, photo):
    raw = make_csv(
        row(facebook="si", medios="Foto-Otoño.jpg", texto="Programada"),
        row(facebook="si", hora="", texto="Borrador"),
        row(fecha="", hora="", texto=""),
        row(facebook="si", texto="No marcada"),
    )
    result = csvio.import_rows(db, brand, raw, clock, lines={2, 3})
    assert result.created == 2 and result.scheduled == 1 and result.drafts == 1 and result.failed == []
    posts = {p.text: p for p in db.query(Post).all()}
    assert set(posts) == {"Programada", "Borrador"}
    scheduled = posts["Programada"]
    assert scheduled.source == PostSource.CSV
    assert [t.status for t in scheduled.targets] == [TargetStatus.SCHEDULED]
    assert scheduled.scheduled_at == datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
    assert [m.id for m in scheduled.media] == [photo.id]
    draft = posts["Borrador"]
    assert draft.scheduled_at is None and [t.status for t in draft.targets] == [TargetStatus.DRAFT]


def test_import_all_lines_and_file_errors(db, brand, clock, fb):
    result = csvio.import_rows(db, brand, make_csv(row(facebook="si"), row(facebook="si")), clock)
    assert result.scheduled == 2
    bad = csvio.import_rows(db, brand, b"", clock)
    assert bad.created == 0 and bad.failed == [(0, "El archivo está vacío.")]


def test_import_row_without_networks_becomes_a_draft(db, brand, clock):
    result = csvio.import_rows(db, brand, make_csv(row(texto="Idea suelta")), clock)
    assert result.drafts == 1
    post = db.query(Post).one()
    assert post.targets == [] and post.source == "csv"


# ------------------------------------------------------------------------------------------ export


def _post(db, brand, account, text, at, *, fmt="post", options=None, status=TargetStatus.SCHEDULED, media=()):
    post = Post(brand_id=brand.id, text=text, scheduled_at=at)
    post.set_media(list(media))
    db.add(post)
    db.flush()
    db.add(
        PostTarget(
            post_id=post.id,
            account_id=account.id,
            network=account.network,
            format=fmt,
            options=options or {},
            status=status,
            scheduled_at=at,
        )
    )
    db.commit()
    return post


def test_export_roundtrip(db, brand, clock, fb, photo):
    yt = add_account(db, brand, Network.YOUTUBE, account_type="channel")
    tt = add_account(db, brand, Network.TIKTOK, account_type="creator")
    p1 = _post(db, brand, fb, "=Oferta del día", datetime(2026, 11, 10, 9, 30, tzinfo=UTC), media=[photo])
    _post(
        db,
        brand,
        yt,
        "Vídeo",
        datetime(2026, 11, 12, 17, 0, tzinfo=UTC),
        fmt="short",
        options={"title": "Corto", "privacy_status": "unlisted", "made_for_kids": False},
    )
    _post(
        db,
        brand,
        tt,
        "Tiktok",
        datetime(2026, 11, 13, 17, 0, tzinfo=UTC),
        fmt="video",
        options={"privacy_level": "SELF_ONLY"},
    )
    _post(db, brand, fb, "Fuera de rango", datetime(2027, 3, 1, 9, 0, tzinfo=UTC))
    deleted = _post(db, brand, fb, "Borrada", datetime(2026, 11, 11, 9, 0, tzinfo=UTC))
    deleted.deleted_at = datetime(2026, 11, 2, tzinfo=UTC)
    db.commit()

    raw = csvio.export_csv(db, brand, date(2026, 11, 1), date(2026, 11, 30))
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert [r["texto"] for r in rows] == ["'=Oferta del día", "Vídeo", "Tiktok"]
    first = rows[0]
    assert (first["fecha"], first["hora"], first["facebook"], first["instagram"]) == (
        "2026-11-10",
        "10:30",
        "si",
        "no",
    )
    assert (
        first["medios"] == "Foto-Otoño.jpg" and first["estado"] == "Programado" and first["id"] == str(p1.id)
    )
    assert (rows[1]["formato_youtube"], rows[1]["titulo_youtube"], rows[1]["privacidad_youtube"]) == (
        "short",
        "Corto",
        "oculto",
    )
    assert rows[1]["ninos_youtube"] == "no"
    assert rows[2]["privacidad_tiktok"] == "Solo yo"

    preview = csvio.build_preview(db, brand, raw, clock)
    assert preview.notices == []
    again = preview.data_rows[0]
    assert again.text == "=Oferta del día" and again.scheduled_at == p1.scheduled_at
    assert [m.id for m in again.media] == [photo.id]
    assert preview.data_rows[2].data.targets[0].options["privacy_level"] == "SELF_ONLY"
    assert preview.data_rows[1].data.targets[0].options["privacy_status"] == "unlisted"


def test_export_filename(brand):
    assert csvio.export_filename(brand, date(2026, 1, 1), date(2026, 1, 31)) == (
        "publicaciones-mi-marca-2026-01-01-2026-01-31.csv"
    )


def test_optional_demo_column_only_in_demo_mode(db, brand, clock, demo_account):
    """With demo mode on, a ``demo`` column (not in the template) targets the brand's Demo account, so the
    import can be tried without real accounts. With demo mode off the column is ignored (and reported)."""
    columns = (*csvio.TEMPLATE_COLUMNS, "demo")
    raw = make_csv(row(demo="si", texto="Prueba demo"), columns=columns)
    off = csvio.build_preview(db, brand, raw, clock)
    assert any("demo" in n for n in off.notices) and off.data_rows[0].networks == []

    config.set_setting(db, "demo_mode", True)
    on = csvio.build_preview(db, brand, raw, clock)
    assert not any("demo" in n for n in on.notices)
    preview_row = on.data_rows[0]
    assert preview_row.networks == ["demo"] and preview_row.formats == {"demo": "post"}
    assert not [i for i in preview_row.issues if i.level == "error"]
    result = csvio.import_rows(db, brand, raw, clock)
    assert result.scheduled == 1
    post = db.query(Post).one()
    assert [(t.account_id, t.format, t.status) for t in post.targets] == [
        (demo_account.id, "post", TargetStatus.SCHEDULED)
    ]

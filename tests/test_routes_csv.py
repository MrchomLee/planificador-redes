"""Routes of CSV import/export (/importar, /exportar.csv)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from planificador import csvio
from planificador.models import Account, AccountStatus, Network, Post, PostTarget, TargetStatus

CSV_ROWS = (
    "fecha;hora;texto;facebook\n"
    "2026-11-10;10:30;Primera;si\n"
    "2026-11-11;;Sin hora;si\n"
    ";;;\n"
    "2026-11-12;12:00;Tercera;si\n"
)


@pytest.fixture
def fb(db, brand):
    account = Account(
        brand_id=brand.id,
        network=Network.FACEBOOK,
        external_id="page",
        name="Página",
        account_type="page",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens("tok")
    db.add(account)
    db.commit()
    return account


def upload(csrf_client, content: str | bytes, name: str = "mis-posts.csv"):
    data = content.encode("utf-8") if isinstance(content, str) else content
    return csrf_client.post(
        "/importar/revisar",
        data={"csrf_token": csrf_client.token},
        files={"archivo": (name, data, "text/csv")},
        follow_redirects=False,
    )


def upload_dir(app):
    return app.state.settings.paths.tmp_dir / "csv-imports"


def test_page(client, brand):
    response = client.get("/importar")
    assert response.status_code == 200
    html = response.text
    assert "Importar publicaciones" in html and "Exportar publicaciones" in html
    assert 'href="/importar/plantilla.csv"' in html
    assert 'action="/exportar.csv"' in html
    assert "ninos_youtube" in html  # column help
    assert 'name="csrf_token"' in html


def test_result_banner(client, brand):
    html = client.get("/importar?programadas=2&borradores=1&fallidas=0").text
    assert "Se han importado 3 publicaciones" in html and "2 programadas y 1 guardada como borrador" in html
    assert "No se ha importado ninguna" in client.get("/importar?programadas=0&borradores=0").text


def test_template_download(client, brand):
    response = client.get("/importar/plantilla.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="plantilla.csv"' in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf" + ";".join(csvio.TEMPLATE_COLUMNS).encode())


def test_upload_preview_and_confirm(app, csrf_client, db, fb):
    response = upload(csrf_client, CSV_ROWS)
    assert response.status_code == 303
    location = response.headers["location"]
    token = location.rsplit("/", 1)[1]
    assert re.fullmatch(r"/importar/revisar/[A-Za-z0-9_-]{16,64}", location)
    assert (upload_dir(app) / f"{token}.csv").is_file()

    preview = csrf_client.get(location)
    assert preview.status_code == 200
    html = preview.text
    assert "mis-posts.csv" in html and "Revisar importación" in html
    assert html.count('name="filas"') == 3
    assert "Falta la hora." in html and "Fila vacía" in html
    assert "Se programará" in html and "Borrador · 1 error" in html
    assert "Confirmación de uso de música" not in html  # no TikTok rows

    done = csrf_client.post(
        f"{location}/confirmar",
        data={"csrf_token": csrf_client.token, "filas": ["2", "3"]},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/importar?programadas=1&borradores=1&fallidas=0"
    texts = {p.text: p for p in db.query(Post).all()}
    assert set(texts) == {"Primera", "Sin hora"}
    assert all(p.source == "csv" for p in texts.values())
    assert not (upload_dir(app) / f"{token}.csv").exists()  # discarded after importing
    assert csrf_client.get(location).status_code == 404


def test_confirm_without_rows_goes_back(csrf_client, db, fb):
    location = upload(csrf_client, CSV_ROWS).headers["location"]
    response = csrf_client.post(
        f"{location}/confirmar", data={"csrf_token": csrf_client.token}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == location
    assert db.query(Post).count() == 0


def test_tiktok_consent_checkbox_and_value(csrf_client, db, brand):
    account = Account(
        brand_id=brand.id,
        network=Network.TIKTOK,
        external_id="tt",
        name="tt",
        account_type="creator",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens("tok")
    db.add(account)
    db.commit()
    location = upload(
        csrf_client, "fecha;hora;texto;tiktok;privacidad_tiktok\n2026-11-10;10:30;Vídeo;si;solo yo\n"
    ).headers["location"]
    html = csrf_client.get(location).text
    assert 'name="consentimiento_tiktok"' in html
    csrf_client.post(
        f"{location}/confirmar",
        data={"csrf_token": csrf_client.token, "filas": "2", "consentimiento_tiktok": "1"},
        follow_redirects=False,
    )
    target = db.query(PostTarget).one()
    assert target.options["consent"] is True


def test_upload_without_file_or_empty(csrf_client, brand):
    response = csrf_client.post(
        "/importar/revisar", data={"csrf_token": csrf_client.token}, follow_redirects=False
    )
    assert response.status_code in (303, 422)
    empty = upload(csrf_client, "   ")
    assert empty.status_code == 303 and empty.headers["location"] == "/importar"
    page = csrf_client.get("/importar")
    assert "El archivo est\\u00e1 vac\\u00edo." in page.text  # queued toast (flash JSON)


def test_upload_too_large(csrf_client, brand, monkeypatch):
    monkeypatch.setattr(csvio, "MAX_BYTES", 10)
    response = upload(csrf_client, "fecha;hora;texto\n2026-11-10;10:30;Hola\n")
    assert response.status_code == 303 and response.headers["location"] == "/importar"


def test_upload_requires_csrf(client, brand):
    response = client.post(
        "/importar/revisar", files={"archivo": ("a.csv", b"fecha;hora\n", "text/csv")}, follow_redirects=False
    )
    assert response.status_code == 403


def test_preview_of_unrecognised_file(csrf_client, brand):
    location = upload(csrf_client, "nombre;apellido\nAna;Ruiz\n").headers["location"]
    html = csrf_client.get(location).text
    assert "No se puede importar este archivo" in html and "No se reconocen las columnas" in html
    assert 'name="filas"' not in html


@pytest.mark.parametrize("token", ["corto", "../../etc/passwd", "a" * 70])
def test_invalid_tokens(client, brand, token):
    assert client.get(f"/importar/revisar/{token}").status_code == 404


def test_export(client, db, brand, fb):
    post = Post(brand_id=brand.id, text="Exportada", scheduled_at=datetime(2026, 11, 10, 9, 30, tzinfo=UTC))
    db.add(post)
    db.flush()
    db.add(
        PostTarget(
            post_id=post.id,
            account_id=fb.id,
            network=Network.FACEBOOK,
            format="post",
            status=TargetStatus.SCHEDULED,
            scheduled_at=post.scheduled_at,
        )
    )
    db.commit()
    response = client.get("/exportar.csv?desde=2026-11-30&hasta=2026-11-01")  # swapped range is fixed
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "publicaciones-mi-marca-2026-11-01-2026-11-30.csv" in response.headers["content-disposition"]
    text = response.content.decode("utf-8-sig")
    assert "Exportada" in text and text.splitlines()[0].endswith(";estado;enlaces;id")
    default = client.get("/exportar.csv")  # default range: 30 days back, 90 ahead
    assert "Exportada" in default.content.decode("utf-8-sig")
    outside = client.get("/exportar.csv?desde=2027-01-01&hasta=2027-01-31").content.decode("utf-8-sig")
    assert "Exportada" not in outside

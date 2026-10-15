"""Marcas: list, create, edit (name/colour/time zone), archive/restore/delete, switcher integration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config
from planificador.models import Account, AccountStatus, Brand, Post, PostTarget, TargetStatus
from planificador.routes import brands as brands_mod

FLASH_RE = '<script type="application/json" id="flash-data">'


def flashes(html: str) -> list[str]:
    start = html.index(FLASH_RE) + len(FLASH_RE)
    return [f["message"] for f in json.loads(html[start : html.index("</script>", start)])]


def active_id(db: Session) -> int:
    db.expire_all()
    return int(config.get_setting(db, "active_brand_id"))


def new_brand(db: Session, name: str, **kw: Any) -> Brand:
    b = Brand(name=name, color=kw.pop("color", "#2a78d6"), timezone=kw.pop("timezone", "Europe/Madrid"), **kw)
    db.add(b)
    db.commit()
    return b


# ---------------------------------------------------------------------------------------------- list


def test_brands_page(client: TestClient, db: Session, brand: Brand) -> None:
    other = new_brand(db, "Estudio Norte", timezone="America/Mexico_City")
    old = new_brand(db, "Antigua", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
    acc = Account(
        brand_id=other.id,
        network="instagram",
        external_id="i",
        name="I",
        status=AccountStatus.NEEDS_RECONNECT,
    )
    db.add(acc)
    db.commit()
    html = client.get("/marcas").text
    assert "Mi marca" in html and "Estudio Norte" in html and "America/Mexico City" in html
    assert "Activa" in html
    assert f'action="/marcas/{other.id}/activar"' in html  # "Usar esta marca"
    assert "Archivadas" in html and "Antigua" in html and f'action="/marcas/{old.id}/restaurar"' in html
    assert f'action="/marcas/{old.id}/eliminar"' in html  # archived without posts can be deleted
    assert "reconexión necesaria" in html
    # The current brand is listed first.
    assert html.index("Mi marca") < html.index("Estudio Norte")


def test_new_brand_form_modal_and_page(client: TestClient, db: Session, brand: Brand) -> None:
    config.set_setting(db, "default_timezone", "Atlantic/Canary")
    modal = client.get("/marcas/nueva", headers={"HX-Request": "true", "HX-Target": "modal-root"})
    assert modal.status_code == 200 and "<html" not in modal.text and 'class="modal' in modal.text
    assert '<option value="Atlantic/Canary" selected>' in modal.text
    assert "data-tz-filter" in modal.text and 'name="color"' in modal.text
    page = client.get("/marcas/nueva")
    assert page.status_code == 200 and "<html" in page.text and "Crear marca" in page.text


# -------------------------------------------------------------------------------------------- create


def test_create_brand_and_switch(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/marcas",
        data={"name": "  Panadería Lucía ", "color": "#E5A000", "timezone": "Europe/Madrid", "activar": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/conexiones"
    created = db.scalar(select(Brand).where(Brand.name == "Panadería Lucía"))
    assert created is not None and created.color == "#e5a000" and created.timezone == "Europe/Madrid"
    assert active_id(db) == created.id
    html = csrf_client.get("/marcas").text
    assert any("Panadería Lucía" in m for m in flashes(html))
    assert 'class="brand-switcher__name">Panadería Lucía<' in html  # switcher shows the new active brand


def test_create_brand_without_switching_htmx(csrf_client: Any, db: Session, brand: Brand) -> None:
    r = csrf_client.post(
        "/marcas",
        data={"name": "Otra", "color": "#16a34a", "timezone": "America/Bogota"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/marcas"
    assert active_id(db) == brand.id
    assert db.scalar(select(Brand).where(Brand.name == "Otra")).timezone == "America/Bogota"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": "", "color": "#5b4cf0", "timezone": "Europe/Madrid"}, "Escribe un nombre"),
        ({"name": "x" * 121, "color": "#5b4cf0", "timezone": "Europe/Madrid"}, "más de 120"),
        ({"name": "Nueva", "color": "red", "timezone": "Europe/Madrid"}, "color válido"),
        ({"name": "Nueva", "color": "#5b4cf0", "timezone": "Mars/Olympus"}, "zona horaria de la lista"),
        ({"name": "Nueva", "color": "#5b4cf0", "timezone": "localtime"}, "zona horaria de la lista"),
        ({"name": "mi MARCA", "color": "#5b4cf0", "timezone": "Europe/Madrid"}, "Ya tienes una marca"),
    ],
)
def test_create_brand_validation(
    csrf_client: Any, db: Session, brand: Brand, data: dict, message: str
) -> None:
    r = csrf_client.post("/marcas", data=data, headers={"HX-Request": "true"})
    assert r.status_code == 422
    assert message in r.text and 'class="modal' in r.text and "form-field__error" in r.text
    assert len(db.scalars(select(Brand)).all()) == 1
    # Without htmx the full page is re-rendered with the errors.
    r = csrf_client.post("/marcas", data=data)
    assert r.status_code == 422 and message in r.text and "<html" in r.text


def test_create_requires_csrf(client: TestClient, brand: Brand) -> None:
    r = client.post("/marcas", data={"name": "X", "color": "#5b4cf0", "timezone": "Europe/Madrid"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------------------------- edit


def test_edit_brand(csrf_client: Any, db: Session, brand: Brand) -> None:
    form = csrf_client.get(f"/marcas/{brand.id}/editar", headers={"HX-Request": "true"})
    assert form.status_code == 200 and 'value="Mi marca"' in form.text and "Guardar cambios" in form.text
    r = csrf_client.post(
        f"/marcas/{brand.id}",
        data={"name": "Mi marca renombrada", "color": "#e5484d", "timezone": "America/Mexico_City"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/marcas"
    db.expire_all()
    edited = db.get(Brand, brand.id)
    assert (edited.name, edited.color, edited.timezone) == (
        "Mi marca renombrada",
        "#e5484d",
        "America/Mexico_City",
    )
    msgs = flashes(csrf_client.get("/marcas").text)
    assert any("mantienen su hora exacta" in m for m in msgs)
    # Keeping its own name is not a duplicate.
    r = csrf_client.post(
        f"/marcas/{brand.id}",
        data={"name": "Mi marca renombrada", "color": "#e5484d", "timezone": "America/Mexico_City"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert csrf_client.get("/marcas/999/editar").status_code == 404
    assert csrf_client.post("/marcas/999", data={"name": "x"}).status_code == 404


def test_edit_brand_validation(csrf_client: Any, db: Session, brand: Brand) -> None:
    new_brand(db, "Ocupada")
    r = csrf_client.post(
        f"/marcas/{brand.id}",
        data={"name": "ocupada", "color": "#e5484d", "timezone": "Nope/Zone"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422 and "Ya tienes una marca" in r.text and "zona horaria" in r.text
    db.expire_all()
    assert db.get(Brand, brand.id).name == "Mi marca"


# ------------------------------------------------------------------------------------------- archive


def test_cannot_archive_only_brand(csrf_client: Any, db: Session, brand: Brand) -> None:
    csrf_client.post(f"/marcas/{brand.id}/archivar", follow_redirects=False)
    db.expire_all()
    assert db.get(Brand, brand.id).archived_at is None
    assert any("única marca" in m for m in flashes(csrf_client.get("/marcas").text))


def test_archive_active_brand_switches(csrf_client: Any, db: Session, brand: Brand, clock: Any) -> None:
    other = new_brand(db, "Segunda")
    r = csrf_client.post(f"/marcas/{brand.id}/archivar", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Brand, brand.id).archived_at == clock.now()
    assert active_id(db) == other.id
    html = csrf_client.get("/marcas").text
    assert any("Ahora trabajas con «Segunda»" in m for m in flashes(html))
    assert f'action="/marcas/{brand.id}/activar"' not in html  # not offered in the switcher any more
    # Restore it.
    csrf_client.post(f"/marcas/{brand.id}/restaurar", follow_redirects=False)
    db.expire_all()
    assert db.get(Brand, brand.id).archived_at is None


def test_archive_blocked_by_pending_posts(
    csrf_client: Any, db: Session, brand: Brand, demo_account: Account
) -> None:
    new_brand(db, "Segunda")
    post = Post(brand_id=brand.id, text="hola", scheduled_at=datetime(2026, 12, 1, tzinfo=UTC))
    db.add(post)
    db.flush()
    db.add(
        PostTarget(post_id=post.id, account_id=demo_account.id, network="demo", status=TargetStatus.SCHEDULED)
    )
    db.commit()
    csrf_client.post(f"/marcas/{brand.id}/archivar", follow_redirects=False)
    db.expire_all()
    assert db.get(Brand, brand.id).archived_at is None
    html = csrf_client.get("/marcas").text
    assert any("1 publicación pendiente" in m for m in flashes(html))
    assert "no puede tener publicaciones pendientes" in html


def test_restore_with_name_clash(csrf_client: Any, db: Session, brand: Brand) -> None:
    old = new_brand(db, "Mi marca", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
    csrf_client.post(f"/marcas/{old.id}/restaurar", follow_redirects=False)
    db.expire_all()
    restored = db.get(Brand, old.id)
    assert restored.archived_at is None and restored.name == "Mi marca (restaurada)"


def test_delete_archived_brand(csrf_client: Any, db: Session, brand: Brand) -> None:
    other = new_brand(db, "Para borrar")
    acc = Account(
        brand_id=other.id, network="demo", external_id="d", name="D", status=AccountStatus.CONNECTED
    )
    db.add(acc)
    db.commit()
    # Active (not archived) brands cannot be deleted.
    csrf_client.post(f"/marcas/{other.id}/eliminar", follow_redirects=False)
    db.expire_all()
    assert db.get(Brand, other.id) is not None
    other_id = other.id
    csrf_client.post(f"/marcas/{other_id}/archivar", follow_redirects=False)
    csrf_client.post(f"/marcas/{other_id}/eliminar", follow_redirects=False)
    db.expunge_all()
    assert db.scalar(select(Brand).where(Brand.id == other_id)) is None
    assert db.scalar(select(Account).where(Account.external_id == "d")) is None


def test_delete_refused_with_posts(csrf_client: Any, db: Session, brand: Brand) -> None:
    other = new_brand(db, "Con posts", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
    db.add(Post(brand_id=other.id, text="x"))
    db.commit()
    csrf_client.post(f"/marcas/{other.id}/eliminar", follow_redirects=False)
    db.expire_all()
    assert db.get(Brand, other.id) is not None
    assert any("sin publicaciones" in m for m in flashes(csrf_client.get("/marcas").text))


def test_activate_brand_switcher(csrf_client: Any, db: Session, brand: Brand) -> None:
    other = new_brand(db, "Segunda")
    r = csrf_client.post(f"/marcas/{other.id}/activar", data={"next": "/conexiones"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/conexiones"
    assert active_id(db) == other.id
    html = csrf_client.get("/conexiones").text
    assert "«Segunda»" in html


# ---------------------------------------------------------------------------------------- time zones


def test_timezone_helpers() -> None:
    assert brands_mod.is_known_timezone("Europe/Madrid")
    assert brands_mod.is_known_timezone("UTC")
    assert not brands_mod.is_known_timezone("Europe/Atlantis")
    assert not brands_mod.is_known_timezone("")
    assert not brands_mod.is_known_timezone(None)
    winter = datetime(2026, 1, 15, 12, tzinfo=UTC)
    assert brands_mod.utc_offset_label("Asia/Kolkata", winter) == "UTC+05:30"
    assert brands_mod.utc_offset_label("Europe/Madrid", winter) == "UTC+01:00"
    assert brands_mod.utc_offset_label("America/Mexico_City", winter) == "UTC−06:00"
    groups = brands_mod.timezone_groups()
    assert groups[0][0] == "Habituales"
    assert groups[0][1][0][0] == "Europe/Madrid"
    labels = [label for label, _ in groups]
    assert "Europa" in labels and "América" in labels
    all_zones = {z for _, items in groups for z, _ in items}
    assert "America/Argentina/Buenos_Aires" in all_zones and "Etc/GMT+5" not in all_zones
    # A legacy alias still known to zoneinfo stays selectable when it is the current value.
    legacy = brands_mod.timezone_groups("US/Eastern")
    assert legacy[0][1][0][0] == "US/Eastern"

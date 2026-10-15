"""Notification centre, bell summary and read/unread actions (routes/notifications.py)."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from planificador.models import Account, AccountStatus, Network, Notification, Post, PostTarget, TargetStatus
from planificador.routes import notifications as notif_routes

HX = {"HX-Request": "true"}


def _seed(db: Session, brand, clock) -> dict[str, Any]:
    account = Account(
        brand_id=brand.id,
        network=Network.INSTAGRAM,
        external_id="ig-1",
        name="Cafetería",
        username="cafeteria.aurora",
        status=AccountStatus.CONNECTED,
    )
    db.add(account)
    db.flush()
    post = Post(brand_id=brand.id, text="Menú de otoño con calabaza", scheduled_at=clock.now())
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=account.id,
        network=Network.INSTAGRAM,
        status=TargetStatus.MANUAL_PENDING,
        auto_publish=False,
        scheduled_at=clock.now(),
    )
    db.add(target)
    db.flush()
    now = clock.now()
    rows = {
        "old": Notification(
            kind="info", title="Cuenta conectada", body="", created_at=now - timedelta(days=3), read_at=now
        ),
        "reconnect": Notification(
            kind="reconnect",
            title="Reconecta TikTok",
            body="Caducó",
            created_at=now - timedelta(days=1),
            account_id=account.id,
        ),
        "error": Notification(
            kind="error",
            title="Error al publicar",
            body="Detalle <b>x</b>",
            created_at=now - timedelta(minutes=30),
            target_id=target.id,
        ),
        "manual": Notification(
            kind="manual_publish",
            title="Toca publicar en Instagram",
            body="Abre la publicación asistida",
            created_at=now,
            target_id=target.id,
        ),
    }
    db.add_all(rows.values())
    db.commit()
    return {"target": target, "post": post, "account": account, **rows}


def test_page_lists_grouped_with_deep_links(client: TestClient, db: Session, brand, clock) -> None:
    data = _seed(db, brand, clock)
    r = client.get("/notificaciones")
    assert r.status_code == 200
    html = r.text
    assert 'class="topbar"' in html and "Notificaciones" in html
    # Day groups (Europe/Madrid; DEFAULT_NOW is Tuesday 3 Nov 10:00 local).
    assert ">Hoy<" in html and ">Ayer<" in html and "Sábado 31 de octubre" in html
    tid = data["target"].id
    # manual_publish → assisted modal; error → post drawer; reconnect → /conexiones.
    assert f'hx-get="/objetivos/{tid}/asistida" hx-target="#modal-root"' in html
    assert f'hx-get="/objetivos/{tid}" hx-target="#drawer-root"' in html
    assert 'href="/conexiones"' in html and "Reconectar" in html
    assert "Publicar ahora" in html and "Ver publicación" in html
    # Context: account + post snippet + target status; user text escaped.
    assert "@cafeteria.aurora" in html and "«Menú de otoño con calabaza»" in html
    assert "Detalle &lt;b&gt;x&lt;/b&gt;" in html
    # Unread items carry data-notif-read and a mark-as-read button; read ones a mark-as-unread one.
    assert f'data-notif-read="{data["manual"].id}"' in html
    assert f'hx-post="/notificaciones/{data["manual"].id}/leer"' in html
    assert f'hx-post="/notificaciones/{data["old"].id}/no-leida"' in html
    # Tab counts.
    assert re.search(r"Todas</span><span class=\"count-pill\">4<", html)
    assert re.search(r"No leídas</span><span class=\"count-pill\">3<", html)


def test_filters_unread_and_kind(client: TestClient, db: Session, brand, clock) -> None:
    data = _seed(db, brand, clock)
    html = client.get("/notificaciones?filtro=no-leidas").text
    assert "Cuenta conectada" not in html and "Toca publicar en Instagram" in html
    html = client.get("/notificaciones?tipo=errores").text
    assert "Error al publicar" in html and "Toca publicar en Instagram" not in html
    html = client.get("/notificaciones?tipo=asistida&filtro=no-leidas").text
    assert "Toca publicar en Instagram" in html and "Reconecta TikTok" not in html
    # Unknown values fall back to the defaults.
    assert client.get("/notificaciones?filtro=x&tipo=y").status_code == 200
    # htmx reload of the list only.
    frag = client.get("/notificaciones", headers={**HX, "HX-Target": "notif-page-list"}).text
    assert "<html" not in frag.lower() and 'class="nday"' in frag
    assert data["manual"].title in frag


def test_empty_states(client: TestClient) -> None:
    assert "No hay notificaciones" in client.get("/notificaciones").text
    assert "Lo tienes todo al día" in client.get("/notificaciones?filtro=no-leidas").text


def test_pagination_load_more(client: TestClient, db: Session, clock, monkeypatch) -> None:
    monkeypatch.setattr(notif_routes, "PAGE_SIZE", 3)
    now = clock.now()
    db.add_all(
        [
            Notification(kind="info", title=f"Aviso {i}", created_at=now - timedelta(minutes=i))
            for i in range(5)
        ]
    )
    db.commit()
    html = client.get("/notificaciones").text
    assert "Aviso 0" in html and "Aviso 2" in html and "Aviso 3" not in html
    match = re.search(r'hx-get="(/notificaciones\?filtro=todas&amp;tipo=&amp;antes=\d+)"', html)
    assert match
    more = client.get(match.group(1).replace("&amp;", "&"), headers={**HX, "HX-Target": "notif-more"}).text
    assert "Aviso 3" in more and "Aviso 4" in more and "Aviso 0" not in more
    assert "Cargar más" not in more and "No hay notificaciones" not in more


def test_mark_read_unread(csrf_client, db: Session, brand, clock) -> None:
    data = _seed(db, brand, clock)
    nid = data["manual"].id
    r = csrf_client.post(f"/notificaciones/{nid}/leer", headers=HX)
    assert r.status_code == 200
    assert r.headers["HX-Trigger"] == "notifications:refresh"
    assert f'id="notif-{nid}"' in r.text and "nitem is-unread" not in r.text
    db.expire_all()
    assert db.get(Notification, nid).read_at == clock.now()

    r = csrf_client.post(f"/notificaciones/{nid}/no-leida", headers=HX)
    assert r.status_code == 200 and "nitem is-unread" in r.text
    db.expire_all()
    assert db.get(Notification, nid).read_at is None

    # JSON (app.js fire-and-forget from the bell / browser notifications).
    r = csrf_client.post(f"/notificaciones/{nid}/leer", headers={"Accept": "application/json"})
    assert r.json() == {"ok": True, "id": nid, "read": True}
    # Plain form → back to the referring page.
    r = csrf_client.post(
        f"/notificaciones/{nid}/no-leida",
        headers={"Referer": "http://localhost:8765/notificaciones?filtro=todas"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/notificaciones?filtro=todas"
    # A foreign Referer is refused by the security middleware; without one we go back to the centre.
    r = csrf_client.post(
        f"/notificaciones/{nid}/leer", headers={"Referer": "https://evil.example/x"}, follow_redirects=False
    )
    assert r.status_code == 403
    r = csrf_client.post(f"/notificaciones/{nid}/leer", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notificaciones"
    assert csrf_client.post("/notificaciones/999/leer", headers=HX).status_code == 404


def test_mark_all_read(csrf_client, client: TestClient, db: Session, brand, clock) -> None:
    _seed(db, brand, clock)
    r = csrf_client.post("/notificaciones/leer-todas", headers=HX)
    assert r.status_code == 200
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["notifications:refresh"] and trigger["notifications:changed"]
    assert trigger["toast"]["message"] == "3 notificaciones marcadas como leídas."
    assert notif_routes.unread_count(db) == 0
    r = csrf_client.post("/notificaciones/leer-todas", headers=HX)
    assert json.loads(r.headers["HX-Trigger"])["toast"]["message"] == "No había notificaciones sin leer."
    r = csrf_client.post("/notificaciones/leer-todas", follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/notificaciones/leer-todas", headers=HX).status_code == 403  # CSRF


def test_bell_summary_deep_links_and_payload(client: TestClient, db: Session, brand, clock) -> None:
    data = _seed(db, brand, clock)
    r = client.get("/api/notificaciones/resumen", headers=HX)
    assert r.status_code == 200
    html = r.text
    tid = data["target"].id
    assert '<span class="notif__badge" aria-hidden="true">3</span>' in html
    assert 'data-unread="3"' in html
    listing = html[html.index('id="notif-list"') :]
    assert listing.count("notif-item is-unread") == 3
    assert f'hx-get="/objetivos/{tid}/asistida" hx-target="#modal-root"' in listing
    assert f'hx-get="/objetivos/{tid}" hx-target="#drawer-root"' in listing
    assert f'data-notif-read="{data["manual"].id}"' in listing
    assert 'href="/conexiones"' in listing
    # Network of the target shown on the bubble.
    assert "notif-item__net" in listing
    payload = json.loads(re.search(r"data-notify-items='([^']*)'", html).group(1).replace("&#34;", '"'))
    urls = {item["kind"]: item["url"] for item in payload}
    assert urls["manual_publish"] == f"/objetivos/{tid}/asistida"
    assert urls["error"] == f"/objetivos/{tid}"
    assert urls["reconnect"] == "/conexiones"
    assert urls["info"] == "/notificaciones"


def test_bell_markup_in_topbar(client: TestClient) -> None:
    html = client.get("/planificacion").text
    assert 'hx-post="/notificaciones/leer-todas"' in html
    assert "data-notify-enable" in html
    assert 'href="/notificaciones"' in html

"""Routes of the comments inbox (/comentarios)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest

from planificador import config, inbox
from planificador.models import Account, AccountStatus, Brand, InboxComment, Network

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
HX = {"HX-Request": "true"}


def add_comment(db, account, ext, text, minutes_ago, *, parent=None, author="Lucía", from_us=False, obj="p1"):
    row = InboxComment(
        account_id=account.id,
        external_id=ext,
        parent_external_id=parent,
        object_external_id=obj,
        object_permalink=f"https://www.instagram.com/p/{obj}/",
        author_name=author,
        author_id=f"id-{author}",
        text=text,
        created_time=NOW - timedelta(minutes=minutes_ago),
        from_us=from_us,
        read_at=NOW if from_us else None,
        raw={},
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def demo_thread(db, demo_mode, demo_account):
    root = add_comment(db, demo_account, "c1", "¿Tenéis envíos a Canarias?", 60)
    add_comment(db, demo_account, "c2", "Yo también lo pregunto", 30, parent="c1", author="Pedro")
    add_comment(db, demo_account, "c3", "Me encanta", 90, author="Marta", obj="p2")
    return root


@pytest.fixture
def ig_account(db, brand):
    account = Account(
        brand_id=brand.id,
        network=Network.INSTAGRAM,
        external_id="ig1",
        name="Mi cuenta",
        username="micuenta",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens("tok")
    db.add(account)
    db.commit()
    return account


def item_ids(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'id="inbox-item-(\d+)"', html)]


def test_page_without_accounts(client, brand):
    response = client.get("/comentarios")
    assert response.status_code == 200
    assert "Conecta tus redes para ver los comentarios" in response.text
    assert "Selecciona una conversación a la izquierda" in response.text
    assert 'href="/conexiones"' in response.text


def test_page_lists_conversations(client, demo_thread):
    response = client.get("/comentarios")
    assert response.status_code == 200
    html = response.text
    assert "Lucía" in html and "Marta" in html
    assert "Pedro:" in html  # latest comment of the thread is a reply by someone else
    assert len(item_ids(html)) == 2
    assert 'id="inbox-count-sin_responder"' in html
    assert "TikTok" in html  # the disabled TikTok filter logo carries the explanation


def test_page_with_selected_thread_marks_it_read(client, db, demo_thread):
    response = client.get(f"/comentarios?c={demo_thread.id}")
    assert response.status_code == 200
    assert 'class="ib-thread"' in response.text
    assert "Yo también lo pregunto" in response.text
    assert "Responder" in response.text
    db.expire_all()
    assert all(
        r.read_at is not None
        for r in db.query(InboxComment).filter(InboxComment.external_id.in_(["c1", "c2"]))
    )


def test_thread_fragment_with_oob_item_and_counts(client, demo_thread):
    response = client.get(f"/comentarios/{demo_thread.id}", headers=HX)
    assert response.status_code == 200
    html = response.text
    assert 'class="ib-thread"' in html
    assert f'id="inbox-item-{demo_thread.id}"' in html and 'hx-swap-oob="outerHTML"' in html
    assert 'id="inbox-count-no_leidos" hx-swap-oob="true"' in html


def test_thread_without_htmx_redirects(client, demo_thread):
    response = client.get(f"/comentarios/{demo_thread.id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/comentarios?c={demo_thread.id}"


def test_thread_of_another_brand_is_404(client, db, demo_thread):
    other = Brand(name="Otra")
    db.add(other)
    db.flush()
    account = Account(brand_id=other.id, network=Network.DEMO, external_id="x", name="x")
    db.add(account)
    db.commit()
    row = add_comment(db, account, "z1", "secreto", 10)
    assert client.get(f"/comentarios/{row.id}", headers=HX).status_code == 404
    assert client.get(f"/comentarios?c={row.id}").status_code == 200  # ignored on the full page


def test_list_fragments_tabs_and_search(client, db, demo_thread):
    panel = client.get("/comentarios/lista?tab=todos", headers=HX)
    assert panel.status_code == 200 and 'id="inbox-list-panel"' in panel.text
    body = client.get("/comentarios/lista?parte=cuerpo&tab=todos&q=canarias", headers=HX)
    assert body.status_code == 200
    assert 'id="inbox-list-panel"' not in body.text
    assert item_ids(body.text) == [demo_thread.id]
    assert 'id="inbox-count-sin_responder" hx-swap-oob="true"' in body.text
    short = client.get("/comentarios/lista?parte=cuerpo&tab=todos&q=ca", headers=HX)
    assert len(item_ids(short.text)) == 2 and "Escribe al menos 3 caracteres" in short.text
    none = client.get("/comentarios/lista?parte=cuerpo&tab=todos&q=xyzxyz", headers=HX)
    assert item_ids(none.text) == [] and "Sin resultados" in none.text
    redirect = client.get("/comentarios/lista?tab=todos", follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == "/comentarios?tab=todos"


def test_unread_tab(client, db, demo_thread):
    client.get(f"/comentarios/{demo_thread.id}", headers=HX)
    body = client.get("/comentarios/lista?parte=cuerpo&tab=no_leidos", headers=HX)
    assert len(item_ids(body.text)) == 1


def test_network_filter_and_reconnect_badge(client, db, demo_thread, ig_account):
    add_comment(db, ig_account, "i1", "Comentario de Instagram", 5, author="ana")
    html = client.get("/comentarios?red=instagram&tab=todos").text
    assert len(item_ids(html)) == 1 and "Comentario de Instagram" in html
    assert 'class="ib-net__alert"' not in html
    ig_account.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    html = client.get("/comentarios").text
    assert 'class="ib-net__alert"' in html
    # Replying is blocked while the account needs reconnecting.
    thread_html = client.get(
        f"/comentarios?c={item_ids(client.get('/comentarios?red=instagram&tab=todos').text)[0]}"
    ).text
    assert "Reconecta la cuenta" in thread_html


def test_tiktok_note_when_brand_has_tiktok(client, db, brand):
    db.add(Account(brand_id=brand.id, network=Network.TIKTOK, external_id="tt", name="tt"))
    db.commit()
    html = client.get("/comentarios").text
    assert inbox.TIKTOK_NOTE in html
    assert 'class="ib-list-foot"' in html


def test_reply_flow(csrf_client, db, demo_thread):
    response = csrf_client.post(
        f"/comentarios/{demo_thread.id}/responder", data={"texto": "¡Sí, enviamos!"}, headers=HX
    )
    assert response.status_code == 200
    assert "inbox:changed" in response.headers["HX-Trigger"]
    assert "Respuesta publicada en Demo" in response.headers["HX-Trigger"]
    assert "¡Sí, enviamos!" in response.text
    db.expire_all()
    ours = db.query(InboxComment).filter(InboxComment.from_us.is_(True)).one()
    assert ours.external_id.startswith("demo-reply-") and ours.parent_external_id == "c1"


def test_reply_to_a_specific_comment(csrf_client, db, demo_thread):
    pedro = db.query(InboxComment).filter_by(external_id="c2").one()
    response = csrf_client.post(
        f"/comentarios/{demo_thread.id}/responder",
        data={"texto": "Hola Pedro", "responder_a": str(pedro.id)},
        headers=HX,
    )
    assert response.status_code == 200
    db.expire_all()
    ours = db.query(InboxComment).filter(InboxComment.from_us.is_(True)).one()
    assert ours.raw["_app"]["in_reply_to"] == "c2"


def test_reply_errors_return_message_header(csrf_client, demo_thread):
    response = csrf_client.post(f"/comentarios/{demo_thread.id}/responder", data={"texto": "  "}, headers=HX)
    assert response.status_code == 400
    assert unquote(response.headers["X-Error-Message"]) == "Escribe una respuesta antes de enviarla."


def test_reply_requires_csrf(client, demo_thread):
    response = client.post(f"/comentarios/{demo_thread.id}/responder", data={"texto": "Hola"}, headers=HX)
    assert response.status_code == 403


def test_resolve_and_reopen(csrf_client, db, demo_thread):
    response = csrf_client.post(f"/comentarios/{demo_thread.id}/resolver", data={"resuelto": "1"}, headers=HX)
    assert response.status_code == 200 and "Reabrir" in response.text
    assert "inbox:changed" in response.headers["HX-Trigger"]
    body = csrf_client.get("/comentarios/lista?parte=cuerpo", headers=HX)
    assert demo_thread.id not in item_ids(body.text)  # no longer "Sin responder"
    response = csrf_client.post(f"/comentarios/{demo_thread.id}/resolver", data={"resuelto": "0"}, headers=HX)
    assert "Marcar como resuelto" in response.text
    plain = csrf_client.post(
        f"/comentarios/{demo_thread.id}/resolver", data={"resuelto": "1"}, follow_redirects=False
    )
    assert plain.status_code == 303


def test_mark_unread(csrf_client, db, demo_thread):
    csrf_client.get(f"/comentarios/{demo_thread.id}", headers=HX)
    response = csrf_client.post(f"/comentarios/{demo_thread.id}/no-leido", headers=HX)
    assert response.status_code == 200
    assert "Selecciona una conversación" in response.text
    body = csrf_client.get("/comentarios/lista?parte=cuerpo&tab=no_leidos", headers=HX)
    assert demo_thread.id in item_ids(body.text)


def test_refresh_polls_demo_account(csrf_client, db, demo_mode, demo_account):
    response = csrf_client.post("/comentarios/actualizar", data={"tab": "todos"}, headers=HX)
    assert response.status_code == 200
    assert "comentario" in response.headers["HX-Trigger"]
    assert 'id="inbox-list-panel"' in response.text
    assert 'id="inbox-updated" hx-swap-oob="true"' in response.text
    assert len(item_ids(response.text)) > 0
    again = csrf_client.post("/comentarios/actualizar", data={"tab": "todos"}, headers=HX)
    assert "No hay comentarios nuevos" in again.headers["HX-Trigger"]


def test_refresh_without_accounts(csrf_client, brand):
    response = csrf_client.post("/comentarios/actualizar", headers=HX)
    assert response.status_code == 200
    assert "No hay cuentas" in response.headers["HX-Trigger"]


def test_refresh_reports_failures(csrf_client, db, demo_mode, demo_account, monkeypatch):
    from planificador.providers.base import ErrorKind, ProviderError
    from planificador.providers.demo import DemoProvider

    def boom(self, *args, **kwargs):
        raise ProviderError(ErrorKind.TRANSIENT, "demo_down", "La red Demo no responde.")

    monkeypatch.setattr(DemoProvider, "fetch_comments", boom)
    response = csrf_client.post("/comentarios/actualizar", headers=HX)
    assert "No se pudieron actualizar" in response.headers["HX-Trigger"]
    assert "La red Demo no responde." in response.text


def test_summary_api(client, demo_thread):
    data = client.get("/api/comentarios/resumen").json()
    assert data == {"sin_responder": 2, "no_leidos": 2, "todos": 2}


def test_demo_threads_hidden_without_demo_mode(client, db, demo_thread):
    config.set_setting(db, "demo_mode", False)
    html = client.get("/comentarios?tab=todos").text
    assert item_ids(html) == []

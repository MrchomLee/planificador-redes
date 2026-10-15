"""Tests for planificador.templating (filters, icons, render without DB, macros).

Run: uv run pytest -q tests/test_templating.py
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from starlette.requests import Request

from planificador import templating as T


def make_request(path="/planificacion", session=None, state=None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"localhost:8765")],
        "scheme": "http",
        "server": ("localhost", 8765),
        "root_path": "",
        "state": dict(state or {}),
    }
    if session is not None:
        scope["session"] = session
    return Request(scope)


CTX = {"active_brand": NS(id=1, name="X", color="#123456", timezone="Europe/Madrid")}


def test_dt_es_converts_to_brand_timezone_and_spanish_names():
    year = datetime.now(UTC).year
    dt = datetime(year, 11, 3, 9, 30, tzinfo=UTC)  # 10:30 in Madrid (CET)
    weekday = T.DAYS_ES[dt.weekday()]
    assert T.dt_es(CTX, dt) == f"{weekday} 3 de noviembre, 10:30"
    assert T.dt_es(CTX, dt, fmt="short") == "3 nov, 10:30"
    assert T.dt_es(CTX, dt, tz="Atlantic/Canary") == f"{weekday} 3 de noviembre, 09:30"
    assert T.time_es(CTX, dt) == "10:30"
    assert T.date_es(CTX, dt, fmt="full") == f"{weekday} 3 de noviembre de {year}"
    assert T.iso_local(CTX, dt) == f"{year}-11-03T10:30"


def test_dt_es_naive_is_utc_and_other_year_adds_year():
    dt = datetime(2030, 7, 1, 8, 0)  # naive -> UTC -> 10:00 CEST
    assert T.dt_es(CTX, dt) == "lunes 1 de julio de 2030, 10:00"
    assert T.dt_es(CTX, None) == ""
    assert T.date_es({}, dt, fmt="numeric") == "01/07/2030"


def test_iso_utc():
    assert T.iso_utc(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05Z"
    assert T.iso_utc(None) == ""


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=10), "hace un momento"),
        (timedelta(minutes=5), "hace 5 min"),
        (timedelta(hours=3), "hace 3 h"),
        (timedelta(hours=30), "ayer"),
        (timedelta(days=5), "hace 5 días"),
        (timedelta(days=40), "hace 1 mes"),
        (timedelta(days=100), "hace 3 meses"),
        (timedelta(days=800), "hace 2 años"),
        (-timedelta(minutes=5), "en 5 min"),
        (-timedelta(hours=30), "mañana"),
    ],
)
def test_timeago(delta, expected):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    assert T.timeago_es(now - delta, now=now) == expected


def test_numbers():
    assert T.number_es(1234567) == "1.234.567"
    assert T.number_es(3.14159, 2) == "3,14"
    assert T.number_es(12345, compact=True) == "12,3 mil"
    assert T.number_es(1_200_000, compact=True) == "1,2 M"
    assert T.number_es(9999, compact=True) == "9.999"
    assert T.number_es(None) == "—"
    assert T.pct(12.345) == "12,3 %"
    assert T.pct(-4.2, signed=True) == "−4,2 %"
    assert T.pct(4.2, signed=True) == "+4,2 %"
    assert T.pct(0.071, ratio=True) == "7,1 %"
    assert T.pct(0) == "0 %"
    assert T.pct(None) == "—"
    assert T.filesize(512) == "512 B"
    assert T.filesize(1536) == "1,5 KB"
    assert T.filesize(15 * 1024 * 1024) == "15 MB"
    assert T.filesize(3 * 1024**3) == "3 GB"


def test_css_color_only_accepts_hex():
    assert T.css_color("#e8590c") == "#e8590c"
    assert T.css_color("red; background:url(x)") == "#5b4cf0"


def test_status_maps_cover_all_target_statuses():
    statuses = [
        "draft",
        "scheduled",
        "publishing",
        "published",
        "manual_pending",
        "manual_done",
        "sent_to_app",
        "error",
        "missed",
        "unknown",
        "cancelled",
    ]
    for s in statuses:
        assert s in T.STATUS_LABELS and s in T.STATUS_TONES and s in T.STATUS_ICONS
    assert T.status_label("manual_pending") == "Publicación asistida"
    assert T.status_tone("missed") == "danger"


def test_icons_and_aliases():
    svg = str(T.icon("calendar", 20))
    assert svg.startswith("<svg") and 'width="20"' in svg and "<path" in svg and 'aria-hidden="true"' in svg
    assert "<path" in str(T.icon("trash-2"))  # lucide 0.x name resolved through aliases.json
    assert 'role="img"' in str(T.icon("bell", label="Avisos"))
    assert str(T.icon("no-such-icon-xyz")).startswith("<svg")  # fallback, no exception
    for net in ("facebook", "instagram", "tiktok", "demo"):
        html = str(T.net_icon(net, 24))
        assert f"net-icon--{net}" in html and "<svg" in html
    assert "<path" in str(T.net_icon("facebook"))


def test_static_url_cache_busting():
    url = T.static_url("css/app.css")
    assert url.startswith("/static/css/app.css?v=")
    assert T.static_url("/static/js/app.js").startswith("/static/js/app.js?v=")
    assert T.static_url("nope/missing.js") == "/static/nope/missing.js"
    assert T.static_exists("vendor/htmx.org/htmx.min.js")
    assert not T.static_exists("../templating.py")


def test_nav_key():
    assert T.nav_key("/") == "planificacion"
    assert T.nav_key("/estadisticas/cuenta/3") == "estadisticas"
    assert T.nav_key("/publicaciones/nueva") == "planificacion"
    assert T.nav_key("/ajustes") == "ajustes"


def test_render_without_database_uses_empty_globals():
    req = make_request("/biblioteca", session={"csrf_token": "tok123"})
    resp = T.render(req, "placeholder.html", page_title="Biblioteca")
    html = resp.body.decode()
    assert resp.status_code == 200
    assert '<meta name="csrf-token" content="tok123">' in html
    assert "En construcción" in html and "Sin marca" in html
    assert 'class="mainnav__link is-active" aria-current="page"' in html  # Biblioteca active


def test_render_state_csrf_and_flashes():
    session = {"_flashes": [{"message": "Guardado", "kind": "success"}]}
    req = make_request("/ajustes", session=session, state={"csrf_token": "state-tok"})
    html = T.render(req, "placeholder.html", page_title="Ajustes").body.decode()
    assert 'content="state-tok"' in html
    assert "Guardado" in html and "_flashes" not in session


def test_render_with_globals_and_demo_ribbon(monkeypatch):
    brands = [
        NS(id=1, name="Panadería La Espiga", color="#e8590c", timezone="Europe/Madrid"),
        NS(id=2, name="Estudio <b>", color="javascript:x", timezone="Europe/Madrid"),
    ]
    monkeypatch.setattr(
        T,
        "_db_context",
        lambda request: {
            "brands": brands,
            "active_brand": brands[0],
            "brand_networks": {1: ["facebook", "tiktok"]},
            "unread_notifications": 120,
            "demo_mode": True,
            "app_timezone": "Europe/Madrid",
        },
    )
    html = T.render(make_request("/"), "placeholder.html", page_title="Inicio").body.decode()
    assert "Panadería La Espiga" in html and "Modo prueba activo" in html
    assert "Estudio &lt;b&gt;" in html and "javascript:x" not in html
    assert "99+" in html
    assert 'action="/marcas/2/activar"' in html


def test_error_and_login_pages_render():
    req = make_request("/x")
    assert "No encontramos esta página" in T.render(req, "errors/404.html", status_code=404).body.decode()
    html = T.render(req, "errors/500.html", status_code=500, error_id="abc").body.decode()
    assert "Algo ha fallado" in html and "abc" in html
    html = T.render(req, "login.html", error="Mal", next="/ajustes").body.decode()
    assert 'action="/login"' in html and "Mal" in html and 'value="/ajustes"' in html
    # Direct TemplateResponse (no render) still works thanks to env defaults.
    assert T.templates.TemplateResponse(req, "placeholder.html", {"page_title": "X"}).status_code == 200


def test_notif_summary_partial():
    now = datetime.now(UTC)
    items = [
        {
            "id": 5,
            "kind": "manual_publish",
            "title": "Toca publicar",
            "body": "Texto",
            "created_at": now,
            "read_at": None,
            "target_id": 9,
        },
        NS(
            id=4,
            kind="reconnect",
            title="Reconecta",
            body=None,
            created_at=now - timedelta(hours=2),
            read_at=now,
            target_id=None,
        ),
    ]
    html = T.render(
        make_request("/api/notificaciones/resumen"), "partials/notif_summary.html", unread=1, items=items
    ).body.decode()
    assert 'hx-swap-oob="innerHTML"' in html and 'hx-get="/objetivos/9/asistida"' in html
    assert "data-notify-items=" in html and '"kind": "manual_publish"' in html
    assert 'href="/conexiones"' in html and "notif__badge" in html


def test_ui_macros_render():
    tpl = T.env.from_string(
        '{% import "components/ui.html" as ui with context %}'
        '{{ ui.button("Guardar", variant="primary", icon_name="check", attrs={"hx-post": "/x"}) }}'
        '{{ ui.badge("manual_pending") }}{{ ui.badge("cancelled") }}'
        '{{ ui.kpi_card("Seguidores", 12345, delta=-3.2) }}'
        '{{ ui.form_field("name", "Nombre", error="Obligatorio", required=True) }}'
        '{{ ui.switch("auto", "Auto", checked=True, off_value="0") }}'
        '{{ ui.tabs([{"key": "a", "label": "A"}, ("b", "B", "/b")], active="a") }}'
        '{{ ui.avatar("Ana García", "#7048e8") }}{{ ui.net_badge("instagram") }}'
        '{% call ui.modal("Título", size="lg") %}cuerpo{% endcall %}'
        '{% call ui.drawer("Panel") %}detalle{% endcall %}'
        '{% call ui.empty_state("Vacío", "Nada") %}acción{% endcall %}'
        '{% call ui.dropdown("Más") %}{{ ui.dropdown_item("Borrar", danger=True) }}{% endcall %}'
        "{{ ui.reltime(now) }}"
    )
    html = tpl.render(now=datetime.now(UTC), active_brand=None)
    assert 'class="btn btn--primary"' in html and 'hx-post="/x"' in html
    assert "Publicación asistida" in html and "st-cancelled" in html
    assert "12,3 mil" in html and "delta--bad" in html and "−3,2 %" in html
    assert 'aria-invalid="true"' in html and "Obligatorio" in html
    assert 'type="hidden" name="auto" value="0"' in html
    assert 'aria-current="page"' in html and "count-pill" not in html
    assert ">AG<" in html and "--avatar-bg:#7048e8" in html
    assert 'role="dialog"' in html and "modal--lg" in html and "drawer--md" in html
    assert "data-timeago" in html and "hace un momento" in html


def test_fragment_render_keeps_flashes_for_next_page():
    session = {"_flashes": [{"message": "Hola", "kind": "info"}]}
    req = make_request("/x", session=session)
    T.render(req, "partials/notif_badge.html", unread=2)
    assert session["_flashes"]  # not consumed by a fragment
    T.flash(req, "Otra", "success")
    html = T.render(req, "placeholder.html").body.decode()
    assert "Hola" in html and "Otra" in html and "_flashes" not in session


def test_toast_headers():
    import json

    h = T.toast_headers("Guardado", closeModal=True)
    assert json.loads(h["HX-Trigger"]) == {
        "toast": {"message": "Guardado", "kind": "success"},
        "closeModal": True,
    }

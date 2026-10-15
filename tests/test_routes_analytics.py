"""Estadísticas routes: pages, tabs, periods, empty states, CSV exports and "Actualizar ahora"."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import analytics, config
from planificador.models import (
    Account,
    AccountStatus,
    AnalyticsSnapshot,
    Network,
    Post,
    PostMetric,
    PostTarget,
    TargetStatus,
)


def _no_http() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def chart_data(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="an-data">(.*?)</script>', html, re.S)
    assert m, "chart JSON missing"
    return json.loads(m.group(1))


@pytest.fixture
def seeded(db: Session, demo_mode, demo_account, clock):
    """Demo account with 90 days of snapshots and 30 days of post metrics."""
    with _no_http() as http:
        summary = analytics.snapshot_all(db, http, clock, config.provider_settings(db))
    assert summary["ok"] == 1
    return demo_account


def make_account(db: Session, brand, network: str, **kw) -> Account:
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=f"{network}-ext",
        name=kw.pop("name", f"Cuenta {network}"),
        username=kw.pop("username", f"{network}_user"),
        status=kw.pop("status", AccountStatus.CONNECTED),
        **kw,
    )
    db.add(account)
    db.commit()
    return account


# ------------------------------------------------------------------------------------------- pages


def test_summary_without_accounts_shows_sidebar_and_empty_state(client: TestClient, brand) -> None:
    r = client.get("/estadisticas")
    assert r.status_code == 200
    html = r.text
    assert "Conecta tus redes para ver estadísticas" in html
    for label in ("Resumen", "Facebook", "Instagram", "TikTok", "YouTube", "Más conexiones", "Exportar CSV"):
        assert label in html
    assert html.count('class="an-side__plus" href="/conexiones"') == 4  # one (+) per unconnected network
    assert 'aria-current="page"' in html
    assert "css/analytics.css" in html and "js/analytics.js" in html and "chart.umd.min.js" in html


def test_summary_with_data(client: TestClient, seeded, brand) -> None:
    r = client.get("/estadisticas")
    assert r.status_code == 200
    html = r.text
    assert "Visión general" in html and "Por red" in html
    assert "Comunidad total" in html and "Nuevos seguidores" in html
    assert "Periodo principal" in html and "Periodo de comparación" in html
    assert "Mejores horas para publicar" in html
    assert html.count('class="an-heat__row"') == 7
    data = chart_data(html)
    assert set(data["charts"]) == {"summary-posts", "summary-views"}
    posts = data["charts"]["summary-posts"]
    assert len(posts["labels"]) == 28 and posts["stacked"] is True
    assert posts["series"][0]["label"] == "Demo" and posts["series"][0]["color"] == "chart-7"
    assert "Ver datos en tabla" in html
    assert "Actualizar ahora" in html


def test_network_page_comunidad(client: TestClient, seeded) -> None:
    r = client.get("/estadisticas/demo")
    assert r.status_code == 200
    html = r.text
    assert "COMUNIDAD" in html.upper() and "Crecimiento" in html
    assert (
        'data-tile="followers"' in html
        and 'data-tile="posts"' in html
        and 'data-tile="followers_net"' in html
    )
    assert "Balance de seguidores" in html
    assert "Datos de demostración" in html  # per-network info alert
    assert "an-chip" in html and "Cuenta Demo" in html
    charts = chart_data(html)["charts"]
    assert charts["followers"]["type"] == "line" and charts["followers"]["stock"] is True
    assert len(charts["followers"]["labels"]) == 28
    assert all(v is not None for v in charts["followers"]["series"][0]["values"])
    assert charts["posts"]["type"] == "bar" and charts["balance"]["diverging"] is True
    assert "respecto al periodo anterior" in html  # deltas shown (comparison on by default)


def test_network_page_tabs_and_periods(client: TestClient, seeded) -> None:
    cuenta = client.get("/estadisticas/demo?tab=cuenta&periodo=7")
    assert cuenta.status_code == 200
    charts = chart_data(cuenta.text)["charts"]
    assert "metric-views" in charts and "metric-reach" in charts
    views = charts["metric-views"]
    assert len(views["labels"]) == 7
    assert [s["label"] for s in views["series"]] == ["Visualizaciones", "Periodo anterior"]
    assert views["series"][1]["color"] == "muted"
    assert "Visitas al perfil" in cuenta.text

    no_cmp = client.get("/estadisticas/demo?tab=cuenta&periodo=7&comparar=ninguno")
    charts = chart_data(no_cmp.text)["charts"]
    assert len(charts["metric-views"]["series"]) == 1
    assert "respecto al periodo anterior" not in no_cmp.text
    assert "Sin comparación" in no_cmp.text

    custom = client.get("/estadisticas/demo?periodo=personalizado&desde=2026-10-01&hasta=2026-10-10")
    assert "1 oct 2026 - 10 oct 2026" in custom.text
    assert len(chart_data(custom.text)["charts"]["followers"]["labels"]) == 10
    # links keep the period and comparison
    assert "periodo=personalizado&amp;desde=2026-10-01&amp;hasta=2026-10-10" in custom.text

    posts = client.get("/estadisticas/demo?tab=posts")
    html = posts.text
    assert posts.status_code == 200
    assert "Descargar CSV" in html and f"/estadisticas/{seeded.id}/publicaciones.csv" in html
    assert "data-an-sortable" in html and "Engagement" in html and "Alcance" in html
    assert "[Demo] Publicación de ejemplo" in html
    assert 'data-tile="engagement"' in html
    assert "Mejores horas para publicar en Demo" in html

    unknown_tab = client.get("/estadisticas/demo?tab=nada")
    assert unknown_tab.status_code == 200 and "Crecimiento" in unknown_tab.text


def test_unconnected_network_and_errors(client: TestClient, db: Session, brand, demo_mode_off) -> None:
    r = client.get("/estadisticas/tiktok")
    assert r.status_code == 200
    assert "Conecta tu cuenta de TikTok" in r.text and 'href="/conexiones"' in r.text
    assert client.get("/estadisticas/linkedin").status_code == 404
    demo = client.get("/estadisticas/demo", follow_redirects=False)
    assert demo.status_code == 303 and demo.headers["location"] == "/estadisticas"


def test_connected_without_data_and_error_alert(client: TestClient, db: Session, brand) -> None:
    account = make_account(db, brand, Network.INSTAGRAM)
    r = client.get("/estadisticas/instagram")
    assert "Aún no hay datos de Instagram" in r.text
    assert "El número de seguidores solo estará disponible" in r.text
    account.meta = {"analytics_error": {"message": "La sesión de Instagram ha caducado.", "code": "x"}}
    account.status = AccountStatus.NEEDS_RECONNECT
    db.commit()
    r = client.get("/estadisticas/instagram")
    assert "La sesión de Instagram ha caducado." in r.text
    assert "Hay que volver a conectar la cuenta" in r.text
    assert "Reintentar" in r.text
    assert 'class="an-side__flag"' in r.text


def test_youtube_native_metrics_only(client: TestClient, db: Session, brand, clock) -> None:
    yt = make_account(db, brand, Network.YOUTUBE, name="Mi canal")
    today = date(2026, 11, 3)
    for i in range(5):
        d = today - timedelta(days=i)
        db.add(
            AnalyticsSnapshot(
                account_id=yt.id,
                day=d,
                metrics={"followers": 1200, "views": 100 + i, "subs_gained": 2, "subs_lost": 1, "likes": 9},
            )
        )
    db.add(
        PostMetric(
            account_id=yt.id,
            external_id="v1",
            published_at=clock.now() - timedelta(days=1),
            caption="Mi vídeo",
            media_type="video",
            metrics={"views": 50, "likes": 5, "comments": 1, "watch_minutes": 30},
        )
    )
    db.commit()
    r = client.get("/estadisticas/youtube")
    html = r.text
    assert "Suscriptores" in html and "≈" in html
    assert "Suscriptores ganados y perdidos" in html
    assert chart_data(html)["charts"]["balance"]["stacked"] is True
    posts = client.get("/estadisticas/youtube?tab=posts").text
    assert "Minutos vistos" in posts and ">Engagement<" not in posts and "Alcance" not in posts
    assert "métricas nativas de YouTube" in posts
    summary = client.get("/estadisticas").text
    assert "n/d" in summary and "YouTube aporta solo métricas nativas" in summary


# --------------------------------------------------------------------------------------------- CSV


def test_posts_csv_export(client: TestClient, seeded, db: Session) -> None:
    r = client.get(f"/estadisticas/{seeded.id}/publicaciones.csv?periodo=90")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert (
        "attachment" in r.headers["content-disposition"]
        and "publicaciones-demo-" in r.headers["content-disposition"]
    )
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert rows[0][:4] == ["fecha", "hora", "texto", "tipo"]
    expected = len(analytics.post_rows(db, seeded, date(2026, 8, 6), date(2026, 11, 3)))
    assert len(rows) - 1 == expected > 0
    assert client.get("/estadisticas/999/publicaciones.csv").status_code == 404


def test_snapshots_csv_export(client: TestClient, seeded) -> None:
    r = client.get("/estadisticas/exportar.csv?periodo=7")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8").lstrip("﻿")), delimiter=";"))
    assert rows[0][:4] == ["fecha", "red", "cuenta", "followers"]
    assert len(rows) == 1 + 7
    assert {row[1] for row in rows[1:]} == {"Demo"}
    assert rows[-1][0] == "2026-11-03"


# --------------------------------------------------------------------------------- Actualizar ahora


def _wait_idle(client: TestClient, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/estadisticas/estado").json()
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("refresh did not finish")


def test_refresh_now(client: TestClient, csrf_client, db: Session, demo_mode, demo_account) -> None:
    assert client.post("/estadisticas/actualizar").status_code == 403  # CSRF required
    r = csrf_client.post("/estadisticas/actualizar", headers={"Accept": "application/json"})
    assert r.status_code in (200, 202)
    body = r.json()
    assert body["accounts"] == 1 and body["running"] in (True, False)
    status = _wait_idle(client)
    assert status["summary"]["ok"] == 1 and status["kind"] == "success"
    assert status["message"] == "Estadísticas actualizadas."
    db.expire_all()
    assert db.scalar(select(AnalyticsSnapshot.id).where(AnalyticsSnapshot.account_id == demo_account.id))

    # plain form post (no JS): redirect back with a flash
    r = csrf_client.post(
        "/estadisticas/actualizar",
        headers={"Referer": "http://localhost:8765/estadisticas/demo?tab=cuenta"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/estadisticas/demo?tab=cuenta"
    _wait_idle(client)


def test_refresh_without_accounts(csrf_client, brand) -> None:
    r = csrf_client.post("/estadisticas/actualizar", headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {
        "started": False,
        "running": False,
        "accounts": 0,
        "message": "No hay cuentas conectadas en esta marca.",
    }


def test_best_times_on_posts_tab_uses_our_posts(client: TestClient, db: Session, brand, clock) -> None:
    fb = make_account(db, brand, Network.FACEBOOK)
    for i in range(12):
        when = clock.now() - timedelta(days=i + 1)
        post = Post(brand_id=brand.id, text=f"p{i}", scheduled_at=when)
        db.add(post)
        db.flush()
        target = PostTarget(
            post_id=post.id,
            account_id=fb.id,
            network=Network.FACEBOOK,
            status=TargetStatus.PUBLISHED,
            scheduled_at=when,
            published_at=when,
            external_id=f"x_{i}",
        )
        db.add(target)
        db.flush()
        db.add(
            PostMetric(
                account_id=fb.id,
                external_id=f"x_{i}",
                target_id=target.id,
                published_at=when,
                caption=f"Post {i}",
                media_type="PHOTO",
                metrics={"likes": 10, "reach": 100, "interactions": 10, "engagement": 10.0},
            )
        )
    db.commit()
    html = client.get("/estadisticas/facebook?tab=posts").text
    assert "Según el rendimiento de tus 12 publicaciones" in html
    assert "Desde la app" in html and "/objetivos/" in html
    assert datetime  # imported for clarity in fixtures

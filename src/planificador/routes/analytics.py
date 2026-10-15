"""Analytics pages ("Estadísticas", SPEC §12, doc 05 §4 / screenshot 2).

- ``GET  /estadisticas`` — Resumen: every network of the active brand.
- ``GET  /estadisticas/{red}`` — one network (``facebook|instagram|tiktok|youtube|demo``) with the tabs
  ``?tab=comunidad|cuenta|posts``.
- ``GET  /estadisticas/{account_id}/publicaciones.csv`` — posts table of one account as CSV.
- ``GET  /estadisticas/exportar.csv`` — daily snapshots of every account of the brand as CSV.
- ``POST /estadisticas/actualizar`` — "Actualizar ahora": snapshot in a background thread (202).
- ``GET  /estadisticas/estado`` — JSON status of that background refresh (polled by the page).

Every page takes ``?periodo=7|28|90|personalizado`` (+ ``desde``/``hasta`` ISO dates) and
``?comparar=anterior|ninguno``. Charts are drawn by ``static/js/analytics.js`` from the JSON embedded in the
page (``#an-data``); every chart has a table view rendered on the server.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planificador import analytics, config
from planificador.analytics import Period
from planificador.deps import ActiveBrand, ClockDep, DbSession, ProviderSettingsDep
from planificador.models import (
    NETWORK_LABELS,
    Account,
    AccountStatus,
    AnalyticsSnapshot,
    Brand,
)
from planificador.timeutil import DAY_NAMES_ES, MONTH_ABBR_ES

log = logging.getLogger(__name__)

router = APIRouter()

NETWORK_ORDER: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube", "demo")
REAL_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube")
TABS: tuple[tuple[str, str], ...] = (("comunidad", "Comunidad"), ("cuenta", "Cuenta"), ("posts", "Posts"))
COMPARE_OPTIONS: dict[str, str] = {"anterior": "Periodo anterior", "ninguno": "Sin comparación"}
PRESET_LABELS: dict[str, str] = {"7": "Últimos 7 días", "28": "Últimos 28 días", "90": "Últimos 90 días"}

#: Categorical slot per network (fixed: colour follows the entity). Slots 1·4·5·6 validate all-pairs in both
#: themes with the dataviz validator (CVD + normal-vision floors); demo (7) only appears in demo mode.
NETWORK_SERIES: dict[str, str] = {
    "facebook": "chart-1",
    "instagram": "chart-5",
    "tiktok": "chart-6",
    "youtube": "chart-4",
    "demo": "chart-7",
}

#: Per-network notes shown in the blue info alert (doc 05 §4).
NETWORK_NOTES: dict[str, str] = {
    "facebook": (
        "Facebook no ofrece el histórico de seguidores: la evolución se construye con una foto diaria "
        "desde el día en que conectaste la página. Las estadísticas de la página requieren al menos "
        "100 «Me gusta»."
    ),
    "instagram": (
        "El número de seguidores solo estará disponible desde el día en que la cuenta se conectó: "
        "Instagram solo da la cifra actual (y la evolución de los últimos 30 días a cuentas con más de "
        "100 seguidores). Las métricas de las historias desaparecen a las 24 horas."
    ),
    "tiktok": (
        "Es posible que algunas métricas de TikTok no estén disponibles durante los últimos 1-2 días. "
        "El número de seguidores solo estará disponible desde el día en que la cuenta se conectó."
    ),
    "youtube": (
        "YouTube Analytics publica los datos diarios con 2-3 días de retraso y redondea el número de "
        "suscriptores (≈). Se muestran solo las métricas nativas de YouTube."
    ),
    "demo": "Datos de demostración generados automáticamente para probar la aplicación (modo prueba).",
}

#: Metrics of the CUENTA tab per network: (metric, tile colour).
ACCOUNT_METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "facebook": (("views", "blue"), ("reach", "violet"), ("interactions", "teal")),
    "instagram": (
        ("views", "blue"),
        ("reach", "violet"),
        ("interactions", "teal"),
        ("profile_views", "pink"),
    ),
    "tiktok": (("likes_total", "pink"), ("video_count", "amber"), ("following", "violet")),
    "youtube": (
        ("views", "blue"),
        ("watch_minutes", "violet"),
        ("likes", "pink"),
        ("comments", "teal"),
        ("shares", "green"),
        ("subs_gained", "amber"),
    ),
    "demo": (("views", "blue"), ("reach", "violet"), ("interactions", "teal"), ("profile_views", "pink")),
}
METRIC_HELP: dict[str, str] = {
    "views": "Veces que se ha visto tu contenido (suma diaria).",
    "reach": "Cuentas alcanzadas cada día (suma diaria; una misma cuenta puede contar varios días).",
    "interactions": "Me gusta, comentarios, compartidos y guardados (suma diaria).",
    "profile_views": "Visitas a tu perfil (suma diaria).",
    "watch_minutes": "Minutos de reproducción estimados (YouTube Analytics).",
    "likes_total": "Me gusta acumulados en todos tus vídeos (cifra total de TikTok).",
    "video_count": "Vídeos publicados en la cuenta (cifra total).",
    "following": "Cuentas que sigues.",
    "engagement": (
        "Interacciones ÷ alcance × 100 de las publicaciones del periodo (o ÷ visualizaciones si la red no "
        "da alcance)."
    ),
}

#: Columns of the POSTS table: (key, label, numeric).
POST_COLUMNS_DEFAULT: tuple[tuple[str, str], ...] = (
    ("views", "Visualizaciones"),
    ("reach", "Alcance"),
    ("likes", "Me gusta"),
    ("comments", "Comentarios"),
    ("shares", "Compartidos"),
    ("engagement", "Engagement"),
)
POST_COLUMNS_YOUTUBE: tuple[tuple[str, str], ...] = (
    ("views", "Visualizaciones"),
    ("likes", "Me gusta"),
    ("comments", "Comentarios"),
    ("shares", "Compartidos"),
    ("watch_minutes", "Minutos vistos"),
)

HEAT_LEVELS = 7
NO_STORE = {"Cache-Control": "no-store"}


# ------------------------------------------------------------------------------------------- helpers


def short_date(d: date, year: bool = True) -> str:
    """'15 sep 2026' (doc 05 §4 range label)."""
    s = f"{d.day} {MONTH_ABBR_ES[d.month - 1]}"
    return f"{s} {d.year}" if year else s


def range_label(period: Period) -> str:
    return f"{short_date(period.start)} - {short_date(period.end)}"


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:40] or "marca"


def network_label(network: str) -> str:
    return NETWORK_LABELS.get(network, network.capitalize())


def brand_accounts(db: Session, brand: Brand) -> dict[str, Account]:
    """Accounts of the brand by network (disconnected ones included: their history stays visible)."""
    rows = db.scalars(select(Account).where(Account.brand_id == brand.id))
    return {str(a.network): a for a in rows}


def last_update(db: Session, account_ids: list[int]) -> datetime | None:
    if not account_ids:
        return None
    return db.scalar(
        select(func.max(AnalyticsSnapshot.fetched_at)).where(AnalyticsSnapshot.account_id.in_(account_ids))
    )


def has_any_data(db: Session, account: Account) -> bool:
    from planificador.models import PostMetric

    snap = db.scalar(select(AnalyticsSnapshot.id).where(AnalyticsSnapshot.account_id == account.id).limit(1))
    if snap is not None:
        return True
    return db.scalar(select(PostMetric.id).where(PostMetric.account_id == account.id).limit(1)) is not None


class PageState:
    """Period / comparison / tab parameters and the URLs that keep them."""

    def __init__(self, request: Request, today: date, path: str, tab: str | None = None) -> None:
        params = request.query_params
        self.today = today
        self.path = path
        self.period, self.preset = analytics.resolve_period(
            today, params.get("periodo"), params.get("desde"), params.get("hasta")
        )
        compare = params.get("comparar") or "anterior"
        self.compare_key = compare if compare in COMPARE_OPTIONS else "anterior"
        self.compare = self.compare_key == "anterior"
        self.previous = self.period.previous()
        self.tab = tab

    def params(self, **overrides: Any) -> dict[str, str]:
        out: dict[str, Any] = {}
        if self.tab and self.tab != "comunidad":
            out["tab"] = self.tab
        out["periodo"] = self.preset
        if self.preset == "personalizado":
            out["desde"] = self.period.start.isoformat()
            out["hasta"] = self.period.end.isoformat()
        if self.compare_key != "anterior":
            out["comparar"] = self.compare_key
        out.update(overrides)
        if out.get("periodo") != "personalizado":
            out.pop("desde", None)
            out.pop("hasta", None)
        if out.get("periodo") == str(analytics.DEFAULT_PRESET):
            out.pop("periodo")
        if out.get("tab") == "comunidad":
            out.pop("tab")
        return {k: str(v) for k, v in out.items() if v not in (None, "")}

    def url(self, path: str | None = None, **overrides: Any) -> str:
        query = urlencode(self.params(**overrides))
        return (path or self.path) + (f"?{query}" if query else "")

    def context(self) -> dict[str, Any]:
        presets = [
            {"key": key, "label": label, "href": self.url(periodo=key), "active": self.preset == key}
            for key, label in PRESET_LABELS.items()
        ]
        compare_options = [
            {
                "key": key,
                "label": label,
                "detail": range_label(self.previous) if key == "anterior" else None,
                "href": self.url(comparar=key),
                "active": self.compare_key == key,
            }
            for key, label in COMPARE_OPTIONS.items()
        ]
        hidden = {k: v for k, v in self.params().items() if k not in ("periodo", "desde", "hasta")}
        return {
            "period": self.period,
            "previous_period": self.previous,
            "preset": self.preset,
            "period_label": range_label(self.period),
            "previous_label": range_label(self.previous),
            "compare": self.compare,
            "compare_key": self.compare_key,
            "compare_label": COMPARE_OPTIONS[self.compare_key],
            "presets": presets,
            "compare_options": compare_options,
            "custom_hidden": hidden,
            "today_iso": self.today.isoformat(),
            "url_for_state": self.url,
            "params": self.params(),
        }


def sidebar_items(
    accounts: dict[str, Account], demo_mode: bool, active: str, state: PageState
) -> list[dict[str, Any]]:
    networks = list(REAL_NETWORKS) + (["demo"] if demo_mode else [])
    items: list[dict[str, Any]] = []
    for net in networks:
        account = accounts.get(net)
        connected = account is not None and account.status != AccountStatus.DISCONNECTED
        items.append(
            {
                "network": net,
                "label": network_label(net),
                "account": account if connected else None,
                "needs_reconnect": connected and account.status == AccountStatus.NEEDS_RECONNECT,
                "has_error": connected and bool((account.meta or {}).get("analytics_error")),
                "href": state.url(f"/estadisticas/{net}", tab=None),
                "active": active == net,
            }
        )
    return items


def _iso_days(period: Period) -> list[str]:
    return [d.isoformat() for d in period.dates()]


def _values(points: list[tuple[date, float | None]]) -> list[float | None]:
    return [analytics.clean_number(v) for _, v in points]


def tile(
    key: str,
    label: str,
    kpi: dict[str, Any] | None,
    color: str,
    *,
    fmt: str = "number",
    signed: bool = False,
    approx: bool = False,
    help: str | None = None,
) -> dict[str, Any]:
    kpi = kpi or {}
    return {
        "key": key,
        "label": label,
        "value": kpi.get("value"),
        "previous": kpi.get("previous"),
        "delta": kpi.get("change_pct"),
        "color": color,
        "fmt": fmt,
        "signed": signed,
        "approx": approx,
        "help": help,
    }


def heatmap(result: dict[str, Any]) -> dict[str, Any]:
    """Best-times matrix → cells with a sequential level (1..7) and the top slots (table twin)."""
    matrix = result["matrix"]
    flat = [float(v) for row in matrix for v in row]
    lo, hi = min(flat), max(flat)
    span = (hi - lo) or 1.0
    rows = []
    slots = []
    for d, row in enumerate(matrix):
        cells = []
        for h, v in enumerate(row):
            # Display levels stretch the matrix range over the 7 steps of the sequential ramp.
            level = 1 + min(HEAT_LEVELS - 1, int((float(v) - lo) / span * HEAT_LEVELS))
            cells.append({"hour": h, "value": v, "level": level, "pct": round(float(v) * 100)})
            slots.append((float(v), d, h))
        rows.append(
            {"day": d, "label": DAY_NAMES_ES[d].capitalize(), "short": DAY_NAMES_ES[d][:3], "cells": cells}
        )
    slots.sort(key=lambda s: (-s[0], s[1], s[2]))
    top = [{"day": DAY_NAMES_ES[d], "hour": f"{h:02d}:00", "pct": round(v * 100)} for v, d, h in slots[:5]]
    source = result.get("source")
    posts = int(result.get("posts") or 0)
    if source == "datos":
        source_label = f"Según el rendimiento de tus {posts} publicaciones del último año."
    else:
        source_label = (
            f"Patrón general: hacen falta al menos {analytics.BEST_TIMES_MIN_POSTS} publicaciones propias "
            f"con estadísticas (ahora {posts})."
        )
    return {"rows": rows, "top": top, "source": source, "source_label": source_label, "posts": posts}


# ------------------------------------------------------------------------------------- network page


def network_context(
    db: Session, account: Account, state: PageState, clock: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(template context, chart JSON) for one account."""
    network = str(account.network)
    native = network in analytics.NATIVE_ONLY_NETWORKS
    period, prev = state.period, state.previous
    k = analytics.kpis(db, account, period.start, period.end, compare=state.compare)
    labels = _iso_days(period)
    prev_labels = _iso_days(prev)
    charts: dict[str, Any] = {}
    ctx: dict[str, Any] = {"kpis": k}

    followers_label = analytics.metric_label("followers", network)
    # ---- COMUNIDAD
    followers = analytics.series(db, account, "followers", period.start, period.end)
    posts_daily = analytics.posts_per_day(db, account, period.start, period.end)
    balance = analytics.follower_balance(db, account, period.start, period.end)
    charts["followers"] = {
        "type": "line",
        "stock": True,
        "labels": labels,
        "series": [
            {"label": followers_label, "values": _values(followers), "color": "chart-1", "fill": True}
        ],
    }
    charts["posts"] = {
        "type": "bar",
        "labels": labels,
        "integer": True,
        "series": [{"label": "Posts", "values": [n for _, n in posts_daily], "color": "chart-4"}],
    }
    if balance["native"]:
        charts["balance"] = {
            "type": "bar",
            "labels": labels,
            "stacked": True,
            "integer": True,
            "series": [
                {
                    "label": "Suscriptores ganados",
                    "values": [d["gained"] for d in balance["days"]],
                    "color": "chart-1",
                },
                {
                    "label": "Suscriptores perdidos",
                    "values": [-(d["lost"] or 0) if d["lost"] is not None else None for d in balance["days"]],
                    "color": "chart-8",
                    "abs": True,
                },
            ],
        }
    else:
        charts["balance"] = {
            "type": "bar",
            "labels": labels,
            "diverging": True,
            "integer": True,
            "series": [
                {"label": "Balance", "values": [d["net"] for d in balance["days"]], "color": "chart-1"}
            ],
        }
    growth_tiles = [
        tile("followers", followers_label, k.get("followers"), "blue", approx=network == "youtube"),
    ]
    if balance["native"]:
        growth_tiles.append(tile("subs_gained", "Nuevos suscriptores", k.get("subs_gained"), "green"))
    else:
        growth_tiles.append(tile("followers_net", "Balance", k.get("followers_net"), "green", signed=True))
    growth_tiles.append(tile("posts", "Posts", k.get("posts"), "amber"))
    ctx.update(
        followers_label=followers_label,
        growth_tiles=growth_tiles,
        balance=balance,
        community_table=[
            {
                "day": d,
                "followers": followers[i][1],
                "posts": posts_daily[i][1],
                "net": balance["days"][i]["net"],
                "gained": balance["days"][i]["gained"],
                "lost": balance["days"][i]["lost"],
            }
            for i, d in enumerate(period.dates())
        ],
        following=k.get("following"),
    )

    # ---- CUENTA
    metric_cards = []
    for metric, color in ACCOUNT_METRICS.get(network, ()):
        cur = analytics.series(db, account, metric, period.start, period.end)
        if all(v is None for _, v in cur):
            continue
        prev_points = analytics.series(db, account, metric, prev.start, prev.end) if state.compare else []
        label = analytics.metric_label(metric, network)
        series = [{"label": label, "values": _values(cur), "color": "chart-1", "fill": True, "dates": labels}]
        if prev_points and any(v is not None for _, v in prev_points):
            series.append(
                {
                    "label": "Periodo anterior",
                    "values": _values(prev_points),
                    "color": "muted",
                    "dates": prev_labels,
                }
            )
        stock = metric in analytics.STOCK_METRICS
        charts[f"metric-{metric}"] = {"type": "line", "labels": labels, "series": series, "stock": stock}
        metric_cards.append(
            {
                "key": metric,
                "label": label,
                "tile": tile(metric, label, k.get(metric), color, help=METRIC_HELP.get(metric)),
                "chart": f"metric-{metric}",
                "stock": stock,
                "has_prev": len(series) > 1,
                "rows": [
                    {"day": d, "value": cur[i][1], "prev": prev_points[i][1] if prev_points else None}
                    for i, d in enumerate(period.dates())
                ],
                "help": METRIC_HELP.get(metric),
            }
        )
    ctx["metric_cards"] = metric_cards

    # ---- POSTS
    rows = analytics.post_rows(db, account, period.start, period.end)
    for row in rows:
        row["thumb"] = (
            f"/medios/{row['media_id']}/miniatura" if row.get("media_id") else row.get("thumbnail_url")
        )
    if native:
        post_tiles = [
            tile("posts", "Posts", k.get("posts"), "amber"),
            tile("post_views", "Visualizaciones", k.get("post_views"), "blue"),
            tile("post_likes", "Me gusta", k.get("post_likes"), "pink"),
            tile("post_comments", "Comentarios", k.get("post_comments"), "teal"),
        ]
        columns = POST_COLUMNS_YOUTUBE
    else:
        post_tiles = [
            tile("posts", "Posts", k.get("posts"), "amber"),
            tile("post_views", "Visualizaciones", k.get("post_views"), "blue"),
            tile("post_interactions", "Interacciones", k.get("post_interactions"), "violet"),
            tile(
                "engagement",
                "Engagement medio",
                k.get("engagement"),
                "teal",
                fmt="pct",
                help=METRIC_HELP["engagement"],
            ),
        ]
        columns = POST_COLUMNS_DEFAULT
    best = analytics.best_times(db, account.brand_id, network, clock)
    ctx.update(
        post_rows=rows,
        post_tiles=post_tiles,
        post_columns=columns,
        heat=heatmap(best),
        csv_url=state.url(f"/estadisticas/{account.id}/publicaciones.csv", tab=None, comparar=None),
    )
    return ctx, {"charts": charts}


def alerts_for(account: Account | None, network: str) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if account is not None:
        meta = account.meta or {}
        if account.status == AccountStatus.NEEDS_RECONNECT:
            alerts.append(
                {
                    "tone": "warning",
                    "title": "Hay que volver a conectar la cuenta",
                    "text": account.last_error
                    or (
                        "La autorización ha caducado o se ha revocado. Las estadísticas no se actualizan "
                        "hasta que la reconectes."
                    ),
                    "link": ("/conexiones", "Ir a Cuentas conectadas"),
                }
            )
        elif account.status == AccountStatus.DISCONNECTED:
            alerts.append(
                {
                    "tone": "muted",
                    "title": "Cuenta desconectada",
                    "text": "Se muestran los datos guardados hasta la desconexión.",
                    "link": ("/conexiones", "Volver a conectar"),
                }
            )
        error = meta.get("analytics_error")
        if isinstance(error, dict) and error.get("message"):
            alerts.append(
                {
                    "tone": "danger",
                    "title": "No se pudieron actualizar las estadísticas",
                    "text": str(error.get("message")),
                    "at": error.get("at"),
                    "retry": True,
                }
            )
        notice = meta.get("analytics_notice")
        if notice:
            alerts.append({"tone": "warning", "title": None, "text": str(notice)})
    note = NETWORK_NOTES.get(network)
    if note:
        alerts.append({"tone": "info", "title": None, "text": note})
    return alerts


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# ------------------------------------------------------------------------------------- summary page


def summary_context(
    db: Session, brand: Brand, accounts: dict[str, Account], state: PageState, clock: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    period = state.period
    labels = _iso_days(period)
    rows = []
    keys = ("followers", "net", "posts", "views", "interactions")
    totals = {k: 0.0 for k in keys} | {k + "_prev": 0.0 for k in keys}
    seen = {k: False for k in ("followers", "net", "views", "interactions")}
    seen_prev = {k: False for k in ("followers", "net", "views", "interactions")}
    posts_series = []
    views_series = []
    ordered = [accounts[n] for n in NETWORK_ORDER if n in accounts]
    for account in ordered:
        network = str(account.network)
        if account.status == AccountStatus.DISCONNECTED and not has_any_data(db, account):
            continue
        native = network in analytics.NATIVE_ONLY_NETWORKS
        k = analytics.kpis(db, account, period.start, period.end, compare=state.compare)
        views_kpi = k.get("views") or {"value": None, "previous": None, "change_pct": None}
        inter_kpi = None if native else (k.get("interactions") or k.get("post_interactions"))
        row = {
            "account": account,
            "network": network,
            "label": network_label(network),
            "href": state.url(f"/estadisticas/{network}", tab=None),
            "color": NETWORK_SERIES.get(network, "chart-1"),
            "followers": k.get("followers"),
            "followers_label": analytics.metric_label("followers", network),
            "net": k.get("followers_net"),
            "posts": k.get("posts"),
            "views": views_kpi,
            "interactions": inter_kpi,
            "engagement": None if native else k.get("engagement"),
            "native": native,
            "error": (account.meta or {}).get("analytics_error"),
        }
        rows.append(row)
        for key, kpi in (("followers", row["followers"]), ("net", row["net"]), ("posts", row["posts"]),
                         ("views", views_kpi), ("interactions", inter_kpi)):  # fmt: skip
            if not kpi:
                continue
            if kpi.get("value") is not None:
                totals[key] += float(kpi["value"])
                seen[key] = True
            if kpi.get("previous") is not None:
                totals[key + "_prev"] += float(kpi["previous"])
                seen_prev[key] = True
        posts_series.append(
            {
                "label": network_label(network),
                "values": [n for _, n in analytics.posts_per_day(db, account, period.start, period.end)],
                "color": NETWORK_SERIES.get(network, "chart-1"),
                "network": network,
            }
        )
        daily_views = analytics.series(db, account, "views", period.start, period.end)
        if any(v is not None for _, v in daily_views):
            views_series.append(
                {
                    "label": network_label(network),
                    "values": _values(daily_views),
                    "color": NETWORK_SERIES.get(network, "chart-1"),
                    "network": network,
                }
            )

    def total_kpi(key: str) -> dict[str, Any]:
        value = totals[key] if seen.get(key, True) else None
        prev = totals[key + "_prev"] if state.compare and seen_prev.get(key, True) else None
        return {
            "value": analytics.clean_number(value),
            "previous": analytics.clean_number(prev),
            "change_pct": analytics.change_pct(value, prev) if state.compare else None,
        }

    summary_tiles = [
        tile("followers", "Comunidad total", total_kpi("followers"), "blue"),
        tile("net", "Nuevos seguidores", total_kpi("net"), "green", signed=True),
        tile("posts", "Posts", total_kpi("posts"), "amber"),
        tile("views", "Visualizaciones", total_kpi("views"), "violet"),
        tile("interactions", "Interacciones", total_kpi("interactions"), "teal"),
    ]
    charts = {
        "summary-posts": {
            "type": "bar",
            "labels": labels,
            "stacked": True,
            "integer": True,
            "series": posts_series,
        },
        "summary-views": {"type": "line", "labels": labels, "series": views_series},
    }
    best = analytics.best_times(db, brand.id, None, clock)
    ctx = {
        "summary_rows": rows,
        "summary_tiles": summary_tiles,
        "posts_series": posts_series,
        "views_series": views_series,
        "posts_table": [
            {"day": d, "values": [s["values"][i] for s in posts_series]} for i, d in enumerate(period.dates())
        ],
        "views_table": [
            {"day": d, "values": [s["values"][i] for s in views_series]} for i, d in enumerate(period.dates())
        ],
        "heat": heatmap(best),
        "has_youtube": any(r["native"] for r in rows),
    }
    return ctx, {"charts": charts}


# -------------------------------------------------------------------------------------------- routes


def _base_context(
    request: Request, db: Session, brand: Brand, clock: Any, active: str, tab: str | None
) -> tuple[PageState, dict[str, Any], dict[str, Account]]:
    demo_mode = config.is_demo_mode(db)
    today = analytics.local_today(clock, brand.timezone)
    path = "/estadisticas" if active == "resumen" else f"/estadisticas/{active}"
    state = PageState(request, today, path, tab)
    accounts = brand_accounts(db, brand)
    connected_ids = [a.id for a in accounts.values()]
    status = analytics.refresh_status()
    ctx = {
        **state.context(),
        "sidebar": sidebar_items(accounts, demo_mode, active, state),
        "resumen_href": state.url("/estadisticas", tab=None),
        "export_url": state.url("/estadisticas/exportar.csv", tab=None, comparar=None),
        "active_section": active,
        "last_update": last_update(db, connected_ids),
        "refresh_running": bool(status.get("running")),
        "tz": brand.timezone,
        "network_series": NETWORK_SERIES,
    }
    return state, ctx, accounts


@router.get("/estadisticas")
def summary_page(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    from planificador.templating import render

    state, ctx, accounts = _base_context(request, db, brand, clock, "resumen", None)
    visible = {
        n: a for n, a in accounts.items() if a.status != AccountStatus.DISCONNECTED or has_any_data(db, a)
    }
    if not config.is_demo_mode(db):
        visible.pop("demo", None)
    data: dict[str, Any] = {"charts": {}}
    if visible:
        extra, data = summary_context(db, brand, visible, state, clock)
        ctx.update(extra)
    return render(
        request,
        "analytics/summary.html",
        page_title="Estadísticas",
        has_accounts=bool(visible),
        chart_data=data,
        **ctx,
    )


@router.get("/estadisticas/estado")
def refresh_state() -> JSONResponse:
    status = analytics.refresh_status()
    summary = status.get("summary") or {}
    message = None
    kind = "info"
    if not status.get("running") and status.get("finished_at"):
        if status.get("error"):
            message, kind = "No se pudieron actualizar las estadísticas.", "error"
        elif summary.get("errors"):
            message = (
                f"Estadísticas actualizadas con {summary['errors']} "
                f"{'error' if summary['errors'] == 1 else 'errores'}: revisa los avisos de cada red."
            )
            kind = "warning"
        elif summary.get("accounts"):
            message, kind = "Estadísticas actualizadas.", "success"
        else:
            message, kind = "No hay cuentas conectadas que actualizar.", "info"
    return JSONResponse(
        {
            "running": bool(status.get("running")),
            "started_at": status.get("started_at"),
            "finished_at": status.get("finished_at"),
            "summary": status.get("summary"),
            "message": message,
            "kind": kind,
        },
        headers=NO_STORE,
    )


@router.post("/estadisticas/actualizar")
def refresh_now(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    provider_settings: ProviderSettingsDep,
) -> Response:
    ids = [
        a.id
        for a in db.scalars(
            select(Account).where(Account.brand_id == brand.id, Account.status == AccountStatus.CONNECTED)
        )
    ]
    started = False
    if ids:
        started = analytics.start_background_refresh(
            request.app.state.db.session, clock, provider_settings, account_ids=ids
        )
    running = bool(analytics.refresh_status().get("running"))
    if not ids:
        message = "No hay cuentas conectadas en esta marca."
    elif started:
        message = "Actualizando estadísticas… Tardará unos segundos."
    else:
        message = "Ya se están actualizando las estadísticas."
    if _wants_json(request):
        return JSONResponse(
            {"started": started, "running": running, "accounts": len(ids), "message": message},
            status_code=202 if started else 200,
            headers=NO_STORE,
        )
    from planificador.templating import flash

    flash(request, message, "info" if ids else "warning")
    referer = request.headers.get("referer") or "/estadisticas"
    target = referer if "/estadisticas" in referer else "/estadisticas"
    parts = urlsplit(target)
    return RedirectResponse((parts.path or "/estadisticas") + (f"?{parts.query}" if parts.query else ""), 303)


@router.get("/estadisticas/exportar.csv")
def export_snapshots_csv(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    today = analytics.local_today(clock, brand.timezone)
    period, _ = analytics.resolve_period(
        today,
        request.query_params.get("periodo"),
        request.query_params.get("desde"),
        request.query_params.get("hasta"),
    )
    accounts = [a for n, a in sorted(brand_accounts(db, brand).items(), key=lambda kv: _order(kv[0]))]
    text = analytics.snapshots_csv(db, accounts, period.start, period.end)
    name = f"estadisticas-{_slug(brand.name)}-{period.start.isoformat()}-{period.end.isoformat()}.csv"
    return Response(
        text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"', **NO_STORE},
    )


@router.get("/estadisticas/{account_id}/publicaciones.csv")
def export_posts_csv(account_id: int, request: Request, db: DbSession, clock: ClockDep) -> Response:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "Cuenta no encontrada")
    tz = account.brand.timezone if account.brand is not None else None
    today = analytics.local_today(clock, tz)
    period, _ = analytics.resolve_period(
        today,
        request.query_params.get("periodo"),
        request.query_params.get("desde"),
        request.query_params.get("hasta"),
    )
    network = str(account.network)
    rows = analytics.post_rows(db, account, period.start, period.end)
    text = analytics.posts_csv(rows, network)
    name = f"publicaciones-{network}-{period.start.isoformat()}-{period.end.isoformat()}.csv"
    return Response(
        text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"', **NO_STORE},
    )


def _order(network: str) -> int:
    try:
        return NETWORK_ORDER.index(network)
    except ValueError:
        return len(NETWORK_ORDER)


@router.get("/estadisticas/{network}")
def network_page(
    network: str, request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    from planificador.templating import render

    network = network.lower()
    if network not in NETWORK_ORDER:
        raise HTTPException(404, "Red no encontrada")
    demo_mode = config.is_demo_mode(db)
    accounts = brand_accounts(db, brand)
    if network == "demo" and not demo_mode:
        return RedirectResponse("/estadisticas", 303)
    tab = request.query_params.get("tab") or "comunidad"
    if tab not in dict(TABS):
        tab = "comunidad"
    state, ctx, accounts = _base_context(request, db, brand, clock, network, tab)
    account = accounts.get(network)
    if account is not None and account.status == AccountStatus.DISCONNECTED and not has_any_data(db, account):
        account = None
    data: dict[str, Any] = {"charts": {}}
    has_data = False
    if account is not None:
        has_data = has_any_data(db, account)
        if has_data:
            extra, data = network_context(db, account, state, clock)
            ctx.update(extra)
        meta = account.meta or {}
        ctx["account_last_ok"] = _parse_iso(meta.get("analytics_last_ok"))
    tabs = [{"key": key, "label": label, "href": state.url(tab=key)} for key, label in TABS]
    return render(
        request,
        "analytics/network.html",
        page_title=f"Estadísticas · {network_label(network)}",
        network=network,
        network_name=network_label(network),
        account=account,
        has_data=has_data,
        tab=tab,
        tabs=tabs,
        alerts=alerts_for(account, network),
        chart_data=data,
        **ctx,
    )

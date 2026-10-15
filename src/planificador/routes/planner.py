"""Planificación: Calendario (Mes/Semana/Día), Lista, Ideas board and Papelera (SPEC §1, §15; doc 05 §2, §5c).

Pages (the sub-tabs share ``planner/_subnav.html`` with the brand time zone indicator):

- ``GET /planificacion``         Calendario (FullCalendar, floating brand wall times — SPEC §15)
- ``GET /planificacion/lista``   Lista (table, search, date range, filters, bulk actions)
- ``GET /ideas``                 Ideas (Kanban of undated ``kind=idea`` posts)
- ``GET /papelera``              Papelera (trashed posts, 45-day retention)

JSON / actions:

- ``GET  /api/planificacion/eventos?start&end&q&redes&estados``   calendar feed (wall times, no offset)
- ``GET  /api/planificacion/mejores-horas?red=``                  best-times matrix (7x24, Mon=0) + 5 levels
- ``POST /api/planificacion/publicaciones/{id}/reprogramar``      drag & drop (form field ``start``)
- ``POST /planificacion/zona-horaria``                            change the active brand's time zone
- ``POST /planificacion/lista/acciones``                          bulk ``duplicar`` / ``papelera``
- ``POST /ideas/{id}/etapa`` · ``POST /ideas/{id}/acciones``      Ideas column change / card menu
- ``POST /papelera/{id}/restaurar`` · ``/papelera/{id}/eliminar`` · ``/papelera/vaciar``

Post mutations go through ``postsvc`` (Composer agent), imported lazily so this module loads on its own.
"""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from planificador.deps import ActiveBrand, ClockDep, DbSession
from planificador.models import (
    IDEA_STAGE_LABELS,
    NETWORK_LABELS,
    TARGET_STATUS_LABELS,
    Brand,
    IdeaStage,
    Post,
    PostKind,
    TargetStatus,
)
from planificador.timeutil import (
    DAY_ABBR_ES,
    MONTH_ABBR_ES,
    Clock,
    format_dt_es,
    from_brand_local,
    is_valid_timezone,
    local_day_bounds,
    local_wall_iso,
    parse_wall_iso,
    to_brand_local,
    wall_time_status,
)

log = logging.getLogger(__name__)

router = APIRouter()

# ------------------------------------------------------------------------------------------- constants

#: Sub-tabs: (key, label, href, lucide icon).
PLANNER_TABS: tuple[tuple[str, str, str, str], ...] = (
    ("calendario", "Calendario", "/planificacion", "calendar-days"),
    ("lista", "Lista", "/planificacion/lista", "list"),
    ("ideas", "Ideas", "/ideas", "lightbulb"),
    ("papelera", "Papelera", "/papelera", "trash"),
)

TRASH_RETENTION_DAYS = 45
EXCERPT_CHARS = 60
LIST_EXCERPT_CHARS = 180
#: Networks offered by the planner (filters, "Mejores horas"). Demo is added while demo mode is on.
PLANNER_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube")
ALL_NETWORKS: tuple[str, ...] = (*PLANNER_NETWORKS, "demo")
#: Target statuses that may be moved by drag & drop (SPEC §15 / doc 01 §2.4).
MOVABLE_STATUSES = frozenset({TargetStatus.DRAFT, TargetStatus.SCHEDULED})
#: Status filter options, in display order.
FILTER_STATUSES: tuple[str, ...] = (
    "scheduled",
    "draft",
    "publishing",
    "published",
    "manual_pending",
    "manual_done",
    "sent_to_app",
    "error",
    "missed",
    "unknown",
    "cancelled",
)
IDEA_COLUMNS: tuple[IdeaStage, ...] = (IdeaStage.IDEA, IdeaStage.EN_PROGRESO, IdeaStage.LISTA)
IDEA_COLUMN_HINTS = {
    "idea": "Ocurrencias sin desarrollar",
    "en_progreso": "Textos y medios en preparación",
    "lista": "Listas para programar",
}
BULK_ACTIONS = frozenset({"duplicar", "papelera"})
LIST_DEFAULT_PAST_DAYS = 7
LIST_DEFAULT_FUTURE_DAYS = 30
LIST_MAX_SPAN_DAYS = 400
FEED_MAX_SPAN_DAYS = 62

#: "Mejores horas": 5 shading levels (1 = worst … 5 = best).
BEST_LEVEL_THRESHOLDS = (0.2, 0.4, 0.6, 0.8)
BEST_LEVEL_LABELS = ("Muy baja", "Baja", "Media", "Buena", "Excelente")

#: Time zones offered first in the picker (Spanish-speaking audiences).
COMMON_TIMEZONES: tuple[str, ...] = (
    "Europe/Madrid",
    "Atlantic/Canary",
    "America/Mexico_City",
    "America/Bogota",
    "America/Lima",
    "America/Argentina/Buenos_Aires",
    "America/Santiago",
    "America/Caracas",
    "America/Guatemala",
    "America/Montevideo",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Lisbon",
    "UTC",
)
_TZ_REGIONS = (
    "Africa/",
    "America/",
    "Antarctica/",
    "Asia/",
    "Atlantic/",
    "Australia/",
    "Europe/",
    "Indian/",
    "Pacific/",
)


# --------------------------------------------------------------------------------------------- helpers


class PlannerError(Exception):
    """Invalid planner operation (Spanish message, HTTP status)."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _postsvc() -> Any:
    try:
        from planificador import postsvc
    except ImportError as exc:  # integration not complete: answer with a clear message instead of a 500
        log.error("postsvc is not available: %s", exc)
        raise PlannerError("Esta acción todavía no está disponible en esta versión.", 503) from exc
    return postsvc


def _post_error_types() -> tuple[type[BaseException], ...]:
    try:
        err = _postsvc().PostError
    except Exception:  # noqa: BLE001 - postsvc not available yet
        return (PlannerError,)
    return (PlannerError, err)


def _error_message(exc: BaseException) -> str:
    msg = getattr(exc, "message", None) or (exc.args[0] if exc.args else "")
    return str(msg) or "No se ha podido completar la acción."


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "") and not _is_htmx(request)


def _error_response(request: Request, message: str, status_code: int) -> Response:
    """Text + ``X-Error-Message`` (→ toast in app.js) for htmx; JSON ``{"detail"}`` otherwise."""
    if _is_htmx(request):
        return Response(
            message,
            status_code=status_code,
            media_type="text/plain; charset=utf-8",
            headers={"X-Error-Message": quote(message)},
        )
    return JSONResponse({"detail": message}, status_code=status_code)


def _toast_trigger(message: str, kind: str = "success", **events: Any) -> dict[str, str]:
    payload: dict[str, Any] = {"toast": {"message": message, "kind": kind}}
    payload.update(events)
    return {"HX-Trigger": json.dumps(payload, ensure_ascii=True)}


def brand_tz(brand: Brand) -> str:
    tz = getattr(brand, "timezone", None)
    return tz if is_valid_timezone(tz) else "Europe/Madrid"


def normalize_text(text: str | None) -> str:
    """Case- and accent-insensitive form used by search (``Café`` ~ ``cafe``)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def split_multi(values: list[str] | None, allowed: tuple[str, ...] | None = None) -> list[str]:
    """Repeated and/or comma-separated values → unique list (unknown values dropped)."""
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            item = part.strip().lower()
            if item and item not in out and (allowed is None or item in allowed):
                out.append(item)
    return out


def _network_order(network: str) -> int:
    return ALL_NETWORKS.index(network) if network in ALL_NETWORKS else len(ALL_NETWORKS)


def _post_text(post: Post) -> str:
    if post.text:
        return post.text
    for target in post.targets:
        if target.text_override:
            return target.text_override
    return ""


def _post_networks(post: Post) -> list[str]:
    return sorted({str(t.network) for t in post.targets}, key=_network_order)


def _post_media(post: Post) -> tuple[str | None, str | None, int]:
    """(thumbnail URL, kind, count) of the post's base media; falls back to the first target override."""
    media = [link.media for link in post.media_links if link.media is not None]
    if media:
        first = media[0]
        return f"/medios/{first.id}/miniatura", str(first.kind), len(media)
    for target in post.targets:
        if target.media_override:
            ids = [int(m) for m in target.media_override]
            return f"/medios/{ids[0]}/miniatura", None, len(ids)
    return None, None, 0


def post_status(post: Post) -> str:
    return str(post.display_status)


def is_movable(post: Post) -> bool:
    """Drag & drop is allowed for scheduled-kind posts whose non-cancelled targets are draft/scheduled."""
    if post.kind != PostKind.SCHEDULED or post.deleted_at is not None:
        return False
    active = [t.status for t in post.targets if t.status != TargetStatus.CANCELLED]
    if post.targets and not active:
        return False
    return all(status in MOVABLE_STATUSES for status in active)


def _matches(post: Post, q_norm: str, redes: list[str], estados: list[str]) -> bool:
    if redes and not any(str(t.network) in redes for t in post.targets):
        return False
    if estados:
        statuses = {str(t.status) for t in post.targets} | {post_status(post)}
        if not statuses & set(estados):
            return False
    if q_norm:
        haystack = [post.text, post.notes, *(t.text_override for t in post.targets)]
        if not any(q_norm in normalize_text(h) for h in haystack if h):
            return False
    return True


def _base_query(brand_id: int) -> Any:
    return (
        select(Post)
        .where(Post.brand_id == brand_id)
        .options(selectinload(Post.targets), selectinload(Post.media_links))
    )


def _get_post(db: Session, brand: Brand, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None or post.brand_id != brand.id:
        raise HTTPException(status_code=404, detail="Publicación no encontrada.")
    return post


def _date_label(d: date) -> str:
    return f"{d.day} {MONTH_ABBR_ES[d.month - 1]} {d.year}"


def _hm(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


def range_label(start: date, end: date) -> str:
    """``16 sept 2026 - 15 oct 2026`` (inclusive end)."""
    return f"{_date_label(start)} - {_date_label(end)}"


def _offset_label(offset: timedelta | None) -> str:
    minutes = int((offset or timedelta()).total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


@lru_cache(maxsize=4)
def _timezone_choices_cached(hour_key: str) -> tuple[dict[str, Any], ...]:
    now = datetime.fromisoformat(hour_key)
    regional = sorted(n for n in available_timezones() if n.startswith(_TZ_REGIONS))
    names = list(COMMON_TIMEZONES) + [n for n in regional if n not in COMMON_TIMEZONES]
    out = []
    for name in names:
        try:
            offset = now.astimezone(ZoneInfo(name)).utcoffset()
        except Exception:  # noqa: BLE001 - missing/broken tzdata entry
            continue
        out.append(
            {
                "name": name,
                "label": name.replace("_", " "),
                "offset": _offset_label(offset),
                "common": name in COMMON_TIMEZONES,
            }
        )
    return tuple(out)


def timezone_choices(now: datetime) -> list[dict[str, Any]]:
    """IANA zones for the picker (common ones first) with their current UTC offset."""
    hour_key = now.replace(minute=0, second=0, microsecond=0).isoformat()
    return list(_timezone_choices_cached(hour_key))


def clock_label(now: datetime, tz: str) -> str:
    """``9:18`` — current time in the brand zone (indicator next to the sub-tabs)."""
    local = to_brand_local(now, tz)
    assert local is not None
    return f"{local.hour}:{local.minute:02d}"


def _local_today(clock: Clock, tz: str) -> date:
    local = to_brand_local(clock.now(), tz)
    assert local is not None
    return local.date()


# ------------------------------------------------------------------------------------------ view models


@dataclass
class TargetChip:
    network: str
    status: str
    label: str
    format: str


@dataclass
class PostRow:
    """Template view model shared by Lista, Ideas and Papelera."""

    id: int
    kind: str
    text: str
    excerpt: str
    status: str
    status_label: str
    networks: list[str]
    targets: list[TargetChip]
    thumb_url: str | None
    media_kind: str | None
    media_count: int
    local_dt: datetime | None = None
    date_label: str = ""
    weekday_time: str = ""
    datetime_long: str = ""
    idea_stage: str = "idea"
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    purge_in_days: int | None = None
    movable: bool = False


def build_row(post: Post, tz: str, now: datetime, *, excerpt_chars: int = LIST_EXCERPT_CHARS) -> PostRow:
    text = _post_text(post)
    status = post_status(post)
    thumb, media_kind, media_count = _post_media(post)
    row = PostRow(
        id=post.id,
        kind=str(post.kind),
        text=text,
        excerpt=excerpt(text, excerpt_chars),
        status=status,
        status_label=TARGET_STATUS_LABELS.get(status, status),
        networks=_post_networks(post),
        targets=[
            TargetChip(
                network=str(t.network),
                status=str(t.status),
                label=TARGET_STATUS_LABELS.get(str(t.status), str(t.status)),
                format=t.format,
            )
            for t in sorted(post.targets, key=lambda t: _network_order(str(t.network)))
        ],
        thumb_url=thumb,
        media_kind=media_kind,
        media_count=media_count,
        idea_stage=str(post.idea_stage or IdeaStage.IDEA),
        notes=post.notes,
        created_at=post.created_at,
        updated_at=post.updated_at,
        deleted_at=post.deleted_at,
        movable=is_movable(post),
    )
    if post.scheduled_at is not None:
        local = to_brand_local(post.scheduled_at, tz)
        assert local is not None
        row.local_dt = local
        row.date_label = _date_label(local.date())
        row.weekday_time = f"{DAY_ABBR_ES[local.weekday()]} · {_hm(local)}"
        row.datetime_long = format_dt_es(post.scheduled_at, tz, year=True)
    if post.deleted_at is not None:
        purge_at = post.deleted_at + timedelta(days=TRASH_RETENTION_DAYS)
        row.purge_in_days = max(0, math.ceil((purge_at - now).total_seconds() / 86400))
    return row


#: Visual length of a post in the time grid (a post is an instant; this only sizes the card).
EVENT_SPAN = timedelta(hours=1)


def _event_end(local: datetime | None) -> str | None:
    """Card end: one hour later, but never past midnight — a 23:30 post must not spill into the next day's
    column (FullCalendar would draw it twice)."""
    if local is None:
        return None
    wall = local.replace(tzinfo=None)
    midnight = datetime.combine(wall.date() + timedelta(days=1), datetime.min.time())
    return min(wall + EVENT_SPAN, midnight).isoformat(timespec="seconds")


def event_json(post: Post, tz: str, now: datetime) -> dict[str, Any]:
    """FullCalendar event with a floating brand wall time (SPEC §15)."""
    row = build_row(post, tz, now, excerpt_chars=EXCERPT_CHARS)
    return {
        "id": f"post-{post.id}",
        "title": row.excerpt or "Publicación sin texto",
        "start": local_wall_iso(post.scheduled_at, tz),
        "end": _event_end(row.local_dt),
        "allDay": False,
        "startEditable": row.movable,
        "durationEditable": False,
        "classNames": [f"st-{row.status}"],
        "extendedProps": {
            "postId": post.id,
            "status": row.status,
            "statusLabel": row.status_label,
            "networks": row.networks,
            "targets": [
                {"network": t.network, "status": t.status, "statusLabel": t.label} for t in row.targets
            ],
            "text": row.excerpt,
            "time": _hm(row.local_dt) if row.local_dt else "",
            "thumb": row.thumb_url,
            "mediaKind": row.media_kind,
            "mediaCount": row.media_count,
            "movable": row.movable,
            "hasNotes": bool(post.notes),
            "nativeScheduled": any((t.provider_state or {}).get("native_scheduled") for t in post.targets),
        },
    }


# --------------------------------------------------------------------------------------- best times

# General audience-activity curve (0..1) per local hour, used until the brand has enough own data
# (SPEC §12 "Patrón general"). Deliberately simple: commute/lunch/evening peaks on weekdays, later and
# flatter mornings at weekends, small per-network nudges.
_WEEKDAY_CURVE = (
    0.06, 0.04, 0.03, 0.02, 0.03, 0.07, 0.18, 0.36, 0.52, 0.6, 0.58, 0.62,
    0.72, 0.76, 0.6, 0.54, 0.56, 0.64, 0.76, 0.85, 0.9, 0.8, 0.56, 0.28,
)  # fmt: skip
_WEEKEND_CURVE = (
    0.1, 0.06, 0.04, 0.03, 0.03, 0.04, 0.08, 0.16, 0.3, 0.46, 0.6, 0.68,
    0.72, 0.7, 0.62, 0.56, 0.56, 0.6, 0.68, 0.76, 0.82, 0.76, 0.56, 0.32,
)  # fmt: skip
_DAY_FACTOR = (0.94, 1.0, 1.0, 0.97, 0.9, 0.8, 0.84)
_NETWORK_BOOSTS: dict[str, dict[str, Any]] = {
    "facebook": {"hours": {9: 0.08, 10: 0.08, 11: 0.06, 12: 0.06, 13: 0.05, 22: -0.08, 23: -0.06}},
    "instagram": {"hours": {12: 0.07, 13: 0.07, 19: 0.06, 20: 0.07, 21: 0.05, 8: -0.04}},
    "tiktok": {
        "hours": {18: 0.06, 19: 0.08, 20: 0.1, 21: 0.12, 22: 0.12, 23: 0.06, 8: -0.08, 9: -0.08, 10: -0.06},
        "weekend": 0.1,
    },
    "youtube": {"hours": {15: 0.06, 16: 0.08, 17: 0.08, 18: 0.06, 9: -0.05}, "weekend": 0.12},
}


def default_best_times(network: str | None) -> list[list[float]]:
    """7×24 matrix (Mon=0, brand-local hours) of the general pattern, normalised to 0..1. Uses
    :func:`analytics.general_pattern` (one source of truth with the analytics heatmap), else a local copy."""
    try:
        from planificador import analytics

        matrix = analytics.general_pattern(network)
        if _valid_matrix(matrix):
            return [[max(0.0, min(1.0, float(v))) for v in row] for row in matrix]
    except Exception:  # noqa: BLE001 - analytics unavailable: local pattern
        log.debug("analytics.general_pattern unavailable; using the planner's copy", exc_info=True)
    return _local_best_times(network)


def _local_best_times(network: str | None) -> list[list[float]]:
    boosts = _NETWORK_BOOSTS.get(network or "", {})
    hour_boosts: dict[int, float] = boosts.get("hours", {})
    weekend_bonus = float(boosts.get("weekend", 0.0))
    raw: list[list[float]] = []
    for day in range(7):
        weekend = day >= 5
        curve = _WEEKEND_CURVE if weekend else _WEEKDAY_CURVE
        factor = _DAY_FACTOR[day] + (weekend_bonus if weekend else 0.0)
        raw.append([max(0.0, (curve[h] + hour_boosts.get(h, 0.0)) * factor) for h in range(24)])
    top = max(max(row) for row in raw) or 1.0
    return [[round(min(1.0, v / top), 3) for v in row] for row in raw]


def best_level(score: float) -> int:
    """Score 0..1 → shading level 1..5."""
    return 1 + sum(1 for t in BEST_LEVEL_THRESHOLDS if score >= t)


def _valid_matrix(matrix: Any) -> bool:
    if not isinstance(matrix, list | tuple) or len(matrix) != 7:
        return False
    for row in matrix:
        if not isinstance(row, list | tuple) or len(row) != 24:
            return False
        if not all(isinstance(v, int | float) and not math.isnan(v) for v in row):
            return False
    return True


def best_times_data(db: Session, brand: Brand, network: str | None, clock: Clock) -> dict[str, Any]:
    """``analytics.best_times`` when available and well formed, else the general pattern."""
    matrix: list[list[float]] | None = None
    source = "patron_general"
    posts = 0
    try:
        from planificador import analytics

        result = analytics.best_times(db, brand.id, network, clock)
        if isinstance(result, dict) and _valid_matrix(result.get("matrix")):
            matrix = [[max(0.0, min(1.0, float(v))) for v in row] for row in result["matrix"]]
            source = "datos" if result.get("source") == "datos" else "patron_general"
            posts = int(result.get("posts") or 0)
    except Exception as exc:  # noqa: BLE001 - analytics not implemented yet or failing: use the pattern
        log.debug("best_times unavailable (%s); using the general pattern", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    if matrix is None:
        matrix = default_best_times(network)
        source, posts = "patron_general", 0
    if source == "datos":
        source_label = f"Según el rendimiento de tus publicaciones ({posts})"
    else:
        source_label = "Patrón general: aún no hay suficientes publicaciones propias"
    return {
        "network": network,
        "label": NETWORK_LABELS.get(network, network) if network else "Todas las redes",
        "source": source,
        "sourceLabel": source_label,
        "posts": posts,
        "matrix": matrix,
        "levels": [[best_level(v) for v in row] for row in matrix],
        "levelLabels": list(BEST_LEVEL_LABELS),
    }


# ------------------------------------------------------------------------------------------ contexts


def templating_render(request: Request, name: str, **ctx: Any) -> Response:
    from planificador.templating import render

    return render(request, name, **ctx)


def _networks_for(db: Session) -> list[str]:
    from planificador import config

    nets = list(PLANNER_NETWORKS)
    if config.is_demo_mode(db):
        nets.append("demo")
    return nets


def _connected_networks(db: Session, brand: Brand) -> list[str]:
    from planificador.models import Account, AccountStatus

    rows = db.scalars(
        select(Account.network).where(
            Account.brand_id == brand.id, Account.status != AccountStatus.DISCONNECTED
        )
    )
    return sorted({str(n) for n in rows}, key=_network_order)


def _tab_counts(db: Session, brand: Brand) -> dict[str, int]:
    ideas = db.scalar(
        select(func.count())
        .select_from(Post)
        .where(Post.brand_id == brand.id, Post.kind == PostKind.IDEA, Post.deleted_at.is_(None))
    )
    trash = db.scalar(
        select(func.count()).select_from(Post).where(Post.brand_id == brand.id, Post.deleted_at.is_not(None))
    )
    return {"ideas": int(ideas or 0), "papelera": int(trash or 0)}


def _page_ctx(db: Session, brand: Brand, clock: Clock, tab: str) -> dict[str, Any]:
    tz = brand_tz(brand)
    now = clock.now()
    counts = _tab_counts(db, brand)
    tabs = [
        {"key": key, "label": label, "href": href, "icon": ic, "count": counts.get(key) or None}
        for key, label, href, ic in PLANNER_TABS
    ]
    return {
        "page_title": "Planificación",
        "planner_tab": tab,
        "planner_tabs": tabs,
        "tz": tz,
        "tz_clock": clock_label(now, tz),
        "planner_networks": _networks_for(db),
        "brand_networks_connected": _connected_networks(db, brand),
        "filter_statuses": [(s, TARGET_STATUS_LABELS[s]) for s in FILTER_STATUSES],
        "today_local": _local_today(clock, tz),
    }


# ---------------------------------------------------------------------------------------------- pages


@router.get("/planificacion")
def planner_page(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    vista: str | None = None,
    fecha: str | None = None,
) -> Response:
    ctx = _page_ctx(db, brand, clock, "calendario")
    views = {"mes": "dayGridMonth", "semana": "timeGridWeek", "dia": "timeGridDay"}
    initial_date = ctx["today_local"].isoformat()
    if fecha:
        try:
            initial_date = date.fromisoformat(fecha[:10]).isoformat()
        except ValueError:
            pass
    planner_config = {
        "tz": ctx["tz"],
        "initialView": views.get(vista or "", ""),
        "initialDate": initial_date,
        "eventsUrl": "/api/planificacion/eventos",
        "bestTimesUrl": "/api/planificacion/mejores-horas",
        "rescheduleUrl": "/api/planificacion/publicaciones/{id}/reprogramar",
        "detailUrl": "/publicaciones/{id}",
        "composerUrl": "/publicaciones/nueva",
        "statusLabels": {str(k): v for k, v in TARGET_STATUS_LABELS.items()},
        "networkLabels": {str(k): v for k, v in NETWORK_LABELS.items()},
        "bestLevelLabels": list(BEST_LEVEL_LABELS),
        "defaultBest": next(
            (
                n
                for n in ("instagram", "facebook", "tiktok", "youtube")
                if n in ctx["brand_networks_connected"]
            ),
            "",
        ),
    }
    return templating_render(request, "planner/calendar.html", planner_config=planner_config, **ctx)


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def list_range(desde: str | None, hasta: str | None, today: date) -> tuple[date, date]:
    """Inclusive local date range of the Lista tab (default: last week + next 30 days)."""
    start = _parse_day(desde) or today - timedelta(days=LIST_DEFAULT_PAST_DAYS)
    end = _parse_day(hasta) or today + timedelta(days=LIST_DEFAULT_FUTURE_DAYS)
    if end < start:
        start, end = end, start
    if (end - start).days > LIST_MAX_SPAN_DAYS:
        end = start + timedelta(days=LIST_MAX_SPAN_DAYS)
    return start, end


def _list_rows(
    db: Session,
    brand: Brand,
    clock: Clock,
    start: date,
    end: date,
    q: str,
    redes: list[str],
    estados: list[str],
) -> list[PostRow]:
    tz = brand_tz(brand)
    start_utc = local_day_bounds(start, tz)[0]
    end_utc = local_day_bounds(end, tz)[1]
    in_range = (Post.scheduled_at >= start_utc) & (Post.scheduled_at < end_utc)
    # Undated drafts ("Sin fecha") are pending work: listed whenever the range covers today.
    if start <= _local_today(clock, tz) <= end:
        in_range = Post.scheduled_at.is_(None) | in_range
    stmt = _base_query(brand.id).where(Post.deleted_at.is_(None), Post.kind == PostKind.SCHEDULED, in_range)
    q_norm = normalize_text(q)
    posts = [p for p in db.scalars(stmt) if _matches(p, q_norm, redes, estados)]
    # Undated drafts first ("Sin fecha"), then chronological.
    posts.sort(key=lambda p: (p.scheduled_at is not None, p.scheduled_at or p.created_at, p.id))
    now = clock.now()
    return [build_row(p, tz, now) for p in posts]


def _list_ctx(
    db: Session,
    brand: Brand,
    clock: Clock,
    *,
    q: str | None,
    desde: str | None,
    hasta: str | None,
    redes: list[str] | None,
    estados: list[str] | None,
) -> dict[str, Any]:
    tz = brand_tz(brand)
    start, end = list_range(desde, hasta, _local_today(clock, tz))
    nets = split_multi(redes, ALL_NETWORKS)
    states = split_multi(estados, FILTER_STATUSES)
    query = (q or "").strip()[:200]
    rows = _list_rows(db, brand, clock, start, end, query, nets, states)
    params: list[tuple[str, str]] = [("desde", start.isoformat()), ("hasta", end.isoformat())]
    if query:
        params.append(("q", query))
    params += [("redes", n) for n in nets] + [("estados", s) for s in states]
    return {
        "rows": rows,
        "q": query,
        "desde": start.isoformat(),
        "hasta": end.isoformat(),
        "range_label": range_label(start, end),
        "sel_redes": nets,
        "sel_estados": states,
        "filter_count": len(nets) + len(states),
        "has_filters": bool(query or nets or states),
        "list_query": urlencode(params),
        "tz": tz,
    }


@router.get("/planificacion/lista")
def list_page(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    q: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    redes: Annotated[list[str] | None, Query()] = None,
    estados: Annotated[list[str] | None, Query()] = None,
) -> Response:
    lctx = _list_ctx(db, brand, clock, q=q, desde=desde, hasta=hasta, redes=redes, estados=estados)
    if _is_htmx(request) and request.headers.get("hx-target") == "plan-list":
        return templating_render(request, "planner/_list_body.html", **lctx)
    ctx = _page_ctx(db, brand, clock, "lista")
    ctx.update(lctx)
    return templating_render(request, "planner/list.html", **ctx)


def _ideas_ctx(db: Session, brand: Brand, clock: Clock) -> dict[str, Any]:
    tz = brand_tz(brand)
    now = clock.now()
    stmt = (
        _base_query(brand.id)
        .where(Post.deleted_at.is_(None), Post.kind == PostKind.IDEA)
        .order_by(Post.updated_at.desc(), Post.id.desc())
    )
    columns: dict[str, list[PostRow]] = {str(stage): [] for stage in IDEA_COLUMNS}
    for post in db.scalars(stmt):
        row = build_row(post, tz, now, excerpt_chars=220)
        columns.get(row.idea_stage, columns["idea"]).append(row)
    return {
        "idea_columns": [
            {
                "key": str(stage),
                "label": IDEA_STAGE_LABELS[stage],
                "hint": IDEA_COLUMN_HINTS[str(stage)],
                "rows": columns[str(stage)],
            }
            for stage in IDEA_COLUMNS
        ],
        "idea_total": sum(len(v) for v in columns.values()),
        "idea_stage_labels": {str(k): v for k, v in IDEA_STAGE_LABELS.items()},
    }


@router.get("/ideas")
def ideas_page(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    ictx = _ideas_ctx(db, brand, clock)
    if _is_htmx(request) and request.headers.get("hx-target") == "ideas-board":
        return templating_render(request, "planner/_ideas_board.html", **ictx)
    ctx = _page_ctx(db, brand, clock, "ideas")
    ctx.update(ictx)
    return templating_render(request, "planner/ideas.html", **ctx)


def _trash_ctx(db: Session, brand: Brand, clock: Clock) -> dict[str, Any]:
    tz = brand_tz(brand)
    now = clock.now()
    stmt = (
        _base_query(brand.id)
        .where(Post.deleted_at.is_not(None))
        .order_by(Post.deleted_at.desc(), Post.id.desc())
    )
    return {"rows": [build_row(p, tz, now) for p in db.scalars(stmt)], "retention_days": TRASH_RETENTION_DAYS}


@router.get("/papelera")
def trash_page(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    tctx = _trash_ctx(db, brand, clock)
    if _is_htmx(request) and request.headers.get("hx-target") == "trash-list":
        return templating_render(request, "planner/_trash_body.html", **tctx)
    ctx = _page_ctx(db, brand, clock, "papelera")
    ctx.update(tctx)
    return templating_render(request, "planner/trash.html", **ctx)


# --------------------------------------------------------------------------------------------- API


def _parse_feed_bound(value: str | None, tz: str, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        return from_brand_local(parse_wall_iso(value), tz)
    except ValueError:
        raise HTTPException(status_code=422, detail="Rango de fechas no válido.") from None


@router.get("/api/planificacion/eventos")
def events_feed(
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    start: str | None = None,
    end: str | None = None,
    q: str | None = None,
    redes: Annotated[list[str] | None, Query()] = None,
    estados: Annotated[list[str] | None, Query()] = None,
) -> JSONResponse:
    """Posts of the active brand in [start, end). Bounds and event starts are brand wall times: FullCalendar
    runs with ``timeZone: 'UTC'``, so a trailing ``Z`` on the bounds is ignored (SPEC §15)."""
    tz = brand_tz(brand)
    now = clock.now()
    today_start = local_day_bounds(_local_today(clock, tz), tz)[0]
    start_utc = _parse_feed_bound(start, tz, today_start - timedelta(days=7))
    end_utc = _parse_feed_bound(end, tz, start_utc + timedelta(days=14))
    if end_utc <= start_utc:
        raise HTTPException(status_code=422, detail="Rango de fechas no válido.")
    end_utc = min(end_utc, start_utc + timedelta(days=FEED_MAX_SPAN_DAYS))
    stmt = (
        _base_query(brand.id)
        .where(
            Post.deleted_at.is_(None),
            Post.kind == PostKind.SCHEDULED,
            Post.scheduled_at.is_not(None),
            Post.scheduled_at >= start_utc,
            Post.scheduled_at < end_utc,
        )
        .order_by(Post.scheduled_at, Post.id)
    )
    nets = split_multi(redes, ALL_NETWORKS)
    states = split_multi(estados, FILTER_STATUSES)
    q_norm = normalize_text((q or "").strip()[:200])
    events = [event_json(p, tz, now) for p in db.scalars(stmt) if _matches(p, q_norm, nets, states)]
    return JSONResponse(events, headers={"Cache-Control": "no-store"})


@router.get("/api/planificacion/zonas-horarias")
def timezones_api(clock: ClockDep) -> JSONResponse:
    """Choices for the time zone picker (loaded when the dropdown opens)."""
    return JSONResponse(timezone_choices(clock.now()), headers={"Cache-Control": "max-age=600"})


@router.get("/api/planificacion/mejores-horas")
def best_times_api(
    db: DbSession, brand: ActiveBrand, clock: ClockDep, red: str | None = None
) -> JSONResponse:
    network = (red or "").strip().lower() or None
    if network in ("todas", "all"):
        network = None
    if network is not None and network not in ALL_NETWORKS:
        raise HTTPException(status_code=422, detail="Red social no válida.")
    return JSONResponse(best_times_data(db, brand, network, clock), headers={"Cache-Control": "no-store"})


@router.post("/api/planificacion/publicaciones/{post_id:int}/reprogramar")
def reschedule(
    request: Request,
    post_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    start: Annotated[str, Form()] = "",
) -> Response:
    """Drag & drop: ``start`` is the new brand wall time (floating ISO). Validated here and in postsvc."""
    tz = brand_tz(brand)
    post = _get_post(db, brand, post_id)
    if post.deleted_at is not None or post.kind != PostKind.SCHEDULED:
        return _error_response(request, "Esta publicación ya no está en el calendario.", 404)
    try:
        wall = parse_wall_iso(start)
    except ValueError:
        return _error_response(request, "La fecha indicada no es válida.", 422)
    new_at = from_brand_local(wall, tz)
    if new_at <= clock.now():
        return _error_response(request, "No se puede mover una publicación a una fecha pasada.", 422)
    if not is_movable(post):
        return _error_response(
            request,
            "Solo se pueden mover publicaciones programadas o en borrador. "
            "Si quieres volver a publicarla, duplícala.",
            409,
        )
    try:
        updated = _postsvc().reschedule_post(db, post.id, new_at, clock)
        db.commit()
    except _post_error_types() as exc:
        db.rollback()
        return _error_response(request, _error_message(exc), getattr(exc, "status_code", 409))
    post = updated if isinstance(updated, Post) else post
    db.refresh(post)
    message = f"Publicación movida al {format_dt_es(post.scheduled_at or new_at, tz)}."
    if wall_time_status(wall, tz) == "nonexistent":
        message += " Esa hora no existe por el cambio de horario; se ha ajustado automáticamente."
    return JSONResponse({"ok": True, "message": message, "event": event_json(post, tz, clock.now())})


@router.post("/planificacion/zona-horaria")
def change_timezone(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    timezone: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/planificacion",
) -> Response:
    from planificador.security import safe_next_path
    from planificador.templating import flash

    name = timezone.strip()
    if not is_valid_timezone(name) or not (name == "UTC" or name.startswith(_TZ_REGIONS)):
        return _error_response(request, "Zona horaria no válida.", 422)
    changed = brand.timezone != name
    brand.timezone = name
    db.commit()
    if changed:
        flash(request, f"Zona horaria de «{brand.name}»: {name.replace('_', ' ')}.", "success")
    if _wants_json(request):
        return JSONResponse({"ok": True, "timezone": name})
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(safe_next_path(next, "/planificacion"), status_code=303)


@router.post("/planificacion/lista/acciones")
def list_bulk_action(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    accion: Annotated[str, Form()] = "",
    ids: Annotated[list[int] | None, Form()] = None,
    q: Annotated[str | None, Form()] = None,
    desde: Annotated[str | None, Form()] = None,
    hasta: Annotated[str | None, Form()] = None,
    redes: Annotated[list[str] | None, Form()] = None,
    estados: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Bulk (or single-row menu) actions of the Lista tab; answers the refreshed list + a toast."""
    action = accion.strip().lower()
    if action not in BULK_ACTIONS:
        return _error_response(request, "Acción no válida.", 422)
    wanted = list(dict.fromkeys(ids or []))
    if not wanted:
        return _error_response(request, "Selecciona al menos una publicación.", 422)
    posts = [db.get(Post, pid) for pid in wanted]
    if any(p is None or p.brand_id != brand.id or p.deleted_at is not None for p in posts):
        return _error_response(request, "Alguna de las publicaciones ya no existe.", 404)
    try:
        svc = _postsvc()
    except PlannerError as exc:
        return _error_response(request, exc.message, exc.status_code)
    done = 0
    errors: list[str] = []
    for pid in wanted:
        try:
            if action == "duplicar":
                svc.duplicate_post(db, pid, clock)
            else:
                svc.delete_post(db, pid, clock)
            db.commit()
            done += 1
        except _post_error_types() as exc:
            db.rollback()
            errors.append(_error_message(exc))
    db.expire_all()
    if action == "duplicar":
        message = "Publicación duplicada." if done == 1 else f"{done} publicaciones duplicadas."
    else:
        message = (
            "Publicación movida a la papelera."
            if done == 1
            else f"{done} publicaciones movidas a la papelera."
        )
    kind = "success"
    if errors:
        kind = "warning" if done else "error"
        message = (message + " " if done else "") + errors[0]
    if _wants_json(request):
        return JSONResponse({"ok": not errors, "done": done, "errors": errors, "message": message})
    lctx = _list_ctx(db, brand, clock, q=q, desde=desde, hasta=hasta, redes=redes, estados=estados)
    response = templating_render(request, "planner/_list_body.html", **lctx)
    response.headers.update(_toast_trigger(message, kind, **{"posts:changed": True}))
    return response


@router.post("/ideas/{post_id:int}/etapa")
def idea_stage(
    request: Request,
    post_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    etapa: Annotated[str, Form()] = "",
) -> Response:
    """Move an idea between the board columns (drag & drop or the card menu)."""
    post = _get_post(db, brand, post_id)
    if post.deleted_at is not None or post.kind != PostKind.IDEA:
        return _error_response(request, "Esta idea ya no está en el tablero.", 404)
    try:
        stage = IdeaStage(etapa.strip().lower())
    except ValueError:
        return _error_response(request, "Columna no válida.", 422)
    post.idea_stage = stage
    post.updated_at = clock.now()
    db.commit()
    label = IDEA_STAGE_LABELS[stage]
    message = f"Idea movida a «{label}»."
    if _wants_json(request):
        return JSONResponse({"ok": True, "stage": str(stage), "label": label, "message": message})
    response = templating_render(request, "planner/_ideas_board.html", **_ideas_ctx(db, brand, clock))
    response.headers.update(_toast_trigger(message))
    return response


@router.post("/ideas/{post_id:int}/acciones")
def idea_action(
    request: Request,
    post_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    accion: Annotated[str, Form()] = "",
) -> Response:
    """Card menu on the Ideas board: ``duplicar`` / ``papelera``."""
    post = _get_post(db, brand, post_id)
    action = accion.strip().lower()
    if action not in BULK_ACTIONS:
        return _error_response(request, "Acción no válida.", 422)
    if post.deleted_at is not None:
        return _error_response(request, "Esta idea ya no está en el tablero.", 404)
    try:
        if action == "duplicar":
            _postsvc().duplicate_post(db, post.id, clock)
            message = "Idea duplicada."
        else:
            _postsvc().delete_post(db, post.id, clock)
            message = "Idea movida a la papelera."
        db.commit()
    except _post_error_types() as exc:
        db.rollback()
        return _error_response(request, _error_message(exc), getattr(exc, "status_code", 409))
    db.expire_all()
    if _wants_json(request):
        return JSONResponse({"ok": True, "message": message})
    response = templating_render(request, "planner/_ideas_board.html", **_ideas_ctx(db, brand, clock))
    response.headers.update(_toast_trigger(message, **{"posts:changed": True}))
    return response


def _trash_response(request: Request, db: Session, brand: Brand, clock: Clock, message: str) -> Response:
    if _wants_json(request):
        return JSONResponse({"ok": True, "message": message})
    response = templating_render(request, "planner/_trash_body.html", **_trash_ctx(db, brand, clock))
    response.headers.update(_toast_trigger(message, **{"posts:changed": True}))
    return response


@router.post("/papelera/{post_id:int}/restaurar")
def trash_restore(
    request: Request, post_id: int, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    post = _get_post(db, brand, post_id)
    if post.deleted_at is None:
        return _error_response(request, "Esta publicación no está en la papelera.", 404)
    is_idea = post.kind == PostKind.IDEA
    try:
        _postsvc().restore_post(db, post.id, clock)
        db.commit()
    except _post_error_types() as exc:
        db.rollback()
        return _error_response(request, _error_message(exc), getattr(exc, "status_code", 409))
    db.expire_all()
    where = "Ideas" if is_idea else "el calendario"
    return _trash_response(request, db, brand, clock, f"Publicación restaurada en {where}.")


def purge_post(db: Session, post: Post) -> None:
    """Delete a trashed post for good (targets, events and media links cascade; library media stay)."""
    if post.deleted_at is None:
        raise PlannerError("Solo se pueden eliminar definitivamente publicaciones de la papelera.", 409)
    db.delete(post)


@router.post("/papelera/{post_id:int}/eliminar")
def trash_purge(
    request: Request, post_id: int, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    post = _get_post(db, brand, post_id)
    if post.deleted_at is None:
        return _error_response(request, "Esta publicación no está en la papelera.", 404)
    purge_post(db, post)
    db.commit()
    return _trash_response(request, db, brand, clock, "Publicación eliminada definitivamente.")


@router.post("/papelera/vaciar")
def trash_empty(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    posts = list(db.scalars(select(Post).where(Post.brand_id == brand.id, Post.deleted_at.is_not(None))))
    for post in posts:
        purge_post(db, post)
    db.commit()
    if not posts:
        message = "La papelera ya estaba vacía."
    elif len(posts) == 1:
        message = "Se ha eliminado 1 publicación definitivamente."
    else:
        message = f"Se han eliminado {len(posts)} publicaciones definitivamente."
    return _trash_response(request, db, brand, clock, message)

"""Notification centre + top-bar bell (SPEC §11).

- ``GET  /notificaciones``                 notification centre (``?filtro=todas|no-leidas``, ``?tipo=``,
  ``?antes=<id>`` → next page). htmx requests aimed at ``#notif-page-list`` (or with ``antes``) get the list
  fragment only.
- ``GET  /api/notificaciones/resumen``     bell summary, polled every 30 s by the top bar: unread badge + the
  latest notifications (swapped out of band into ``#notif-list``) + the payload for browser notifications.
- ``POST /notificaciones/{id}/leer``       mark one as read (htmx: the item re-rendered; ``Accept: json``:
  ``{"ok": true}``; plain form: redirect back).
- ``POST /notificaciones/{id}/no-leida``   mark one as unread again.
- ``POST /notificaciones/leer-todas``      mark every notification as read.

Events (HX-Trigger): ``notifications:refresh`` (bell badge reloads) after every change, plus
``notifications:changed`` after "mark all" (the centre's list reloads).

Deep links (``notifications/_macros.html``): ``manual_publish`` → "Publicación asistida" modal
(``/objetivos/{id}/asistida``); ``error``/``unknown``/``missed`` (and any other with a target) → post detail
drawer (``/objetivos/{id}``); ``reconnect`` → ``/conexiones``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from planificador.deps import ClockDep, DbSession
from planificador.models import Notification, PostTarget
from planificador.timeutil import format_date_es, get_tz

log = logging.getLogger(__name__)

router = APIRouter()

#: How many notifications the bell dropdown lists.
SUMMARY_LIMIT = 8
#: Page size of the notification centre ("Cargar más" loads the next page).
PAGE_SIZE = 30

#: ``?filtro=`` values → label (sub-tabs).
FILTERS: dict[str, str] = {"todas": "Todas", "no-leidas": "No leídas"}
#: ``?tipo=`` values → (label, notification kinds).
KIND_FILTERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "": ("Todos los tipos", ()),
    "asistida": ("Publicación asistida", ("manual_publish",)),
    "errores": ("Errores", ("error", "unknown", "missed")),
    "conexiones": ("Conexiones", ("reconnect",)),
    "info": ("Información", ("info",)),
}

NO_STORE = {"Cache-Control": "no-store"}


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def unread_count(db: Session) -> int:
    count = db.scalar(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None)))
    return int(count or 0)


def _with_context(stmt: Any) -> Any:
    """Eager-load what the items show (target → post/account, account)."""
    return stmt.options(
        selectinload(Notification.target).selectinload(PostTarget.post),
        selectinload(Notification.target).selectinload(PostTarget.account),
        selectinload(Notification.account),
    )


def display_tz(db: Session) -> str:
    """Active brand's time zone (falls back to the app default)."""
    try:
        from planificador import config
        from planificador.deps import ensure_active_brand

        brand = ensure_active_brand(db)
        return brand.timezone or config.default_timezone(db)
    except Exception:  # noqa: BLE001 - display only
        log.debug("Could not resolve the display time zone", exc_info=True)
        return "Europe/Madrid"


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Hoy"
    if day == today - timedelta(days=1):
        return "Ayer"
    text = format_date_es(day, year=day.year != today.year)
    return text[:1].upper() + text[1:]


def group_by_day(items: list[Notification], tz: str, now: datetime) -> list[tuple[str, list[Notification]]]:
    """``[(label, items)]`` in the given order; labels "Hoy", "Ayer", "Lunes 2 de noviembre"…"""
    zone = get_tz(tz)
    today = now.astimezone(zone).date()
    groups: list[tuple[str, list[Notification]]] = []
    current: date | None = None
    for item in items:
        day = item.created_at.astimezone(zone).date()
        if day != current:
            groups.append((_day_label(day, today), []))
            current = day
        groups[-1][1].append(item)
    return groups


def _kinds(tipo: str) -> tuple[str, ...]:
    return KIND_FILTERS.get(tipo, ("", ()))[1]


def _list_query(db: Session, filtro: str, tipo: str, antes: int | None) -> Any:
    """Newest first (``created_at``, then ``id``); ``antes`` = id of the last item already shown."""
    stmt = select(Notification)
    if filtro == "no-leidas":
        stmt = stmt.where(Notification.read_at.is_(None))
    if _kinds(tipo):
        stmt = stmt.where(Notification.kind.in_(_kinds(tipo)))
    if antes is not None:
        cursor = db.get(Notification, antes)
        if cursor is None:
            stmt = stmt.where(Notification.id < antes)
        else:
            stmt = stmt.where(
                or_(
                    Notification.created_at < cursor.created_at,
                    and_(Notification.created_at == cursor.created_at, Notification.id < cursor.id),
                )
            )
    order = (Notification.created_at.desc(), Notification.id.desc())
    return _with_context(stmt.order_by(*order).limit(PAGE_SIZE + 1))


def _counts(db: Session, tipo: str) -> dict[str, int]:
    base = select(func.count()).select_from(Notification)
    if _kinds(tipo):
        base = base.where(Notification.kind.in_(_kinds(tipo)))
    return {
        "todas": int(db.scalar(base) or 0),
        "no-leidas": int(db.scalar(base.where(Notification.read_at.is_(None))) or 0),
    }


@router.get("/notificaciones")
def notifications_page(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    filtro: str = "todas",
    tipo: str = "",
    antes: Annotated[int | None, Query()] = None,
) -> Response:
    from planificador.templating import render

    filtro = filtro if filtro in FILTERS else "todas"
    tipo = tipo if tipo in KIND_FILTERS else ""
    rows = list(db.scalars(_list_query(db, filtro, tipo, antes)))
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    tz = display_tz(db)
    ctx: dict[str, Any] = {
        "page_title": "Notificaciones",
        "groups": group_by_day(rows, tz, clock.now()),
        "has_more": has_more,
        "next_before": rows[-1].id if rows else None,
        "filtro": filtro,
        "tipo": tipo,
        "filters": FILTERS,
        "kind_filters": {key: value[0] for key, value in KIND_FILTERS.items()},
        "counts": _counts(db, tipo),
        "tz": tz,
        "appending": antes is not None,
    }
    if _is_htmx(request) and (antes is not None or request.headers.get("hx-target") == "notif-page-list"):
        return render(request, "notifications/_list.html", headers=NO_STORE, **ctx)
    return render(request, "notifications/index.html", headers=NO_STORE, **ctx)


@router.get("/api/notificaciones/resumen")
def notifications_summary(request: Request, db: DbSession) -> Response:
    from planificador.templating import render

    unread = unread_count(db)
    items = list(
        db.scalars(
            _with_context(
                select(Notification)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .limit(SUMMARY_LIMIT)
            )
        )
    )
    return render(
        request,
        "partials/notif_summary.html",
        unread=unread,
        unread_notifications=unread,
        items=items,
        tz=display_tz(db),
        headers=NO_STORE,
    )


def _get(db: Session, notification_id: int) -> Notification:
    stmt = _with_context(select(Notification).where(Notification.id == notification_id))
    notification = db.scalars(stmt).first()
    if notification is None:
        raise HTTPException(404, "La notificación ya no existe.")
    return notification


def _back(request: Request) -> str:
    """Same-origin path to return to after a plain form POST (never an absolute URL)."""
    referer = request.headers.get("referer") or ""
    parts = urlsplit(referer)
    if parts.netloc and parts.netloc == request.url.netloc and parts.path.startswith("/"):
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/notificaciones"


def _item_response(request: Request, db: Session, notification: Notification, message: str) -> Response:
    from planificador.templating import flash, render

    headers = {"HX-Trigger": "notifications:refresh", **NO_STORE}
    if _wants_json(request):
        payload = {"ok": True, "id": notification.id, "read": notification.read_at is not None}
        return JSONResponse(payload, headers=headers)
    if _is_htmx(request):
        return render(
            request, "notifications/_item_response.html", n=notification, tz=display_tz(db), headers=headers
        )
    flash(request, message, "success")
    return RedirectResponse(_back(request), status_code=303)


@router.post("/notificaciones/{notification_id:int}/leer")
def mark_read(request: Request, notification_id: int, db: DbSession, clock: ClockDep) -> Response:
    notification = _get(db, notification_id)
    if notification.read_at is None:
        notification.read_at = clock.now()
        db.commit()
    return _item_response(request, db, notification, "Notificación marcada como leída.")


@router.post("/notificaciones/{notification_id:int}/no-leida")
def mark_unread(request: Request, notification_id: int, db: DbSession) -> Response:
    notification = _get(db, notification_id)
    if notification.read_at is not None:
        notification.read_at = None
        db.commit()
    return _item_response(request, db, notification, "Notificación marcada como no leída.")


@router.post("/notificaciones/leer-todas")
def mark_all_read(request: Request, db: DbSession, clock: ClockDep) -> Response:
    from planificador.templating import flash, toast_headers

    result = db.execute(
        update(Notification)
        .where(Notification.read_at.is_(None))
        .values(read_at=clock.now())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    changed = int(result.rowcount or 0)
    if not changed:
        message = "No había notificaciones sin leer."
    elif changed == 1:
        message = "1 notificación marcada como leída."
    else:
        message = f"{changed} notificaciones marcadas como leídas."
    events = {"notifications:refresh": True, "notifications:changed": True}
    if _wants_json(request):
        return JSONResponse(
            {"ok": True, "changed": changed}, headers=toast_headers(message, "success", **events)
        )
    if _is_htmx(request):
        return Response(
            status_code=200, headers=toast_headers(message, "success" if changed else "info", **events)
        )
    flash(request, message, "success")
    return RedirectResponse(_back(request), status_code=303)

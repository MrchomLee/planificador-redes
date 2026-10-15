"""Notification center + bell summary (SPEC §11). The page is a SKELETON — owner replaces it.

``GET /api/notificaciones/resumen`` is the top-bar bell endpoint (polled every 30 s by htmx): it renders
``partials/notif_summary.html`` with the unread count and the latest notifications (the template swaps the
list out of band into ``#notif-list``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from planificador.deps import DbSession
from planificador.models import Notification
from planificador.routes import render_placeholder

router = APIRouter()

#: How many notifications the bell dropdown lists.
SUMMARY_LIMIT = 8


@router.get("/notificaciones")
def notifications_page(request: Request) -> Response:
    return render_placeholder(request, "Notificaciones")


@router.get("/api/notificaciones/resumen")
def notifications_summary(request: Request, db: DbSession) -> Response:
    from planificador.templating import render

    unread = db.scalar(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None)))
    items = list(
        db.scalars(
            select(Notification)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(SUMMARY_LIMIT)
        )
    )
    return render(
        request,
        "partials/notif_summary.html",
        unread=int(unread or 0),
        unread_notifications=int(unread or 0),
        items=items,
        headers={"Cache-Control": "no-store"},
    )

"""Post detail / target actions (retry, mark published, cancel, asistida modal). SKELETON."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/publicaciones/{post_id:int}")
def post_detail(request: Request, post_id: int) -> Response:
    return render_placeholder(request, "Publicación")


@router.get("/objetivos/{target_id:int}/asistida")
def assisted_publish(request: Request, target_id: int) -> Response:
    # Opened by the bell (manual_publish notifications) with hx-target="#modal-root" (SPEC §11).
    return render_placeholder(request, "Publicación asistida")

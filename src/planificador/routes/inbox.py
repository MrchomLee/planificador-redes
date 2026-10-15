"""Comments inbox ("Comentarios", SPEC §13). SKELETON — owner replaces it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/comentarios")
def inbox_page(request: Request) -> Response:
    return render_placeholder(request, "Comentarios")

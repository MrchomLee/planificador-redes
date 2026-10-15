"""Analytics ("Estadísticas", SPEC §12). SKELETON — owner replaces it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/estadisticas")
def analytics_page(request: Request) -> Response:
    return render_placeholder(request, "Estadísticas")

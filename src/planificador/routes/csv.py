"""CSV import/export ("Importar CSV / Exportar CSV", SPEC §14). SKELETON — owner replaces it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/importar")
def csv_page(request: Request) -> Response:
    return render_placeholder(request, "Importar o exportar CSV")

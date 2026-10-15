"""Connected accounts + OAuth flows ("Cuentas conectadas", SPEC §6). SKELETON."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/conexiones")
def connections_page(request: Request) -> Response:
    return render_placeholder(request, "Cuentas conectadas")

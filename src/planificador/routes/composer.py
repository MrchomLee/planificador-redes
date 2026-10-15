"""Composer ("Nueva publicación", SPEC §1, §7). SKELETON — owner replaces it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/publicaciones/nueva")
def composer_page(request: Request) -> Response:
    return render_placeholder(request, "Nueva publicación")

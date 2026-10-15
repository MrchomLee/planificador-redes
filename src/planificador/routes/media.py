"""Media library ("Biblioteca", SPEC §8). SKELETON — owner replaces it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/biblioteca")
def library_page(request: Request) -> Response:
    return render_placeholder(request, "Biblioteca")

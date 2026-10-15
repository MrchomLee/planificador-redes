"""Miscellaneous pages (home redirect, help). SKELETON — owner replaces it."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

router = APIRouter()


@router.get("/")
def home() -> Response:
    return RedirectResponse("/planificacion", status_code=303)

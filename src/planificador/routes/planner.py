"""Planner: calendar (Mes/Semana/Día/Lista), Ideas board, Papelera (SPEC §1, §15). SKELETON."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from planificador.routes import render_placeholder

router = APIRouter()


@router.get("/planificacion")
def planner_page(request: Request) -> Response:
    return render_placeholder(request, "Planificación")


@router.get("/ideas")
def ideas_page(request: Request) -> Response:
    return render_placeholder(request, "Ideas")


@router.get("/papelera")
def trash_page(request: Request) -> Response:
    return render_placeholder(request, "Papelera")

"""Brands management ("Marcas"). SKELETON page — owner replaces it; the switch route below is final.

``POST /marcas/{id}/activar`` is used by the top-bar brand switcher (``partials/brand_switcher.html``):
form fields ``csrf_token`` and ``next`` (current path). Answers 303 to ``next`` (``HX-Redirect`` for htmx).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from planificador.deps import DbSession, set_active_brand
from planificador.routes import render_placeholder
from planificador.security import safe_next_path

router = APIRouter()


@router.get("/marcas")
def brands_page(request: Request) -> Response:
    return render_placeholder(request, "Marcas")


@router.post("/marcas/{brand_id:int}/activar")
def activate_brand(
    request: Request,
    brand_id: int,
    db: DbSession,
    next: Annotated[str, Form()] = "/planificacion",
) -> Response:
    try:
        brand = set_active_brand(db, brand_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Marca no encontrada.") from None
    target = safe_next_path(next, "/planificacion")
    from planificador.templating import flash

    flash(request, f"Ahora trabajas con la marca «{brand.name}».", "success")
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)

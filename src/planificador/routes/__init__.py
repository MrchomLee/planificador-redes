"""HTTP routers. Each module exposes ``router = APIRouter()``; :func:`include_routers` mounts them all.

Module owners replace the skeleton placeholder pages with the real implementation. Keep the module list in
:data:`ROUTER_MODULES` in sync with SPEC §2.
"""

from __future__ import annotations

import importlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import escape

log = logging.getLogger(__name__)

#: Router modules (``planificador.routes.<name>``), included in this order.
ROUTER_MODULES: tuple[str, ...] = (
    "pages",
    "planner",
    "composer",
    "posts",
    "media",
    "analytics",
    "inbox",
    "connections",
    "brands",
    "settings",
    "notifications",
    "csv",
    "bulk",
)


def include_routers(app: FastAPI) -> None:
    for name in ROUTER_MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        router = getattr(module, "router", None)
        if router is None:  # pragma: no cover - module under construction
            log.warning("Route module %s has no 'router'; skipping", name)
            continue
        app.include_router(router)


#: htmx target ids of the layer roots in ``base.html`` → layer kind.
LAYER_TARGETS = {"modal-root": "modal", "drawer-root": "drawer"}


def htmx_layer(request: Request) -> str | None:
    """``"modal"``/``"drawer"`` when an htmx request will be swapped into a layer root, else ``None``."""
    if request.headers.get("hx-request") != "true":
        return None
    return LAYER_TARGETS.get(request.headers.get("hx-target", ""))


def render_placeholder(request: Request, title: str, description: str | None = None) -> Response:
    """Placeholder page for routes whose module is not implemented yet.

    Renders ``placeholder.html`` through :func:`planificador.templating.render` — or, for an htmx request
    aimed at ``#modal-root``/``#drawer-root`` (e.g. the "Nueva publicación" button), a modal/drawer fragment
    (``partials/placeholder_layer.html``). Falls back to a tiny built-in page if templating is unavailable.
    """
    description = description or "Esta sección estará disponible muy pronto."
    try:
        from planificador import templating

        layer = htmx_layer(request)
        if layer is not None:
            return templating.render(
                request,
                "partials/placeholder_layer.html",
                layer=layer,
                title=title,
                page_title=title,
                description=description,
            )
        return templating.render(
            request, "placeholder.html", title=title, page_title=title, description=description
        )
    except Exception as exc:
        from jinja2 import TemplateNotFound

        if not isinstance(exc, ImportError | TemplateNotFound):
            raise
    html = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        f"<title>{escape(title)} · Planificador</title></head>"
        f"<body><h1>{escape(title)}</h1><p>{escape(description)}</p>"
        '<p><a href="/planificacion">Planificación</a></p></body></html>'
    )
    return HTMLResponse(html)

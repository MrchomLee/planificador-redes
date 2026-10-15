"""Comments inbox ("Comentarios", SPEC §13, doc 05 §5): two panels — conversation list on the left (network
filter logos, search, tabs Sin responder · No leídos · Todos) and the selected conversation on the right.

Routes:
    GET  /comentarios                       full page (?red=&tab=&q=&c=<comment id>)
    GET  /comentarios/lista                 list panel fragment (htmx); ``parte=cuerpo`` → only the list body
    GET  /comentarios/{id}                  conversation panel fragment (htmx; marks it read)
    POST /comentarios/{id}/responder        reply (form: texto, responder_a)
    POST /comentarios/{id}/resolver         mark resolved / reopen (form: resuelto=1|0)
    POST /comentarios/{id}/no-leido         mark the conversation unread
    POST /comentarios/actualizar            poll the brand's accounts now ("Actualizar")
    GET  /api/comentarios/resumen           {"sin_responder", "no_leidos", "todos"}

htmx events: replies / resolve / unread respond with ``HX-Trigger: inbox:changed`` (the list body refreshes
itself, keeping the search box and scroll position).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from planificador import config, inbox
from planificador.deps import ActiveBrand, ClockDep, DbSession, ProviderSettingsDep
from planificador.models import AccountStatus, Brand, InboxComment, Network
from planificador.templating import render, render_fragment, toast_headers
from planificador.timeutil import Clock, ensure_utc

log = logging.getLogger(__name__)

router = APIRouter()

#: Filter logos, in display order (TikTok is added disabled: its public API has no comments).
FILTER_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "youtube")
#: Avatar colours for commenters (white initial on a colour), picked by a stable hash of the name.
AVATAR_COLORS: tuple[str, ...] = (
    "#5b4cf0",
    "#0b7fc0",
    "#15803d",
    "#b45309",
    "#c2185b",
    "#7c3aed",
    "#0f766e",
    "#c2410c",
)


# ============================================================================================ helpers


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _clean_network(value: str | None, allowed: set[str]) -> str:
    net = str(value or "").strip().lower()
    return net if net in allowed else ""


def _query(**params: Any) -> str:
    return urlencode({k: v for k, v in params.items() if v not in (None, "")})


def _linker(red: str, tab: str, q: str) -> Callable[..., str]:
    """``link(tab="todos", c=None)`` → query string with the current filters overridden."""

    def link(**overrides: Any) -> str:
        params: dict[str, Any] = {"red": red, "tab": tab, "q": q}
        params.update(overrides)
        if params.get("tab") == inbox.DEFAULT_TAB:
            params["tab"] = ""
        return _query(**params)

    return link


def avatar_color(name: str | None) -> str:
    total = sum(ord(ch) for ch in (name or "?"))
    return AVATAR_COLORS[total % len(AVATAR_COLORS)]


def _error_response(message: str, status_code: int = 400) -> Response:
    return Response(
        content=message,
        status_code=status_code,
        media_type="text/plain; charset=utf-8",
        headers={"X-Error-Message": quote(message)},
    )


def _filter_items(
    db: Session, brand: Brand, red: str, demo_mode: bool, state: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Network filter logos: one per comment network (+ Demo in demo mode) and TikTok (disabled)."""
    accounts = {str(a.network): a for a in inbox.brand_accounts(db, brand.id)}
    networks = list(FILTER_NETWORKS) + (["demo"] if demo_mode else [])
    items: list[dict[str, Any]] = []
    for net in networks:
        account = accounts.get(net)
        connected = account is not None and account.status != AccountStatus.DISCONNECTED
        problem = inbox.account_problem(account, state) if connected and account is not None else None
        label = inbox.NETWORK_NAMES[net]
        if not connected:
            title = f"Conecta tu cuenta de {label}"
        elif problem:
            title = f"{label}: {problem}"
        elif red == net:
            title = f"Mostrar todas las redes (quitar el filtro de {label})"
        else:
            title = f"Ver solo {label}"
        items.append(
            {
                "network": net,
                "label": label,
                "account": account,
                "connected": connected,
                "unsupported": False,
                "problem": problem,
                "active": red == net,
                "title": title,
            }
        )
    items.append(
        {
            "network": "tiktok",
            "label": "TikTok",
            "account": accounts.get("tiktok"),
            "connected": False,
            "unsupported": True,
            "problem": None,
            "active": False,
            "title": inbox.TIKTOK_NOTE,
        }
    )
    return items


def list_context(
    db: Session,
    brand: Brand,
    clock: Clock,
    *,
    red: str = "",
    tab: str = "",
    q: str = "",
    selected: int | None = None,
) -> dict[str, Any]:
    """Everything the list panel templates need."""
    demo_mode = config.is_demo_mode(db)
    allowed = set(FILTER_NETWORKS) | ({"demo"} if demo_mode else set())
    red = _clean_network(red, allowed)
    tab = inbox.normalize_tab(tab)
    q = (q or "").strip()[:100]
    state = inbox.poll_state(db)
    all_threads = inbox.load_threads(db, brand.id, clock, network=red or None, demo_mode=demo_mode)
    counts = inbox.tab_counts(all_threads)
    shown = inbox.filter_threads(all_threads, tab, q)
    truncated = len(shown) > inbox.LIST_LIMIT
    shown = shown[: inbox.LIST_LIMIT]
    inbox.attach_contexts(db, shown)

    brand_accounts = inbox.brand_accounts(db, brand.id)
    comment_accounts = [
        a
        for a in brand_accounts
        if str(a.network) in inbox.COMMENT_NETWORKS
        and (demo_mode or str(a.network) != Network.DEMO)
        and a.status != AccountStatus.DISCONNECTED
    ]
    errors = [
        {"network": str(a.network), "label": inbox.NETWORK_NAMES.get(str(a.network)), "message": msg}
        for a in comment_accounts
        if (msg := inbox.last_error(a, state)) and inbox.account_problem(a, state) is None
    ]
    return {
        "active_brand": brand,
        "red": red,
        "tab": tab,
        "q": q,
        "q_short": 0 < len(q) < inbox.SEARCH_MIN,
        "threads": shown,
        "truncated": truncated,
        "counts": counts,
        "tabs": inbox.TABS,
        "filters": _filter_items(db, brand, red, demo_mode, state),
        "has_accounts": bool(comment_accounts),
        "has_tiktok": any(str(a.network) == Network.TIKTOK for a in brand_accounts),
        "tiktok_note": inbox.TIKTOK_NOTE,
        "poll_errors": errors,
        "last_poll": inbox.last_poll_at(state, [a.id for a in comment_accounts]),
        "selected_id": selected,
        "link": _linker(red, tab, q),
        "avatar_color": avatar_color,
        "search_min": inbox.SEARCH_MIN,
    }


def _thread_or_404(db: Session, comment_id: int, brand: Brand) -> inbox.Thread:
    try:
        return inbox.get_thread(db, comment_id, brand.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Esta conversación ya no existe.") from None


def _thread_ctx(thread: inbox.Thread, brand: Brand) -> dict[str, Any]:
    return {"thread": thread, "brand": brand, "active_brand": brand, "avatar_color": avatar_color}


def _render_thread(
    request: Request,
    db: Session,
    thread: inbox.Thread,
    brand: Brand,
    clock: Clock,
    *,
    red: str = "",
    tab: str = "",
    q: str = "",
    oob_item: bool = False,
    headers: dict[str, str] | None = None,
) -> Response:
    """Conversation panel fragment (+ out-of-band refresh of its list item and the tab counters)."""
    html = render_fragment(request, "inbox/_thread.html", **_thread_ctx(thread, brand))
    if oob_item:
        ctx = list_context(db, brand, clock, red=red, tab=tab, q=q, selected=thread.id)
        listed = next((t for t in ctx["threads"] if t.id == thread.id), None)
        if listed is not None:
            html += render_fragment(request, "inbox/_item.html", t=listed, oob=True, **ctx)
        html += render_fragment(request, "inbox/_counts.html", oob=True, **ctx)
    return HTMLResponse(html, headers=headers)


def _redirect_page(**params: Any) -> RedirectResponse:
    qs = _query(**params)
    return RedirectResponse(f"/comentarios{'?' + qs if qs else ''}", status_code=303)


# ============================================================================================= routes


@router.get("/comentarios")
def inbox_page(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    red: str = "",
    tab: str = "",
    q: str = "",
    c: int | None = None,
) -> Response:
    thread = None
    if c is not None:
        try:
            thread = inbox.get_thread(db, c, brand.id)
            inbox.mark_read(db, thread, ensure_utc(clock.now()))
        except LookupError:
            thread = None
    ctx = list_context(db, brand, clock, red=red, tab=tab, q=q, selected=thread.id if thread else None)
    ctx.update(_thread_ctx(thread, brand) if thread is not None else {"thread": None})
    return render(request, "inbox/index.html", page_title="Comentarios", **ctx)


@router.get("/comentarios/lista")
def inbox_list(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    red: str = "",
    tab: str = "",
    q: str = "",
    c: str = "",
    parte: str = "",
) -> Response:
    selected = int(c) if c.isdigit() else None
    if not _is_htmx(request):
        return _redirect_page(red=red, tab=tab, q=q, c=selected)
    ctx = list_context(db, brand, clock, red=red, tab=tab, q=q, selected=selected)
    if parte == "cuerpo":
        return render(request, "inbox/_list_body.html", oob_counts=True, **ctx)
    return render(request, "inbox/_list_panel.html", **ctx)


@router.get("/api/comentarios/resumen")
def inbox_summary(db: DbSession, brand: ActiveBrand, clock: ClockDep) -> JSONResponse:
    return JSONResponse(inbox.summary(db, brand.id, clock, demo_mode=config.is_demo_mode(db)))


@router.post("/comentarios/actualizar")
def inbox_refresh(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    provider_settings: ProviderSettingsDep,
    red: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    c: Annotated[str, Form()] = "",
) -> Response:
    """ "Actualizar": poll the active brand's accounts now (synchronously) and re-render the list."""
    accounts = inbox.comment_accounts(db, brand_id=brand.id, demo_mode=provider_settings.demo_mode)
    new = 0
    if not accounts:
        message, kind = "No hay cuentas de Facebook, Instagram o YouTube conectadas en esta marca.", "info"
    else:
        http = inbox.new_http_client()
        try:
            new = inbox.poll_all(db, http, clock, provider_settings, brand_id=brand.id)
        finally:
            http.close()
        state = inbox.poll_state(db)
        failed = [a for a in accounts if inbox.last_error(a, state)]
        if failed:
            names = ", ".join(inbox.NETWORK_NAMES.get(str(a.network), str(a.network)) for a in failed)
            message, kind = f"No se pudieron actualizar los comentarios de {names}.", "warning"
        elif new == 1:
            message, kind = "1 comentario nuevo.", "success"
        elif new:
            message, kind = f"{new} comentarios nuevos.", "success"
        else:
            message, kind = "No hay comentarios nuevos.", "info"
    selected = int(c) if str(c).isdigit() else None
    if not _is_htmx(request):
        return _redirect_page(red=red, tab=tab, q=q, c=selected)
    ctx = list_context(db, brand, clock, red=red, tab=tab, q=q, selected=selected)
    return render(
        request,
        "inbox/_list_panel.html",
        headers=toast_headers(message, kind),
        new_count=new,
        oob_updated=True,
        **ctx,
    )


@router.get("/comentarios/{comment_id:int}")
def inbox_thread(
    request: Request,
    comment_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    red: str = "",
    tab: str = "",
    q: str = "",
) -> Response:
    if not _is_htmx(request):
        return _redirect_page(red=red, tab=tab, q=q, c=comment_id)
    thread = _thread_or_404(db, comment_id, brand)
    inbox.mark_read(db, thread, ensure_utc(clock.now()))
    return _render_thread(request, db, thread, brand, clock, red=red, tab=tab, q=q, oob_item=True)


@router.post("/comentarios/{comment_id:int}/responder")
def inbox_reply(
    request: Request,
    comment_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    provider_settings: ProviderSettingsDep,
    texto: Annotated[str, Form()] = "",
    responder_a: Annotated[str, Form()] = "",
) -> Response:
    thread = _thread_or_404(db, comment_id, brand)
    reply_to: InboxComment | None = None
    if str(responder_a).isdigit():
        reply_to = next((c for c in thread.comments if c.id == int(responder_a)), None)
    http = inbox.new_http_client()
    try:
        inbox.reply(db, http, clock, provider_settings, thread, texto, reply_to=reply_to)
    except inbox.InboxError as exc:
        # 422 would be swapped by htmx (validation re-render): use 400 so the text stays and a toast shows.
        return _error_response(exc.message, 400 if exc.status_code == 422 else exc.status_code)
    finally:
        http.close()
    thread = _thread_or_404(db, thread.root.id, brand)
    if not _is_htmx(request):
        return _redirect_page(c=thread.id)
    headers = toast_headers(
        f"Respuesta publicada en {thread.network_name}.", "success", **{"inbox:changed": True}
    )
    return _render_thread(request, db, thread, brand, clock, headers=headers)


@router.post("/comentarios/{comment_id:int}/resolver")
def inbox_resolve(
    request: Request,
    comment_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    resuelto: Annotated[str, Form()] = "1",
) -> Response:
    thread = _thread_or_404(db, comment_id, brand)
    resolved = str(resuelto).strip().lower() not in ("0", "false", "no", "")
    inbox.set_resolved(db, thread, resolved, ensure_utc(clock.now()))
    thread = _thread_or_404(db, thread.root.id, brand)
    if not _is_htmx(request):
        return _redirect_page(c=thread.id)
    message = "Conversación marcada como resuelta." if resolved else "Conversación reabierta."
    headers = toast_headers(message, "success", **{"inbox:changed": True})
    return _render_thread(request, db, thread, brand, clock, headers=headers)


@router.post("/comentarios/{comment_id:int}/no-leido")
def inbox_unread(
    request: Request,
    comment_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
) -> Response:
    thread = _thread_or_404(db, comment_id, brand)
    inbox.mark_read(db, thread, ensure_utc(clock.now()), read=False)
    if not _is_htmx(request):
        return _redirect_page()
    headers = toast_headers("Conversación marcada como no leída.", "info", **{"inbox:changed": True})
    return HTMLResponse(render_fragment(request, "inbox/_empty_thread.html"), headers=headers)


__all__ = ["avatar_color", "list_context", "router"]

"""Brands ("Marcas"): list, create, rename, colour, time zone, archive/restore, delete (empty archived ones)
and the top-bar switcher endpoint.

- ``GET  /marcas``                     list (active + archived) with networks and pending publications
- ``GET  /marcas/nueva``               form (modal fragment for htmx into ``#modal-root``, else a full page)
- ``POST /marcas``                     create (``activar=1`` also switches to it)
- ``GET  /marcas/{id}/editar``         edit form (modal / page)
- ``POST /marcas/{id}``                update name, colour, time zone
- ``POST /marcas/{id}/archivar``       archive (not the last active brand, not with pending publications)
- ``POST /marcas/{id}/restaurar``      restore
- ``POST /marcas/{id}/eliminar``       delete an archived brand without publications
- ``POST /marcas/{id}/activar``        used by the top-bar brand switcher (``partials/brand_switcher.html``):
  form fields ``csrf_token`` and ``next`` (current path). Answers 303 to ``next`` (``HX-Redirect`` for htmx).

Validation errors re-render the form with **422** (htmx swaps 422 responses). Time zones must be IANA names
known to ``zoneinfo`` (``tzdata`` is a dependency, so Windows has them too).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planificador import config
from planificador.deps import ActiveBrand, ClockDep, DbSession, list_brands, set_active_brand
from planificador.models import Account, AccountStatus, Brand, Post, PostTarget, TargetStatus
from planificador.security import safe_next_path

router = APIRouter()

NAME_MAX = 120
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLOR = "#5b4cf0"

#: Colour swatches offered in the form (value, Spanish name).
BRAND_COLORS: tuple[tuple[str, str], ...] = (
    ("#5b4cf0", "Violeta"),
    ("#2a78d6", "Azul"),
    ("#0e9f9a", "Turquesa"),
    ("#16a34a", "Verde"),
    ("#e5a000", "Ámbar"),
    ("#eb6834", "Naranja"),
    ("#e5484d", "Rojo"),
    ("#e0569b", "Rosa"),
    ("#8b5cf6", "Lila"),
    ("#475569", "Pizarra"),
)

#: Zones most users of a Spanish-language app pick (shown first).
COMMON_TIMEZONES: tuple[str, ...] = (
    "Europe/Madrid",
    "Atlantic/Canary",
    "America/Mexico_City",
    "America/Bogota",
    "America/Lima",
    "America/Santiago",
    "America/Argentina/Buenos_Aires",
    "America/Caracas",
    "America/Montevideo",
    "America/Guatemala",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "UTC",
)
_REGION_LABELS: dict[str, str] = {
    "Europe": "Europa",
    "America": "América",
    "Africa": "África",
    "Asia": "Asia",
    "Atlantic": "Atlántico",
    "Australia": "Australia",
    "Indian": "Índico",
    "Pacific": "Pacífico",
    "Antarctica": "Antártida",
    "Arctic": "Ártico",
}
#: Pending publications that block archiving (they would publish under a brand the user cannot see).
PENDING_STATUSES = (
    TargetStatus.SCHEDULED,
    TargetStatus.PUBLISHING,
    TargetStatus.MANUAL_PENDING,
)


# ------------------------------------------------------------------------------------------ time zones


@lru_cache(maxsize=1)
def known_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def is_known_timezone(name: str | None) -> bool:
    """IANA zone known to ``zoneinfo`` (rejects ``localtime``/``posixrules``-style pseudo zones)."""
    if not name or name in ("localtime", "posixrules", "Factory"):
        return False
    return name in known_timezones()


def utc_offset_label(name: str, at: datetime | None = None) -> str:
    """'UTC+01:00' for ``name`` at ``at`` (now by default)."""
    try:
        offset = (at or datetime.now(UTC)).astimezone(ZoneInfo(name)).utcoffset()
    except Exception:  # noqa: BLE001 - unknown zone
        return ""
    if offset is None:
        return ""
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _canonical(name: str) -> bool:
    region = name.split("/", 1)[0]
    return name == "UTC" or (region in _REGION_LABELS and "/" in name)


@lru_cache(maxsize=4)
def _timezone_groups(hour_key: str) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    now = datetime.now(UTC)
    names = sorted(n for n in known_timezones() if _canonical(n))
    groups: dict[str, list[tuple[str, str]]] = {}
    for name in names:
        region = "UTC" if name == "UTC" else name.split("/", 1)[0]
        label = f"{name.replace('_', ' ')} · {utc_offset_label(name, now)}"
        groups.setdefault(_REGION_LABELS.get(region, "Otras"), []).append((name, label))
    common = tuple(
        (n, f"{n.replace('_', ' ')} · {utc_offset_label(n, now)}")
        for n in COMMON_TIMEZONES
        if n in known_timezones()
    )
    ordered = [("Habituales", common)]
    for region in _REGION_LABELS.values():
        if region in groups:
            ordered.append((region, tuple(groups[region])))
    if "Otras" in groups:
        ordered.append(("Otras", tuple(groups["Otras"])))
    return tuple(ordered)


def timezone_groups(selected: str | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """``[(group label, [(zone, "Europe/Madrid · UTC+01:00")])]`` for a grouped ``<select>``; "Habituales"
    first. A selected zone outside the lists (legacy alias) is added to "Habituales"."""
    key = datetime.now(UTC).strftime("%Y%m%d%H")
    groups = [(label, list(items)) for label, items in _timezone_groups(key)]
    if (
        selected
        and is_known_timezone(selected)
        and all(selected != z for _, items in groups for z, _ in items)
    ):
        groups[0][1].insert(0, (selected, f"{selected} · {utc_offset_label(selected)}"))
    return groups


# ------------------------------------------------------------------------------------------- helpers


def _render(request: Request, name: str, **ctx: Any) -> Response:
    from planificador.templating import render

    return render(request, name, **ctx)


def _flash(request: Request, message: str, kind: str = "info") -> None:
    from planificador.templating import flash

    flash(request, message, kind)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _to_list(request: Request) -> Response:
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": "/marcas"})
    return RedirectResponse("/marcas", status_code=303)


@dataclass
class BrandForm:
    name: str = ""
    color: str = DEFAULT_COLOR
    timezone: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_brand(
    db: Session, name: str, color: str, timezone: str, *, brand_id: int | None = None
) -> BrandForm:
    form = BrandForm(
        name=(name or "").strip(), color=(color or "").strip().lower(), timezone=(timezone or "").strip()
    )
    if not form.name:
        form.errors["name"] = "Escribe un nombre para la marca."
    elif len(form.name) > NAME_MAX:
        form.errors["name"] = f"El nombre no puede tener más de {NAME_MAX} caracteres."
    else:
        clash = db.scalar(
            select(Brand.id).where(func.lower(Brand.name) == form.name.lower(), Brand.archived_at.is_(None))
        )
        if clash is not None and clash != brand_id:
            form.errors["name"] = "Ya tienes una marca activa con ese nombre."
    if not HEX_RE.match(form.color):
        form.errors["color"] = "Elige un color válido (formato #rrggbb)."
    if not is_known_timezone(form.timezone):
        form.errors["timezone"] = "Elige una zona horaria de la lista (por ejemplo, Europe/Madrid)."
    return form


def _brand_or_404(db: Session, brand_id: int) -> Brand:
    brand = db.get(Brand, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail="Marca no encontrada.")
    return brand


def _pending_by_brand(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Post.brand_id, func.count(func.distinct(Post.id)))
        .join(PostTarget, PostTarget.post_id == Post.id)
        .where(Post.deleted_at.is_(None), PostTarget.status.in_(list(PENDING_STATUSES)))
        .group_by(Post.brand_id)
    ).all()
    return {int(b): int(n) for b, n in rows}


def _posts_by_brand(db: Session) -> dict[int, int]:
    rows = db.execute(select(Post.brand_id, func.count(Post.id)).group_by(Post.brand_id)).all()
    return {int(b): int(n) for b, n in rows}


def _networks_by_brand(db: Session) -> dict[int, list[Account]]:
    out: dict[int, list[Account]] = {}
    order = ("facebook", "instagram", "tiktok", "youtube", "demo")
    accounts = db.scalars(select(Account).where(Account.status != AccountStatus.DISCONNECTED))
    for account in accounts:
        out.setdefault(account.brand_id, []).append(account)
    for items in out.values():
        items.sort(key=lambda a: order.index(str(a.network)) if str(a.network) in order else 99)
    return out


def _form_response(
    request: Request, form: BrandForm, *, brand: Brand | None, status_code: int = 200
) -> Response:
    ctx = {
        "form": form,
        "brand": brand,
        "colors": BRAND_COLORS,
        "tz_groups": timezone_groups(form.timezone),
        "page_title": f"Editar «{brand.name}»" if brand else "Nueva marca",
    }
    template = "brands/_modal.html" if _is_htmx(request) else "brands/form_page.html"
    response = _render(request, template, **ctx)
    response.status_code = status_code
    return response


# ---------------------------------------------------------------------------------------------- pages


@router.get("/marcas")
def brands_page(request: Request, db: DbSession, active: ActiveBrand, clock: ClockDep) -> Response:
    brands = list_brands(db, include_archived=True)
    active_list = sorted(
        (b for b in brands if b.archived_at is None),
        key=lambda b: (b.id != active.id, b.name.casefold(), b.id),
    )
    archived = sorted(
        (b for b in brands if b.archived_at is not None), key=lambda b: b.archived_at, reverse=True
    )
    return _render(
        request,
        "brands/index.html",
        page_title="Marcas",
        active_brands=active_list,
        archived_brands=archived,
        current=active,
        networks=_networks_by_brand(db),
        pending=_pending_by_brand(db),
        post_counts=_posts_by_brand(db),
        now=clock.now(),
        offset_label=utc_offset_label,
    )


@router.get("/marcas/nueva")
def new_brand_form(request: Request, db: DbSession) -> Response:
    form = BrandForm(timezone=config.default_timezone(db))
    return _form_response(request, form, brand=None)


@router.post("/marcas")
def create_brand(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = DEFAULT_COLOR,
    timezone: Annotated[str, Form()] = "",
    activar: Annotated[str, Form()] = "",
) -> Response:
    form = validate_brand(db, name, color, timezone)
    if not form.ok:
        return _form_response(request, form, brand=None, status_code=422)
    brand = Brand(name=form.name, color=form.color, timezone=form.timezone)
    db.add(brand)
    db.commit()
    if activar:
        set_active_brand(db, brand.id)
        _flash(
            request, f"Marca «{brand.name}» creada. Ahora trabajas con ella: conecta sus redes.", "success"
        )
        if _is_htmx(request):
            return Response(status_code=204, headers={"HX-Redirect": "/conexiones"})
        return RedirectResponse("/conexiones", status_code=303)
    _flash(request, f"Marca «{brand.name}» creada.", "success")
    return _to_list(request)


@router.get("/marcas/{brand_id:int}/editar")
def edit_brand_form(request: Request, brand_id: int, db: DbSession) -> Response:
    brand = _brand_or_404(db, brand_id)
    form = BrandForm(name=brand.name, color=(brand.color or DEFAULT_COLOR).lower(), timezone=brand.timezone)
    return _form_response(request, form, brand=brand)


@router.post("/marcas/{brand_id:int}")
def update_brand(
    request: Request,
    brand_id: int,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = DEFAULT_COLOR,
    timezone: Annotated[str, Form()] = "",
) -> Response:
    brand = _brand_or_404(db, brand_id)
    form = validate_brand(db, name, color, timezone, brand_id=brand.id)
    if not form.ok:
        return _form_response(request, form, brand=brand, status_code=422)
    tz_changed = brand.timezone != form.timezone
    brand.name, brand.color, brand.timezone = form.name, form.color, form.timezone
    db.commit()
    message = f"Marca «{brand.name}» guardada."
    if tz_changed:
        message += (
            " Las publicaciones ya programadas mantienen su hora exacta y se mostrarán en la nueva zona."
        )
    _flash(request, message, "success")
    return _to_list(request)


@router.post("/marcas/{brand_id:int}/archivar")
def archive_brand(request: Request, brand_id: int, db: DbSession, clock: ClockDep) -> Response:
    brand = _brand_or_404(db, brand_id)
    if brand.archived_at is not None:
        return _to_list(request)
    others = [b for b in list_brands(db) if b.id != brand.id]
    if not others:
        _flash(request, "No puedes archivar tu única marca activa. Crea otra antes.", "error")
        return _to_list(request)
    pending = _pending_by_brand(db).get(brand.id, 0)
    if pending:
        _flash(
            request,
            f"«{brand.name}» tiene {pending} "
            f"{'publicación pendiente' if pending == 1 else 'publicaciones pendientes'}. "
            "Cancélalas o espera a que salgan antes de archivarla.",
            "error",
        )
        return _to_list(request)
    was_active = config.get_setting(db, "active_brand_id", None)
    brand.archived_at = clock.now()
    db.commit()
    message = f"Marca «{brand.name}» archivada."
    if str(was_active) == str(brand.id):
        replacement = sorted(others, key=lambda b: (b.name.casefold(), b.id))[0]
        set_active_brand(db, replacement.id)
        message += f" Ahora trabajas con «{replacement.name}»."
    _flash(request, message, "success")
    return _to_list(request)


@router.post("/marcas/{brand_id:int}/restaurar")
def restore_brand(request: Request, brand_id: int, db: DbSession) -> Response:
    brand = _brand_or_404(db, brand_id)
    if brand.archived_at is None:
        return _to_list(request)
    clash = db.scalar(
        select(Brand.id).where(
            func.lower(Brand.name) == brand.name.lower(), Brand.archived_at.is_(None), Brand.id != brand.id
        )
    )
    brand.archived_at = None
    if clash is not None:
        brand.name = f"{brand.name} (restaurada)"[:NAME_MAX]
    db.commit()
    _flash(request, f"Marca «{brand.name}» restaurada.", "success")
    return _to_list(request)


@router.post("/marcas/{brand_id:int}/eliminar")
def delete_brand(request: Request, brand_id: int, db: DbSession) -> Response:
    brand = _brand_or_404(db, brand_id)
    if brand.archived_at is None:
        _flash(request, "Archiva la marca antes de eliminarla.", "error")
        return _to_list(request)
    if _posts_by_brand(db).get(brand.id, 0):
        _flash(request, "Solo se pueden eliminar marcas sin publicaciones. Déjala archivada.", "error")
        return _to_list(request)
    name = brand.name
    db.delete(brand)
    db.commit()
    _flash(request, f"Marca «{name}» eliminada.", "success")
    return _to_list(request)


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
    _flash(request, f"Ahora trabajas con la marca «{brand.name}».", "success")
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)

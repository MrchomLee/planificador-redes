"""FastAPI dependencies shared by every router.

Usage in a route module::

    from planificador.deps import ActiveBrand, ClockDep, DbSession

    @router.get("/planificacion")
    def planner(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep): ...

``get_db`` yields a plain session: commit explicitly (``db.commit()``); it is rolled back on error and always
closed. Objects stay usable after commit (``expire_on_commit=False``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config
from planificador.config import AppSettings
from planificador.db import Database
from planificador.models import Brand
from planificador.providers.base import ProviderSettings
from planificador.timeutil import Clock, SystemClock

DEFAULT_BRAND_NAME = "Mi marca"
DEFAULT_BRAND_COLOR = "#5b4cf0"
ACTIVE_BRAND_KEY = "active_brand_id"
#: ``Session.info`` key set when the default brand got the time zone detected on this PC (one-time notice).
DETECTED_TZ_INFO_KEY = "planificador_detected_timezone"

# Serialises the "pick a fallback / create the default brand" path: on first run the page and the bell
# request (or two tabs) may arrive together and must not create two "Mi marca" brands.
_fallback_lock = threading.Lock()


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_db(request: Request) -> Iterator[Session]:
    session = request.app.state.db.session()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_clock(request: Request) -> Clock:
    return getattr(request.app.state, "clock", None) or SystemClock()


def get_settings(request: Request) -> AppSettings:
    return request.app.state.settings


def get_provider_settings(request: Request, db: Annotated[Session, Depends(get_db)]) -> ProviderSettings:
    return config.provider_settings(db, base_url=request.app.state.settings.base_url)


# ------------------------------------------------------------------------------------------- brands


def list_brands(db: Session, *, include_archived: bool = False) -> list[Brand]:
    stmt = select(Brand).order_by(Brand.id)
    if not include_archived:
        stmt = stmt.where(Brand.archived_at.is_(None))
    return list(db.scalars(stmt))


def first_run_timezone(db: Session) -> str:
    """Time zone for the default brand. Unless the user already chose a default time zone, use this PC's
    (stored as ``default_timezone`` so new brands follow it) instead of the hardcoded Europe/Madrid: a user in
    Mexico would otherwise schedule everything 7-8 hours off without noticing."""
    from planificador.timeutil import is_valid_timezone, system_timezone

    stored = config.get_setting(db, "default_timezone", None)
    if is_valid_timezone(stored):
        return str(stored)
    detected = system_timezone()
    if detected is None:
        return config.default_timezone(db)
    config.set_setting(db, "default_timezone", detected, commit=False)
    db.info[DETECTED_TZ_INFO_KEY] = detected
    return detected


def create_default_brand(db: Session) -> Brand:
    brand = Brand(name=DEFAULT_BRAND_NAME, color=DEFAULT_BRAND_COLOR, timezone=first_run_timezone(db))
    db.add(brand)
    db.flush()
    return brand


def announce_detected_timezone(request: Request, db: Session) -> None:
    """One-time toast after the default brand took this PC's time zone (see :func:`first_run_timezone`)."""
    detected = db.info.pop(DETECTED_TZ_INFO_KEY, None)
    if not detected:
        return
    try:
        from planificador.templating import flash

        flash(
            request,
            f"Zona horaria: hemos usado la de este ordenador ({detected}). Si no es la tuya, cámbiala en "
            "☰ › Marcas.",
            "info",
        )
    except Exception:  # pragma: no cover - templating unavailable
        pass


def ensure_active_brand(db: Session) -> Brand:
    """Return the active brand, falling back to the first non-archived one and creating the default
    "Mi marca" on first run. Persists ``active_brand_id`` (commits only when something changed)."""
    brand: Brand | None = None
    active_id = config.get_setting(db, ACTIVE_BRAND_KEY, None)
    if active_id is not None:
        try:
            brand = db.get(Brand, int(active_id))
        except (TypeError, ValueError):
            brand = None
        if brand is not None and brand.archived_at is not None:
            brand = None
    if brand is not None:
        return brand
    with _fallback_lock:
        # Re-read inside the lock: plain SELECTs see what a concurrent request just committed.
        brand = db.scalars(
            select(Brand).where(Brand.archived_at.is_(None)).order_by(Brand.id).limit(1)
        ).first()
        if brand is None:
            brand = create_default_brand(db)
        config.set_setting(db, ACTIVE_BRAND_KEY, brand.id, commit=False)
        db.commit()
    return brand


def set_active_brand(db: Session, brand_id: int, *, commit: bool = True) -> Brand:
    brand = db.get(Brand, brand_id)
    if brand is None or brand.archived_at is not None:
        raise LookupError(f"Brand {brand_id} not found or archived")
    config.set_setting(db, ACTIVE_BRAND_KEY, brand.id, commit=commit)
    return brand


def get_active_brand(request: Request, db: Annotated[Session, Depends(get_db)]) -> Brand:
    brand = ensure_active_brand(db)
    announce_detected_timezone(request, db)
    # Exposed for templating globals (brand switcher, display time zone).
    request.state.active_brand = brand
    request.state.active_brand_id = brand.id
    return brand


# -------------------------------------------------------------------------------- Annotated aliases

DbSession = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]
SettingsDep = Annotated[AppSettings, Depends(get_settings)]
ProviderSettingsDep = Annotated[ProviderSettings, Depends(get_provider_settings)]
ActiveBrand = Annotated[Brand, Depends(get_active_brand)]
DatabaseDep = Annotated[Database, Depends(get_database)]

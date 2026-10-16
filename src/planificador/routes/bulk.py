"""Carga masiva — schedule many videos at once, each with its own text and hashtags (logic in ``bulk.py``).

Page:

- ``GET /carga-masiva`` — four steps (Vídeos · Redes y ajustes · Calendario · Textos) driven by
  ``static/js/bulk.js``. The brand's pending batch (settings ``bulk_batch:<brand_id>``) is loaded back, so
  closing the tab or restarting the app keeps everything. A batch whose "Programar todo" was cut short (app
  closed or crashed) comes back without the rows already created and flagged ``interrupted``.

JSON API (POST needs the CSRF header; bodies are JSON). Every POST carries the ``brand_id`` the page was
opened for (in ``batch`` or at the top level): 409 ``brand_changed`` when the active brand changed since (in
another tab), so a stale tab never touches another brand's batch.

- ``GET  /api/carga-masiva/lote`` — the pending batch (or a new empty one).
- ``POST /api/carga-masiva/lote`` — autosave ``{batch}`` → ``{ok, revision, updated_at}`` (409 ``submitted``
  when that batch was already scheduled).
- ``POST /api/carga-masiva/lote/descartar`` — ``{brand_id}``: drop the pending batch → ``{ok, batch}`` (a
  fresh one).
- ``GET  /api/carga-masiva/medios?ids=1,2`` — library items with per-network compatibility.
- ``POST /api/carga-masiva/repartir`` — ``{distribution, count}`` → ``{ok, items: [{date, time, label,
  note?}], summary}``.
- ``POST /api/carga-masiva/comprobar`` — ``{batch, tiktok_consent, rows?}`` → per-row issues, issues of the
  common settings, daily-limit warnings and the date summary (``bulk.check_batch``).
- ``POST /api/carga-masiva/programar`` — ``{batch, action: schedule|draft, tiktok_consent}`` → result with
  one entry per post (``bulk.submit_batch``; idempotent per batch token).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from planificador import bulk, config, rules
from planificador.bulk import BatchError
from planificador.deps import ActiveBrand, ClockDep, DbSession
from planificador.models import Brand, MediaAsset
from planificador.timeutil import Clock, local_wall_iso, to_brand_local

log = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}
MAX_BODY_ROWS = bulk.MAX_ROWS


def _tz(brand: Brand) -> str:
    return brand.timezone or "Europe/Madrid"


def _error(exc: BatchError) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "message": exc.message, "code": exc.code}
    if exc.report is not None:
        body["report"] = exc.report
    return JSONResponse(body, status_code=exc.status_code, headers=_NO_STORE)


async def _body(request: Request, *, optional: bool = False) -> dict[str, Any]:
    """The JSON object sent; ``optional``: an empty body is ``{}``."""
    if optional and not await request.body():
        return {}
    try:
        data = await request.json()
    except (ValueError, TypeError):
        raise BatchError("Los datos enviados no son válidos.") from None
    if not isinstance(data, dict):
        raise BatchError("Los datos enviados no son válidos.")
    return data


def _same_brand(body: dict[str, Any], brand: Brand) -> None:
    """409 ``brand_changed`` when the page was opened for another brand (``batch.brand_id``/``brand_id``)."""
    batch = body.get("batch")
    bulk.ensure_brand(batch if bulk.batch_brand_id(batch) is not None else body, brand)


def _bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "si", "sí", "on")


# ============================================================================================= media


def media_entry(asset: MediaAsset) -> dict[str, Any]:
    """Library item + compatibility per network (same shape as the upload endpoint's JSON items)."""
    from planificador.routes.media import asset_json

    item = asset_json(asset)
    width, height = asset.width or 0, asset.height or 0
    item["orientation"] = (
        "vertical" if height > width else ("horizontal" if width > height else "square") if width else None
    )
    return item


def media_index(db: Session, brand: Brand, ids: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for media_id in ids:
        asset = db.get(MediaAsset, media_id)
        if asset is None or asset.deleted_at is not None or asset.brand_id not in (None, brand.id):
            continue
        out[str(asset.id)] = media_entry(asset)
    return out


def _parse_ids(value: str | None) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", value or ""):
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out[:MAX_BODY_ROWS]


# ============================================================================================== page


def _suggested_slots(db: Session, brand: Brand, clock: Clock) -> tuple[list[str], str]:
    """Start times from the brand's best times (``analytics.best_times``), else its general pattern."""
    try:
        from planificador import analytics

        result = analytics.best_times(db, brand.id, None, clock)
        slots = bulk.suggested_slots(result.get("matrix"))
        return slots, "datos" if result.get("source") == "datos" else "patron_general"
    except Exception:  # noqa: BLE001 - analytics unavailable: plain defaults
        log.debug("best_times unavailable for bulk slots", exc_info=True)
        db.rollback()
        return list(bulk.DEFAULT_SLOTS), "defecto"


def _new_batch(db: Session, brand: Brand, clock: Clock, slots: list[str] | None = None) -> dict[str, Any]:
    batch = bulk.empty_batch(brand_id=brand.id)
    batch["distribution"]["slots"] = list(slots) if slots else _suggested_slots(db, brand, clock)[0]
    return batch


def _accounts_json(db: Session, brand: Brand) -> list[dict[str, Any]]:
    from planificador.routes.composer import composer_accounts

    out = []
    for _account, info in composer_accounts(db, brand, demo_mode=config.is_demo_mode(db)):
        if info["status"] == "disconnected":
            continue
        out.append(info)
    return out


def page_state(db: Session, brand: Brand, clock: Clock) -> dict[str, Any]:
    from planificador.routes.composer import (
        LANGUAGES,
        TIKTOK_BC_URL,
        TIKTOK_MUSIC_URL,
        YT_DEFAULT_CATEGORIES,
        YT_PRIVACY,
    )

    tz = _tz(brand)
    slots, slots_source = _suggested_slots(db, brand, clock)
    stored = bulk.load_batch(db, brand.id)
    restored = False
    interrupted: dict[str, Any] | None = None
    if stored is not None:
        token = str(stored.get("token") or "")
        state = bulk.submission_state(db, token)
        if state is not None and state.get("status") == "done":
            stored = None  # scheduled already: start a new one
        elif state is not None:
            interrupted = bulk.interrupted_submission(db, token, clock.now())
    if stored is not None:
        try:
            batch = bulk.sanitize_batch(stored, networks=bulk.account_networks(db, brand))
            bulk.remap_demo_key(db, brand, batch)
            batch["brand_id"] = brand.id
            batch["updated_at"] = stored.get("updated_at")
            batch["revision"] = stored.get("revision")
            if interrupted is not None:
                # "Programar todo" was cut short: the rows it created are not editable any more (resuming
                # skips them); the page keeps the others and says how to finish.
                done = interrupted.get("done") or {}
                total = len(batch["rows"])
                batch["rows"] = [r for r in batch["rows"] if r["id"] not in done]
                interrupted = {"done": total - len(batch["rows"]), "left": len(batch["rows"])}
            restored = (
                bool(batch["rows"])
                or any(a.get("selected") for a in batch["accounts"].values())
                or interrupted is not None
            )
        except BatchError:
            log.warning("Stored bulk batch of brand %s is invalid; starting a new one", brand.id)
            batch = _new_batch(db, brand, clock, slots)
            interrupted = None
    else:
        batch = _new_batch(db, brand, clock, slots)
    now_local = to_brand_local(clock.now(), tz)
    assert now_local is not None
    ids = [int(r["media_id"]) for r in batch["rows"]]
    return {
        "brand": {"id": brand.id, "name": brand.name, "color": brand.color, "timezone": tz},
        "accounts": _accounts_json(db, brand),
        "batch": batch,
        "restored": restored,
        "interrupted": interrupted,
        "media_index": media_index(db, brand, ids),
        "suggested_slots": slots,
        "slots_source": slots_source,
        "today": now_local.date().isoformat(),
        "now_local": (local_wall_iso(clock.now(), tz) or "")[:16],
        "last_result": bulk.last_result(db, brand.id) if not restored else None,
        "demo_mode": config.is_demo_mode(db),
        "privacy_labels": dict(rules.TT_PRIVACY_LABELS),
        "limits": {
            "max_rows": bulk.MAX_ROWS,
            "ig_hashtags": rules.IG_MAX_HASHTAGS,
            "yt_title": rules.YT_TITLE_MAX,
            "max_slots": bulk.MAX_SLOTS,
            "max_per_day": bulk.MAX_PER_DAY,
            "max_every_days": bulk.MAX_EVERY_DAYS,
            "min_lead_minutes": int(bulk.MIN_LEAD.total_seconds() // 60),
        },
        "yt_privacy": [{"value": v, "label": label} for v, label in YT_PRIVACY],
        "yt_categories_default": [{"id": str(i), "title": str(t)} for i, t in YT_DEFAULT_CATEGORIES],
        "languages": [{"value": v, "label": label} for v, label in LANGUAGES],
        "legal": {"music_url": TIKTOK_MUSIC_URL, "bc_url": TIKTOK_BC_URL},
        "urls": {
            "batch": "/api/carga-masiva/lote",
            "discard": "/api/carga-masiva/lote/descartar",
            "media": "/api/carga-masiva/medios",
            "distribute": "/api/carga-masiva/repartir",
            "check": "/api/carga-masiva/comprobar",
            "submit": "/api/carga-masiva/programar",
            "upload": "/biblioteca/subir",
            "picker": "/biblioteca/selector?multiple=1&kind=video",
            "creator_info": "/api/tiktok/{id}/creator-info",
            "yt_categories": "/api/youtube/{id}/categorias",
            "connections": "/conexiones",
            "calendar": "/planificacion",
            "list": "/planificacion/lista",
            "post": "/publicaciones/{id}",
        },
    }


@router.get("/carga-masiva")
def bulk_page(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    from planificador import templating
    from planificador.routes.media import ACCEPT_ATTR

    state = page_state(db, brand, clock)
    return templating.render(
        request,
        "bulk/index.html",
        page_title="Carga masiva",
        state=state,
        brand=brand,
        tz=_tz(brand),
        accept_types=ACCEPT_ATTR,
    )


# ============================================================================================== API


@router.get("/api/carga-masiva/lote")
def get_batch(db: DbSession, brand: ActiveBrand, clock: ClockDep) -> JSONResponse:
    state = page_state(db, brand, clock)
    return JSONResponse(
        {"ok": True, "batch": state["batch"], "media_index": state["media_index"]}, headers=_NO_STORE
    )


@router.post("/api/carga-masiva/lote")
async def save_batch(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    try:
        body = await _body(request)
    except BatchError as exc:
        return _error(exc)

    def work() -> Response:
        try:
            _same_brand(body, brand)
            batch = bulk.sanitize_batch(body.get("batch"), networks=bulk.account_networks(db, brand))
            stored = bulk.save_batch(db, brand.id, batch, clock)
        except BatchError as exc:
            return _error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": stored["revision"],
                "updated_at": stored["updated_at"],
                "rows": len(stored["rows"]),
            },
            headers=_NO_STORE,
        )

    return await run_in_threadpool(work)


@router.post("/api/carga-masiva/lote/descartar")
async def discard_batch(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    try:
        _same_brand(await _body(request, optional=True), brand)
    except BatchError as exc:
        return _error(exc)

    def work() -> Response:
        bulk.discard_batch(db, brand.id)
        return JSONResponse({"ok": True, "batch": _new_batch(db, brand, clock)}, headers=_NO_STORE)

    return await run_in_threadpool(work)


@router.get("/api/carga-masiva/medios")
def bulk_media(db: DbSession, brand: ActiveBrand, ids: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, "items": media_index(db, brand, _parse_ids(ids))}, headers=_NO_STORE)


@router.post("/api/carga-masiva/repartir")
async def distribute(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    try:
        body = await _body(request)
        _same_brand(body, brand)
        tz = _tz(brand)
        now_local = to_brand_local(clock.now(), tz)
        assert now_local is not None
        raw = body.get("distribution") if isinstance(body.get("distribution"), dict) else body
        spec = bulk.parse_spec(raw, today=now_local.date())
        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            raise BatchError("Número de vídeos no válido.") from None
        if count < 1:
            raise BatchError("Añade vídeos antes de repartir las fechas.", code="no_rows")
        if count > bulk.MAX_ROWS:
            raise BatchError(f"Como máximo {bulk.MAX_ROWS} vídeos.", code="too_many")
        picks = bulk.distribute(count, spec, tz, clock.now())
    except BatchError as exc:
        return _error(exc)
    items = [bulk.pick_json(p, tz) for p in picks]
    return JSONResponse(
        {
            "ok": True,
            "items": items,
            "summary": bulk.schedule_summary([p.when for p in picks], tz),
            "notes": [i["note"] for i in items if i.get("note")],
            "start_date": spec.start_date.isoformat(),
        },
        headers=_NO_STORE,
    )


@router.post("/api/carga-masiva/comprobar")
async def check(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    try:
        body = await _body(request)
    except BatchError as exc:
        return _error(exc)

    def work() -> Response:
        try:
            _same_brand(body, brand)
            batch = bulk.sanitize_batch(body.get("batch"), networks=bulk.account_networks(db, brand))
        except BatchError as exc:
            return _error(exc)
        raw_rows = body.get("rows")
        row_ids: list[str] | None = None
        if isinstance(raw_rows, list):
            # Only ids of this batch (the page may still list a row it removed); never truncated below them.
            present = {r["id"] for r in batch["rows"]}
            row_ids = [str(r) for r in raw_rows[: 5 * MAX_BODY_ROWS] if str(r) in present]
        report = bulk.check_batch(
            db, brand, batch, clock, tiktok_consent=_bool(body.get("tiktok_consent")), row_ids=row_ids
        )
        return JSONResponse(report, headers=_NO_STORE)

    return await run_in_threadpool(work)


@router.post("/api/carga-masiva/programar")
async def submit(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    try:
        body = await _body(request)
    except BatchError as exc:
        return _error(exc)
    action = str(body.get("action") or "schedule").strip().lower()

    def work() -> Response:
        try:
            _same_brand(body, brand)
            batch = bulk.sanitize_batch(body.get("batch"), networks=bulk.account_networks(db, brand))
            result = bulk.submit_batch(
                db, brand, batch, action, clock, tiktok_consent=_bool(body.get("tiktok_consent"))
            )
        except BatchError as exc:
            return _error(exc)
        if result.get("remaining_token"):
            stored = bulk.load_batch(db, brand.id)
            result["remaining_batch"] = stored
        return JSONResponse(result, headers=_NO_STORE)

    return await run_in_threadpool(work)

"""Composer — "Crear nueva publicación" (SPEC §1, §7, §15; layout doc 05 §3 and §5b).

Pages (an htmx request — ``HX-Request: true``, e.g. the top bar button aimed at ``#modal-root`` — gets the
full-screen overlay fragment; a normal navigation gets the same composer as a full page):

- ``GET /publicaciones/nueva`` — ``?fecha=<brand wall time ISO>`` (planner slot click), ``?idea=1`` or
  ``?tipo=idea`` (+ ``&etapa=idea|en_progreso|lista``) for the Ideas board, ``?red=<network>`` to preselect a
  network, ``?medios=1,2`` to start with library files.
- ``GET /publicaciones/{id}/editar`` — edit form; read-only (with the reason) when the post cannot be edited.

JSON API (CSRF header required for POST; the browser sends the composer state as JSON):

- ``POST /publicaciones`` / ``POST /publicaciones/{id}`` — body ``{action, layer, text, media_ids,
  scheduled_at (brand wall time "YYYY-MM-DDTHH:MM" or null), notes, idea_stage, targets: [{account_id,
  format, text_override, media_override, options, auto_publish}]}``. ``account_id`` may be ``"demo"`` (demo
  mode): the brand's Demo account is created on the fly. A form post with a ``payload`` JSON field is also
  accepted. Success → ``{ok, post_id, message, redirect}`` plus ``HX-Trigger`` (toast, ``closeModal``,
  ``planner:refresh``, ``posts:changed``); errors → ``{ok: false, message, code, validation?}``.
- ``POST /api/composer/validar`` — same body → per-network issues, counters and which actions are allowed.
- ``GET /api/tiktok/{account_id}/creator-info`` — fresh ``creator_info`` (TikTok posting guidelines);
  cached in ``accounts.meta["creator_info"]`` for validation. Demo/fake accounts get deterministic fake data.
- ``GET /api/youtube/{account_id}/categorias`` and ``/listas`` — category / playlist choices (cached in
  ``accounts.meta``; built-in Spanish categories as fallback).

Previews are rendered client-side (``static/js/composer.js``) from the same state.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config, postsvc, rules
from planificador.deps import ActiveBrand, ClockDep, DbSession, ProviderSettingsDep
from planificador.models import (
    ACCOUNT_STATUS_LABELS,
    Account,
    AccountStatus,
    Brand,
    IdeaStage,
    MediaAsset,
    Network,
    Post,
    PostKind,
)
from planificador.postsvc import GENERAL, PostError, PostInput, TargetInput
from planificador.providers.base import Issue, ProviderError, ProviderSettings
from planificador.timeutil import (
    Clock,
    format_dt_es,
    from_brand_local,
    local_wall_iso,
    parse_wall_iso,
    to_brand_local,
)

log = logging.getLogger(__name__)

router = APIRouter()

DEMO_KEY = "demo"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
YT_CATEGORIES_TTL = timedelta(days=7)
YT_PLAYLISTS_TTL = timedelta(minutes=10)
RECENT_HASHTAGS = 24
MAX_TEXT = 100_000

TIKTOK_MUSIC_URL = "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en"
TIKTOK_BC_URL = "https://www.tiktok.com/legal/page/global/bc-policy/en"

#: YouTube categories assignable in Spain (videoCategories.list, hl=es): fallback when the API is unreachable.
YT_DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("1", "Cine y animación"),
    ("2", "Motor"),
    ("10", "Música"),
    ("15", "Mascotas y animales"),
    ("17", "Deportes"),
    ("19", "Viajes y eventos"),
    ("20", "Videojuegos"),
    ("22", "Gente y blogs"),
    ("23", "Comedia"),
    ("24", "Entretenimiento"),
    ("25", "Noticias y política"),
    ("26", "Consejos y estilo"),
    ("27", "Educación"),
    ("28", "Ciencia y tecnología"),
    ("29", "ONG y activismo"),
]
YT_DEMO_PLAYLISTS: list[tuple[str, str]] = [("PL-demo-1", "Tutoriales"), ("PL-demo-2", "Novedades")]
YT_PRIVACY = [("public", "Pública"), ("unlisted", "Oculto (no listado)"), ("private", "Privado")]
IG_TRIAL = [
    ("", "No"),
    ("MANUAL", "Sí, lo comparto yo en el feed"),
    ("SS_PERFORMANCE", "Sí, según su rendimiento"),
]
DEMO_FAILURES = [
    ("none", "Se publica correctamente"),
    ("transient", "Fallo temporal (se reintenta)"),
    ("rate_limited", "Límite de peticiones"),
    ("permanent", "Rechazada por la red"),
    ("auth", "Cuenta desconectada"),
    ("ambiguous", "Sin respuesta tras enviar"),
]
LANGUAGES = [
    ("", "Según el canal"),
    ("es", "Español"),
    ("es-419", "Español (Latinoamérica)"),
    ("en", "Inglés"),
    ("ca", "Catalán"),
    ("gl", "Gallego"),
    ("eu", "Euskera"),
    ("pt", "Portugués"),
    ("fr", "Francés"),
    ("de", "Alemán"),
    ("it", "Italiano"),
]

_HASHTAG_RE = re.compile(r"(?<![\w&])#(\w{2,60})")


# =========================================================================================== helpers


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def brand_tz(brand: Brand) -> str:
    return brand.timezone or "Europe/Madrid"


def media_item(asset: MediaAsset) -> dict[str, Any]:
    """Same shape as the media picker's ``media:selected`` items."""
    return {
        "id": asset.id,
        "kind": str(asset.kind),
        "name": asset.original_name,
        "thumb_url": f"/medios/{asset.id}/miniatura",
        "url": f"/medios/{asset.id}/archivo",
        "width": asset.width,
        "height": asset.height,
        "duration_s": asset.duration_s,
    }


def _visible_assets(db: Session, brand: Brand, ids: list[int]) -> list[MediaAsset]:
    out: list[MediaAsset] = []
    for asset_id in ids:
        asset = db.get(MediaAsset, asset_id)
        if asset is not None and asset.brand_id in (None, brand.id):
            out.append(asset)
    return out


def is_fake_account(account: Account, settings: ProviderSettings | None) -> bool:
    """Demo-mode stand-ins (Demo network, seeded "demo" accounts, accounts without a token) never call the
    real APIs."""
    if account.network == Network.DEMO:
        return True
    if settings is None or not settings.demo_mode:
        return False
    meta = account.meta or {}
    return (
        account.account_type == "demo"
        or str(account.external_id or "").startswith("demo")
        or bool(meta.get("demo"))
        or not account.access_token_enc
    )


def fake_creator_info(account: Account) -> dict[str, Any]:
    return {
        "nickname": account.name or "Cuenta de prueba",
        "username": account.username or "demo",
        "avatar_url": None,
        "privacy_level_options": [
            "PUBLIC_TO_EVERYONE",
            "MUTUAL_FOLLOW_FRIENDS",
            "FOLLOWER_OF_CREATOR",
            "SELF_ONLY",
        ],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": True,
        "max_video_post_duration_sec": 600,
    }


def _privacy_choices(options: list[str] | None) -> list[dict[str, str]]:
    return [{"value": o, "label": rules.TT_PRIVACY_LABELS.get(o, o)} for o in options or []]


def recent_hashtags(db: Session, brand: Brand) -> list[str]:
    """Most used hashtags in the brand's recent posts (hashtag helper of the composer toolbar)."""
    texts = db.scalars(
        select(Post.text)
        .where(Post.brand_id == brand.id, Post.deleted_at.is_(None))
        .order_by(Post.updated_at.desc())
        .limit(200)
    ).all()
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for idx, text in enumerate(texts):
        for tag in _HASHTAG_RE.findall(text or ""):
            key = tag.lower()
            counts[key] = counts.get(key, 0) + 1
            first_seen.setdefault(key, idx)
    ranked = sorted(counts, key=lambda k: (-counts[k], first_seen[k]))
    return [f"#{k}" for k in ranked[:RECENT_HASHTAGS]]


def default_slot(clock: Clock, tz: str) -> datetime:
    """Next full hour at least 30 minutes from now (brand wall time), as aware UTC."""
    local = to_brand_local(clock.now() + timedelta(minutes=30), tz)
    assert local is not None
    wall = local.replace(minute=0, second=0, microsecond=0, tzinfo=None) + timedelta(hours=1)
    return from_brand_local(wall, tz)


# ====================================================================================== account info


def account_json(account: Account, *, pseudo: bool = False) -> dict[str, Any]:
    """What the composer needs to know about one account (formats, limits, defaults, TikTok/YouTube facts)."""
    network = str(account.network)
    formats = []
    for spec in rules.FORMATS.get(network, []):
        formats.append(
            {
                "key": spec.key,
                "label": spec.label,
                "media": spec.media,
                "disabled": rules.disabled_reason(network, spec.key),
                "limit": rules.char_limit(network, spec.key),
                "defaults": rules.default_options(network, spec.key),
            }
        )
    enabled = [f for f in formats if not f["disabled"]]
    meta = account.meta or {}
    creator = meta.get("creator_info") if network == Network.TIKTOK else None
    status = AccountStatus.CONNECTED.value if pseudo else str(account.status)
    return {
        "key": DEMO_KEY if pseudo else str(account.id),
        "id": None if pseudo else account.id,
        "pseudo": pseudo,
        "network": network,
        "network_label": rules.network_label(network),
        "name": account.name or rules.network_label(network),
        "username": account.username,
        "display": account.display_name,
        "account_type": account.account_type,
        "status": status,
        "status_label": ACCOUNT_STATUS_LABELS.get(status, status),
        "formats": formats,
        "default_format": enabled[0]["key"] if enabled else (formats[0]["key"] if formats else "post"),
        "unit": rules.char_unit(network),
        "auto_publish_default": True if pseudo else postsvc.default_auto_publish(account),
        "youtube_unlocked": postsvc.youtube_unlocked(account) if network == Network.YOUTUBE else None,
        "creator_info": creator if isinstance(creator, dict) else None,
    }


def composer_accounts(
    db: Session, brand: Brand, *, demo_mode: bool, keep_ids: set[int] | None = None
) -> list[tuple[Account, dict[str, Any]]]:
    """Accounts offered in the network row: the brand's non-disconnected accounts (Demo only in demo mode)
    plus any account already used by the post being edited. In demo mode without a Demo account a pseudo
    ``"demo"`` entry is offered (the account is created when the post is validated/saved)."""
    keep_ids = keep_ids or set()
    rows = list(db.scalars(select(Account).where(Account.brand_id == brand.id)))
    order = {n: i for i, n in enumerate(rules.NETWORKS)}
    rows.sort(key=lambda a: (order.get(str(a.network), 99), a.id))
    out: list[tuple[Account, dict[str, Any]]] = []
    has_demo = False
    for account in rows:
        if account.id not in keep_ids:
            if account.status == AccountStatus.DISCONNECTED:
                continue
            if account.network == Network.DEMO and not demo_mode:
                continue
        if account.network == Network.DEMO:
            has_demo = True
        out.append((account, account_json(account)))
    if demo_mode and not has_demo:
        pseudo = Account(network=Network.DEMO, external_id="demo", name="Cuenta Demo", username="demo")
        out.append((pseudo, account_json(pseudo, pseudo=True)))
    return out


# ============================================================================================ context


def _target_json(t: TargetInput) -> dict[str, Any]:
    return {
        "format": t.format,
        "text_override": t.text_override,
        "media_override": list(t.media_override) if t.media_override is not None else None,
        "options": t.options,
        "auto_publish": bool(t.auto_publish),
    }


def composer_context(
    request: Request,
    db: Session,
    brand: Brand,
    clock: Clock,
    *,
    post: Post | None = None,
    scheduled_at: datetime | None = None,
    idea: bool = False,
    idea_stage: str | None = None,
    preselect_network: str | None = None,
    media_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Template context + the JSON state the Alpine component starts from."""
    tz = brand_tz(brand)
    demo_mode = config.is_demo_mode(db)
    form = postsvc.post_input_from_post(post) if post is not None else None
    keep = {t.account_id for t in form.targets} if form else set()
    accounts = composer_accounts(db, brand, demo_mode=demo_mode, keep_ids=keep)

    readonly_reason = postsvc.edit_lock_reason(post) if post is not None else None
    if post is not None:
        idea = post.kind == PostKind.IDEA
        scheduled_at = post.scheduled_at
    elif scheduled_at is None and not idea:
        scheduled_at = default_slot(clock, tz)

    selected: list[str] = []
    targets: dict[str, Any] = {}
    if form is not None:
        for t in form.targets:
            selected.append(str(t.account_id))
            targets[str(t.account_id)] = _target_json(t)
    elif preselect_network:
        for _account, info in accounts:
            if info["network"] == preselect_network:
                selected.append(info["key"])
                break

    base_ids = list(form.media_ids) if form is not None else list(media_ids or [])
    extra_ids: set[int] = set()
    if form is not None:
        for t in form.targets:
            extra_ids.update(t.media_override or [])
            thumb = (t.options or {}).get("thumbnail_media_id")
            if thumb is not None and str(thumb).isdigit():
                extra_ids.add(int(thumb))
    assets = {a.id: a for a in _visible_assets(db, brand, list(dict.fromkeys(base_ids + sorted(extra_ids))))}
    media = [media_item(assets[i]) for i in base_ids if i in assets]

    title = (
        "Editar publicación" if post is not None else ("Nueva idea" if idea else "Crear nueva publicación")
    )
    if readonly_reason:
        title = "Ver publicación"
    state = {
        "mode": "edit" if post is not None else "create",
        "layer": "overlay",
        "post_id": post.id if post is not None else None,
        "readonly": bool(readonly_reason),
        "readonly_reason": readonly_reason,
        "primary": "idea" if idea else "schedule",
        "brand": {"id": brand.id, "name": brand.name, "color": brand.color, "timezone": tz},
        "accounts": [info for _a, info in accounts],
        "selected": selected,
        "targets": targets,
        "text": form.text if form else "",
        "media": media,
        "media_index": {str(a.id): media_item(a) for a in assets.values()},
        "notes": (form.notes if form else None) or "",
        "idea_stage": (form.idea_stage if form else None) or (idea_stage if idea else None),
        "scheduled_local": (local_wall_iso(scheduled_at, tz) or "")[:16] or None,
        "now_local": (local_wall_iso(clock.now(), tz) or "")[:16],
        "demo_mode": demo_mode,
        "recent_hashtags": recent_hashtags(db, brand),
        "privacy_labels": dict(rules.TT_PRIVACY_LABELS),
        "limits": {
            "ig_hashtags": rules.IG_MAX_HASHTAGS,
            "ig_collaborators": rules.IG_COLLABORATORS_MAX,
            "yt_title": rules.YT_TITLE_MAX,
            "yt_tags": rules.YT_TAGS_MAX,
            "fb_title": rules.FB_TITLE_MAX,
        },
        "urls": {
            "save": f"/publicaciones/{post.id}" if post is not None else "/publicaciones",
            "validate": "/api/composer/validar",
            "creator_info": "/api/tiktok/{id}/creator-info",
            "yt_categories": "/api/youtube/{id}/categorias",
            "yt_playlists": "/api/youtube/{id}/listas",
            "picker": "/biblioteca/selector",
            "upload": "/biblioteca/subir",
            "connections": "/conexiones",
            "back": "/ideas" if idea else "/planificacion",
            "detail": f"/publicaciones/{post.id}" if post is not None else None,
        },
        "legal": {"music_url": TIKTOK_MUSIC_URL, "bc_url": TIKTOK_BC_URL},
        "yt_categories_default": _choices(YT_DEFAULT_CATEGORIES),
    }
    return {
        "state": state,
        "accounts": accounts,
        "brand": brand,
        "post": post,
        "readonly_reason": readonly_reason,
        "is_idea": idea,
        "tz": tz,
        "yt_privacy": YT_PRIVACY,
        "ig_trial": IG_TRIAL,
        "demo_failures": DEMO_FAILURES,
        "languages": LANGUAGES,
        "composer_title": title,
    }


def _render_composer(request: Request, ctx: dict[str, Any]) -> Response:
    from planificador.templating import render

    if _is_htmx(request):
        return render(request, "composer/overlay.html", layer="overlay", **ctx)
    ctx["state"]["layer"] = "page"
    return render(request, "composer/page.html", layer="page", page_title=ctx["composer_title"], **ctx)


def _parse_media_param(value: str | None) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", value or ""):
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out[:20]


# ============================================================================================== pages


@router.get("/publicaciones/nueva")
def composer_new(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    fecha: str | None = None,
    idea: str | None = None,
    tipo: str | None = None,
    etapa: str | None = None,
    red: str | None = None,
    medios: str | None = None,
) -> Response:
    tz = brand_tz(brand)
    is_idea = (idea or "").lower() in ("1", "true", "si", "sí") or (tipo or "").lower() == "idea"
    scheduled_at: datetime | None = None
    if fecha:
        try:
            wall: datetime | None = parse_wall_iso(fecha)
        except ValueError:
            wall = None
        if wall is not None:
            if len(fecha.strip()) <= 10:  # a day without time (month view) → 10:00
                wall = wall.replace(hour=10, minute=0)
            scheduled_at = from_brand_local(wall, tz)
    stage = None
    if is_idea:
        try:
            stage = IdeaStage((etapa or "idea").lower()).value
        except ValueError:
            stage = IdeaStage.IDEA.value
    ctx = composer_context(
        request,
        db,
        brand,
        clock,
        scheduled_at=scheduled_at,
        idea=is_idea,
        idea_stage=stage,
        preselect_network=red if red in rules.NETWORKS else None,
        media_ids=_parse_media_param(medios),
    )
    return _render_composer(request, ctx)


@router.get("/publicaciones/{post_id:int}/editar")
def composer_edit(
    request: Request, post_id: int, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    post = db.get(Post, post_id)
    if post is None or post.brand_id != brand.id or post.deleted_at is not None:
        raise HTTPException(status_code=404, detail="La publicación no existe o está en la papelera.")
    return _render_composer(request, composer_context(request, db, brand, clock, post=post))


# =========================================================================================== payload


class PayloadError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def _read_payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            raw = form.get("payload")
            body = json.loads(str(raw)) if raw else {}
            if form.get("action") and isinstance(body, dict):
                body.setdefault("action", str(form.get("action")))
    except (ValueError, TypeError) as exc:
        raise PayloadError("Los datos enviados no son válidos.") from exc
    if not isinstance(body, dict):
        raise PayloadError("Los datos enviados no son válidos.")
    return body


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise PayloadError("Los datos enviados no son válidos.")
    return str(value)


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "si", "sí", "on", "yes")


def _ids(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PayloadError("La lista de archivos no es válida.")
    out: list[int] = []
    for v in value:
        if isinstance(v, dict):
            v = v.get("id")
        if isinstance(v, bool):
            raise PayloadError("La lista de archivos no es válida.")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise PayloadError("La lista de archivos no es válida.") from None
        if n not in out:
            out.append(n)
    return out


def parse_payload(db: Session, brand: Brand, body: dict[str, Any]) -> tuple[PostInput, dict[str, int]]:
    """Composer JSON → :class:`PostInput` (+ mapping of the pseudo ``"demo"`` key to the account created)."""
    tz = brand_tz(brand)
    text = _opt_str(body.get("text")) or ""
    if len(text) > MAX_TEXT:
        raise PayloadError("El texto es demasiado largo.")
    raw_when = body.get("scheduled_at")
    scheduled_at: datetime | None = None
    if raw_when not in (None, ""):
        try:
            wall = parse_wall_iso(str(raw_when))
        except ValueError:
            raise PayloadError("La fecha de publicación no es válida.") from None
        scheduled_at = from_brand_local(wall, tz)
    raw_targets = body.get("targets") or []
    if not isinstance(raw_targets, list):
        raise PayloadError("La lista de redes no es válida.")
    account_map: dict[str, int] = {}
    targets: list[TargetInput] = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise PayloadError("La lista de redes no es válida.")
        account_id: Any = raw.get("account_id")
        if str(account_id) == DEMO_KEY:
            if not config.is_demo_mode(db):
                raise PayloadError("Activa el modo prueba en Ajustes para usar la red Demo.")
            demo = postsvc.ensure_demo_account(db, brand.id)
            account_map[DEMO_KEY] = demo.id
            account_id = demo.id
        if isinstance(account_id, bool):
            raise PayloadError("Cuenta no válida.")
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            raise PayloadError("Cuenta no válida.") from None
        options = raw.get("options") or {}
        if not isinstance(options, dict):
            raise PayloadError("Las opciones de la red no son válidas.")
        override = raw.get("text_override")
        media_override = raw.get("media_override")
        targets.append(
            TargetInput(
                account_id=account_id,
                format=str(raw.get("format") or ""),
                text_override=_opt_str(override) if override is not None else None,
                media_override=_ids(media_override) if media_override is not None else None,
                options=options,
                auto_publish=_bool(raw.get("auto_publish"), True),
            )
        )
    stage = body.get("idea_stage")
    is_idea = body.get("action") == "idea" or (body.get("action") is None and body.get("kind") == "idea")
    data = PostInput(
        brand_id=brand.id,
        text=text,
        media_ids=_ids(body.get("media_ids")),
        scheduled_at=scheduled_at,
        kind="idea" if is_idea else "scheduled",
        idea_stage=str(stage) if stage else None,
        notes=_opt_str(body.get("notes")),
        targets=targets,
    )
    return data, account_map


# ======================================================================================== validation


def validation_json(
    db: Session, brand: Brand, data: PostInput, result: dict[int, list[Issue]], account_map: dict[str, int]
) -> dict[str, Any]:
    """Validation result for the browser: flat error/warning lists (with the account key and network),
    per-account character counters and which actions are possible."""
    tz = brand_tz(brand)
    accounts = {t.account_id: db.get(Account, t.account_id) for t in data.targets}
    reverse = {v: k for k, v in account_map.items()}
    items: list[dict[str, Any]] = []
    for key, issues in result.items():
        account = accounts.get(key) if key != GENERAL else None
        for issue in issues:
            entry = postsvc.issue_dict(issue)
            entry["account_id"] = key if key != GENERAL else None
            entry["account_key"] = reverse.get(key, str(key)) if key != GENERAL else None
            entry["network"] = str(account.network) if account is not None else None
            entry["network_label"] = (
                rules.network_label(str(account.network)) if account is not None else None
            )
            items.append(entry)
    counters: dict[str, Any] = {}
    for t in data.targets:
        account = accounts.get(t.account_id)
        if account is None or rules.format_spec(str(account.network), t.format) is None:
            continue
        network = str(account.network)
        text = t.text_override if t.text_override is not None else data.text
        counters[reverse.get(t.account_id, str(t.account_id))] = {
            "network": network,
            "length": rules.text_length(network, text),
            "limit": rules.char_limit(network, t.format),
            "unit": rules.char_unit(network),
            "hashtags": rules.count_hashtags(text) if network == Network.INSTAGRAM else None,
        }
    return {
        "ok": True,
        "errors": [i for i in items if i["level"] == "error"],
        "warnings": [i for i in items if i["level"] != "error"],
        "counters": counters,
        "can_schedule": not postsvc.has_blocking_errors(result, "schedule"),
        "can_publish_now": not postsvc.has_blocking_errors(result, "publish_now"),
        "scheduled_label": format_dt_es(data.scheduled_at, tz, year=True) if data.scheduled_at else None,
        "account_map": account_map,
    }


def _payload_error(exc: PayloadError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "message": exc.message, "code": "invalid_payload"}, status_code=exc.status_code
    )


@router.post("/api/composer/validar")
async def composer_validate(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    try:
        body = await _read_payload(request)
    except PayloadError as exc:
        return _payload_error(exc)

    def work() -> dict[str, Any]:
        data, account_map = parse_payload(db, brand, body)
        action = body.get("action") if body.get("action") in postsvc.ACTIONS else None
        result = postsvc.validate_post(db, data, clock=clock, action=action)
        return validation_json(db, brand, data, result, account_map)

    try:
        payload = await run_in_threadpool(work)
    except PayloadError as exc:
        return _payload_error(exc)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


# ============================================================================================== save

_SUCCESS = {
    "schedule": "Publicación programada para el {when}.",
    "draft": "Borrador guardado.",
    "idea": "Idea guardada.",
    "publish_now": "Publicando ahora. Te avisaremos si algo falla.",
}


def _success_message(action: str, post: Post, brand: Brand) -> str:
    when = format_dt_es(post.scheduled_at, brand_tz(brand)) if post.scheduled_at else ""
    message = _SUCCESS[action].format(when=when)
    if action == "schedule" and any(not t.auto_publish for t in post.targets):
        message += " Te avisaremos a esa hora para la publicación asistida."
    if action == "publish_now" and post.targets and not any(_sends_itself(t) for t in post.targets):
        message = "Listo para publicar: abre el aviso de «Publicación asistida» para terminarla tú."
    return message


def _sends_itself(target: Any) -> bool:
    """Whether the app sends this target itself (auto-publish, or TikTok's "send to the app" inbox mode)."""
    options = target.options or {}
    return bool(target.auto_publish) or (
        str(target.network) == "tiktok" and str(options.get("mode") or "").lower() == "inbox"
    )


def _redirect_for(action: str, post: Post, brand: Brand) -> str:
    if action == "idea":
        return "/ideas"
    if post.scheduled_at is not None:
        local = to_brand_local(post.scheduled_at, brand_tz(brand))
        if local is not None:
            return f"/planificacion?fecha={local.date().isoformat()}"
    return "/planificacion"


def _post_error_json(
    exc: PostError, db: Session, brand: Brand, data: PostInput, account_map: dict[str, int]
) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "message": exc.message, "code": exc.code}
    if exc.issues is not None:
        body["validation"] = validation_json(db, brand, data, exc.issues, account_map)
    return JSONResponse(body, status_code=exc.status_code)


async def _save(request: Request, db: Session, brand: Brand, clock: Clock, post_id: int | None) -> Response:
    try:
        body = await _read_payload(request)
    except PayloadError as exc:
        return _payload_error(exc)
    action = str(body.get("action") or "").strip().lower()
    if action not in postsvc.ACTIONS:
        return JSONResponse(
            {"ok": False, "message": "Acción no válida.", "code": "invalid_action"}, status_code=422
        )
    layer = str(body.get("layer") or "overlay")

    def work() -> Response:
        if post_id is not None:
            existing = db.get(Post, post_id)
            if existing is None or existing.brand_id != brand.id:
                return JSONResponse(
                    {"ok": False, "message": "La publicación no existe.", "code": "not_found"},
                    status_code=404,
                )
        try:
            data, account_map = parse_payload(db, brand, body)
        except PayloadError as exc:
            return _payload_error(exc)
        try:
            if post_id is None:
                post = postsvc.create_post(db, data, action, clock)
            else:
                post = postsvc.update_post(db, post_id, data, action, clock)
        except PostError as exc:
            return _post_error_json(exc, db, brand, data, account_map)
        db.refresh(post)
        message = _success_message(action, post, brand)
        if layer == "page":
            from planificador.templating import flash

            flash(request, message, "success")
        trigger = {
            "toast": {"message": message, "kind": "success"},
            "closeModal": True,
            "planner:refresh": True,
            "posts:changed": {"post_id": post.id, "action": action},
        }
        return JSONResponse(
            {
                "ok": True,
                "post_id": post.id,
                "action": action,
                "message": message,
                "redirect": _redirect_for(action, post, brand),
                "status": str(post.display_status),
                "account_map": account_map,
            },
            headers={"HX-Trigger": json.dumps(trigger, ensure_ascii=True)},
        )

    return await run_in_threadpool(work)


@router.post("/publicaciones")
async def composer_create(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    return await _save(request, db, brand, clock, None)


@router.post("/publicaciones/{post_id:int}")
async def composer_update(
    request: Request, post_id: int, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    return await _save(request, db, brand, clock, post_id)


# ================================================================================= provider helpers


def _brand_account(db: Session, brand: Brand, account_id: int, networks: tuple[str, ...]) -> Account:
    account = db.get(Account, account_id)
    if account is None or account.brand_id != brand.id or str(account.network) not in networks:
        raise HTTPException(status_code=404, detail="La cuenta no existe en esta marca.")
    return account


def _iso_now(clock: Clock) -> str:
    return clock.now().astimezone(UTC).isoformat(timespec="seconds")


def _cache_fresh(entry: Any, clock: Clock, ttl: timedelta) -> bool:
    if not isinstance(entry, dict) or "fetched_at" not in entry or not entry.get("items"):
        return False
    try:
        fetched = datetime.fromisoformat(str(entry["fetched_at"]))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return clock.now() - fetched < ttl


def _store_meta(db: Session, account: Account, key: str, value: Any) -> None:
    meta = dict(account.meta or {})
    meta[key] = value
    account.meta = meta
    db.commit()


_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/tiktok/{account_id:int}/creator-info")
def tiktok_creator_info(
    account_id: int, db: DbSession, brand: ActiveBrand, clock: ClockDep, settings: ProviderSettingsDep
) -> JSONResponse:
    """Fresh ``creator_info`` (nickname, privacy options, interaction flags, max duration). TikTok requires
    it every time the posting screen is shown; the copy cached in ``accounts.meta`` feeds validation."""
    account = _brand_account(db, brand, account_id, ("tiktok", "demo"))
    fake = is_fake_account(account, settings)
    if fake:
        info = fake_creator_info(account)
    else:
        try:
            from planificador.providers import tiktok

            with httpx.Client(timeout=HTTP_TIMEOUT) as http:
                info = tiktok.query_creator_info(account.to_creds(), settings, http).to_dict()
        except ProviderError as exc:
            cached = (account.meta or {}).get("creator_info")
            cached = cached if isinstance(cached, dict) else None
            return JSONResponse(
                {
                    "ok": False,
                    "message": exc.message,
                    "code": exc.code,
                    "creator_info": cached,
                    "privacy_options": _privacy_choices((cached or {}).get("privacy_level_options")),
                },
                headers=_NO_STORE,
            )
    info = {k: v for k, v in info.items() if k != "fetched_at"}
    if account.network == Network.TIKTOK:
        _store_meta(db, account, "creator_info", {**info, "fetched_at": _iso_now(clock)})
    return JSONResponse(
        {
            "ok": True,
            "demo": fake,
            "creator_info": info,
            "privacy_options": _privacy_choices(info.get("privacy_level_options")),
        },
        headers=_NO_STORE,
    )


def _choices(items: Any) -> list[dict[str, str]]:
    return [{"id": str(i), "title": str(t)} for i, t in items]


def _keep_refreshed_tokens(db: Session, account: Account, creds: Any) -> None:
    """The YouTube helpers refresh an expired access token in place on ``creds``: save it (encrypted) so the
    next call does not refresh again."""
    if not creds.access_token or creds.access_token == account.access_token:
        return
    from planificador.providers.base import TokenUpdate

    account.apply_token_update(
        TokenUpdate(
            access_token=creds.access_token,
            token_expires_at=creds.token_expires_at,
            refresh_token=creds.refresh_token if creds.refresh_token != account.refresh_token else None,
            refresh_expires_at=creds.refresh_expires_at,
        )
    )
    db.commit()


def _youtube_list(
    db: Session,
    account: Account,
    clock: Clock,
    settings: ProviderSettings,
    *,
    key: str,
    ttl: timedelta,
    fetch: Any,
    fallback: list[tuple[str, str]],
    refresh: bool,
) -> JSONResponse:
    cached = (account.meta or {}).get(key)
    if not refresh and _cache_fresh(cached, clock, ttl):
        return JSONResponse(
            {"ok": True, "cached": True, "items": _choices(cached["items"])}, headers=_NO_STORE
        )
    creds = account.to_creds()
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as http:
            items = fetch(creds, settings, http)
    except ProviderError as exc:
        _keep_refreshed_tokens(db, account, creds)
        previous = cached["items"] if isinstance(cached, dict) and cached.get("items") else fallback
        return JSONResponse(
            {"ok": False, "message": exc.message, "items": _choices(previous)}, headers=_NO_STORE
        )
    _keep_refreshed_tokens(db, account, creds)
    items = list(items) or list(fallback)
    _store_meta(db, account, key, {"items": [list(i) for i in items], "fetched_at": _iso_now(clock)})
    return JSONResponse({"ok": True, "items": _choices(items)}, headers=_NO_STORE)


@router.get("/api/youtube/{account_id:int}/categorias")
def youtube_categories(
    account_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    settings: ProviderSettingsDep,
    refrescar: int = 0,
) -> JSONResponse:
    account = _brand_account(db, brand, account_id, ("youtube",))
    if is_fake_account(account, settings):
        return JSONResponse(
            {"ok": True, "demo": True, "items": _choices(YT_DEFAULT_CATEGORIES)}, headers=_NO_STORE
        )
    from planificador.providers import youtube

    return _youtube_list(
        db,
        account,
        clock,
        settings,
        key="yt_categories",
        ttl=YT_CATEGORIES_TTL,
        fetch=lambda creds, s, http: youtube.list_categories(creds, s, http, region="ES", hl="es"),
        fallback=YT_DEFAULT_CATEGORIES,
        refresh=bool(refrescar),
    )


@router.get("/api/youtube/{account_id:int}/listas")
def youtube_playlists(
    account_id: int,
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    settings: ProviderSettingsDep,
    refrescar: int = 0,
) -> JSONResponse:
    account = _brand_account(db, brand, account_id, ("youtube",))
    if is_fake_account(account, settings):
        return JSONResponse(
            {"ok": True, "demo": True, "items": _choices(YT_DEMO_PLAYLISTS)}, headers=_NO_STORE
        )
    from planificador.providers import youtube

    return _youtube_list(
        db,
        account,
        clock,
        settings,
        key="yt_playlists",
        ttl=YT_PLAYLISTS_TTL,
        fetch=youtube.list_playlists,
        fallback=[],
        refresh=bool(refrescar),
    )

"""Post detail drawer, per-network target actions, "Publicación asistida" modal and media ZIP (SPEC §10, §11).

Pages / fragments:

- ``GET  /publicaciones/{id}``        post detail: a right-side drawer for htmx requests (``#drawer-root``),
  a full page otherwise. ``?parcial=1&modo=drawer|pagina`` returns only the ``#post-detail`` block (the
  drawer refreshes itself with it on ``posts:changed`` / ``post:saved``).
- ``GET  /objetivos/{id}``            same, for the post of a target (deep link from notifications; the
  target's card is highlighted).
- ``GET  /objetivos/{id}/asistida``   "Publicación asistida" modal (htmx) / page: copy text (YouTube: title,
  description, tags), download media one by one or as a ZIP, open the network, checklist, mark as published.
- ``GET  /objetivos/{id}/medios.zip`` streamed ZIP of the target's media (stored, no recompression).

Actions (POST, CSRF; htmx answers with toasts via ``HX-Trigger`` + ``posts:changed`` /
``notifications:refresh``; plain form posts are redirected back with a flash message):

- ``/objetivos/{id}/reintentar`` (``confirmado=1`` required for ``unknown``) → ``engine.retry_target``
- ``/objetivos/{id}/cancelar``    → ``engine.cancel_target``
- ``/objetivos/{id}/marcar-publicado`` (optional ``url``; ``origen=asistida`` also closes the modal)
  → ``engine.mark_done_manually``
- ``/publicaciones/{id}/publicar-ahora`` → ``engine.publish_now``
- ``/publicaciones/{id}/duplicar`` · ``/papelera`` · ``/restaurar`` → ``postsvc.duplicate_post`` /
  ``delete_post`` / ``restore_post``

Engine and post-service functions are imported lazily (other modules own them).
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from planificador.deps import ClockDep, DbSession
from planificador.models import (
    NETWORK_LABELS,
    Account,
    AccountStatus,
    MediaAsset,
    Post,
    PostEvent,
    PostTarget,
    TargetStatus,
)
from planificador.timeutil import Clock, ensure_utc, format_dt_es

log = logging.getLogger(__name__)

router = APIRouter()

# ------------------------------------------------------------------------------------------ constants

#: The open post detail polls while a target is publishing or due within this lead time.
IN_FLIGHT_LEAD = timedelta(minutes=2)

# Fallbacks mirroring engine.py (used only if the engine module cannot be imported).
_PUBLISH_NOW_FROM = frozenset(
    {TargetStatus.DRAFT, TargetStatus.SCHEDULED, TargetStatus.ERROR, TargetStatus.MISSED}
)
_RETRY_FROM = frozenset({TargetStatus.ERROR, TargetStatus.MISSED, TargetStatus.UNKNOWN})
_CANCEL_FROM = frozenset(
    {
        TargetStatus.DRAFT,
        TargetStatus.SCHEDULED,
        TargetStatus.MANUAL_PENDING,
        TargetStatus.MISSED,
        TargetStatus.ERROR,
        TargetStatus.UNKNOWN,
    }
)
_MARK_DONE_FROM = frozenset(
    {
        TargetStatus.MANUAL_PENDING,
        TargetStatus.UNKNOWN,
        TargetStatus.ERROR,
        TargetStatus.MISSED,
        TargetStatus.SENT_TO_APP,
    }
)

FORMAT_LABELS = {
    "post": "Publicación",
    "reel": "Reel",
    "story": "Historia",
    "video": "Vídeo",
    "short": "Short",
    "photo": "Fotos",
}
TT_PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Público",
    "MUTUAL_FOLLOW_FRIENDS": "Amigos mutuos",
    "FOLLOWER_OF_CREATOR": "Seguidores",
    "SELF_ONLY": "Solo yo",
}
YT_PRIVACY_LABELS = {"public": "Pública", "unlisted": "Oculto (no listado)", "private": "Privado"}
#: Assignable YouTube categories (Spanish names, regionCode=ES) used in the assisted checklist.
YT_CATEGORIES = {
    "1": "Cine y animación",
    "2": "Motor",
    "10": "Música",
    "15": "Mascotas y animales",
    "17": "Deportes",
    "19": "Viajes y eventos",
    "20": "Videojuegos",
    "22": "Gente y blogs",
    "23": "Humor",
    "24": "Entretenimiento",
    "25": "Noticias y política",
    "26": "Consejos y estilo",
    "27": "Educación",
    "28": "Ciencia y tecnología",
    "29": "ONG y activismo",
}
#: Where "Abrir red" sends the user (desktop web).
NETWORK_OPEN_LABELS = {
    "facebook": "Abrir Facebook",
    "instagram": "Abrir Instagram",
    "tiktok": "Abrir TikTok Studio",
    "youtube": "Abrir YouTube Studio",
}
ZIP_CHUNK = 1 << 20

_VALID_MODES = ("drawer", "pagina")


# ------------------------------------------------------------------------------------------ helpers


def _engine() -> Any:
    from planificador import engine

    return engine


def _postsvc() -> Any:
    from planificador import postsvc

    return postsvc


def _status_sets() -> dict[str, frozenset]:
    try:
        engine = _engine()
        return {
            "publish_now": frozenset(engine.PUBLISH_NOW_FROM),
            "retry": frozenset(engine.RETRY_FROM),
            "cancel": frozenset(engine.CANCEL_FROM),
            "mark_done": frozenset(engine.MARK_DONE_FROM),
        }
    except Exception:  # noqa: BLE001 - engine under construction
        return {
            "publish_now": _PUBLISH_NOW_FROM,
            "retry": _RETRY_FROM,
            "cancel": _CANCEL_FROM,
            "mark_done": _MARK_DONE_FROM,
        }


def _user_errors() -> tuple[type[Exception], ...]:
    """Exception types whose ``message`` is a Spanish, user-facing explanation."""
    found: list[type[Exception]] = []
    for loader, name in ((_engine, "EngineError"), (_postsvc, "PostError")):
        try:
            cls = getattr(loader(), name, None)
        except Exception:  # noqa: BLE001
            cls = None
        if isinstance(cls, type) and issubclass(cls, Exception):
            found.append(cls)
    return tuple(found)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def network_label(network: Any) -> str:
    key = str(network or "")
    return NETWORK_LABELS.get(key, key.capitalize())


def format_label(network: Any, fmt: Any) -> str:
    try:
        from planificador import rules

        label = rules.format_label(str(network), str(fmt))
        if label and label != str(fmt):
            return str(label)
    except Exception:  # noqa: BLE001
        pass
    return FORMAT_LABELS.get(str(fmt or ""), str(fmt or "").capitalize())


def target_text(target: PostTarget) -> str:
    """Final text for the target (``postsvc.target_text`` when available)."""
    try:
        return str(_postsvc().target_text(target))
    except Exception:  # noqa: BLE001
        return target.effective_text or ""


def target_media(db: Session, target: PostTarget) -> list[MediaAsset]:
    """Effective media of the target in order (``postsvc.target_media`` when available)."""
    try:
        return list(_postsvc().target_media(db, target))
    except Exception:  # noqa: BLE001
        out: list[MediaAsset] = []
        for media_id in target.effective_media_ids:
            asset = db.get(MediaAsset, int(media_id))
            if asset is not None and asset.deleted_at is None:
                out.append(asset)
        return out


def _asset_path(asset: MediaAsset) -> Path | None:
    try:
        from planificador import media

        path = media.asset_path(asset)
    except Exception:  # noqa: BLE001 - path outside the media dir or media module unavailable
        return None
    return path if path.is_file() else None


def edit_lock_reason(post: Post) -> str | None:
    try:
        return _postsvc().edit_lock_reason(post)
    except Exception:  # noqa: BLE001
        if post.deleted_at is not None:
            return "La publicación está en la papelera. Restáurala para editarla."
        return None


def _snippet(text: str | None, limit: int = 80) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _post_tz(post: Post) -> str:
    tz = getattr(post.brand, "timezone", None) if post.brand is not None else None
    return tz or "Europe/Madrid"


def _same_origin_path(request: Request, value: str | None) -> str | None:
    """A local path (``/x?y``) from a form field or Referer; never an absolute or protocol-relative URL."""
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        if parts.netloc != request.url.netloc:
            return None
    path = parts.path or ""
    if not path.startswith("/") or path.startswith("//"):
        return None
    return path + (f"?{parts.query}" if parts.query else "")


# ------------------------------------------------------------------------------------------ view model


@dataclass
class Action:
    key: str
    label: str
    url: str
    icon: str
    variant: str = "secondary"  # primary | secondary | soft | ghost | danger-ghost
    kind: str = "post"  # post | modal | mark_done | link
    confirm: str | None = None
    confirm_title: str | None = None
    confirm_ok: str | None = None
    danger: bool = False
    vals: dict[str, str] | None = None


@dataclass
class TargetView:
    target: PostTarget
    network: str
    network_label: str
    account: Account | None
    account_name: str
    account_needs_reconnect: bool
    format_label: str
    status: str
    text: str
    has_text_override: bool
    media: list[MediaAsset]
    has_media_override: bool
    events: list[PostEvent]
    actions: list[Action]
    external_url: str | None
    external_label: str
    when_label: str | None
    note: str | None
    note_tone: str
    assisted: bool
    native_scheduled: bool
    error_detail: dict | None
    focus: bool = False
    options_summary: list[tuple[str, str]] = field(default_factory=list)


def _native_scheduled(target: PostTarget) -> bool:
    state = target.provider_state or {}
    if state.get("native_scheduled"):
        return True
    inner = state.get("state") if isinstance(state.get("state"), dict) else None
    return bool(inner and inner.get("native_scheduled"))


def _target_actions(target: PostTarget, sets: dict[str, frozenset], post_deleted: bool) -> list[Action]:
    if post_deleted:
        return []
    status = target.status
    tid = target.id
    actions: list[Action] = []
    if status == TargetStatus.MANUAL_PENDING or (not target.auto_publish and status in sets["retry"]):
        actions.append(
            Action(
                "assisted",
                "Publicar ahora (asistida)",
                f"/objetivos/{tid}/asistida",
                "hand",
                "primary",
                "modal",
            )
        )
    elif status == TargetStatus.SCHEDULED and not target.auto_publish:
        actions.append(
            Action("assisted", "Preparar publicación", f"/objetivos/{tid}/asistida", "hand", "soft", "modal")
        )
    if status in sets["retry"]:
        if status == TargetStatus.UNKNOWN:
            actions.append(
                Action(
                    "retry",
                    "Reintentar",
                    f"/objetivos/{tid}/reintentar",
                    "rotate-ccw",
                    "secondary",
                    confirm=(
                        "No se pudo confirmar si se publicó. Antes de reintentar, revisa en "
                        f"{network_label(target.network)} que la publicación NO aparece: si se publicó, "
                        "reintentar la duplicaría."
                    ),
                    confirm_title="¿Seguro que NO se publicó?",
                    confirm_ok="Sí, no se publicó: reintentar",
                    danger=True,
                    vals={"confirmado": "1"},
                )
            )
        elif status == TargetStatus.MISSED:
            actions.append(
                Action(
                    "retry",
                    "Publicar ahora",
                    f"/objetivos/{tid}/reintentar",
                    "send",
                    "primary" if target.auto_publish else "secondary",
                    confirm="Se enviará ahora a esta red.",
                    confirm_title="¿Publicar ahora?",
                    confirm_ok="Publicar ahora",
                )
            )
        else:
            actions.append(
                Action(
                    "retry",
                    "Reintentar",
                    f"/objetivos/{tid}/reintentar",
                    "rotate-ccw",
                    "primary" if target.auto_publish else "secondary",
                )
            )
    if status in sets["mark_done"] or status == TargetStatus.MANUAL_DONE:
        label = "Cambiar enlace" if status == TargetStatus.MANUAL_DONE else "Marcar como publicado"
        icon_name = "link" if status == TargetStatus.MANUAL_DONE else "circle-check"
        actions.append(
            Action(
                "mark_done", label, f"/objetivos/{tid}/marcar-publicado", icon_name, "secondary", "mark_done"
            )
        )
    can_cancel = status in sets["cancel"]
    if status == TargetStatus.PUBLISHING:
        can_cancel = target.next_attempt_at is not None and str(target.commit_phase) != "commit_sent"
    if status == TargetStatus.PUBLISHED:
        can_cancel = _native_scheduled(target)
    if can_cancel:
        label = "Descartar" if status == TargetStatus.MISSED else "Cancelar"
        actions.append(
            Action(
                "cancel",
                label,
                f"/objetivos/{tid}/cancelar",
                "ban",
                "danger-ghost",
                confirm=f"No se publicará en {network_label(target.network)}. Las demás redes no cambian.",
                confirm_title=f"¿Cancelar la publicación en {network_label(target.network)}?",
                confirm_ok="Cancelar en esta red",
                danger=True,
            )
        )
    return actions


def _when_label(target: PostTarget, tz: str) -> str | None:
    status = target.status
    if status in (TargetStatus.PUBLISHED, TargetStatus.MANUAL_DONE) and target.published_at:
        if status == TargetStatus.PUBLISHED and _native_scheduled(target) and target.scheduled_at:
            when = format_dt_es(target.scheduled_at, tz)
            return f"Programado en {network_label(target.network)} para el {when}"
        return f"Publicado el {format_dt_es(target.published_at, tz)}"
    if status == TargetStatus.SENT_TO_APP and target.published_at:
        return f"Enviado a la app el {format_dt_es(target.published_at, tz)}"
    if status == TargetStatus.PUBLISHING and target.next_attempt_at:
        return f"Siguiente intento: {format_dt_es(target.next_attempt_at, tz)}"
    if status == TargetStatus.SCHEDULED and target.next_attempt_at and target.attempts:
        return f"Reintento programado: {format_dt_es(target.next_attempt_at, tz)}"
    return None


_NOTES: dict[str, tuple[str, str]] = {
    "manual_pending": (
        "warning",
        "Ha llegado la hora: publícala tú con la Publicación asistida y márcala como publicada.",
    ),
    "sent_to_app": (
        "warning",
        "El vídeo está en la bandeja de la app de TikTok: termina la publicación allí.",
    ),
    "missed": ("danger", "No se publicó porque la aplicación estaba cerrada a la hora prevista."),
    "unknown": (
        "danger",
        "Se envió pero no se pudo confirmar el resultado. Comprueba en la red si aparece antes de "
        "reintentar.",
    ),
    "publishing": ("info", "Enviando a la red… puede tardar unos minutos (procesado del vídeo)."),
}


def _target_note(target: PostTarget) -> tuple[str | None, str]:
    status = str(target.status)
    if status == "scheduled" and not target.auto_publish:
        return (
            "Publicación asistida: a la hora programada te avisaremos para que la publiques tú.",
            "info",
        )
    if status == "published" and _native_scheduled(target):
        return (
            f"Programada en {network_label(target.network)}: se publicará aunque el PC esté apagado.",
            "info",
        )
    tone_note = _NOTES.get(status)
    if tone_note:
        return tone_note[1], tone_note[0]
    return None, "info"


def _options_summary(target: PostTarget) -> list[tuple[str, str]]:
    """Short (label, value) pairs of the per-network options shown on the target card."""
    o = dict(target.options or {})
    net = str(target.network)
    out: list[tuple[str, str]] = []
    if net == "tiktok":
        if o.get("mode") == "inbox":
            out.append(("Modo", "Borrador en la app"))
        if o.get("privacy_level"):
            out.append(
                ("Visibilidad", TT_PRIVACY_LABELS.get(str(o["privacy_level"]), str(o["privacy_level"])))
            )
    elif net == "youtube":
        if o.get("title"):
            out.append(("Título", str(o["title"])))
        if o.get("privacy_status"):
            out.append(
                ("Visibilidad", YT_PRIVACY_LABELS.get(str(o["privacy_status"]), str(o["privacy_status"])))
            )
    elif net in ("facebook", "instagram"):
        if o.get("title"):
            out.append(("Título", str(o["title"])))
        if o.get("first_comment"):
            out.append(("Primer comentario", _snippet(str(o["first_comment"]), 60)))
        collaborators = o.get("collaborators") or []
        if collaborators:
            out.append(("Colaboradores", ", ".join(f"@{c.lstrip('@')}" for c in collaborators)))
        if o.get("link"):
            out.append(("Enlace", str(o["link"])))
    return out


def build_target_view(
    db: Session, target: PostTarget, tz: str, sets: dict[str, frozenset], *, post_deleted: bool, focus: bool
) -> TargetView:
    account = target.account
    network = str(target.network)
    external_label = f"Ver en {network_label(network)}" if network != "demo" else "Ver publicación"
    note, note_tone = _target_note(target)
    detail = target.last_error_detail if isinstance(target.last_error_detail, dict) else None
    events = sorted(target.events, key=lambda e: (e.ts, e.id or 0), reverse=True)
    return TargetView(
        target=target,
        network=network,
        network_label=network_label(network),
        account=account,
        account_name=account.display_name if account is not None else "Cuenta eliminada",
        account_needs_reconnect=bool(account is not None and account.status != AccountStatus.CONNECTED),
        format_label=format_label(network, target.format),
        status=str(target.status),
        text=target_text(target),
        has_text_override=target.text_override is not None,
        media=target_media(db, target) if target.media_override is not None else [],
        has_media_override=target.media_override is not None,
        events=events,
        actions=_target_actions(target, sets, post_deleted),
        external_url=target.external_url,
        external_label=external_label,
        when_label=_when_label(target, tz),
        note=note,
        note_tone=note_tone,
        assisted=not target.auto_publish,
        native_scheduled=_native_scheduled(target),
        error_detail=detail or None,
        focus=focus,
        options_summary=_options_summary(target),
    )


def load_post(db: Session, post_id: int) -> Post:
    stmt = (
        select(Post)
        .where(Post.id == post_id)
        .options(
            selectinload(Post.brand),
            selectinload(Post.media_links),
            selectinload(Post.targets).selectinload(PostTarget.account),
            selectinload(Post.targets).selectinload(PostTarget.events),
        )
        .execution_options(populate_existing=True)
    )
    post = db.scalars(stmt).first()
    if post is None:
        raise HTTPException(404, "La publicación no existe o se ha eliminado definitivamente.")
    return post


def detail_context(
    db: Session, post: Post, clock: Clock, *, mode: str, focus_target: int | None = None
) -> dict:
    tz = _post_tz(post)
    sets = _status_sets()
    deleted = post.deleted_at is not None
    targets = [
        build_target_view(db, t, tz, sets, post_deleted=deleted, focus=t.id == focus_target)
        for t in sorted(post.targets, key=lambda t: t.id)
    ]
    statuses = [t.status for t in post.targets]
    can_publish_now = not deleted and any(s in sets["publish_now"] for s in statuses)
    now = clock.now()
    # The scheduler changes statuses in the background: keep the open detail fresh while something is being
    # sent (or is about to be), e.g. right after "Publicar ahora".
    in_flight = not deleted and any(
        t.status == TargetStatus.PUBLISHING
        or (
            t.status == TargetStatus.SCHEDULED
            and t.scheduled_at is not None
            and ensure_utc(t.scheduled_at) <= now + IN_FLIGHT_LEAD
        )
        for t in post.targets
    )
    lock = edit_lock_reason(post)
    errors = [t for t in targets if t.status in ("error", "unknown", "missed")]
    title = _snippet(post.text, 60) or ("Idea sin texto" if post.is_idea else "Publicación sin texto")
    return {
        "post": post,
        "post_title": title,
        "targets": targets,
        "media": [link.media for link in post.media_links if link.media is not None],
        "derived_status": str(post.display_status),
        "tz": tz,
        "mode": mode if mode in _VALID_MODES else "drawer",
        "can_publish_now": can_publish_now,
        "edit_lock": lock,
        "deleted": deleted,
        "error_count": len(errors),
        "focus_target": focus_target,
        "now": now,
        "in_flight": in_flight,
    }


def _render_detail(
    request: Request,
    db: Session,
    post: Post,
    clock: Clock,
    *,
    parcial: bool,
    modo: str,
    focus_target: int | None,
) -> Response:
    from planificador.templating import render

    headers = {"Cache-Control": "no-store"}
    if parcial:
        ctx = detail_context(db, post, clock, mode=modo or "drawer", focus_target=focus_target)
        return render(request, "posts/_detail.html", headers=headers, **ctx)
    if _is_htmx(request):
        ctx = detail_context(db, post, clock, mode="drawer", focus_target=focus_target)
        return render(request, "posts/drawer.html", headers=headers, **ctx)
    ctx = detail_context(db, post, clock, mode="pagina", focus_target=focus_target)
    return render(request, "posts/page.html", page_title="Publicación", headers=headers, **ctx)


# ------------------------------------------------------------------------------------------ detail pages


@router.get("/publicaciones/{post_id:int}")
def post_detail(
    request: Request,
    post_id: int,
    db: DbSession,
    clock: ClockDep,
    parcial: int = 0,
    modo: str = "",
    objetivo: int | None = None,
) -> Response:
    post = load_post(db, post_id)
    return _render_detail(request, db, post, clock, parcial=bool(parcial), modo=modo, focus_target=objetivo)


@router.get("/objetivos/{target_id:int}")
def target_detail(request: Request, target_id: int, db: DbSession, clock: ClockDep) -> Response:
    target = db.get(PostTarget, target_id)
    if target is None:
        raise HTTPException(404, "Esta publicación ya no existe.")
    post = load_post(db, target.post_id)
    return _render_detail(request, db, post, clock, parcial=False, modo="", focus_target=target_id)


# ------------------------------------------------------------------------------------------ assisted


def open_network_link(target: PostTarget) -> tuple[str | None, str | None]:
    """(url, label) for "Abrir red" in the assisted flow (desktop web)."""
    network = str(target.network)
    account = target.account
    external = (account.external_id if account is not None else "") or ""
    if network == "youtube":
        if external.startswith("UC"):
            return (
                f"https://studio.youtube.com/channel/{quote(external)}/videos/upload?d=ud",
                NETWORK_OPEN_LABELS[network],
            )
        return "https://studio.youtube.com/", NETWORK_OPEN_LABELS[network]
    if network == "tiktok":
        return "https://www.tiktok.com/tiktokstudio/upload", NETWORK_OPEN_LABELS[network]
    if network == "instagram":
        return "https://www.instagram.com/", NETWORK_OPEN_LABELS[network]
    if network == "facebook":
        if external.isdigit():
            return f"https://www.facebook.com/{external}", NETWORK_OPEN_LABELS[network]
        return "https://www.facebook.com/", NETWORK_OPEN_LABELS[network]
    return None, None


@dataclass
class CheckItem:
    text: str
    copy: str | None = None
    download_url: str | None = None


def _yes_no(value: Any) -> str:
    return "Sí" if value else "No"


def assisted_checklist(target: PostTarget, media_count: int) -> list[CheckItem]:
    """What the user must do by hand, in order, from the target's format and options."""
    net = str(target.network)
    o = dict(target.options or {})
    fmt = str(target.format or "")
    items: list[CheckItem] = []
    if net in ("facebook", "instagram"):
        kind = {"reel": "un Reel", "story": "una historia", "post": "una publicación"}.get(
            fmt, "una publicación"
        )
        items.append(CheckItem(f"Crea {kind} en {network_label(net)}."))
    elif net == "tiktok":
        items.append(CheckItem("Sube el vídeo en TikTok Studio."))
    elif net == "youtube":
        items.append(
            CheckItem(
                "Sube el vídeo en YouTube Studio"
                + (" (vertical: se mostrará como Short)." if fmt == "short" else ".")
            )
        )
    if media_count:
        items.append(
            CheckItem(
                "Añade los medios en el mismo orden que aquí." if media_count > 1 else "Añade el archivo."
            )
        )
    items.append(
        CheckItem(
            "Pega el texto de la publicación." if net != "youtube" else "Pega el título y la descripción."
        )
    )

    if net == "instagram":
        if o.get("collaborators"):
            people = ", ".join(f"@{str(c).lstrip('@')}" for c in o["collaborators"])
            items.append(CheckItem(f"Invita como colaboradores a {people}.", copy=people))
        if fmt == "reel" and o.get("share_to_feed") is False:
            items.append(CheckItem("Desactiva «Compartir también en el feed»."))
        if o.get("alt_text"):
            items.append(CheckItem("Añade el texto alternativo de la imagen.", copy=str(o["alt_text"])))
        if o.get("location_id"):
            items.append(CheckItem("Añade la ubicación."))
        if o.get("trial_reel"):
            items.append(CheckItem("Publícalo como reel de prueba."))
        if o.get("audio_name"):
            items.append(CheckItem(f"Elige el audio «{o['audio_name']}».", copy=str(o["audio_name"])))
    elif net == "facebook":
        if o.get("title"):
            items.append(CheckItem("Escribe el título del vídeo.", copy=str(o["title"])))
        if o.get("link"):
            items.append(CheckItem("Añade el enlace.", copy=str(o["link"])))
    elif net == "tiktok":
        if o.get("privacy_level"):
            label = TT_PRIVACY_LABELS.get(str(o["privacy_level"]), str(o["privacy_level"]))
            items.append(CheckItem(f"«¿Quién puede ver este vídeo?»: {label}."))
        items.append(
            CheckItem(
                "Permitir comentarios: {c} · Dúo: {d} · Stitch: {s}.".format(
                    c=_yes_no(not o.get("disable_comment")),
                    d=_yes_no(not o.get("disable_duet")),
                    s=_yes_no(not o.get("disable_stitch")),
                )
            )
        )
        if o.get("is_aigc"):
            items.append(CheckItem("Activa «Contenido generado por IA»."))
        if o.get("brand_organic_toggle") or o.get("brand_content_toggle"):
            kinds = [
                label
                for key, label in (
                    ("brand_organic_toggle", "Tu marca"),
                    ("brand_content_toggle", "Contenido de marca"),
                )
                if o.get(key)
            ]
            items.append(CheckItem(f"Activa «Divulgación de contenido comercial»: {' y '.join(kinds)}."))
    elif net == "youtube":
        if o.get("privacy_status"):
            label = YT_PRIVACY_LABELS.get(str(o["privacy_status"]), str(o["privacy_status"]))
            items.append(CheckItem(f"Visibilidad: {label}."))
        if o.get("made_for_kids") is not None:
            kids = (
                "Sí, es contenido creado para niños"
                if o["made_for_kids"]
                else "No, no es contenido creado para niños"
            )
            items.append(CheckItem(f"Audiencia: «{kids}»."))
        items.append(CheckItem(f"Contenido alterado o sintético: {_yes_no(o.get('synthetic_media'))}."))
        category = str(o.get("category_id") or "")
        if category:
            items.append(CheckItem(f"Categoría: {YT_CATEGORIES.get(category, category)}."))
        if o.get("paid_promotion"):
            items.append(CheckItem("Marca «Incluye promoción pagada»."))
        if o.get("notify_subscribers") is False:
            items.append(CheckItem("Desactiva «Notificar a los suscriptores»."))
        if o.get("thumbnail_media_id"):
            items.append(
                CheckItem(
                    "Sube la miniatura personalizada.",
                    download_url=f"/medios/{int(o['thumbnail_media_id'])}/archivo?descargar=1",
                )
            )
        if o.get("playlist_id"):
            items.append(CheckItem("Añádelo a la lista de reproducción elegida."))
        if o.get("license") == "creativeCommon":
            items.append(CheckItem("Licencia: Creative Commons - Atribución."))
        if o.get("embeddable") is False:
            items.append(CheckItem("Desactiva «Permitir insertar»."))
    if o.get("first_comment") and net in ("instagram", "facebook", "youtube"):
        items.append(CheckItem("Publica el primer comentario.", copy=str(o["first_comment"])))
    items.append(CheckItem("Publica y vuelve aquí para marcarla como publicada."))
    return items


def _youtube_fields(target: PostTarget, text: str) -> dict[str, str] | None:
    if str(target.network) != "youtube":
        return None
    o = dict(target.options or {})
    title = str(o.get("title") or "").strip()
    if not title:
        title = next((line.strip() for line in text.splitlines() if line.strip()), "")[:100]
    tags = o.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return {"title": title, "description": text, "tags": ", ".join(str(t) for t in tags)}


@router.get("/objetivos/{target_id:int}/asistida")
def assisted_publish(request: Request, target_id: int, db: DbSession, clock: ClockDep) -> Response:
    """Opened by the bell / notification centre / drawer with hx-target="#modal-root" (SPEC §11)."""
    from planificador.templating import render

    target = db.get(PostTarget, target_id, populate_existing=True)
    if target is None or target.post is None:
        raise HTTPException(404, "Esta publicación ya no existe.")
    post = target.post
    tz = _post_tz(post)
    text = target_text(target)
    assets = target_media(db, target)
    media_rows = [{"asset": a, "available": _asset_path(a) is not None} for a in assets]
    open_url, open_label = open_network_link(target)
    sets = _status_sets()
    status = str(target.status)
    ctx = {
        "target": target,
        "post": post,
        "account": target.account,
        "network": str(target.network),
        "network_name": network_label(target.network),
        "format_label": format_label(target.network, target.format),
        "text": text,
        "youtube": _youtube_fields(target, text),
        "media_rows": media_rows,
        "zip_ready": sum(1 for r in media_rows if r["available"]) > 1,
        "open_url": open_url,
        "open_label": open_label,
        "checklist": assisted_checklist(target, len(assets)),
        "status": status,
        "done": status in ("manual_done", "published"),
        "can_mark_done": status in {str(s) for s in sets["mark_done"]} and post.deleted_at is None,
        "deleted": post.deleted_at is not None,
        "tz": tz,
        "now": clock.now(),
    }
    headers = {"Cache-Control": "no-store"}
    if _is_htmx(request):
        return render(request, "posts/assisted_modal.html", headers=headers, **ctx)
    return render(
        request, "posts/assisted_page.html", page_title="Publicación asistida", headers=headers, **ctx
    )


# ------------------------------------------------------------------------------------------ media ZIP

_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


def _archive_name(index: int, asset: MediaAsset, path: Path) -> str:
    name = unicodedata.normalize("NFC", Path(asset.original_name or "").name)
    name = _UNSAFE_NAME.sub("_", name).strip(" .") or f"medio-{asset.id}"
    if not Path(name).suffix and path.suffix:
        name += path.suffix.lower()
    stem, suffix = Path(name).stem[:80], Path(name).suffix[:10]
    return f"{index:02d}-{stem}{suffix}"


class _ChunkSink(io.RawIOBase):
    """Write-only, non-seekable sink collecting what ZipFile writes so it can be streamed."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:  # type: ignore[override]
        chunk = bytes(data)
        self._chunks.append(chunk)
        return len(chunk)

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def zip_stream(files: Iterable[tuple[str, Path]]) -> Iterator[bytes]:
    """Stream a ZIP (stored entries, ZIP64 when needed) without holding whole files in memory."""
    sink = _ChunkSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for arcname, path in files:
            info = zipfile.ZipInfo.from_file(path, arcname)
            info.compress_type = zipfile.ZIP_STORED
            with path.open("rb") as src, archive.open(info, "w") as dst:
                while chunk := src.read(ZIP_CHUNK):
                    dst.write(chunk)
                    data = sink.drain()
                    if data:
                        yield data
            data = sink.drain()
            if data:
                yield data
    data = sink.drain()
    if data:
        yield data


@router.get("/objetivos/{target_id:int}/medios.zip")
def target_media_zip(target_id: int, db: DbSession) -> Response:
    target = db.get(PostTarget, target_id)
    if target is None or target.post is None:
        raise HTTPException(404, "Esta publicación ya no existe.")
    files: list[tuple[str, Path]] = []
    for index, asset in enumerate(target_media(db, target), start=1):
        path = _asset_path(asset)
        if path is not None:
            files.append((_archive_name(index, asset, path), path))
    if not files:
        raise HTTPException(404, "Esta publicación no tiene medios para descargar.")
    filename = f"medios-{target.network}-{target.post_id}.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(zip_stream(files), media_type="application/zip", headers=headers)


# ------------------------------------------------------------------------------------------ actions


def _events(**extra: Any) -> dict[str, Any]:
    events: dict[str, Any] = {"posts:changed": True, "notifications:refresh": True}
    events.update(extra)
    return events


def _ok(
    request: Request,
    message: str,
    *,
    fallback: str,
    next_path: str | None = None,
    events: dict[str, Any] | None = None,
    kind: str = "success",
    redirect_htmx: str | None = None,
) -> Response:
    from planificador.templating import flash, toast_headers

    if _wants_json(request):
        return JSONResponse({"ok": True, "message": message})
    if _is_htmx(request):
        headers = toast_headers(message, kind, **(events if events is not None else _events()))
        if redirect_htmx:
            headers["HX-Redirect"] = redirect_htmx
        return Response(status_code=200, headers=headers)
    flash(request, message, kind)
    return RedirectResponse(_same_origin_path(request, next_path) or fallback, status_code=303)


def _fail(
    request: Request, message: str, *, fallback: str, next_path: str | None, status_code: int = 409
) -> Response:
    from planificador.templating import flash

    if _wants_json(request):
        return JSONResponse({"ok": False, "detail": message}, status_code=status_code)
    if _is_htmx(request):
        return Response(
            message,
            status_code=status_code,
            media_type="text/plain; charset=utf-8",
            headers={"X-Error-Message": quote(message)},
        )
    flash(request, message, "error")
    return RedirectResponse(_same_origin_path(request, next_path) or fallback, status_code=303)


def _run(fn: Any) -> str | None:
    """Run an engine/postsvc call; return the Spanish error message of a user error, else ``None``."""
    errors = _user_errors()
    try:
        fn()
    except errors as exc:  # type: ignore[misc]
        return str(getattr(exc, "message", None) or exc)
    return None


def _target_or_404(db: Session, target_id: int) -> PostTarget:
    target = db.get(PostTarget, target_id)
    if target is None:
        raise HTTPException(404, "Esta publicación ya no existe.")
    return target


def _feature_missing(request: Request, *, fallback: str, next_path: str | None) -> Response:
    return _fail(
        request,
        "Esta acción todavía no está disponible en esta versión.",
        fallback=fallback,
        next_path=next_path,
        status_code=503,
    )


Next = Annotated[str | None, Form(alias="next")]


@router.post("/objetivos/{target_id:int}/reintentar")
def retry_target(
    request: Request,
    target_id: int,
    db: DbSession,
    clock: ClockDep,
    confirmado: Annotated[str | None, Form()] = None,
    next_path: Next = None,
) -> Response:
    target = _target_or_404(db, target_id)
    post_id, network, previous = target.post_id, network_label(target.network), str(target.status)
    fallback = f"/publicaciones/{post_id}"
    confirmed = (confirmado or "").lower() in ("1", "true", "si", "sí", "on")
    try:
        engine = _engine()
    except ImportError:
        return _feature_missing(request, fallback=fallback, next_path=next_path)
    error = _run(lambda: engine.retry_target(db, target_id, clock, confirmed_not_published=confirmed))
    if error:
        return _fail(request, error, fallback=fallback, next_path=next_path)
    message = (
        f"Publicando ahora en {network}."
        if previous == "missed"
        else f"Reintentando la publicación en {network}."
    )
    return _ok(request, message, fallback=fallback, next_path=next_path)


@router.post("/objetivos/{target_id:int}/cancelar")
def cancel_target(
    request: Request, target_id: int, db: DbSession, clock: ClockDep, next_path: Next = None
) -> Response:
    target = _target_or_404(db, target_id)
    post_id, network = target.post_id, network_label(target.network)
    fallback = f"/publicaciones/{post_id}"
    try:
        engine = _engine()
    except ImportError:
        return _feature_missing(request, fallback=fallback, next_path=next_path)
    error = _run(lambda: engine.cancel_target(db, target_id, clock))
    if error:
        return _fail(request, error, fallback=fallback, next_path=next_path)
    return _ok(request, f"Publicación cancelada en {network}.", fallback=fallback, next_path=next_path)


@router.post("/objetivos/{target_id:int}/marcar-publicado")
def mark_published(
    request: Request,
    target_id: int,
    db: DbSession,
    clock: ClockDep,
    url: Annotated[str | None, Form()] = None,
    origen: Annotated[str | None, Form()] = None,
    next_path: Next = None,
) -> Response:
    target = _target_or_404(db, target_id)
    post_id, network = target.post_id, network_label(target.network)
    fallback = f"/publicaciones/{post_id}"
    link = (url or "").strip() or None
    if link and not re.match(r"^https?://\S+$", link, re.IGNORECASE):
        return _fail(
            request,
            "El enlace debe ser una dirección web completa (https://…).",
            fallback=fallback,
            next_path=next_path,
            status_code=422,
        )
    try:
        engine = _engine()
    except ImportError:
        return _feature_missing(request, fallback=fallback, next_path=next_path)
    error = _run(lambda: engine.mark_done_manually(db, target_id, clock, external_url=link))
    if error:
        return _fail(request, error, fallback=fallback, next_path=next_path)
    extra = {"closeModal": True} if origen == "asistida" else {}
    return _ok(
        request,
        f"Marcada como publicada en {network}.",
        fallback=fallback,
        next_path=next_path,
        events=_events(**extra),
    )


@router.post("/publicaciones/{post_id:int}/publicar-ahora")
def publish_now(
    request: Request, post_id: int, db: DbSession, clock: ClockDep, next_path: Next = None
) -> Response:
    load_post(db, post_id)
    fallback = f"/publicaciones/{post_id}"
    try:
        engine = _engine()
    except ImportError:
        return _feature_missing(request, fallback=fallback, next_path=next_path)
    error = _run(lambda: engine.publish_now(db, post_id, clock))
    if error:
        return _fail(request, error, fallback=fallback, next_path=next_path)
    return _ok(request, "Publicando ahora en las redes pendientes.", fallback=fallback, next_path=next_path)


@router.post("/publicaciones/{post_id:int}/duplicar")
def duplicate_post(
    request: Request,
    post_id: int,
    db: DbSession,
    clock: ClockDep,
    modo: Annotated[str | None, Form()] = None,
    next_path: Next = None,
) -> Response:
    load_post(db, post_id)
    fallback = f"/publicaciones/{post_id}"
    try:
        postsvc = _postsvc()
    except ImportError:
        return _feature_missing(request, fallback=fallback, next_path=next_path)
    created: list[Post] = []
    error = _run(lambda: created.append(postsvc.duplicate_post(db, post_id, clock)))
    if error:
        return _fail(request, error, fallback=fallback, next_path=next_path)
    new_id = created[0].id
    message = "Copia creada como borrador."
    if _is_htmx(request) and modo == "pagina":
        return _ok(request, message, fallback=fallback, redirect_htmx=f"/publicaciones/{new_id}")
    events = _events(**{"post:open": {"id": new_id}})
    return _ok(request, message, fallback=f"/publicaciones/{new_id}", next_path=None, events=events)


@router.post("/publicaciones/{post_id:int}/papelera")
def trash_post(
    request: Request,
    post_id: int,
    db: DbSession,
    clock: ClockDep,
    modo: Annotated[str | None, Form()] = None,
    next_path: Next = None,
) -> Response:
    load_post(db, post_id)
    fallback = f"/publicaciones/{post_id}"
    try:
        postsvc = _postsvc()
    except ImportError:
        return _feature_missing(request, fallback=fallback, next_path=next_path)
    error = _run(lambda: postsvc.delete_post(db, post_id, clock))
    if error:
        return _fail(request, error, fallback=fallback, next_path=next_path)
    message = "Publicación movida a la papelera. Puedes restaurarla durante 45 días."
    extra = {} if modo == "pagina" else {"closeDrawer": True}
    return _ok(request, message, fallback=fallback, next_path=next_path, events=_events(**extra))


@router.post("/publicaciones/{post_id:int}/restaurar")
def restore_post(
    request: Request, post_id: int, db: DbSession, clock: ClockDep, next_path: Next = None
) -> Response:
    load_post(db, post_id)
    fallback = f"/publicaciones/{post_id}"
    try:
        postsvc = _postsvc()
    except ImportError:
        return _feature_missing(request, fallback=fallback, next_path=next_path)
    error = _run(lambda: postsvc.restore_post(db, post_id, clock))
    if error:
        return _fail(request, error, fallback=fallback, next_path=next_path)
    return _ok(request, "Publicación restaurada.", fallback=fallback, next_path=next_path)

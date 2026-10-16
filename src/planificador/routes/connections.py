"""Connected accounts ("Cuentas conectadas") and the OAuth orchestration (SPEC §6).

One card per network for the **active brand** (Facebook, Instagram, TikTok, YouTube, plus Demo in demo mode)
with its status, account, token expiry, last error and actions.

Routes:

- ``GET  /conexiones``                               the cards
- ``GET  /conexiones/tarjeta/{network}``             one card (htmx refresh; polled while a lock test runs)
- ``GET  /conexiones/meta/iniciar[?red=instagram]``  Facebook Login (Pages + Instagram via Facebook Login)
- ``GET  /conexiones/callback/meta``                 callback → code exchange → pending chooser
- ``GET  /conexiones/meta/elegir?pendiente=…``       page chooser (pages, linked Instagram, warnings)
- ``POST /conexiones/meta/seleccionar``              creates/updates the facebook / instagram accounts
- ``GET  /conexiones/tiktok/iniciar``                TikTok Login Kit with PKCE (verifier in ``oauth_states``)
- ``GET  /conexiones/callback/social1``              callback (the path must not contain "tiktok")
- ``GET  /conexiones/youtube/iniciar``               Google OAuth with PKCE
- ``GET  /conexiones/callback/google``               callback → channels (chooser when > 1)
- ``GET  /conexiones/youtube/elegir?pendiente=…`` / ``POST /conexiones/youtube/seleccionar``
- ``POST /conexiones/demo/conectar``                 one-click Demo account (demo mode only)
- ``POST /conexiones/{id}/desconectar``              wipe tokens (+ best-effort revoke for TikTok/YouTube)
- ``POST /conexiones/{id}/comprobar``                ``provider.check_account``
- ``POST /conexiones/{id}/youtube/prueba-bloqueo``   "Prueba de bloqueo" in a background thread
- ``POST /conexiones/{id}/youtube/modo``             assisted / API publishing (+ "proyecto auditado")
- ``GET  /conexiones/avatar/{id}``                   cached account avatar (``<data>/media/avatars``)
- ``GET  /ayuda/{name}``                             setup guides (``docs/guia-<name>.md``, safe Markdown)

OAuth state: a row in ``oauth_states`` (10 min TTL, single use, bound to the brand) **and** the same value in
the session, so a callback only completes in the browser that started it. Tokens obtained in a callback that
still needs a choice (Meta page, YouTube channel) wait in an in-memory store (15 min) — never in the URL.
"""

from __future__ import annotations

import inspect
import io
import logging
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from markupsafe import Markup, escape
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from planificador import __version__, config, crypto
from planificador.deps import ActiveBrand, ClockDep, DbSession
from planificador.models import (
    Account,
    AccountStatus,
    AnalyticsSnapshot,
    Brand,
    InboxComment,
    Network,
    OAuthState,
    PostMetric,
    PostTarget,
    TargetStatus,
)
from planificador.providers.base import AccountCreds, ProviderError, ProviderSettings
from planificador.timeutil import Clock

log = logging.getLogger(__name__)

router = APIRouter()

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
STATE_TTL = timedelta(seconds=OAuthState.OAUTH_STATE_TTL_SECONDS)
PENDING_TTL = timedelta(minutes=15)
SESSION_FLOWS_KEY = "oauth_flows"
SESSION_PENDING_KEY = "oauth_pending"
MAX_SESSION_FLOWS = 6
AVATAR_MAX_BYTES = 3 * 1024 * 1024
AVATAR_SIZE = 256

REAL_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube")
#: Pending publications that a disconnection would block.
PENDING_STATUSES = (TargetStatus.SCHEDULED, TargetStatus.PUBLISHING)

STATUS_LABELS = {
    "connected": "Conectada",
    "needs_reconnect": "Reconexión necesaria",
    "none": "Sin conectar",
}
STATUS_TONES = {"connected": "success", "needs_reconnect": "warning", "none": "muted"}

NETWORK_INFO: dict[str, dict[str, str]] = {
    "facebook": {
        "label": "Facebook",
        "what": "Publica en una Página de Facebook: publicaciones, reels e historias.",
        "credentials": "meta",
        "guide": "meta",
    },
    "instagram": {
        "label": "Instagram",
        "what": "Cuenta profesional (empresa o creador) vinculada a una Página de Facebook: publicaciones, "
        "carruseles, reels e historias.",
        "credentials": "meta",
        "guide": "meta",
    },
    "tiktok": {
        "label": "TikTok",
        "what": "Publica vídeos directamente o envíalos como borrador a la app de TikTok.",
        "credentials": "tiktok",
        "guide": "tiktok",
    },
    "youtube": {
        "label": "YouTube",
        "what": "Sube vídeos y Shorts a tu canal o prepáralos para publicarlos tú (publicación asistida).",
        "credentials": "google",
        "guide": "youtube",
    },
    "demo": {
        "label": "Demo",
        "what": "Red de prueba sin conexión real: sirve para probar la app sin publicar nada de verdad.",
        "credentials": "",
        "guide": "",
    },
}

CREDENTIAL_GROUPS: dict[str, dict[str, str]] = {
    "meta": {
        "anchor": "/ajustes#credenciales-meta",
        "missing": "Falta configurar tu app de Meta (App ID y App Secret) en Ajustes.",
    },
    "tiktok": {
        "anchor": "/ajustes#credenciales-tiktok",
        "missing": "Falta configurar tu app de TikTok (Client Key y Client Secret) en Ajustes.",
    },
    "google": {
        "anchor": "/ajustes#credenciales-google",
        "missing": "Falta configurar tu cliente de Google (ID de cliente y secreto) en Ajustes.",
    },
}

ACCOUNT_TYPE_LABELS: dict[str, dict[str, str]] = {
    "facebook": {"": "Página de Facebook"},
    "instagram": {
        "business": "Cuenta de empresa de Instagram",
        "creator": "Cuenta de creador de Instagram",
        "": "Cuenta profesional de Instagram",
    },
    "tiktok": {"business": "Cuenta de empresa de TikTok", "": "Cuenta de TikTok"},
    "youtube": {"": "Canal de YouTube"},
    "demo": {"": "Cuenta de prueba"},
}

IG_TYPES = {"business": "business", "creator": "creator"}

MSG_STATE_INVALID = (
    "La solicitud de conexión no es válida o ya se usó. Vuelve a intentarlo desde Cuentas conectadas."
)
MSG_STATE_EXPIRED = "La solicitud de conexión ha caducado (más de 10 minutos). Vuelve a intentarlo."
MSG_PENDING_GONE = "La selección ha caducado. Vuelve a conectar la cuenta."


class ConnectError(Exception):
    """A user-facing (Spanish) problem in a connection flow."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


# ------------------------------------------------------------------------------------------- helpers


def _http_client() -> httpx.Client:
    """HTTP client for OAuth exchanges (tests intercept it with respx)."""
    return httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": f"Planificador/{__version__}"})


def _start_background(fn: Callable[[], None], name: str) -> None:
    """Run ``fn`` in a daemon thread (tests replace this to run it inline)."""
    threading.Thread(target=fn, name=name, daemon=True).start()


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call(fn: Callable[..., Any], *args: Any, **optional: Any) -> Any:
    """Call a provider function passing only the optional keyword arguments it supports (the SPEC contract
    signatures are minimal; implementations may add ``clock=``, ``scopes=``…)."""
    kwargs = {k: v for k, v in optional.items() if v is not None and _accepts(fn, k)}
    return fn(*args, **kwargs)


def _flash(request: Request, message: str, kind: str = "info") -> None:
    from planificador.templating import flash

    flash(request, message, kind)


def _render(request: Request, name: str, **ctx: Any) -> Response:
    from planificador.templating import render

    return render(request, name, **ctx)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _redirect(request: Request, url: str) -> Response:
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _provider_settings(request: Request, db: Session) -> ProviderSettings:
    return config.provider_settings(db, base_url=request.app.state.settings.base_url)


def _paths(request: Request) -> Any:
    paths = getattr(request.app.state, "paths", None)
    return paths if paths is not None else request.app.state.settings.paths


def _network_label(network: str) -> str:
    return NETWORK_INFO.get(network, {}).get("label", network.capitalize())


def credentials_ready(ps: ProviderSettings, group: str) -> bool:
    if group == "meta":
        return bool(ps.meta_app_id and ps.meta_app_secret)
    if group == "tiktok":
        return bool(ps.tiktok_client_key and ps.tiktok_client_secret)
    if group == "google":
        return bool(ps.google_client_id and ps.google_client_secret)
    return True


def redirect_uris(ps: ProviderSettings) -> dict[str, str]:
    """Exact redirect URIs to register in each developer console."""
    from planificador.providers import meta_common, tiktok, youtube

    return {
        "meta": meta_common.redirect_uri(ps),
        "tiktok": tiktok.redirect_uri(ps),
        "google": youtube.redirect_uri(ps),
    }


def account_type_label(account: Account) -> str:
    labels = ACCOUNT_TYPE_LABELS.get(str(account.network), {})
    return labels.get(str(account.account_type or ""), labels.get("", ""))


def avatar_url(account: Account | None) -> str | None:
    if account is None or not account.avatar_path:
        return None
    stamp = int(account.connected_at.timestamp()) if account.connected_at else 0
    return f"/conexiones/avatar/{account.id}?v={stamp}"


def _brand_account(db: Session, brand_id: int, network: str) -> Account | None:
    return db.scalar(select(Account).where(Account.brand_id == brand_id, Account.network == network))


def _pending_counts(db: Session, account_ids: list[int]) -> dict[int, int]:
    if not account_ids:
        return {}
    rows = db.execute(
        select(PostTarget.account_id, func.count())
        .where(PostTarget.account_id.in_(account_ids), PostTarget.status.in_(list(PENDING_STATUSES)))
        .group_by(PostTarget.account_id)
    ).all()
    return {int(account_id): int(count) for account_id, count in rows}


# ------------------------------------------------------------------------------------------ OAuth state


def _session_flows(request: Request) -> dict[str, dict[str, Any]]:
    flows = request.session.get(SESSION_FLOWS_KEY)
    return dict(flows) if isinstance(flows, dict) else {}


def new_oauth_state(
    db: Session,
    request: Request,
    network: str,
    brand_id: int,
    now: datetime,
    *,
    code_verifier: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Create the ``oauth_states`` row and remember the state in the session (single use, 10 min)."""
    db.execute(delete(OAuthState).where(OAuthState.created_at < now - STATE_TTL))
    state = secrets.token_urlsafe(32)
    db.add(
        OAuthState(
            state=state, network=network, brand_id=brand_id, code_verifier=code_verifier, created_at=now
        )
    )
    db.commit()
    flows = _session_flows(request)
    flows[state] = {"network": network, **(extra or {})}
    if len(flows) > MAX_SESSION_FLOWS:
        flows = dict(list(flows.items())[-MAX_SESSION_FLOWS:])
    request.session[SESSION_FLOWS_KEY] = flows
    return state


@dataclass
class ConsumedState:
    network: str
    brand_id: int
    code_verifier: str | None
    extra: dict[str, Any] = field(default_factory=dict)


def consume_oauth_state(
    db: Session, request: Request, network: str, state: str, now: datetime
) -> ConsumedState:
    """Validate and delete the state (raises :class:`ConnectError`). Checks: the row exists for this
    network, the same browser session started the flow, it has not expired and the brand is still active."""
    flows = _session_flows(request)
    flow = flows.pop(state, None) if state else None
    if state:
        request.session[SESSION_FLOWS_KEY] = flows
    row = db.get(OAuthState, state) if state else None
    if row is None or row.network != network or flow is None or flow.get("network") != network:
        raise ConnectError(MSG_STATE_INVALID)
    consumed = ConsumedState(
        network=row.network,
        brand_id=int(row.brand_id or 0),
        code_verifier=row.code_verifier,
        extra={k: v for k, v in flow.items() if k != "network"},
    )
    expired = row.is_expired(now)
    db.delete(row)
    db.commit()
    if expired:
        raise ConnectError(MSG_STATE_EXPIRED)
    brand = db.get(Brand, consumed.brand_id) if consumed.brand_id else None
    if brand is None or brand.archived_at is not None:
        raise ConnectError("La marca para la que iniciaste la conexión ya no existe o está archivada.")
    return consumed


# ------------------------------------------------------------------------------------- pending choices


class PendingStore:
    """Short-lived, in-memory storage for OAuth results that still need a choice (tokens never leave the
    process; the browser only gets an opaque nonce, also remembered in its session)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[datetime, str, dict[str, Any]]] = {}

    def put(self, kind: str, data: dict[str, Any], now: datetime) -> str:
        nonce = secrets.token_urlsafe(24)
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if now - v[0] <= PENDING_TTL}
            self._items[nonce] = (now, kind, data)
        return nonce

    def get(self, nonce: str, kind: str, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(nonce or "")
        if item is None or item[1] != kind or now - item[0] > PENDING_TTL:
            return None
        return item[2]

    def pop(self, nonce: str) -> None:
        with self._lock:
            self._items.pop(nonce or "", None)


_store_lock = threading.Lock()


def pending_store(request_or_app: Any) -> PendingStore:
    app = getattr(request_or_app, "app", request_or_app)
    state = app.state
    store = getattr(state, "oauth_pending", None)
    if store is None:
        with _store_lock:
            store = getattr(state, "oauth_pending", None)
            if store is None:
                store = PendingStore()
                state.oauth_pending = store
    return store


def _remember_pending(request: Request, nonce: str) -> None:
    items = [n for n in request.session.get(SESSION_PENDING_KEY, []) if isinstance(n, str)]
    request.session[SESSION_PENDING_KEY] = [*items[-4:], nonce]


def _pending_for(request: Request, nonce: str, kind: str, now: datetime) -> dict[str, Any] | None:
    if not nonce or nonce not in (request.session.get(SESSION_PENDING_KEY) or []):
        return None
    return pending_store(request).get(nonce, kind, now)


def _forget_pending(request: Request, nonce: str) -> None:
    pending_store(request).pop(nonce)
    items = [n for n in request.session.get(SESSION_PENDING_KEY, []) if n != nonce]
    request.session[SESSION_PENDING_KEY] = items


# --------------------------------------------------------------------------------------- account upsert


def reset_account_history(db: Session, account_id: int) -> None:
    """Forget everything stored for what the account row pointed at before: daily analytics snapshots, post
    metrics («Mejores posts», best times, CSV), inbox comments and the inbox poll bookmark. Used when the row
    is reused for a *different* page / Instagram account / channel (one account per network per brand), so
    the new one neither inherits the old follower history and comments (whose replies would fail) nor starts
    its comment import from the old bookmark instead of the full window. Flushes, does not commit."""
    from planificador import inbox

    for model in (AnalyticsSnapshot, PostMetric, InboxComment):
        db.execute(delete(model).where(model.account_id == account_id))
    state = inbox.poll_state(db)
    if state.pop(str(account_id), None) is not None:
        config.set_setting(db, inbox.STATE_KEY, state, commit=False)
    db.flush()


def upsert_account(
    db: Session,
    brand_id: int,
    network: str,
    external_id: str,
    now: datetime,
    *,
    name: str,
    username: str | None = None,
    account_type: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    token_expires_at: datetime | None = None,
    refresh_expires_at: datetime | None = None,
    scopes: list[str] | None = None,
    meta: dict[str, Any] | None = None,
    keep_meta: tuple[str, ...] = (),
) -> Account:
    """Create or update the brand's account for ``network`` (one per network per brand) as ``connected``
    with freshly encrypted tokens. ``meta`` replaces the stored meta, except the ``keep_meta`` keys when the
    same external account reconnects. Expiry-warning bookkeeping (``*_warned``) is always reset.

    A *different* external account replacing the brand's current one keeps the row (scheduled posts follow
    it) but drops the previous account's analytics, post metrics and comments
    (:func:`reset_account_history`)."""
    account = _brand_account(db, brand_id, network)
    old_meta: dict[str, Any] = {}
    if account is None:
        account = Account(brand_id=brand_id, network=network, external_id=external_id)
        db.add(account)
    else:
        if account.external_id == external_id:
            old_meta = dict(account.meta or {})
        else:
            log.info(
                "Account %s (%s) now points at another %s account: resetting its history",
                account.id,
                network,
                network,
            )
            reset_account_history(db, account.id)
            account.avatar_path = None
    new_meta = {k: v for k, v in old_meta.items() if k in keep_meta}
    new_meta.update({k: v for k, v in (meta or {}).items() if v is not None})
    for key in [k for k in new_meta if k.endswith("_warned")]:
        new_meta.pop(key)
    account.external_id = external_id
    account.name = (name or external_id)[:255]
    account.username = str(username)[:255] if username else None
    account.account_type = account_type
    account.status = AccountStatus.CONNECTED
    account.set_tokens(
        access_token,
        refresh_token,
        token_expires_at=token_expires_at,
        refresh_expires_at=refresh_expires_at,
    )
    account.scopes = list(scopes or [])
    account.meta = new_meta
    account.connected_at = now
    account.last_checked_at = now
    account.last_error = None
    db.flush()
    return account


def cache_avatar(http: httpx.Client, url: str | None, account: Account, avatars_dir: Path) -> bool:
    """Download the account picture once (provider URLs are signed and expire) as a small JPEG.
    Best effort: any problem leaves the initials avatar."""
    if not url or not str(url).startswith("https://") or account.id is None:
        return False
    try:
        data = bytearray()
        with http.stream("GET", str(url), timeout=HTTP_TIMEOUT, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return False
            for chunk in resp.iter_bytes():
                data.extend(chunk)
                if len(data) > AVATAR_MAX_BYTES:
                    return False
        from PIL import Image

        with Image.open(io.BytesIO(bytes(data))) as img:
            img.draft("RGB", (AVATAR_SIZE * 2, AVATAR_SIZE * 2))
            picture = img.convert("RGB")
        picture.thumbnail((AVATAR_SIZE, AVATAR_SIZE))
        avatars_dir.mkdir(parents=True, exist_ok=True)
        target = avatars_dir / f"account-{account.id}.jpg"
        tmp = target.with_suffix(".tmp")
        picture.save(tmp, "JPEG", quality=88)
        tmp.replace(target)
        account.avatar_path = target.name
        return True
    except Exception as exc:  # noqa: BLE001 - cosmetic only
        log.info("Avatar for account %s not cached: %s", account.id, type(exc).__name__)
        return False


# ---------------------------------------------------------------------------------------------- cards


@dataclass
class Card:
    network: str
    label: str
    description: str
    account: Account | None
    status: str
    configured: bool
    credentials_group: str
    settings_url: str | None
    missing_message: str | None
    redirect_uri: str | None
    guide_url: str | None
    connect_url: str | None
    type_label: str = ""
    avatar_url: str | None = None
    expiry_text: str | None = None
    expiry_tone: str = "muted"
    expiry_when: datetime | None = None
    details: list[tuple[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pending_targets: int = 0
    youtube: dict[str, Any] | None = None
    tiktok: dict[str, Any] | None = None  # publishing modes (tiktok_modes_info)
    requested_scopes: list[str] = field(default_factory=list)  # TikTok: scopes asked for at login
    polling: bool = False

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def status_tone(self) -> str:
        return STATUS_TONES[self.status]

    @property
    def is_connected(self) -> bool:
        return self.status == "connected"

    @property
    def has_account(self) -> bool:
        return self.status != "none"


def _connect_url(network: str) -> str | None:
    return {
        "facebook": "/conexiones/meta/iniciar",
        "instagram": "/conexiones/meta/iniciar?red=instagram",
        "tiktok": "/conexiones/tiktok/iniciar",
        "youtube": "/conexiones/youtube/iniciar",
    }.get(network)


def _lock_test_running(request: Request, account_id: int) -> bool:
    running = getattr(request.app.state, "yt_lock_tests", None)
    return bool(running) and account_id in running


def _youtube_info(request: Request, account: Account) -> dict[str, Any]:
    from planificador.providers import youtube

    meta = dict(account.meta or {})
    status = youtube.lock_status(meta)
    raw_test = meta.get("youtube_lock_test")
    test: dict[str, Any] = raw_test if isinstance(raw_test, dict) else {}
    running = test.get("state") == "running" and _lock_test_running(request, account.id)
    interrupted = test.get("state") == "running" and not running
    mode = meta.get(youtube.META_PUBLISH_MODE) or (
        "api" if youtube.default_auto_publish(meta) else "assisted"
    )
    studio = getattr(youtube, "studio_upload_url", None)
    return {
        "lock_status": status,
        "lock_label": {
            "unknown": "Sin comprobar",
            "unlocked": "Subidas automáticas permitidas",
            "locked": "YouTube deja las subidas en privado",
        }[status],
        "lock_tone": {"unknown": "muted", "unlocked": "success", "locked": "danger"}[status],
        "test_running": running,
        "test_interrupted": interrupted,
        "test_message": test.get("message") if test.get("state") == "done" else None,
        "test_result": test.get("result"),
        "test_finished_at": test.get("finished_at"),
        "mode": mode if mode in ("api", "assisted") else "assisted",
        "audited": bool(meta.get(youtube.META_AUDITED)),
        "automatic_allowed": youtube.automatic_publishing_allowed(meta),
        "testing": account.refresh_expires_at is not None or meta.get("client_status_hint") == "testing",
        "studio_url": studio(account.external_id) if callable(studio) else "https://studio.youtube.com/",
    }


def _expiry(account: Account, now: datetime) -> tuple[str | None, str, datetime | None]:
    """(text, tone, when) describing how long the authorisation lasts."""
    network = str(account.network)
    if network in ("facebook", "instagram"):
        return "Token de página sin caducidad fija", "success", None
    if network == "demo":
        return "No caduca (cuenta de prueba)", "success", None
    if account.refresh_token_enc:
        if account.refresh_expires_at is not None:
            remaining = account.refresh_expires_at - now
            if remaining.total_seconds() <= 0:
                return "La autorización ha caducado", "danger", account.refresh_expires_at
            tone = "warning" if remaining < timedelta(days=8) else "success"
            return "La autorización caduca", tone, account.refresh_expires_at
        return "Se renueva automáticamente", "success", None
    if account.token_expires_at is not None:
        if account.token_expires_at <= now:
            return "El token ha caducado", "danger", account.token_expires_at
        return "El token caduca", "warning", account.token_expires_at
    return "Sin caducidad conocida", "muted", None


def build_card(
    request: Request,
    network: str,
    account: Account | None,
    ps: ProviderSettings,
    now: datetime,
    pending: dict[int, int] | None = None,
) -> Card:
    info = NETWORK_INFO[network]
    group = info["credentials"]
    configured = credentials_ready(ps, group) if group else True
    uris = redirect_uris(ps) if group else {}
    has_account = account is not None and account.status != AccountStatus.DISCONNECTED
    status = str(account.status) if has_account and account is not None else "none"
    card = Card(
        network=network,
        label=info["label"],
        description=info["what"],
        account=account if has_account else None,
        status=status,
        configured=configured,
        credentials_group=group,
        settings_url=CREDENTIAL_GROUPS[group]["anchor"] if group else None,
        missing_message=None if configured or not group else CREDENTIAL_GROUPS[group]["missing"],
        redirect_uri=uris.get(group),
        guide_url=f"/ayuda/{info['guide']}" if info["guide"] else None,
        connect_url=_connect_url(network),
    )
    if network == "tiktok":
        from planificador.providers import tiktok

        card.requested_scopes = tiktok.login_scopes(ps)
    if not has_account or account is None:
        return card
    card.type_label = account_type_label(account)
    card.avatar_url = avatar_url(account)
    text, tone, when = _expiry(account, now)
    card.expiry_text, card.expiry_tone, card.expiry_when = text, tone, when
    card.pending_targets = (pending or {}).get(account.id, 0)
    meta = dict(account.meta or {})
    details: list[tuple[str, Any]] = [("Conectada", account.connected_at)]
    if account.last_checked_at and account.last_checked_at != account.connected_at:
        details.append(("Última comprobación", account.last_checked_at))
    if network == "instagram" and meta.get("page_name"):
        details.append(("Página vinculada", meta.get("page_name")))
    card.details = details
    if network == "instagram" and str(account.account_type or "") != "business":
        card.warnings.append(
            "Instagram solo permite programar historias con cuentas de empresa."
            + (
                " Esta cuenta es de creador: las historias tendrás que publicarlas a mano."
                if account.account_type == "creator"
                else " Si tu cuenta es de creador, publica las historias a mano."
            )
        )
    if network == "tiktok":
        card.tiktok = tiktok_modes_info(account)
        creator = meta.get("creator_info") if isinstance(meta.get("creator_info"), dict) else None
        if (
            creator
            and creator.get("privacy_level_options") == ["SELF_ONLY"]
            and "direct" in card.tiktok["available"]
        ):
            card.warnings.append(
                "Tu app de TikTok aún no está auditada: solo podrás publicar en privado («Solo yo»)"
                + (
                    " o enviar los vídeos como borrador a la app de TikTok."
                    if "inbox" in card.tiktok["available"]
                    else "."
                )
            )
        warning = tiktok_scope_warning(account.scopes or meta.get("granted_scopes"))
        if warning:
            card.warnings.append(warning)
    if network == "youtube":
        from planificador.providers import youtube

        card.youtube = _youtube_info(request, account)
        card.polling = bool(card.youtube["test_running"])
        if card.youtube["testing"] and account.refresh_expires_at is not None:
            card.warnings.append(
                "Tu app de Google está en modo «Prueba»: la autorización dura 7 días y tendrás que "
                "reconectar cada semana. Publícala («En producción») en Google Cloud para evitarlo."
            )
        card.warnings.extend(youtube.scope_warnings(meta.get("granted_scopes") or account.scopes or None))
    return card


def brand_cards(request: Request, db: Session, brand: Brand, now: datetime) -> list[Card]:
    ps = _provider_settings(request, db)
    accounts = {str(a.network): a for a in db.scalars(select(Account).where(Account.brand_id == brand.id))}
    pending = _pending_counts(db, [a.id for a in accounts.values()])
    networks = list(REAL_NETWORKS)
    demo_account = accounts.get("demo")
    if ps.demo_mode or (demo_account is not None and demo_account.status != AccountStatus.DISCONNECTED):
        networks.append("demo")
    return [build_card(request, n, accounts.get(n), ps, now, pending) for n in networks]


# ---------------------------------------------------------------------------------------------- pages


@router.get("/conexiones")
def connections_page(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    now = clock.now()
    cards = brand_cards(request, db, brand, now)
    connected = sum(1 for c in cards if c.is_connected and c.network != "demo")
    attention = [c for c in cards if c.status == "needs_reconnect"]
    return _render(
        request,
        "connections/index.html",
        page_title="Cuentas conectadas",
        cards=cards,
        connected_count=connected,
        real_count=len(REAL_NETWORKS),
        attention=attention,
        demo_on=config.is_demo_mode(db),
        now=now,
    )


def _card_response(request: Request, db: Session, brand_id: int, network: str, now: datetime) -> Response:
    ps = _provider_settings(request, db)
    account = _brand_account(db, brand_id, network)
    pending = _pending_counts(db, [account.id] if account else [])
    card = build_card(request, network, account, ps, now, pending)
    return _render(request, "connections/_card_response.html", card=card, now=now)


@router.get("/conexiones/tarjeta/{network}")
def connection_card(
    request: Request, network: str, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    if network not in NETWORK_INFO:
        raise HTTPException(status_code=404, detail="Red desconocida.")
    return _card_response(request, db, brand.id, network, clock.now())


@router.get("/conexiones/avatar/{account_id:int}")
def account_avatar(request: Request, account_id: int, db: DbSession) -> Response:
    account = db.get(Account, account_id)
    if account is None or not account.avatar_path:
        raise HTTPException(status_code=404)
    path = _paths(request).avatars_dir / Path(account.avatar_path).name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _after_callback_error(request: Request, network: str, exc: Exception) -> Response:
    if isinstance(exc, ConnectError):
        _flash(request, exc.message, exc.kind)
    elif isinstance(exc, ProviderError):
        log.warning("%s connection failed: %s %s", network, exc.code, exc.detail)
        _flash(request, f"No se pudo conectar {_network_label(network)}: {exc.message}", "error")
    return RedirectResponse("/conexiones", status_code=303)


def _cancelled(error: str, description: str, label: str) -> ConnectError:
    if error in ("access_denied", "user_denied", "user_cancelled") or "denied" in error:
        return ConnectError(f"Has cancelado la conexión con {label}.", "info")
    detail = f": {description[:200]}" if description else f" ({error[:60]})."
    return ConnectError(f"{label} no autorizó la conexión{detail}")


def _require_credentials(ps: ProviderSettings, group: str) -> None:
    if not credentials_ready(ps, group):
        raise ConnectError(CREDENTIAL_GROUPS[group]["missing"], "warning")


def _start_error(request: Request, group: str, exc: Exception) -> Response:
    kind = exc.kind if isinstance(exc, ConnectError) else "error"
    _flash(request, getattr(exc, "message", str(exc)), kind)
    return RedirectResponse(CREDENTIAL_GROUPS[group]["anchor"], status_code=303)


# ---------------------------------------------------------------------------------------------- Meta


@router.get("/conexiones/meta/iniciar")
def meta_start(
    request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep, red: str = ""
) -> Response:
    from planificador.providers import meta_common

    ps = _provider_settings(request, db)
    try:
        _require_credentials(ps, "meta")
        focus = "instagram" if red == "instagram" else "facebook"
        state = new_oauth_state(db, request, "meta", brand.id, clock.now(), extra={"focus": focus})
        config_id = str(config.get_setting(db, "meta_config_id", "") or "").strip() or None
        url = _call(meta_common.build_login_url, ps, meta_common.redirect_uri(ps), state, config_id=config_id)
    except (ConnectError, ProviderError) as exc:
        return _start_error(request, "meta", exc)
    return RedirectResponse(url, status_code=303)


@router.get("/conexiones/callback/meta")
def meta_callback(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    state: str = "",
    code: str = "",
    error: str = "",
    error_reason: str = "",
    error_description: str = "",
) -> Response:
    from planificador.providers import meta_common

    now = clock.now()
    try:
        flow = consume_oauth_state(db, request, "meta", state, now)
        if error or error_reason:
            raise _cancelled(error_reason or error, error_description, "Meta")
        if not code:
            raise ConnectError("Meta no devolvió el código de autorización. Vuelve a intentarlo.")
        ps = _provider_settings(request, db)
        _require_credentials(ps, "meta")
        with _http_client() as http:
            user_token, user_expires = _call(
                meta_common.exchange_code, ps, http, code, meta_common.redirect_uri(ps), clock=clock
            )
            pages = _call(meta_common.list_pages, ps, http, user_token, include_business=True)
            granted: list[str] | None = None
            try:
                granted = meta_common.granted_permissions(ps, http, user_token)
            except ProviderError as exc:
                log.info("Meta permissions lookup skipped: %s", exc.code)
    except (ConnectError, ProviderError) as exc:
        return _after_callback_error(request, "facebook", exc)
    missing = meta_common.missing_scopes(granted) if granted else []
    nonce = pending_store(request).put(
        "meta",
        {
            "brand_id": flow.brand_id,
            "focus": flow.extra.get("focus", "facebook"),
            "user_token": user_token,
            "user_expires": user_expires,
            "pages": list(pages),
            "granted": granted or [],
            "missing": missing,
        },
        now,
    )
    _remember_pending(request, nonce)
    return RedirectResponse(f"/conexiones/meta/elegir?pendiente={nonce}", status_code=303)


def _page_view(page: Any) -> dict[str, Any]:
    from planificador.providers import meta_common

    page_warnings = getattr(meta_common, "page_warnings", None)
    ig = page.instagram
    return {
        "id": str(page.id),
        "name": page.name,
        "picture_url": page.picture_url,
        "category": getattr(page, "category", None),
        "followers": getattr(page, "followers_count", None),
        "can_publish": bool(getattr(page, "can_publish", True)),
        # The missing-Instagram warning is already shown as "Sin Instagram vinculado".
        "warnings": [w for w in page_warnings(page) if ig is not None or "Instagram" not in w]
        if callable(page_warnings)
        else [],
        "instagram": None
        if ig is None
        else {
            "id": str(ig.id),
            "username": ig.username,
            "name": ig.name,
            "picture_url": ig.picture_url,
            "followers": getattr(ig, "followers_count", None),
        },
    }


@router.get("/conexiones/meta/elegir")
def meta_choose(request: Request, db: DbSession, clock: ClockDep, pendiente: str = "") -> Response:
    data = _pending_for(request, pendiente, "meta", clock.now())
    if data is None:
        _flash(request, MSG_PENDING_GONE, "warning")
        return RedirectResponse("/conexiones", status_code=303)
    target_brand = db.get(Brand, data["brand_id"])
    current = {}
    for net in ("facebook", "instagram"):
        acc = _brand_account(db, data["brand_id"], net)
        current[net] = acc if acc is not None and acc.status != AccountStatus.DISCONNECTED else None
    pages = [_page_view(p) for p in data["pages"]]
    selectable = [p for p in pages if p["can_publish"]]
    preselect = None
    if current["facebook"] is not None:
        preselect = next((p["id"] for p in selectable if p["id"] == current["facebook"].external_id), None)
    if preselect is None and current["instagram"] is not None:
        preselect = next(
            (
                p["id"]
                for p in selectable
                if p["instagram"] and p["instagram"]["id"] == current["instagram"].external_id
            ),
            None,
        )
    if preselect is None and data.get("focus") == "instagram":
        preselect = next((p["id"] for p in selectable if p["instagram"]), None)
    if preselect is None and selectable:
        preselect = selectable[0]["id"]
    return _render(
        request,
        "connections/meta_chooser.html",
        page_title="Elige tu página de Facebook",
        pending=pendiente,
        pages=pages,
        preselect=preselect,
        focus=data.get("focus", "facebook"),
        missing_scopes=data.get("missing", []),
        target_brand=target_brand,
        current=current,
        nav_active="conexiones",
    )


@router.post("/conexiones/meta/seleccionar")
def meta_select(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    pendiente: Annotated[str, Form()] = "",
    page_id: Annotated[str, Form()] = "",
    facebook: Annotated[str, Form()] = "",
    instagram: Annotated[str, Form()] = "",
    ig_type: Annotated[str, Form()] = "",
) -> Response:
    now = clock.now()
    data = _pending_for(request, pendiente, "meta", now)
    if data is None:
        _flash(request, MSG_PENDING_GONE, "warning")
        return RedirectResponse("/conexiones", status_code=303)
    page = next((p for p in data["pages"] if str(p.id) == page_id), None)
    back = f"/conexiones/meta/elegir?pendiente={pendiente}"
    if page is None:
        _flash(request, "Elige una de las páginas de la lista.", "error")
        return RedirectResponse(back, status_code=303)
    want_fb, want_ig = bool(facebook), bool(instagram)
    if not want_fb and not want_ig:
        _flash(request, "Marca al menos una red (Facebook o Instagram) para conectar.", "error")
        return RedirectResponse(back, status_code=303)
    if not getattr(page, "can_publish", True):
        _flash(
            request,
            "Tu rol en esa página no permite crear contenido (falta la tarea CREATE_CONTENT). Elige otra "
            "página o pide a un administrador que te dé permisos de contenido.",
            "error",
        )
        return RedirectResponse(back, status_code=303)
    if want_ig and page.instagram is None:
        _flash(request, "Esa página no tiene ninguna cuenta profesional de Instagram vinculada.", "error")
        return RedirectResponse(back, status_code=303)
    brand_id = int(data["brand_id"])
    if db.get(Brand, brand_id) is None:
        _flash(request, "La marca ya no existe.", "error")
        return RedirectResponse("/conexiones", status_code=303)
    user_expires = data.get("user_expires")
    shared_meta: dict[str, Any] = {
        "user_token_enc": crypto.encrypt(data["user_token"]) if data.get("user_token") else None,
        "user_token_expires_at": user_expires.isoformat() if isinstance(user_expires, datetime) else None,
        "page_id": str(page.id),
        "page_name": page.name,
        "tasks": list(getattr(page, "tasks", []) or []),
    }
    granted = list(data.get("granted") or [])
    connected: list[tuple[Account, str | None]] = []
    if want_fb:
        account = upsert_account(
            db,
            brand_id,
            Network.FACEBOOK,
            str(page.id),
            now,
            name=page.name,
            username=getattr(page, "username", None),
            account_type="page",
            access_token=page.access_token,
            scopes=granted,
            meta={**shared_meta, "category": getattr(page, "category", None)},
        )
        connected.append((account, page.picture_url))
    if want_ig and page.instagram is not None:
        ig = page.instagram
        account = upsert_account(
            db,
            brand_id,
            Network.INSTAGRAM,
            str(ig.id),
            now,
            name=ig.name or ig.username or str(ig.id),
            username=ig.username,
            account_type=IG_TYPES.get(ig_type) or getattr(ig, "account_type", None),
            access_token=page.access_token,
            scopes=granted,
            meta={**shared_meta, "username": ig.username},
        )
        connected.append((account, ig.picture_url))
    db.commit()
    avatars_dir = _paths(request).avatars_dir
    with _http_client() as http:
        for account, picture in connected:
            cache_avatar(http, picture, account, avatars_dir)
    db.commit()
    _forget_pending(request, pendiente)
    names = " e " if len(connected) == 2 and str(connected[1][0].network) == "instagram" else " y "
    summary = names.join(f"{_network_label(str(a.network))} ({a.display_name})" for a, _ in connected)
    _flash(request, f"Conectado: {summary}.", "success")
    missing = data.get("missing") or []
    if missing:
        _flash(
            request,
            "Faltan permisos de Meta (" + ", ".join(missing) + "). Algunas funciones pueden fallar: vuelve "
            "a conectar y acepta todos los permisos.",
            "warning",
        )
    return RedirectResponse("/conexiones", status_code=303)


# -------------------------------------------------------------------------------------------- TikTok


@router.get("/conexiones/tiktok/iniciar")
def tiktok_start(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    from planificador.providers import tiktok

    ps = _provider_settings(request, db)
    try:
        _require_credentials(ps, "tiktok")
        verifier, challenge = tiktok.new_pkce_pair(ps.tiktok_pkce_encoding or "hex")
        state = new_oauth_state(db, request, "tiktok", brand.id, clock.now(), code_verifier=verifier)
        # Exactly the scopes ticked in Ajustes (TikTok rejects the whole request for a scope the app lacks).
        scopes = tiktok.login_scopes(ps)
        url = _call(tiktok.build_login_url, ps, tiktok.redirect_uri(ps), state, challenge, scopes=scopes)
    except (ConnectError, ProviderError) as exc:
        return _start_error(request, "tiktok", exc)
    return RedirectResponse(url, status_code=303)


def _tiktok_creator_info(ps: ProviderSettings, http: httpx.Client, tokens: Any, now: datetime) -> dict | None:
    """Best effort ``creator_info`` cache at connection time (the composer refreshes it before posting)."""
    from planificador.providers import tiktok

    creds = AccountCreds(
        id=0,
        network="tiktok",
        external_id=str(tokens.open_id or ""),
        name="",
        account_type=None,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        token_expires_at=tokens.expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
        meta={"granted_scopes": list(tokens.scopes or [])},
    )
    if tokens.scopes and "direct" not in tiktok.available_modes(tokens.scopes):
        return None  # creator_info belongs to Direct Post (video.publish): an inbox-only account has none
    try:
        info = tiktok.query_creator_info(creds, ps, http)
    except ProviderError as exc:
        log.info("TikTok creator_info skipped at connect: %s", exc.code)
        return None
    data = info.to_dict() if hasattr(info, "to_dict") else dict(vars(info))
    data["fetched_at"] = now.isoformat()
    return data


@router.get("/conexiones/callback/social1")
def tiktok_callback(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
) -> Response:
    from planificador.providers import tiktok

    now = clock.now()
    try:
        flow = consume_oauth_state(db, request, "tiktok", state, now)
        if error:
            raise _cancelled(error, error_description, "TikTok")
        if not code:
            raise ConnectError("TikTok no devolvió el código de autorización. Vuelve a intentarlo.")
        if not flow.code_verifier:
            raise ConnectError(MSG_STATE_INVALID)
        ps = _provider_settings(request, db)
        _require_credentials(ps, "tiktok")
        with _http_client() as http:
            tokens = _call(
                tiktok.exchange_code, ps, http, code, tiktok.redirect_uri(ps), flow.code_verifier, clock=clock
            )
            user = tiktok.get_user_info(ps, http, tokens.access_token)
            creator = _tiktok_creator_info(ps, http, tokens, now)
            open_id = str(tokens.open_id or user.get("open_id") or "")
            if not open_id:
                raise ConnectError("TikTok no devolvió el identificador de la cuenta. Vuelve a intentarlo.")
            username = user.get("username") or (creator or {}).get("username")
            meta: dict[str, Any] = {
                "open_id": open_id,
                "union_id": user.get("union_id"),
                "granted_scopes": list(tokens.scopes or []),
                "profile_available": user.get("profile_available"),
                "is_verified": user.get("is_verified"),
                "profile_deep_link": user.get("profile_deep_link"),
                "creator_info": creator,
            }
            account = upsert_account(
                db,
                flow.brand_id,
                Network.TIKTOK,
                open_id,
                now,
                name=user.get("display_name") or (creator or {}).get("nickname") or username or "TikTok",
                username=username,
                account_type=None,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                token_expires_at=tokens.expires_at,
                refresh_expires_at=tokens.refresh_expires_at,
                scopes=list(tokens.scopes or []),
                meta=meta,
            )
            db.commit()
            picture = (
                user.get("avatar_large_url") or user.get("avatar_url") or (creator or {}).get("avatar_url")
            )
            cache_avatar(http, picture, account, _paths(request).avatars_dir)
            db.commit()
    except (ConnectError, ProviderError) as exc:
        db.rollback()
        return _after_callback_error(request, "tiktok", exc)
    _flash(request, f"Conectado: TikTok ({account.display_name}).", "success")
    message = tiktok_scope_warning(tokens.scopes)
    if message:
        _flash(request, message, "warning")
    return RedirectResponse("/conexiones", status_code=303)


def tiktok_scope_warning(granted: Any) -> str | None:
    """Warning for a TikTok connection without any publishing permission (at least one of ``video.publish`` /
    ``video.upload`` is needed; one alone is fine — the composer then offers only that mode)."""
    from planificador.providers import tiktok

    if granted and not tiktok.available_modes(granted):
        return (
            "TikTok no ha concedido ningún permiso de publicación (video.publish o video.upload), así que no "
            "podrás publicar con esta cuenta. Comprueba que tu app de TikTok tiene Content Posting API, "
            "marca sus permisos en Ajustes › Credenciales › TikTok y vuelve a conectar aceptándolos."
        )
    return None


TIKTOK_MODE_LABELS: dict[str, tuple[str, str]] = {
    "direct": ("Publicación directa", "send"),
    "inbox": ("Borrador en la app", "inbox"),
}


def tiktok_modes_info(account: Account) -> dict[str, Any]:
    """Publishing modes of a TikTok account for its card: ``[{"key", "label", "icon", "available"}]`` plus a
    Spanish ``note`` when one mode is missing (``known`` is False when the grants are unknown)."""
    from planificador.providers import tiktok

    granted = tiktok.granted_scopes(account)
    available = tiktok.available_modes(granted)
    modes = [
        {"key": key, "label": label, "icon": icon_name, "available": key in available}
        for key, (label, icon_name) in TIKTOK_MODE_LABELS.items()
    ]
    note = None
    if granted is not None and available and len(available) < len(TIKTOK_MODE_LABELS):
        missing = next(m for m in TIKTOK_MODE_LABELS if m not in available)
        note = tiktok.MSG_MODE_UNAVAILABLE[missing]
    return {"known": granted is not None, "modes": modes, "available": list(available), "note": note}


# ------------------------------------------------------------------------------------------- YouTube


@router.get("/conexiones/youtube/iniciar")
def youtube_start(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    from planificador.providers import youtube

    ps = _provider_settings(request, db)
    try:
        _require_credentials(ps, "google")
        verifier, challenge = youtube.new_pkce_pair()
        state = new_oauth_state(db, request, "youtube", brand.id, clock.now(), code_verifier=verifier)
        url = youtube.build_login_url(ps, youtube.redirect_uri(ps), state, challenge)
    except (ConnectError, ProviderError) as exc:
        return _start_error(request, "google", exc)
    return RedirectResponse(url, status_code=303)


_YT_KEEP_META = (
    "youtube_lock_status",
    "youtube_publish_mode",
    "youtube_project_audited",
    "youtube_lock_test",
)


def save_youtube_channel(
    request: Request, db: Session, http: httpx.Client, brand_id: int, channel: Any, tokens: Any, now: datetime
) -> Account:
    """Create/update the YouTube account for ``channel`` (keeps the lock status and mode of the same
    channel across reconnections)."""
    from planificador.providers import youtube

    meta = youtube.account_meta_from_channel(channel, tokens)
    existing = _brand_account(db, brand_id, Network.YOUTUBE)
    if existing is not None and existing.external_id == str(channel.id):
        for key in _YT_KEEP_META:
            if key in (existing.meta or {}):
                meta[key] = existing.meta[key]
    test = meta.get("youtube_lock_test")
    if isinstance(test, dict) and test.get("state") == "running":
        meta.pop("youtube_lock_test")
    account = upsert_account(
        db,
        brand_id,
        Network.YOUTUBE,
        str(channel.id),
        now,
        name=channel.title or str(channel.id),
        username=getattr(channel, "handle", None),
        account_type="channel",
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        token_expires_at=tokens.expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
        scopes=list(tokens.scopes or []),
        meta=meta,
    )
    db.commit()
    cache_avatar(http, getattr(channel, "thumbnail_url", None), account, _paths(request).avatars_dir)
    db.commit()
    return account


def _youtube_connected_flashes(request: Request, account: Account, tokens: Any) -> None:
    from planificador.providers import youtube

    _flash(request, f"Conectado: YouTube ({account.name}).", "success")
    if getattr(tokens, "refresh_expires_at", None) is not None:
        _flash(
            request,
            getattr(tokens, "warning", None)
            or "Tu app de Google está en modo «Prueba»: tendrás que reconectar YouTube cada 7 días.",
            "warning",
        )
    if not tokens.refresh_token:
        _flash(
            request,
            "Google no devolvió un token de renovación: tendrás que reconectar YouTube cuando caduque la "
            "sesión.",
            "warning",
        )
    for message in youtube.scope_warnings(list(tokens.scopes or []) or None):
        _flash(request, message, "warning")


@router.get("/conexiones/callback/google")
def youtube_callback(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
) -> Response:
    from planificador.providers import youtube

    now = clock.now()
    try:
        flow = consume_oauth_state(db, request, "youtube", state, now)
        if error:
            raise _cancelled(error, error_description, "Google")
        if not code:
            raise ConnectError("Google no devolvió el código de autorización. Vuelve a intentarlo.")
        if not flow.code_verifier:
            raise ConnectError(MSG_STATE_INVALID)
        ps = _provider_settings(request, db)
        _require_credentials(ps, "google")
        with _http_client() as http:
            tokens = _call(
                youtube.exchange_code,
                ps,
                http,
                code,
                youtube.redirect_uri(ps),
                flow.code_verifier,
                clock=clock,
            )
            channels = list(youtube.list_my_channels(ps, http, tokens.access_token))
            if not channels:
                raise ConnectError(
                    "Esta cuenta de Google no tiene canal de YouTube. Créalo en youtube.com y vuelve a "
                    "conectar."
                )
            if len(channels) == 1:
                account = save_youtube_channel(request, db, http, flow.brand_id, channels[0], tokens, now)
                _youtube_connected_flashes(request, account, tokens)
                return RedirectResponse("/conexiones", status_code=303)
    except (ConnectError, ProviderError) as exc:
        db.rollback()
        return _after_callback_error(request, "youtube", exc)
    nonce = pending_store(request).put(
        "youtube", {"brand_id": flow.brand_id, "tokens": tokens, "channels": channels}, now
    )
    _remember_pending(request, nonce)
    return RedirectResponse(f"/conexiones/youtube/elegir?pendiente={nonce}", status_code=303)


@router.get("/conexiones/youtube/elegir")
def youtube_choose(request: Request, db: DbSession, clock: ClockDep, pendiente: str = "") -> Response:
    data = _pending_for(request, pendiente, "youtube", clock.now())
    if data is None:
        _flash(request, MSG_PENDING_GONE, "warning")
        return RedirectResponse("/conexiones", status_code=303)
    current = _brand_account(db, data["brand_id"], Network.YOUTUBE)
    return _render(
        request,
        "connections/youtube_chooser.html",
        page_title="Elige tu canal de YouTube",
        pending=pendiente,
        channels=data["channels"],
        testing=getattr(data["tokens"], "refresh_expires_at", None) is not None,
        target_brand=db.get(Brand, data["brand_id"]),
        current=current if current is not None and current.status != AccountStatus.DISCONNECTED else None,
        nav_active="conexiones",
    )


@router.post("/conexiones/youtube/seleccionar")
def youtube_select(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    pendiente: Annotated[str, Form()] = "",
    channel_id: Annotated[str, Form()] = "",
) -> Response:
    now = clock.now()
    data = _pending_for(request, pendiente, "youtube", now)
    if data is None:
        _flash(request, MSG_PENDING_GONE, "warning")
        return RedirectResponse("/conexiones", status_code=303)
    channel = next((c for c in data["channels"] if str(c.id) == channel_id), None)
    if channel is None:
        _flash(request, "Elige uno de los canales de la lista.", "error")
        return RedirectResponse(f"/conexiones/youtube/elegir?pendiente={pendiente}", status_code=303)
    if db.get(Brand, data["brand_id"]) is None:
        _flash(request, "La marca ya no existe.", "error")
        return RedirectResponse("/conexiones", status_code=303)
    tokens = data["tokens"]
    with _http_client() as http:
        account = save_youtube_channel(request, db, http, int(data["brand_id"]), channel, tokens, now)
    _forget_pending(request, pendiente)
    _youtube_connected_flashes(request, account, tokens)
    return RedirectResponse("/conexiones", status_code=303)


# ---------------------------------------------------------------------------------------------- Demo


@router.post("/conexiones/demo/conectar")
def demo_connect(request: Request, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    if not config.is_demo_mode(db):
        _flash(request, "Activa el modo prueba en Ajustes para usar la red Demo.", "warning")
        return _redirect(request, "/conexiones")
    now = clock.now()
    upsert_account(
        db,
        brand.id,
        Network.DEMO,
        f"demo-{brand.id}",
        now,
        name=f"{brand.name} (Demo)",
        username="demo",
        account_type="demo",
        access_token=f"demo-access-{secrets.token_hex(8)}",
        refresh_token=f"demo-refresh-{secrets.token_hex(8)}",
        token_expires_at=now + timedelta(days=60),
        meta={"demo": True},
    )
    db.commit()
    _flash(
        request,
        f"Cuenta Demo conectada a «{brand.name}». Ya puedes programar publicaciones de prueba.",
        "success",
    )
    return _redirect(request, "/conexiones")


# ------------------------------------------------------------------------------------- account actions


def _account_or_404(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada.")
    return account


def _revoke_best_effort(account: Account, ps: ProviderSettings) -> None:
    network = str(account.network)
    try:
        if network == "tiktok" and account.access_token and credentials_ready(ps, "tiktok"):
            from planificador.providers import tiktok

            with _http_client() as http:
                tiktok.revoke(ps, http, account.access_token)
        elif network == "youtube" and (account.refresh_token or account.access_token):
            from planificador.providers import youtube

            with _http_client() as http:
                youtube.revoke(ps, http, account.refresh_token or account.access_token or "")
    except Exception as exc:  # noqa: BLE001 - revoking is best effort
        log.info("Token revoke for account %s failed: %s", account.id, type(exc).__name__)


@router.post("/conexiones/{account_id:int}/desconectar")
def disconnect_account(request: Request, account_id: int, db: DbSession) -> Response:
    account = _account_or_404(db, account_id)
    ps = _provider_settings(request, db)
    _revoke_best_effort(account, ps)
    account.wipe_tokens()
    account.status = AccountStatus.DISCONNECTED
    account.last_error = None
    meta = dict(account.meta or {})
    for key in ("user_token_enc", "user_token_expires_at"):
        meta.pop(key, None)
    test = meta.get("youtube_lock_test")
    if isinstance(test, dict) and test.get("state") == "running":
        meta.pop("youtube_lock_test", None)
    account.meta = meta
    pending = _pending_counts(db, [account.id]).get(account.id, 0)
    db.commit()
    message = f"Has desconectado {_network_label(str(account.network))} ({account.display_name})."
    if pending == 1:
        message += " Hay 1 publicación pendiente que no saldrá hasta que vuelvas a conectar la cuenta."
    elif pending:
        message += (
            f" Hay {pending} publicaciones pendientes que no saldrán hasta que vuelvas a conectar la cuenta."
        )
    _flash(request, message, "success")
    return _redirect(request, "/conexiones")


@router.post("/conexiones/{account_id:int}/comprobar")
def check_account(request: Request, account_id: int, db: DbSession, clock: ClockDep) -> Response:
    from planificador.providers import registry

    account = _account_or_404(db, account_id)
    label = _network_label(str(account.network))
    if account.status == AccountStatus.DISCONNECTED:
        _flash(request, "La cuenta está desconectada.", "warning")
        return _redirect(request, "/conexiones")
    ps = _provider_settings(request, db)
    now = clock.now()
    try:
        provider = registry.get_provider(str(account.network))
        with _http_client() as http:
            provider.check_account(account.to_creds(), ps, http)
    except ProviderError as exc:
        account.last_checked_at = now
        account.last_error = exc.message
        if str(exc.kind) == "auth":
            account.status = AccountStatus.NEEDS_RECONNECT
            _flash(request, f"{label}: {exc.message}", "error")
        else:
            _flash(request, f"No se pudo comprobar {label}: {exc.message}", "warning")
        db.commit()
        return _redirect(request, "/conexiones")
    account.last_checked_at = now
    account.last_error = None
    account.status = AccountStatus.CONNECTED
    db.commit()
    _flash(request, f"La conexión con {label} funciona correctamente.", "success")
    return _redirect(request, "/conexiones")


# ----------------------------------------------------------------------------------- YouTube lock test


def _lock_test_call(
    creds: AccountCreds, ps: ProviderSettings, http: httpx.Client, clock: Clock
) -> tuple[str, str, str | None]:
    """``(status, message, kept clip id)``: the clip of an ``unlocked`` result is kept for the 24 h re-check
    (``providers/followups.py``), which reads it by this id (by title among the recent uploads without it)."""
    from planificador.providers import youtube

    detailed = getattr(youtube, "lock_test", None)
    if callable(detailed):
        result = detailed(creds, ps, http, clock)
        kept = getattr(result, "video_id", None) if not getattr(result, "deleted", False) else None
        return str(result.status), str(result.message), str(kept) if kept else None
    status = str(youtube.run_lock_test(creds, ps, http, clock))
    messages = {
        "unlocked": "YouTube respetó la visibilidad del vídeo de prueba: ya puedes publicar automáticamente.",
        "locked": "YouTube dejó el vídeo de prueba como privado: usa la publicación asistida.",
        "error": "No se pudo completar la prueba.",
    }
    return status, messages.get(status, messages["error"]), None


def run_lock_test_job(app_state: Any, account_id: int) -> None:
    """Background job: upload the test clip, then store ``youtube_lock_status`` (and switch the default
    publishing mode accordingly) plus the result message in ``accounts.meta``."""
    from planificador.providers import youtube

    database = app_state.db
    clock: Clock = app_state.clock
    try:
        with database.session() as s:
            account = s.get(Account, account_id)
            if account is None:
                return
            ps = config.provider_settings(s, base_url=app_state.settings.base_url)
            creds = account.to_creds()
        before = (creds.access_token, creds.refresh_token)
        video_id: str | None = None
        try:
            with _http_client() as http:
                status, message, video_id = _lock_test_call(creds, ps, http, clock)
        except ProviderError as exc:
            status, message = "error", f"No se pudo completar la prueba: {exc.message}"
        except Exception as exc:  # noqa: BLE001 - reported to the user
            log.exception("YouTube lock test crashed")
            status, message = "error", f"No se pudo completar la prueba ({type(exc).__name__})."
        with database.session() as s:
            account = s.get(Account, account_id)
            if account is None:
                return
            if (creds.access_token, creds.refresh_token) != before and creds.access_token:
                account.set_tokens(
                    creds.access_token,
                    creds.refresh_token,
                    token_expires_at=creds.token_expires_at,
                    refresh_expires_at=creds.refresh_expires_at,
                    keep_refresh=True,
                )
            meta = dict(account.meta or {})
            if status in ("unlocked", "locked"):
                meta[youtube.META_LOCK_STATUS] = status
                meta[youtube.META_PUBLISH_MODE] = "api" if status == "unlocked" else "assisted"
                if status == "unlocked":
                    message += " Hemos activado la publicación automática para este canal."
            meta["youtube_lock_test"] = {
                "state": "done",
                "result": status,
                "message": message,
                "finished_at": clock.now().isoformat(),
            }
            if video_id and status == "unlocked":
                meta["youtube_lock_test"]["video_id"] = video_id
            account.meta = meta
            s.commit()
    finally:
        running = getattr(app_state, "yt_lock_tests", None)
        if running is not None:
            running.discard(account_id)


_lock_tests_guard = threading.Lock()


@router.post("/conexiones/{account_id:int}/youtube/prueba-bloqueo")
def youtube_lock_test(request: Request, account_id: int, db: DbSession, clock: ClockDep) -> Response:
    account = _account_or_404(db, account_id)
    if account.network != Network.YOUTUBE or account.status == AccountStatus.DISCONNECTED:
        raise HTTPException(status_code=400, detail="La prueba de bloqueo solo está disponible para YouTube.")
    ps = _provider_settings(request, db)
    if not credentials_ready(ps, "google"):
        _flash(request, CREDENTIAL_GROUPS["google"]["missing"], "warning")
        return _redirect(request, "/conexiones")
    state = request.app.state
    with _lock_tests_guard:
        running: set[int] | None = getattr(state, "yt_lock_tests", None)
        if running is None:
            running = set()
            state.yt_lock_tests = running
        already = account_id in running
        running.add(account_id)
    brand_id = account.brand_id
    if not already:
        meta = dict(account.meta or {})
        meta["youtube_lock_test"] = {"state": "running", "started_at": clock.now().isoformat()}
        account.meta = meta
        db.commit()
        _start_background(lambda: run_lock_test_job(state, account_id), f"yt-lock-test-{account_id}")
    if _is_htmx(request):
        db.expire_all()
        return _card_response(request, db, brand_id, "youtube", clock.now())
    _flash(request, "Prueba de bloqueo en marcha: puede tardar unos minutos.", "info")
    return RedirectResponse("/conexiones", status_code=303)


@router.post("/conexiones/{account_id:int}/youtube/modo")
def youtube_mode(
    request: Request,
    account_id: int,
    db: DbSession,
    clock: ClockDep,
    modo: Annotated[str, Form()] = "assisted",
    auditado: Annotated[str, Form()] = "",
) -> Response:
    from planificador.providers import youtube

    account = _account_or_404(db, account_id)
    if account.network != Network.YOUTUBE:
        raise HTTPException(status_code=400, detail="Solo para cuentas de YouTube.")
    meta = dict(account.meta or {})
    meta[youtube.META_PUBLISH_MODE] = "api" if modo == "api" else "assisted"
    meta[youtube.META_AUDITED] = bool(auditado)
    account.meta = meta
    db.commit()
    if modo == "api" and not youtube.automatic_publishing_allowed(meta):
        message, kind = (
            "Publicación automática activada. Ojo: si tu proyecto de Google no está auditado, YouTube puede "
            "dejar los vídeos en privado. Haz la prueba de bloqueo para salir de dudas.",
            "warning",
        )
    else:
        message, kind = "Modo de publicación de YouTube guardado.", "success"
    if _is_htmx(request):
        from planificador.templating import toast_headers

        response = _card_response(request, db, account.brand_id, "youtube", clock.now())
        response.headers.update(toast_headers(message, kind))
        return response
    _flash(request, message, kind)
    return RedirectResponse("/conexiones", status_code=303)


# ---------------------------------------------------------------------------------------------- guides

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
GUIDE_DIRS: tuple[Path, ...] = (_PACKAGE_DIR / "docs", _PACKAGE_DIR.parent.parent / "docs")
GUIDE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
GUIDE_TITLES = {
    "meta": "Guía: app de Meta (Facebook e Instagram)",
    "tiktok": "Guía: app de TikTok",
    "youtube": "Guía: cliente de Google (YouTube)",
}


def guide_path(name: str) -> Path | None:
    """``docs/guia-<name>.md`` (package ``docs/`` first, then the repository's)."""
    if not GUIDE_NAME_RE.match(name or ""):
        return None
    for base in GUIDE_DIRS:
        candidate = base / f"guia-{name}.md"
        if candidate.is_file():
            return candidate
    return None


@router.get("/ayuda/{name}")
def guide_page(request: Request, name: str, db: DbSession) -> Response:
    if not GUIDE_NAME_RE.match(name or ""):
        raise HTTPException(status_code=404)
    path = guide_path(name)
    if path is None and name not in GUIDE_TITLES:
        raise HTTPException(status_code=404)
    body: Markup | None = None
    toc: list[tuple[str, str]] = []
    title = GUIDE_TITLES.get(name, "Guía")
    if path is not None:
        text = path.read_text(encoding="utf-8", errors="replace")
        body = render_markdown(text)
        title = _first_heading(text) or title
        toc = _headings(text)
    return _render(
        request,
        "connections/guide.html",
        page_title=title,
        guide_title=title,
        guide_name=name,
        guide_body=body,
        toc=toc,
        redirect_uris=redirect_uris(_provider_settings(request, db)),
        nav_active="conexiones",
    )


# --------------------------------------------------------------------------------- tiny Markdown renderer

_FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_SAFE_SCHEMES = ("http://", "https://", "mailto:")
_GUIDE_LINK_RE = re.compile(r"^(?:\./|docs/|\.\./)?guia-([a-z0-9-]+)\.md(#[\w-]*)?$")


def _slug(text: str) -> str:
    import unicodedata

    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-") or "seccion"


def _strip_inline(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[`*]|(?<!\w)_|_(?!\w)", "", text).strip()


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) == 1:
            return _strip_inline(m.group(2))
    return None


def _headings(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    in_code = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_code = not in_code
            continue
        m = None if in_code else _HEADING_RE.match(line)
        if m and len(m.group(1)) == 2:
            label = _strip_inline(m.group(2))
            out.append((label, _slug(label)))
    return out


def _safe_href(url: str) -> str | None:
    url = url.strip()
    guide = _GUIDE_LINK_RE.match(url)
    if guide:
        return f"/ayuda/{guide.group(1)}{guide.group(2) or ''}"
    low = url.lower()
    if low.startswith(_SAFE_SCHEMES) or low.startswith("#"):
        return url
    if url.startswith("/") and not url.startswith("//"):
        return url
    return None


def _inline(raw: str) -> str:
    """Inline Markdown applied to *escaped* text: code, links, images (as text), bold, italic, autolinks."""
    codes: list[str] = []

    def stash_code(m: re.Match[str]) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = str(escape(raw))
    text = re.sub(r"`([^`]+)`", stash_code, text)
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)",
        lambda m: f'<span class="md-img">[{m.group(1) or "imagen"}]</span>',
        text,
    )

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        href = _safe_href(url.replace("&amp;", "&"))
        if href is None:
            return label
        external = href.startswith(("http://", "https://"))
        attrs = ' target="_blank" rel="noopener noreferrer"' if external else ""
        return f'<a href="{escape(href)}"{attrs}>{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)

    def autolink(m: re.Match[str]) -> str:
        url = m.group(1).replace("&amp;", "&")
        return f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(url)}</a>'

    text = re.sub(r"&lt;(https?://[^\s<>]+?)&gt;", autolink, text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?![\s*])(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_(?![\s_])(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _indent(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def _render_list(lines: list[str]) -> str:
    first = _LIST_RE.match(lines[0])
    assert first is not None
    base = _indent(lines[0])
    ordered = first.group(2)[0].isdigit()
    items: list[list[str]] = []
    for line in lines:
        m = _LIST_RE.match(line)
        if m and _indent(line) <= base:
            items.append([m.group(3)])
        elif items:
            expanded = line.expandtabs(4)
            items[-1].append(expanded[min(base + 2, _indent(line)) :] if line.strip() else "")
    parts = []
    for item in items:
        head, rest = item[0], item[1:]
        nested = next((i for i, ln in enumerate(rest) if _LIST_RE.match(ln) or _FENCE_RE.match(ln)), None)
        text_lines = [head] + (rest if nested is None else rest[:nested])
        body = _inline(" ".join(ln.strip() for ln in text_lines if ln.strip()))
        if nested is not None:
            body += _render_blocks(rest[nested:])
        parts.append(f"<li>{body}</li>")
    tag = "ol" if ordered else "ul"
    start = ""
    if ordered:
        number = int(re.match(r"\d+", first.group(2)).group(0))  # type: ignore[union-attr]
        if number != 1:
            start = f' start="{number}"'
    return f"<{tag}{start}>" + "".join(parts) + f"</{tag}>"


def _split_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _collect_list(lines: list[str], i: int) -> tuple[list[str], int]:
    """Lines of the list starting at ``i`` (items, their indented continuations and inner blank lines)."""
    block: list[str] = []
    n = len(lines)
    first = _LIST_RE.match(lines[i])
    base = _indent(lines[i])
    ordered = bool(first and first.group(2)[0].isdigit())
    while i < n:
        current = lines[i]
        item = _LIST_RE.match(current)
        if block and item and _indent(current) <= base and item.group(2)[0].isdigit() != ordered:
            break  # a list of the other kind starts
        if not current.strip():
            nxt = lines[i + 1] if i + 1 < n else ""
            if not nxt.strip() or not (nxt.startswith((" ", "\t")) or _LIST_RE.match(nxt)):
                break
        elif not (_LIST_RE.match(current) or current.startswith((" ", "\t"))):
            if not block or not block[-1].strip():
                break
            # lazy continuation of the previous item's paragraph
        block.append(current)
        i += 1
    return block, i


def _render_blocks(lines: list[str]) -> str:
    out: list[str] = []
    i, n = 0, len(lines)
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]
        if not line.strip():
            flush_para()
            i += 1
            continue
        fence = _FENCE_RE.match(line)
        if fence:
            flush_para()
            j = i + 1
            code: list[str] = []
            while j < n and not lines[j].strip().startswith(fence.group(1)):
                code.append(lines[j])
                j += 1
            out.append(f"<pre><code>{escape(chr(10).join(code))}</code></pre>")
            i = j + 1
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            level = len(heading.group(1))
            label = heading.group(2)
            out.append(f'<h{level} id="{_slug(_strip_inline(label))}">{_inline(label)}</h{level}>')
            i += 1
            continue
        if _HR_RE.match(line) and not para:
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            flush_para()
            quote: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{_render_blocks(quote)}</blockquote>")
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            header = _split_row(line)
            rows: list[list[str]] = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body_html = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(
                f'<div class="table-wrap"><table class="table"><thead><tr>{head_html}</tr></thead>'
                f"<tbody>{body_html}</tbody></table></div>"
            )
            continue
        if _LIST_RE.match(line) and not para:
            block, i = _collect_list(lines, i)
            out.append(_render_list(block))
            continue
        para.append(line)
        i += 1
    flush_para()
    return "".join(out)


def render_markdown(text: str) -> Markup:
    """Small, safe Markdown subset → HTML. All text is escaped first; links only to http(s), mailto, local
    paths and anchors (``guia-x.md`` → ``/ayuda/x``); images become their alt text."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return Markup(_render_blocks(lines))

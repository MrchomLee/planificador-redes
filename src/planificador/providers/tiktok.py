"""TikTok provider: Login Kit (OAuth v2 + PKCE), Content Posting API and Display API analytics.

Contract: SPEC §5 (provider interface), §6 (module-level OAuth helpers), §7 (TikTok options).
Research: ``docs/research/03-tiktok-api.md`` (its §14 "Verificación" overrides the body) and
``docs/research/04-arquitectura-local.md`` §12 (the redirect host must be ``localhost``).

Publishing (:meth:`TikTokProvider.publish_step`) is a re-entrant state machine kept in ``ctx.state``:

``new`` → (direct mode: fresh ``creator_info`` + post settings check) → ``ctx.before_commit`` →
``video/init`` (direct) or ``inbox/video/init`` (inbox) with ``source=FILE_UPLOAD`` → ``publish_id`` saved
immediately → ``uploading`` (one chunk per step: ``PUT upload_url`` with ``Content-Range``; state saved after
every chunk) → ``processing`` (``status/fetch`` polled through ``wait`` results) → ``done``
(``published`` / ``sent_to_app``).

Safety rules (never publish twice):

- ``init`` is the commit point (SPEC §5) and is never re-sent while a previous ``publish_id`` might still go
  live. With ``FILE_UPLOAD`` nothing can go live until TikTok has received **every** byte, so an init whose
  upload never reached the final chunk may be abandoned and redone (expired upload URL, crash before the
  ``publish_id`` was saved…). Once the final chunk may have arrived, only ``status/fetch`` decides.
- Re-sending a chunk ``PUT`` is always safe: same ``publish_id``, same byte range.
- A ``FAILED`` status is definitive: the state is reset so a later retry starts with a fresh init.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from planificador.providers.base import (
    FOLLOWUP_KEY,
    AccountCreds,
    AccountSnapshot,
    ErrorKind,
    FollowUpResult,
    FormatSpec,
    Issue,
    MediaInfo,
    OptionSpec,
    PostMetricData,
    Provider,
    ProviderError,
    ProviderSettings,
    PublishContext,
    ReconcileResult,
    StepResult,
    TargetSpec,
    TokenUpdate,
    scrub_text,
)
from planificador.timeutil import Clock

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------ endpoints

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_BASE = "https://open.tiktokapis.com"
TOKEN_URL = f"{API_BASE}/v2/oauth/token/"
REVOKE_URL = f"{API_BASE}/v2/oauth/revoke/"
USER_INFO_URL = f"{API_BASE}/v2/user/info/"
CREATOR_INFO_URL = f"{API_BASE}/v2/post/publish/creator_info/query/"
VIDEO_INIT_URL = f"{API_BASE}/v2/post/publish/video/init/"
INBOX_INIT_URL = f"{API_BASE}/v2/post/publish/inbox/video/init/"
STATUS_URL = f"{API_BASE}/v2/post/publish/status/fetch/"
VIDEO_LIST_URL = f"{API_BASE}/v2/video/list/"

#: Callback path registered in the user's TikTok app (no "tiktok" in it, report 03 §1.3).
CALLBACK_PATH = "/conexiones/callback/social1"

# --------------------------------------------------------------------------------------------- scopes

SCOPE_BASIC = "user.info.basic"  # Login Kit identity: always requested
SCOPE_DIRECT = "video.publish"  # Content Posting API, Direct Post
SCOPE_INBOX = "video.upload"  # Content Posting API, upload as a draft to the TikTok app (inbox)
#: Publishing mode → the scope it needs.
MODE_SCOPES: dict[str, str] = {"direct": SCOPE_DIRECT, "inbox": SCOPE_INBOX}
PUBLISH_SCOPES: frozenset[str] = frozenset(MODE_SCOPES.values())

#: Login Kit identity + Content Posting (Direct Post and inbox upload). Report 03 §1.1.
SCOPES_PUBLISH: tuple[str, ...] = (SCOPE_BASIC, SCOPE_DIRECT, SCOPE_INBOX)
#: Optional Display API scopes (may not be offered to new apps, report 03 §1.1 / §14 #21).
SCOPES_ANALYTICS: tuple[str, ...] = ("user.info.profile", "user.info.stats", "video.list")
#: Every scope the app knows, in the order the settings page lists them. TikTok rejects the whole authorize
#: request (``error_type=scope``) when it asks for a scope the developer app does not have — sandbox apps
#: often lack ``video.publish`` or the Display API ones — so the user ticks exactly the scopes of their app
#: (setting ``tiktok_scopes``) and only those are requested.
KNOWN_SCOPES: tuple[str, ...] = SCOPES_PUBLISH + SCOPES_ANALYTICS
#: ``tiktok_scopes`` for a new install.
DEFAULT_SCOPES: tuple[str, ...] = (SCOPE_BASIC, SCOPE_INBOX, SCOPE_DIRECT)
SCOPE_ERROR_CODES = frozenset({"scope_not_authorized", "scope_permission_missed"})

# ------------------------------------------------------------------------------------------- limits

TOKEN_REFRESH_MARGIN = timedelta(hours=1)  # refresh when less than 1 h is left (SPEC §10)
DEFAULT_ACCESS_TTL = 86_400  # 24 h (report 03 §2.5)
CAPTION_MAX_UTF16 = 2200  # report 03 §5.1 / §14 #12
SINGLE_CHUNK_MAX = 64_000_000  # ≤ this → one chunk (decimal MB satisfies both MB readings, report 03 §5.2)
CHUNK_SIZE = 10_000_000  # otherwise 10 MB chunks with the remainder merged into the last one
MAX_CHUNKS = 1000
UPLOAD_BLOCK = 1 << 20  # bytes read from disk at a time while streaming a chunk
DURATION_TOLERANCE_S = 0.5
VIDEO_LIST_PAGE = 20
VIDEO_LIST_MAX_PAGES = 15

API_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
UPLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=300.0, pool=30.0)

# Polling cadence (report 03 §11.4): 5, 10, 20, 30 s, then every 60 s for 30 min, then every 15 min, 24 h max.
POLL_DELAYS: tuple[int, ...] = (5, 10, 20, 30)
POLL_INTERVAL = 60
POLL_FAST_WINDOW = timedelta(minutes=30)
POLL_SLOW_INTERVAL = 900
POLL_GIVE_UP = timedelta(hours=24)
POLL_ERROR_LIMIT = 8
PUBLIC_ID_WAITS = 4  # public posts get their id after moderation (~1 min): wait a little for it
PUBLIC_ID_WAIT_SECONDS = 30
#: Moderation can take hours (report 03 §5.5): a public post finished without its id keeps being resolved by
#: the follow-up job (report 03 §11.4) — hourly for 24 h, then daily up to 30 days.
PUBLIC_ID_HOURLY_FOR = timedelta(hours=24)
PUBLIC_ID_GIVE_UP = timedelta(days=30)
CHUNK_RETRY_DELAYS: tuple[int, ...] = (5, 15, 30, 60)
UPLOAD_RESTART_LIMIT = 2
RECONCILE_DELAYS: tuple[int, ...] = (5, 10, 15)  # short blocking re-checks while TikTok is processing
DAILY_CAP_RETRY_SECONDS = 4 * 3600
DEFAULT_RATE_RETRY_SECONDS = 60

_sleep = time.sleep  # patched in tests

# ------------------------------------------------------------------------------------------- labels

#: Spanish labels for ``privacy_level`` values (layout doc 05 §5b).
PRIVACY_LABELS: dict[str, str] = {
    "PUBLIC_TO_EVERYONE": "Público",
    "MUTUAL_FOLLOW_FRIENDS": "Amigos mutuos",
    "FOLLOWER_OF_CREATOR": "Seguidores",
    "SELF_ONLY": "Solo yo",
}

STATUS_LABELS: dict[str, str] = {
    "PROCESSING_UPLOAD": "procesando la subida",
    "PROCESSING_DOWNLOAD": "descargando el vídeo",
    "SEND_TO_USER_INBOX": "enviado a la bandeja de entrada de la app",
    "PUBLISH_COMPLETE": "publicado",
    "FAILED": "fallido",
}

MUSIC_USAGE_URL = "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en"
BRANDED_CONTENT_URL = "https://www.tiktok.com/legal/page/global/bc-policy/en"

MSG_NOT_CONFIGURED = (
    "Configura la Client Key y el Client Secret de tu app de TikTok en Ajustes antes de conectar la cuenta."
)
MSG_RECONNECT = (
    "La sesión de TikTok ha caducado o se ha revocado. Vuelve a conectar la cuenta en Cuentas conectadas."
)
MSG_UNAUDITED = (
    "Tu app de TikTok todavía no ha pasado la auditoría de TikTok, así que TikTok solo deja publicar en "
    "privado: elige «Solo yo» en «¿Quién puede ver la publicación?» y comprueba que tu cuenta de TikTok es "
    "privada (en la app: Perfil › Ajustes y privacidad › Privacidad › Cuenta privada). Hasta que la app esté "
    "auditada solo funciona «Solo yo»; después puedes cambiar la visibilidad del vídeo desde la app de "
    "TikTok, o usar «Terminar en la app de TikTok» (borrador)."
)
MSG_SCOPE = (
    "Falta un permiso de TikTok para esta acción. Vuelve a conectar la cuenta aceptando todos los permisos y "
    "comprueba que tu app de TikTok tiene los productos necesarios (Login Kit y Content Posting API)."
)
MSG_PHOTO = (
    "Las publicaciones de fotos en TikTok necesitan un dominio público verificado y no están disponibles en "
    "esta versión. Publica un vídeo o usa la Publicación asistida."
)
MSG_TEMPORARY = "TikTok tiene un problema temporal. Se reintentará."
NOTICE_STATS_SCOPE = (
    "TikTok no ha concedido el permiso de estadísticas de la cuenta (user.info.stats), así que no se pueden "
    "leer seguidores, «me gusta» ni número de vídeos. Si tu app de TikTok tiene ese permiso, márcalo en "
    "Ajustes › Credenciales › TikTok y vuelve a conectar la cuenta."
)
NOTICE_VIDEO_SCOPE = (
    "TikTok no ha concedido el permiso para leer tus vídeos (video.list), así que no hay estadísticas por "
    "publicación. Si tu app de TikTok tiene ese permiso, márcalo en Ajustes › Credenciales › TikTok y vuelve "
    "a conectar la cuenta. TikTok solo da estadísticas de vídeos públicos."
)
#: Why a publishing mode is not available for an account (Spanish, shown by the composer, the rules and the
#: engine). Keyed by the missing mode.
MSG_MODE_UNAVAILABLE: dict[str, str] = {
    "direct": (
        "Tu app de TikTok no tiene el permiso de publicación directa (video.publish), así que esta cuenta "
        "solo puede enviar los vídeos como borrador a la app de TikTok, donde terminas la publicación. Para "
        "publicar directamente, activa «Direct Post» en Content Posting API de tu app de TikTok, marca "
        "video.publish en Ajustes › Credenciales › TikTok y vuelve a conectar la cuenta."
    ),
    "inbox": (
        "Tu app de TikTok no tiene el permiso para enviar borradores (video.upload), así que esta cuenta "
        "solo puede publicar directamente. Para enviar borradores, añade video.upload a tu app de TikTok, "
        "márcalo en Ajustes › Credenciales › TikTok y vuelve a conectar la cuenta."
    ),
}
MSG_NO_PUBLISH_SCOPE = (
    "Esta cuenta de TikTok no tiene ningún permiso de publicación (ni video.publish ni video.upload). Añade "
    "Content Posting API a tu app de TikTok, marca sus permisos en Ajustes › Credenciales › TikTok y vuelve "
    "a conectar la cuenta."
)
MSG_MODE_REFUSED: dict[str, str] = {
    "direct": (
        "TikTok ha rechazado la publicación directa: tu app de TikTok no tiene el permiso video.publish (o "
        "no lo aceptaste al conectar). Usa «Enviar a la app de TikTok como borrador», o activa «Direct Post» "
        "en tu app de TikTok y vuelve a conectar la cuenta."
    ),
    "inbox": (
        "TikTok ha rechazado el envío del borrador: tu app de TikTok no tiene el permiso video.upload (o no "
        "lo aceptaste al conectar). Desactiva «Enviar a la app de TikTok como borrador», o añade "
        "video.upload a tu app de TikTok y vuelve a conectar la cuenta."
    ),
}
_INBOX_RECONCILE_MSG = (
    "El vídeo está en la bandeja de entrada de la app de TikTok: ábrela para terminar la publicación y "
    "después márcala como publicada."
)

# error.code → (kind, Spanish message). Report 03 §8.3 (+ §14 #19 extensions).
_ERRORS: dict[str, tuple[ErrorKind, str]] = {
    "access_token_invalid": (ErrorKind.AUTH, MSG_RECONNECT),
    "scope_not_authorized": (ErrorKind.PERMANENT, MSG_SCOPE),
    "scope_permission_missed": (ErrorKind.PERMANENT, MSG_SCOPE),
    "unaudited_client_can_only_post_to_private_accounts": (ErrorKind.PERMANENT, MSG_UNAUDITED),
    "privacy_level_option_mismatch": (
        ErrorKind.PERMANENT,
        "La visibilidad elegida no está disponible para esta cuenta de TikTok. Edita la publicación y elige "
        "otra opción en «¿Quién puede ver la publicación?».",
    ),
    "invalid_param": (
        ErrorKind.PERMANENT,
        "TikTok ha rechazado los datos de la publicación (parámetro no válido).",
    ),
    "invalid_params": (
        ErrorKind.PERMANENT,
        "TikTok ha rechazado los datos de la petición (parámetro no válido).",
    ),
    "invalid_file_upload": (
        ErrorKind.PERMANENT,
        "TikTok ha rechazado la subida del vídeo: el tamaño o los fragmentos no coinciden.",
    ),
    "url_ownership_unverified": (
        ErrorKind.PERMANENT,
        "TikTok exige que el dominio de los archivos esté verificado en tu app de TikTok.",
    ),
    "spam_risk_too_many_posts": (
        ErrorKind.RATE_LIMITED,
        "Has alcanzado el límite diario de publicaciones de TikTok para esta cuenta. Se reintentará más "
        "tarde.",
    ),
    "spam_risk_too_many_pending_share": (
        ErrorKind.PERMANENT,
        "Tienes demasiados borradores pendientes en la app de TikTok (máximo 5 en 24 horas). Termina o borra "
        "los borradores pendientes en la app de TikTok y vuelve a intentarlo.",
    ),
    "spam_risk_user_banned_from_posting": (
        ErrorKind.PERMANENT,
        "TikTok no permite publicar desde esta cuenta ahora mismo (bloqueo por riesgo de spam). Revisa la "
        "app de TikTok.",
    ),
    "spam_risk_text": (
        ErrorKind.PERMANENT,
        "TikTok ha marcado el texto de la publicación como posible spam. Edítalo y vuelve a intentarlo.",
    ),
    "spam_risk": (ErrorKind.PERMANENT, "TikTok ha rechazado la publicación por riesgo de spam."),
    "reached_active_user_cap": (
        ErrorKind.RATE_LIMITED,
        "Tu app de TikTok (sin auditar) ha alcanzado el máximo de usuarios que pueden publicar en 24 horas. "
        "Se reintentará más tarde.",
    ),
    "app_version_check_failed": (
        ErrorKind.PERMANENT,
        "La app de TikTok de tu móvil está desactualizada. Actualízala y vuelve a intentarlo.",
    ),
    "rate_limit_exceeded": (
        ErrorKind.RATE_LIMITED,
        "TikTok ha limitado las peticiones temporalmente. Se reintentará en unos minutos.",
    ),
    "invalid_publish_id": (
        ErrorKind.PERMANENT,
        "TikTok no reconoce esta publicación. Comprueba en la app de TikTok si se publicó.",
    ),
    "token_not_authorized_for_specified_publish_id": (
        ErrorKind.PERMANENT,
        "Esta publicación se creó con otra sesión de TikTok. Comprueba en la app de TikTok si se publicó.",
    ),
    "internal_error": (ErrorKind.TRANSIENT, MSG_TEMPORARY),
    "server_error": (ErrorKind.TRANSIENT, MSG_TEMPORARY),
    "temporarily_unavailable": (ErrorKind.TRANSIENT, MSG_TEMPORARY),
}
_DAILY_CAP_CODES = frozenset({"spam_risk_too_many_posts", "reached_active_user_cap"})

# status FAILED fail_reason → (kind, Spanish message). Report 03 §5.4, §8.3.
_FAIL_REASONS: dict[str, tuple[ErrorKind, str]] = {
    "file_format_check_failed": (
        ErrorKind.PERMANENT,
        "TikTok no admite el formato del vídeo. Usa MP4 (H.264), MOV o WebM.",
    ),
    "duration_check_failed": (
        ErrorKind.PERMANENT,
        "TikTok ha rechazado la duración del vídeo para esta cuenta (demasiado corto o demasiado largo).",
    ),
    "frame_rate_check_failed": (
        ErrorKind.PERMANENT,
        "TikTok ha rechazado la frecuencia de imagen del vídeo (debe estar entre 23 y 60 fps).",
    ),
    "picture_size_check_failed": (
        ErrorKind.PERMANENT,
        "TikTok ha rechazado la resolución del vídeo (mínimo 360 px y máximo 4096 px por lado).",
    ),
    "video_pull_failed": (ErrorKind.PERMANENT, "TikTok no pudo descargar el vídeo."),
    "photo_pull_failed": (ErrorKind.PERMANENT, "TikTok no pudo descargar las fotos."),
    "publish_cancelled": (ErrorKind.PERMANENT, "La publicación se canceló en TikTok."),
    "auth_removed": (
        ErrorKind.AUTH,
        "Se retiró el permiso de la app en TikTok durante la publicación. Vuelve a conectar la cuenta.",
    ),
    "internal": (ErrorKind.TRANSIENT, "TikTok tuvo un error interno al procesar el vídeo. Se reintentará."),
}


# --------------------------------------------------------------------------------------- data types


@dataclass
class TikTokTokens:
    open_id: str
    access_token: str
    expires_at: datetime | None
    refresh_token: str | None
    refresh_expires_at: datetime | None
    scopes: list[str] = field(default_factory=list)


@dataclass
class CreatorInfo:
    nickname: str | None
    username: str | None
    avatar_url: str | None
    privacy_level_options: list[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int | None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> CreatorInfo:
        options = data.get("privacy_level_options") or []
        return cls(
            nickname=_str_or_none(data.get("creator_nickname")),
            username=_str_or_none(data.get("creator_username")),
            avatar_url=_str_or_none(data.get("creator_avatar_url")),
            privacy_level_options=[str(o) for o in options if o],
            comment_disabled=bool(data.get("comment_disabled")),
            duet_disabled=bool(data.get("duet_disabled")),
            stitch_disabled=bool(data.get("stitch_disabled")),
            max_video_post_duration_sec=_int_or_none(data.get("max_video_post_duration_sec")),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (e.g. to cache in ``accounts.meta["creator_info"]``)."""
        return {
            "nickname": self.nickname,
            "username": self.username,
            "avatar_url": self.avatar_url,
            "privacy_level_options": list(self.privacy_level_options),
            "comment_disabled": self.comment_disabled,
            "duet_disabled": self.duet_disabled,
            "stitch_disabled": self.stitch_disabled,
            "max_video_post_duration_sec": self.max_video_post_duration_sec,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CreatorInfo:
        return cls(
            nickname=data.get("nickname"),
            username=data.get("username"),
            avatar_url=data.get("avatar_url"),
            privacy_level_options=list(data.get("privacy_level_options") or []),
            comment_disabled=bool(data.get("comment_disabled")),
            duet_disabled=bool(data.get("duet_disabled")),
            stitch_disabled=bool(data.get("stitch_disabled")),
            max_video_post_duration_sec=_int_or_none(data.get("max_video_post_duration_sec")),
        )

    @property
    def is_unaudited(self) -> bool:
        """TikTok only offers ``SELF_ONLY`` to unaudited client apps (report 03 §4.3)."""
        return self.privacy_level_options == ["SELF_ONLY"]

    def privacy_choices(self) -> list[tuple[str, str]]:
        """``(value, Spanish label)`` pairs in TikTok's order, for the composer's privacy select."""
        return [(o, PRIVACY_LABELS.get(o, o)) for o in self.privacy_level_options]


@dataclass
class TikTokAccountSnapshot(AccountSnapshot):
    """An :class:`AccountSnapshot` that may carry a Spanish ``notice`` (e.g. missing analytics scope)."""

    notice: str | None = None


class TikTokPostList(list):
    """``list[PostMetricData]`` with an optional Spanish ``notice`` (e.g. missing ``video.list`` scope)."""

    def __init__(self, items: Any = (), notice: str | None = None) -> None:
        super().__init__(items)
        self.notice = notice


@dataclass(frozen=True)
class ChunkPlan:
    """FILE_UPLOAD chunking (report 03 §5.2): ``total_chunks = floor(size / chunk_size)`` and the remainder
    goes into the last chunk."""

    video_size: int
    chunk_size: int
    total_chunks: int

    def chunk_range(self, index: int) -> tuple[int, int]:
        """Inclusive ``(first_byte, last_byte)`` of chunk ``index`` (0-based)."""
        if not 0 <= index < self.total_chunks:
            raise IndexError(index)
        first = index * self.chunk_size
        last = self.video_size - 1 if index == self.total_chunks - 1 else first + self.chunk_size - 1
        return first, last

    def chunk_length(self, index: int) -> int:
        first, last = self.chunk_range(index)
        return last - first + 1


def plan_chunks(video_size: int) -> ChunkPlan:
    """One chunk if ``video_size <= 64,000,000`` bytes, else 10,000,000-byte chunks with the remainder merged
    into the last one (< 20 MB). ``ValueError`` if empty or more than 1000 chunks would be needed."""
    size = int(video_size)
    if size <= 0:
        raise ValueError("video_size must be positive")
    if size <= SINGLE_CHUNK_MAX:
        return ChunkPlan(size, size, 1)
    total = size // CHUNK_SIZE
    if total > MAX_CHUNKS:
        raise ValueError(f"video too large: {total} chunks > {MAX_CHUNKS}")
    return ChunkPlan(size, CHUNK_SIZE, total)


# ----------------------------------------------------------------------------------------- helpers


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_TRUE = {"1", "true", "yes", "si", "sí", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def utf16_len(text: str | None) -> int:
    """Caption length as TikTok counts it: UTF-16 code units (SPEC §7)."""
    return len((text or "").encode("utf-16-le")) // 2


def publish_mode(options: Mapping[str, Any] | None) -> str:
    """``"inbox"`` (send as a draft to the TikTok app) or ``"direct"`` (default)."""
    mode = str((options or {}).get("mode") or "direct").strip().lower()
    return "inbox" if mode == "inbox" else "direct"


def post_url(username: str | None, post_id: str | None) -> str | None:
    if not username or not post_id:
        return None
    return f"https://www.tiktok.com/@{username.lstrip('@')}/video/{post_id}"


def profile_url(username: str | None) -> str | None:
    return f"https://www.tiktok.com/@{username.lstrip('@')}" if username else None


def _now(clock: Clock | None) -> datetime:
    return clock.now() if clock is not None else datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _retry_after(headers: Mapping[str, str] | None) -> int | None:
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return None


def _require_client(settings: ProviderSettings, *, secret: bool = True) -> tuple[str, str]:
    key = (settings.tiktok_client_key or "").strip()
    sec = (settings.tiktok_client_secret or "").strip()
    if not key or (secret and not sec):
        raise ProviderError(ErrorKind.PERMANENT, "tiktok_not_configured", MSG_NOT_CONFIGURED)
    return key, sec


def _require_token(account: AccountCreds) -> str:
    if not account.access_token:
        raise ProviderError(ErrorKind.AUTH, "tiktok_no_token", MSG_RECONNECT)
    return account.access_token


def granted_scopes(account: Any) -> set[str] | None:
    """Granted scopes when known — the ``scopes`` attribute (``AccountCreds`` or an ``Account`` row) or
    ``meta["scopes"]`` / ``meta["granted_scopes"]`` — else ``None`` (unknown, e.g. an account connected before
    scopes were stored: just try the API)."""
    raw: Any = getattr(account, "scopes", None) or None
    if raw is None:
        meta = getattr(account, "meta", None) or {}
        raw = meta.get("scopes", meta.get("granted_scopes")) or None
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.replace(" ", ",").split(",")
    scopes = {str(s).strip() for s in raw if str(s).strip()}
    return scopes or None


_granted_scopes = granted_scopes  # internal alias


def available_modes(granted: Any) -> tuple[str, ...]:
    """Publishing modes the granted scopes allow, in UI order: ``"direct"`` needs ``video.publish``,
    ``"inbox"`` needs ``video.upload``. ``None`` / empty (unknown grants) allows both — TikTok decides."""
    if not granted:
        return tuple(MODE_SCOPES)
    scopes = {str(s).strip() for s in granted}
    return tuple(mode for mode, scope in MODE_SCOPES.items() if scope in scopes)


def account_modes(account: Any) -> tuple[str, ...]:
    """:func:`available_modes` for an ``Account`` row or :class:`AccountCreds`."""
    return available_modes(granted_scopes(account))


def mode_unavailable_message(mode: str, granted: Any) -> str:
    """Spanish explanation of why ``mode`` cannot be used with these grants."""
    if not available_modes(granted):
        return MSG_NO_PUBLISH_SCOPE
    return MSG_MODE_UNAVAILABLE.get(mode, MSG_NO_PUBLISH_SCOPE)


def normalize_scopes(items: Any, *, known_only: bool = False) -> list[str]:
    """Scopes from a list or a comma/space separated string: known ones in :data:`KNOWN_SCOPES` order with
    ``user.info.basic`` always included, then (unless ``known_only``) any other scope in the given order."""
    if items is None:
        items = []
    elif isinstance(items, str):
        items = items.replace(" ", ",").split(",")
    given = [str(s).strip() for s in items if str(s).strip()]
    chosen = set(given) | {SCOPE_BASIC}
    out = [s for s in KNOWN_SCOPES if s in chosen]
    if not known_only:
        out.extend(dict.fromkeys(s for s in given if s not in KNOWN_SCOPES))
    return out


def _transport_error(exc: httpx.HTTPError, op: str) -> ProviderError:
    timeout = isinstance(exc, httpx.TimeoutException)
    return ProviderError(
        ErrorKind.TRANSIENT,
        "tiktok_timeout" if timeout else "tiktok_network",
        "TikTok no respondió a tiempo. Se reintentará."
        if timeout
        else "No se pudo conectar con TikTok. Se reintentará.",
        detail={"op": op, "error": type(exc).__name__, "info": scrub_text(str(exc))[:300]},
    )


# ------------------------------------------------------------------------------- error classification


def classify_tiktok_error(
    status_code: int, body: Any, headers: Mapping[str, str] | None = None, *, op: str = ""
) -> ProviderError:
    """Map a TikTok v2 API error (``{"error": {"code", "message", "log_id"}}``, whatever the HTTP status) to a
    :class:`ProviderError` with a Spanish message (report 03 §8)."""
    err: Any = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code = str(err.get("code") or "").strip()
        api_message = str(err.get("message") or "")
        log_id = err.get("log_id")
    else:  # OAuth-style flat errors
        code = str(err or "").strip()
        api_message = str(body.get("error_description") or "") if isinstance(body, dict) else ""
        log_id = body.get("log_id") if isinstance(body, dict) else None
    if code == "ok":
        code = ""
    detail = {
        "op": op,
        "http_status": status_code,
        "error_code": code or None,
        "error_message": api_message[:500] or None,
        "log_id": log_id,
    }
    retry_after = _retry_after(headers)

    if code in _ERRORS:
        kind, message = _ERRORS[code]
        if code in _DAILY_CAP_CODES:
            retry_after = retry_after or DAILY_CAP_RETRY_SECONDS
        elif kind == ErrorKind.RATE_LIMITED:
            retry_after = retry_after or DEFAULT_RATE_RETRY_SECONDS
        return ProviderError(kind, code, message, detail=detail, retry_after=retry_after)
    if status_code == 429:
        return ProviderError(
            ErrorKind.RATE_LIMITED,
            code or "rate_limit_exceeded",
            _ERRORS["rate_limit_exceeded"][1],
            detail=detail,
            retry_after=retry_after or DEFAULT_RATE_RETRY_SECONDS,
        )
    if status_code >= 500 or status_code == 408:
        return ProviderError(
            ErrorKind.TRANSIENT,
            code or f"http_{status_code}",
            MSG_TEMPORARY,
            detail=detail,
            retry_after=retry_after,
        )
    if status_code == 401 and op in {"user_info", "user_stats", "check_account"}:
        # A 401 on /v2/user/info/ means a dead token; elsewhere a bare 401 may be a scope problem (§8.3 note).
        return ProviderError(ErrorKind.AUTH, code or "http_401", MSG_RECONNECT, detail=detail)
    if status_code in (401, 403):
        return ProviderError(
            ErrorKind.PERMANENT,
            code or f"http_{status_code}",
            "TikTok ha denegado el acceso a esta acción. Revisa los permisos de tu app de TikTok y vuelve a "
            "conectar la cuenta.",
            detail=detail,
        )
    if 200 <= status_code < 300 and not code:
        return ProviderError(
            ErrorKind.TRANSIENT,
            "tiktok_bad_response",
            "TikTok devolvió una respuesta no válida. Se reintentará.",
            detail=detail,
        )
    suffix = f" (código: {code})" if code else f" (HTTP {status_code})"
    return ProviderError(
        ErrorKind.PERMANENT,
        code or f"http_{status_code}",
        f"TikTok ha rechazado la petición{suffix}.",
        detail=detail,
    )


def _mode_refused(err: ProviderError, mode: str) -> ProviderError:
    """A scope refusal while publishing names the missing permission of ``mode``; others pass through."""
    if err.code not in SCOPE_ERROR_CODES:
        return err
    return ProviderError(
        ErrorKind.PERMANENT,
        err.code,
        MSG_MODE_REFUSED.get(mode, MSG_SCOPE),
        detail={**err.detail, "mode": mode},
        retry_after=err.retry_after,
    )


def fail_reason_error(reason: str | None, *, detail: dict | None = None) -> ProviderError:
    """ProviderError for a ``status/fetch`` ``FAILED`` result, with a Spanish message per ``fail_reason``."""
    code = str(reason or "").strip()
    det = dict(detail or {})
    det["fail_reason"] = code or None
    retry_after = None
    if code in _FAIL_REASONS:
        kind, message = _FAIL_REASONS[code]
    elif code in _ERRORS:  # spam_risk_* etc. also appear as fail_reason values (report 03 §14 #19)
        kind, message = _ERRORS[code]
        if kind == ErrorKind.RATE_LIMITED:
            retry_after = DAILY_CAP_RETRY_SECONDS if code in _DAILY_CAP_CODES else DEFAULT_RATE_RETRY_SECONDS
    else:
        kind = ErrorKind.PERMANENT
        message = (
            f"TikTok no pudo publicar el vídeo (motivo: {code})."
            if code
            else "TikTok no pudo publicar el vídeo."
        )
    return ProviderError(kind, code or "tiktok_failed", message, detail=det, retry_after=retry_after)


# ------------------------------------------------------------------------------------- HTTP plumbing


def _api(
    http: httpx.Client,
    method: str,
    url: str,
    token: str,
    *,
    op: str,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> dict[str, Any]:
    """Call a v2 endpoint and return its ``data``. ``error.code`` is always checked, even on HTTP 200."""
    is_get = method.upper() == "GET"
    headers = {"Authorization": f"Bearer {token}"}
    if not is_get:
        headers["Content-Type"] = "application/json; charset=UTF-8"
    try:
        resp = http.request(
            method,
            url,
            params=params,
            json=None if is_get else json_body,
            headers=headers,
            timeout=API_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise _transport_error(exc, op) from None
    body = _json_or_none(resp)
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code") if isinstance(err, dict) else err
    if resp.is_success and isinstance(body, dict) and (not code or code == "ok"):
        data = body.get("data")
        return data if isinstance(data, dict) else {}
    raise classify_tiktok_error(resp.status_code, body, resp.headers, op=op)


def _oauth_post(http: httpx.Client, url: str, data: dict[str, str], *, op: str) -> dict[str, Any]:
    """Form POST to an OAuth endpoint (token / refresh / revoke). Errors are top-level ``error`` strings."""
    try:
        resp = http.post(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
            timeout=API_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise _transport_error(exc, op) from None
    body = _json_or_none(resp)
    if isinstance(body, dict) and isinstance(body.get("data"), dict) and "access_token" in body["data"]:
        body = {**{k: v for k, v in body.items() if k != "data"}, **body["data"]}
    if body is None and resp.is_success and op == "revoke":
        return {}
    err = body.get("error") if isinstance(body, dict) else None
    err_code = err.get("code") if isinstance(err, dict) else err
    if resp.is_success and isinstance(body, dict) and (not err_code or err_code == "ok"):
        return body
    raise _classify_oauth_error(resp.status_code, body, resp.headers, op=op)


def _classify_oauth_error(
    status_code: int, body: Any, headers: Mapping[str, str] | None, *, op: str
) -> ProviderError:
    err = body.get("error") if isinstance(body, dict) else None
    code = str((err.get("code") if isinstance(err, dict) else err) or "").strip()
    description = ""
    if isinstance(body, dict):
        nested = err.get("message") if isinstance(err, dict) else ""
        description = str(body.get("error_description") or nested or "")
    detail = {
        "op": op,
        "http_status": status_code,
        "error_code": code or None,
        "error_message": description[:500] or None,
        "log_id": body.get("log_id") if isinstance(body, dict) else None,
    }
    pkce_hint = (
        " Si el problema continúa, cambia la codificación PKCE (hex / base64url) en Ajustes y vuelve a "
        "intentarlo."
        if op == "exchange"
        else ""
    )
    if code == "invalid_grant":
        if op == "exchange":
            message = (
                "El código de autorización de TikTok ha caducado o no es válido. Vuelve a conectar la cuenta."
                + pkce_hint
            )
        else:
            message = MSG_RECONNECT
        return ProviderError(ErrorKind.AUTH, code, message, detail=detail)
    if code in {"invalid_client", "unauthorized_client"}:
        return ProviderError(
            ErrorKind.AUTH,
            code,
            "TikTok ha rechazado la Client Key o el Client Secret. Revísalos en Ajustes y vuelve a conectar "
            "la cuenta.",
            detail=detail,
        )
    if code == "access_denied":
        return ProviderError(
            ErrorKind.PERMANENT, code, "Has cancelado la autorización en TikTok.", detail=detail
        )
    if code == "invalid_request" and op == "exchange":
        return ProviderError(
            ErrorKind.PERMANENT,
            code,
            "TikTok ha rechazado la petición de conexión (URL de redirección o PKCE). Comprueba que la URL "
            "de redirección registrada en tu app de TikTok es exactamente la que muestra Ajustes."
            f"{pkce_hint}",
            detail=detail,
        )
    if code == "rate_limit_exceeded" or status_code == 429:
        return ProviderError(
            ErrorKind.RATE_LIMITED,
            code or "rate_limit_exceeded",
            _ERRORS["rate_limit_exceeded"][1],
            detail=detail,
            retry_after=_retry_after(headers) or DEFAULT_RATE_RETRY_SECONDS,
        )
    if status_code >= 500 or code in {"server_error", "temporarily_unavailable", "internal_error"}:
        return ProviderError(ErrorKind.TRANSIENT, code or f"http_{status_code}", MSG_TEMPORARY, detail=detail)
    if 200 <= status_code < 300 and not code:
        return ProviderError(
            ErrorKind.TRANSIENT,
            "tiktok_bad_response",
            "TikTok devolvió una respuesta no válida.",
            detail=detail,
        )
    return ProviderError(
        ErrorKind.PERMANENT,
        code or f"http_{status_code}",
        f"TikTok ha rechazado la conexión ({code or f'HTTP {status_code}'})." + pkce_hint,
        detail=detail,
    )


def _tokens_from_payload(payload: Mapping[str, Any], now: datetime) -> TikTokTokens:
    access = _str_or_none(payload.get("access_token"))
    if not access:
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "tiktok_no_access_token",
            "TikTok no devolvió un token de acceso. Inténtalo de nuevo.",
            detail={"keys": sorted(str(k) for k in payload)},
        )
    expires_in = _int_or_none(payload.get("expires_in")) or DEFAULT_ACCESS_TTL
    refresh_in = _int_or_none(payload.get("refresh_expires_in"))
    scope_raw = str(payload.get("scope") or "")
    scopes = [s.strip() for s in scope_raw.replace(" ", ",").split(",") if s.strip()]
    return TikTokTokens(
        open_id=str(payload.get("open_id") or ""),
        access_token=access,
        expires_at=now + timedelta(seconds=expires_in),
        refresh_token=_str_or_none(payload.get("refresh_token")),
        refresh_expires_at=(now + timedelta(seconds=refresh_in)) if refresh_in else None,
        scopes=scopes,
    )


# ------------------------------------------------------------------------------------ OAuth (SPEC §6)


def redirect_uri(settings: ProviderSettings) -> str:
    """``http://localhost:<port>/conexiones/callback/social1``. Loopback IPs are rewritten to ``localhost``
    (one canonical host for the UI and every redirect URI, report 04 §12 #12)."""
    base = (settings.base_url or "http://localhost:8765").strip().rstrip("/")
    parts = urlsplit(base)
    host = parts.hostname or "localhost"
    if host in {"127.0.0.1", "::1", "0.0.0.0"}:
        host = "localhost"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme or "http", netloc, parts.path.rstrip("/") + CALLBACK_PATH, "", ""))


def pkce_challenge(verifier: str, encoding: str = "hex") -> str:
    """S256 challenge. TikTok deviates from RFC 7636 and expects the **hex** SHA-256 (report 03 §2.2);
    ``base64url`` (RFC 7636, unpadded) is the switchable fallback (setting ``tiktok_pkce_encoding``)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    enc = (encoding or "hex").strip().lower()
    if enc == "hex":
        return digest.hex()
    if enc == "base64url":
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    raise ValueError(f"Unknown PKCE encoding: {encoding!r}")


def new_pkce_pair(encoding: str = "hex") -> tuple[str, str]:
    """``(code_verifier, code_challenge)``; the verifier is 86 chars of the RFC 7636 unreserved set."""
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier, encoding)


def login_scopes(
    settings: ProviderSettings | None = None, *, include_analytics: bool | None = None
) -> list[str]:
    """Scopes to request. ``settings.tiktok_scopes`` (list or comma string: the scopes the user ticked in
    Ajustes, see ``config.tiktok_scopes``) wins, always with ``user.info.basic``; otherwise the publishing
    scopes, plus the Display API ones when ``include_analytics`` (default: a legacy
    ``tiktok_analytics_scopes`` attribute of ``settings``, False when absent)."""
    explicit = getattr(settings, "tiktok_scopes", None) if settings is not None else None
    if explicit:
        return normalize_scopes(explicit)
    if include_analytics is None:
        include_analytics = bool(getattr(settings, "tiktok_analytics_scopes", False))
    return list(SCOPES_PUBLISH) + (list(SCOPES_ANALYTICS) if include_analytics else [])


def build_login_url(
    settings: ProviderSettings,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    *,
    scopes: list[str] | None = None,
    disable_auto_auth: bool = True,
) -> str:
    """Authorize URL (``https://www.tiktok.com/v2/auth/authorize/``) with PKCE S256."""
    client_key, _ = _require_client(settings, secret=False)
    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": ",".join(scopes if scopes else login_scopes(settings)),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if disable_auto_auth:
        params["disable_auto_auth"] = "1"  # always show the consent page (lets the user pick another account)
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(
    settings: ProviderSettings,
    http: httpx.Client,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    clock: Clock | None = None,
) -> TikTokTokens:
    """Authorization code → tokens (form POST to ``/v2/oauth/token/``)."""
    client_key, client_secret = _require_client(settings)
    payload = _oauth_post(
        http,
        TOKEN_URL,
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        op="exchange",
    )
    return _tokens_from_payload(payload, _now(clock))


def refresh_access_token(
    settings: ProviderSettings, http: httpx.Client, refresh_token: str, *, clock: Clock | None = None
) -> TikTokTokens:
    """Refresh grant. TikTok may rotate the refresh token: callers must store the returned one."""
    client_key, client_secret = _require_client(settings)
    payload = _oauth_post(
        http,
        TOKEN_URL,
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        op="refresh",
    )
    return _tokens_from_payload(payload, _now(clock))


_USER_FIELDS_BASIC = (
    "open_id",
    "union_id",
    "avatar_url",
    "avatar_url_100",
    "avatar_large_url",
    "display_name",
)
_USER_FIELDS_PROFILE = ("username", "bio_description", "profile_deep_link", "is_verified")


def get_user_info(settings: ProviderSettings, http: httpx.Client, access_token: str) -> dict:
    """Identity of the connected user: ``open_id``, ``union_id``, ``display_name``, ``username`` (may be
    ``None``), ``avatar_url`` (+ ``avatar_url_100``, ``avatar_large_url``), ``bio_description``,
    ``profile_deep_link``, ``is_verified`` and ``profile_available`` (``user.info.profile`` granted).

    Falls back to the always-granted Login Kit fields when the profile scope is missing, then fills
    ``username`` / ``display_name`` from ``creator_info`` (``video.publish`` scope) when possible."""
    profile_available = True
    try:
        data = _api(
            http,
            "GET",
            USER_INFO_URL,
            access_token,
            params={"fields": ",".join(_USER_FIELDS_BASIC + _USER_FIELDS_PROFILE)},
            op="user_info",
        )
    except ProviderError as err:
        if err.code not in SCOPE_ERROR_CODES:
            raise
        profile_available = False
        data = _api(
            http,
            "GET",
            USER_INFO_URL,
            access_token,
            params={"fields": ",".join(_USER_FIELDS_BASIC)},
            op="user_info",
        )
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    out: dict[str, Any] = {k: user.get(k) for k in _USER_FIELDS_BASIC + _USER_FIELDS_PROFILE}
    out["username"] = _str_or_none(user.get("username"))
    out["display_name"] = _str_or_none(user.get("display_name"))
    out["is_verified"] = bool(user.get("is_verified")) if "is_verified" in user else None
    out["profile_available"] = profile_available
    if not out.get("open_id"):
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "tiktok_no_open_id",
            "TikTok no devolvió el identificador de la cuenta. Inténtalo de nuevo.",
            detail={"op": "user_info"},
        )
    if not out["username"]:
        try:
            creator = _api(http, "POST", CREATOR_INFO_URL, access_token, json_body={}, op="creator_info")
            info: CreatorInfo | None = CreatorInfo.from_api(creator)
        except ProviderError:
            info = None
        if info is not None:
            out["username"] = info.username
            out["display_name"] = out["display_name"] or info.nickname
            out["avatar_url"] = out.get("avatar_url") or info.avatar_url
    return out


def query_creator_info(account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> CreatorInfo:
    """``POST /v2/post/publish/creator_info/query/``. Refusals (``spam_risk_too_many_posts``,
    ``reached_active_user_cap``, ``spam_risk_user_banned_from_posting``…) arrive with HTTP 200 and an
    ``error.code``, which is always checked."""
    token = _require_token(account)
    data = _api(http, "POST", CREATOR_INFO_URL, token, json_body={}, op="creator_info")
    return CreatorInfo.from_api(data)


def revoke(
    settings: ProviderSettings, http: httpx.Client, access_token: str, *, strict: bool = False
) -> None:
    """Revoke the token (``POST /v2/oauth/revoke/``). Best effort: errors are logged and swallowed unless
    ``strict=True`` (then a :class:`ProviderError` is raised)."""
    if not access_token:
        return
    try:
        client_key, client_secret = _require_client(settings)
        _oauth_post(
            http,
            REVOKE_URL,
            {"client_key": client_key, "client_secret": client_secret, "token": access_token},
            op="revoke",
        )
    except ProviderError as err:
        if strict:
            raise
        log.warning("TikTok token revoke failed: %s", err.code)


def fetch_status(http: httpx.Client, access_token: str, publish_id: str) -> dict[str, Any]:
    """``POST /v2/post/publish/status/fetch/`` → ``data`` (``status``, ``fail_reason``,
    ``publicaly_available_post_id``, ``uploaded_bytes``…)."""
    return _api(
        http, "POST", STATUS_URL, access_token, json_body={"publish_id": publish_id}, op="status_fetch"
    )


def public_post_id(data: Mapping[str, Any]) -> str | None:
    """First public post id in a status payload (TikTok's field name has a typo; both spellings accepted)."""
    for key in ("publicaly_available_post_id", "publicly_available_post_id"):
        ids = data.get(key)
        values = ids if isinstance(ids, list | tuple) else [ids]
        for value in values:
            if value not in (None, "", 0):
                return str(value)
    return None


# ------------------------------------------------------------------------------ post settings (§7)


def _the_video(target: TargetSpec) -> MediaInfo | None:
    videos = [m for m in target.media if str(m.kind) == "video"]
    return videos[0] if len(target.media) == 1 and len(videos) == 1 else None


def check_post_settings(target: TargetSpec, info: CreatorInfo | None = None) -> list[Issue]:
    """TikTok checks that may need ``creator_info`` (Content Sharing Guidelines, report 03 §4.2).

    ``publish_step`` runs them right before the commit point (any error aborts the publish); the composer can
    use them for live validation of the TikTok panel."""
    issues: list[Issue] = []
    opts = target.options or {}
    if target.format == "photo":
        issues.append(Issue("error", "tiktok_photo_unsupported", MSG_PHOTO, "format"))
        return issues
    video = _the_video(target)
    if video is None:
        issues.append(Issue("error", "tiktok_one_video", "TikTok necesita exactamente un vídeo.", "media"))
    if publish_mode(opts) == "inbox":
        return issues  # caption and settings are chosen by the user in the TikTok app

    length = utf16_len(target.text)
    if length > CAPTION_MAX_UTF16:
        issues.append(
            Issue(
                "error",
                "tiktok_caption_too_long",
                f"El texto para TikTok tiene {length} caracteres y el máximo es {CAPTION_MAX_UTF16}.",
                "text",
            )
        )
    privacy = _str_or_none(opts.get("privacy_level"))
    if privacy is None:
        issues.append(
            Issue(
                "error",
                "tiktok_privacy_required",
                "Elige quién puede ver la publicación en TikTok (no hay valor por defecto).",
                "privacy_level",
            )
        )
    elif privacy not in PRIVACY_LABELS:
        issues.append(
            Issue(
                "error",
                "tiktok_privacy_invalid",
                "La visibilidad elegida para TikTok no es válida.",
                "privacy_level",
            )
        )
    elif info is not None and privacy not in info.privacy_level_options:
        allowed = ", ".join(f"«{label}»" for _, label in info.privacy_choices()) or "ninguna"
        message = (
            f"La visibilidad «{PRIVACY_LABELS[privacy]}» no está disponible para esta cuenta de TikTok "
            f"(opciones: {allowed})."
        )
        if info.is_unaudited:
            message += " " + MSG_UNAUDITED
        issues.append(Issue("error", "tiktok_privacy_not_allowed", message, "privacy_level"))
    if _as_bool(opts.get("brand_content_toggle"), False) and privacy == "SELF_ONLY":
        issues.append(
            Issue(
                "error",
                "tiktok_branded_private",
                "El contenido de marca no se puede publicar como «Solo yo». Elige otra visibilidad o "
                "desactiva «Contenido de marca».",
                "brand_content_toggle",
            )
        )
    if not _as_bool(opts.get("consent"), False):  # a missing consent is not a consent (SPEC §7)
        issues.append(
            Issue(
                "error",
                "tiktok_consent",
                "Para publicar directamente en TikTok tienes que aceptar la Confirmación de uso de música de "
                "TikTok.",
                "consent",
            )
        )
    if (
        info is not None
        and video is not None
        and info.max_video_post_duration_sec
        and video.duration_s is not None
        and video.duration_s - info.max_video_post_duration_sec > DURATION_TOLERANCE_S
    ):
        issues.append(
            Issue(
                "error",
                "tiktok_too_long",
                f"El vídeo dura {round(video.duration_s)} s y esta cuenta de TikTok permite como máximo "
                f"{info.max_video_post_duration_sec} s.",
                "media",
            )
        )
    if (
        info is not None
        and info.is_unaudited
        and not any(i.code == "tiktok_privacy_not_allowed" for i in issues)
    ):
        issues.append(
            Issue(
                "warning",
                "tiktok_unaudited",
                "Tu app de TikTok no está auditada: TikTok solo permite publicar como «Solo yo». Cambia la "
                "visibilidad después en la app de TikTok, o usa «Terminar en la app de TikTok».",
                "privacy_level",
            )
        )
    return issues


def build_post_info(target: TargetSpec, info: CreatorInfo | None) -> dict[str, Any]:
    """``post_info`` for ``/v2/post/publish/video/init/`` from the target options (SPEC §7).

    Interactions stay disabled unless explicitly allowed (TikTok's "unchecked by default" rule) and are always
    disabled when the creator turned them off in the app."""
    opts = target.options or {}
    post_info: dict[str, Any] = {}
    if target.text:
        post_info["title"] = target.text
    post_info["privacy_level"] = _str_or_none(opts.get("privacy_level"))
    post_info["disable_comment"] = _as_bool(opts.get("disable_comment"), True) or bool(
        info and info.comment_disabled
    )
    post_info["disable_duet"] = _as_bool(opts.get("disable_duet"), True) or bool(info and info.duet_disabled)
    post_info["disable_stitch"] = _as_bool(opts.get("disable_stitch"), True) or bool(
        info and info.stitch_disabled
    )
    post_info["is_aigc"] = _as_bool(opts.get("is_aigc"), False)
    post_info["brand_organic_toggle"] = _as_bool(opts.get("brand_organic_toggle"), False)
    post_info["brand_content_toggle"] = _as_bool(opts.get("brand_content_toggle"), False)
    cover = _int_or_none(opts.get("video_cover_timestamp_ms"))
    if cover is not None and cover >= 0:
        video = _the_video(target)
        if video is not None and video.duration_s:
            cover = min(cover, max(0, int(video.duration_s * 1000) - 1))
        post_info["video_cover_timestamp_ms"] = cover
    return post_info


def _video_mime(video: MediaInfo | None) -> str:
    mime = ((video.mime if video else "") or "").lower()
    if mime in {"video/mp4", "video/quicktime", "video/webm"}:
        return mime
    suffix = Path(video.path).suffix.lower() if video else ""
    return {".mov": "video/quicktime", ".webm": "video/webm"}.get(suffix, "video/mp4")


def _iter_file_range(path: Path, offset: int, length: int) -> Iterator[bytes]:
    """Stream ``length`` bytes from ``offset`` (never loads the whole chunk or file in memory)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        remaining = length
        while remaining > 0:
            block = fh.read(min(UPLOAD_BLOCK, remaining))
            if not block:
                raise OSError("unexpected end of file while uploading")
            remaining -= len(block)
            yield block


# --------------------------------------------------------------------------------------- provider

_ACTIVE_KEYS = (
    "publish_id",
    "upload_url",
    "next_chunk",
    "final_chunk_started",
    "final_chunk_uncertain",
    "chunk_retries",
    "uploaded_at",
    "polls",
    "poll_errors",
    "id_waits",
    "last_status",
    "init_at",
    "reassert_commit",
)


class TikTokProvider(Provider):
    network = "tiktok"
    display_name = "TikTok"
    formats = [
        FormatSpec("video", "Vídeo", "video"),
        FormatSpec("photo", "Fotos", "images"),  # listed but disabled in v1 (needs a verified public domain)
    ]
    supports_comments = False
    supports_analytics = True

    option_specs = [
        OptionSpec(
            "mode",
            "Modo de publicación",
            "select",
            "direct",
            choices=[("direct", "Publicación directa"), ("inbox", "Terminar en la app de TikTok (borrador)")],
        ),
        OptionSpec(
            "privacy_level",
            "¿Quién puede ver la publicación?",
            "select",
            None,
            choices=list(PRIVACY_LABELS.items()),
            help="Obligatorio. Las opciones dependen de tu cuenta de TikTok.",
        ),
        OptionSpec("disable_comment", "Desactivar comentarios", "bool", True),
        OptionSpec("disable_duet", "Desactivar dúo", "bool", True, formats=["video"]),
        OptionSpec("disable_stitch", "Desactivar pegar (Stitch)", "bool", True, formats=["video"]),
        OptionSpec("is_aigc", "Contenido generado por IA", "bool", False, formats=["video"]),
        OptionSpec("brand_organic_toggle", "Tu marca", "bool", False),
        OptionSpec("brand_content_toggle", "Contenido de marca", "bool", False),
        OptionSpec(
            "video_cover_timestamp_ms", "Fotograma de portada (ms)", "number", None, formats=["video"]
        ),
        OptionSpec("consent", "Acepto la Confirmación de uso de música de TikTok", "bool", False),
    ]

    # ------------------------------------------------------------------------------- publishing

    def publish_step(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        if st.get("phase") == "done":
            return self._final_result(ctx)
        if not st.get("publish_id"):
            return self._start(ctx)
        if st.get("phase") == "uploading":
            return self._upload_next(ctx)
        return self._poll(ctx)

    # -- step: creator_info + init (the commit point) ------------------------------------------------

    def _start(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        target = ctx.target
        mode = publish_mode(target.options)
        token = _require_token(ctx.account)
        granted = granted_scopes(ctx.account)
        if mode not in available_modes(granted):
            # The composer and the rules already refuse this; it still happens when the grants changed after
            # scheduling (scopes revoked, the account reconnected with fewer permissions).
            raise ProviderError(
                ErrorKind.PERMANENT,
                "tiktok_mode_not_granted",
                mode_unavailable_message(mode, granted),
                detail={"mode": mode, "granted_scopes": sorted(granted or [])},
            )

        errors = [i for i in check_post_settings(target, None) if i.level == "error"]
        if errors:
            raise ProviderError(
                ErrorKind.PERMANENT,
                errors[0].code,
                errors[0].message,
                detail={"issues": [i.code for i in errors]},
            )
        video = _the_video(target)
        if video is None:  # pragma: no cover - guaranteed by check_post_settings
            raise ProviderError(
                ErrorKind.PERMANENT, "tiktok_one_video", "TikTok necesita exactamente un vídeo."
            )
        try:
            size = Path(video.path).stat().st_size
        except OSError:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "tiktok_file_missing",
                "No se encuentra el archivo de vídeo en la biblioteca. Vuelve a subirlo.",
                detail={"media_id": video.id},
            ) from None
        try:
            plan = plan_chunks(size)
        except ValueError:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "tiktok_file_size",
                "El vídeo está vacío o es demasiado grande para subirlo a TikTok.",
                detail={"size_bytes": size},
            ) from None

        info: CreatorInfo | None = None
        post_info: dict[str, Any] | None = None
        if mode == "direct":
            # Fresh creator_info right before posting (Content Sharing Guidelines, report 03 §4.2 / §11.4).
            try:
                info = query_creator_info(ctx.account, ctx.settings, ctx.http)
            except ProviderError as err:
                raise _mode_refused(err, mode) from None
            errors = [i for i in check_post_settings(target, info) if i.level == "error"]
            if errors:
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    errors[0].code,
                    errors[0].message,
                    detail={
                        "issues": [i.code for i in errors],
                        "privacy_options": info.privacy_level_options,
                    },
                )
            post_info = build_post_info(target, info)
            ctx.log(
                "tiktok_creator",
                f"Publicando en TikTok como {info.nickname or info.username or ctx.account.name}.",
                {"privacy_level": post_info.get("privacy_level"), "options": info.privacy_level_options},
            )

        for key in _ACTIVE_KEYS:
            st.pop(key, None)
        st.update(
            phase="init_sent",
            mode=mode,
            media_id=video.id,
            video_size=plan.video_size,
            chunk_size=plan.chunk_size,
            total_chunks=plan.total_chunks,
            privacy_level=(post_info or {}).get("privacy_level"),
            creator_username=(info.username if info else None) or st.get("creator_username"),
            init_attempts=int(st.get("init_attempts", 0)) + 1,
        )
        body: dict[str, Any] = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": plan.video_size,
                "chunk_size": plan.chunk_size,
                "total_chunk_count": plan.total_chunks,
            }
        }
        if post_info is not None:
            body["post_info"] = post_info
        url = INBOX_INIT_URL if mode == "inbox" else VIDEO_INIT_URL

        ctx.before_commit(st)  # --- commit point: once a publish_id exists the post can go live ---
        try:
            data = _api(ctx.http, "POST", url, token, json_body=body, op=f"{mode}_init")
        except ProviderError as err:
            # No publish_id → no bytes uploaded → nothing can go live; a later attempt simply re-inits.
            st["phase"] = "new"
            ctx.save_state(st)
            raise _mode_refused(err, mode) from None
        publish_id = _str_or_none(data.get("publish_id"))
        upload_url = _str_or_none(data.get("upload_url"))
        if not publish_id or not upload_url:
            st["phase"] = "new"
            ctx.save_state(st)
            raise ProviderError(
                ErrorKind.TRANSIENT,
                "tiktok_init_incomplete",
                "TikTok no devolvió los datos de subida. Se reintentará.",
                detail={"op": f"{mode}_init", "has_publish_id": bool(publish_id)},
            )
        st.update(
            phase="uploading",
            publish_id=publish_id,
            upload_url=upload_url,
            next_chunk=0,
            final_chunk_started=False,
            chunk_retries=0,
            init_at=_iso(ctx.clock.now()),
        )
        ctx.save_state(st)  # persist the publish_id at once: it is the only handle status/fetch accepts
        ctx.log(
            "tiktok_init",
            "TikTok ha aceptado la publicación. Subiendo el vídeo…"
            if mode == "direct"
            else "TikTok ha aceptado el borrador. Subiendo el vídeo…",
            {
                "publish_id": publish_id,
                "mode": mode,
                "chunks": plan.total_chunks,
                "size_bytes": plan.video_size,
            },
        )
        return StepResult("continue", message="Subiendo el vídeo a TikTok…")

    # -- step: upload one chunk ----------------------------------------------------------------------

    def _video_for_state(self, ctx: PublishContext) -> MediaInfo | None:
        media_id = ctx.state.get("media_id")
        for m in ctx.target.media:
            if m.id == media_id:
                return m
        return _the_video(ctx.target)

    def _upload_next(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        plan = ChunkPlan(int(st["video_size"]), int(st["chunk_size"]), int(st["total_chunks"]))
        index = int(st.get("next_chunk") or 0)
        if st.pop("reassert_commit", None):
            # A reconcile answered "not_published" for this unfinished upload and the engine left the commit
            # phase; this publish_id may now go live again, so the engine must reconcile (never resume) if
            # the app stops from here on.
            ctx.before_commit(st)
        if index >= plan.total_chunks:
            return self._upload_complete(ctx)
        video = self._video_for_state(ctx)
        path = Path(video.path) if video is not None else None
        try:
            unchanged = path is not None and path.stat().st_size == plan.video_size
        except OSError:
            unchanged = False
        if path is None or not unchanged:
            if st.get("final_chunk_uncertain"):
                return self._upload_complete(
                    ctx
                )  # cannot re-send, but an earlier attempt may have completed it
            self._abandon(ctx, "file_changed")
            raise ProviderError(
                ErrorKind.PERMANENT,
                "tiktok_file_changed",
                "El archivo de vídeo ha cambiado o ya no existe mientras se subía a TikTok. Vuelve a "
                "intentarlo.",
                detail={"media_id": st.get("media_id")},
            )
        is_final = index == plan.total_chunks - 1
        if is_final and not st.get("final_chunk_started"):
            st["final_chunk_started"] = True
            ctx.save_state(st)  # from now on the upload may complete → status/fetch decides, never re-init
        elif is_final and not st.get("final_chunk_uncertain"):
            # The final chunk was already sent once (e.g. the app stopped mid-PUT): TikTok may hold every
            # byte, so a 4xx on this re-send (upload already complete, expired upload_url…) must lead to
            # status/fetch, never to a fresh init — that would post the video twice.
            st["final_chunk_uncertain"] = True
            ctx.save_state(st)
        first, last = plan.chunk_range(index)
        headers = {
            "Content-Type": _video_mime(video),
            "Content-Length": str(last - first + 1),
            "Content-Range": f"bytes {first}-{last}/{plan.video_size}",
        }
        try:
            resp = ctx.http.put(
                str(st["upload_url"]),
                content=_iter_file_range(path, first, last - first + 1),
                headers=headers,
                timeout=UPLOAD_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return self._chunk_failed(ctx, index, is_final, type(exc).__name__)
        except OSError as exc:
            if st.get("final_chunk_uncertain"):
                return self._upload_complete(ctx)
            self._abandon(ctx, "file_read_error")
            raise ProviderError(
                ErrorKind.PERMANENT,
                "tiktok_file_read",
                "No se pudo leer el archivo de vídeo para subirlo a TikTok.",
                detail={"error": type(exc).__name__},
            ) from None

        status = resp.status_code
        if status in (200, 201, 206):
            st["chunk_retries"] = 0
            st["next_chunk"] = index + 1
            if st["next_chunk"] >= plan.total_chunks:
                return self._upload_complete(ctx)
            ctx.save_state(st)
            ctx.log(
                "tiktok_chunk",
                f"Subido el fragmento {index + 1} de {plan.total_chunks} a TikTok.",
                {"chunk": index + 1, "total": plan.total_chunks},
            )
            return StepResult("continue", message=f"Subiendo a TikTok ({index + 1}/{plan.total_chunks})…")
        if status in (408, 429) or status >= 500:
            return self._chunk_failed(ctx, index, is_final, f"HTTP {status}")

        # Other 4xx: the upload session was rejected (expired upload_url, bad range…).
        detail = {"op": "upload_chunk", "http_status": status, "chunk": index + 1, "total": plan.total_chunks}
        if st.get("final_chunk_uncertain"):
            # An earlier attempt of the final chunk may have completed the upload: let status/fetch decide.
            ctx.log("tiktok_upload_check", "Comprobando en TikTok si la subida se completó…", detail)
            return self._upload_complete(ctx)
        restarts = int(st.get("upload_restarts", 0))
        self._abandon(ctx, f"upload_http_{status}")  # incomplete upload: this publish_id can never go live
        if restarts < UPLOAD_RESTART_LIMIT:
            st["upload_restarts"] = restarts + 1
            ctx.save_state(st)
            ctx.log(
                "tiktok_upload_restart",
                "TikTok rechazó la subida (el enlace de subida puede haber caducado). Se reinicia la subida "
                "desde el principio.",
                detail,
            )
            return StepResult("continue", message="Reiniciando la subida a TikTok…")
        st["upload_restarts"] = 0  # a later manual retry gets its own restarts
        ctx.save_state(st)
        raise ProviderError(
            ErrorKind.PERMANENT,
            "tiktok_upload_rejected",
            "TikTok ha rechazado la subida del vídeo varias veces. Vuelve a intentarlo más tarde.",
            detail=detail,
        )

    def _chunk_failed(self, ctx: PublishContext, index: int, is_final: bool, why: str) -> StepResult:
        """Network error / 5xx / 429 while sending a chunk: re-sending the same range is always safe."""
        st = ctx.state
        attempt = int(st.get("chunk_retries", 0)) + 1
        st["chunk_retries"] = attempt
        if is_final:
            st["final_chunk_uncertain"] = True
        detail = {"chunk": index + 1, "attempt": attempt, "error": why}
        if attempt <= len(CHUNK_RETRY_DELAYS):
            ctx.save_state(st)
            ctx.log(
                "tiktok_upload_retry",
                f"Fallo de conexión al subir el fragmento {index + 1} a TikTok; se reintentará.",
                detail,
            )
            return StepResult(
                "wait",
                wait_seconds=CHUNK_RETRY_DELAYS[attempt - 1],
                message="Reintentando la subida a TikTok…",
            )
        if is_final:
            return self._upload_complete(ctx)  # the final chunk may have arrived after all: ask TikTok
        st["chunk_retries"] = 0
        ctx.save_state(st)
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "tiktok_upload_network",
            "No se pudo subir el vídeo a TikTok por un problema de conexión. Se reintentará.",
            detail=detail,
        )

    def _upload_complete(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        st.update(
            phase="processing", uploaded_at=_iso(ctx.clock.now()), polls=0, poll_errors=0, chunk_retries=0
        )
        ctx.save_state(st)
        ctx.log(
            "tiktok_uploaded",
            "Vídeo subido a TikTok. Esperando a que TikTok lo procese…",
            {"publish_id": st.get("publish_id")},
        )
        return StepResult("wait", wait_seconds=POLL_DELAYS[0], message="TikTok está procesando el vídeo…")

    # -- step: poll status/fetch ---------------------------------------------------------------------

    def _poll(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        publish_id = str(st["publish_id"])
        try:
            data = fetch_status(ctx.http, _require_token(ctx.account), publish_id)
        except ProviderError as err:
            return self._poll_failed(ctx, err)
        st["poll_errors"] = 0
        st["polls"] = int(st.get("polls", 0)) + 1
        status = str(data.get("status") or "").strip().upper()
        if status and status != st.get("last_status"):
            ctx.log(
                "tiktok_status",
                f"Estado en TikTok: {STATUS_LABELS.get(status, status)}.",
                {"status": status, "publish_id": publish_id},
            )
        st["last_status"] = status

        if status == "PUBLISH_COMPLETE":
            post_id = public_post_id(data)
            waits = int(st.get("id_waits", 0))
            if (
                post_id is None
                and st.get("mode") == "direct"
                and st.get("privacy_level") == "PUBLIC_TO_EVERYONE"
                and waits < PUBLIC_ID_WAITS
            ):
                st["id_waits"] = waits + 1
                ctx.save_state(st)
                return StepResult(
                    "wait",
                    wait_seconds=PUBLIC_ID_WAIT_SECONDS,
                    message="Publicado en TikTok; esperando el enlace público…",
                )
            return self._finish(ctx, "published", post_id)
        if status == "SEND_TO_USER_INBOX":
            return self._finish(ctx, "sent_to_app", None)
        if status == "FAILED":
            reason = _str_or_none(data.get("fail_reason"))
            err = fail_reason_error(reason, detail={"publish_id": publish_id, "op": "status_fetch"})
            self._abandon(ctx, f"failed:{reason or ''}")
            raise err
        return self._wait_more(ctx)  # PROCESSING_UPLOAD / PROCESSING_DOWNLOAD / anything new

    def _poll_failed(self, ctx: PublishContext, err: ProviderError) -> StepResult:
        st = ctx.state
        attempt = int(st.get("poll_errors", 0)) + 1
        st["poll_errors"] = attempt
        ctx.save_state(st)
        retryable = err.kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED, ErrorKind.AUTH)
        if retryable and attempt <= POLL_ERROR_LIMIT:
            ctx.log(
                "tiktok_status_retry",
                "No se pudo consultar el estado en TikTok; se volverá a intentar.",
                {"code": err.code, "attempt": attempt},
            )
            delay = max(err.retry_after or 0, min(300, 15 * attempt))
            return StepResult("wait", wait_seconds=delay, message="Comprobando el estado en TikTok…")
        raise ProviderError(
            ErrorKind.AMBIGUOUS,
            err.code,
            f"No se pudo confirmar en TikTok si el vídeo se publicó: {err.message} Revisa tu perfil de "
            "TikTok.",
            detail={**err.detail, "publish_id": st.get("publish_id")},
        )

    def _wait_more(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        now = ctx.clock.now()
        elapsed = now - (_parse_iso(st.get("uploaded_at")) or now)
        ctx.save_state(st)
        if elapsed >= POLL_GIVE_UP:
            raise ProviderError(
                ErrorKind.AMBIGUOUS,
                "tiktok_status_timeout",
                "TikTok sigue procesando el vídeo después de 24 horas. Revisa tu perfil de TikTok para saber "
                "si se publicó.",
                detail={"publish_id": st.get("publish_id"), "status": st.get("last_status")},
            )
        polls = int(st.get("polls", 0))
        if polls < len(POLL_DELAYS):
            delay = POLL_DELAYS[polls]
        elif elapsed < POLL_FAST_WINDOW:
            delay = POLL_INTERVAL
        else:
            delay = POLL_SLOW_INTERVAL
        return StepResult("wait", wait_seconds=delay, message="TikTok está procesando el vídeo…")

    # -- terminal states -----------------------------------------------------------------------------

    def _finish(self, ctx: PublishContext, result: str, post_id: str | None) -> StepResult:
        st = ctx.state
        username = (
            st.get("creator_username")
            or getattr(ctx.account, "username", None)  # if AccountCreds gains a username field
            or (ctx.account.meta or {}).get("username")
        )
        if result == "published":
            url = post_url(username, post_id) if post_id else profile_url(username)
        else:
            url = None
        now = ctx.clock.now()
        st.update(
            phase="done",
            result=result,
            public_post_id=post_id,
            external_id=post_id or str(st["publish_id"]),
            external_url=url,
            finished_at=_iso(now),
        )
        if (
            result == "published"
            and post_id is None
            and st.get("mode") == "direct"
            and st.get("privacy_level") == "PUBLIC_TO_EVERYONE"
        ):
            # Still under moderation: resolve the public id later (per-post analytics and the video link).
            st[FOLLOWUP_KEY] = {"kind": "public_id", "next_at": _iso(now + timedelta(hours=1))}
        ctx.save_state(st)
        return self._final_result(ctx)

    # -- follow-up: public post id after a slow moderation ------------------------------------------

    def follow_up(self, ctx: PublishContext) -> FollowUpResult | None:
        """``status/fetch`` again for a public post published without ``publicaly_available_post_id``
        (report 03 §5.5/§11.4): when it appears, the target gets the real post id and video URL."""
        st = ctx.state
        info = st.get(FOLLOWUP_KEY) or {}
        publish_id = st.get("publish_id")
        if info.get("kind") != "public_id" or not publish_id or st.get("public_post_id"):
            return None
        now = ctx.clock.now()
        started = _parse_iso(st.get("finished_at")) or now
        age = now - started
        if age >= PUBLIC_ID_GIVE_UP:
            return None
        next_at = now + (timedelta(hours=1) if age < PUBLIC_ID_HOURLY_FOR else timedelta(days=1))
        try:
            data = fetch_status(ctx.http, _require_token(ctx.account), str(publish_id))
        except ProviderError as err:
            log.info("TikTok public id lookup failed: %s", err.code)
            return FollowUpResult(next_at=next_at)
        post_id = public_post_id(data)
        if post_id is None:
            return FollowUpResult(next_at=next_at)
        username = (
            st.get("creator_username")
            or getattr(ctx.account, "username", None)
            or (ctx.account.meta or {}).get("username")
        )
        url = post_url(username, post_id)
        st.update(public_post_id=post_id, external_id=post_id, external_url=url or st.get("external_url"))
        return FollowUpResult(
            external_id=post_id,
            external_url=url,
            message="TikTok ya muestra el vídeo públicamente: enlace y estadísticas del vídeo actualizados.",
        )

    def _final_result(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        external_id = st.get("external_id") or st.get("publish_id")
        if st.get("result") == "sent_to_app":
            if st.get("mode") == "inbox":
                message = (
                    "Vídeo enviado a la bandeja de entrada de la app de TikTok. Abre TikTok para añadir el "
                    "texto (cópialo desde la publicación) y terminar de publicar."
                )
            else:
                message = (
                    "TikTok ha guardado el vídeo como borrador en tu bandeja de entrada en lugar de "
                    "publicarlo. Abre la app de TikTok para terminar la publicación."
                )
            return StepResult("sent_to_app", external_id=external_id, message=message)
        if st.get("privacy_level") == "SELF_ONLY":
            message = (
                "Publicado en TikTok como «Solo yo» (privado). Puede tardar unos minutos en aparecer en tu "
                "perfil; cambia la visibilidad desde la app de TikTok si quieres que sea pública."
            )
        else:
            message = "Publicado en TikTok. Puede tardar unos minutos en aparecer en tu perfil."
        return StepResult(
            "published", external_id=external_id, external_url=st.get("external_url"), message=message
        )

    def _abandon(self, ctx: PublishContext, reason: str) -> None:
        """Forget the active publish (it can no longer go live) so the next attempt starts a fresh init."""
        st = ctx.state
        if st.get("publish_id"):
            history = list(st.get("abandoned") or [])
            history.append({"publish_id": st["publish_id"], "reason": reason, "at": _iso(ctx.clock.now())})
            st["abandoned"] = history[-10:]
        for key in _ACTIVE_KEYS:
            st.pop(key, None)
        st["phase"] = "new"
        ctx.save_state(st)

    # -- reconcile -----------------------------------------------------------------------------------

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        """Called after a crash/ambiguous error past the commit point: decide from the saved state and
        ``status/fetch`` (never re-sends)."""
        st = ctx.state
        if st.get("phase") == "done":
            if st.get("result") == "published":
                return ReconcileResult(
                    "published",
                    external_id=st.get("external_id"),
                    external_url=st.get("external_url"),
                    message="TikTok confirma la publicación.",
                )
            return ReconcileResult("unknown", message=_INBOX_RECONCILE_MSG)
        publish_id = st.get("publish_id")
        if not publish_id:
            return ReconcileResult(
                "not_published",
                message="TikTok no llegó a recibir el vídeo, así que no se ha publicado. Se puede "
                "reintentar.",
            )
        if not st.get("final_chunk_started"):
            # The upload resumes with this publish_id: ask for the commit phase to be re-asserted before the
            # next chunk, because the engine treats "not_published" as "nothing was sent".
            st["reassert_commit"] = True
            ctx.save_state(st)
            return ReconcileResult(
                "not_published",
                message="La subida del vídeo a TikTok no se completó, así que no se ha publicado. Se puede "
                "reanudar.",
            )
        token = ctx.account.access_token
        if not token:
            return ReconcileResult(
                "unknown",
                message="No se pudo consultar TikTok (falta la sesión). Revisa tu perfil de TikTok.",
            )
        for attempt in range(len(RECONCILE_DELAYS) + 1):
            try:
                data = fetch_status(ctx.http, token, str(publish_id))
            except ProviderError as err:
                return ReconcileResult(
                    "unknown",
                    message=f"No se pudo consultar el estado en TikTok: {err.message} Revisa tu perfil "
                    "de TikTok.",
                )
            status = str(data.get("status") or "").strip().upper()
            if status == "PUBLISH_COMPLETE":
                step = self._finish(ctx, "published", public_post_id(data))
                return ReconcileResult(
                    "published",
                    external_id=step.external_id,
                    external_url=step.external_url,
                    message="TikTok confirma la publicación.",
                )
            if status == "FAILED":
                reason = _str_or_none(data.get("fail_reason"))
                err = fail_reason_error(reason)
                self._abandon(ctx, f"failed:{reason or ''}")
                return ReconcileResult("not_published", message=f"TikTok no publicó el vídeo: {err.message}")
            if status == "SEND_TO_USER_INBOX":
                self._finish(ctx, "sent_to_app", None)
                return ReconcileResult("unknown", message=_INBOX_RECONCILE_MSG)
            if attempt < len(RECONCILE_DELAYS):
                _sleep(RECONCILE_DELAYS[attempt])
        return ReconcileResult(
            "unknown",
            message="TikTok sigue procesando el vídeo. Revisa tu perfil de TikTok en unos minutos para "
            "saber si se publicó.",
        )

    # ------------------------------------------------------------------------------ credentials

    def refresh_credentials(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
    ) -> TokenUpdate | None:
        """Refresh when the access token has less than 1 h left (or no known expiry). The refresh token is
        rotated whenever TikTok returns a new one (``apply_token_update`` keeps the old one otherwise)."""
        now = clock.now()
        if account.token_expires_at is not None and account.token_expires_at - now > TOKEN_REFRESH_MARGIN:
            return None
        if not account.refresh_token:
            raise ProviderError(ErrorKind.AUTH, "tiktok_no_refresh_token", MSG_RECONNECT)
        if account.refresh_expires_at is not None and account.refresh_expires_at <= now:
            raise ProviderError(
                ErrorKind.AUTH,
                "tiktok_refresh_expired",
                "La autorización de TikTok ha caducado (dura un año). Vuelve a conectar la cuenta.",
            )
        tokens = refresh_access_token(settings, http, account.refresh_token, clock=clock)
        return TokenUpdate(
            access_token=tokens.access_token,
            token_expires_at=tokens.expires_at,
            refresh_token=tokens.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at if tokens.refresh_token else None,
            scopes=tokens.scopes or None,
        )

    def check_account(self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> None:
        token = _require_token(account)
        _api(http, "GET", USER_INFO_URL, token, params={"fields": "open_id"}, op="check_account")

    # -------------------------------------------------------------------------------- analytics

    def fetch_account_snapshot(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, day: date
    ) -> AccountSnapshot:
        """Lifetime totals from ``/v2/user/info/`` (``user.info.stats``): ``followers``, ``following``,
        ``likes_total``, ``video_count``. Empty with a Spanish ``notice`` when the scope is missing."""
        granted = _granted_scopes(account)
        if granted is not None and "user.info.stats" not in granted:
            log.info("TikTok account %s lacks user.info.stats: no snapshot", account.id)
            return TikTokAccountSnapshot(day=day, metrics={}, notice=NOTICE_STATS_SCOPE)
        token = _require_token(account)
        try:
            data = _api(
                http,
                "GET",
                USER_INFO_URL,
                token,
                params={"fields": "open_id,follower_count,following_count,likes_count,video_count"},
                op="user_stats",
            )
        except ProviderError as err:
            if err.code in SCOPE_ERROR_CODES:
                log.info("TikTok account %s: stats scope refused", account.id)
                return TikTokAccountSnapshot(day=day, metrics={}, notice=NOTICE_STATS_SCOPE)
            raise
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        metrics: dict[str, float | int] = {}
        for src, dst in (
            ("follower_count", "followers"),
            ("following_count", "following"),
            ("likes_count", "likes_total"),
            ("video_count", "video_count"),
        ):
            value = _int_or_none(user.get(src))
            if value is not None:
                metrics[dst] = value
        return TikTokAccountSnapshot(day=day, metrics=metrics)

    def fetch_recent_posts(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[PostMetricData]:
        """Public videos created since ``since`` (``/v2/video/list/``, paginated, newest first) with lifetime
        ``views``, ``likes``, ``comments``, ``shares`` (+ ``interactions``, ``engagement`` over views).
        Empty with a Spanish ``notice`` when ``video.list`` is missing."""
        granted = _granted_scopes(account)
        if granted is not None and "video.list" not in granted:
            return TikTokPostList([], notice=NOTICE_VIDEO_SCOPE)
        token = _require_token(account)
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        out = TikTokPostList()
        cursor: Any = None
        for _page in range(VIDEO_LIST_MAX_PAGES):
            body: dict[str, Any] = {"max_count": VIDEO_LIST_PAGE}
            if cursor is not None:
                body["cursor"] = cursor
            try:
                data = _api(
                    http,
                    "POST",
                    VIDEO_LIST_URL,
                    token,
                    params={"fields": _VIDEO_FIELDS},
                    json_body=body,
                    op="video_list",
                )
            except ProviderError as err:
                if err.code in SCOPE_ERROR_CODES:
                    return TikTokPostList(out, notice=NOTICE_VIDEO_SCOPE)
                if err.code in {"invalid_params", "invalid_param"} and cursor is not None:
                    break  # expired cursor: keep what we already have (report 03 §8.3)
                raise
            videos = data.get("videos") if isinstance(data.get("videos"), list) else []
            reached_since = False
            for video in videos:
                if not isinstance(video, dict) or not video.get("id"):
                    continue
                created = _from_unix(video.get("create_time"))
                if created is not None and created < since:
                    reached_since = True
                    continue
                out.append(_video_metrics(video, created))
            if reached_since or not videos or not data.get("has_more"):
                break
            cursor = data.get("cursor")
            if cursor is None:
                break
        return out


_VIDEO_FIELDS = (
    "id,create_time,cover_image_url,share_url,video_description,title,duration,"
    "like_count,comment_count,share_count,view_count"
)


def _from_unix(value: Any) -> datetime | None:
    seconds = _int_or_none(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _strip_query(url: Any) -> str | None:
    text = _str_or_none(url)
    if not text:
        return None
    parts = urlsplit(text)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "", "")
    )  # the query may contain the client key


def _video_metrics(video: Mapping[str, Any], created: datetime | None) -> PostMetricData:
    views = _int_or_none(video.get("view_count")) or 0
    likes = _int_or_none(video.get("like_count")) or 0
    comments = _int_or_none(video.get("comment_count")) or 0
    shares = _int_or_none(video.get("share_count")) or 0
    interactions = likes + comments + shares
    return PostMetricData(
        external_id=str(video["id"]),
        published_at=created,
        permalink=_strip_query(video.get("share_url")),
        caption=_str_or_none(video.get("video_description")) or _str_or_none(video.get("title")),
        media_type="VIDEO",
        thumbnail_url=_str_or_none(video.get("cover_image_url")),
        metrics={
            "views": views,
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "interactions": interactions,
            "engagement": round(interactions / views * 100, 2) if views else 0.0,
        },
    )

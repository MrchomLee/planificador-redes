"""YouTube provider: Google OAuth (PKCE), Data API v3 resumable uploads, Analytics API v2 and comments.

Contract: SPEC §5 (provider interface), §6 (module-level OAuth helpers), §7 (YouTube formats/options).
Research: ``docs/research/06-youtube-api.md`` — its §17 "Verificación" overrides the body. In particular the
"private lock" for unaudited Google projects is **uncertain** (Google's reference now says uploads are not
restricted): we never assume it, we *detect* it (:func:`detect_lock`) and offer a throw-away upload test
(:func:`run_lock_test`, report 06 §3.7).

Publishing (:meth:`YouTubeProvider.publish_step`) is a re-entrant state machine kept in ``ctx.state``:

``init`` (``POST …/upload/youtube/v3/videos?uploadType=resumable`` → session URI saved immediately) →
``uploading`` (a few 8 MiB chunks per step, each ``PUT`` with ``Content-Range``; ``308`` + ``Range`` gives the
server's offset, which always wins over local state) → ``post`` (thumbnail, playlist, first comment — each
idempotent and flagged in the state; failures are warnings, the video already exists) → ``processing``
(``videos.list part=status,processingDetails`` polled through ``wait`` results) → ``done``.

Safety rules (never publish twice):

- YouTube creates the video only when the **final** byte arrives, so the final chunk is the commit point:
  ``ctx.before_commit(state)`` is called right before it. Init and non-final chunks are reversible.
- A session is never replaced once the final chunk may have been sent; the status query
  (``PUT <session>`` with ``Content-Range: bytes */TOTAL``) is the source of truth: ``200/201`` ⇒ the video
  exists, ``308`` ⇒ resume from ``Range``, ``404/410`` ⇒ session gone.
- After a process restart (or any chunk failure) the first thing done for a session is that status query.

Account meta keys used (``accounts.meta``): ``channel_id``, ``handle``, ``uploads_playlist_id``,
``long_uploads_status``, ``granted_scopes``, ``client_status_hint`` (``testing``), ``youtube_publish_mode``
(``assisted`` | ``api``), ``youtube_lock_status`` (``unknown`` | ``unlocked`` | ``locked``),
``upload_lock_suspected``, ``youtube_project_audited``. The provider cannot write the account row itself: when
an upload proves or disproves the lock it stores the desired meta changes in
``state["account_meta_updates"]`` (and logs an event); the engine merges them into ``accounts.meta``.

Token refresh during a publish: access tokens last 1 h, so long uploads refresh in memory when less than
5 minutes are left (and once after a 401). If the engine sets ``ctx.update_tokens`` (a callable taking a
:class:`TokenUpdate`) the new tokens are handed to it for persistence.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import random
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from planificador.providers.base import (
    AccountCreds,
    AccountSnapshot,
    CommentData,
    ErrorKind,
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
from planificador.timeutil import Clock, SystemClock, ensure_utc, format_time_es

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------ endpoints

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"  # report 06 §2.1
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_VIDEOS_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAILS_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
STUDIO_URL = "https://studio.youtube.com/"

#: Callback path registered in the user's Google "Web application" OAuth client (report 06 §1.3).
CALLBACK_PATH = "/conexiones/callback/google"

# --------------------------------------------------------------------------------------------- scopes

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPE_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPE_YOUTUBE = "https://www.googleapis.com/auth/youtube"

#: Exactly what we request (report 06 §1.2 decision; no openid/email/profile).
SCOPES_YT: list[str] = [SCOPE_UPLOAD, SCOPE_READONLY, SCOPE_FORCE_SSL, SCOPE_ANALYTICS]

# Feature → scopes that grant it (any of them). Report 06 §1.2 feature gating table.
_FEATURE_SCOPES: dict[str, tuple[str, ...]] = {
    "read": (SCOPE_READONLY, SCOPE_FORCE_SSL, SCOPE_YOUTUBE),
    "upload": (SCOPE_UPLOAD, SCOPE_FORCE_SSL, SCOPE_YOUTUBE),
    "playlist": (SCOPE_FORCE_SSL, SCOPE_YOUTUBE),
    "comments": (SCOPE_FORCE_SSL,),
    "analytics": (SCOPE_ANALYTICS, SCOPE_READONLY),
}
_FEATURE_LABELS: dict[str, str] = {
    "read": "ver tu canal y tus vídeos",
    "upload": "subir vídeos",
    "playlist": "añadir vídeos a listas de reproducción",
    "comments": "leer y responder comentarios",
    "analytics": "ver las estadísticas (YouTube Analytics)",
}

# ------------------------------------------------------------------------------------------- limits

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)  # access tokens last 1 h (report 06 §2.6)
DEFAULT_ACCESS_TTL = 3600

UPLOAD_QUANTUM = 256 * 1024  # every non-final chunk must be a multiple of 256 KiB (report 06 §4.4)
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB (Postiz/OpenPost)
CHUNKS_PER_STEP = 4  # chunks sent per publish_step call before returning "continue"
STEP_TIME_BUDGET_S = 60.0  # …or this much wall time, whichever comes first
READ_BLOCK = 1 << 20  # bytes read from disk at a time while streaming a chunk
SESSION_MAX_AGE = timedelta(days=6)  # resumable sessions last ~1 week; stop using them after 6 days
SESSION_RESTART_LIMIT = 2
CHUNK_RETRY_DELAYS: tuple[int, ...] = (10, 30, 60, 120)
STALL_LIMIT = 3  # 308 answers without progress before giving up (as TRANSIENT)

POLL_DELAYS: tuple[int, ...] = (30, 30, 60)  # then every POLL_INTERVAL (report 06 §8)
POLL_INTERVAL = 120
POLL_GIVE_UP = timedelta(minutes=30)
POLL_ERROR_LIMIT = 5
EXTRA_RETRY_LIMIT = 2  # transient retries for thumbnail / playlist / first comment
EXTRA_RETRY_WAIT = 30

NATIVE_SCHEDULE_MIN_LEAD = timedelta(seconds=60)  # publishAt only when still in the future
EARLY_START_TOLERANCE_S = 5  # a non-native upload never starts more than this before its scheduled time
RECONCILE_LOOKBACK = timedelta(minutes=5)
RECONCILE_RETRY_DELAY = 5

RATE_LIMIT_RETRY = 300  # rateLimitExceeded / 429 (report 06 §9.3)
UPLOAD_LIMIT_RETRY = 24 * 3600  # uploadLimitExceeded (per-channel daily cap)
THUMB_RATE_RETRY = 3600
QUOTA_TZ = "America/Los_Angeles"  # quotas reset at midnight Pacific
DISPLAY_TZ = "Europe/Madrid"

TITLE_MAX = 100  # characters (report 06 §4.3)
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX = 500
COMMENT_MAX = 10_000
SHORT_MAX_S = 180.0  # report 06 §5
LONG_UPLOAD_S = 15 * 60
THUMB_MAX_BYTES = 2 * 1024 * 1024  # we stay ≤ 2 MB even though the API now allows 50 MB (report 06 §6)
THUMB_MAX_SIDE = 1280
DEFAULT_CATEGORY = "22"  # People & Blogs / "Gente y blogs"

COMMENT_PAGES = 5
COMMENT_EXTRA_REPLY_FETCHES = 10
COMMENT_LOOKBACK = timedelta(hours=2)
RECENT_POSTS_PAGES = 4
ANALYTICS_HISTORY_DAYS = 7  # Analytics lags 2–3 days: re-fetch the last week every day (report 06 §10.2)
PLAYLIST_PAGES = 10

LOCK_TEST_TITLE = "Prueba Planificador — puedes borrarlo"
LOCK_TEST_SECONDS = 2
LOCK_TEST_SIZE = (360, 640)  # vertical
LOCK_TEST_FPS = 15
LOCK_TEST_POLL_DELAY = 10
LOCK_TEST_POLL_MAX = 30  # ≈ 5 minutes

API_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
UPLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=30.0)

# Account meta keys / values (SPEC §6).
META_LOCK_STATUS = "youtube_lock_status"
META_PUBLISH_MODE = "youtube_publish_mode"
META_AUDITED = "youtube_project_audited"
LOCK_STATUSES = ("unknown", "unlocked", "locked")
#: provider_state key holding ``accounts.meta`` changes the engine must merge (lock detection).
STATE_ACCOUNT_META = "account_meta_updates"

_sleep = time.sleep  # patched in tests
_monotonic = time.monotonic

# ------------------------------------------------------------------------------------------ messages

MSG_RECONNECT = (
    "La sesión de YouTube ha caducado o se ha revocado. Vuelve a conectar la cuenta en Conexiones."
)
MSG_NOT_CONFIGURED = "Configura el ID de cliente y el secreto de Google en Ajustes → YouTube."
MSG_TESTING = (
    "Tu app de Google está en modo «Prueba»: la autorización caduca a los 7 días y tendrás que reconectar "
    "YouTube cada semana. Publícala («En producción») en Google Cloud para evitarlo."
)
MSG_NO_CHANNEL = "Esta cuenta de Google no tiene canal de YouTube. Créalo en youtube.com y vuelve a conectar."
MSG_LOCKED = (
    "YouTube ha dejado el vídeo como privado: probablemente tu proyecto de Google no ha superado la "
    "auditoría de YouTube y bloquea las subidas hechas con la API. El vídeo está subido, pero solo tú puedes "
    "verlo. Usa la Publicación asistida o solicita la auditoría."
)
MSG_SCOPE = "Faltan permisos de YouTube: vuelve a conectar la cuenta y marca todas las casillas."

PRIVACY_LABELS: dict[str, str] = {
    "public": "Pública",
    "unlisted": "Oculto (no listado)",
    "private": "Privado",
}
_PRIVACY_ALIASES: dict[str, str] = {
    "public": "public",
    "publica": "public",
    "pública": "public",
    "publico": "public",
    "público": "public",
    "unlisted": "unlisted",
    "oculto": "unlisted",
    "no listado": "unlisted",
    "private": "private",
    "privado": "private",
    "privada": "private",
}
_LICENSES: dict[str, str] = {
    "youtube": "youtube",
    "standard": "youtube",
    "estandar": "youtube",
    "creativecommon": "creativeCommon",
    "creative_commons": "creativeCommon",
    "creativecommons": "creativeCommon",
    "cc": "creativeCommon",
}

_FIELD_ERRORS: dict[str, str] = {
    "invalidTitle": "El título del vídeo no es válido (obligatorio, máximo 100 caracteres y sin < ni >).",
    "invalidDescription": "La descripción no es válida (máximo 5000 bytes y sin < ni >).",
    "invalidTags": "Las etiquetas no son válidas (máximo 500 caracteres en total).",
    "invalidCategoryId": "La categoría elegida no está disponible para tu canal o región.",
    "invalidPublishAt": "La fecha de programación en YouTube no es válida.",
    "invalidRecordingDetails": "Los datos de grabación del vídeo no son válidos.",
    "invalidVideoGameRating": "La clasificación de videojuego no es válida.",
    "invalidFilename": "YouTube no acepta el nombre del archivo de vídeo.",
    "invalidVideoMetadata": "YouTube rechazó los datos del vídeo (título, descripción u opciones).",
    "defaultLanguageNotSet": "Falta el idioma del vídeo.",
    "mediaBodyRequired": "YouTube no recibió el archivo.",
    "forbiddenLicenseSetting": "YouTube no admite esa licencia para este vídeo.",
    "forbiddenPrivacySetting": "YouTube no admite esa visibilidad para este vídeo.",
    "commentsDisabled": "Los comentarios están desactivados en este vídeo.",
    "ineligibleAccount": "Tu canal de YouTube debe estar vinculado a tu cuenta de Google para comentar.",
    "operationNotSupported": "YouTube no permite responder a este comentario.",
    "parentCommentIsPrivate": "El comentario original es privado; no se puede responder.",
    "parentIdMissing": "Falta el comentario al que responder.",
    "commentTextRequired": "Escribe el texto del comentario.",
    "commentTextTooLong": "El comentario es demasiado largo.",
    "invalidCommentMetadata": "YouTube rechazó los datos del comentario.",
    "imageFormatUnsupported": "La miniatura debe ser una imagen JPG o PNG.",
    "imageTooTall": "La miniatura es demasiado alta (usa 1280×720).",
    "imageTooWide": "La miniatura es demasiado ancha (usa 1280×720).",
    "videoNotFound": "El vídeo ya no existe en YouTube.",
    "channelNotFound": "No se encontró el canal de YouTube.",
    "parentCommentNotFound": "El comentario ya no existe en YouTube.",
    "commentNotFound": "El comentario ya no existe en YouTube.",
    "commentThreadNotFound": "El comentario ya no existe en YouTube.",
    "playlistNotFound": "La lista de reproducción ya no existe.",
    "playlistItemsNotAccessible": "No tienes permiso para modificar esa lista de reproducción.",
    "playlistContainsMaximumNumberOfVideos": "La lista de reproducción está llena.",
    "videoAlreadyInPlaylist": "El vídeo ya estaba en la lista de reproducción.",
    "youtubeSignupRequired": MSG_NO_CHANNEL,
}

_REJECTION_MESSAGES: dict[str, str] = {
    "duplicate": "YouTube lo rechazó por ser un vídeo duplicado.",
    "copyright": "YouTube lo rechazó por derechos de autor.",
    "inappropriate": "YouTube lo rechazó por contenido inapropiado.",
    "termsOfUse": "YouTube lo rechazó por incumplir sus condiciones de uso.",
    "length": "El vídeo supera la duración permitida para tu canal.",
    "uploaderAccountSuspended": "Tu cuenta de YouTube está suspendida.",
    "uploaderAccountClosed": "Tu cuenta de YouTube está cerrada.",
    "claim": "YouTube lo rechazó por una reclamación de derechos.",
    "trademark": "YouTube lo rechazó por una reclamación de marca registrada.",
    "legal": "YouTube lo rechazó por motivos legales.",
}
_FAILURE_MESSAGES: dict[str, str] = {
    "conversion": "YouTube no pudo convertir el vídeo.",
    "invalidFile": "El archivo de vídeo no es válido.",
    "emptyFile": "El archivo de vídeo está vacío.",
    "tooSmall": "El archivo de vídeo es demasiado pequeño.",
    "codec": "YouTube no admite el códec del vídeo.",
    "uploadAborted": "La subida a YouTube se interrumpió.",
    "uploadFailed": "La subida a YouTube falló.",
    "transcodeFailed": "YouTube no pudo procesar (transcodificar) el vídeo.",
    "streamingFailed": "YouTube no pudo preparar el vídeo para reproducirlo.",
    "other": "YouTube no pudo procesar el vídeo.",
}

# ----------------------------------------------------------------------------------------- dataclasses


@dataclass
class GoogleTokens:
    access_token: str
    expires_at: datetime | None
    refresh_token: str | None
    refresh_expires_at: datetime | None  # from refresh_token_expires_in ("Testing" projects: ~7 days)
    scopes: list[str] = field(default_factory=list)

    @property
    def is_testing(self) -> bool:
        """True when Google limited the grant (``refresh_token_expires_in``): project in "Testing"."""
        return self.refresh_expires_at is not None

    @property
    def warning(self) -> str | None:
        """Spanish warning to show after connecting (Testing → reconnect every 7 days)."""
        return MSG_TESTING if self.is_testing else None


@dataclass
class YouTubeChannel:
    id: str
    title: str
    custom_url: str | None = None  # "@handle"
    thumbnail_url: str | None = None
    long_uploads_status: str | None = None  # allowed | eligible | disallowed | longUploadsUnspecified
    uploads_playlist_id: str | None = None
    country: str | None = None
    default_language: str | None = None
    subscriber_count: int | None = None
    video_count: int | None = None
    view_count: int | None = None
    hidden_subscriber_count: bool = False
    privacy_status: str | None = None
    made_for_kids: bool | None = None

    @property
    def handle(self) -> str | None:
        return self.custom_url.lstrip("@") if self.custom_url else None

    def to_meta(self) -> dict[str, Any]:
        """Values to store in ``accounts.meta`` for this channel."""
        return {
            "channel_id": self.id,
            "handle": self.custom_url,
            "uploads_playlist_id": self.uploads_playlist_id,
            "long_uploads_status": self.long_uploads_status,
            "country": self.country,
            "default_language": self.default_language,
        }


@dataclass
class UploadPlan:
    """Everything sent in the resumable session start (built by :func:`build_upload_plan`)."""

    body: dict[str, Any]
    parts: list[str]
    notify_subscribers: bool
    requested_privacy: str
    native_scheduled: bool
    publish_at: str | None
    title: str


@dataclass
class LockTestResult:
    status: Literal["unlocked", "locked", "error"]
    message: str  # Spanish
    video_id: str | None = None
    privacy_status: str | None = None
    deleted: bool = False


@dataclass
class YouTubeAccountSnapshot(AccountSnapshot):
    """Snapshot for ``day`` plus the daily Analytics rows of the previous days (``history``: Analytics lags
    2–3 days, so they are re-fetched every day) and an optional Spanish ``notice`` (e.g. Analytics disabled).
    Consumers should *merge* ``history`` metrics into the existing rows of those days."""

    history: list[AccountSnapshot] = field(default_factory=list)
    notice: str | None = None


@dataclass
class YouTubePostMetricData(PostMetricData):
    title: str | None = None
    description: str | None = None
    privacy_status: str | None = None
    duration_s: float | None = None


class YouTubePostList(list):
    """``list[PostMetricData]`` with an optional Spanish ``notice``."""

    def __init__(self, items: Any = (), notice: str | None = None) -> None:
        super().__init__(items)
        self.notice = notice


# ------------------------------------------------------------------------------------- small helpers


def _now(clock: Clock | None) -> datetime:
    return (clock or SystemClock()).now()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def rfc3339(dt: datetime) -> str:
    """UTC RFC 3339 with ``Z`` (``status.publishAt``)."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _tri_bool(value: Any) -> bool | None:
    """``True``/``False`` from bools and form-ish strings; ``None`` when not chosen."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "si", "sí", "on", "y", "s"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return None


def _as_bool(value: Any, default: bool) -> bool:
    parsed = _tri_bool(value)
    return default if parsed is None else parsed


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (ValueError, UnicodeDecodeError):
        return None


def _retry_after(headers: Mapping[str, str] | None) -> int | None:
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return max(1, int(raw))
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(1, int((when - datetime.now(UTC)).total_seconds()))


_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")


def next_offset_from_range(header: str | None) -> int:
    """``Range: bytes=0-524287`` → 524288 (next byte to send). No header ⇒ nothing stored yet ⇒ 0."""
    if not header:
        return 0
    m = _RANGE_RE.search(header)
    if not m:
        return 0
    return int(m.group(2)) + 1


def _chunk_size() -> int:
    size = max(UPLOAD_QUANTUM, int(CHUNK_SIZE))
    return size - (size % UPLOAD_QUANTUM)


def _iter_file(path: Path, offset: int, length: int) -> Iterator[bytes]:
    """Stream ``length`` bytes from ``offset`` (never loads the whole file in memory)."""
    remaining = length
    with path.open("rb") as fh:
        fh.seek(offset)
        while remaining > 0:
            block = fh.read(min(READ_BLOCK, remaining))
            if not block:
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "youtube_file_changed",
                    "El archivo de vídeo ha cambiado o está incompleto; vuelve a añadirlo a la publicación.",
                )
            remaining -= len(block)
            yield block


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def shorts_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def studio_upload_url(channel_id: str | None) -> str:
    """YouTube Studio upload page for assisted publishing (deep link UNVERIFIED → Studio home fallback)."""
    if channel_id:
        return f"https://studio.youtube.com/channel/{quote(channel_id)}/videos/upload?d=ud"
    return STUDIO_URL


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(value: str | None) -> str | None:
    """Video id from ``watch?v=``, ``youtu.be/``, ``/shorts/``, ``/live/``, ``/embed/`` URLs or a bare id."""
    if not value:
        return None
    text = value.strip()
    if _VIDEO_ID_RE.match(text):
        return text
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parts.path.strip("/").split("/")[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None
    if not (host == "youtube.com" or host.endswith(".youtube.com") or host.endswith("youtube-nocookie.com")):
        return None
    query = parse_qs(parts.query)
    if "v" in query and _VIDEO_ID_RE.match(query["v"][0]):
        return query["v"][0]
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[0] in {"shorts", "live", "embed", "v"}:
        return segments[1] if _VIDEO_ID_RE.match(segments[1]) else None
    return None


_DURATION_RE = re.compile(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?")


def parse_duration(value: str | None) -> float | None:
    """ISO 8601 duration (``PT1H2M3S``, ``P1DT2S``) → seconds."""
    if not value:
        return None
    m = _DURATION_RE.fullmatch(value.strip())
    if not m:
        return None
    weeks, days, hours, minutes, seconds = m.groups()
    return (
        int(weeks or 0) * 7 * 86400
        + int(days or 0) * 86400
        + int(hours or 0) * 3600
        + int(minutes or 0) * 60
        + float(seconds or 0)
    )


# ---------------------------------------------------------------------------------- lock / mode helpers


def lock_status(meta: Mapping[str, Any] | None) -> str:
    value = str((meta or {}).get(META_LOCK_STATUS) or "unknown")
    return value if value in LOCK_STATUSES else "unknown"


def automatic_publishing_allowed(meta: Mapping[str, Any] | None) -> bool:
    """API ("automatic") publishing is recommended only when the lock test passed or the project is audited
    (report 06 §3.7, SPEC §7)."""
    meta = meta or {}
    return lock_status(meta) == "unlocked" or bool(_tri_bool(meta.get(META_AUDITED)))


def default_auto_publish(meta: Mapping[str, Any] | None) -> bool:
    """Default ``auto_publish`` for new YouTube targets: assisted publishing until proven unlocked."""
    meta = meta or {}
    mode = meta.get(META_PUBLISH_MODE)
    if mode == "api":
        return True
    if mode == "assisted":
        return False
    return automatic_publishing_allowed(meta)


def detect_lock(
    video_resource: Mapping[str, Any] | None, requested: str | None, *, now: datetime | None = None
) -> bool:
    """Signal of the "private lock" (report 06 §3.4): we asked for ``public``/``unlisted`` and YouTube
    reports ``private``. A video scheduled with ``publishAt`` is private on purpose until that time (pass
    ``now`` to also flag one that is still private 15 min after its ``publishAt``)."""
    if not video_resource or requested not in ("public", "unlisted"):
        return False
    status = video_resource.get("status") or {}
    if status.get("privacyStatus") != "private":
        return False
    publish_at = _parse_iso(status.get("publishAt"))
    if publish_at is not None:
        return now is not None and now - publish_at > timedelta(minutes=15)
    return True


def granted_scopes(account: AccountCreds) -> set[str] | None:
    scopes = (account.meta or {}).get("granted_scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not scopes:
        return None
    return {str(s) for s in scopes}


def has_feature(scopes: set[str] | list[str] | None, feature: str) -> bool:
    if scopes is None:
        return True  # unknown → let the API decide
    granted = set(scopes)
    return any(s in granted for s in _FEATURE_SCOPES[feature])


def missing_features(scopes: list[str] | set[str] | None) -> list[str]:
    """Feature keys (``read``, ``upload``, ``playlist``, ``comments``, ``analytics``) the grant lacks."""
    if scopes is None:
        return []
    return [f for f in _FEATURE_SCOPES if not has_feature(scopes, f)]


def scope_warnings(scopes: list[str] | set[str] | None) -> list[str]:
    """Spanish warnings for the connection page (granular consent lets the user untick scopes)."""
    return [
        f"No diste permiso para {_FEATURE_LABELS[f]}. Vuelve a conectar YouTube y marca todas las casillas."
        for f in missing_features(scopes)
    ]


def account_meta_from_channel(channel: YouTubeChannel, tokens: GoogleTokens | None = None) -> dict[str, Any]:
    """``accounts.meta`` for a freshly connected channel (Connections route helper)."""
    meta: dict[str, Any] = channel.to_meta()
    meta.setdefault(META_PUBLISH_MODE, "assisted")
    meta.setdefault(META_LOCK_STATUS, "unknown")
    if tokens is not None:
        meta["granted_scopes"] = list(tokens.scopes)
        meta["client_status_hint"] = "testing" if tokens.is_testing else "production"
    return meta


# ------------------------------------------------------------------------------------ error mapping


_HTML_TAG = re.compile(r"<[^>]+>")


def _clean_message(text: Any, limit: int = 300) -> str:
    if not text:
        return ""
    cleaned = _HTML_TAG.sub("", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return scrub_text(cleaned)[:limit]


def _error_fields(body: Any) -> tuple[str | None, str | None, str, str | None]:
    """(reason, status, message, domain) from a Google error envelope (or an OAuth-style error)."""
    if not isinstance(body, Mapping):
        return None, None, "", None
    err = body.get("error")
    if isinstance(err, Mapping):
        reason = domain = None
        errors = err.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
            reason = errors[0].get("reason")
            domain = errors[0].get("domain")
        details = err.get("details")
        if not reason and isinstance(details, list):
            for d in details:
                if isinstance(d, Mapping) and d.get("reason"):
                    reason = d.get("reason")
                    domain = domain or d.get("domain")
                    break
        return (
            str(reason) if reason else None,
            str(err.get("status")) if err.get("status") else None,
            _clean_message(err.get("message")),
            str(domain) if domain else None,
        )
    if isinstance(err, str):
        return err, None, _clean_message(body.get("error_description")), None
    return None, None, "", None


def quota_reset_at(now: datetime) -> datetime:
    """Next quota reset (00:00 Pacific) plus a 5-minute margin, as aware UTC."""
    pacific = ZoneInfo(QUOTA_TZ)
    local = now.astimezone(pacific)
    nxt = datetime.combine(local.date() + timedelta(days=1), dtime(0, 5), tzinfo=pacific)
    return nxt.astimezone(UTC)


def _quota_bucket(url: str | None) -> str:
    u = url or ""
    if "/upload/youtube/v3/videos" in u:
        return "subidas de vídeo"
    if "/youtube/v3/search" in u:
        return "búsquedas"
    if "youtubeanalytics" in u:
        return "YouTube Analytics"
    return "la API de YouTube"


def _api_name(url: str | None) -> str:
    return "YouTube Analytics API" if "youtubeanalytics" in (url or "") else "YouTube Data API v3"


_FORBIDDEN_BY_OP: dict[str, str] = {
    "thumbnail": "Para usar miniaturas personalizadas tu canal debe estar verificado (youtube.com/verify).",
    "upload_init": "YouTube no permite subir vídeos con esta cuenta.",
    "upload_chunk": "YouTube no permite subir vídeos con esta cuenta.",
    "comment": "YouTube no permite comentar en este vídeo.",
    "reply": "YouTube no permite responder a este comentario.",
    "playlist": "No tienes permiso para modificar esa lista de reproducción.",
    "analytics": "YouTube no permite ver las estadísticas de este canal con estos permisos.",
}


def classify_google_error(
    status_code: int,
    body: Any,
    headers: Mapping[str, str] | None = None,
    *,
    op: str = "api",
    url: str | None = None,
    now: datetime | None = None,
) -> ProviderError:
    """Map a Google API error response to a :class:`ProviderError` with a Spanish message (report 06 §12)."""
    reason, status, message, domain = _error_fields(body)
    retry_after = _retry_after(headers)
    detail: dict[str, Any] = {
        "http_status": status_code,
        "reason": reason,
        "status": status,
        "domain": domain,
        "message": message,
        "op": op,
    }
    if url:
        detail["endpoint"] = urlsplit(url).path
    now = now or datetime.now(UTC)
    code = f"youtube_{reason}" if reason else f"youtube_http_{status_code}"
    lowered = message.lower()

    def err(
        kind: ErrorKind, msg: str, *, retry: int | None = None, code_: str | None = None
    ) -> ProviderError:
        return ProviderError(kind, code_ or code, msg, detail=detail, retry_after=retry)

    if reason in ("quotaExceeded", "dailyLimitExceeded") or (
        status == "RESOURCE_EXHAUSTED" and "quota" in lowered
    ):
        reset = quota_reset_at(now)
        when = format_time_es(reset, DISPLAY_TZ)
        return err(
            ErrorKind.RATE_LIMITED,
            f"Se agotó la cuota diaria de {_quota_bucket(url)} de tu proyecto de Google. Se reintentará a "
            f"partir de las {when} (hora peninsular), cuando Google la renueve.",
            retry=max(60, int((reset - now).total_seconds())),
            code_="youtube_quota_exceeded",
        )
    if reason == "uploadLimitExceeded":
        return err(
            ErrorKind.RATE_LIMITED,
            "Has alcanzado el límite diario de subidas de tu canal de YouTube. Se reintentará mañana.",
            retry=UPLOAD_LIMIT_RETRY,
        )
    if reason == "uploadRateLimitExceeded":
        return err(
            ErrorKind.RATE_LIMITED,
            "Has cambiado demasiadas miniaturas en poco tiempo; YouTube pide esperar.",
            retry=retry_after or THUMB_RATE_RETRY,
        )
    if (
        reason in ("rateLimitExceeded", "userRateLimitExceeded")
        or status_code == 429
        or status == "RESOURCE_EXHAUSTED"
    ):
        return err(
            ErrorKind.RATE_LIMITED,
            "YouTube pide ir más despacio; se reintentará en unos minutos.",
            retry=retry_after or RATE_LIMIT_RETRY,
            code_=code if reason else "youtube_rate_limited",
        )
    if reason == "youtubeSignupRequired":
        return err(ErrorKind.PERMANENT, MSG_NO_CHANNEL)
    if status_code == 401:
        return err(ErrorKind.AUTH, MSG_RECONNECT, code_="youtube_unauthorized")
    if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientScope") or (
        status_code == 403 and "insufficient" in lowered and "scope" in lowered
    ):
        return err(ErrorKind.AUTH, MSG_SCOPE, code_="youtube_insufficient_scope")
    if (
        reason in ("accessNotConfigured", "SERVICE_DISABLED")
        or "has not been used in project" in lowered
        or "it is disabled" in lowered
    ):
        return err(
            ErrorKind.PERMANENT,
            f"Activa la «{_api_name(url)}» en tu proyecto de Google Cloud (APIs y servicios → Biblioteca).",
            code_="youtube_api_disabled",
        )
    if reason == "processingFailure":
        return err(
            ErrorKind.TRANSIENT, "YouTube no pudo procesar la petición; se reintentará.", retry=retry_after
        )
    if reason and reason in _FIELD_ERRORS:
        if op == "thumbnail" and reason == "mediaBodyRequired":
            return err(ErrorKind.PERMANENT, "La miniatura no es válida (usa JPG o PNG de 1280×720).")
        return err(ErrorKind.PERMANENT, _FIELD_ERRORS[reason])
    if op == "thumbnail" and reason == "failedPrecondition":
        return err(ErrorKind.PERMANENT, "La miniatura no es válida (máximo 2 MB, JPG o PNG).")
    if status_code >= 500 or status_code == 408 or reason in ("backendError", "internalError"):
        return err(
            ErrorKind.TRANSIENT,
            "YouTube ha tenido un error temporal; se reintentará.",
            retry=retry_after,
            code_=code if reason else "youtube_server_error",
        )
    if status_code == 403:
        base = _FORBIDDEN_BY_OP.get(op, "YouTube ha denegado la operación.")
        extra = f" ({message})" if message and reason != "forbidden" else ""
        return err(ErrorKind.PERMANENT, base + extra, code_=code if reason else "youtube_forbidden")
    if status_code == 404:
        return err(ErrorKind.PERMANENT, "YouTube no encuentra el recurso solicitado.", code_=code)
    suffix = f": {message}" if message else "."
    return err(ErrorKind.PERMANENT, f"YouTube rechazó la petición (código {status_code}){suffix}")


def _transport_error(exc: httpx.HTTPError, op: str) -> ProviderError:
    detail = {"op": op, "error": type(exc).__name__, "message": scrub_text(str(exc))[:300]}
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(
            ErrorKind.TRANSIENT,
            "youtube_timeout",
            "YouTube no respondió a tiempo; se reintentará.",
            detail=detail,
        )
    return ProviderError(
        ErrorKind.TRANSIENT,
        "youtube_network",
        "No se pudo conectar con YouTube; se reintentará.",
        detail=detail,
    )


def classify_oauth_error(
    status_code: int, body: Any, *, op: str, redirect: str | None = None, testing: bool = False
) -> ProviderError:
    """Errors of ``oauth2.googleapis.com/token`` (report 06 §2.4)."""
    err = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(err, Mapping):  # some Google endpoints answer with the API envelope
        return classify_google_error(status_code, body, op=op)
    error = str(err) if isinstance(err, str) else None
    desc = _clean_message(body.get("error_description")) if isinstance(body, Mapping) else ""
    subtype = str(body.get("error_subtype") or "") if isinstance(body, Mapping) else ""
    detail = {
        "http_status": status_code,
        "error": error,
        "error_subtype": subtype or None,
        "message": desc,
        "op": op,
    }
    if error == "invalid_grant":
        if subtype == "invalid_rapt":
            msg = "Tu organización de Google Workspace exige volver a iniciar sesión; reconecta YouTube."
        elif op == "exchange":
            return ProviderError(
                ErrorKind.PERMANENT,
                "google_invalid_grant",
                "El código de autorización de Google ha caducado o ya se usó. Vuelve a intentar la conexión.",
                detail=detail,
            )
        elif testing:
            msg = (
                "La autorización de YouTube ha caducado: tu app de Google está en modo «Prueba» y solo "
                "dura 7 días. Vuelve a conectar la cuenta (o publica la app en Google Cloud)."
            )
        else:
            msg = MSG_RECONNECT
        return ProviderError(ErrorKind.AUTH, "google_invalid_grant", msg, detail=detail)
    if error in ("invalid_client", "unauthorized_client"):
        return ProviderError(
            ErrorKind.AUTH,
            "google_invalid_client",
            "El ID o el secreto de cliente de Google no son válidos. Revísalos en Ajustes → YouTube.",
            detail=detail,
        )
    if error == "redirect_uri_mismatch":
        uri = redirect or f"http://localhost:8765{CALLBACK_PATH}"
        return ProviderError(
            ErrorKind.PERMANENT,
            "google_redirect_uri_mismatch",
            "Google rechazó la dirección de retorno. Crea un cliente de tipo «Aplicación web» y añade "
            f"esta URI autorizada: {uri}",
            detail=detail,
        )
    if error == "access_denied":
        return ProviderError(
            ErrorKind.PERMANENT, "google_access_denied", "Se canceló el permiso en Google.", detail=detail
        )
    if status_code >= 500:
        return ProviderError(
            ErrorKind.TRANSIENT,
            "google_server_error",
            "Google ha tenido un error temporal; vuelve a intentarlo.",
            detail=detail,
        )
    if status_code == 429:
        return ProviderError(
            ErrorKind.RATE_LIMITED,
            "google_rate_limited",
            "Google pide ir más despacio; vuelve a intentarlo en unos minutos.",
            detail=detail,
            retry_after=RATE_LIMIT_RETRY,
        )
    suffix = f": {desc}" if desc else "."
    return ProviderError(
        ErrorKind.PERMANENT,
        f"google_{error or status_code}",
        f"Google rechazó la autorización ({error or status_code}){suffix}",
        detail=detail,
    )


# ------------------------------------------------------------------------------------------ OAuth


def _require_client(settings: ProviderSettings) -> tuple[str, str]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ProviderError(ErrorKind.AUTH, "google_not_configured", MSG_NOT_CONFIGURED)
    return settings.google_client_id, settings.google_client_secret


def _oauth_post(
    http: httpx.Client,
    data: dict[str, str],
    *,
    op: str,
    redirect: str | None = None,
    testing: bool = False,
) -> dict[str, Any]:
    try:
        resp = http.post(TOKEN_URL, data=data, headers={"Accept": "application/json"}, timeout=API_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _transport_error(exc, op) from exc
    body = _json_or_none(resp)
    if resp.status_code >= 400 or not isinstance(body, dict) or "error" in body:
        raise classify_oauth_error(resp.status_code, body, op=op, redirect=redirect, testing=testing)
    return body


def _tokens_from_payload(payload: Mapping[str, Any], now: datetime) -> GoogleTokens:
    access = payload.get("access_token")
    if not access:
        raise ProviderError(
            ErrorKind.PERMANENT,
            "google_no_access_token",
            "Google no devolvió un token de acceso. Vuelve a intentar la conexión.",
            detail={"keys": sorted(payload)},
        )
    expires_in = _int_or_none(payload.get("expires_in")) or DEFAULT_ACCESS_TTL
    refresh_in = _int_or_none(payload.get("refresh_token_expires_in"))
    return GoogleTokens(
        access_token=str(access),
        expires_at=now + timedelta(seconds=expires_in),
        refresh_token=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
        refresh_expires_at=now + timedelta(seconds=refresh_in) if refresh_in else None,
        scopes=str(payload.get("scope") or "").split(),
    )


def redirect_uri(settings: ProviderSettings) -> str:
    """``http://localhost:<port>/conexiones/callback/google`` — always ``localhost`` (SPEC §2)."""
    base = (settings.base_url or "http://localhost:8765").rstrip("/")
    return f"{base}{CALLBACK_PATH}"


def pkce_challenge(verifier: str) -> str:
    """S256 challenge: base64url(sha256(verifier)) **without** ``=`` padding (report 06 §2.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_pkce_pair() -> tuple[str, str]:
    """(verifier, challenge). Verifier: 86 chars from ``[A-Za-z0-9_-]`` (RFC 7636 allows 43–128)."""
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)


def build_login_url(settings: ProviderSettings, redirect_uri: str, state: str, code_challenge: str) -> str:
    """Google consent URL: offline access (refresh token), ``prompt=consent select_account``, PKCE S256."""
    client_id = settings.google_client_id
    if not client_id:
        raise ProviderError(ErrorKind.AUTH, "google_not_configured", MSG_NOT_CONFIGURED)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES_YT),
        "state": state,
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params, quote_via=quote)}"


def exchange_code(
    settings: ProviderSettings,
    http: httpx.Client,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    clock: Clock | None = None,
) -> GoogleTokens:
    """Authorization code → tokens. ``client_secret`` is always sent (Google requires it for every client
    type). ``refresh_token_expires_in`` in the answer means the project is in "Testing" (7-day grant):
    ``tokens.is_testing`` / ``tokens.warning``."""
    client_id, client_secret = _require_client(settings)
    payload = _oauth_post(
        http,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
        op="exchange",
        redirect=redirect_uri,
    )
    return _tokens_from_payload(payload, _now(clock))


def refresh_access_token(
    settings: ProviderSettings,
    http: httpx.Client,
    refresh_token: str,
    *,
    clock: Clock | None = None,
    testing: bool = False,
) -> GoogleTokens:
    """New access token. Google normally does not rotate the refresh token (``refresh_token`` is ``None``
    in the result unless a new one came back)."""
    client_id, client_secret = _require_client(settings)
    payload = _oauth_post(
        http,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        op="refresh",
        testing=testing,
    )
    return _tokens_from_payload(payload, _now(clock))


def revoke(settings: ProviderSettings, http: httpx.Client, token: str, *, strict: bool = False) -> None:
    """Revoke a token (revoking the refresh token kills the whole grant). Best effort: ``400 invalid_token``
    counts as already revoked; other failures are logged and swallowed unless ``strict=True``."""
    if not token:
        return
    try:
        resp = http.post(REVOKE_URL, data={"token": token}, timeout=API_TIMEOUT)
    except httpx.HTTPError as exc:
        if strict:
            raise _transport_error(exc, "revoke") from exc
        log.warning("Google token revoke failed: %s", type(exc).__name__)
        return
    if resp.status_code < 400:
        return
    body = _json_or_none(resp)
    if isinstance(body, Mapping) and body.get("error") == "invalid_token":
        return
    if strict:
        raise classify_oauth_error(resp.status_code, body, op="revoke")
    log.warning("Google token revoke failed with HTTP %s", resp.status_code)


def apply_token_update(account: AccountCreds, update: TokenUpdate) -> None:
    """Update an in-memory :class:`AccountCreds` (the caller persists encrypted tokens)."""
    account.access_token = update.access_token
    account.token_expires_at = update.token_expires_at
    if update.refresh_token:
        account.refresh_token = update.refresh_token
    if update.refresh_expires_at:
        account.refresh_expires_at = update.refresh_expires_at


def _refresh_account(
    account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock | None
) -> TokenUpdate:
    now = _now(clock)
    if not account.refresh_token:
        raise ProviderError(ErrorKind.AUTH, "youtube_no_refresh_token", MSG_RECONNECT)
    testing = account.refresh_expires_at is not None
    if account.refresh_expires_at is not None and account.refresh_expires_at <= now:
        raise ProviderError(
            ErrorKind.AUTH,
            "youtube_refresh_expired",
            "La autorización de YouTube ha caducado (tu app de Google está en modo «Prueba» y solo dura 7 "
            "días). Vuelve a conectar la cuenta.",
        )
    tokens = refresh_access_token(settings, http, account.refresh_token, clock=clock, testing=testing)
    return TokenUpdate(
        access_token=tokens.access_token,
        token_expires_at=tokens.expires_at,
        refresh_token=tokens.refresh_token,
        refresh_expires_at=tokens.refresh_expires_at,
        scopes=tokens.scopes or None,
    )


# ------------------------------------------------------------------------------------ API session


class _Api:
    """Authenticated calls for one account: refreshes the access token when < 5 min are left and retries
    once after a 401 (report 06 §2.6). New tokens are applied to ``account`` in memory and handed to
    ``on_tokens`` (when the caller can persist them)."""

    def __init__(
        self,
        account: AccountCreds,
        settings: ProviderSettings,
        http: httpx.Client,
        clock: Clock | None = None,
        on_tokens: Callable[[TokenUpdate], None] | None = None,
    ) -> None:
        self.account = account
        self.settings = settings
        self.http = http
        self.clock = clock or SystemClock()
        self.on_tokens = on_tokens

    def _can_refresh(self) -> bool:
        return bool(
            self.account.refresh_token
            and self.settings.google_client_id
            and self.settings.google_client_secret
        )

    def refresh(self) -> None:
        update = _refresh_account(self.account, self.settings, self.http, self.clock)
        apply_token_update(self.account, update)
        if self.on_tokens is not None:
            try:
                self.on_tokens(update)
            except Exception:  # persisting is best effort; the in-memory token still works
                log.exception("Could not persist refreshed YouTube tokens")

    def token(self) -> str:
        acc = self.account
        expires = acc.token_expires_at
        fresh = expires is None or expires - self.clock.now() > TOKEN_REFRESH_MARGIN
        if acc.access_token and fresh:
            return acc.access_token
        if self._can_refresh():
            self.refresh()
            if acc.access_token:
                return acc.access_token
        if acc.access_token:
            return acc.access_token  # let the API decide (401 → AUTH)
        raise ProviderError(ErrorKind.AUTH, "youtube_no_token", MSG_RECONNECT)

    def request(
        self,
        method: str,
        url: str,
        *,
        op: str,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | Callable[[], Iterator[bytes]] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout = API_TIMEOUT,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> httpx.Response:
        for attempt in range(2):
            hdrs = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
            body: Any = content() if callable(content) else content
            if json_body is not None:
                body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                hdrs["Content-Type"] = "application/json; charset=UTF-8"
            if headers:
                hdrs.update(headers)
            try:
                resp = self.http.request(
                    method,
                    url,
                    params=params,
                    content=body,
                    headers=hdrs,
                    timeout=timeout,
                    follow_redirects=False,  # a 308 "Resume Incomplete" must reach us
                )
            except httpx.HTTPError as exc:
                raise _transport_error(exc, op) from exc
            if resp.status_code in ok:
                return resp
            payload = _json_or_none(resp)
            if resp.status_code == 401 and attempt == 0 and self._can_refresh():
                if _error_fields(payload)[0] != "youtubeSignupRequired":
                    self.refresh()
                    continue
            raise classify_google_error(
                resp.status_code, payload, resp.headers, op=op, url=url, now=self.clock.now()
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, url: str, params: Mapping[str, Any], *, op: str) -> dict[str, Any]:
        resp = self.request("GET", url, params=params, op=op)
        body = _json_or_none(resp)
        return body if isinstance(body, dict) else {}


def _bearer_get(http: httpx.Client, url: str, token: str, params: Mapping[str, Any], *, op: str) -> dict:
    """GET with a bare access token (right after the OAuth exchange, before an account exists)."""
    try:
        resp = http.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=API_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise _transport_error(exc, op) from exc
    body = _json_or_none(resp)
    if resp.status_code >= 400:
        raise classify_google_error(resp.status_code, body, resp.headers, op=op, url=url)
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------------- channels & lists


def _channel_from_item(item: Mapping[str, Any]) -> YouTubeChannel:
    snippet = item.get("snippet") or {}
    thumbs = snippet.get("thumbnails") or {}
    thumb = (thumbs.get("medium") or thumbs.get("default") or thumbs.get("high") or {}).get("url")
    stats = item.get("statistics") or {}
    status = item.get("status") or {}
    related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
    return YouTubeChannel(
        id=str(item.get("id") or ""),
        title=str(snippet.get("title") or ""),
        custom_url=snippet.get("customUrl") or None,
        thumbnail_url=thumb,
        long_uploads_status=status.get("longUploadsStatus"),
        uploads_playlist_id=related.get("uploads"),
        country=snippet.get("country"),
        default_language=snippet.get("defaultLanguage"),
        subscriber_count=_int_or_none(stats.get("subscriberCount")),
        video_count=_int_or_none(stats.get("videoCount")),
        view_count=_int_or_none(stats.get("viewCount")),
        hidden_subscriber_count=bool(stats.get("hiddenSubscriberCount")),
        privacy_status=status.get("privacyStatus"),
        made_for_kids=status.get("madeForKids"),
    )


def list_my_channels(
    settings: ProviderSettings, http: httpx.Client, access_token: str
) -> list[YouTubeChannel]:
    """Channels bound to the token (``channels.list mine=true``). Raises ``ProviderError`` with code
    ``youtube_no_channel`` (Spanish message) when the Google account has no channel."""
    body = _bearer_get(
        http,
        f"{API_BASE}/channels",
        access_token,
        {"part": "snippet,contentDetails,statistics,status", "mine": "true", "maxResults": 50},
        op="channels",
    )
    channels = [
        _channel_from_item(item)
        for item in body.get("items") or []
        if isinstance(item, Mapping) and item.get("id")
    ]
    if not channels:
        raise ProviderError(ErrorKind.PERMANENT, "youtube_no_channel", MSG_NO_CHANNEL)
    return channels


def list_categories(
    account: AccountCreds,
    settings: ProviderSettings,
    http: httpx.Client,
    region: str = "ES",
    hl: str = "es",
) -> list[tuple[str, str]]:
    """Assignable video categories for ``region`` with titles in ``hl`` (``videoCategories.list``)."""
    api = _Api(account, settings, http)
    body = api.get(
        f"{API_BASE}/videoCategories",
        {"part": "snippet", "regionCode": region or "ES", "hl": hl or "es"},
        op="categories",
    )
    out: list[tuple[str, str]] = []
    for item in body.get("items") or []:
        snippet = item.get("snippet") or {}
        if snippet.get("assignable") is False:
            continue
        if item.get("id") and snippet.get("title"):
            out.append((str(item["id"]), str(snippet["title"])))
    return out


def list_playlists(
    account: AccountCreds, settings: ProviderSettings, http: httpx.Client
) -> list[tuple[str, str]]:
    """The channel's playlists (``playlists.list mine=true``), all pages (capped)."""
    api = _Api(account, settings, http)
    out: list[tuple[str, str]] = []
    page_token: str | None = None
    for _ in range(PLAYLIST_PAGES):
        params: dict[str, Any] = {"part": "snippet", "mine": "true", "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        body = api.get(f"{API_BASE}/playlists", params, op="playlists")
        for item in body.get("items") or []:
            title = (item.get("snippet") or {}).get("title")
            if item.get("id"):
                out.append((str(item["id"]), str(title or item["id"])))
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return out


def _uploads_playlist_id(api: _Api) -> str | None:
    account = api.account
    pid = (account.meta or {}).get("uploads_playlist_id")
    if pid:
        return str(pid)
    body = api.get(
        f"{API_BASE}/channels", {"part": "contentDetails", "id": account.external_id}, op="channels"
    )
    for item in body.get("items") or []:
        uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        if uploads:
            return str(uploads)
    ch = account.external_id or ""
    return "UU" + ch[2:] if ch.startswith("UC") else None


def _videos(api: _Api, ids: list[str], part: str, *, op: str) -> list[dict[str, Any]]:
    """``videos.list`` by id, 50 ids per call (1 unit). ``maxResults`` is not allowed together with ``id``."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), 50):
        batch = [v for v in ids[i : i + 50] if v]
        if not batch:
            continue
        body = api.get(f"{API_BASE}/videos", {"part": part, "id": ",".join(batch)}, op=op)
        out.extend(item for item in body.get("items") or [] if isinstance(item, dict))
    return out


def _find_by_title(api: _Api, title: str, since: datetime | None) -> dict[str, Any] | None:
    uploads = _uploads_playlist_id(api)
    if not uploads or not title:
        return None
    body = api.get(
        f"{API_BASE}/playlistItems",
        {"part": "contentDetails,snippet", "playlistId": uploads, "maxResults": 10},
        op="reconcile",
    )
    ids: list[str] = []
    for item in body.get("items") or []:
        vid = (item.get("contentDetails") or {}).get("videoId") or (
            ((item.get("snippet") or {}).get("resourceId") or {}).get("videoId")
        )
        if vid:
            ids.append(str(vid))
    for video in _videos(api, ids, "snippet,status", op="reconcile"):
        snippet = video.get("snippet") or {}
        if (snippet.get("title") or "").strip() != title.strip():
            continue
        published = _parse_iso(snippet.get("publishedAt"))
        if since is not None and published is not None and published < since - RECONCILE_LOOKBACK:
            continue
        return video
    return None


def find_uploaded_video(
    account: AccountCreds,
    settings: ProviderSettings,
    http: httpx.Client,
    title: str,
    since: datetime | None,
    *,
    clock: Clock | None = None,
) -> dict[str, Any] | None:
    """Latest upload whose title equals ``title`` (published ≥ ``since`` − 5 min), via the uploads playlist
    (report 06 §4.8). Also useful to link an assisted-mode post to the video the user uploaded in Studio."""
    return _find_by_title(_Api(account, settings, http, clock), title, since)


# --------------------------------------------------------------------------------- target → metadata


def _options(target: TargetSpec) -> dict[str, Any]:
    return target.options or {}


def title_for(target: TargetSpec) -> tuple[str, bool]:
    """(title, explicit). Explicit = the ``title`` option; otherwise the first line of the text (with
    ``<``/``>`` removed and cut to 100 characters, because the user did not type it as a title)."""
    explicit = str(_options(target).get("title") or "").strip()
    if explicit:
        return explicit, True
    for line in (target.text or "").splitlines():
        line = line.replace("<", "").replace(">", "").strip()
        if line:
            if len(line) > TITLE_MAX:
                cut = line[: TITLE_MAX - 1].rsplit(" ", 1)[0].rstrip() or line[: TITLE_MAX - 1]
                line = cut + "…"
            return line, False
    return "", False


def tags_for(target: TargetSpec) -> list[str]:
    raw = _options(target).get("tags")
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        tag = str(item).strip().lstrip("#").strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def tags_length(tags: list[str]) -> int:
    """YouTube's count: commas between tags count, a tag with spaces counts as if quoted (+2)."""
    if not tags:
        return 0
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1


def privacy_for(target: TargetSpec, account_meta: Mapping[str, Any] | None = None) -> str:
    raw = str(_options(target).get("privacy_status") or "").strip().lower()
    if raw in _PRIVACY_ALIASES:
        return _PRIVACY_ALIASES[raw]
    return "public" if automatic_publishing_allowed(account_meta) else "private"


def _the_video(target: TargetSpec) -> MediaInfo | None:
    for m in target.media:
        if m.kind == "video":
            return m
    return None


def check_target(target: TargetSpec, account_meta: Mapping[str, Any] | None = None) -> list[Issue]:
    """YouTube-specific checks (report 06 §13). Used as the publish-time pre-check and as a fallback for
    :meth:`YouTubeProvider.validate` while ``rules`` has no YouTube rules."""
    issues: list[Issue] = []
    opts = _options(target)
    videos = [m for m in target.media if m.kind == "video"]
    if len(videos) != 1:
        issues.append(Issue("error", "youtube_one_video", "YouTube necesita exactamente un vídeo.", "media"))
    title, explicit = title_for(target)
    if not title:
        issues.append(
            Issue("error", "youtube_title_required", "Escribe el título del vídeo de YouTube.", "title")
        )
    elif explicit:
        if len(title) > TITLE_MAX:
            issues.append(
                Issue(
                    "error",
                    "youtube_title_too_long",
                    f"El título de YouTube tiene {len(title)} caracteres (máximo {TITLE_MAX}).",
                    "title",
                )
            )
        if "<" in title or ">" in title:
            issues.append(
                Issue(
                    "error", "youtube_title_chars", "El título de YouTube no puede contener < ni >.", "title"
                )
            )
    description = target.text or ""
    size = len(description.encode("utf-8"))
    if size > DESCRIPTION_MAX_BYTES:
        issues.append(
            Issue(
                "error",
                "youtube_description_too_long",
                f"La descripción de YouTube ocupa {size} bytes (máximo {DESCRIPTION_MAX_BYTES}).",
                "text",
            )
        )
    if "<" in description or ">" in description:
        issues.append(
            Issue(
                "error",
                "youtube_description_chars",
                "La descripción de YouTube no puede contener < ni >.",
                "text",
            )
        )
    total_tags = tags_length(tags_for(target))
    if total_tags > TAGS_MAX:
        issues.append(
            Issue(
                "error",
                "youtube_tags_too_long",
                f"Las etiquetas de YouTube suman {total_tags} caracteres (máximo {TAGS_MAX}).",
                "tags",
            )
        )
    if _tri_bool(opts.get("made_for_kids")) is None:
        issues.append(
            Issue(
                "error",
                "youtube_made_for_kids_required",
                "Indica si el vídeo es contenido creado para niños (obligatorio en YouTube).",
                "made_for_kids",
            )
        )
    raw_privacy = str(opts.get("privacy_status") or "").strip().lower()
    if raw_privacy and raw_privacy not in _PRIVACY_ALIASES:
        issues.append(
            Issue("error", "youtube_privacy", "Elige la visibilidad del vídeo de YouTube.", "privacy_status")
        )
    video = videos[0] if videos else None
    if video is not None:
        duration = video.duration_s
        if target.format == "short":
            if duration is not None and duration > SHORT_MAX_S:
                issues.append(
                    Issue(
                        "error",
                        "youtube_short_too_long",
                        "Un Short puede durar como máximo 3 minutos.",
                        "media",
                    )
                )
            if video.width and video.height and video.height < video.width:
                issues.append(
                    Issue("error", "youtube_short_aspect", "Un Short debe ser vertical o cuadrado.", "media")
                )
        elif duration is not None and duration > LONG_UPLOAD_S:
            status = (account_meta or {}).get("long_uploads_status")
            if status and status != "allowed":
                issues.append(
                    Issue(
                        "error",
                        "youtube_long_upload",
                        "Tu canal solo permite vídeos de hasta 15 min; verifica tu canal en "
                        "youtube.com/verify.",
                        "media",
                    )
                )
    return issues


def build_upload_plan(
    target: TargetSpec, *, now: datetime, account_meta: Mapping[str, Any] | None = None
) -> UploadPlan:
    """Video resource for ``videos.insert``. Privacy is **always** sent explicitly (omitted ⇒ public); with
    ``native_schedule`` and a future ``scheduled_at`` the video goes up private with ``publishAt``.
    Raises ``ProviderError(PERMANENT)`` for metadata YouTube would reject (never burns an upload call)."""
    errors = [i for i in check_target(target, account_meta) if i.level == "error"]
    if errors:
        raise ProviderError(
            ErrorKind.PERMANENT,
            errors[0].code,
            errors[0].message,
            detail={"issues": [i.code for i in errors]},
        )
    opts = _options(target)
    title, _ = title_for(target)
    privacy = privacy_for(target, account_meta)
    snippet: dict[str, Any] = {
        "title": title,
        "description": target.text or "",
        "categoryId": str(opts.get("category_id") or DEFAULT_CATEGORY),
    }
    tags = tags_for(target)
    if tags:
        snippet["tags"] = tags
    language = str(opts.get("language") or "").strip()
    if language:
        snippet["defaultLanguage"] = language
        snippet["defaultAudioLanguage"] = language
    license_key = str(opts.get("license") or "youtube").strip().lower().replace(" ", "")
    status: dict[str, Any] = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": bool(_tri_bool(opts.get("made_for_kids"))),
        "containsSyntheticMedia": _as_bool(opts.get("synthetic_media"), False),
        "embeddable": _as_bool(opts.get("embeddable"), True),
        "license": _LICENSES.get(license_key, "youtube"),
    }
    native = False
    publish_at: str | None = None
    if (
        _as_bool(opts.get("native_schedule"), False)
        and privacy == "public"
        and target.scheduled_at is not None
        and target.scheduled_at - now >= NATIVE_SCHEDULE_MIN_LEAD
    ):
        native = True
        publish_at = rfc3339(target.scheduled_at)
        status["privacyStatus"] = "private"  # publishAt requires private; YouTube flips it to public
        status["publishAt"] = publish_at
    body = {
        "snippet": snippet,
        "status": status,
        "paidProductPlacementDetails": {
            "hasPaidProductPlacement": _as_bool(opts.get("paid_promotion"), False)
        },
    }
    return UploadPlan(
        body=body,
        parts=["snippet", "status", "paidProductPlacementDetails"],
        notify_subscribers=_as_bool(opts.get("notify_subscribers"), True),
        requested_privacy=privacy,
        native_scheduled=native,
        publish_at=publish_at,
        title=title,
    )


def _video_mime(video: MediaInfo) -> str:
    mime = (video.mime or "").lower()
    return mime if mime.startswith("video/") else "application/octet-stream"


def _start_session(api: _Api, plan: UploadPlan, size: int, mime: str) -> str:
    resp = api.request(
        "POST",
        UPLOAD_VIDEOS_URL,
        op="upload_init",
        params={
            "uploadType": "resumable",
            "part": ",".join(plan.parts),
            "notifySubscribers": "true" if plan.notify_subscribers else "false",
        },
        json_body=plan.body,
        headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime},
        ok=(200, 201),
    )
    location = resp.headers.get("Location")
    if not location:
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "youtube_no_session",
            "YouTube no devolvió la dirección de subida; se reintentará.",
            detail={"http_status": resp.status_code},
        )
    return location


def _query_session(api: _Api, session_uri: str, total: int) -> tuple[str, Any]:
    """Status query (report 06 §4.5) → ``("complete", video)`` | ``("incomplete", (offset, retry_after))``
    | ``("gone", None)``."""
    resp = api.request(
        "PUT",
        session_uri,
        op="upload_status",
        content=b"",
        headers={"Content-Range": f"bytes */{total}", "Content-Length": "0"},
        ok=(200, 201, 308, 404, 410),
    )
    if resp.status_code in (200, 201):
        body = _json_or_none(resp)
        return "complete", body if isinstance(body, dict) else {}
    if resp.status_code in (404, 410):
        return "gone", None
    return "incomplete", (next_offset_from_range(resp.headers.get("Range")), _retry_after(resp.headers))


def _put_chunk(
    api: _Api, session_uri: str, path: Path, offset: int, length: int, total: int, mime: str
) -> httpx.Response:
    return api.request(
        "PUT",
        session_uri,
        op="upload_chunk",
        content=lambda: _iter_file(path, offset, length),
        headers={
            "Content-Type": mime,
            "Content-Length": str(length),
            "Content-Range": f"bytes {offset}-{offset + length - 1}/{total}",
        },
        timeout=UPLOAD_TIMEOUT,
        ok=(200, 201, 308, 404, 410),
    )


# ------------------------------------------------------------------------------------- thumbnails


def prepare_thumbnail(path: Path, mime: str | None) -> tuple[bytes, str]:
    """JPEG/PNG ≤ 2 MB as-is; anything else (or bigger) re-encoded to JPEG, long side ≤ 1280 px."""
    size = path.stat().st_size
    kind = (mime or "").lower()
    if kind in ("image/jpeg", "image/png") and size <= THUMB_MAX_BYTES:
        return path.read_bytes(), kind
    from PIL import Image, ImageOps

    with Image.open(path) as src:
        img = ImageOps.exif_transpose(src).convert("RGB")
    img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))
    data = b""
    for quality in (90, 80, 70, 60, 50, 40):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= THUMB_MAX_BYTES:
            break
    return data, "image/jpeg"


def _lookup_media_file(media_id: int) -> tuple[Path | None, str | None]:
    """Path + mime of a library asset (used for ``thumbnail_media_id``). Best effort, lazy imports."""
    try:
        from planificador import db as dbmod
        from planificador.models import MediaAsset

        session = dbmod.SessionLocal()
    except Exception:
        log.warning("No database available to resolve thumbnail media %s", media_id)
        return None, None
    try:
        asset = session.get(MediaAsset, media_id)
        if asset is None or asset.deleted_at is not None:
            return None, None
        path: Path | None = None
        try:
            from planificador import media as media_mod

            asset_path = getattr(media_mod, "asset_path", None)
            if callable(asset_path):
                path = Path(asset_path(asset))
        except Exception:
            path = None
        if path is None:
            from planificador.config import get_app_settings

            path = get_app_settings().paths.media_dir / asset.rel_path
        return path, asset.mime
    except Exception:
        log.exception("Could not resolve thumbnail media %s", media_id)
        return None, None
    finally:
        session.close()


# ------------------------------------------------------------------------------------ the provider


class YouTubeProvider(Provider):
    network = "youtube"
    display_name = "YouTube"
    formats = [
        FormatSpec("video", "Vídeo", "video"),
        FormatSpec("short", "Short", "video"),
    ]
    supports_comments = True
    supports_analytics = True

    option_specs = [
        OptionSpec("title", "Título", "text", None, help="Obligatorio, máximo 100 caracteres, sin < ni >."),
        OptionSpec("privacy_status", "Visibilidad", "select", "public", choices=list(PRIVACY_LABELS.items())),
        OptionSpec(
            "native_schedule",
            "Programar en YouTube (se publica aunque el PC esté apagado)",
            "bool",
            False,
            help="Se sube ahora como privado y YouTube lo hará público a la hora indicada.",
        ),
        OptionSpec("category_id", "Categoría", "select", DEFAULT_CATEGORY),
        OptionSpec("tags", "Etiquetas", "list", None, help="Máximo 500 caracteres en total."),
        OptionSpec(
            "made_for_kids",
            "Audiencia: ¿es contenido creado para niños?",
            "select",
            None,
            choices=[
                ("true", "Sí, es contenido creado para niños"),
                ("false", "No, no es contenido creado para niños"),
            ],
            help="Obligatorio por ley (COPPA). Si es para niños se desactivan los comentarios y otras "
            "funciones.",
        ),
        OptionSpec(
            "synthetic_media",
            "Contenido alterado o sintético (realista, generado o modificado con IA)",
            "bool",
            False,
        ),
        OptionSpec(
            "paid_promotion", "Incluye promoción pagada (product placement, patrocinio)", "bool", False
        ),
        OptionSpec("notify_subscribers", "Notificar a los suscriptores", "bool", True),
        OptionSpec(
            "thumbnail_media_id",
            "Miniatura personalizada (opcional)",
            "number",
            None,
            help="Requiere canal verificado. JPG/PNG, 1280×720 recomendado.",
            formats=["video"],
        ),
        OptionSpec("playlist_id", "Añadir a lista de reproducción (opcional)", "select", None),
        OptionSpec("first_comment", "Primer comentario (opcional)", "text", None),
        OptionSpec("language", "Idioma del vídeo", "text", None),
        OptionSpec("embeddable", "Permitir insertar el vídeo en otras webs", "bool", True),
        OptionSpec(
            "license",
            "Licencia",
            "select",
            "youtube",
            choices=[
                ("youtube", "Licencia estándar de YouTube"),
                ("creativeCommon", "Creative Commons - Atribución"),
            ],
        ),
    ]

    def __init__(self) -> None:
        # Upload sessions whose offset was confirmed by the server in *this* process. After a restart the
        # first step for a session always asks YouTube where it stands (server Range beats local state).
        self._verified_sessions: set[str] = set()

    # ------------------------------------------------------------------------------- validation

    def validate(self, target: TargetSpec) -> list[Issue]:
        try:
            from planificador import rules

            return rules.validate_target(target)
        except NotImplementedError:
            return check_target(target)

    # ------------------------------------------------------------------------------- publishing

    def _api(self, ctx: PublishContext) -> _Api:
        hook = getattr(ctx, "update_tokens", None)
        return _Api(ctx.account, ctx.settings, ctx.http, ctx.clock, hook if callable(hook) else None)

    def publish_step(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        phase = st.get("phase")
        if phase == "done":
            return self._final_result(ctx)
        if phase == "failed":
            self._reset_for_retry(ctx)
        if st.get("video_id"):
            if st.get("phase") == "processing":
                return self._poll(ctx)
            return self._post_upload(ctx)
        if st.get("session_uri"):
            return self._upload(ctx)
        return self._init(ctx)

    def _reset_for_retry(self, ctx: PublishContext) -> None:
        """YouTube rejected/failed the previous upload and the user asked to retry: start a new upload."""
        st = ctx.state
        old = st.get("video_id")
        restarts = st.get("restarts")
        if st.get("session_uri"):
            self._verified_sessions.discard(str(st["session_uri"]))
        st.clear()
        if restarts:
            st["restarts"] = restarts
        ctx.save_state(st)
        ctx.log(
            "youtube_retry_new_upload",
            "YouTube no aceptó el vídeo anterior; se subirá de nuevo.",
            {"previous_video_id": old},
        )

    def _video_file(self, ctx: PublishContext) -> tuple[MediaInfo, Path]:
        video = _the_video(ctx.target)
        if video is None:
            raise ProviderError(
                ErrorKind.PERMANENT, "youtube_one_video", "YouTube necesita exactamente un vídeo."
            )
        path = Path(video.path)
        if not path.is_file():
            raise ProviderError(
                ErrorKind.PERMANENT,
                "youtube_file_missing",
                "No se encuentra el archivo de vídeo en la biblioteca; vuelve a añadirlo a la publicación.",
                detail={"media_id": video.id},
            )
        return video, path

    # -- step: start the resumable session ------------------------------------------------------------

    def _init(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        now = ctx.clock.now()
        video, path = self._video_file(ctx)
        if not has_feature(granted_scopes(ctx.account), "upload"):
            raise ProviderError(ErrorKind.AUTH, "youtube_missing_upload_scope", MSG_SCOPE)
        plan = build_upload_plan(ctx.target, now=now, account_meta=ctx.account.meta)
        due_in = (ctx.target.scheduled_at - now).total_seconds() if ctx.target.scheduled_at else 0.0
        if not plan.native_scheduled and due_in > EARLY_START_TOLERANCE_S:
            # Claimed ahead of time for native scheduling, but YouTube cannot schedule this one (not public,
            # or too close): never upload before the scheduled time.
            return StepResult(
                "wait",
                wait_seconds=max(1, math.ceil(due_in)),
                message="Se subirá a YouTube a la hora programada.",
            )
        size = path.stat().st_size
        if size <= 0:
            raise ProviderError(ErrorKind.PERMANENT, "youtube_empty_file", "El archivo de vídeo está vacío.")
        mime = _video_mime(video)
        session_uri = _start_session(self._api(ctx), plan, size, mime)
        st.update(
            phase="uploading",
            session_uri=session_uri,
            session_started_at=_iso(now),
            total_bytes=size,
            uploaded_bytes=0,
            mime=mime,
            media_id=video.id,
            title=plan.title,
            requested_privacy=plan.requested_privacy,
            native_scheduled=plan.native_scheduled,
            publish_at=plan.publish_at,
            commit_sent=False,
            chunk_failures=0,
            stalls=0,
            resync=False,
        )
        ctx.save_state(st)
        self._verified_sessions.add(session_uri)
        privacy_label = PRIVACY_LABELS.get(plan.requested_privacy, plan.requested_privacy)
        msg = f"Subida a YouTube iniciada ({size / (1024 * 1024):.1f} MB, visibilidad: {privacy_label})."
        if plan.native_scheduled:
            msg += " Se subirá como privado y YouTube lo publicará a la hora programada."
        ctx.log("youtube_upload_started", msg, {"total_bytes": size, "privacy": plan.requested_privacy})
        return StepResult("continue")

    # -- step: upload chunks ---------------------------------------------------------------------------

    def _apply_incomplete(self, ctx: PublishContext, offset: int) -> None:
        """The server answered 308 to a status query: it stores ``offset`` bytes and **no video exists**, so
        the commit point has not been passed (it is passed again when the final chunk is re-sent)."""
        st = ctx.state
        total = int(st.get("total_bytes") or 0)
        st["uploaded_bytes"] = min(offset, total)
        st["resync"] = False
        if offset < total:
            st["commit_sent"] = False
        ctx.save_state(st)

    def _upload(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        api = self._api(ctx)
        session_uri = str(st["session_uri"])
        video, path = self._video_file(ctx)
        total = int(st.get("total_bytes") or 0) or path.stat().st_size
        st["total_bytes"] = total
        mime = str(st.get("mime") or "application/octet-stream")
        changed = (st.get("media_id") is not None and st.get("media_id") != video.id) or (
            path.stat().st_size != total
        )
        if changed and not st.get("commit_sent"):
            return self._restart_session(
                ctx, "El vídeo de la publicación ha cambiado; se vuelve a empezar la subida."
            )
        started = _parse_iso(st.get("session_started_at"))
        if started is not None and ctx.clock.now() - started > SESSION_MAX_AGE and not st.get("commit_sent"):
            return self._restart_session(
                ctx, "La sesión de subida de YouTube caducó; se vuelve a empezar la subida."
            )

        if session_uri not in self._verified_sessions or st.get("resync"):
            try:
                outcome, value = _query_session(api, session_uri, total)
            except ProviderError as err:
                return self._chunk_failed(ctx, api, err, final=False)
            if outcome == "complete":
                return self._on_uploaded(ctx, value)
            if outcome == "gone":
                return self._session_gone(ctx)
            offset, retry_after = value
            self._apply_incomplete(ctx, offset)
            self._verified_sessions.add(session_uri)
            if retry_after:
                return StepResult("wait", wait_seconds=retry_after)

        deadline = _monotonic() + STEP_TIME_BUDGET_S
        sent = 0
        chunk = _chunk_size()
        while True:
            offset = int(st.get("uploaded_bytes") or 0)
            if offset >= total:
                # The server holds every byte but we never saw its final answer: ask for it.
                try:
                    outcome, value = _query_session(api, session_uri, total)
                except ProviderError as err:
                    return self._chunk_failed(ctx, api, err, final=True)
                if outcome == "complete":
                    return self._on_uploaded(ctx, value)
                if outcome == "gone":
                    return self._session_gone(ctx)
                if value[0] >= total:
                    raise ProviderError(
                        ErrorKind.AMBIGUOUS,
                        "youtube_upload_unconfirmed",
                        "YouTube recibió todo el vídeo pero no confirmó la subida; se comprobará en tu "
                        "canal.",
                    )
                self._apply_incomplete(ctx, value[0])
                continue
            length = min(chunk, total - offset)
            final = offset + length >= total
            if final:
                st["commit_sent"] = True
                st["final_chunk_at"] = _iso(ctx.clock.now())
                ctx.before_commit(st)
            try:
                resp = _put_chunk(api, session_uri, path, offset, length, total, mime)
            except ProviderError as err:
                return self._chunk_failed(ctx, api, err, final=final)
            if resp.status_code in (200, 201):
                body = _json_or_none(resp)
                return self._on_uploaded(ctx, body if isinstance(body, dict) else {})
            if resp.status_code in (404, 410):
                return self._session_gone(ctx)
            new_offset = min(next_offset_from_range(resp.headers.get("Range")), total)
            if new_offset > offset:
                st["chunk_failures"] = 0
                st["stalls"] = 0
            else:
                st["stalls"] = int(st.get("stalls") or 0) + 1
            st["uploaded_bytes"] = new_offset
            ctx.save_state(st)
            self._log_progress(ctx, new_offset, total)
            if int(st.get("stalls") or 0) > STALL_LIMIT:
                st["stalls"] = 0
                st["resync"] = True
                ctx.save_state(st)
                raise ProviderError(
                    ErrorKind.TRANSIENT,
                    "youtube_upload_stalled",
                    "YouTube no está aceptando los datos del vídeo; se reintentará más tarde.",
                    detail={"offset": new_offset, "total": total},
                )
            retry_after = _retry_after(resp.headers)
            if retry_after:
                return StepResult("wait", wait_seconds=retry_after)
            sent += 1
            if sent >= CHUNKS_PER_STEP or _monotonic() >= deadline:
                return StepResult("continue")

    def _log_progress(self, ctx: PublishContext, offset: int, total: int) -> None:
        st = ctx.state
        pct = int(offset * 100 / total) if total else 0
        logged = int(st.get("progress_logged") or 0)
        milestone = max((m for m in (25, 50, 75) if pct >= m), default=0)
        if milestone > logged:
            st["progress_logged"] = milestone
            ctx.save_state(st)
            mb_done, mb_total = offset / (1024 * 1024), total / (1024 * 1024)
            ctx.log(
                "youtube_upload_progress",
                f"Subida a YouTube: {milestone} % ({mb_done:.1f} de {mb_total:.1f} MB).",
                {"uploaded_bytes": offset, "total_bytes": total},
            )

    def _chunk_failed(self, ctx: PublishContext, api: _Api, err: ProviderError, *, final: bool) -> StepResult:
        """A chunk PUT (or a status query) failed. Before the final chunk this is harmless: wait and resume
        from the server's offset. On the final chunk the video may already exist → ask the server first."""
        st = ctx.state
        st["resync"] = True
        ctx.save_state(st)
        if err.kind == ErrorKind.AUTH:
            raise err  # a 401/403 answer (or no usable token): YouTube refused the bytes, nothing was created
        if final:
            try:
                outcome, value = _query_session(api, str(st["session_uri"]), int(st["total_bytes"]))
            except ProviderError as qerr:
                raise ProviderError(
                    ErrorKind.AMBIGUOUS,
                    "youtube_upload_ambiguous",
                    "No se pudo confirmar si YouTube recibió el vídeo completo; se comprobará antes de "
                    "reintentar.",
                    detail={"chunk_error": err.to_dict(), "query_error": qerr.to_dict()},
                ) from err
            if outcome == "complete":
                return self._on_uploaded(ctx, value)
            if outcome == "gone":
                return self._session_gone(ctx)
            self._apply_incomplete(ctx, value[0])
            if err.kind == ErrorKind.PERMANENT:
                raise err  # the server refused the bytes and kept nothing: a real rejection
        if err.kind not in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED):
            raise err
        failures = int(st.get("chunk_failures") or 0) + 1
        if failures > len(CHUNK_RETRY_DELAYS):
            st["chunk_failures"] = 0
            ctx.save_state(st)
            raise err
        st["chunk_failures"] = failures
        ctx.save_state(st)
        delay = err.retry_after or CHUNK_RETRY_DELAYS[failures - 1]
        ctx.log(
            "youtube_upload_retry",
            f"Fallo temporal al subir a YouTube ({err.message}); se reanudará en {delay} s.",
            {"code": err.code, "attempt": failures},
        )
        return StepResult("wait", wait_seconds=delay)

    def _session_gone(self, ctx: PublishContext) -> StepResult:
        if ctx.state.get("commit_sent"):
            raise ProviderError(
                ErrorKind.AMBIGUOUS,
                "youtube_session_gone",
                "La sesión de subida de YouTube ya no existe y no se sabe si el vídeo llegó completo; se "
                "comprobará en tu canal.",
            )
        return self._restart_session(
            ctx, "La sesión de subida de YouTube caducó; se vuelve a empezar la subida."
        )

    def _restart_session(self, ctx: PublishContext, message: str) -> StepResult:
        st = ctx.state
        restarts = int(st.get("restarts") or 0) + 1
        if restarts > SESSION_RESTART_LIMIT:
            raise ProviderError(
                ErrorKind.TRANSIENT,
                "youtube_session_restarts",
                "YouTube cerró varias veces la sesión de subida; se reintentará más tarde.",
            )
        old = st.get("session_uri")
        if old:
            self._verified_sessions.discard(str(old))
        for key in (
            "session_uri",
            "session_started_at",
            "uploaded_bytes",
            "commit_sent",
            "final_chunk_at",
            "chunk_failures",
            "stalls",
            "resync",
            "progress_logged",
        ):
            st.pop(key, None)
        st["restarts"] = restarts
        st["phase"] = "init"
        ctx.save_state(st)
        ctx.log("youtube_session_restarted", message, {"restarts": restarts})
        return StepResult("continue")

    def _on_uploaded(self, ctx: PublishContext, video: Mapping[str, Any]) -> StepResult:
        st = ctx.state
        video_id = video.get("id")
        if not video_id:
            raise ProviderError(
                ErrorKind.AMBIGUOUS,
                "youtube_upload_no_id",
                "YouTube terminó la subida pero no devolvió el identificador del vídeo; se comprobará en tu "
                "canal.",
            )
        status = video.get("status") or {}
        st.update(
            video_id=str(video_id),
            returned_privacy=status.get("privacyStatus"),
            upload_status=status.get("uploadStatus"),
            uploaded_bytes=int(st.get("total_bytes") or 0),
            resync=False,
            phase="post",
        )
        self._check_lock(ctx, video)
        ctx.save_state(st)
        session = st.get("session_uri")
        if session:
            self._verified_sessions.discard(str(session))
        ctx.log(
            "youtube_uploaded",
            f"Vídeo subido a YouTube (id {video_id}).",
            {"video_id": str(video_id), "privacy": status.get("privacyStatus")},
        )
        return StepResult("continue")

    def _check_lock(
        self, ctx: PublishContext, video: Mapping[str, Any], *, after_processing: bool = False
    ) -> None:
        st = ctx.state
        requested = st.get("requested_privacy")
        if detect_lock(video, requested, now=ctx.clock.now()):
            if not st.get("lock_detected"):
                st["lock_detected"] = True
                st[STATE_ACCOUNT_META] = {
                    META_LOCK_STATUS: "locked",
                    "upload_lock_suspected": True,
                    META_PUBLISH_MODE: "assisted",
                }
                ctx.log(
                    "youtube_lock_detected",
                    MSG_LOCKED,
                    {
                        "requested_privacy": requested,
                        "returned_privacy": "private",
                        STATE_ACCOUNT_META: st[STATE_ACCOUNT_META],
                    },
                )
            return
        privacy = (video.get("status") or {}).get("privacyStatus")
        if (
            after_processing
            and not st.get("native_scheduled")
            and requested in ("public", "unlisted")
            and privacy == requested
        ):
            st["lock_detected"] = False
            st[STATE_ACCOUNT_META] = {META_LOCK_STATUS: "unlocked", "upload_lock_suspected": False}

    # -- step: post-upload extras (idempotent) ----------------------------------------------------------

    def _post_upload(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        api = self._api(ctx)
        opts = _options(ctx.target)
        video_id = str(st["video_id"])
        if st.get("phase") != "post":
            st["phase"] = "post"
            ctx.save_state(st)

        if opts.get("thumbnail_media_id"):
            res = self._run_extra(
                ctx,
                "thumbnail",
                "no se pudo aplicar la miniatura",
                lambda: self._do_thumbnail(ctx, api, video_id),
            )
            if res is not None:
                return res
        if opts.get("playlist_id"):
            playlist_id = str(opts["playlist_id"])
            res = self._run_extra(
                ctx,
                "playlist",
                "no se pudo añadir a la lista de reproducción",
                lambda: self._do_playlist(ctx, api, video_id, playlist_id),
            )
            if res is not None:
                return res
        first_comment = str(opts.get("first_comment") or "").strip()
        if first_comment:
            res = self._run_extra(
                ctx,
                "first_comment",
                "no se pudo publicar el primer comentario",
                lambda: self._do_first_comment(ctx, api, video_id, first_comment),
            )
            if res is not None:
                return res

        if st.get("native_scheduled"):
            return self._finish(
                ctx,
                "Programado en YouTube: se subió como privado y YouTube lo hará público a la hora indicada, "
                "aunque el PC esté apagado.",
            )
        st["phase"] = "processing"
        st["processing_started_at"] = _iso(ctx.clock.now())
        st["poll_count"] = 0
        ctx.save_state(st)
        return StepResult("wait", wait_seconds=POLL_DELAYS[0], message="YouTube está procesando el vídeo.")

    def _run_extra(
        self, ctx: PublishContext, key: str, failure_es: str, fn: Callable[[], None]
    ) -> StepResult | None:
        st = ctx.state
        if st.get(f"{key}_done"):
            return None
        try:
            fn()
        except ProviderError as err:
            tries = int(st.get(f"{key}_errors") or 0) + 1
            st[f"{key}_errors"] = tries
            if err.kind == ErrorKind.TRANSIENT and tries <= EXTRA_RETRY_LIMIT:
                ctx.save_state(st)
                return StepResult("wait", wait_seconds=err.retry_after or EXTRA_RETRY_WAIT)
            st[f"{key}_error"] = err.message
            ctx.log(
                f"youtube_{key}_failed",
                f"El vídeo está en YouTube, pero {failure_es}: {err.message}",
                {"code": err.code, "detail": err.detail},
            )
        st[f"{key}_done"] = True
        ctx.save_state(st)
        return None

    def _thumbnail_file(self, ctx: PublishContext) -> tuple[Path, str | None]:
        media_id = _int_or_none(_options(ctx.target).get("thumbnail_media_id"))
        if media_id is None:
            raise ProviderError(
                ErrorKind.PERMANENT, "youtube_thumbnail_invalid", "La miniatura elegida no es válida."
            )
        for m in ctx.target.media:
            if m.id == media_id and m.kind == "image":
                return Path(m.path), m.mime
        path, mime = _lookup_media_file(media_id)
        if path is None or not path.is_file():
            raise ProviderError(
                ErrorKind.PERMANENT,
                "youtube_thumbnail_missing",
                "No se encuentra la imagen de la miniatura en la biblioteca.",
                detail={"media_id": media_id},
            )
        return path, mime

    def _do_thumbnail(self, ctx: PublishContext, api: _Api, video_id: str) -> None:
        path, mime = self._thumbnail_file(ctx)
        try:
            data, content_type = prepare_thumbnail(path, mime)
        except Exception as exc:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "youtube_thumbnail_unreadable",
                "No se pudo preparar la imagen de la miniatura.",
                detail={"error": type(exc).__name__},
            ) from exc
        api.request(
            "POST",
            THUMBNAILS_URL,
            op="thumbnail",
            params={"videoId": video_id, "uploadType": "media"},
            content=data,
            headers={"Content-Type": content_type, "Content-Length": str(len(data))},
            timeout=UPLOAD_TIMEOUT,
        )
        ctx.state["thumbnail_applied"] = True
        ctx.log("youtube_thumbnail_set", "Miniatura personalizada aplicada.", None)

    def _do_playlist(self, ctx: PublishContext, api: _Api, video_id: str, playlist_id: str) -> None:
        st = ctx.state
        if st.get("playlist_attempted"):
            # A previous attempt may have succeeded right before a crash: check before inserting again.
            body = api.get(
                f"{API_BASE}/playlistItems",
                {"part": "id", "playlistId": playlist_id, "videoId": video_id, "maxResults": 5},
                op="playlist",
            )
            if body.get("items"):
                st["playlist_applied"] = True
                return
        st["playlist_attempted"] = True
        ctx.save_state(st)
        api.request(
            "POST",
            f"{API_BASE}/playlistItems",
            op="playlist",
            params={"part": "snippet"},
            json_body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )
        st["playlist_applied"] = True
        ctx.log(
            "youtube_playlist_added",
            "Vídeo añadido a la lista de reproducción.",
            {"playlist_id": playlist_id},
        )

    def _do_first_comment(self, ctx: PublishContext, api: _Api, video_id: str, text: str) -> None:
        st = ctx.state
        skip: str | None = None
        if _tri_bool(_options(ctx.target).get("made_for_kids")):
            skip = "los vídeos creados para niños no admiten comentarios"
        elif st.get("native_scheduled"):
            skip = "el vídeo está programado y aún es privado; añade el comentario cuando sea público"
        elif st.get("lock_detected") or st.get("returned_privacy") == "private":
            skip = "el vídeo es privado"
        if skip:
            st["first_comment_skipped"] = True
            ctx.log("youtube_first_comment_skipped", f"No se publicó el primer comentario: {skip}.", None)
            return
        if st.get("first_comment_attempted"):
            existing = self._find_own_comment(api, ctx.account.external_id, video_id, text)
            if existing:
                st["first_comment_id"] = existing
                return
        st["first_comment_attempted"] = True
        ctx.save_state(st)
        resp = api.request(
            "POST",
            f"{API_BASE}/commentThreads",
            op="comment",
            params={"part": "snippet"},
            json_body={
                "snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}
            },
        )
        body = _json_or_none(resp)
        st["first_comment_id"] = body.get("id") if isinstance(body, dict) else None
        ctx.log(
            "youtube_first_comment",
            "Primer comentario publicado en YouTube.",
            {"comment_id": st["first_comment_id"]},
        )

    def _find_own_comment(self, api: _Api, channel_id: str, video_id: str, text: str) -> str | None:
        body = api.get(
            f"{API_BASE}/commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": 50,
                "order": "time",
                "textFormat": "plainText",
            },
            op="comment",
        )
        for thread in body.get("items") or []:
            top = (thread.get("snippet") or {}).get("topLevelComment") or {}
            snippet = top.get("snippet") or {}
            author = (snippet.get("authorChannelId") or {}).get("value")
            content = (snippet.get("textOriginal") or snippet.get("textDisplay") or "").strip()
            if author == channel_id and content == text.strip():
                return str(top.get("id") or thread.get("id"))
        return None

    # -- step: processing ------------------------------------------------------------------------------

    def _poll(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        api = self._api(ctx)
        video_id = str(st["video_id"])
        now = ctx.clock.now()
        started = _parse_iso(st.get("processing_started_at")) or now
        try:
            items = _videos(api, [video_id], "status,processingDetails", op="processing")
        except ProviderError as err:
            errors = int(st.get("poll_errors") or 0) + 1
            st["poll_errors"] = errors
            ctx.save_state(st)
            if errors >= POLL_ERROR_LIMIT or now - started >= POLL_GIVE_UP:
                return self._finish(
                    ctx,
                    f"Vídeo subido a YouTube, pero no se pudo confirmar el procesamiento ({err.message}). "
                    "Revísalo en YouTube Studio.",
                )
            return StepResult("wait", wait_seconds=err.retry_after or 60)
        count = int(st.get("poll_count") or 0) + 1
        st["poll_count"] = count
        if items:
            video = items[0]
            status = video.get("status") or {}
            processing = video.get("processingDetails") or {}
            upload_status = status.get("uploadStatus")
            proc_status = processing.get("processingStatus")
            st["returned_privacy"] = status.get("privacyStatus")
            st["upload_status"] = upload_status
            if upload_status in ("failed", "rejected", "deleted") or proc_status in ("failed", "terminated"):
                st["phase"] = "failed"
                ctx.save_state(st)
                raise processing_error(video)
            if upload_status == "processed" or proc_status == "succeeded":
                self._check_lock(ctx, video, after_processing=True)
                ctx.save_state(st)
                return self._finish(ctx, None)
        if now - started >= POLL_GIVE_UP:
            return self._finish(
                ctx, "Vídeo subido a YouTube; YouTube sigue procesándolo y aparecerá cuando termine."
            )
        ctx.save_state(st)
        delay = POLL_DELAYS[count] if count < len(POLL_DELAYS) else POLL_INTERVAL
        return StepResult("wait", wait_seconds=delay)

    def _finish(self, ctx: PublishContext, message: str | None) -> StepResult:
        st = ctx.state
        video_id = str(st["video_id"])
        url = watch_url(video_id)
        if message is None:
            privacy = st.get("returned_privacy") or st.get("requested_privacy")
            if privacy == "unlisted":
                message = "Subido a YouTube como oculto (no listado)."
            elif privacy == "private":
                message = "Subido a YouTube como privado."
            else:
                message = "Publicado en YouTube."
        if st.get("lock_detected"):
            message = MSG_LOCKED
        st.update(
            phase="done", result="published", external_id=video_id, external_url=url, final_message=message
        )
        ctx.save_state(st)
        return StepResult("published", external_id=video_id, external_url=url, message=message)

    def _final_result(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        video_id = str(st.get("external_id") or st.get("video_id") or "")
        return StepResult(
            "published",
            external_id=video_id or None,
            external_url=st.get("external_url") or (watch_url(video_id) if video_id else None),
            message=st.get("final_message"),
        )

    # ------------------------------------------------------------------------------- reconcile

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        """Decide from the saved state + the server (never re-sends): video id → ``videos.list``; session →
        status query (200/201 published, 308 not published, 404/410 → uploads playlist by title)."""
        st = ctx.state
        if st.get("phase") == "done" and st.get("video_id"):
            vid = str(st["video_id"])
            return ReconcileResult(
                "published", external_id=vid, external_url=watch_url(vid), message=st.get("final_message")
            )
        api = self._api(ctx)
        video_id = st.get("video_id")
        if video_id:
            try:
                items = _videos(api, [str(video_id)], "status,processingDetails", op="reconcile")
            except ProviderError as err:
                return ReconcileResult(
                    "unknown",
                    message=f"No se pudo consultar YouTube ({err.message}). Revisa tu canal en YouTube "
                    "Studio.",
                )
            if items:
                status = items[0].get("status") or {}
                processing = items[0].get("processingDetails") or {}
                if status.get("uploadStatus") in ("failed", "rejected", "deleted") or processing.get(
                    "processingStatus"
                ) in ("failed", "terminated"):
                    reason = processing_error(items[0]).message
                    return ReconcileResult("unknown", message=f"{reason} Revisa el vídeo en YouTube Studio.")
                return ReconcileResult(
                    "published",
                    external_id=str(video_id),
                    external_url=watch_url(str(video_id)),
                    message="YouTube confirma que el vídeo existe." + self._extras_note(ctx),
                )
            return ReconcileResult(
                "unknown",
                message="El vídeo subido ya no aparece en YouTube (¿se eliminó?). Revisa YouTube Studio.",
            )
        session_uri = st.get("session_uri")
        if not session_uri:
            return ReconcileResult(
                "not_published", message="YouTube no llegó a recibir el vídeo; se puede reintentar."
            )
        total = int(st.get("total_bytes") or 0)
        outcome: str | None = None
        value: Any = None
        last_error: ProviderError | None = None
        for attempt in range(2):
            try:
                outcome, value = _query_session(api, str(session_uri), total)
                break
            except ProviderError as err:
                last_error = err
                if err.kind != ErrorKind.TRANSIENT or attempt:
                    break
                _sleep(RECONCILE_RETRY_DELAY)
        if outcome is None:
            why = last_error.message if last_error else "error desconocido"
            return ReconcileResult(
                "unknown",
                message=f"No se pudo consultar la subida en YouTube ({why}). Revisa YouTube Studio.",
            )
        if outcome == "complete":
            vid = str((value or {}).get("id") or "")
            if not vid:
                return ReconcileResult(
                    "unknown",
                    message="YouTube terminó la subida sin devolver el vídeo. Revisa YouTube Studio.",
                )
            st.update(video_id=vid, returned_privacy=((value or {}).get("status") or {}).get("privacyStatus"))
            ctx.save_state(st)
            return ReconcileResult(
                "published",
                external_id=vid,
                external_url=watch_url(vid),
                message="YouTube confirma que recibió el vídeo completo." + self._extras_note(ctx),
            )
        if outcome == "incomplete":
            offset = int(value[0])
            st["uploaded_bytes"] = offset
            st["resync"] = True
            if offset < total:
                st["commit_sent"] = False
            ctx.save_state(st)
            return ReconcileResult(
                "not_published",
                message=f"La subida no llegó a completarse ({offset} de {total} bytes); se reanudará la "
                "misma sesión.",
            )
        # Session gone: look for the video by title in the uploads playlist (report 06 §4.8).
        try:
            found = _find_by_title(api, str(st.get("title") or ""), _parse_iso(st.get("session_started_at")))
        except ProviderError as err:
            return ReconcileResult(
                "unknown",
                message=f"No se pudo buscar el vídeo en tu canal ({err.message}). Revisa YouTube Studio.",
            )
        if found and found.get("id"):
            vid = str(found["id"])
            st["video_id"] = vid
            ctx.save_state(st)
            return ReconcileResult(
                "published",
                external_id=vid,
                external_url=watch_url(vid),
                message="El vídeo aparece en tu canal de YouTube." + self._extras_note(ctx),
            )
        return ReconcileResult(
            "unknown",
            message="No se sabe si el vídeo llegó a YouTube: la sesión de subida caducó y no aparece en tu "
            "canal. Revisa YouTube Studio antes de reintentar.",
        )

    def _extras_note(self, ctx: PublishContext) -> str:
        opts = _options(ctx.target)
        st = ctx.state
        missing = [
            label
            for key, done, label in (
                ("thumbnail_media_id", "thumbnail_done", "la miniatura"),
                ("playlist_id", "playlist_done", "la lista de reproducción"),
                ("first_comment", "first_comment_done", "el primer comentario"),
            )
            if opts.get(key) and not st.get(done)
        ]
        if not missing:
            return ""
        return f" No se aplicaron {', '.join(missing)}: añádelos en YouTube Studio."

    def cancel_native(self, ctx: PublishContext) -> None:
        """Withdraw a video handed to YouTube's scheduler (uploaded private with ``publishAt``): delete it so
        it never goes public (engine ``cancel_target``)."""
        video_id = ctx.state.get("video_id")
        if not video_id:
            raise ProviderError(
                ErrorKind.PERMANENT, "youtube_native_no_id", "No consta el vídeo programado en YouTube."
            )
        if not _delete_video(self._api(ctx), str(video_id)):
            raise ProviderError(
                ErrorKind.PERMANENT, "youtube_native_delete_failed", "YouTube no permitió borrar el vídeo."
            )
        ctx.state["native_scheduled"] = False
        ctx.state["native_cancelled"] = True
        ctx.save_state(ctx.state)

    # ------------------------------------------------------------------------------ credentials

    def refresh_credentials(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
    ) -> TokenUpdate | None:
        """Refresh when the access token has less than 5 minutes left (or no known expiry)."""
        now = clock.now()
        if (
            account.access_token
            and account.token_expires_at is not None
            and account.token_expires_at - now > TOKEN_REFRESH_MARGIN
        ):
            return None
        return _refresh_account(account, settings, http, clock)

    def check_account(self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> None:
        api = _Api(account, settings, http)
        body = api.get(f"{API_BASE}/channels", {"part": "id", "mine": "true"}, op="check_account")
        ids = [str(i.get("id")) for i in body.get("items") or [] if isinstance(i, Mapping)]
        if not ids or (account.external_id and account.external_id not in ids):
            raise ProviderError(
                ErrorKind.AUTH,
                "youtube_channel_unavailable",
                "El canal de YouTube ya no está disponible con esta autorización. Vuelve a conectar la "
                "cuenta.",
                detail={"channels": len(ids)},
            )

    # -------------------------------------------------------------------------------- analytics

    def fetch_account_snapshot(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, day: date
    ) -> YouTubeAccountSnapshot:
        """Lifetime counters (Data API) for ``day`` + daily Analytics rows ``day-7 … day`` (``history``).
        Keys: ``followers`` (≈, YouTube rounds), ``posts_count``, ``views_total`` and daily ``views``,
        ``watch_minutes``, ``likes``, ``comments``, ``shares``, ``subs_gained``, ``subs_lost``. No derived
        metrics (YouTube developer policies, report 06 §10.3)."""
        api = _Api(account, settings, http)
        metrics: dict[str, float | int] = {}
        body = api.get(
            f"{API_BASE}/channels", {"part": "statistics", "id": account.external_id}, op="channels"
        )
        items = body.get("items") or []
        if items:
            stats = items[0].get("statistics") or {}
            subs = _int_or_none(stats.get("subscriberCount"))
            if subs is not None and not stats.get("hiddenSubscriberCount"):
                metrics["followers"] = subs
            videos = _int_or_none(stats.get("videoCount"))
            if videos is not None:
                metrics["posts_count"] = videos
            views = _int_or_none(stats.get("viewCount"))
            if views is not None:
                metrics["views_total"] = views
        history: list[AccountSnapshot] = []
        notice: str | None = None
        if not has_feature(granted_scopes(account), "analytics"):
            notice = (
                "Falta el permiso de YouTube Analytics: vuelve a conectar YouTube para ver las estadísticas "
                "diarias."
            )
            return YouTubeAccountSnapshot(day=day, metrics=metrics, history=history, notice=notice)
        try:
            rows = _analytics_rows(
                api,
                start=day - timedelta(days=ANALYTICS_HISTORY_DAYS),
                end=day,
                metrics=list(_DAILY_METRICS),
                dimensions="day",
                sort="day",
            )
        except ProviderError as err:
            if err.kind == ErrorKind.AUTH and err.code != "youtube_insufficient_scope":
                raise
            notice = f"No se pudieron leer las estadísticas diarias de YouTube Analytics: {err.message}"
            rows = []
        for row in rows:
            row_day = _parse_day(row.get("day"))
            if row_day is None:
                continue
            daily: dict[str, float | int] = {}
            for name, key in _DAILY_METRICS.items():
                value = _num(row.get(name))
                if value is not None:
                    daily[key] = value
            if row_day == day:
                metrics.update(daily)
            elif daily:
                history.append(AccountSnapshot(day=row_day, metrics=daily))
        return YouTubeAccountSnapshot(day=day, metrics=metrics, history=history, notice=notice)

    def fetch_recent_posts(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> YouTubePostList:
        """Uploads playlist → ``videos.list`` statistics (views/likes/comments) + Analytics per video
        (watch time, average view duration/percentage, shares, subscribers gained)."""
        since = ensure_utc(since)
        api = _Api(account, settings, http)
        uploads = _uploads_playlist_id(api)
        if not uploads:
            return YouTubePostList([], notice="No se encontró la lista de subidas del canal.")
        ids: list[str] = []
        page_token: str | None = None
        for _ in range(RECENT_POSTS_PAGES):
            params: dict[str, Any] = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            try:
                body = api.get(f"{API_BASE}/playlistItems", params, op="recent_posts")
            except ProviderError as err:
                if err.code == "youtube_playlistNotFound":
                    return YouTubePostList([], notice="El canal todavía no tiene vídeos.")
                raise
            reached_old = False
            for item in body.get("items") or []:
                details = item.get("contentDetails") or {}
                vid = details.get("videoId")
                published = _parse_iso(details.get("videoPublishedAt"))
                if published is not None and published < since:
                    reached_old = True
                    continue
                if vid and str(vid) not in ids:
                    ids.append(str(vid))
            page_token = body.get("nextPageToken")
            if reached_old or not page_token:
                break
        if not ids:
            return YouTubePostList([])
        videos = _videos(api, ids, "snippet,statistics,status,contentDetails", op="recent_posts")
        extra: dict[str, Mapping[str, Any]] = {}
        notice: str | None = None
        if videos and has_feature(granted_scopes(account), "analytics"):
            published_dates = [
                p for v in videos if (p := _parse_iso((v.get("snippet") or {}).get("publishedAt")))
            ]
            start = min(published_dates, default=since).date()
            try:
                rows = _analytics_rows(
                    api,
                    start=start,
                    end=datetime.now(UTC).date(),
                    metrics=list(_VIDEO_ANALYTICS),
                    dimensions="video",
                    filters="video==" + ",".join(str(v.get("id")) for v in videos[:500]),
                )
                extra = {str(r.get("video")): r for r in rows if r.get("video")}
            except ProviderError as err:
                if err.kind == ErrorKind.AUTH and err.code != "youtube_insufficient_scope":
                    raise
                notice = f"No se pudieron leer las estadísticas por vídeo de YouTube Analytics: {err.message}"
        out: list[PostMetricData] = []
        for video in videos:
            data = _post_metric(video, extra.get(str(video.get("id"))))
            if data.published_at is not None and data.published_at < since:
                continue
            out.append(data)
        out.sort(key=lambda p: p.published_at or since, reverse=True)
        return YouTubePostList(out, notice=notice)

    # --------------------------------------------------------------------------------- comments

    def fetch_comments(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[CommentData]:
        """Comment threads of all the channel's videos (newest first, early stop 2 h before ``since``,
        5 pages max). Replies hidden behind ``totalReplyCount`` are fetched with ``comments.list``."""
        if not has_feature(granted_scopes(account), "comments"):
            raise ProviderError(
                ErrorKind.AUTH,
                "youtube_missing_comments_scope",
                "Falta el permiso para leer comentarios de YouTube: vuelve a conectar la cuenta y marca "
                "todas las casillas.",
            )
        since = ensure_utc(since)
        api = _Api(account, settings, http)
        channel_id = account.external_id
        stop_before = since - COMMENT_LOOKBACK
        out: list[CommentData] = []
        seen: set[str] = set()
        reply_fetches = 0
        page_token: str | None = None
        for _ in range(COMMENT_PAGES):
            params: dict[str, Any] = {
                "part": "snippet,replies",
                "allThreadsRelatedToChannelId": channel_id,
                "order": "time",
                "maxResults": 100,
                "textFormat": "plainText",
            }
            if page_token:
                params["pageToken"] = page_token
            body = api.get(f"{API_BASE}/commentThreads", params, op="comments")
            threads = body.get("items") or []
            oldest: datetime | None = None
            for thread in threads:
                tsnip = thread.get("snippet") or {}
                top = tsnip.get("topLevelComment") or {}
                top_id = str(top.get("id") or thread.get("id") or "")
                video_id = tsnip.get("videoId") or (top.get("snippet") or {}).get("videoId")
                extra = {
                    "thread_id": thread.get("id"),
                    "can_reply": tsnip.get("canReply"),
                    "total_reply_count": tsnip.get("totalReplyCount"),
                    "is_public": tsnip.get("isPublic"),
                }
                top_data = _comment_data(top, None, video_id, channel_id, extra)
                if top_data is not None:
                    oldest = top_data.created_time if oldest is None else min(oldest, top_data.created_time)
                    if top_data.created_time >= since and top_data.external_id not in seen:
                        seen.add(top_data.external_id)
                        out.append(top_data)
                replies = list((thread.get("replies") or {}).get("comments") or [])
                total_replies = _int_or_none(tsnip.get("totalReplyCount")) or 0
                if total_replies > len(replies) and reply_fetches < COMMENT_EXTRA_REPLY_FETCHES:
                    reply_fetches += 1
                    replies = self._all_replies(api, top_id) or replies
                for reply in replies:
                    data = _comment_data(reply, top_id, video_id, channel_id, {"thread_id": thread.get("id")})
                    if data is not None and data.created_time >= since and data.external_id not in seen:
                        seen.add(data.external_id)
                        out.append(data)
            page_token = body.get("nextPageToken")
            if not page_token or not threads or (oldest is not None and oldest < stop_before):
                break
        return out

    def _all_replies(self, api: _Api, parent_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(3):
            params: dict[str, Any] = {
                "part": "snippet",
                "parentId": parent_id,
                "maxResults": 100,
                "textFormat": "plainText",
            }
            if page_token:
                params["pageToken"] = page_token
            body = api.get(f"{API_BASE}/comments", params, op="comments")
            out.extend(i for i in body.get("items") or [] if isinstance(i, dict))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out

    def reply_comment(
        self,
        account: AccountCreds,
        settings: ProviderSettings,
        http: httpx.Client,
        comment_external_id: str,
        text: str,
    ) -> str:
        """Reply via ``comments.insert``. YouTube has one level of replies: answering a reply (id
        ``<parent>.<reply>``) posts under its top-level comment. Returns the new comment id."""
        body_text = (text or "").strip()
        if not body_text:
            raise ProviderError(
                ErrorKind.PERMANENT, "youtube_reply_empty", "Escribe el texto de la respuesta."
            )
        if len(body_text) > COMMENT_MAX:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "youtube_reply_too_long",
                f"La respuesta es demasiado larga (máximo {COMMENT_MAX} caracteres).",
            )
        if not has_feature(granted_scopes(account), "comments"):
            raise ProviderError(
                ErrorKind.AUTH,
                "youtube_missing_comments_scope",
                "Falta el permiso para responder comentarios de YouTube: vuelve a conectar la cuenta y marca "
                "todas las casillas.",
            )
        api = _Api(account, settings, http)
        resp = api.request(
            "POST",
            f"{API_BASE}/comments",
            op="reply",
            params={"part": "snippet"},
            json_body={
                "snippet": {"parentId": top_level_comment_id(comment_external_id), "textOriginal": body_text}
            },
        )
        body = _json_or_none(resp)
        new_id = body.get("id") if isinstance(body, dict) else None
        if not new_id:
            raise ProviderError(
                ErrorKind.AMBIGUOUS,
                "youtube_reply_no_id",
                "YouTube no confirmó la respuesta; revisa el vídeo antes de volver a enviarla.",
            )
        return str(new_id)


# ----------------------------------------------------------------------------- analytics helpers

#: Analytics API daily metric → canonical snapshot key (report 06 §10.4).
_DAILY_METRICS: dict[str, str] = {
    "views": "views",
    "estimatedMinutesWatched": "watch_minutes",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "subscribersGained": "subs_gained",
    "subscribersLost": "subs_lost",
}
#: Analytics per-video metric → post metric key. Views/likes/comments come from the Data API (fresher).
_VIDEO_ANALYTICS: dict[str, str] = {
    "estimatedMinutesWatched": "watch_minutes",
    "averageViewDuration": "avg_view_duration",
    "averageViewPercentage": "avg_view_pct",
    "shares": "shares",
    "subscribersGained": "subs_gained",
}


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _analytics_rows(
    api: _Api,
    *,
    start: date,
    end: date,
    metrics: list[str],
    dimensions: str | None = None,
    filters: str | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    """``reports.query`` rows as dicts keyed by column name (report 06 §10.2)."""
    params: dict[str, Any] = {
        "ids": "channel==MINE",
        "startDate": start.isoformat(),
        "endDate": max(end, start).isoformat(),
        "metrics": ",".join(metrics),
    }
    if dimensions:
        params["dimensions"] = dimensions
    if filters:
        params["filters"] = filters
    if sort:
        params["sort"] = sort
    body = api.get(ANALYTICS_URL, params, op="analytics")
    names = [str(h.get("name")) for h in body.get("columnHeaders") or [] if isinstance(h, Mapping)]
    return [dict(zip(names, row, strict=False)) for row in body.get("rows") or [] if isinstance(row, list)]


def _post_metric(video: Mapping[str, Any], analytics_row: Mapping[str, Any] | None) -> YouTubePostMetricData:
    vid = str(video.get("id") or "")
    snippet = video.get("snippet") or {}
    stats = video.get("statistics") or {}
    status = video.get("status") or {}
    details = video.get("contentDetails") or {}
    thumbs = snippet.get("thumbnails") or {}
    thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
    metrics: dict[str, float | int] = {}
    for api_key, key in (("viewCount", "views"), ("likeCount", "likes"), ("commentCount", "comments")):
        value = _int_or_none(stats.get(api_key))
        if value is not None:
            metrics[key] = value
    if analytics_row:
        for api_key, key in _VIDEO_ANALYTICS.items():
            value = _num(analytics_row.get(api_key))
            if value is not None:
                metrics[key] = value
    title = snippet.get("title")
    return YouTubePostMetricData(
        external_id=vid,
        published_at=_parse_iso(snippet.get("publishedAt")),
        permalink=watch_url(vid),
        caption=title,
        media_type="video",
        thumbnail_url=thumb,
        metrics=metrics,
        title=title,
        description=snippet.get("description"),
        privacy_status=status.get("privacyStatus"),
        duration_s=parse_duration(details.get("duration")),
    )


def top_level_comment_id(comment_id: str) -> str:
    """Reply ids look like ``<top-level id>.<reply id>``; YouTube only accepts top-level parents."""
    return comment_id.split(".", 1)[0] if "." in comment_id else comment_id


def _comment_data(
    comment: Mapping[str, Any],
    parent_id: str | None,
    video_id: str | None,
    channel_id: str,
    extra: Mapping[str, Any],
) -> CommentData | None:
    cid = comment.get("id")
    snippet = comment.get("snippet") or {}
    created = _parse_iso(snippet.get("publishedAt"))
    if not cid or created is None:
        return None
    vid = video_id or snippet.get("videoId")
    author_id = (snippet.get("authorChannelId") or {}).get("value")
    raw: dict[str, Any] = {k: v for k, v in snippet.items() if k != "imageUrl"}  # signed 6-hour URL
    if snippet.get("imageUrl"):
        raw["has_image"] = True
    raw.update({k: v for k, v in extra.items() if v is not None})
    raw["id"] = cid
    return CommentData(
        external_id=str(cid),
        parent_external_id=parent_id or snippet.get("parentId"),
        object_external_id=str(vid or channel_id),
        object_permalink=f"{watch_url(str(vid))}&lc={cid}" if vid else None,
        author_name=snippet.get("authorDisplayName"),
        author_id=author_id,
        text=str(snippet.get("textDisplay") or snippet.get("textOriginal") or ""),
        created_time=created,
        from_us=bool(author_id and author_id == channel_id),
        raw=raw,
    )


def processing_error(video: Mapping[str, Any]) -> ProviderError:
    """``failed``/``rejected`` processing → PERMANENT with a Spanish reason (report 06 §8)."""
    status = video.get("status") or {}
    processing = video.get("processingDetails") or {}
    upload_status = status.get("uploadStatus")
    reason = (
        status.get("rejectionReason")
        or status.get("failureReason")
        or processing.get("processingFailureReason")
    )
    if upload_status == "rejected":
        msg = _REJECTION_MESSAGES.get(str(reason), "YouTube rechazó el vídeo.")
    elif upload_status == "deleted":
        msg = "El vídeo se eliminó de YouTube mientras se procesaba."
    else:
        msg = _FAILURE_MESSAGES.get(str(reason), "YouTube no pudo procesar el vídeo.")
    return ProviderError(
        ErrorKind.PERMANENT,
        f"youtube_{upload_status or 'processing'}_{reason or 'failed'}",
        msg,
        detail={
            "video_id": video.get("id"),
            "upload_status": upload_status,
            "reason": reason,
            "processing_status": processing.get("processingStatus"),
        },
    )


# ------------------------------------------------------------------------------------ lock test


def make_test_video(
    path: Path, *, seconds: int = LOCK_TEST_SECONDS, size: tuple[int, int] = LOCK_TEST_SIZE
) -> Path:
    """Encode a tiny vertical H.264 (fallback MPEG-4) clip with PyAV. Each clip is unique (random colours and
    a timestamp) so YouTube never rejects a repeated test as a duplicate."""
    import av
    from PIL import Image, ImageDraw

    width, height = size
    rng = random.Random(secrets.randbits(64))
    base = (rng.randrange(40, 200), rng.randrange(40, 200), rng.randrange(40, 200))
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    container = av.open(str(path), mode="w")
    try:
        codec = "libx264" if "libx264" in av.codecs_available else "mpeg4"
        stream = container.add_stream(codec, rate=LOCK_TEST_FPS)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        if codec == "libx264":
            stream.options = {"preset": "ultrafast"}
        for i in range(max(1, int(seconds * LOCK_TEST_FPS))):
            shade = tuple(min(255, c + (i * 3) % 50) for c in base)
            img = Image.new("RGB", (width, height), shade)
            draw = ImageDraw.Draw(img)
            box = (20, height // 2 - 40, width - 20, height // 2 + 40)
            draw.rectangle(box, outline=(255, 255, 255), width=3)
            draw.text((32, height // 2 - 24), "Prueba Planificador", fill=(255, 255, 255))
            draw.text((32, height // 2 + 4), stamp, fill=(255, 255, 255))
            for packet in stream.encode(av.VideoFrame.from_image(img)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return path


def _upload_blocking(api: _Api, path: Path, mime: str, plan: UploadPlan) -> dict[str, Any]:
    """Whole resumable upload in one go (small files only: the lock test)."""
    total = path.stat().st_size
    session_uri = _start_session(api, plan, total, mime)
    offset = 0
    chunk = _chunk_size()
    for _ in range(total // chunk + 5):
        length = min(chunk, total - offset)
        resp = _put_chunk(api, session_uri, path, offset, length, total, mime)
        if resp.status_code in (200, 201):
            body = _json_or_none(resp)
            return body if isinstance(body, dict) else {}
        if resp.status_code != 308:
            raise ProviderError(
                ErrorKind.TRANSIENT, "youtube_session_gone", "La sesión de subida de YouTube caducó."
            )
        offset = next_offset_from_range(resp.headers.get("Range"))
    raise ProviderError(
        ErrorKind.TRANSIENT, "youtube_upload_stalled", "YouTube no aceptó el vídeo de prueba."
    )


def _delete_video(api: _Api, video_id: str) -> bool:
    try:
        api.request("DELETE", f"{API_BASE}/videos", op="delete", params={"id": video_id}, ok=(200, 204, 404))
    except ProviderError as err:
        log.warning("Could not delete YouTube test video: %s", err.code)
        return False
    return True


def lock_test(
    account: AccountCreds,
    settings: ProviderSettings,
    http: httpx.Client,
    clock: Clock,
    *,
    delete: bool = True,
) -> LockTestResult:
    """ "Prueba de bloqueo" (report 06 §3.7): upload a unique 2-second vertical clip as **unlisted**
    (``notifySubscribers=false``, not for kids), wait for processing, read ``status.privacyStatus`` back and
    delete the clip. ``unlisted`` ⇒ unlocked; ``private`` ⇒ locked (unaudited project). Blocking (up to a
    few minutes): run it from a background task."""
    api = _Api(account, settings, http, clock)
    options = {
        "title": LOCK_TEST_TITLE,
        "privacy_status": "unlisted",
        "made_for_kids": False,
        "synthetic_media": False,
        "notify_subscribers": False,
        "category_id": DEFAULT_CATEGORY,
    }
    description = (
        "Vídeo de prueba creado por Planificador para comprobar si YouTube bloquea las subidas hechas con la "
        "API. Puedes borrarlo."
    )
    video_id: str | None = None
    privacy: str | None = None

    def failed(message: str) -> LockTestResult:
        result = LockTestResult("error", message, video_id=video_id, privacy_status=privacy)
        if video_id and delete:
            result.deleted = _delete_video(api, video_id)
        return result

    try:
        with tempfile.TemporaryDirectory(prefix="planificador-yt-") as tmp:
            path = make_test_video(Path(tmp) / "prueba.mp4")
            info = MediaInfo(
                id=0,
                kind="video",
                path=path,
                mime="video/mp4",
                size_bytes=path.stat().st_size,
                width=LOCK_TEST_SIZE[0],
                height=LOCK_TEST_SIZE[1],
                duration_s=float(LOCK_TEST_SECONDS),
                fps=float(LOCK_TEST_FPS),
                video_codec="h264",
                audio_codec=None,
                bitrate=None,
                is_hdr=False,
                original_name="prueba.mp4",
            )
            target = TargetSpec("youtube", "short", description, [info], options, None, True)
            plan = build_upload_plan(target, now=clock.now())
            video = _upload_blocking(api, path, "video/mp4", plan)
        video_id = str(video.get("id") or "") or None
        if not video_id:
            return failed("YouTube no devolvió el vídeo de prueba.")
        privacy = (video.get("status") or {}).get("privacyStatus")
        for _ in range(LOCK_TEST_POLL_MAX):
            items = _videos(api, [video_id], "status,processingDetails", op="lock_test")
            if items:
                status = items[0].get("status") or {}
                privacy = status.get("privacyStatus") or privacy
                proc = (items[0].get("processingDetails") or {}).get("processingStatus")
                finished_upload = status.get("uploadStatus") in ("processed", "failed", "rejected", "deleted")
                if finished_upload or proc in ("succeeded", "failed", "terminated"):
                    break
            _sleep(LOCK_TEST_POLL_DELAY)
    except ProviderError as err:
        return failed(f"No se pudo completar la prueba: {err.message}")
    except Exception as exc:  # e.g. the clip could not be encoded
        log.exception("YouTube lock test failed")
        return failed(f"No se pudo completar la prueba ({type(exc).__name__}).")
    if privacy == "private":
        result = LockTestResult(
            "locked",
            "YouTube ha dejado el vídeo de prueba como privado: tu proyecto de Google bloquea las subidas "
            "hechas con la API. Usa la Publicación asistida o solicita la auditoría de YouTube.",
            video_id=video_id,
            privacy_status=privacy,
        )
    elif privacy in ("unlisted", "public"):
        result = LockTestResult(
            "unlocked",
            "YouTube respetó la visibilidad del vídeo de prueba: puedes usar la publicación automática.",
            video_id=video_id,
            privacy_status=privacy,
        )
    else:
        return failed("No se pudo leer la visibilidad del vídeo de prueba.")
    if delete:
        result.deleted = _delete_video(api, video_id)
        if not result.deleted:
            result.message += " No se pudo borrar el vídeo de prueba: bórralo en YouTube Studio."
    return result


def run_lock_test(
    account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
) -> Literal["unlocked", "locked", "error"]:
    """SPEC §6 contract wrapper around :func:`lock_test`. Store the result in
    ``accounts.meta["youtube_lock_status"]`` (``error`` → leave it ``unknown``)."""
    return lock_test(account, settings, http, clock).status

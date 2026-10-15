"""Shared Meta Graph API helpers: requests, error classification, usage headers, Facebook Login (SPEC §6).

Used by the Facebook and Instagram providers and by the connection routes. Research:
``docs/research/02-meta-api.md`` (§2 OAuth, §3–§4 publishing, §5 insights, §7 rate limits / error map, §11
corrections).

Conventions:

- Every Graph call goes through :func:`graph_request`, which raises a classified :class:`ProviderError` for
  transport errors, HTTP errors **and** HTTP 200 bodies that carry an ``error`` object (Meta does that, §4.1).
- Tokens travel as ``access_token`` query/form parameters (plus ``appsecret_proof`` when the app secret is
  known) and as ``Authorization: OAuth …`` for rupload; error details are scrubbed and carry no query strings.
- ``X-App-Usage`` / ``X-Business-Use-Case-Usage`` / ``X-Page-Usage`` are parsed on every response; background
  jobs (analytics, comments) call :func:`ensure_background_budget` and pause above 80 % (§7.1).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from planificador.providers.base import (
    ErrorKind,
    MediaInfo,
    ProviderError,
    ProviderSettings,
    scrub,
    scrub_text,
)

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
GRAPH_HOST = "graph.facebook.com"
FACEBOOK_WEB = "https://www.facebook.com"
DIALOG_BASE = "https://www.facebook.com"
RUPLOAD_HOST = "rupload.facebook.com"
DEFAULT_API_VERSION = "v25.0"

SCOPES_FB_IG: list[str] = [
    "pages_show_list",
    "pages_manage_posts",
    "pages_read_engagement",
    "read_insights",
    "pages_manage_engagement",
    "pages_read_user_content",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "instagram_manage_comments",
    "business_management",
]

#: Page task required to publish (§2.2 step 5; Meta SDK task enum, §11 #4).
TASK_CREATE_CONTENT = "CREATE_CONTENT"

#: Throttling error codes (§7.1/§7.2 + §11 #25: the whole BUC family).
THROTTLE_CODES: frozenset[int] = frozenset({4, 17, 32, 613, *range(80000, 80010), 80014})

#: Default waits (seconds) when Meta gives no ``estimated_time_to_regain_access``.
DEFAULT_THROTTLE_WAIT = 15 * 60
BLOCKED_WAIT = 60 * 60
QUOTA_WAIT = 60 * 60

#: Background jobs pause above this usage percentage (§7.1 decision).
BACKGROUND_PAUSE_PCT = 80.0
USAGE_TTL_SECONDS = 15 * 60

UPLOAD_CHUNK = 1024 * 1024
UPLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=300.0, pool=30.0)


# ------------------------------------------------------------------------------------------- basics


def api_version(settings: ProviderSettings) -> str:
    """Graph API version from the settings (``v25.0`` default), always with the leading ``v``."""
    version = str(getattr(settings, "meta_api_version", "") or DEFAULT_API_VERSION).strip()
    if not version.startswith("v"):
        version = f"v{version}"
    return version


def graph_url(settings: ProviderSettings, path: str) -> str:
    """``https://graph.facebook.com/{version}/{path}`` (absolute URLs are returned unchanged)."""
    if path.startswith(("https://", "http://")):
        return path
    return f"{GRAPH}/{api_version(settings)}/{path.lstrip('/')}"


def redirect_uri(settings: ProviderSettings) -> str:
    return f"{settings.base_url}/conexiones/callback/meta"


def appsecret_proof(token: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _no_token_error() -> ProviderError:
    return ProviderError(
        ErrorKind.AUTH,
        "meta_no_token",
        "La cuenta no tiene un acceso válido a Meta. Vuelve a conectarla en Cuentas conectadas.",
    )


def auth_params(settings: ProviderSettings, token: str | None) -> dict[str, str]:
    """``access_token`` (+ ``appsecret_proof`` when the app secret is configured)."""
    if not token:
        raise _no_token_error()
    params = {"access_token": token}
    secret = getattr(settings, "meta_app_secret", None)
    if secret:
        params["appsecret_proof"] = appsecret_proof(token, secret)
    return params


def form_fields(values: Mapping[str, Any]) -> dict[str, str]:
    """Form-encode values: drop ``None``, booleans → ``true``/``false``, lists/dicts → compact JSON."""
    out: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, list | tuple | dict):
            out[key] = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        else:
            out[key] = str(value)
    return out


def parse_time(value: Any) -> datetime | None:
    """Parse Meta timestamps (``2026-10-15T10:00:00+0000``, ISO 8601 or UNIX seconds) to aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=UTC)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def absolute_facebook_url(url: str | None) -> str | None:
    """Facebook sometimes returns relative permalinks (``/reel/123``)."""
    if not url:
        return None
    if url.startswith("/"):
        return f"{FACEBOOK_WEB}{url}"
    return url


def normalize_text(text: str | None) -> str:
    """Whitespace-insensitive comparison key for matching our posts/comments on the remote side."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _safe_url(url: str) -> str:
    """URL without its query string (tokens live there)."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return "?"


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------- usage headers


@dataclass(frozen=True)
class UsageReading:
    """One usage header entry: the highest of call_count / total_cputime / total_time (percent)."""

    source: str
    percent: float
    regain_seconds: int | None = None


def _pct(entry: Mapping[str, Any]) -> float:
    best = 0.0
    for key in ("call_count", "total_cputime", "total_time", "acc_id_util_pct"):
        try:
            best = max(best, float(entry.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return best


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        return headers.get(name)
    except AttributeError:
        return None


def parse_usage_headers(headers: Any) -> list[UsageReading]:
    """Parse ``X-App-Usage``, ``X-Page-Usage``, ``X-Ad-Account-Usage`` and ``X-Business-Use-Case-Usage``."""
    readings: list[UsageReading] = []
    for name in ("x-app-usage", "x-page-usage", "x-ad-account-usage"):
        raw = _header(headers, name)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            readings.append(UsageReading(name, _pct(data)))
    raw = _header(headers, "x-business-use-case-usage")
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for object_id, entries in data.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    regain = entry.get("estimated_time_to_regain_access")
                    try:
                        regain_s = int(float(regain) * 60) if regain else None  # minutes → seconds
                    except (TypeError, ValueError):
                        regain_s = None
                    readings.append(
                        UsageReading(f"buc:{object_id}:{entry.get('type', '')}", _pct(entry), regain_s)
                    )
    return readings


class _UsageTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readings: dict[str, tuple[UsageReading, float]] = {}

    def record(self, readings: list[UsageReading]) -> None:
        if not readings:
            return
        now = time.monotonic()
        with self._lock:
            for reading in readings:
                self._readings[reading.source] = (reading, now)

    def fresh(self) -> list[UsageReading]:
        now = time.monotonic()
        with self._lock:
            out = []
            for key, (reading, at) in list(self._readings.items()):
                ttl = max(USAGE_TTL_SECONDS, reading.regain_seconds or 0)
                if now - at > ttl:
                    del self._readings[key]
                    continue
                out.append(reading)
            return out

    def reset(self) -> None:
        with self._lock:
            self._readings.clear()


_usage = _UsageTracker()


def record_usage(headers: Any) -> list[UsageReading]:
    readings = parse_usage_headers(headers)
    _usage.record(readings)
    for reading in readings:
        if reading.percent >= BACKGROUND_PAUSE_PCT:
            log.warning("Meta usage high: %s at %.0f%%", reading.source, reading.percent)
    return readings


def usage_percent() -> float:
    """Highest recent usage percentage reported by Meta (0 when nothing recent)."""
    return max((r.percent for r in _usage.fresh()), default=0.0)


def usage_regain_seconds() -> int | None:
    values = [r.regain_seconds for r in _usage.fresh() if r.regain_seconds]
    return max(values) if values else None


def background_paused(threshold: float = BACKGROUND_PAUSE_PCT) -> bool:
    return usage_percent() >= threshold


def ensure_background_budget(threshold: float = BACKGROUND_PAUSE_PCT) -> None:
    """Raise ``RATE_LIMITED`` when non-urgent jobs should pause (usage above ``threshold``)."""
    pct = usage_percent()
    if pct >= threshold:
        raise ProviderError(
            ErrorKind.RATE_LIMITED,
            "meta_usage_high",
            f"Meta indica un uso alto de la API ({pct:.0f} %). Las estadísticas y los comentarios se "
            "actualizarán más tarde.",
            detail={"usage_percent": pct},
            retry_after=usage_regain_seconds() or DEFAULT_THROTTLE_WAIT,
        )


def reset_usage() -> None:
    """Forget recorded usage (tests)."""
    _usage.reset()


# ------------------------------------------------------------------------------------ classification

_AUTH_190: dict[int, tuple[str, str]] = {
    458: ("meta_app_removed", "Se quitó el acceso de la app en Facebook. Vuelve a conectar la cuenta."),
    459: (
        "meta_checkpoint",
        "Facebook ha bloqueado la sesión por seguridad. Entra en facebook.com, completa la verificación "
        "y vuelve a conectar la cuenta.",
    ),
    460: ("meta_password_changed", "La contraseña de Facebook ha cambiado. Vuelve a conectar la cuenta."),
    463: ("meta_token_expired", "El acceso a Meta ha caducado. Vuelve a conectar la cuenta."),
    464: (
        "meta_unconfirmed_user",
        "La cuenta de Facebook no está confirmada. Confírmala en facebook.com y vuelve a conectarla.",
    ),
    467: ("meta_token_invalid", "El acceso a Meta ya no es válido. Vuelve a conectar la cuenta."),
    492: (
        "meta_page_role_lost",
        "Ya no tienes un rol en la página de Facebook. Pide a un administrador que te lo devuelva y "
        "vuelve a conectar la cuenta.",
    ),
}

_P = ErrorKind.PERMANENT
_T = ErrorKind.TRANSIENT
_R = ErrorKind.RATE_LIMITED

_SECURITY_CHECK = (
    _P,
    "fb_security_check",
    "Facebook pide una verificación de identidad o seguridad. Complétala en facebook.com y vuelve a "
    "intentarlo.",
    None,
)
_CONTENT_BLOCKED = (
    _P,
    "fb_content_blocked",
    "Facebook ha bloqueado el contenido por no cumplir sus normas.",
    None,
)
_ASPECT = (
    _P,
    "ig_aspect_ratio",
    "La proporción de la imagen no es válida para Instagram (en el feed debe estar entre 4:5 y 1,91:1).",
    None,
)
_TRIAL = (
    _P,
    "ig_trial_reel_unavailable",
    "Instagram no permite publicar este reel como reel de prueba.",
    None,
)

#: subcode → (kind, our code, Spanish message, retry_after). §7.2 error map.
SUBCODE_MAP: dict[int, tuple[ErrorKind, str, str, int | None]] = {
    # Instagram — media not ready / container lifecycle (a new container fixes them)
    2207027: (_T, "ig_media_not_ready", "Instagram aún está procesando el archivo.", 30),
    2207006: (
        _T,
        "ig_container_invalid",
        "El contenedor de Instagram ya no es válido; se creará otro.",
        None,
    ),
    2207008: (_T, "ig_container_invalid", "Instagram no pudo usar el contenedor; se creará otro.", 60),
    2207020: (_T, "ig_container_expired", "El contenedor de Instagram ha caducado; se creará otro.", None),
    2207032: (_T, "ig_upload_failed", "Instagram no pudo crear la publicación; se reintentará.", None),
    2207053: (_T, "ig_upload_failed", "Instagram tuvo un error desconocido al subir el archivo.", None),
    # Instagram — fetching the media from the public URL
    2207003: (
        _T,
        "ig_media_fetch_failed",
        "Instagram tardó demasiado en descargar el archivo desde la URL pública. Se reintentará.",
        None,
    ),
    2207052: (
        _T,
        "ig_media_fetch_failed",
        "Instagram no pudo descargar la imagen desde la URL pública. Revisa la URL pública en Ajustes.",
        None,
    ),
    2207077: (_T, "ig_media_fetch_failed", "Instagram no pudo descargar el archivo. Se reintentará.", None),
    # Instagram — validation (terminal)
    2207004: (_P, "ig_image_too_large", "La imagen supera el tamaño máximo de Instagram (8 MB).", None),
    2207005: (_P, "ig_media_format", "Instagram no admite el formato del archivo.", None),
    2207009: _ASPECT,
    2207026: (
        _P,
        "ig_video_format",
        "Instagram no admite el vídeo (usa MP4 con H.264 y audio AAC, máx. 1920 px y 25 Mbps).",
        None,
    ),
    2207085: (_P, "ig_media_rejected", "Instagram rechazó el archivo por no cumplir sus requisitos.", None),
    2207057: (_P, "ig_thumb_offset", "El fotograma de portada elegido no es válido para este vídeo.", None),
    2207010: (
        _P,
        "ig_caption_invalid",
        "El texto no es válido para Instagram (máx. 2200 caracteres, 30 hashtags y 20 menciones).",
        None,
    ),
    2207028: (
        _P,
        "ig_carousel_invalid",
        "El carrusel no es válido: necesita entre 2 y 10 archivos ya procesados.",
        None,
    ),
    2207018: (
        _P,
        "ig_collaborators",
        "Algún colaborador no es válido (escribe el usuario sin @ y comprueba que acepta colaboraciones).",
        None,
    ),
    2207078: _TRIAL,
    2207081: _TRIAL,
    2207082: (_P, "ig_audio", "Instagram no pudo procesar el audio del vídeo.", None),
    # Instagram — account state / spam / quota
    2207001: (
        _P,
        "ig_spam",
        "Instagram ha bloqueado la publicación por posible spam. Espera unas horas o cambia el contenido.",
        None,
    ),
    2207051: (
        _P,
        "ig_activity_restricted",
        "Instagram ha restringido temporalmente la actividad de la cuenta. Espera 24 horas antes de "
        "reintentar.",
        None,
    ),
    2207050: (_P, "ig_user_restricted", "La cuenta de Instagram está restringida y no puede publicar.", None),
    2207042: (
        _R,
        "ig_quota_reached",
        "Has alcanzado el límite de publicaciones de Instagram por API en 24 horas. Se reintentará más "
        "tarde.",
        QUOTA_WAIT,
    ),
    1772107: (_P, "ig_comment_failed", "Instagram no pudo añadir el comentario.", None),
    # Facebook
    1366046: (
        _P,
        "fb_photo_invalid",
        "Facebook rechazó la foto: debe ser JPG o PNG y pesar menos de 4 MB.",
        None,
    ),
    2069019: (_P, "fb_photo_invalid", "Facebook rechazó el archivo por no ser válido.", None),
    1366051: (_P, "fb_duplicate_photos", "Facebook indica que estas fotos ya se publicaron.", None),
    1346003: _CONTENT_BLOCKED,
    1404102: _CONTENT_BLOCKED,
    1404078: (
        _P,
        "fb_publishing_authorization",
        "La página debe completar la «Autorización de publicación de páginas» en Facebook antes de publicar.",
        None,
    ),
    4854002: _SECURITY_CHECK,
    1404006: _SECURITY_CHECK,
    1404112: _SECURITY_CHECK,
    1609005: (_P, "fb_link_scrape", "Facebook no pudo leer el enlace.", None),
    1609008: (_P, "fb_link_not_allowed", "Facebook no permite publicar aquí enlaces a facebook.com.", None),
    1390008: (
        _R,
        "meta_temporarily_blocked",
        "Meta ha bloqueado temporalmente la publicación por publicar demasiado rápido. Se reintentará "
        "más tarde.",
        BLOCKED_WAIT,
    ),
}

#: Top-level codes with a fixed meaning (checked after subcodes).
CODE_MAP: dict[int, tuple[ErrorKind, str, str, int | None]] = {
    9007: SUBCODE_MAP[2207027],
    9004: SUBCODE_MAP[2207052],
    36003: _ASPECT,
    36001: (_P, "ig_resolution", "La resolución de la imagen no es válida para Instagram.", None),
    368: SUBCODE_MAP[1390008],
    506: (_P, "fb_duplicate", "Facebook rechaza publicaciones duplicadas: cambia el texto.", None),
}

_TOKEN_ERROR_CODES: frozenset[str] = frozenset(
    {"meta_no_token", "meta_wrong_token", "meta_token_invalid"} | {v[0] for v in _AUTH_190.values()}
)


def is_token_error(exc: ProviderError) -> bool:
    """True when the account token itself is broken (not just a missing permission on one object)."""
    return exc.kind == ErrorKind.AUTH and exc.code in _TOKEN_ERROR_CODES


def classify_meta_error(status_code: int, body: dict | None, headers: object) -> ProviderError:
    """Map a Meta error response (HTTP status + JSON body + usage headers) to a :class:`ProviderError`."""
    err: Any = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):
        err = {"message": err}
    if not isinstance(err, dict):
        err = {}
    code = _to_int(err.get("code"))
    sub = _to_int(err.get("error_subcode"))
    message = str(err.get("message") or "")
    user_message = str(err.get("error_user_msg") or "")
    lower = f"{message} {user_message}".lower()

    readings = parse_usage_headers(headers)
    regains = [r.regain_seconds for r in readings if r.regain_seconds]
    regain = max(regains) if regains else None

    detail: dict[str, Any] = {
        "http_status": status_code,
        "code": code,
        "subcode": sub,
        "type": err.get("type"),
        "message": scrub_text(message)[:500] or None,
        "user_title": err.get("error_user_title"),
        "user_message": scrub_text(user_message)[:500] or None,
        "is_transient": err.get("is_transient"),
        "fbtrace_id": err.get("fbtrace_id"),
    }
    if readings:
        detail["usage"] = {r.source: r.percent for r in readings}
    debug_info = body.get("debug_info") if isinstance(body, dict) else None
    if isinstance(debug_info, dict):
        detail["debug_info"] = scrub(debug_info)
    if not err and isinstance(body, dict) and not isinstance(debug_info, dict):
        detail["body"] = scrub_text(json.dumps(body, ensure_ascii=False)[:500])
    detail = {k: v for k, v in detail.items() if v is not None}

    def make(kind: ErrorKind, our_code: str, text: str, retry_after: int | None = None) -> ProviderError:
        return ProviderError(kind, our_code, text, detail=detail, retry_after=retry_after)

    # rupload errors come as {"debug_info": {"retriable": bool, "type": ..., "message": ...}}
    if not err and isinstance(debug_info, dict):
        info_msg = scrub_text(str(debug_info.get("message") or "")).strip()[:200]
        if debug_info.get("retriable") or status_code >= 500:
            return make(_T, "meta_upload_failed", "La subida del archivo a Meta falló. Se reintentará.")
        return make(
            _P,
            "meta_upload_rejected",
            "Meta rechazó el archivo subido" + (f": {info_msg}" if info_msg else "."),
        )

    # 1) token problems
    if code in (190, 102):
        if sub in _AUTH_190:
            our_code, text = _AUTH_190[sub]
            return make(ErrorKind.AUTH, our_code, text)
        if "log in to www.facebook.com" in lower or "checkpoint" in lower:
            our_code, text = _AUTH_190[459]
            return make(ErrorKind.AUTH, our_code, text)
        return make(
            ErrorKind.AUTH,
            "meta_token_invalid",
            "El acceso a Meta no es válido o ha caducado. Vuelve a conectar la cuenta en Cuentas conectadas.",
        )

    # 2) throttling
    if code in THROTTLE_CODES or status_code == 429:
        return make(
            _R,
            "meta_rate_limited",
            "Meta ha limitado temporalmente las peticiones de la app. Se reintentará más tarde.",
            regain or DEFAULT_THROTTLE_WAIT,
        )

    # 3) specific subcodes / codes
    if sub is not None and sub in SUBCODE_MAP:
        kind, our_code, text, retry = SUBCODE_MAP[sub]
        if kind == _R and regain:
            retry = max(retry or 0, regain)
        return make(kind, our_code, text, retry)
    if code is not None and code in CODE_MAP:
        kind, our_code, text, retry = CODE_MAP[code]
        return make(kind, our_code, text, retry)

    # 4) message-based cases
    if "not an instagram business" in lower:
        return make(
            _P,
            "ig_not_business",
            "Las historias de Instagram solo se pueden publicar desde cuentas de empresa (no de creador).",
        )
    if "unpublished posts must be posted to a page as the page itself" in lower:
        return make(
            ErrorKind.AUTH,
            "meta_wrong_token",
            "Facebook no aceptó el acceso de la página. Vuelve a conectar la cuenta.",
        )
    if "valid insights metric" in lower or ("metric" in lower and "insights" in lower and code == 100):
        return make(_P, "meta_invalid_metric", "Meta ya no ofrece una de las métricas solicitadas.")
    if "not enough viewers" in lower or ("insights" in lower and code in (10, 100)):
        return make(_P, "meta_insights_unavailable", "Meta no ofrece estadísticas para este contenido.")
    if "client secret" in lower or "appsecret_proof" in lower:
        return make(_P, "meta_bad_app_secret", "El App Secret de Meta no es correcto. Revísalo en Ajustes.")
    if code == 101 or "invalid application id" in lower:
        return make(_P, "meta_bad_app_id", "El App ID de Meta no es correcto. Revísalo en Ajustes.")
    if "redirect_uri" in lower or code == 191:
        return make(
            _P,
            "meta_redirect_uri",
            "La URL de redirección no coincide con la configurada en tu app de Meta. Añádela en "
            "«URI de redireccionamiento de OAuth válidos».",
        )
    if "authorization code" in lower or "verification code" in lower:
        return make(
            _P,
            "meta_oauth_code_invalid",
            "El código de autorización de Meta no es válido o ya se usó. Vuelve a intentar la conexión.",
        )

    # 5) permissions
    if code == 10 or (code is not None and 200 <= code <= 299):
        return make(
            ErrorKind.AUTH,
            "meta_permission",
            "Falta un permiso de Meta para esta acción. Vuelve a conectar la cuenta aceptando todos los "
            "permisos y comprueba que tu rol en la página permite crear contenido.",
        )
    if code == 100 and sub == 33:
        return make(
            _P, "meta_not_found", "El objeto ya no existe en Meta (se borró) o no tienes acceso a él."
        )

    # 6) transient / unknown
    if code in (1, 2) or err.get("is_transient") is True or status_code >= 500:
        return make(_T, "meta_transient", "Meta tuvo un problema temporal. Se reintentará.")
    if not err and status_code in (401, 403):
        return make(
            ErrorKind.AUTH, "meta_token_invalid", "Meta rechazó el acceso. Vuelve a conectar la cuenta."
        )
    if not err and status_code < 400:
        return make(_T, "meta_bad_response", "Meta devolvió una respuesta inesperada. Se reintentará.")

    shown = scrub_text(user_message or message).strip()[:300]
    suffix = f" (código {code})" if code is not None else ""
    text = f"Meta rechazó la petición{suffix}" + (f": {shown}" if shown else ".")
    return make(_P, f"meta_error_{code}" if code is not None else "meta_error", text)


# ----------------------------------------------------------------------------------------- requests


def _json_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def graph_request(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    headers: dict | None = None,
    timeout: float | httpx.Timeout = 60.0,
    content: Any = None,
) -> dict:
    """One Graph/rupload request → JSON body as a dict (lists → ``{"data": [...]}``, scalars →
    ``{"success": value}``). Raises a classified :class:`ProviderError` on transport errors, HTTP errors and
    HTTP 200 bodies containing ``error``. ``content`` (extension) sends a raw/streamed body."""
    try:
        response = http.request(
            method.upper(),
            url,
            params=params,
            data=data,
            files=files,
            headers=headers,
            content=content,
            timeout=timeout,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        # Nothing reached Meta: safe to retry even after the commit point.
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "meta_connect_failed",
            "No se pudo conectar con Meta (¿hay conexión a Internet?). Se reintentará.",
            detail={"error": type(exc).__name__, "url": _safe_url(url), "method": method.upper()},
        ) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "meta_timeout",
            "Meta no respondió a tiempo. Se reintentará.",
            detail={"error": type(exc).__name__, "url": _safe_url(url), "method": method.upper()},
        ) from exc
    except httpx.RequestError as exc:
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "meta_network",
            "No se pudo conectar con Meta (¿hay conexión a Internet?). Se reintentará.",
            detail={"error": type(exc).__name__, "url": _safe_url(url), "method": method.upper()},
        ) from exc

    record_usage(response.headers)
    body = _json_body(response)
    has_error = isinstance(body, dict) and bool(body.get("error"))
    if response.status_code >= 400 or has_error:
        exc = classify_meta_error(
            response.status_code, body if isinstance(body, dict) else None, response.headers
        )
        exc.detail.setdefault("url", _safe_url(url))
        raise exc
    if body is None:
        if not response.content.strip():
            return {}
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "meta_bad_response",
            "Meta devolvió una respuesta inesperada. Se reintentará.",
            detail={
                "http_status": response.status_code,
                "url": _safe_url(url),
                "body": scrub_text(response.text[:300]),
            },
        )
    if isinstance(body, dict):
        return body
    if isinstance(body, list):
        return {"data": body}
    return {"success": body}


def graph_get(
    http: httpx.Client, settings: ProviderSettings, path: str, token: str | None, **params: Any
) -> dict:
    """GET ``{graph}/{version}/{path}`` with auth params."""
    query = {**auth_params(settings, token), **form_fields(params)}
    return graph_request(http, "GET", graph_url(settings, path), params=query)


def graph_post(
    http: httpx.Client,
    settings: ProviderSettings,
    path: str,
    token: str | None,
    data: Mapping[str, Any] | None = None,
    *,
    files: dict | None = None,
    timeout: float | httpx.Timeout = 60.0,
) -> dict:
    """POST ``{graph}/{version}/{path}`` with auth + form fields (multipart when ``files`` is given)."""
    form = {**auth_params(settings, token), **form_fields(data or {})}
    return graph_request(http, "POST", graph_url(settings, path), data=form, files=files, timeout=timeout)


def graph_delete(http: httpx.Client, settings: ProviderSettings, path: str, token: str | None) -> dict:
    return graph_request(http, "DELETE", graph_url(settings, path), params=auth_params(settings, token))


def iter_paginated(
    http: httpx.Client,
    url: str,
    params: dict[str, Any],
    *,
    max_pages: int = 10,
) -> Iterator[dict]:
    """Yield ``data`` items following ``paging.next`` (only to graph.facebook.com; auth re-added)."""
    auth = {k: v for k, v in params.items() if k in ("access_token", "appsecret_proof")}
    next_url: str | None = url
    next_params: dict[str, Any] | None = params
    pages = 0
    while next_url and pages < max_pages:
        body = graph_request(http, "GET", next_url, params=next_params)
        pages += 1
        for item in body.get("data") or []:
            if isinstance(item, dict):
                yield item
        nxt = (body.get("paging") or {}).get("next")
        if not nxt:
            break
        parts = urlsplit(nxt)
        if parts.scheme != "https" or parts.hostname != GRAPH_HOST:
            log.warning("Ignoring paging.next to unexpected host %s", parts.hostname)
            break
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(auth)
        next_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        next_params = query


def iter_edge(http: httpx.Client, block: Any, auth: dict[str, Any], *, max_pages: int = 3) -> Iterator[dict]:
    """Items of a nested edge (``{"data": [...], "paging": {"next": ...}}``) plus its following pages."""
    if not isinstance(block, dict):
        return
    for item in block.get("data") or []:
        if isinstance(item, dict):
            yield item
    nxt = (block.get("paging") or {}).get("next")
    if not nxt or max_pages <= 0:
        return
    parts = urlsplit(nxt)
    if parts.scheme != "https" or parts.hostname != GRAPH_HOST:
        return
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: v for k, v in auth.items() if k in ("access_token", "appsecret_proof")})
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    yield from iter_paginated(http, base, query, max_pages=max_pages)


# ----------------------------------------------------------------------------------- commit protocol

#: TRANSIENT codes that prove the request had no effect (safe to treat as "not published").
DEFINITE_TRANSIENT_CODES: frozenset[str] = frozenset(
    {
        "meta_connect_failed",
        "ig_media_not_ready",
        "ig_container_invalid",
        "ig_container_expired",
        "ig_upload_failed",
    }
)


def run_commit(ctx: Any, network_label: str, call: Any, *, sent: dict | None = None) -> dict:
    """Run the single irreversible request of a publish (SPEC §5 engine/provider protocol).

    Persists ``stage="committing"`` + ``commit_at`` through ``ctx.before_commit`` first. A transient failure
    whose effect is unknown becomes ``AMBIGUOUS`` (the engine reconciles, never re-sends); a definite
    rejection marks ``commit_rejected`` so :meth:`reconcile` can answer ``not_published``."""
    state = ctx.state
    if state.get("stage") != "committing":
        state["commit_stage"] = state.get("stage")
    state["stage"] = "committing"
    state["commit_at"] = iso(ctx.clock.now())
    state.pop("commit_rejected", None)
    if sent:
        state["sent"] = sent
    ctx.before_commit(state)
    try:
        return call()
    except ProviderError as exc:
        if exc.kind == ErrorKind.AMBIGUOUS:
            raise
        if exc.kind == ErrorKind.TRANSIENT and exc.code not in DEFINITE_TRANSIENT_CODES:
            raise ProviderError(
                ErrorKind.AMBIGUOUS,
                exc.code,
                f"No se pudo confirmar si {network_label} recibió la publicación. Se comprobará antes de "
                "reintentar.",
                detail=exc.detail,
                retry_after=exc.retry_after,
            ) from exc
        mark_commit_rejected(ctx)
        raise


def mark_commit_rejected(ctx: Any) -> None:
    """The commit request was definitely refused: nothing was published."""
    state = ctx.state
    state["commit_rejected"] = True
    reopen_commit(ctx, save=False)
    ctx.save_state(state)


def reopen_commit(ctx: Any, *, save: bool = True) -> None:
    """Go back to the stage that performs the commit (after a refusal or a ``not_published`` check)."""
    state = ctx.state
    if state.get("stage") == "committing":
        state["stage"] = state.get("commit_stage") or "commit"
    state.pop("commit_at", None)
    if save:
        ctx.save_state(state)


# ------------------------------------------------------------------------------------------- uploads


def _file_chunks(path: Path, offset: int = 0, chunk_size: int = UPLOAD_CHUNK) -> Iterator[bytes]:
    with path.open("rb") as fh:
        fh.seek(offset)
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


def validate_rupload_url(upload_url: str, prefixes: tuple[str, ...]) -> None:
    """Refuse to send the token anywhere but ``https://rupload.facebook.com/<prefix>…`` (§3.3, §4.3)."""
    try:
        parts = urlsplit(upload_url)
        port = parts.port
        ok = (
            parts.scheme == "https"
            and parts.hostname == RUPLOAD_HOST
            and port is None
            and not parts.username
            and parts.path.startswith(prefixes)
        )
        host, path = parts.hostname, parts.path
    except ValueError:
        ok, host, path = False, None, ""
    if not ok:
        raise ProviderError(
            ErrorKind.PERMANENT,
            "meta_bad_upload_url",
            "Meta devolvió una dirección de subida no válida.",
            detail={"host": host, "path": (path or "")[:80]},
        )


def media_size(path: Path, name: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError as exc:
        raise ProviderError(
            ErrorKind.PERMANENT,
            "media_missing",
            f"No se encuentra el archivo «{name}» en la biblioteca de medios.",
            detail={"name": name},
        ) from exc


def rupload(
    http: httpx.Client,
    upload_url: str,
    token: str | None,
    path: Path,
    *,
    prefixes: tuple[str, ...] = ("/video-upload/", "/ig-api-upload/"),
    offset: int = 0,
    timeout: float | httpx.Timeout = UPLOAD_TIMEOUT,
) -> dict:
    """Upload local bytes to rupload.facebook.com (FB Reels / video Stories, IG resumable containers).

    Headers ``Authorization: OAuth <token>``, ``offset``, ``file_size``; body = the file, streamed. Success is
    exactly ``{"success": true}``; anything else raises."""
    if not token:
        raise _no_token_error()
    validate_rupload_url(upload_url, prefixes)
    path = Path(path)
    size = media_size(path, path.name)
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": str(offset),
        "file_size": str(size),
        "Content-Type": "application/octet-stream",
        "Content-Length": str(max(0, size - offset)),
    }
    body = graph_request(
        http, "POST", upload_url, headers=headers, content=_file_chunks(path, offset), timeout=timeout
    )
    if body.get("success") is not True:
        raise ProviderError(
            ErrorKind.TRANSIENT,
            "meta_upload_failed",
            "Meta no confirmó la subida del archivo. Se reintentará.",
            detail={"body": scrub_text(json.dumps(body, ensure_ascii=False)[:300])},
        )
    return body


# ------------------------------------------------------------------------------------------- images


def _derived_via_media(info: MediaInfo, max_bytes: int) -> Path | None:
    """``media.derived_jpeg`` through the asset row (the Media module's cached sRGB JPEG), if available."""
    try:
        from planificador import db as dbmod
        from planificador import media
        from planificador.models import MediaAsset

        database = dbmod.get_current_database()
    except Exception:
        return None
    try:
        with database.session_scope() as session:
            asset = session.get(MediaAsset, info.id)
            if asset is None:
                return None
            path = Path(media.derived_jpeg(asset, max_bytes))
        if path.is_file() and path.stat().st_size <= max_bytes:
            return path
    except NotImplementedError:
        return None
    except Exception:
        log.warning("media.derived_jpeg failed for media %s; converting locally", info.id, exc_info=True)
    return None


def _derived_dir() -> Path:
    try:
        from planificador.paths import get_paths

        directory = get_paths().derived_dir
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except Exception:
        directory = Path(tempfile.gettempdir()) / "planificador-derived"
        directory.mkdir(parents=True, exist_ok=True)
        return directory


def convert_to_jpeg(info: MediaInfo, max_bytes: int) -> Path:
    """Local Pillow fallback: EXIF-rotated RGB JPEG no larger than ``max_bytes`` (cached by content)."""
    try:
        stat = info.path.stat()
    except OSError as exc:
        raise ProviderError(
            ErrorKind.PERMANENT,
            "media_missing",
            f"No se encuentra el archivo «{info.original_name}» en la biblioteca de medios.",
            detail={"name": info.original_name},
        ) from exc
    key = hashlib.sha256(f"{info.path}|{stat.st_size}|{stat.st_mtime_ns}|{max_bytes}".encode()).hexdigest()
    target = _derived_dir() / f"meta-{key[:24]}.jpg"
    if target.is_file() and target.stat().st_size <= max_bytes:
        return target
    try:
        from PIL import Image, ImageOps

        with Image.open(info.path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        tmp = target.with_suffix(".tmp")
        for _ in range(8):
            for quality in (92, 85, 78, 70):
                image.save(tmp, "JPEG", quality=quality, optimize=True)
                if tmp.stat().st_size <= max_bytes:
                    tmp.replace(target)
                    return target
            image = image.resize((max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8))))
    except Exception as exc:
        raise ProviderError(
            ErrorKind.PERMANENT,
            "image_conversion_failed",
            f"No se pudo convertir la imagen «{info.original_name}» a JPEG.",
            detail={"name": info.original_name, "error": type(exc).__name__},
        ) from exc
    raise ProviderError(
        ErrorKind.PERMANENT,
        "image_too_large",
        f"No se pudo reducir la imagen «{info.original_name}» al tamaño máximo permitido.",
        detail={"name": info.original_name},
    )


def prepare_image(
    info: MediaInfo,
    max_bytes: int,
    *,
    accept: tuple[str, ...] = ("image/jpeg",),
    prefer_derived: bool = False,
) -> tuple[Path, str]:
    """Image file ready for Meta: the original when its type is in ``accept`` and it fits ``max_bytes``
    (unless ``prefer_derived``), else ``media.derived_jpeg`` or a local JPEG conversion."""
    original_ok = info.mime in accept and info.size_bytes <= max_bytes and info.path.is_file()
    if original_ok and not prefer_derived:
        return info.path, info.mime
    derived = _derived_via_media(info, max_bytes)
    if derived is not None:
        return derived, "image/jpeg"
    if original_ok and info.mime == "image/jpeg":
        return info.path, "image/jpeg"
    return convert_to_jpeg(info, max_bytes), "image/jpeg"


# ----------------------------------------------------------------------------------------- insights


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict):
        total: float | int = 0
        found = False
        for v in value.values():
            n = _number(v)
            if n is not None:
                total += n
                found = True
        return total if found else None
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def insight_value(entry: Mapping[str, Any], day: date | None = None) -> float | int | None:
    """Value of one insights entry: ``total_value`` first, else the value for ``day`` (``end_time`` marks the
    end of the period) or the last one. Breakdown dicts are summed (§5 general rules)."""
    total = entry.get("total_value")
    if isinstance(total, dict):
        if "value" in total:
            return _number(total.get("value"))
        values = [
            _number(result.get("value"))
            for breakdown in (total.get("breakdowns") or [])
            if isinstance(breakdown, dict)
            for result in (breakdown.get("results") or [])
            if isinstance(result, dict)
        ]
        found = [v for v in values if v is not None]
        return sum(found) if found else None
    values_list = [v for v in (entry.get("values") or []) if isinstance(v, dict)]
    if not values_list:
        return None
    if day is not None:
        for item in values_list:
            end = parse_time(item.get("end_time"))
            # end_time is the end of the period: midnight Pacific (07/08 UTC) or UTC of the next day
            if end is not None and (end - timedelta(days=1)).date() == day:
                return _number(item.get("value"))
    return _number(values_list[-1].get("value"))


def fetch_insights(
    http: httpx.Client, url: str, params: dict[str, Any], metrics: list[str]
) -> dict[str, dict]:
    """Request ``metrics`` in one call; when Meta rejects it, fall back to one metric at a time and skip the
    ones it refuses (deprecated / invalid metric, §5). Auth, rate-limit and transient errors propagate."""
    if not metrics:
        return {}

    def request(metric_list: list[str]) -> dict[str, dict]:
        body = graph_request(http, "GET", url, params={**params, "metric": ",".join(metric_list)})
        out: dict[str, dict] = {}
        for entry in body.get("data") or []:
            if isinstance(entry, dict) and entry.get("name"):
                out[str(entry["name"])] = entry
        return out

    try:
        return request(metrics)
    except ProviderError as exc:
        if exc.kind != ErrorKind.PERMANENT:
            raise
        if len(metrics) == 1:
            log.info("Insights metric %s unavailable: %s", metrics[0], exc.code)
            return {}
    result: dict[str, dict] = {}
    for metric in metrics:
        try:
            result.update(request([metric]))
        except ProviderError as exc:
            if exc.kind != ErrorKind.PERMANENT:
                raise
            log.info("Insights metric %s unavailable: %s", metric, exc.code)
    return result


def day_window(day: date) -> tuple[int, int]:
    """UNIX ``since``/``until`` covering one UTC day."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return to_unix(start), to_unix(start + timedelta(days=1))


def engagement(metrics: Mapping[str, float | int]) -> float | None:
    """interactions / reach × 100 (or / views when there is no reach) — SPEC §12."""
    interactions = metrics.get("interactions")
    if interactions is None:
        return None
    base = metrics.get("reach") or metrics.get("views")
    if not base:
        return None
    return round(float(interactions) / float(base) * 100, 2)


# ----------------------------------------------------------------------------------- Facebook Login


@dataclass
class MetaIgAccount:
    id: str
    username: str | None = None
    name: str | None = None
    picture_url: str | None = None
    account_type: str | None = None  # not exposed by the Facebook-Login API → None (unknown)
    followers_count: int | None = None
    media_count: int | None = None


@dataclass
class MetaPage:
    id: str
    name: str
    access_token: str
    tasks: list[str] = field(default_factory=list)
    picture_url: str | None = None
    instagram: MetaIgAccount | None = None
    username: str | None = None
    category: str | None = None
    followers_count: int | None = None

    @property
    def can_publish(self) -> bool:
        """``CREATE_CONTENT`` in tasks. Pages listed without tasks get the benefit of the doubt."""
        return not self.tasks or TASK_CREATE_CONTENT in self.tasks


def _require_app(settings: ProviderSettings, *, secret: bool) -> None:
    if not settings.meta_app_id or (secret and not settings.meta_app_secret):
        raise ProviderError(
            ErrorKind.PERMANENT,
            "meta_not_configured",
            "Configura el App ID y el App Secret de tu app de Meta en Ajustes antes de conectar.",
        )


def build_login_url(
    settings: ProviderSettings,
    redirect_uri: str,
    state: str,
    *,
    config_id: str | None = None,
    scopes: list[str] | None = None,
) -> str:
    """Facebook Login dialog URL: ``scope`` by default, or a Login-for-Business ``config_id`` (§2.1–§2.2)."""
    _require_app(settings, secret=False)
    params: dict[str, str] = {
        "client_id": str(settings.meta_app_id),
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "auth_type": "rerequest",
    }
    if config_id:
        params["config_id"] = config_id
        params["override_default_response_type"] = "true"
    else:
        params["scope"] = ",".join(scopes or SCOPES_FB_IG)
    return f"{DIALOG_BASE}/{api_version(settings)}/dialog/oauth?{urlencode(params)}"


def exchange_code(
    settings: ProviderSettings,
    http: httpx.Client,
    code: str,
    redirect_uri: str,
    *,
    clock: Any = None,
) -> tuple[str, datetime | None]:
    """Authorization code → short-lived user token → long-lived user token (~60 days).

    Returns ``(user_token, expires_at)``; ``expires_at`` is None when Meta does not report it."""
    _require_app(settings, secret=True)
    short = graph_request(
        http,
        "GET",
        graph_url(settings, "oauth/access_token"),
        params={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    short_token = short.get("access_token")
    if not short_token:
        raise ProviderError(
            ErrorKind.PERMANENT,
            "meta_oauth_failed",
            "Meta no devolvió un token de acceso. Vuelve a intentar la conexión.",
        )
    long = graph_request(
        http,
        "GET",
        graph_url(settings, "oauth/access_token"),
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "fb_exchange_token": short_token,
        },
    )
    if long.get("access_token"):
        token, expires_in = str(long["access_token"]), _to_int(long.get("expires_in"))
    else:
        token, expires_in = str(short_token), _to_int(short.get("expires_in"))
    now = clock.now() if clock is not None else datetime.now(UTC)
    expires_at = now + timedelta(seconds=expires_in) if expires_in else None
    return token, expires_at


PAGE_FIELDS = (
    "id,name,username,access_token,category,tasks,followers_count,picture.type(large){url},"
    "instagram_business_account{id,username,name,profile_picture_url,followers_count,media_count}"
)


def _page_from(item: Mapping[str, Any]) -> MetaPage | None:
    page_id = item.get("id")
    token = item.get("access_token")
    if not page_id or not token:
        return None
    picture = item.get("picture")
    picture_url = None
    if isinstance(picture, dict):
        data = picture.get("data")
        picture_url = data.get("url") if isinstance(data, dict) else picture.get("url")
    ig = item.get("instagram_business_account")
    instagram = None
    if isinstance(ig, dict) and ig.get("id"):
        instagram = MetaIgAccount(
            id=str(ig["id"]),
            username=ig.get("username"),
            name=ig.get("name"),
            picture_url=ig.get("profile_picture_url"),
            account_type=None,
            followers_count=_to_int(ig.get("followers_count")),
            media_count=_to_int(ig.get("media_count")),
        )
    return MetaPage(
        id=str(page_id),
        name=str(item.get("name") or page_id),
        access_token=str(token),
        tasks=[str(t) for t in (item.get("tasks") or []) if t],
        picture_url=picture_url,
        instagram=instagram,
        username=item.get("username"),
        category=item.get("category"),
        followers_count=_to_int(item.get("followers_count")),
    )


def list_pages(
    settings: ProviderSettings,
    http: httpx.Client,
    user_token: str,
    *,
    include_business: bool = False,
    max_pages: int = 20,
) -> list[MetaPage]:
    """Pages the user manages (``/me/accounts``, paginated) with Page tokens, tasks and the linked Instagram
    professional account. ``include_business`` also walks ``/me/businesses`` owned/client pages (best effort;
    needs ``business_management``)."""
    params = {**auth_params(settings, user_token), "fields": PAGE_FIELDS, "limit": "100"}
    pages: dict[str, MetaPage] = {}
    for item in iter_paginated(http, graph_url(settings, "me/accounts"), params, max_pages=max_pages):
        page = _page_from(item)
        if page and page.id not in pages:
            pages[page.id] = page
    if include_business:
        try:
            biz_params = {**auth_params(settings, user_token), "fields": "id,name", "limit": "50"}
            businesses = list(
                iter_paginated(http, graph_url(settings, "me/businesses"), biz_params, max_pages=5)
            )
            for biz in businesses:
                for edge in ("owned_pages", "client_pages"):
                    url = graph_url(settings, f"{biz.get('id')}/{edge}")
                    for item in iter_paginated(http, url, params, max_pages=5):
                        page = _page_from(item)
                        if page and page.id not in pages:
                            pages[page.id] = page
        except ProviderError as exc:
            if is_token_error(exc):
                raise
            log.info("Business pages lookup skipped: %s", exc.code)
    return list(pages.values())


def page_warnings(page: MetaPage) -> list[str]:
    """Spanish warnings for the page chooser (SPEC §6)."""
    warnings: list[str] = []
    if not page.can_publish:
        warnings.append(
            "Tu rol en esta página no permite crear contenido (falta la tarea CREATE_CONTENT): no se podrá "
            "publicar."
        )
    if page.instagram is None:
        warnings.append(
            "No hay ninguna cuenta profesional de Instagram vinculada a esta página. Vincúlala desde la "
            "configuración de la página para publicar en Instagram."
        )
    return warnings


def granted_permissions(settings: ProviderSettings, http: httpx.Client, user_token: str) -> list[str]:
    """Scopes with ``status == granted`` (``/me/permissions``)."""
    body = graph_request(
        http, "GET", graph_url(settings, "me/permissions"), params=auth_params(settings, user_token)
    )
    return [
        str(p.get("permission"))
        for p in body.get("data") or []
        if isinstance(p, dict) and p.get("status") == "granted" and p.get("permission")
    ]


def missing_scopes(granted: list[str], required: list[str] | None = None) -> list[str]:
    have = set(granted)
    return [s for s in (required or SCOPES_FB_IG) if s not in have]


def debug_token(settings: ProviderSettings, http: httpx.Client, token: str) -> dict:
    """``/debug_token`` with the app token → ``data`` (is_valid, scopes, expiry dates)."""
    _require_app(settings, secret=True)
    body = graph_request(
        http,
        "GET",
        graph_url(settings, "debug_token"),
        params={"input_token": token, "access_token": f"{settings.meta_app_id}|{settings.meta_app_secret}"},
    )
    data = body.get("data")
    return data if isinstance(data, dict) else {}

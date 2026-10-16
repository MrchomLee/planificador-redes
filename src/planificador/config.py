"""Settings: process settings (env/CLI) and user settings stored in the ``settings`` table (SPEC §4).

Two layers:

- :class:`AppSettings` — process-level (data dir, port, scheduler on/off). Built by :func:`load_app_settings`
  from env vars and explicit overrides; ``create_app`` installs it as the *current* settings.
- DB settings — ``get_setting`` / ``set_setting`` / ``get_secret``. Values are JSON. Secrets are stored as
  ``{"enc": "v1:..."}`` (Fernet, see :mod:`planificador.crypto`). Keys in :data:`SECRET_KEYS` are always
  encrypted, even when ``secret=True`` is forgotten.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from planificador import DEFAULT_PORT, crypto
from planificador.models import Setting, utcnow
from planificador.paths import DataPaths, resolve_data_dir
from planificador.providers.base import ProviderSettings

ENV_PORT = "PLANIFICADOR_PORT"
ENV_NO_SCHEDULER = "PLANIFICADOR_NO_SCHEDULER"

_TRUE = {"1", "true", "yes", "si", "sí", "on"}

# ------------------------------------------------------------------------------------- DB setting keys

#: Defaults for user settings (``get_setting`` falls back to these).
DEFAULTS: dict[str, Any] = {
    "meta_app_id": None,
    "meta_app_secret": None,  # secret
    "meta_api_version": "v25.0",
    "meta_config_id": None,  # Facebook Login for Business configuration id (optional)
    "tiktok_client_key": None,
    "tiktok_client_secret": None,  # secret
    "tiktok_pkce_encoding": "hex",  # "hex" | "base64url"
    "tiktok_scopes": None,  # scopes the user's TikTok app has (list); None = derive (see tiktok_scopes())
    "tiktok_analytics_scopes": False,  # legacy (before tiktok_scopes): also request the Display API scopes
    "google_client_id": None,  # YouTube
    "google_client_secret": None,  # secret
    "grace_minutes": 120,  # only used when publish_late is off
    # Late publishing (SPEC §10): a post that could not go out at its time (PC off/asleep, app closed, no
    # internet) is published as soon as possible instead of becoming "missed". On by default, also for
    # installs that never saved it (the missing key falls back to this default).
    "publish_late": True,
    "publish_late_max_hours": 0,  # 0 = no limit; later than this → "missed" anyway
    "catchup_spacing_minutes": 15,  # minimum gap between overdue posts of the same account
    "public_media_mode": "off",  # "off" | "tunnel" | "base_url"
    "public_media_base_url": None,
    "public_media_tunnel_consent_at": None,  # ISO timestamp of the user's consent to download cloudflared
    "default_timezone": "Europe/Madrid",
    "demo_mode": False,
    "password_hash": None,  # managed by security.set_password
    "notifications_desktop": True,
    "notifications_browser": True,
    "active_brand_id": None,  # managed by deps.set_active_brand
}

#: Keys whose values are always encrypted at rest.
SECRET_KEYS: frozenset[str] = frozenset({"meta_app_secret", "tiktok_client_secret", "google_client_secret"})

PUBLIC_MEDIA_MODES = ("off", "tunnel", "base_url")
PKCE_ENCODINGS = ("hex", "base64url")
#: Ajustes › Publicación choices: "Límite de retraso" (hours, 0 = no limit) and catch-up spacing (minutes).
PUBLISH_LATE_MAX_HOURS_CHOICES: tuple[int, ...] = (0, 2, 6, 12, 24)
CATCHUP_SPACING_CHOICES: tuple[int, ...] = (5, 10, 15, 30, 60)
MAX_PUBLISH_LATE_HOURS = 24 * 30
MAX_CATCHUP_SPACING_MINUTES = 24 * 60

_MISSING: Any = object()


# ------------------------------------------------------------------------------------- process settings


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


@dataclass
class AppSettings:
    """Process-level settings (not stored in the DB)."""

    data_dir: Path
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"  # never 0.0.0.0
    scheduler_enabled: bool = True
    public_media_port: int | None = None  # default: port + 1
    log_to_console: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def paths(self) -> DataPaths:
        return DataPaths(self.data_dir)

    @property
    def base_url(self) -> str:
        """Canonical origin. Always ``localhost`` (cookies set on localhost are not sent to 127.0.0.1)."""
        return f"http://localhost:{self.port}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {f"localhost:{self.port}", f"127.0.0.1:{self.port}"}
        if self.port == 80:
            hosts |= {"localhost", "127.0.0.1"}
        return frozenset(hosts)

    @property
    def allowed_origins(self) -> frozenset[str]:
        origins = {f"http://localhost:{self.port}", f"http://127.0.0.1:{self.port}"}
        if self.port == 80:
            origins |= {"http://localhost", "http://127.0.0.1"}
        return frozenset(origins)

    @property
    def pubmedia_port(self) -> int:
        return self.public_media_port or (self.port + 1)


def load_app_settings(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    port: int | None = None,
    scheduler_enabled: bool | None = None,
    **overrides: Any,
) -> AppSettings:
    """Build :class:`AppSettings` from env vars (``PLANIFICADOR_DATA_DIR``, ``PLANIFICADOR_PORT``,
    ``PLANIFICADOR_NO_SCHEDULER``) and explicit overrides (which win)."""
    if port is None:
        env_port = os.environ.get(ENV_PORT, "").strip()
        port = int(env_port) if env_port.isdigit() else DEFAULT_PORT
    if scheduler_enabled is None:
        scheduler_enabled = not _env_bool(ENV_NO_SCHEDULER)
    settings = AppSettings(
        data_dir=resolve_data_dir(data_dir),
        port=int(port),
        scheduler_enabled=bool(scheduler_enabled),
    )
    if overrides:
        known = {k: v for k, v in overrides.items() if k in AppSettings.__dataclass_fields__}
        unknown = {k: v for k, v in overrides.items() if k not in known}
        settings = replace(settings, **known)
        settings.extra.update(unknown)
    return settings


_current: AppSettings | None = None


def set_app_settings(settings: AppSettings | None) -> None:
    global _current
    _current = settings


def get_app_settings() -> AppSettings:
    """Settings of the running app (installed by ``create_app``), else freshly loaded from env."""
    return _current if _current is not None else load_app_settings()


# ------------------------------------------------------------------------------------------ DB settings


def _decode(key: str, raw: Any) -> Any:
    if isinstance(raw, dict) and set(raw) == {"enc"}:
        return crypto.decrypt_optional(raw["enc"], on_error="none")
    return raw


def get_setting(db: Session, key: str, default: Any = _MISSING) -> Any:
    """Return the stored value (secrets decrypted). Missing/null → ``default`` or :data:`DEFAULTS`."""
    raw = db.execute(select(Setting.value).where(Setting.key == key)).scalar_one_or_none()
    value = _decode(key, raw) if raw is not None else None
    if value is None:
        if default is not _MISSING:
            return default
        return DEFAULTS.get(key)
    return value


def get_secret(db: Session, key: str) -> str | None:
    """Decrypted secret value or ``None``."""
    raw = db.execute(select(Setting.value).where(Setting.key == key)).scalar_one_or_none()
    if raw is None:
        return None
    if isinstance(raw, dict) and "enc" in raw:
        return crypto.decrypt_optional(raw["enc"], on_error="none")
    return raw if isinstance(raw, str) and raw else None  # legacy plaintext


def is_secret_set(db: Session, key: str) -> bool:
    return bool(get_secret(db, key))


def set_setting(db: Session, key: str, value: Any, secret: bool = False, *, commit: bool = True) -> None:
    """Upsert a setting. ``secret=True`` (or a key in :data:`SECRET_KEYS`) encrypts ``value`` (a str).

    Empty secrets (``None``/``""``) clear the value. Commits by default; pass ``commit=False`` to only flush
    as part of a larger transaction.
    """
    if secret or key in SECRET_KEYS:
        if value is None or value == "":
            stored: Any = None
        else:
            if not isinstance(value, str):
                raise TypeError(f"Secret setting {key!r} must be a string")
            stored = {"enc": crypto.encrypt(value)}
    else:
        stored = value
    db.flush()
    stmt = sqlite_insert(Setting).values(key=key, value=stored, updated_at=utcnow())
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
    )
    db.execute(stmt)
    # The upsert bypasses the ORM identity map: expire a loaded Setting object so it re-reads the value.
    existing = db.identity_map.get(Session.identity_key(Setting, key))
    if existing is not None:
        db.expire(existing)
    if commit:
        db.commit()


def delete_setting(db: Session, key: str, *, commit: bool = True) -> None:
    db.execute(delete(Setting).where(Setting.key == key))
    if commit:
        db.commit()


def get_bool(db: Session, key: str, default: bool | None = None) -> bool:
    value = get_setting(db, key, DEFAULTS.get(key) if default is None else default)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return bool(value)


def get_int(db: Session, key: str, default: int | None = None) -> int:
    fallback = DEFAULTS.get(key) if default is None else default
    value = get_setting(db, key, fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback or 0)


def get_timestamp(db: Session, key: str) -> datetime | None:
    """Read an ISO timestamp setting (used for periodic-job bookkeeping)."""
    value = get_setting(db, key, None)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    from planificador.timeutil import ensure_utc

    return ensure_utc(dt)


def set_timestamp(db: Session, key: str, when: datetime, *, commit: bool = True) -> None:
    from planificador.timeutil import ensure_utc

    set_setting(db, key, ensure_utc(when).isoformat(), commit=commit)


def is_demo_mode(db: Session) -> bool:
    return get_bool(db, "demo_mode")


def default_timezone(db: Session) -> str:
    from planificador.timeutil import DEFAULT_TIMEZONE, is_valid_timezone

    tz = get_setting(db, "default_timezone")
    return tz if is_valid_timezone(tz) else DEFAULT_TIMEZONE


def tiktok_scopes(db: Session) -> list[str]:
    """Scopes requested at TikTok login: the ones the user ticked in Ajustes (``tiktok_scopes``, normalised,
    ``user.info.basic`` always included). Unset or unusable (no publishing scope) → derived from the legacy
    ``tiktok_analytics_scopes`` flag: the default scopes, plus the Display API ones when it was on."""
    from planificador.providers import tiktok

    raw = get_setting(db, "tiktok_scopes", None)
    if isinstance(raw, str | list | tuple):
        scopes = tiktok.normalize_scopes(raw, known_only=True)
        if tiktok.PUBLISH_SCOPES & set(scopes):
            return scopes
    legacy = list(tiktok.DEFAULT_SCOPES)
    if get_bool(db, "tiktok_analytics_scopes"):
        legacy += list(tiktok.SCOPES_ANALYTICS)
    return tiktok.normalize_scopes(legacy, known_only=True)


def provider_settings(db: Session, base_url: str | None = None) -> ProviderSettings:
    """Snapshot of everything providers need (decrypted app secrets included — never log it)."""
    encoding = get_setting(db, "tiktok_pkce_encoding")
    if encoding not in PKCE_ENCODINGS:
        encoding = DEFAULTS["tiktok_pkce_encoding"]
    return ProviderSettings(
        meta_app_id=(get_setting(db, "meta_app_id") or None),
        meta_app_secret=get_secret(db, "meta_app_secret"),
        meta_api_version=str(get_setting(db, "meta_api_version") or DEFAULTS["meta_api_version"]),
        tiktok_client_key=(get_setting(db, "tiktok_client_key") or None),
        tiktok_client_secret=get_secret(db, "tiktok_client_secret"),
        tiktok_pkce_encoding=encoding,
        base_url=base_url or get_app_settings().base_url,
        demo_mode=get_bool(db, "demo_mode"),
        google_client_id=(get_setting(db, "google_client_id") or None),
        google_client_secret=get_secret(db, "google_client_secret"),
        meta_config_id=(str(get_setting(db, "meta_config_id") or "").strip() or None),
        tiktok_scopes=tiktok_scopes(db),
    )


def public_settings(db: Session) -> dict[str, Any]:
    """All known settings for display; secrets replaced by a masked hint (``****abcd``) or ``""``."""
    out: dict[str, Any] = {}
    for key in DEFAULTS:
        if key == "password_hash":
            out["password_set"] = bool(get_setting(db, key))
            continue
        if key in SECRET_KEYS:
            out[key] = crypto.mask_secret(get_secret(db, key))
            continue
        out[key] = get_setting(db, key)
    return out

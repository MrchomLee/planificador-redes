"""Provider interface — the CONTRACT shared by every network implementation (SPEC §5).

The publishing engine drives a provider through :meth:`Provider.publish_step` repeatedly; each call performs
the next unit of work based on ``ctx.state`` and must be re-entrant from any persisted state. Before the
single irreversible request a provider **must** call ``ctx.before_commit(state)``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from planificador.timeutil import Clock

# ---------------------------------------------------------------------------------------------- errors


class ErrorKind(StrEnum):
    TRANSIENT = "transient"  # network blip, 5xx, timeout BEFORE the commit point → retry with backoff
    RATE_LIMITED = "rate_limited"  # retry after retry_after / backoff
    AUTH = "auth"  # token expired/revoked/missing permission → account needs_reconnect
    PERMANENT = "permanent"  # invalid media, policy rejection... → error, no retry
    AMBIGUOUS = "ambiguous"  # commit may or may not have happened → reconcile, never re-send


_SECRET_KEYS = re.compile(
    r"(access_token|refresh_token|client_secret|app_secret|fb_exchange_token|input_token|code_verifier|"
    r"appsecret_proof|authorization|password|secret|^token$)",
    re.IGNORECASE,
)
_SECRET_IN_TEXT = re.compile(
    r"(?P<key>access_token|refresh_token|client_secret|fb_exchange_token|input_token|code_verifier|"
    r"appsecret_proof|csrf_token|(?<=[?&])code)(?P<sep>[\"']?\s*[=:]\s*[\"']?)(?P<val>[^&\s\"',}]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer|OAuth)\s+[A-Za-z0-9._\-~+/=]{8,}", re.IGNORECASE)
REDACTED = "***"


def scrub_text(text: str) -> str:
    """Redact tokens/secrets that may appear in URLs, query strings, headers or JSON-ish text."""
    if not text:
        return text
    text = _SECRET_IN_TEXT.sub(lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", text)
    return _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)


def scrub(value: Any, _depth: int = 0) -> Any:
    """Return a deep copy of ``value`` with secret-looking keys masked and token-looking text redacted."""
    if _depth > 20:
        return REDACTED
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEYS.search(k):
                out[k] = REDACTED if v not in (None, "") else v
            else:
                out[k] = scrub(v, _depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [scrub(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


class ProviderError(Exception):
    """Error raised by providers. ``message`` is Spanish and user-facing; ``detail`` is scrubbed tech info."""

    def __init__(
        self,
        kind: ErrorKind,
        code: str,
        message: str,
        *,
        detail: dict | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.kind = ErrorKind(kind)
        self.code = code
        self.message = message
        self.detail: dict = scrub(detail) if detail else {}
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "retry_after": self.retry_after,
        }

    def __repr__(self) -> str:
        return f"ProviderError(kind={self.kind!s}, code={self.code!r}, message={self.message!r})"


# ------------------------------------------------------------------------------------------ data types


@dataclass
class MediaInfo:  # built from MediaAsset (media.to_media_info)
    id: int
    kind: str  # "image" | "video"
    path: Path
    mime: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_s: float | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    bitrate: int | None
    is_hdr: bool
    original_name: str


@dataclass
class TargetSpec:
    network: str
    format: str
    text: str
    media: list[MediaInfo]
    options: dict
    scheduled_at: datetime | None
    auto_publish: bool


@dataclass
class Issue:
    level: Literal["error", "warning"]
    code: str
    message: str
    field: str | None = None


@dataclass
class AccountCreds:
    id: int
    network: str
    external_id: str
    name: str
    account_type: str | None
    access_token: str | None
    refresh_token: str | None
    token_expires_at: datetime | None
    refresh_expires_at: datetime | None
    meta: dict
    username: str | None = None
    scopes: list[str] = field(default_factory=list)


@dataclass
class TokenUpdate:
    access_token: str
    token_expires_at: datetime | None
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None
    scopes: list[str] | None = None


@dataclass
class ProviderSettings:  # built by config.provider_settings(db)
    meta_app_id: str | None
    meta_app_secret: str | None
    meta_api_version: str  # default "v25.0"
    tiktok_client_key: str | None
    tiktok_client_secret: str | None
    tiktok_pkce_encoding: str  # "hex" (default) | "base64url"
    base_url: str  # "http://localhost:8765"
    demo_mode: bool
    google_client_id: str | None = None  # YouTube (Google OAuth client)
    google_client_secret: str | None = None  # secret
    meta_config_id: str | None = None  # Facebook Login for Business configuration id (optional)
    tiktok_analytics_scopes: bool = False  # also request TikTok's analytics scopes at login


class PublicMedia(Protocol):  # implemented by publicmedia.PublicMediaService
    def url_for(self, path: Path, mime: str, ttl_seconds: int = 3600) -> str: ...  # public HTTPS URL

    def release(self, url: str) -> None: ...


@dataclass
class PublishContext:
    account: AccountCreds
    target: TargetSpec
    target_id: int
    state: dict  # provider_state (mutable copy)
    settings: ProviderSettings
    http: httpx.Client
    clock: Clock  # timeutil.Clock (now() -> aware UTC)
    public_media: PublicMedia | None
    save_state: Callable[[dict], None]  # persist provider_state immediately (own transaction)
    before_commit: Callable[
        [dict], None
    ]  # persist state + commit_phase=commit_sent BEFORE the irreversible call
    log: Callable[[str, str, dict | None], None]  # (event, spanish_message, data) -> post_events
    # Optional: persist tokens refreshed mid-run (e.g. during a long upload). None = keep them in memory.
    update_tokens: Callable[[TokenUpdate], None] | None = None


@dataclass
class StepResult:
    status: Literal["continue", "wait", "published", "sent_to_app"]
    wait_seconds: int = 0  # for "wait": engine re-runs publish_step after this delay
    external_id: str | None = None
    external_url: str | None = None
    message: str | None = None  # Spanish, for post_events


@dataclass
class ReconcileResult:
    outcome: Literal["published", "not_published", "unknown"]
    external_id: str | None = None
    external_url: str | None = None
    message: str | None = None


#: ``provider_state`` key holding a pending follow-up check of a finished target:
#: ``{"next_at": iso datetime, ...provider data}`` (see :meth:`Provider.follow_up`).
FOLLOWUP_KEY = "followup"


@dataclass
class FollowUpResult:
    """Outcome of :meth:`Provider.follow_up`, applied by ``providers.followups``."""

    next_at: datetime | None = None  # check again at this time; None = nothing left to check
    external_id: str | None = None  # a better id / URL for the target (e.g. TikTok's public post id)
    external_url: str | None = None
    message: str | None = None  # Spanish, written to post_events
    level: Literal["info", "warning", "error"] = "info"
    notify: str | None = None  # Spanish notification body for something the user must act on
    failed: ProviderError | None = None  # the post is not live after all: the target becomes ``error``


@dataclass
class FormatSpec:
    key: str
    label: str  # e.g. ("reel", "Reel")
    media: Literal["none_or_images", "images", "video", "image_or_video", "images_or_video"]


@dataclass
class OptionSpec:  # optional helper for templates; not required
    key: str
    label: str
    kind: Literal["bool", "text", "number", "select", "list"] = "text"
    default: Any = None
    choices: list[tuple[str, str]] = field(default_factory=list)  # (value, Spanish label)
    help: str | None = None
    formats: list[str] | None = None  # formats this option applies to (None = all)


@dataclass
class AccountSnapshot:  # analytics, one day
    day: date
    metrics: dict[str, float | int]  # canonical keys, see SPEC §12


@dataclass
class PostMetricData:
    external_id: str
    published_at: datetime | None
    permalink: str | None
    caption: str | None
    media_type: str | None
    thumbnail_url: str | None
    metrics: dict[str, float | int]


@dataclass
class CommentData:
    external_id: str
    parent_external_id: str | None
    object_external_id: str
    object_permalink: str | None
    author_name: str | None
    author_id: str | None
    text: str
    created_time: datetime
    from_us: bool
    raw: dict


# ---------------------------------------------------------------------------------------------- base


class Provider(ABC):
    network: str
    display_name: str
    formats: list[FormatSpec]
    supports_comments: bool = False
    supports_analytics: bool = True

    def validate(self, target: TargetSpec) -> list[Issue]:
        from planificador import rules

        return rules.validate_target(target)

    @abstractmethod
    def publish_step(self, ctx: PublishContext) -> StepResult:  # may raise ProviderError
        ...

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        return ReconcileResult(
            "unknown",
            message="No se pudo comprobar en la red si la publicación salió. Revísalo manualmente.",
        )

    def refresh_credentials(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
    ) -> TokenUpdate | None:
        return None

    def follow_up(self, ctx: PublishContext) -> FollowUpResult | None:
        """Check a target the engine already finished (``published``) again, when its state holds
        ``state[FOLLOWUP_KEY]["next_at"]`` (set by the provider while publishing): outcomes only known later,
        e.g. TikTok's public post id after moderation or a YouTube ``publishAt`` that should have gone public.
        Run by the scheduler through ``providers.followups``; ``ctx.before_commit`` must not be used.
        ``None`` = nothing to check."""
        return None

    def check_account(self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> None:
        """Raise ``ProviderError(AUTH)`` if the account's credentials are broken. Default: no check."""
        return None

    def fetch_account_snapshot(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, day: date
    ) -> AccountSnapshot:
        return AccountSnapshot(day=day, metrics={})

    def fetch_recent_posts(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[PostMetricData]:
        return []

    def fetch_comments(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[CommentData]:
        return []

    def reply_comment(
        self,
        account: AccountCreds,
        settings: ProviderSettings,
        http: httpx.Client,
        comment_external_id: str,
        text: str,
    ) -> str:
        raise ProviderError(
            ErrorKind.PERMANENT,
            "comments_not_supported",
            f"{self.display_name} no permite responder comentarios desde la app.",
        )

    def format_spec(self, key: str) -> FormatSpec | None:
        for f in self.formats:
            if f.key == key:
                return f
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} network={getattr(self, 'network', '?')}>"

"""ORM models (SPEC §4). SQLAlchemy 2 declarative.

Conventions:

- Every timestamp column uses :class:`UTCDateTime`: values are stored as naive UTC in SQLite and always come
  back as **aware UTC** datetimes. Naive values written by callers are assumed to be UTC, and assignments are
  normalised to aware UTC immediately (sessions use ``expire_on_commit=False``, so an assigned value stays
  in memory: without this a naive value would leak into comparisons with ``clock.now()``).
- Enums are ``StrEnum`` stored as their string value (:class:`StrEnumType`); plain strings are accepted on
  assignment. Unknown values read from the DB are returned as plain strings (forward compatible).
- JSON dict/list columns are mutation-tracked at the top level (``MutableDict`` / ``MutableList``), so
  ``target.options["x"] = 1`` is persisted. Nested mutations still require reassigning the column.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    select,
    update,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:  # pragma: no cover
    from planificador.providers.base import AccountCreds, TokenUpdate


# --------------------------------------------------------------------------------------- column types


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC, returns aware UTC."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expects datetime, got {type(value).__name__}")
        if value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None:
            value = value.astimezone(UTC)
        return value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class StrEnumType(TypeDecorator[Any]):
    """Stores a ``StrEnum`` as its string value (no CHECK constraint, so new members need no migration).

    ``strict=True`` (default) rejects unknown values on write (``ValueError`` → ``StatementError`` at flush);
    ``strict=False`` stores unknown strings as-is (used for open-ended labels such as notification kinds).
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[StrEnum], length: int = 32, strict: bool = True) -> None:
        super().__init__(length=length)
        self.enum_cls = enum_cls
        self.strict = strict

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        try:
            return self.enum_cls(value).value  # validates plain strings too
        except ValueError:
            if self.strict:
                raise
            return str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        if value is None:
            return None
        try:
            return self.enum_cls(value)
        except ValueError:
            return value


def JSONDict() -> Any:  # noqa: N802 - reads like a type
    return MutableDict.as_mutable(JSON())


def JSONList() -> Any:  # noqa: N802
    return MutableList.as_mutable(JSON())


# ---------------------------------------------------------------------------------------------- enums


class Network(StrEnum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    DEMO = "demo"


class AccountStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_RECONNECT = "needs_reconnect"
    DISCONNECTED = "disconnected"


class TargetStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    MANUAL_PENDING = "manual_pending"
    MANUAL_DONE = "manual_done"
    SENT_TO_APP = "sent_to_app"
    ERROR = "error"
    MISSED = "missed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class PostKind(StrEnum):
    SCHEDULED = "scheduled"
    IDEA = "idea"


class IdeaStage(StrEnum):
    IDEA = "idea"
    EN_PROGRESO = "en_progreso"
    LISTA = "lista"


class CommitPhase(StrEnum):
    NONE = "none"
    PREPARED = "prepared"
    COMMIT_SENT = "commit_sent"


class EventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class NotificationKind(StrEnum):
    MANUAL_PUBLISH = "manual_publish"
    ERROR = "error"
    RECONNECT = "reconnect"
    UNKNOWN = "unknown"
    MISSED = "missed"
    INFO = "info"


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class PostSource(StrEnum):
    COMPOSER = "composer"
    CSV = "csv"
    DUPLICATE = "duplicate"


NETWORK_LABELS: dict[str, str] = {
    Network.FACEBOOK: "Facebook",
    Network.INSTAGRAM: "Instagram",
    Network.TIKTOK: "TikTok",
    Network.YOUTUBE: "YouTube",
    Network.DEMO: "Demo",
}

# Single source for status wording: templating's STATUS_LABELS / ACCOUNT_STATUS_LABELS (badges, filters) are
# built from these, so ``target.status_label`` and the ``|status_label`` filter always agree. Keep them short.
TARGET_STATUS_LABELS: dict[str, str] = {
    TargetStatus.DRAFT: "Borrador",
    TargetStatus.SCHEDULED: "Programado",
    TargetStatus.PUBLISHING: "Publicando",
    TargetStatus.PUBLISHED: "Publicado",
    TargetStatus.MANUAL_PENDING: "Publicación asistida",
    TargetStatus.MANUAL_DONE: "Hecho a mano",
    TargetStatus.SENT_TO_APP: "Enviado a la app",
    TargetStatus.ERROR: "Error",
    TargetStatus.MISSED: "Perdido",
    TargetStatus.UNKNOWN: "Sin confirmar",
    TargetStatus.CANCELLED: "Cancelado",
}

ACCOUNT_STATUS_LABELS: dict[str, str] = {
    AccountStatus.CONNECTED: "Conectada",
    AccountStatus.NEEDS_RECONNECT: "Reconexión necesaria",
    AccountStatus.DISCONNECTED: "Desconectada",
}

IDEA_STAGE_LABELS: dict[str, str] = {
    IdeaStage.IDEA: "Idea",
    IdeaStage.EN_PROGRESO: "En progreso",
    IdeaStage.LISTA: "Lista",
}

# Display priority for the derived post status (SPEC §4): first match wins.
_POST_STATUS_PRIORITY: tuple[TargetStatus, ...] = (
    TargetStatus.ERROR,
    TargetStatus.UNKNOWN,
    TargetStatus.MISSED,
    TargetStatus.MANUAL_PENDING,
    TargetStatus.PUBLISHING,
    TargetStatus.SCHEDULED,
    TargetStatus.SENT_TO_APP,
    TargetStatus.DRAFT,
)
_PUBLISHED_LIKE = {TargetStatus.PUBLISHED, TargetStatus.MANUAL_DONE}


def derive_post_status(statuses: list[str] | list[TargetStatus]) -> TargetStatus:
    """Collapse target statuses into one display status for planner cards.

    Priority: error > unknown > missed > manual_pending > publishing > scheduled > sent_to_app > draft >
    published/manual_done. Cancelled targets are ignored unless every target is cancelled. No targets → draft.
    Published + manual_done mix → published; only manual_done → manual_done.
    """
    values = [TargetStatus(s) for s in statuses]
    active = [s for s in values if s != TargetStatus.CANCELLED]
    if not values:
        return TargetStatus.DRAFT
    if not active:
        return TargetStatus.CANCELLED
    present = set(active)
    for status in _POST_STATUS_PRIORITY:
        if status in present:
            return status
    if present == {TargetStatus.MANUAL_DONE}:
        return TargetStatus.MANUAL_DONE
    return TargetStatus.PUBLISHED


# ---------------------------------------------------------------------------------------------- base


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime(),
        date: Date(),
        dict: JSONDict(),
        list: JSONList(),
    }


# --------------------------------------------------------------------------------------------- tables


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<Setting {self.key}>"


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    color: Mapped[str] = mapped_column(String(16), default="#5b4cf0")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Madrid")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(default=None)

    accounts: Mapped[list[Account]] = relationship(
        back_populates="brand", cascade="all, delete-orphan", passive_deletes=True
    )
    posts: Mapped[list[Post]] = relationship(
        back_populates="brand", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def account_for(self, network: str) -> Account | None:
        for acc in self.accounts:
            if acc.network == network:
                return acc
        return None

    def __repr__(self) -> str:
        return f"<Brand {self.id} {self.name!r}>"


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("brand_id", "network", name="uq_accounts_brand_network"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), index=True)
    network: Mapped[Network] = mapped_column(StrEnumType(Network))
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str | None] = mapped_column(String(255), default=None)
    avatar_path: Mapped[str | None] = mapped_column(String(512), default=None)
    account_type: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[AccountStatus] = mapped_column(StrEnumType(AccountStatus), default=AccountStatus.CONNECTED)
    access_token_enc: Mapped[str | None] = mapped_column(Text, default=None)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, default=None)
    token_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    scopes: Mapped[list] = mapped_column(default=list)
    meta: Mapped[dict] = mapped_column(default=dict)
    connected_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    brand: Mapped[Brand] = relationship(back_populates="accounts")
    targets: Mapped[list[PostTarget]] = relationship(back_populates="account", passive_deletes=True)

    # -- token helpers (encryption via crypto module) ------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self.status == AccountStatus.CONNECTED

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}" if self.network in (Network.INSTAGRAM, Network.TIKTOK) else self.name
        return self.name or self.external_id

    @property
    def access_token(self) -> str | None:
        from planificador import crypto

        return crypto.decrypt_optional(self.access_token_enc, on_error="none")

    @property
    def refresh_token(self) -> str | None:
        from planificador import crypto

        return crypto.decrypt_optional(self.refresh_token_enc, on_error="none")

    def set_tokens(
        self,
        access_token: str | None,
        refresh_token: str | None = None,
        *,
        token_expires_at: datetime | None = None,
        refresh_expires_at: datetime | None = None,
        keep_refresh: bool = False,
    ) -> None:
        """Encrypt and store tokens. ``keep_refresh=True`` leaves the existing refresh token untouched
        when ``refresh_token`` is None."""
        from planificador import crypto

        self.access_token_enc = crypto.encrypt_optional(access_token)
        self.token_expires_at = token_expires_at
        if refresh_token is not None or not keep_refresh:
            self.refresh_token_enc = crypto.encrypt_optional(refresh_token)
            self.refresh_expires_at = refresh_expires_at

    def apply_token_update(self, update: TokenUpdate) -> None:
        self.set_tokens(
            update.access_token,
            update.refresh_token,
            token_expires_at=update.token_expires_at,
            refresh_expires_at=update.refresh_expires_at,
            keep_refresh=True,
        )
        if update.scopes is not None:
            self.scopes = list(update.scopes)

    def wipe_tokens(self) -> None:
        self.access_token_enc = None
        self.refresh_token_enc = None
        self.token_expires_at = None
        self.refresh_expires_at = None

    def to_creds(self) -> AccountCreds:
        """Decrypted credentials for providers (never log the result)."""
        from planificador.providers.base import AccountCreds

        return AccountCreds(
            id=self.id,
            network=str(self.network),
            external_id=self.external_id,
            name=self.name,
            account_type=self.account_type,
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            token_expires_at=self.token_expires_at,
            refresh_expires_at=self.refresh_expires_at,
            meta=dict(self.meta or {}),
            username=self.username,
            scopes=list(self.scopes or []),
        )

    def __repr__(self) -> str:
        return f"<Account {self.id} {self.network}:{self.external_id}>"


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int | None] = mapped_column(
        ForeignKey("brands.id", ondelete="SET NULL"), default=None, index=True
    )
    kind: Mapped[MediaKind] = mapped_column(StrEnumType(MediaKind))
    original_name: Mapped[str] = mapped_column(String(255), default="")
    rel_path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    mime: Mapped[str] = mapped_column(String(64), default="application/octet-stream")
    width: Mapped[int | None] = mapped_column(default=None)
    height: Mapped[int | None] = mapped_column(default=None)
    duration_s: Mapped[float | None] = mapped_column(Float, default=None)
    fps: Mapped[float | None] = mapped_column(Float, default=None)
    video_codec: Mapped[str | None] = mapped_column(String(32), default=None)
    audio_codec: Mapped[str | None] = mapped_column(String(32), default=None)
    bitrate: Mapped[int | None] = mapped_column(BigInteger, default=None)
    is_hdr: Mapped[bool] = mapped_column(Boolean, default=False)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    thumb_rel_path: Mapped[str | None] = mapped_column(String(512), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)

    @property
    def is_video(self) -> bool:
        return self.kind == MediaKind.VIDEO

    @property
    def is_image(self) -> bool:
        return self.kind == MediaKind.IMAGE

    def __repr__(self) -> str:
        return f"<MediaAsset {self.id} {self.kind} {self.original_name!r}>"


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        Index("ix_posts_brand_scheduled", "brand_id", "scheduled_at"),
        Index("ix_posts_deleted_at", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"))
    kind: Mapped[PostKind] = mapped_column(StrEnumType(PostKind), default=PostKind.SCHEDULED)
    idea_stage: Mapped[IdeaStage | None] = mapped_column(StrEnumType(IdeaStage), default=None)
    text: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str] = mapped_column(String(16), default=PostSource.COMPOSER.value)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)

    brand: Mapped[Brand] = relationship(back_populates="posts")
    media_links: Mapped[list[PostMedia]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="PostMedia.position",
        passive_deletes=True,
    )
    targets: Mapped[list[PostTarget]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="PostTarget.id",
        passive_deletes=True,
    )

    @property
    def media(self) -> list[MediaAsset]:
        """Base media list, ordered by position."""
        return [link.media for link in self.media_links]

    def set_media(self, media: list[MediaAsset] | list[int]) -> None:
        """Replace the base media list keeping the given order (accepts assets or ids)."""
        self.media_links.clear()
        for pos, item in enumerate(media):
            if isinstance(item, MediaAsset):
                self.media_links.append(PostMedia(media=item, position=pos))
            else:
                self.media_links.append(PostMedia(media_id=int(item), position=pos))

    @property
    def display_status(self) -> TargetStatus:
        return derive_post_status([t.status for t in self.targets])

    @property
    def is_idea(self) -> bool:
        return self.kind == PostKind.IDEA

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def __repr__(self) -> str:
        return f"<Post {self.id} {self.kind} at={self.scheduled_at}>"


class PostMedia(Base):
    __tablename__ = "post_media"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True)
    media_id: Mapped[int] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)

    post: Mapped[Post] = relationship(back_populates="media_links")
    media: Mapped[MediaAsset] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<PostMedia post={self.post_id} media={self.media_id} pos={self.position}>"


class PostTarget(Base):
    __tablename__ = "post_targets"
    __table_args__ = (
        Index("ix_post_targets_status_scheduled", "status", "scheduled_at"),
        Index("ix_post_targets_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    network: Mapped[Network] = mapped_column(StrEnumType(Network))
    format: Mapped[str] = mapped_column(String(32), default="post")
    text_override: Mapped[str | None] = mapped_column(Text, default=None)
    media_override: Mapped[list | None] = mapped_column(default=None)  # list of media ids
    options: Mapped[dict] = mapped_column(default=dict)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[TargetStatus] = mapped_column(StrEnumType(TargetStatus), default=TargetStatus.DRAFT)
    scheduled_at: Mapped[datetime | None] = mapped_column(default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    commit_phase: Mapped[CommitPhase] = mapped_column(StrEnumType(CommitPhase), default=CommitPhase.NONE)
    provider_state: Mapped[dict] = mapped_column(default=dict)
    external_id: Mapped[str | None] = mapped_column(String(255), default=None)
    external_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    last_error_detail: Mapped[dict | None] = mapped_column(default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    post: Mapped[Post] = relationship(back_populates="targets")
    account: Mapped[Account] = relationship(back_populates="targets")
    events: Mapped[list[PostEvent]] = relationship(
        back_populates="target",
        cascade="all, delete-orphan",
        order_by="PostEvent.ts",
        passive_deletes=True,
    )

    @property
    def effective_text(self) -> str:
        """Per-network override if set, else the post's base ("Común") text."""
        if self.text_override is not None:
            return self.text_override
        return self.post.text if self.post is not None else ""

    @property
    def effective_media_ids(self) -> list[int]:
        if self.media_override is not None:
            return [int(m) for m in self.media_override]
        if self.post is None:
            return []
        return [link.media_id for link in self.post.media_links]

    @property
    def status_label(self) -> str:
        return TARGET_STATUS_LABELS.get(self.status, str(self.status))

    def __repr__(self) -> str:
        return f"<PostTarget {self.id} {self.network}/{self.format} {self.status}>"


class PostEvent(Base):
    __tablename__ = "post_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("post_targets.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    level: Mapped[EventLevel] = mapped_column(
        StrEnumType(EventLevel, 16, strict=False), default=EventLevel.INFO
    )
    event: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(default=None)

    target: Mapped[PostTarget] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<PostEvent {self.id} t={self.target_id} {self.event}>"


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_read_created", "read_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[NotificationKind] = mapped_column(StrEnumType(NotificationKind, strict=False))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    target_id: Mapped[int | None] = mapped_column(
        ForeignKey("post_targets.id", ondelete="SET NULL"), default=None, index=True
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), default=None, index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    desktop_sent_at: Mapped[datetime | None] = mapped_column(default=None)

    target: Mapped[PostTarget | None] = relationship()
    account: Mapped[Account | None] = relationship()

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def __repr__(self) -> str:
        return f"<Notification {self.id} {self.kind}>"


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "day", name="uq_analytics_snapshots_account_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    metrics: Mapped[dict] = mapped_column(default=dict)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)

    account: Mapped[Account] = relationship()


class PostMetric(Base):
    __tablename__ = "post_metrics"
    __table_args__ = (UniqueConstraint("account_id", "external_id", name="uq_post_metrics_account_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    target_id: Mapped[int | None] = mapped_column(
        ForeignKey("post_targets.id", ondelete="SET NULL"), default=None, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    permalink: Mapped[str | None] = mapped_column(String(1024), default=None)
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    media_type: Mapped[str | None] = mapped_column(String(32), default=None)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    metrics: Mapped[dict] = mapped_column(default=dict)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)

    account: Mapped[Account] = relationship()
    target: Mapped[PostTarget | None] = relationship()


class InboxComment(Base):
    __tablename__ = "inbox_comments"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_inbox_comments_account_external"),
        Index("ix_inbox_comments_object", "account_id", "object_external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    parent_external_id: Mapped[str | None] = mapped_column(String(255), default=None)
    object_external_id: Mapped[str] = mapped_column(String(255))
    object_permalink: Mapped[str | None] = mapped_column(String(1024), default=None)
    author_name: Mapped[str | None] = mapped_column(String(255), default=None)
    author_id: Mapped[str | None] = mapped_column(String(255), default=None)
    text: Mapped[str] = mapped_column(Text, default="")
    created_time: Mapped[datetime] = mapped_column()
    from_us: Mapped[bool] = mapped_column(Boolean, default=False)
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    replied_at: Mapped[datetime | None] = mapped_column(default=None)
    raw: Mapped[dict] = mapped_column(default=dict)

    account: Mapped[Account] = relationship()


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    network: Mapped[str] = mapped_column(String(32))
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), default=None)
    code_verifier: Mapped[str | None] = mapped_column(String(256), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    OAUTH_STATE_TTL_SECONDS = 600

    def is_expired(self, now: datetime) -> bool:
        return (now - self.created_at).total_seconds() > self.OAUTH_STATE_TTL_SECONDS


class PublicMediaToken(Base):
    __tablename__ = "public_media_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    rel_path: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)

    def is_valid(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# ------------------------------------------------------------------------- aware-UTC on assignment


def _as_aware_utc(value: Any) -> Any:
    if not isinstance(value, datetime):
        return value  # None, or a wrong type that UTCDateTime rejects at flush
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_utc_on_set(_target: Any, value: Any, _oldvalue: Any, _initiator: Any) -> Any:
    return _as_aware_utc(value)


def _install_utc_normalizers() -> None:
    """Every ``UTCDateTime`` attribute converts assigned values to aware UTC (naive = UTC)."""
    for mapper in Base.registry.mappers:
        for prop in mapper.column_attrs:
            if any(isinstance(col.type, UTCDateTime) for col in prop.columns):
                event.listen(getattr(mapper.class_, prop.key), "set", _normalize_utc_on_set, retval=True)


_install_utc_normalizers()


@event.listens_for(Brand, "before_delete")
def _retire_brand_media(_mapper: Any, connection: Any, target: Brand) -> None:
    """Deleting a brand must not leak its private library into every other brand: ``ON DELETE SET NULL``
    would turn its assets into *shared* ones (``brand_id`` NULL = visible in every brand). They leave the
    library instead (soft delete; files stay on disk), except those another brand's posts use, which stay
    shared so those posts keep working."""
    used_elsewhere = (
        select(PostMedia.media_id).join(Post, Post.id == PostMedia.post_id).where(Post.brand_id != target.id)
    )
    connection.execute(
        update(MediaAsset)
        .where(
            MediaAsset.brand_id == target.id,
            MediaAsset.deleted_at.is_(None),
            MediaAsset.id.not_in(used_elsewhere),
        )
        .values(deleted_at=utcnow())
    )


__all__ = [
    "ACCOUNT_STATUS_LABELS",
    "IDEA_STAGE_LABELS",
    "NETWORK_LABELS",
    "TARGET_STATUS_LABELS",
    "Account",
    "AccountStatus",
    "AnalyticsSnapshot",
    "Base",
    "Brand",
    "CommitPhase",
    "EventLevel",
    "IdeaStage",
    "InboxComment",
    "MediaAsset",
    "MediaKind",
    "Network",
    "Notification",
    "NotificationKind",
    "OAuthState",
    "Post",
    "PostEvent",
    "PostKind",
    "PostMedia",
    "PostMetric",
    "PostSource",
    "PostTarget",
    "PublicMediaToken",
    "SchemaVersion",
    "Setting",
    "StrEnumType",
    "TargetStatus",
    "UTCDateTime",
    "derive_post_status",
    "utcnow",
]

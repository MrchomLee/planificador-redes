"""Comments inbox: polling Facebook / Instagram / YouTube comments and replying (SPEC §13, doc 05 §5).

- :func:`poll_all` (scheduler every 15 min, and "Actualizar" on demand): for every connected account whose
  provider supports comments, ``fetch_comments`` for posts of the last 30 days and upsert ``inbox_comments``
  (deduplicated by ``(account_id, external_id)``). Per-account results (last success / last error) live in
  the ``inbox_poll_state`` setting, so polling never writes to ``accounts`` rows.
- :func:`reply` sends a reply through ``Provider.reply_comment`` and stores it as a ``from_us`` comment of
  the same thread.
- Read / resolved state: ``read_at`` per comment; "resolved" is kept in the app-owned ``raw["_app"]`` dict of
  the thread's root comment (``resolved_at``). A thread counts as answered when our latest reply is newer than
  the latest comment from someone else; a new comment re-opens a resolved thread.

Threads are built in memory from the brand's recent comments (a single user's volume is small): a thread is a
top-level comment plus its replies, grouped by the provider's ``parent_external_id`` (Facebook, Instagram and
YouTube all thread one level deep for our purposes).
"""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador.models import (
    Account,
    AccountStatus,
    InboxComment,
    MediaAsset,
    Network,
    PostMetric,
    PostTarget,
)
from planificador.providers.base import (
    CommentData,
    ErrorKind,
    Provider,
    ProviderError,
    ProviderSettings,
    scrub_text,
)
from planificador.timeutil import Clock, ensure_utc

log = logging.getLogger(__name__)

#: Posts of the last N days are scanned for comments (SPEC §13).
POLL_DAYS = 30
#: Re-read this much before the last successful poll (late replies, clock skew).
POLL_OVERLAP = timedelta(hours=24)
#: Comments stamped further in the future than this are skipped until a later poll.
FUTURE_SKEW = timedelta(minutes=10)
#: Threads whose comments are all older than this are not listed.
LIST_WINDOW = timedelta(days=120)
#: Maximum threads rendered in the list.
LIST_LIMIT = 200
#: Minimum search length (doc 05 §5: "Escribe al menos 3 caracteres").
SEARCH_MIN = 3
#: Setting key with per-account poll results: {"<account id>": {"last_ok", "last_try", "error"}}.
STATE_KEY = "inbox_poll_state"
#: App-owned keys inside ``InboxComment.raw`` (providers never send this key).
APP_KEY = "_app"

#: Networks whose comments the inbox can read (TikTok's public API cannot, SPEC §13).
COMMENT_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "youtube", "demo")
#: Maximum reply length per network (IG 2,200; YouTube 10,000 (06 §11.2); FB generous).
REPLY_MAX: dict[str, int] = {"facebook": 8000, "instagram": 2200, "youtube": 10000, "demo": 2200}
NETWORK_NAMES: dict[str, str] = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "demo": "Demo",
}
TIKTOK_NOTE = (
    "TikTok no permite leer ni responder comentarios desde aplicaciones externas con su API pública. "
    "Revisa los comentarios de TikTok en la propia app."
)

#: List tabs (doc 05 §5): key → label.
TABS: tuple[tuple[str, str], ...] = (
    ("sin_responder", "Sin responder"),
    ("no_leidos", "No leídos"),
    ("todos", "Todos"),
)
DEFAULT_TAB = "sin_responder"

_poll_lock = threading.Lock()


class InboxError(Exception):
    """User-facing (Spanish) inbox error. ``status_code`` suggests the HTTP status."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "inbox") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# ============================================================================================ helpers


def new_http_client() -> httpx.Client:
    """HTTP client for on-demand polls/replies from routes (tests may monkeypatch this)."""
    try:
        from planificador import __version__
    except ImportError:  # pragma: no cover
        __version__ = "dev"
    return httpx.Client(
        timeout=httpx.Timeout(60.0, connect=15.0), headers={"User-Agent": f"Planificador/{__version__}"}
    )


def _provider(network: str) -> Provider | None:
    from planificador.providers import registry

    try:
        return registry.get_provider(str(network))
    except Exception:  # noqa: BLE001 - provider module missing/broken: treat as unsupported
        log.debug("Provider %s unavailable", network, exc_info=True)
        return None


def supports_comments(network: str) -> bool:
    if str(network) not in COMMENT_NETWORKS:
        return False
    provider = _provider(str(network))
    return bool(provider is not None and getattr(provider, "supports_comments", False))


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def app_state(row: InboxComment) -> dict[str, Any]:
    raw = row.raw or {}
    value = raw.get(APP_KEY) if isinstance(raw, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def _set_app_state(row: InboxComment, **changes: Any) -> None:
    raw = dict(row.raw or {})
    state = dict(raw.get(APP_KEY) or {})
    for key, value in changes.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    if state:
        raw[APP_KEY] = state
    else:
        raw.pop(APP_KEY, None)
    row.raw = raw


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def fold(text: str | None) -> str:
    """Case- and accent-insensitive form for searching."""
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).casefold()


# ======================================================================================= poll state


def poll_state(db: Session) -> dict[str, dict[str, Any]]:
    from planificador import config

    value = config.get_setting(db, STATE_KEY, None)
    return dict(value) if isinstance(value, dict) else {}


def _save_poll_state(db: Session, account_id: int, **changes: Any) -> None:
    from planificador import config

    state = poll_state(db)
    entry = dict(state.get(str(account_id)) or {})
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    state[str(account_id)] = entry
    config.set_setting(db, STATE_KEY, state, commit=True)


def _error_info(exc: BaseException, now: datetime) -> dict[str, Any]:
    if isinstance(exc, ProviderError):
        return {"message": exc.message, "code": exc.code, "kind": str(exc.kind), "at": now.isoformat()}
    return {
        "message": "No se pudieron leer los comentarios de esta cuenta. Se reintentará más tarde.",
        "code": type(exc).__name__,
        "kind": "internal",
        "detail": scrub_text(str(exc))[:300],
        "at": now.isoformat(),
    }


def account_problem(account: Account, state: dict[str, dict[str, Any]] | None = None) -> str | None:
    """Spanish reason when the account needs reconnecting (red "!" badge), else ``None``."""
    if account.status == AccountStatus.NEEDS_RECONNECT:
        return account.last_error or "La cuenta necesita reconexión. Ve a «Cuentas conectadas»."
    if account.status == AccountStatus.DISCONNECTED:
        return "La cuenta está desconectada. Vuelve a conectarla en «Cuentas conectadas»."
    entry = (state or {}).get(str(account.id)) or {}
    error = entry.get("error") or {}
    if error.get("kind") == str(ErrorKind.AUTH):
        return str(error.get("message") or "La cuenta necesita reconexión.")
    return None


def last_error(account: Account, state: dict[str, dict[str, Any]] | None) -> str | None:
    """Message of the last failed poll of ``account`` (cleared by the next success)."""
    entry = (state or {}).get(str(account.id)) or {}
    error = entry.get("error") or {}
    return str(error["message"]) if error.get("message") else None


def last_poll_at(state: dict[str, dict[str, Any]], account_ids: Iterable[int]) -> datetime | None:
    stamps = [_parse_iso((state.get(str(i)) or {}).get("last_ok")) for i in account_ids]
    values = [s for s in stamps if s is not None]
    return max(values) if values else None


# ============================================================================================ polling


def comment_accounts(db: Session, *, brand_id: int | None = None, demo_mode: bool = False) -> list[Account]:
    """Connected accounts whose network supports comments (Demo only in demo mode)."""
    stmt = select(Account).where(Account.status == AccountStatus.CONNECTED).order_by(Account.id)
    if brand_id is not None:
        stmt = stmt.where(Account.brand_id == brand_id)
    out: list[Account] = []
    for account in db.scalars(stmt):
        network = str(account.network)
        if network == Network.DEMO and not demo_mode:
            continue
        if supports_comments(network):
            out.append(account)
    return out


def upsert_comment(
    db: Session, account: Account, data: CommentData, now: datetime
) -> tuple[InboxComment, bool]:
    """Insert or refresh one comment. Returns ``(row, created)``. App state in ``raw["_app"]`` is kept."""
    row = db.scalar(
        select(InboxComment).where(
            InboxComment.account_id == account.id, InboxComment.external_id == str(data.external_id)
        )
    )
    raw = _json_safe(data.raw or {})
    if not isinstance(raw, dict):
        raw = {"value": raw}
    raw.pop(APP_KEY, None)
    created = ensure_utc(data.created_time)
    if row is None:
        row = InboxComment(
            account_id=account.id,
            external_id=str(data.external_id),
            parent_external_id=str(data.parent_external_id) if data.parent_external_id else None,
            object_external_id=str(data.object_external_id),
            object_permalink=data.object_permalink,
            author_name=data.author_name,
            author_id=data.author_id,
            text=data.text or "",
            created_time=created,
            from_us=bool(data.from_us),
            read_at=now if data.from_us else None,
            raw=raw,
        )
        db.add(row)
        return row, True
    state = app_state(row)
    sent_from_app = bool(state.get("sent_from_app"))
    if data.parent_external_id and not sent_from_app:
        row.parent_external_id = str(data.parent_external_id)
    row.object_external_id = str(data.object_external_id or row.object_external_id)
    row.object_permalink = data.object_permalink or row.object_permalink
    if data.author_name and not sent_from_app:
        row.author_name = data.author_name
    if data.author_id:
        row.author_id = data.author_id
    row.text = data.text or row.text
    if not sent_from_app:
        row.created_time = created
    row.from_us = bool(row.from_us or data.from_us)
    if row.from_us and row.read_at is None:
        row.read_at = now
    if state:
        raw[APP_KEY] = state
    row.raw = raw
    return row, False


def _mark_parent_replied(db: Session, account: Account, row: InboxComment) -> None:
    """Our own reply found on the network (e.g. made in the network's app): stamp the parent's
    ``replied_at``."""
    if not row.from_us or not row.parent_external_id:
        return
    parent = db.scalar(
        select(InboxComment).where(
            InboxComment.account_id == account.id, InboxComment.external_id == row.parent_external_id
        )
    )
    if parent is not None and (parent.replied_at is None or parent.replied_at < row.created_time):
        parent.replied_at = row.created_time


def poll_account(
    db: Session, account: Account, http: httpx.Client, clock: Clock, settings: ProviderSettings
) -> int:
    """Fetch and store the comments of one account (no error handling, no commit). Returns how many new
    comments from others were stored."""
    provider = _provider(str(account.network))
    if provider is None or not getattr(provider, "supports_comments", False):
        return 0
    now = ensure_utc(clock.now())
    since = now - timedelta(days=POLL_DAYS)
    last_ok = _parse_iso((poll_state(db).get(str(account.id)) or {}).get("last_ok"))
    if last_ok is not None and last_ok - POLL_OVERLAP > since:
        since = last_ok - POLL_OVERLAP
    comments = provider.fetch_comments(account.to_creds(), settings, http, since) or []
    new = 0
    rows: list[InboxComment] = []
    for data in comments:
        if not getattr(data, "external_id", None) or data.created_time is None:
            continue
        if ensure_utc(data.created_time) > now + FUTURE_SKEW:
            continue
        row, created = upsert_comment(db, account, data, now)
        rows.append(row)
        if created and not row.from_us:
            new += 1
    db.flush()
    for row in rows:
        _mark_parent_replied(db, account, row)
    return new


def poll_all(
    db: Session,
    http: httpx.Client,
    clock: Clock,
    provider_settings: ProviderSettings | None = None,
    *,
    account_ids: Iterable[int] | None = None,
    brand_id: int | None = None,
) -> int:
    """Poll every connected account that supports comments; returns the number of NEW comments from others.

    Never raises because of one account: its error is stored in the ``inbox_poll_state`` setting (shown in the
    UI; AUTH errors show the red "!" badge) and cleared on the next success. Commits per account. Concurrent
    calls (scheduler + "Actualizar") are serialised.
    """
    if provider_settings is None:
        from planificador import config

        provider_settings = config.provider_settings(db)
    wanted = set(account_ids) if account_ids is not None else None
    total = 0
    with _poll_lock:
        accounts = comment_accounts(db, brand_id=brand_id, demo_mode=bool(provider_settings.demo_mode))
        for account in accounts:
            if wanted is not None and account.id not in wanted:
                continue
            account_id = account.id
            now = ensure_utc(clock.now())
            try:
                new = poll_account(db, account, http, clock, provider_settings)
                db.commit()
                _save_poll_state(
                    db, account_id, last_ok=now.isoformat(), last_try=now.isoformat(), error=None
                )
                total += new
            except Exception as exc:  # noqa: BLE001 - recorded per account, never propagated
                if isinstance(exc, ProviderError):
                    log.warning("Comment polling failed for account %s: %s", account_id, exc.code)
                else:
                    log.exception("Comment polling failed for account %s", account_id)
                db.rollback()
                try:
                    _save_poll_state(db, account_id, last_try=now.isoformat(), error=_error_info(exc, now))
                except Exception:  # noqa: BLE001
                    log.exception("Could not record the inbox error of account %s", account_id)
                    db.rollback()
    if total:
        log.info("Inbox: %d new comment(s)", total)
    return total


# ============================================================================================ threads


@dataclass
class PostContext:
    """What the comments are about: our post (planner) or a post read from the network."""

    caption: str | None = None
    thumb_url: str | None = None
    permalink: str | None = None
    post_id: int | None = None

    @property
    def has_any(self) -> bool:
        return bool(self.caption or self.thumb_url or self.permalink)


@dataclass
class Thread:
    root: InboxComment
    account: Account
    comments: list[InboxComment] = field(default_factory=list)  # root first, then chronological
    context: PostContext = field(default_factory=PostContext)

    @property
    def id(self) -> int:
        return self.root.id

    @property
    def network(self) -> str:
        return str(self.account.network)

    @property
    def network_name(self) -> str:
        return NETWORK_NAMES.get(self.network, self.network)

    @property
    def others(self) -> list[InboxComment]:
        return [c for c in self.comments if not c.from_us]

    @property
    def ours(self) -> list[InboxComment]:
        return [c for c in self.comments if c.from_us]

    @property
    def replies(self) -> list[InboxComment]:
        return [c for c in self.comments if c.id != self.root.id]

    @property
    def last_activity(self) -> datetime:
        return max(c.created_time for c in self.comments)

    @property
    def last_other(self) -> InboxComment | None:
        others = self.others
        return max(others, key=lambda c: (c.created_time, c.id)) if others else None

    @property
    def unread_count(self) -> int:
        return sum(1 for c in self.others if c.read_at is None)

    @property
    def is_unread(self) -> bool:
        return self.unread_count > 0

    @property
    def answered_at(self) -> datetime | None:
        stamps = [c.created_time for c in self.ours]
        stamps += [c.replied_at for c in self.comments if c.replied_at is not None]
        return max(stamps) if stamps else None

    @property
    def is_answered(self) -> bool:
        last = self.last_other
        answered = self.answered_at
        return last is not None and answered is not None and answered >= last.created_time

    @property
    def resolved_at(self) -> datetime | None:
        return _parse_iso(app_state(self.root).get("resolved_at"))

    @property
    def is_resolved(self) -> bool:
        resolved = self.resolved_at
        last = self.last_other
        return resolved is not None and (last is None or resolved >= last.created_time)

    @property
    def needs_reply(self) -> bool:
        return not self.is_answered and not self.is_resolved

    @property
    def author(self) -> str:
        first = self.root if not self.root.from_us else (self.last_other or self.root)
        return first.author_name or "Usuario"

    @property
    def excerpt(self) -> str:
        latest = self.last_other or self.root
        return " ".join((latest.text or "").split())

    @property
    def reply_block_reason(self) -> str | None:
        if self.account.status != AccountStatus.CONNECTED:
            return "Reconecta la cuenta en «Cuentas conectadas» para poder responder."
        raw = self.root.raw or {}
        if raw.get("can_reply") is False or raw.get("canReply") is False:
            return f"{self.network_name} no permite responder a este comentario."
        return None

    @property
    def can_reply(self) -> bool:
        return self.reply_block_reason is None

    @property
    def reply_max(self) -> int:
        return REPLY_MAX.get(self.network, 2200)

    @property
    def comment_link(self) -> str | None:
        """Link to the comment itself on the network, when known."""
        raw = self.root.raw or {}
        link = raw.get("permalink_url")
        if isinstance(link, str) and link.startswith("https://"):
            return link
        if self.network == Network.YOUTUBE and self.root.object_permalink:
            return self.root.object_permalink
        return None


def _root_external_id(row: InboxComment, by_ext: dict[str, InboxComment], depth: int = 0) -> str:
    state = app_state(row)
    root = state.get("thread_root")
    if root and root in by_ext and root != row.external_id and depth < 5:
        return _root_external_id(by_ext[root], by_ext, depth + 1)
    parent = row.parent_external_id
    if parent and parent != row.object_external_id and parent in by_ext and depth < 5:
        return _root_external_id(by_ext[parent], by_ext, depth + 1)
    return row.external_id


def build_threads(rows: Sequence[InboxComment], accounts: dict[int, Account]) -> list[Thread]:
    """Group comments into threads (root = top-level comment). Threads without any comment from others are
    skipped. Newest activity first."""
    by_account: dict[int, dict[str, InboxComment]] = {}
    for row in rows:
        by_account.setdefault(row.account_id, {})[row.external_id] = row
    groups: dict[tuple[int, str], list[InboxComment]] = {}
    for account_id, by_ext in by_account.items():
        for row in by_ext.values():
            groups.setdefault((account_id, _root_external_id(row, by_ext)), []).append(row)
    threads: list[Thread] = []
    for (account_id, root_ext), members in groups.items():
        account = accounts.get(account_id)
        if account is None:
            continue
        root = by_account[account_id].get(root_ext) or min(members, key=lambda c: (c.created_time, c.id))
        members.sort(key=lambda c: (c.id != root.id, c.created_time, c.id))
        thread = Thread(root=root, account=account, comments=members)
        if thread.others:
            threads.append(thread)
    threads.sort(key=lambda t: (t.last_activity, t.root.id), reverse=True)
    return threads


def _thumb_of_target(db: Session, target: PostTarget) -> str | None:
    for media_id in target.effective_media_ids:
        asset = db.get(MediaAsset, media_id)
        if asset is not None and asset.deleted_at is None:
            return f"/medios/{asset.id}/miniatura"
    return None


def attach_contexts(db: Session, threads: Sequence[Thread]) -> None:
    """Fill ``thread.context`` (caption, thumbnail, permalink) from our posts and stored post metrics."""
    if not threads:
        return
    account_ids = {t.account.id for t in threads}
    object_ids = {t.root.object_external_id for t in threads}
    targets: dict[tuple[int, str], PostTarget] = {}
    for target in db.scalars(
        select(PostTarget).where(
            PostTarget.account_id.in_(account_ids), PostTarget.external_id.in_(object_ids)
        )
    ):
        targets[(target.account_id, str(target.external_id))] = target
    metrics: dict[tuple[int, str], PostMetric] = {}
    for metric in db.scalars(
        select(PostMetric).where(
            PostMetric.account_id.in_(account_ids), PostMetric.external_id.in_(object_ids)
        )
    ):
        metrics[(metric.account_id, metric.external_id)] = metric
    for thread in threads:
        key = (thread.account.id, thread.root.object_external_id)
        ctx = PostContext()
        target = targets.get(key)
        metric = metrics.get(key)
        if target is not None:
            ctx.post_id = target.post_id
            ctx.caption = (target.effective_text or "").strip() or None
            ctx.thumb_url = _thumb_of_target(db, target)
            ctx.permalink = target.external_url
        if metric is not None:
            ctx.caption = ctx.caption or (metric.caption or "").strip() or None
            ctx.thumb_url = ctx.thumb_url or metric.thumbnail_url
            ctx.permalink = ctx.permalink or metric.permalink
        if thread.network == Network.YOUTUBE and thread.root.object_external_id != thread.account.external_id:
            vid = thread.root.object_external_id
            ctx.thumb_url = ctx.thumb_url or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
            ctx.permalink = ctx.permalink or f"https://www.youtube.com/watch?v={vid}"
        if not ctx.permalink and thread.root.object_permalink:
            ctx.permalink = thread.root.object_permalink
        if ctx.permalink and not ctx.permalink.startswith(("https://", "http://")):
            ctx.permalink = None
        thread.context = ctx


def brand_accounts(db: Session, brand_id: int) -> list[Account]:
    return list(db.scalars(select(Account).where(Account.brand_id == brand_id).order_by(Account.id)))


def load_threads(
    db: Session,
    brand_id: int,
    clock: Clock,
    *,
    network: str | None = None,
    demo_mode: bool = False,
) -> list[Thread]:
    """All threads of the brand's comment-capable accounts (optionally one network), newest first."""
    accounts = {
        a.id: a
        for a in brand_accounts(db, brand_id)
        if str(a.network) in COMMENT_NETWORKS
        and (demo_mode or str(a.network) != Network.DEMO)
        and (not network or str(a.network) == network)
    }
    if not accounts:
        return []
    cutoff = ensure_utc(clock.now()) - LIST_WINDOW
    rows = list(
        db.scalars(
            select(InboxComment).where(
                InboxComment.account_id.in_(list(accounts)), InboxComment.created_time >= cutoff
            )
        )
    )
    return build_threads(rows, accounts)


def _matches(thread: Thread, needle: str) -> bool:
    haystack = [thread.context.caption or ""]
    for c in thread.comments:
        haystack.append(c.author_name or "")
        haystack.append(c.text or "")
    return any(needle in fold(h) for h in haystack)


def normalize_tab(tab: str | None) -> str:
    keys = {k for k, _ in TABS}
    value = str(tab or "").strip().lower()
    return value if value in keys else DEFAULT_TAB


def filter_threads(threads: Sequence[Thread], tab: str, query: str | None = None) -> list[Thread]:
    """Threads of ``tab`` (sin_responder | no_leidos | todos) matching ``query`` (ignored below 3 chars)."""
    tab = normalize_tab(tab)
    needle = fold((query or "").strip())
    out: list[Thread] = []
    for thread in threads:
        if tab == "sin_responder" and not thread.needs_reply:
            continue
        if tab == "no_leidos" and not thread.is_unread:
            continue
        if len(needle) >= SEARCH_MIN and not _matches(thread, needle):
            continue
        out.append(thread)
    return out


def tab_counts(threads: Sequence[Thread]) -> dict[str, int]:
    return {
        "sin_responder": sum(1 for t in threads if t.needs_reply),
        "no_leidos": sum(1 for t in threads if t.is_unread),
        "todos": len(threads),
    }


def get_thread(db: Session, comment_id: int, brand_id: int) -> Thread:
    """The thread containing comment ``comment_id`` (any comment of it), with its post context.
    ``LookupError`` when it does not exist or belongs to another brand."""
    row = db.get(InboxComment, comment_id)
    if row is None:
        raise LookupError(comment_id)
    account = db.get(Account, row.account_id)
    if account is None or account.brand_id != brand_id:
        raise LookupError(comment_id)
    rows = list(db.scalars(select(InboxComment).where(InboxComment.account_id == account.id)))
    found: Thread | None = None
    for thread in build_threads(rows, {account.id: account}):
        if any(c.id == row.id for c in thread.comments):
            found = thread
            break
    if found is None:  # a thread made only of our own comments: show it anyway
        found = Thread(root=row, account=account, comments=[row])
    attach_contexts(db, [found])
    return found


# ========================================================================================== actions


def mark_read(db: Session, thread: Thread, now: datetime, *, read: bool = True, commit: bool = True) -> int:
    """Mark the thread's comments from others as read; ``read=False`` marks the latest one as unread."""
    changed = 0
    if read:
        for c in thread.others:
            if c.read_at is None:
                c.read_at = now
                changed += 1
    else:
        latest = thread.last_other
        if latest is not None and latest.read_at is not None:
            latest.read_at = None
            changed = 1
    if commit:
        db.commit()
    return changed


def set_resolved(db: Session, thread: Thread, resolved: bool, now: datetime, *, commit: bool = True) -> None:
    """ "Marcar como resuelto" (also marks the thread read) or reopen it."""
    _set_app_state(thread.root, resolved_at=now.isoformat() if resolved else None)
    if resolved:
        mark_read(db, thread, now, commit=False)
    if commit:
        db.commit()


def reply(
    db: Session,
    http: httpx.Client,
    clock: Clock,
    provider_settings: ProviderSettings,
    thread: Thread,
    text: str,
    *,
    reply_to: InboxComment | None = None,
) -> InboxComment:
    """Reply to ``reply_to`` (default: the thread's root) through the provider and store the reply as a
    ``from_us`` comment of the thread. Raises :class:`InboxError` (Spanish message) when it cannot be sent."""
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        raise InboxError("Escribe una respuesta antes de enviarla.", status_code=422, code="empty")
    if len(body) > thread.reply_max:
        raise InboxError(
            f"La respuesta es demasiado larga (máximo {thread.reply_max} caracteres).",
            status_code=422,
            code="too_long",
        )
    reason = thread.reply_block_reason
    if reason:
        raise InboxError(reason, status_code=409, code="cannot_reply")
    target = reply_to if reply_to is not None else thread.root
    if target.from_us and target.id != thread.root.id:
        target = thread.root
    account = thread.account
    provider = _provider(str(account.network))
    if provider is None or not getattr(provider, "supports_comments", False):
        raise InboxError(
            f"{thread.network_name} no permite responder comentarios desde la app.",
            status_code=409,
            code="unsupported",
        )
    now = ensure_utc(clock.now())
    try:
        reply_id = provider.reply_comment(
            account.to_creds(), provider_settings, http, target.external_id, body
        )
    except ProviderError as exc:
        if exc.kind == ErrorKind.AUTH:
            try:
                _save_poll_state(db, account.id, error=_error_info(exc, now))
            except Exception:  # noqa: BLE001
                db.rollback()
        status = (
            502 if exc.kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED, ErrorKind.AMBIGUOUS) else 409
        )
        raise InboxError(exc.message, status_code=status, code=exc.code) from exc
    except httpx.HTTPError as exc:
        raise InboxError(
            "No se ha podido conectar con la red social. Comprueba tu conexión e inténtalo de nuevo.",
            status_code=502,
            code="network",
        ) from exc

    reply_id = str(reply_id)
    row = db.scalar(
        select(InboxComment).where(
            InboxComment.account_id == account.id, InboxComment.external_id == reply_id
        )
    )
    if row is None:
        row = InboxComment(account_id=account.id, external_id=reply_id)
        db.add(row)
    row.parent_external_id = thread.root.external_id
    row.object_external_id = thread.root.object_external_id
    row.object_permalink = thread.root.object_permalink
    row.author_name = account.display_name
    row.author_id = account.external_id
    row.text = body
    row.created_time = now
    row.from_us = True
    row.read_at = now
    row.raw = {
        APP_KEY: {
            "sent_from_app": True,
            "thread_root": thread.root.external_id,
            "in_reply_to": target.external_id,
        }
    }
    target.replied_at = now
    thread.root.replied_at = now
    mark_read(db, thread, now, commit=False)
    db.commit()
    if row not in thread.comments:
        thread.comments.append(row)
    return row


def summary(db: Session, brand_id: int, clock: Clock, *, demo_mode: bool = False) -> dict[str, int]:
    """Counts for badges: ``{"sin_responder", "no_leidos", "todos"}``."""
    return tab_counts(load_threads(db, brand_id, clock, demo_mode=demo_mode))


__all__ = [
    "COMMENT_NETWORKS",
    "DEFAULT_TAB",
    "TABS",
    "TIKTOK_NOTE",
    "InboxError",
    "PostContext",
    "Thread",
    "account_problem",
    "attach_contexts",
    "build_threads",
    "comment_accounts",
    "filter_threads",
    "get_thread",
    "load_threads",
    "mark_read",
    "normalize_tab",
    "poll_account",
    "poll_all",
    "poll_state",
    "reply",
    "set_resolved",
    "summary",
    "supports_comments",
    "tab_counts",
    "upsert_comment",
]

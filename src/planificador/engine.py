"""Publishing engine: runs one post target through its provider's state machine (SPEC §10).

This module owns **every status transition** of a ``PostTarget`` once it is scheduled; ``scheduler.py`` only
selects targets, enforces timing/concurrency and calls the functions here.

Lifecycle of an automatic target::

    scheduled ──claim_due──▶ publishing ──run_target──▶ published | sent_to_app | error | unknown
         │                     │   ▲
         │                     │   └── wait / retry: stays ``publishing`` with ``next_attempt_at`` set
         ├─▶ manual_pending  (auto_publish off: Publicación asistida)
         └─▶ missed          (due while the app was not running, beyond the grace window)

Idempotency rules (research 04 §2.3):

- A claim is an atomic ``UPDATE … WHERE status=…`` checked by ``rowcount``; no transaction is ever held
  across network I/O. ``save_state`` / ``before_commit`` persist in their **own** short transactions.
- ``before_commit`` stores ``commit_phase = commit_sent`` *before* the provider's irreversible request. From
  then on the engine never calls ``publish_step`` again after an ambiguous failure or a crash: it calls
  ``provider.reconcile`` and only a definitive ``not_published`` re-opens the target for a new commit. An
  undeterminable outcome ends in ``unknown`` (never retried automatically).
- A provider that returns ``wait`` after its commit point (polling IG/TikTok/FB processing) is simply
  re-entered later; that is the provider's own re-entrant protocol. Such a resume skips the pre-flight gates
  (post trashed, account not connected, missing media) and a failed token refresh is only a warning: those
  decide whether to *send*, they are not an outcome of what was already sent.
- Token refreshes are serialised per account (:func:`credentials_lock`, shared with the scheduler's token
  job) and always start from the tokens stored at that moment (TikTok rotates refresh tokens).
- A Facebook/YouTube ``native_schedule`` hand-off claimed ahead of time that fails before its commit goes
  back to ``scheduled`` (never ``error`` while the post is not due): the hand-off is retried later and, if it
  never succeeds, the app publishes it itself at its time.

Engine bookkeeping that must survive restarts lives under the reserved key ``"_engine"`` inside
``provider_state`` (stripped before the state is handed to providers), e.g. ``{"pending": "reconcile"}``
when a reconcile has to be retried later.

Retry policy for ``TRANSIENT``/``RATE_LIMITED`` errors before the commit point: up to :data:`MAX_RETRIES`
retries after 1, 5, 15 and 30 minutes (±20 % jitter); ``retry_after`` from the provider wins. The next failure
is final (``error``). Every transition writes a ``post_events`` row; errors, reconnects, unknown outcomes,
missed posts, manual reminders and TikTok drafts also create a notification.

Offline-aware retries: a **network-level** failure before the commit point (connection refused, DNS
failure, no route, connect/read timeout — :func:`is_network_failure`) is confirmed with a cheap connectivity
probe (:mod:`planificador.connectivity`). No internet → the target keeps ``publishing`` with
``next_attempt_at`` about a minute later and engine flag ``offline`` (:func:`_Runner._offline_wait`): no retry
is spent, no error is recorded, one ``offline`` event per outage and one "Sin conexión a internet"
notification per outage (after :data:`OFFLINE_NOTIFY_AFTER`). The scheduler probes again before resuming
it and publishes it as soon as the connection is back. A reachable host means a genuine API problem: the
usual backoff applies. After the commit point nothing changes (reconcile, never a blind re-send) — but when
the reconcile confirms that nothing went out (``not_published``: the irreversible request was the run's first
network call, e.g. a Facebook text post or a TikTok inbox init, or an upload broke off) and the failure was a
network-level one with no internet, the target waits for the connection the same way.

The outage record (:data:`OUTAGE_KEY`) ends when a probe succeeds, when nothing waits for the connection any
more (:func:`settle_outage`, every scheduler tick) or, on the next failed probe, when the previous failed
probe is older than :data:`OUTAGE_STALE_AFTER` (a new outage: notified again).

Late publishing (:class:`LatePolicy`, settings ``publish_late`` / ``publish_late_max_hours`` /
``catchup_spacing_minutes``): the scheduler decides *whether* a late target still goes out; the engine
records when it was due (engine bookkeeping ``due_at``) and why it went out late (``late``: ``unavailable`` =
the PC or the app was not available, ``offline`` = no internet), and on success adds a ``published_late``
event and an informative notification ("Publicada con retraso…").

Public API for routes (contract): :func:`publish_now`, :func:`retry_target`, :func:`cancel_target`,
:func:`mark_done_manually` (raise :class:`EngineError` with a Spanish message on invalid operations).
"""

from __future__ import annotations

import logging
import random
import re
import socket
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from planificador import config, keepawake
from planificador import connectivity as connectivity_mod
from planificador.models import (
    NETWORK_LABELS,
    Account,
    AccountStatus,
    CommitPhase,
    EventLevel,
    MediaAsset,
    NotificationKind,
    Post,
    PostEvent,
    PostKind,
    PostTarget,
    TargetStatus,
)
from planificador.providers.base import (
    AccountCreds,
    ErrorKind,
    Issue,
    MediaInfo,
    Provider,
    ProviderError,
    ProviderSettings,
    PublishContext,
    ReconcileResult,
    StepResult,
    TargetSpec,
    TokenUpdate,
    scrub,
    scrub_text,
)
from planificador.timeutil import (
    Clock,
    SystemClock,
    ensure_utc,
    format_dt_es,
    format_duration_es,
    format_time_es,
)

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------------------------- policy

RETRY_DELAYS_S: tuple[int, ...] = (60, 300, 900, 1800)  # 1, 5, 15, 30 min
MAX_RETRIES = len(RETRY_DELAYS_S)  # retries after the first attempt; the next failure is final
RETRY_JITTER = 0.2
MAX_RETRY_AFTER_S = 48 * 3600
MIN_WAIT_S = 1
MAX_WAIT_S = 48 * 3600
STALE_CLAIM_AFTER = timedelta(minutes=10)
#: After a failed early hand-off to Facebook/YouTube's own scheduler (transient or account problem), when to
#: try handing it off again (never later than the scheduled time, when the app publishes it itself).
NATIVE_HANDOFF_RETRY = timedelta(hours=6)
LATE_NOTE_AFTER = timedelta(minutes=2)
MAX_STEPS_PER_RUN = 200
HEARTBEAT_EVERY = timedelta(seconds=60)
HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=30.0)
ENGINE_STATE_KEY = "_engine"
#: While there is no internet, how often a waiting target re-checks the connection.
OFFLINE_RECHECK = timedelta(seconds=60)
#: The "Sin conexión a internet" notification is shown once per outage, when it lasts at least this long.
OFFLINE_NOTIFY_AFTER = timedelta(minutes=5)
#: ``settings`` key with the current internet outage
#: ``{"since": iso, "last_failed_at": iso, "notified": bool}`` (absent while online or nothing waits for it).
OUTAGE_KEY = "connectivity_outage"
OUTAGE_TITLE = "Sin conexión a internet"
#: A failed probe this long after the previous one starts a new outage (while posts wait for the connection
#: the scheduler probes about every :data:`OFFLINE_RECHECK`; a longer silence means nobody was waiting, or the
#: PC was asleep).
OUTAGE_STALE_AFTER = 3 * OFFLINE_RECHECK
#: How long after the last failed probe the scheduler may drop the outage record when no post waits for the
#: connection (a short margin so a run that is about to start waiting is not raced).
OUTAGE_SETTLE_AFTER = timedelta(seconds=30)
#: Why a target went out late (engine bookkeeping ``late``).
LATE_UNAVAILABLE = "unavailable"  # the PC was off/asleep or the app was closed
LATE_OFFLINE = "offline"  # no internet connection
#: Why a target became ``missed`` (:func:`set_missed`).
MISSED_GRACE = "grace"  # publish_late off: the app was off beyond the grace window
MISSED_MAX_LATE = "max_late"  # publish_late on: beyond the "Límite de retraso"
MISSED_OFFLINE = "offline"  # publish_late off: no internet beyond the grace window
#: Provider-state key with ``accounts.meta`` changes the engine merges into the account (YouTube lock status).
ACCOUNT_META_UPDATES_KEY = "account_meta_updates"

PUBLISH_NOW_FROM = frozenset(
    {TargetStatus.DRAFT, TargetStatus.SCHEDULED, TargetStatus.ERROR, TargetStatus.MISSED}
)
RETRY_FROM = frozenset({TargetStatus.ERROR, TargetStatus.MISSED, TargetStatus.UNKNOWN})
CANCEL_FROM = frozenset(
    {
        TargetStatus.DRAFT,
        TargetStatus.SCHEDULED,
        TargetStatus.MANUAL_PENDING,
        TargetStatus.MISSED,
        TargetStatus.ERROR,
        TargetStatus.UNKNOWN,
    }
)
MARK_DONE_FROM = frozenset(
    {
        TargetStatus.MANUAL_PENDING,
        TargetStatus.UNKNOWN,
        TargetStatus.ERROR,
        TargetStatus.MISSED,
        TargetStatus.SENT_TO_APP,
    }
)
#: Notifications that become moot once the user acts on the target.
_RESOLVABLE_KINDS = (
    NotificationKind.MANUAL_PUBLISH,
    NotificationKind.ERROR,
    NotificationKind.UNKNOWN,
    NotificationKind.MISSED,
)

#: Run outcomes returned by :func:`run_target`.
OUTCOMES = (
    "published",
    "sent_to_app",
    "wait",
    "retry",
    "reconcile_retry",
    "error",
    "unknown",
    "paused",
    "cancelled",
    "skipped",
    "handoff_failed",
    "offline",
)


class EngineError(Exception):
    """An invalid manual operation. ``message`` is Spanish and safe to show to the user."""

    def __init__(self, message: str, *, code: str = "invalid_state") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ------------------------------------------------------------------------------------------- runtime


@dataclass
class EngineRuntime:
    """Everything a run needs besides the target id (built by the scheduler; defaults for ad-hoc use)."""

    session_factory: Callable[[], Session]
    clock: Clock = field(default_factory=SystemClock)
    http: httpx.Client | None = None
    public_media: Any = None  # PublicMedia | () -> PublicMedia | None | None
    base_url: str | None = None
    write_lock: Any = None  # threading lock serialising engine writes (db.Database.write_lock)
    stop_event: threading.Event | None = None
    rng: random.Random = field(default_factory=random.Random)
    connectivity: connectivity_mod.Connectivity | None = None  # internet probe (default: real HEAD probes)

    def __post_init__(self) -> None:
        if self.connectivity is None:
            self.connectivity = connectivity_mod.Connectivity(self.clock)

    def get_connectivity(self) -> connectivity_mod.Connectivity:
        if self.connectivity is None:  # pragma: no cover - set in __post_init__
            self.connectivity = connectivity_mod.Connectivity(self.clock)
        return self.connectivity

    def get_public_media(self) -> Any:
        pm = self.public_media
        if pm is None or hasattr(pm, "url_for"):
            return pm
        if callable(pm):
            try:
                return pm()
            except Exception:
                log.debug("Public media service unavailable", exc_info=True)
                return None
        return None

    @property
    def stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()


def default_runtime(clock: Clock | None = None) -> EngineRuntime:
    """Runtime bound to the current app database (or the running scheduler's runtime)."""
    try:
        from planificador import scheduler as _scheduler

        sched = _scheduler.get_scheduler()
    except Exception:  # pragma: no cover - import problems
        sched = None
    if sched is not None:
        rt = sched.runtime
        if clock is None or clock is rt.clock:
            return rt
        return EngineRuntime(
            session_factory=rt.session_factory,
            clock=clock,
            http=rt.http,
            public_media=rt.public_media,
            base_url=rt.base_url,
            write_lock=rt.write_lock,
            stop_event=rt.stop_event,
            rng=rt.rng,
            connectivity=rt.connectivity,
        )
    from planificador import db as dbmod

    database = dbmod.get_current_database()
    return EngineRuntime(
        session_factory=database.session,
        clock=clock or SystemClock(),
        write_lock=getattr(database, "write_lock", None),
    )


# Targets currently being run in this process (any scheduler instance): stale-claim recovery skips them.
_active_lock = threading.Lock()
_active_runs: set[int] = set()


def active_runs() -> set[int]:
    with _active_lock:
        return set(_active_runs)


def is_running(target_id: int) -> bool:
    with _active_lock:
        return target_id in _active_runs


# Token refreshes of one account are serialised process-wide (engine runs and the hourly token job): a network
# may rotate the refresh token on every use (TikTok), so two refreshes sent with the same one can invalidate
# each other. Whoever holds the lock re-reads the account first (:func:`load_credentials`), so the second
# caller sees the tokens the first one stored and usually has nothing left to refresh.
_credentials_guard = threading.Lock()
_credentials_locks: dict[int, Any] = {}


def credentials_lock(account_id: int) -> Any:
    """Re-entrant lock serialising credential refreshes (and their storage) for ``account_id``."""
    with _credentials_guard:
        lock = _credentials_locks.get(account_id)
        if lock is None:
            lock = _credentials_locks[account_id] = threading.RLock()
        return lock


def load_credentials(session_factory: Callable[[], Session], account_id: int) -> AccountCreds | None:
    """The account's current credentials, read in a fresh session (never from a cached ORM object)."""
    with session_factory() as s:
        account = s.get(Account, account_id)
        return account.to_creds() if account is not None else None


def _adopt_tokens(creds: AccountCreds, latest: AccountCreds) -> None:
    creds.access_token = latest.access_token
    creds.refresh_token = latest.refresh_token
    creds.token_expires_at = latest.token_expires_at
    creds.refresh_expires_at = latest.refresh_expires_at
    creds.scopes = list(latest.scopes or [])


# ------------------------------------------------------------------------------------------- helpers


def network_label(network: Any) -> str:
    key = str(network)
    return NETWORK_LABELS.get(key, key.capitalize())


def snippet(text: str | None, limit: int = 80) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def retry_delay(attempt: int, retry_after: int | None = None, rng: random.Random | None = None) -> timedelta:
    """Delay before retry number ``attempt`` (1-based). ``retry_after`` (seconds) wins when given."""
    if retry_after is not None and retry_after > 0:
        return timedelta(seconds=min(int(retry_after), MAX_RETRY_AFTER_S))
    base = RETRY_DELAYS_S[min(max(attempt, 1), len(RETRY_DELAYS_S)) - 1]
    factor = (rng or random).uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)
    return timedelta(seconds=max(1.0, base * factor))


def is_tiktok_inbox(network: Any, options: dict | None) -> bool:
    """TikTok "send to the app as a draft" mode goes through the provider even with auto-publish off."""
    if str(network) != "tiktok":
        return False
    return str((options or {}).get("mode") or "").strip().lower() == "inbox"


#: Networks whose providers can hand a post over to the network's own scheduler (``native_schedule``).
NATIVE_SCHEDULE_NETWORKS: tuple[str, ...] = ("facebook", "youtube")
_FALSY = {"", "0", "false", "no", "off", "none", "null"}


def wants_native_schedule(network: Any, options: dict | None, fmt: str | None = None) -> bool:
    """Whether the target asks to be handed over to the network's scheduler ahead of time (FB/YouTube).

    With ``fmt`` the check is strict about what the network can schedule natively: Facebook only schedules
    ``post`` targets, and YouTube only a **public** video (``publishAt`` flips private → public). Anything
    else must not be claimed early (it would go out before its time)."""
    network = str(network)
    if network not in NATIVE_SCHEDULE_NETWORKS:
        return False
    opts = options or {}
    value = opts.get("native_schedule")
    wanted = value.strip().lower() not in _FALSY if isinstance(value, str) else bool(value)
    if not wanted or fmt is None:
        return wanted
    if network == "facebook":
        return str(fmt) == "post"
    privacy = str(opts.get("privacy_status") or "public").strip().lower()
    return privacy in ("public", "publico", "público")


def split_state(raw: dict | None) -> tuple[dict, dict]:
    """``provider_state`` → (provider part, engine bookkeeping)."""
    state = dict(raw or {})
    meta = state.pop(ENGINE_STATE_KEY, None)
    return state, dict(meta) if isinstance(meta, dict) else {}


def merge_state(state: dict, meta: dict) -> dict:
    out = {k: v for k, v in (state or {}).items() if k != ENGINE_STATE_KEY}
    if meta:
        out[ENGINE_STATE_KEY] = dict(meta)
    return out


def engine_meta(target: PostTarget) -> dict:
    """The engine bookkeeping stored in a target's ``provider_state``."""
    return split_state(target.provider_state)[1]


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------- late publishing


@dataclass(frozen=True)
class LatePolicy:
    """What to do with targets that could not go out at their time (settings in Ajustes › Publicación).

    - ``publish_late`` on (default): publish them as soon as possible — PC on, app open, internet — unless
      they are later than ``max_late`` (``publish_late_max_hours``; ``None`` = no limit), then ``missed``.
    - ``publish_late`` off (old behaviour): due while the app was not running and later than ``grace``
      (``grace_minutes``) → ``missed``; waiting for internet beyond ``grace`` → ``missed`` too.
    - ``spacing`` (``catchup_spacing_minutes``): minimum gap between overdue posts of the same account.
    """

    publish_late: bool = True
    grace: timedelta = timedelta(minutes=120)
    max_late: timedelta | None = None
    spacing: timedelta = timedelta(minutes=15)

    @classmethod
    def load(cls, db: Session) -> LatePolicy:
        hours = max(0, min(config.get_int(db, "publish_late_max_hours"), config.MAX_PUBLISH_LATE_HOURS))
        spacing = max(
            0, min(config.get_int(db, "catchup_spacing_minutes"), config.MAX_CATCHUP_SPACING_MINUTES)
        )
        return cls(
            publish_late=config.get_bool(db, "publish_late"),
            grace=timedelta(minutes=max(0, config.get_int(db, "grace_minutes"))),
            max_late=timedelta(hours=hours) if hours > 0 else None,
            spacing=timedelta(minutes=spacing),
        )


def lateness_reference(target: PostTarget) -> datetime | None:
    """When a ``publishing`` target was due: the moment recorded at its claim (``due_at``: the scheduled time,
    or the time of an explicit retry), else its scheduled time."""
    due = _parse_iso(engine_meta(target).get("due_at"))
    if due is not None:
        return due
    return target.scheduled_at or target.next_attempt_at or target.claimed_at


def is_offline_waiting(target: PostTarget) -> bool:
    """A ``publishing`` target that sent nothing irreversible and waits for the internet connection."""
    if target.status != TargetStatus.PUBLISHING or target.commit_phase == CommitPhase.COMMIT_SENT:
        return False
    return bool(engine_meta(target).get("offline"))


def is_queued_after_outage(target: PostTarget) -> bool:
    """A ``publishing`` target that waited for the internet connection, which is back, and now waits for its
    turn in its account's catch-up queue (engine flag ``queued``, :func:`end_offline_wait`)."""
    if target.status != TargetStatus.PUBLISHING or target.commit_phase == CommitPhase.COMMIT_SENT:
        return False
    return bool(engine_meta(target).get("queued"))


#: Exception class names that mean "the request did not get through" (as providers record them in
#: ``ProviderError.detail["error"]`` when they wrap transport errors) and the codes they use for them.
_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "ReadError",
        "WriteError",
        "NetworkError",
        "RemoteProtocolError",
        "TransportError",
        "ProxyError",
        "gaierror",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "TimeoutError",
    }
)
_NETWORK_ERROR_CODES = frozenset(
    {
        "meta_connect_failed",
        "meta_timeout",
        "meta_network",
        "tiktok_network",
        "tiktok_timeout",
        "youtube_network",
        "youtube_timeout",
        "demo_offline",
    }
)
_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    socket.gaierror,
    ConnectionError,
    TimeoutError,
)


def is_network_failure(exc: BaseException) -> bool:
    """Whether ``exc`` looks like "the request never got an answer" (no internet, DNS failure, no route,
    connect/read timeout) rather than an answer from the API. HTTP errors (5xx, 429) and rejections are not.
    It is only a hint: the engine confirms it with a connectivity probe before treating it as offline."""
    if isinstance(exc, ProviderError) and exc.kind != ErrorKind.TRANSIENT:
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TRANSPORT_EXCEPTIONS):
            return True
        current = current.__cause__ or current.__context__
    if isinstance(exc, ProviderError):
        if exc.code in _NETWORK_ERROR_CODES:
            return True
        name = exc.detail.get("error") or exc.detail.get("exception")
        return isinstance(name, str) and name in _TRANSPORT_ERROR_NAMES
    return False


def _late_reason_text(reason: str | None) -> str:
    if reason == LATE_OFFLINE:
        return "no había conexión a internet"
    return "el equipo o la app no estaba disponible"


def _resolve_outage_notices(s: Session, now: datetime) -> None:
    """Mark the unread "Sin conexión a internet" notifications read (their outage is over)."""
    from planificador.models import Notification

    s.execute(
        update(Notification)
        .where(
            Notification.kind == NotificationKind.INFO,
            Notification.title == OUTAGE_TITLE,
            Notification.read_at.is_(None),
        )
        .values(read_at=now)
        .execution_options(synchronize_session=False)
    )


def _clear_outage(s: Session, now: datetime) -> dict | None:
    """Drop the outage record and resolve its notification (no commit). Returns the dropped record, if any."""
    raw = config.get_setting(s, OUTAGE_KEY, None)
    if raw is None:
        return None
    config.delete_setting(s, OUTAGE_KEY, commit=False)
    _resolve_outage_notices(s, now)
    return raw if isinstance(raw, dict) else {}


def note_connectivity(rt: EngineRuntime, online: bool, now: datetime) -> None:
    """Track the current internet outage in ``settings`` (:data:`OUTAGE_KEY`): started on the first failed
    probe, one "Sin conexión a internet" notification once it lasts :data:`OFFLINE_NOTIFY_AFTER`, cleared (and
    that notification marked read) when a probe succeeds. A failed probe more than :data:`OUTAGE_STALE_AFTER`
    after the previous one starts a new outage (the old notification is resolved; the new one is notified
    again). Never raises."""
    conn = rt.get_connectivity()
    if online and getattr(conn, "outage_active", None) is False:
        return
    try:
        if online:
            previous = _write(rt, lambda s: _clear_outage(s, now))
            if previous is not None:
                log.info("Internet connection is back (offline since %s)", previous.get("since"))
            conn.outage_active = False
            return

        def mark(s: Session) -> None:
            raw = config.get_setting(s, OUTAGE_KEY, None)
            info = dict(raw) if isinstance(raw, dict) else {}
            since = _parse_iso(info.get("since"))
            last = _parse_iso(info.get("last_failed_at")) or since
            if since is None or since > now or last is None or now - last > OUTAGE_STALE_AFTER:
                if raw is not None:
                    _resolve_outage_notices(s, now)  # that outage ended unnoticed: this is a new one
                fresh = {"since": now.isoformat(), "last_failed_at": now.isoformat(), "notified": False}
                config.set_setting(s, OUTAGE_KEY, fresh, commit=False)
                log.warning("No internet connection: publishing waits for it")
                return
            info["last_failed_at"] = max(last, now).isoformat()
            if not info.get("notified") and now - since >= OFFLINE_NOTIFY_AFTER:
                info["notified"] = True
                _notify(
                    s,
                    NotificationKind.INFO,
                    OUTAGE_TITLE,
                    "Hay publicaciones esperando a la conexión. Saldrán solas en cuanto vuelva internet, sin "
                    "gastar reintentos: deja el ordenador encendido y la app abierta.",
                    now=now,
                )
            config.set_setting(s, OUTAGE_KEY, info, commit=False)

        _write(rt, mark)
        conn.outage_active = True
    except Exception:  # bookkeeping only: never break publishing
        log.debug("Could not record the connectivity state", exc_info=True)


def settle_outage(rt: EngineRuntime, now: datetime) -> bool:
    """End the outage record when no target waits for the connection any more (the scheduler calls this
    every tick; it is one cheap read while there is no record). Only waiting targets probe the connection, so
    if they all left that state (cancelled, ``missed``…) before internet came back, no successful probe would
    ever clear the record: its notification would stay unread and the next outage would not be notified.
    Records younger than :data:`OUTAGE_SETTLE_AFTER` are left alone (a run may be about to start waiting).
    Returns whether a record was dropped. Never raises."""
    conn = rt.get_connectivity()

    def settle(s: Session) -> bool | None:
        raw = config.get_setting(s, OUTAGE_KEY, None)
        if raw is None:
            return None
        info = raw if isinstance(raw, dict) else {}
        last = _parse_iso(info.get("last_failed_at")) or _parse_iso(info.get("since"))
        if last is not None and now - last < OUTAGE_SETTLE_AFTER:
            return False
        publishing = s.scalars(select(PostTarget).where(PostTarget.status == TargetStatus.PUBLISHING))
        if any(is_offline_waiting(t) for t in publishing):
            return False
        _clear_outage(s, now)
        return True

    try:
        with rt.session_factory() as s:
            recorded = config.get_setting(s, OUTAGE_KEY, None) is not None
        dropped = _write(rt, settle) if recorded else None
    except Exception:  # bookkeeping only
        log.debug("Could not settle the connectivity state", exc_info=True)
        return False
    conn.outage_active = dropped is False
    if dropped:
        log.info("No post waits for the internet connection any more: outage record cleared")
    return bool(dropped)


def media_info(asset: MediaAsset) -> MediaInfo:
    """``media.to_media_info`` when implemented, else a local equivalent."""
    try:
        from planificador import media as media_mod

        fn = getattr(media_mod, "to_media_info", None)
        if fn is not None:
            return fn(asset)
    except NotImplementedError:
        pass
    return _fallback_media_info(asset)


def _fallback_media_info(asset: MediaAsset) -> MediaInfo:
    media_dir = config.get_app_settings().paths.media_dir
    return MediaInfo(
        id=asset.id,
        kind=str(asset.kind),
        path=media_dir / asset.rel_path,
        mime=asset.mime,
        size_bytes=int(asset.size_bytes or 0),
        width=asset.width,
        height=asset.height,
        duration_s=asset.duration_s,
        fps=asset.fps,
        video_codec=asset.video_codec,
        audio_codec=asset.audio_codec,
        bitrate=asset.bitrate,
        is_hdr=bool(asset.is_hdr),
        original_name=asset.original_name,
    )


def target_text(target: PostTarget) -> str:
    """Final text for a target (``postsvc.target_text`` when available, else the override/base text)."""
    try:
        from planificador import postsvc

        fn = getattr(postsvc, "target_text", None)
        if fn is not None:
            return str(fn(target))
    except (ImportError, NotImplementedError):
        pass
    except Exception:
        log.debug("postsvc.target_text failed; using effective_text", exc_info=True)
    return target.effective_text


def load_media(db: Session, target: PostTarget, *, strict: bool = True) -> tuple[list[MediaInfo], list[str]]:
    """MediaInfo list for the target's effective media + names of missing/unreadable files."""
    ids = target.effective_media_ids
    if not ids:
        return [], []
    assets = {a.id: a for a in db.scalars(select(MediaAsset).where(MediaAsset.id.in_(ids)))}
    infos: list[MediaInfo] = []
    missing: list[str] = []
    for media_id in ids:
        asset = assets.get(media_id)
        if asset is None:
            missing.append(f"#{media_id}")
            continue
        try:
            info = media_info(asset)
        except Exception:
            log.warning("Could not read media %s for target %s", media_id, target.id, exc_info=True)
            missing.append(asset.original_name or f"#{media_id}")
            continue
        if not info.path.is_file():
            missing.append(asset.original_name or f"#{media_id}")
            if strict:
                continue
        infos.append(info)
    return infos, missing


def _guarded_update(
    db: Session,
    target_id: int,
    values: dict[str, Any],
    *,
    status_in: Any,
    extra: tuple[Any, ...] = (),
) -> bool:
    statuses = [status_in] if isinstance(status_in, str) else list(status_in)
    stmt = (
        update(PostTarget)
        .where(PostTarget.id == target_id, PostTarget.status.in_(statuses), *extra)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return db.execute(stmt).rowcount == 1


def add_event(
    db: Session,
    target_id: int,
    now: datetime,
    event: str,
    message: str,
    *,
    level: str = EventLevel.INFO,
    data: dict | None = None,
) -> None:
    db.add(
        PostEvent(
            target_id=target_id,
            ts=now,
            level=level,
            event=event[:64],
            message=scrub_text(message or ""),
            data=scrub(data) if data else None,
        )
    )


def _notify(db: Session, kind: str, title: str, body: str, *, now: datetime, **kwargs: Any) -> None:
    from planificador import notify as notify_mod

    try:
        notify_mod.notify(db, kind, title, body, commit=False, now=now, **kwargs)
    except Exception:  # a notification must never break a status transition
        log.exception("Could not create notification %s", kind)


def _refresh_object(db: Session, obj: Any) -> None:
    try:
        db.refresh(obj)
    except Exception:  # pragma: no cover - deleted concurrently
        log.debug("Could not refresh %r", obj, exc_info=True)


def _wake_scheduler() -> None:
    try:
        from planificador import scheduler as _scheduler

        _scheduler.wake()
    except Exception:  # pragma: no cover
        log.debug("Scheduler wake failed", exc_info=True)


@contextmanager
def _write_session(rt: EngineRuntime) -> Iterator[Session]:
    lock = rt.write_lock if rt.write_lock is not None else nullcontext()
    with lock:
        session = rt.session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


def _write[T](rt: EngineRuntime, fn: Callable[[Session], T], *, attempts: int = 3) -> T:
    """Run ``fn`` in its own short write transaction, retrying briefly on ``database is locked``."""
    for attempt in range(attempts):
        try:
            with _write_session(rt) as session:
                return fn(session)
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------------------- messages


def _msg_manual(network: str, account: str, text: str) -> tuple[str, str]:
    label = network_label(network)
    lead = f"{account}: «{snippet(text, 90)}»." if text and text.strip() else f"{account}."
    return (
        f"Es hora de publicar en {label}",
        f"{lead} Abre la publicación asistida para copiar el texto, descargar los archivos y publicarla en "
        f"{label}.",
    )


_LATE_SETTING_HINT = (
    "Para que las publicaciones atrasadas salgan solas en cuanto el equipo, la app e internet vuelvan a "
    "estar disponibles, activa «Publicar las publicaciones atrasadas en cuanto se pueda» en Ajustes › "
    "Publicación."
)


def _msg_missed(
    network: str,
    account: str,
    scheduled_at: datetime | None,
    tz: str | None,
    reason: str = MISSED_GRACE,
    limit: timedelta | None = None,
) -> tuple[str, str]:
    label = network_label(network)
    when = format_dt_es(scheduled_at, tz) if scheduled_at else "la hora prevista"
    actions = "Puedes publicarla ahora, reprogramarla o descartarla."
    if reason == MISSED_MAX_LATE:
        bound = format_duration_es(limit.total_seconds()) if limit is not None else "el máximo"
        body = (
            f"{account}: estaba programada para el {when} y ya supera el límite de retraso ({bound}) de "
            f"Ajustes › Publicación. {actions}"
        )
    elif reason == MISSED_OFFLINE:
        body = f"{account}: estaba programada para el {when} y no había conexión a internet. {actions} "
        body += _LATE_SETTING_HINT
    else:
        body = f"{account}: estaba programada para el {when} y la app no estaba abierta. {actions} "
        body += _LATE_SETTING_HINT
    return f"Publicación perdida en {label}", body


def _msg_published_late(
    network: str, account: str, due_at: datetime, tz: str | None, delay: timedelta, reason: str | None
) -> tuple[str, str]:
    label = network_label(network)
    return (
        f"Publicada con retraso en {label}",
        f"{account}: estaba programada para el {format_dt_es(due_at, tz)} y ha salido "
        f"{format_duration_es(delay.total_seconds())} tarde porque {_late_reason_text(reason)}.",
    )


def _msg_error(network: str, account: str, message: str) -> tuple[str, str]:
    return f"No se pudo publicar en {network_label(network)}", f"{account}: {message}"


_RECONNECT_ADVICE = re.compile(r"\b(?:re)?conect|\breconex")


def _says_how_to_reconnect(message: str) -> bool:
    """Whether a provider message already tells the user to reconnect ("Vuelve a conectarla…",
    "Reconéctala…", "necesita reconexión"; most AUTH messages end that way). "desconectada" does not count."""
    plain = unicodedata.normalize("NFKD", message or "").encode("ascii", "ignore").decode().lower()
    return _RECONNECT_ADVICE.search(plain) is not None


def _msg_reconnect(network: str, account: str, message: str) -> tuple[str, str]:
    label = network_label(network)
    text = (message or "").strip()
    if not _says_how_to_reconnect(text):
        text = f"{text} Ve a Cuentas conectadas para volver a conectarla.".strip()
    return f"Reconecta tu cuenta de {label}", f"{account}: {text}"


def _msg_unknown(network: str, account: str) -> tuple[str, str]:
    label = network_label(network)
    return (
        f"Comprueba tu publicación en {label}",
        f"No hemos podido confirmar si la publicación salió en {account}. Revísalo en {label} y márcala como "
        "publicada, o reinténtala si no aparece.",
    )


def _msg_sent_to_app(network: str, account: str) -> tuple[str, str]:
    label = network_label(network)
    return (
        f"Termina la publicación en {label}",
        f"Abre {label} para terminar la publicación: el vídeo de {account} te espera en la bandeja de "
        "la app.",
    )


# ------------------------------------------------------------------- scheduler-facing transitions


#: ``post_events`` written when a target is (re)started: the catch-up spacing measures the gap from the latest
#: of these per account (and from the latest ``published_at``).
RELEASE_EVENTS: tuple[str, ...] = ("publishing", "catchup", "recovered")


def _update_meta(
    db: Session, target_id: int, *, set_: dict | None = None, drop: tuple[str, ...] = ()
) -> None:
    """Merge engine bookkeeping into a target this caller just claimed (same transaction as the claim)."""
    row = db.get(PostTarget, target_id, populate_existing=True)
    if row is None:  # pragma: no cover - deleted concurrently (the claim would have failed)
        return
    state, meta = split_state(row.provider_state)
    for key in drop:
        meta.pop(key, None)
    meta.update(set_ or {})
    row.provider_state = merge_state(state, meta)


def claim_due(
    db: Session,
    target_id: int,
    now: datetime,
    *,
    late_by: timedelta | None = None,
    due_at: datetime | None = None,
    catchup: bool = False,
    tz: str | None = None,
) -> bool:
    """``scheduled`` → ``publishing`` (atomic). Commits. Returns whether this caller owns the target.

    Records when it was due (``due_at``, default: its scheduled time) for the late-publishing rules; a
    ``catchup`` claim (it is due since the PC/app was not available) is marked ``late = unavailable`` and its
    ``late`` event says when it was due."""
    claimed = _guarded_update(
        db,
        target_id,
        {
            "status": TargetStatus.PUBLISHING,
            "claimed_at": now,
            "next_attempt_at": None,
            "updated_at": now,
        },
        status_in=TargetStatus.SCHEDULED,
    )
    if not claimed:
        db.rollback()
        return False
    row = db.get(PostTarget, target_id, populate_existing=True)
    reference = due_at or (row.scheduled_at if row is not None else None) or now
    meta: dict[str, Any] = {"due_at": ensure_utc(reference).isoformat()}
    if catchup:
        meta["late"] = LATE_UNAVAILABLE
    _update_meta(
        db, target_id, set_=meta, drop=("late", "offline", "offline_since", "queued", "last_wait_message")
    )
    add_event(db, target_id, now, "publishing", "Empieza la publicación.")
    if late_by is not None and late_by > LATE_NOTE_AFTER:
        message = f"Se publica con {format_duration_es(late_by.total_seconds())} de retraso."
        if catchup:
            message = (
                f"Se publica con {format_duration_es(late_by.total_seconds())} de retraso: estaba programada "
                f"para el {format_dt_es(reference, tz)} y el equipo o la app no estaba disponible."
            )
        add_event(
            db,
            target_id,
            now,
            "late",
            message,
            level=EventLevel.WARNING,
            data={"late_seconds": int(late_by.total_seconds()), "catchup": catchup},
        )
    db.commit()
    return True


def _release_note(release: str, *, queued: bool = False) -> str:
    if queued:
        return "Le llega el turno: se retoma la publicación atrasada."
    if release == LATE_OFFLINE:
        return "Ha vuelto la conexión a internet: se retoma la publicación."
    return "Publicación atrasada: se retoma ahora que el equipo y la app vuelven a estar disponibles."


def claim_waiting(
    db: Session,
    target_id: int,
    seen_next_attempt_at: datetime,
    now: datetime,
    *,
    release: str | None = None,
) -> bool:
    """Take a ``publishing`` target whose wait/backoff elapsed (atomic). Commits.

    ``release`` (``unavailable`` | ``offline``): it resumes late, as a catch-up release (a ``catchup`` event,
    which the per-account spacing counts, and ``late`` bookkeeping for the "Publicada con retraso" note)."""
    claimed = _guarded_update(
        db,
        target_id,
        {"claimed_at": now, "next_attempt_at": None, "updated_at": now},
        status_in=TargetStatus.PUBLISHING,
        extra=(PostTarget.next_attempt_at == seen_next_attempt_at,),
    )
    if not claimed:
        db.rollback()
        return False
    row = db.get(PostTarget, target_id)
    meta = engine_meta(row) if row is not None else {}
    if release:
        _update_meta(
            db,
            target_id,
            set_={"late": meta.get("late") or release},
            drop=("offline", "offline_since", "queued"),
        )
        note = _release_note(release, queued=bool(meta.get("queued")))
        add_event(db, target_id, now, "catchup", note, data={"reason": release})
    elif "queued" in meta:
        _update_meta(db, target_id, drop=("queued",))
    db.commit()
    return True


def claim_stale(
    db: Session,
    target_id: int,
    seen_claimed_at: datetime | None,
    now: datetime,
    *,
    release: str | None = None,
) -> bool:
    """Re-claim a ``publishing`` target abandoned by a crashed run (atomic). Commits. ``release`` as in
    :func:`claim_waiting` (the ``recovered`` event already counts as a release for the spacing)."""
    seen = (
        PostTarget.claimed_at.is_(None)
        if seen_claimed_at is None
        else PostTarget.claimed_at == seen_claimed_at
    )
    claimed = _guarded_update(
        db,
        target_id,
        {"claimed_at": now, "updated_at": now},
        status_in=TargetStatus.PUBLISHING,
        extra=(PostTarget.next_attempt_at.is_(None), seen),
    )
    if not claimed:
        db.rollback()
        return False
    if release:
        row = db.get(PostTarget, target_id)
        reason = engine_meta(row).get("late") if row is not None else None
        _update_meta(
            db, target_id, set_={"late": reason or release}, drop=("offline", "offline_since", "queued")
        )
    add_event(
        db,
        target_id,
        now,
        "recovered",
        "Se retoma la publicación tras un cierre inesperado de la app.",
        level=EventLevel.WARNING,
    )
    db.commit()
    return True


def postpone_offline(db: Session, target: PostTarget, now: datetime) -> bool:
    """Still no internet: push a waiting target's next check :data:`OFFLINE_RECHECK` later (quietly: no event,
    no retry spent). Guarded on the ``next_attempt_at`` the caller read. Commits."""
    ok = _guarded_update(
        db,
        target.id,
        {"next_attempt_at": now + OFFLINE_RECHECK},
        status_in=TargetStatus.PUBLISHING,
        extra=(
            PostTarget.next_attempt_at == target.next_attempt_at,
            PostTarget.commit_phase != CommitPhase.COMMIT_SENT,
        ),
    )
    if not ok:
        db.rollback()
        return False
    db.commit()
    return True


def end_offline_wait(db: Session, target: PostTarget, now: datetime, *, queued: bool = False) -> bool:
    """The connection is back, but the scheduler does not resume this offline-waiting target in this tick
    (the catch-up spacing of its account holds it back, or no worker is free): it no longer waits for the
    internet, so drop ``offline`` / ``offline_since`` (no more "Esperando conexión a internet" in its detail).
    ``queued``: it is an overdue catch-up release, so record engine flag ``queued`` to keep its place in the
    account's catch-up queue (:func:`is_queued_after_outage`). Guarded on the ``next_attempt_at`` the caller
    read. Commits."""
    state, meta = split_state(target.provider_state)
    if not meta.get("offline"):
        return False
    meta.pop("offline", None)
    meta.pop("offline_since", None)
    if queued:
        meta["queued"] = True
    ok = _guarded_update(
        db,
        target.id,
        {"provider_state": merge_state(state, meta), "updated_at": now},
        status_in=TargetStatus.PUBLISHING,
        extra=(
            PostTarget.next_attempt_at == target.next_attempt_at,
            PostTarget.commit_phase != CommitPhase.COMMIT_SENT,
        ),
    )
    if not ok:
        db.rollback()
        return False
    if queued:
        message = (
            "Ha vuelto la conexión a internet. Saldrá en su turno: las publicaciones atrasadas de una misma "
            "cuenta salen de una en una, con la separación elegida en Ajustes › Publicación."
        )
    else:
        message = "Ha vuelto la conexión a internet: se continúa con la publicación."
    add_event(db, target.id, now, "online", message)
    db.commit()
    return True


def set_manual_pending(db: Session, target: PostTarget, now: datetime) -> bool:
    """Due target with auto-publish off → ``manual_pending`` + "Publicación asistida" notification."""
    if not _guarded_update(
        db,
        target.id,
        {"status": TargetStatus.MANUAL_PENDING, "next_attempt_at": None, "updated_at": now},
        status_in=TargetStatus.SCHEDULED,
    ):
        db.rollback()
        return False
    account = target.account
    label = account.display_name if account is not None else ""
    add_event(
        db,
        target.id,
        now,
        "manual_pending",
        "Es la hora: publícala a mano con la publicación asistida.",
        level=EventLevel.WARNING,
    )
    title, body = _msg_manual(str(target.network), label, target_text(target))
    _notify(
        db,
        NotificationKind.MANUAL_PUBLISH,
        title,
        body,
        now=now,
        target_id=target.id,
        account_id=target.account_id,
    )
    db.commit()
    return True


def resume_reference(target: PostTarget) -> datetime | None:
    """When a ``publishing`` target was meant to carry on: its scheduled time, or the later end of its
    wait/backoff (``next_attempt_at``) or of its last claim heartbeat (``claimed_at``)."""
    moments = [m for m in (target.scheduled_at, target.next_attempt_at, target.claimed_at) if m is not None]
    return max(moments) if moments else None


def can_miss_while_publishing(target: PostTarget) -> bool:
    """A ``publishing`` target that has sent nothing irreversible (waiting before a retry, paused at shutdown,
    or abandoned by a crash before its commit) may still be declared missed."""
    if target.status != TargetStatus.PUBLISHING or target.commit_phase == CommitPhase.COMMIT_SENT:
        return False
    _state, meta = split_state(target.provider_state)
    return meta.get("pending") != "reconcile"


def set_missed(
    db: Session,
    target: PostTarget,
    now: datetime,
    *,
    grace: timedelta | None = None,
    reason: str = MISSED_GRACE,
    limit: timedelta | None = None,
) -> bool:
    """A target that will not go out late → ``missed`` + notification. ``reason``: :data:`MISSED_GRACE` (late
    publishing off: due while the app was off and beyond the grace window ``grace``), :data:`MISSED_MAX_LATE`
    (beyond the "Límite de retraso" ``limit``) or :data:`MISSED_OFFLINE` (late publishing off: no internet
    beyond the grace window).

    Also for a ``publishing`` target that has not sent anything irreversible yet (see
    :func:`can_miss_while_publishing`): the guard is on the values the caller read (``next_attempt_at``,
    ``claimed_at``), so a target claimed meanwhile is left alone; its partial provider state is dropped."""
    publishing = target.status == TargetStatus.PUBLISHING
    if publishing:
        if not can_miss_while_publishing(target):
            return False
        seen_next = target.next_attempt_at
        seen_claim = target.claimed_at
        values: dict[str, Any] = {
            "status": TargetStatus.MISSED,
            "provider_state": {},
            "commit_phase": CommitPhase.NONE,
            "attempts": 0,
            "next_attempt_at": None,
            "claimed_at": None,
            "last_error": None,
            "last_error_detail": None,
            "updated_at": now,
        }
        extra: tuple[Any, ...] = (
            PostTarget.commit_phase != CommitPhase.COMMIT_SENT,
            PostTarget.next_attempt_at.is_(None)
            if seen_next is None
            else PostTarget.next_attempt_at == seen_next,
            PostTarget.claimed_at.is_(None) if seen_claim is None else PostTarget.claimed_at == seen_claim,
        )
        ok = _guarded_update(db, target.id, values, status_in=TargetStatus.PUBLISHING, extra=extra)
    else:
        ok = _guarded_update(
            db,
            target.id,
            {"status": TargetStatus.MISSED, "next_attempt_at": None, "updated_at": now},
            status_in=TargetStatus.SCHEDULED,
        )
    if not ok:
        db.rollback()
        return False
    account = target.account
    label = account.display_name if account is not None else ""
    tz = target.post.brand.timezone if target.post is not None and target.post.brand is not None else None
    if limit is None:
        limit = grace
    margin = format_duration_es(limit.total_seconds()) if limit is not None else "espera"
    data: dict[str, Any] = {"reason": reason}
    if grace is not None:
        data["grace_minutes"] = int(grace.total_seconds() // 60)
    if limit is not None:
        data["limit_minutes"] = int(limit.total_seconds() // 60)
    if reason == MISSED_MAX_LATE:
        message = (
            f"No se publicó: ya supera el límite de retraso de {margin} (Ajustes › Publicación), así que "
            "no se envía tan tarde."
        )
    elif reason == MISSED_OFFLINE:
        message = (
            f"No se publicó: no había conexión a internet y ya ha pasado el margen de {margin} (publicar con "
            "retraso está desactivado en Ajustes › Publicación)."
        )
    elif publishing:
        message = (
            "No se publicó: la app no estaba abierta para terminar el envío y ya ha pasado el margen de "
            f"{margin}."
        )
    else:
        message = (
            "No se publicó: la app no estaba abierta a la hora programada y ya ha pasado el margen de "
            f"{margin}."
        )
    add_event(db, target.id, now, "missed", message, level=EventLevel.ERROR, data=data)
    title, body = _msg_missed(str(target.network), label, target.scheduled_at, tz, reason, limit)
    _notify(
        db, NotificationKind.MISSED, title, body, now=now, target_id=target.id, account_id=target.account_id
    )
    db.commit()
    return True


def mark_account_needs_reconnect(
    db: Session, account_id: int, message: str, now: datetime, *, target_id: int | None = None
) -> bool:
    """``connected`` → ``needs_reconnect`` (+ one notification per transition). Does not commit."""
    changed = (
        db.execute(
            update(Account)
            .where(Account.id == account_id, Account.status == AccountStatus.CONNECTED)
            .values(status=AccountStatus.NEEDS_RECONNECT, last_error=message, last_checked_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        == 1
    )
    if changed:
        account = db.get(Account, account_id)
        if account is not None:
            db.refresh(account)
            title, body = _msg_reconnect(str(account.network), account.display_name, message)
            _notify(
                db,
                NotificationKind.RECONNECT,
                title,
                body,
                now=now,
                target_id=target_id,
                account_id=account_id,
            )
    return changed


# ------------------------------------------------------------------------------------------- runner


@dataclass
class _TargetInfo:
    id: int
    post_id: int
    account_id: int
    network: str
    format: str
    text: str
    media: list[MediaInfo]
    missing_media: list[str]
    options: dict
    auto_publish: bool
    status: str
    scheduled_at: datetime | None
    attempts: int
    commit_phase: str
    state: dict
    engine_meta: dict
    account_status: str
    account_label: str
    creds: AccountCreds
    brand_tz: str | None
    settings: ProviderSettings
    post_deleted: bool
    policy: LatePolicy = field(default_factory=LatePolicy)


class _Runner:
    """One run of one target: from claim until it waits, finishes or fails."""

    def __init__(self, rt: EngineRuntime, target_id: int, http: httpx.Client) -> None:
        self.rt = rt
        self.target_id = target_id
        self.http = http
        self.clock = rt.clock
        self.info: _TargetInfo
        self.provider: Provider
        self.ctx: PublishContext | None = None
        self.state: dict = {}
        self.engine_meta: dict = {}
        self.commit_phase: str = CommitPhase.NONE
        self._last_heartbeat: datetime | None = None

    # ---- entry point

    def run(self, *, reconcile: bool = False) -> str:
        info = self._load()
        if info is None or info.status != TargetStatus.PUBLISHING:
            return "skipped"
        self.info = info
        self.state = info.state
        self.engine_meta = info.engine_meta
        self.commit_phase = info.commit_phase
        from planificador.providers import registry

        try:
            self.provider = registry.get_provider(info.network)
        except Exception:
            log.exception("No provider for network %s", info.network)
            return self._fail(
                ProviderError(
                    ErrorKind.PERMANENT,
                    "provider_unavailable",
                    f"La red {network_label(info.network)} no está disponible en esta versión.",
                )
            )

        if reconcile or self.engine_meta.get("pending") == "reconcile":
            ctx = self._context()
            self._refresh_quietly(ctx)
            return self._reconcile(ctx, cause=None)

        # After the irreversible request the provider is only following up (processing polls, extras): the
        # pre-flight gates below are about whether to *send*, so they must not fail a post that may be live.
        committed = self.commit_phase == CommitPhase.COMMIT_SENT
        if not committed:
            if info.post_deleted:
                return self._cancel_trashed()
            if info.account_status != AccountStatus.CONNECTED:
                return self._account_unavailable()
            if info.missing_media:
                return self._fail(
                    ProviderError(
                        ErrorKind.PERMANENT,
                        "media_missing",
                        "Falta un archivo de la publicación en la biblioteca ("
                        + ", ".join(info.missing_media[:3])
                        + "). Vuelve a añadirlo y reintenta.",
                    )
                )
            # Waiting for the internet (or a probe just failed): check the connection before sending anything.
            waiting = self.engine_meta.get("offline") or self.rt.get_connectivity().known_offline(
                info.network
            )
            if waiting and not self._early_handoff():
                if not self._online():
                    return self._offline_wait(None)
                self._back_online()

        ctx = self._context()
        first = not self.state and self.commit_phase == CommitPhase.NONE and info.attempts == 0
        try:
            self._refresh(ctx)
        except Exception as exc:
            if committed:
                # Not an outcome of the commit: carry on with the current token and let the provider's own
                # poll error handling decide (it retries token/network failures while polling).
                self._refresh_failed_after_commit(exc)
            elif isinstance(exc, ProviderError):
                return self._on_provider_error(exc, ctx)
            else:
                return self._on_exception(exc, ctx)

        if first:
            errors = [i for i in self._validate(ctx) if i.level == "error"]
            if errors:
                return self._fail(
                    ProviderError(
                        ErrorKind.PERMANENT,
                        "validation",
                        " ".join(i.message for i in errors[:5]),
                        detail={"issues": [i.code for i in errors]},
                    )
                )
        return self._loop(ctx)

    # ---- loading and context

    def _load(self) -> _TargetInfo | None:
        with self.rt.session_factory() as s:
            t = s.get(PostTarget, self.target_id)
            if t is None:
                return None
            post = t.post
            account = t.account
            state, meta = split_state(t.provider_state)
            media, missing = load_media(s, t, strict=True)
            return _TargetInfo(
                id=t.id,
                post_id=t.post_id,
                account_id=t.account_id,
                network=str(t.network),
                format=t.format,
                text=target_text(t),
                media=media,
                missing_media=missing,
                options=dict(t.options or {}),
                auto_publish=bool(t.auto_publish),
                status=str(t.status),
                scheduled_at=t.scheduled_at,
                attempts=int(t.attempts or 0),
                commit_phase=str(t.commit_phase or CommitPhase.NONE),
                state=state,
                engine_meta=meta,
                account_status=str(account.status),
                account_label=account.display_name,
                creds=account.to_creds(),
                brand_tz=post.brand.timezone if post is not None and post.brand is not None else None,
                settings=config.provider_settings(s, base_url=self.rt.base_url),
                post_deleted=post is not None and post.deleted_at is not None,
                policy=LatePolicy.load(s),
            )

    def _context(self) -> PublishContext:
        info = self.info
        spec = TargetSpec(
            network=info.network,
            format=info.format,
            text=info.text,
            media=list(info.media),
            options=dict(info.options),
            scheduled_at=info.scheduled_at,
            auto_publish=info.auto_publish,
        )
        ctx = PublishContext(
            account=info.creds,
            target=spec,
            target_id=info.id,
            state=self.state,
            settings=info.settings,
            http=self.http,
            clock=self.clock,
            public_media=self.rt.get_public_media(),
            save_state=self._save_state,
            before_commit=self._before_commit,
            log=self._log,
            update_tokens=self._store_tokens,
        )
        self.ctx = ctx
        return ctx

    # ---- callbacks handed to the provider

    def _adopt(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise TypeError("provider state must be a dict")
        state.pop(ENGINE_STATE_KEY, None)  # the engine's bookkeeping key is not the provider's to write
        if state is not self.state:
            self.state = state
            if self.ctx is not None:
                self.ctx.state = state

    def _save_state(self, state: dict) -> None:
        self._adopt(state)
        now = self.clock.now()
        persisted = merge_state(self.state, self.engine_meta)

        def write(s: Session) -> None:
            _guarded_update(
                s,
                self.target_id,
                {"provider_state": persisted, "claimed_at": now, "updated_at": now},
                status_in=TargetStatus.PUBLISHING,
            )

        _write(self.rt, write)
        self._last_heartbeat = now

    def _before_commit(self, state: dict) -> None:
        self._adopt(state)
        now = self.clock.now()
        persisted = merge_state(self.state, self.engine_meta)
        label = network_label(self.info.network)

        def write(s: Session) -> bool:
            ok = _guarded_update(
                s,
                self.target_id,
                {
                    "provider_state": persisted,
                    "commit_phase": CommitPhase.COMMIT_SENT,
                    "claimed_at": now,
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            )
            if ok:
                add_event(s, self.target_id, now, "commit_sent", f"Enviando la publicación a {label}.")
            return ok

        if not _write(self.rt, write):
            # Cancelled or taken over meanwhile: abort BEFORE the irreversible request.
            raise RuntimeError(f"Target {self.target_id} is no longer publishing; commit aborted")
        self.commit_phase = CommitPhase.COMMIT_SENT
        self._last_heartbeat = now

    def _log(self, event: str, message: str, data: dict | None = None) -> None:
        now = self.clock.now()
        try:
            _write(self.rt, lambda s: add_event(s, self.target_id, now, str(event), str(message), data=data))
        except Exception:
            log.warning("Could not write event %s for target %s", event, self.target_id, exc_info=True)

    def _heartbeat(self) -> None:
        now = self.clock.now()
        if self._last_heartbeat is not None and now - self._last_heartbeat < HEARTBEAT_EVERY:
            return
        self._last_heartbeat = now
        try:
            _write(
                self.rt,
                lambda s: _guarded_update(
                    s, self.target_id, {"claimed_at": now}, status_in=TargetStatus.PUBLISHING
                ),
            )
        except Exception:
            log.debug("Heartbeat failed for target %s", self.target_id, exc_info=True)

    # ---- connectivity

    def _online(self, *, fresh: bool = False) -> bool:
        """Probe the network's API host (cached unless ``fresh``) and track the outage state."""
        online = self.rt.get_connectivity().check(self.info.network, self.http, fresh=fresh)
        note_connectivity(self.rt, online, self._now())
        return online

    def _back_online(self) -> None:
        if self.engine_meta.pop("offline", None):
            self.engine_meta.pop("offline_since", None)
            self._log_safe(
                "online",
                "Ha vuelto la conexión a internet: se continúa con la publicación.",
                None,
                EventLevel.INFO,
            )

    def _offline_wait(self, exc: ProviderError | None) -> str:
        """No internet before the commit point: wait for the connection without spending a retry. The target
        stays ``publishing``; the scheduler checks the connection again in :data:`OFFLINE_RECHECK`."""
        now = self._now()
        first = not self.engine_meta.get("offline")
        self.engine_meta["offline"] = True
        self.engine_meta.pop("queued", None)
        self.engine_meta.setdefault("offline_since", now.isoformat())
        self.engine_meta.setdefault("late", LATE_OFFLINE)
        self.engine_meta.pop("pending", None)
        next_at = now + OFFLINE_RECHECK
        updates = self._take_account_updates()
        persisted = merge_state(self.state, self.engine_meta)
        detail = exc.to_dict() if exc is not None else None
        message = (
            "Esperando conexión a internet: la publicación saldrá en cuanto vuelva (no se gastan "
            "reintentos). Deja el ordenador encendido y la app abierta."
        )

        def write(s: Session) -> None:
            self._apply_account_updates(s, updates)
            values: dict[str, Any] = {
                "provider_state": persisted,
                "commit_phase": self.commit_phase,
                "next_attempt_at": next_at,
                "claimed_at": None,
                "last_error": None,
                "updated_at": now,
            }
            if detail is not None:
                values["last_error_detail"] = detail
            if _guarded_update(s, self.target_id, values, status_in=TargetStatus.PUBLISHING) and first:
                add_event(s, self.target_id, now, "offline", message, level=EventLevel.WARNING, data=detail)

        _write(self.rt, write)
        return "offline"

    def _offline_instead(self, exc: BaseException) -> bool:
        """A network-level failure before the commit point while there really is no internet (the probe
        confirms it). Early native hand-offs keep their own retry path."""
        if self.commit_phase == CommitPhase.COMMIT_SENT or not is_network_failure(exc):
            return False
        if self._early_handoff():
            return False
        try:
            return not self._online(fresh=True)
        except Exception:  # pragma: no cover - the probe never raises; be defensive anyway
            log.debug("Connectivity probe failed", exc_info=True)
            return False

    # ---- pre-flight

    def _refresh(self, ctx: PublishContext) -> None:
        with credentials_lock(self.info.account_id):
            # Another run or the token job may have refreshed (and rotated) the tokens since this run loaded
            # them: always refresh from what is stored now.
            latest = load_credentials(self.rt.session_factory, self.info.account_id)
            if latest is not None:
                _adopt_tokens(ctx.account, latest)
            try:
                update_ = self.provider.refresh_credentials(ctx.account, ctx.settings, ctx.http, ctx.clock)
            except NotImplementedError:
                update_ = None
            if update_ is not None:
                self._store_tokens(update_)
        self.engine_meta.pop("refresh_error", None)

    def _refresh_failed_after_commit(self, exc: Exception) -> None:
        """A token refresh failed while resuming a target whose commit was already sent. An ``AUTH`` answer
        still flags the account; the run goes on with the current token (logged once per error code)."""
        if isinstance(exc, ProviderError):
            code, message, data = exc.code, exc.message, exc.to_dict()
        else:
            log.warning("Token refresh crashed for committed target %s", self.target_id, exc_info=exc)
            code, message, data = "unexpected_error", "Error inesperado.", {"exception": type(exc).__name__}
        if isinstance(exc, ProviderError) and exc.kind == ErrorKind.AUTH:
            now = self._now()
            account_id = self.info.account_id
            try:
                _write(
                    self.rt,
                    lambda s: mark_account_needs_reconnect(
                        s, account_id, message, now, target_id=self.target_id
                    ),
                )
            except Exception:
                log.warning("Could not flag account %s", account_id, exc_info=True)
        if self.engine_meta.get("refresh_error") != code:
            self.engine_meta["refresh_error"] = code
            self._log_safe(
                "token_refresh_failed",
                f"No se pudo renovar el acceso a la cuenta ({message}). La publicación ya se envió: se sigue "
                "comprobando con el acceso actual.",
                data,
                EventLevel.WARNING,
            )

    def _store_tokens(self, update_: TokenUpdate) -> None:
        """Persist refreshed tokens (before the first step, or mid-run via ``ctx.update_tokens``)."""
        now = self.clock.now()
        account_id = self.info.account_id

        def write(s: Session) -> None:
            account = s.get(Account, account_id)
            if account is None:
                return
            account.apply_token_update(update_)
            account.last_checked_at = now
            add_event(s, self.target_id, now, "token_refreshed", "Se ha renovado el acceso a la cuenta.")

        with credentials_lock(account_id):
            _write(self.rt, write)
        creds = self.ctx.account if self.ctx is not None else self.info.creds
        creds.access_token = update_.access_token
        creds.token_expires_at = update_.token_expires_at
        if update_.refresh_token is not None:
            creds.refresh_token = update_.refresh_token
            creds.refresh_expires_at = update_.refresh_expires_at
        if update_.scopes is not None:
            creds.scopes = list(update_.scopes)

    def _take_account_updates(self) -> dict:
        """Account ``meta`` changes a provider asked for (``state["account_meta_updates"]``, e.g. YouTube's
        lock-test result). Removed from the state so they are applied once."""
        updates = self.state.pop(ACCOUNT_META_UPDATES_KEY, None)
        return dict(updates) if isinstance(updates, dict) else {}

    def _apply_account_updates(self, s: Session, updates: dict) -> None:
        if not updates:
            return
        account = s.get(Account, self.info.account_id)
        if account is not None:
            account.meta = {**(account.meta or {}), **updates}

    def _refresh_quietly(self, ctx: PublishContext) -> None:
        try:
            self._refresh(ctx)
        except Exception:
            log.info("Token refresh before reconcile failed for target %s", self.target_id, exc_info=True)

    def _validate(self, ctx: PublishContext) -> list[Issue]:
        try:
            return list(self.provider.validate(ctx.target) or [])
        except NotImplementedError:
            return []
        except Exception:
            log.exception("Validation crashed for target %s; continuing", self.target_id)
            return []

    # ---- main loop

    def _loop(self, ctx: PublishContext) -> str:
        for _ in range(MAX_STEPS_PER_RUN):
            if self.rt.stopping:
                return self._pause()
            try:
                result = self.provider.publish_step(ctx)
            except ProviderError as exc:
                return self._on_provider_error(exc, ctx)
            except Exception as exc:
                return self._on_exception(exc, ctx)
            status = getattr(result, "status", None)
            if status == "continue":
                self._heartbeat()
                continue
            if status == "wait":
                return self._wait(result)
            if status in ("published", "sent_to_app"):
                return self._finish(result)
            return self._on_exception(RuntimeError(f"Unexpected step status {status!r}"), ctx)
        log.warning("Target %s did %d steps in one run; yielding", self.target_id, MAX_STEPS_PER_RUN)
        return self._wait(StepResult("wait", wait_seconds=5))

    # ---- outcomes

    def _now(self) -> datetime:
        return self.clock.now()

    def _wait(self, result: StepResult) -> str:
        now = self._now()
        seconds = max(MIN_WAIT_S, min(int(result.wait_seconds or 0), MAX_WAIT_S))
        next_at = now + timedelta(seconds=seconds)
        label = network_label(self.info.network)
        message = result.message or f"Esperando a que {label} procese la publicación…"
        write_event = message != self.engine_meta.get("last_wait_message")
        if write_event:
            self.engine_meta["last_wait_message"] = message
        updates = self._take_account_updates()
        persisted = merge_state(self.state, self.engine_meta)

        def write(s: Session) -> None:
            self._apply_account_updates(s, updates)
            ok = _guarded_update(
                s,
                self.target_id,
                {
                    "provider_state": persisted,
                    "commit_phase": self.commit_phase,
                    "next_attempt_at": next_at,
                    "claimed_at": None,
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            )
            if ok and write_event:
                add_event(s, self.target_id, now, "waiting", message, data={"wait_seconds": seconds})

        _write(self.rt, write)
        return "wait"

    def _pause(self) -> str:
        now = self._now()
        persisted = merge_state(self.state, self.engine_meta)
        message = "Publicación en pausa: la app se está cerrando. Continuará al volver a abrirla."
        policy = self.info.policy
        if self.commit_phase != CommitPhase.COMMIT_SENT:
            if not policy.publish_late:
                message += " Si para entonces ha pasado el margen de espera, quedará como perdida."
            elif policy.max_late is not None:
                bound = format_duration_es(policy.max_late.total_seconds())
                message += f" Si para entonces supera el límite de retraso ({bound}), quedará como perdida."

        def write(s: Session) -> None:
            if _guarded_update(
                s,
                self.target_id,
                {
                    "provider_state": persisted,
                    "commit_phase": self.commit_phase,
                    "next_attempt_at": now,
                    "claimed_at": None,
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            ):
                add_event(s, self.target_id, now, "paused", message)

        _write(self.rt, write)
        return "paused"

    def _finish(self, result: StepResult, *, reconciled: bool = False) -> str:
        now = self._now()
        info = self.info
        label = network_label(info.network)
        sent_to_app = result.status == "sent_to_app"
        status = TargetStatus.SENT_TO_APP if sent_to_app else TargetStatus.PUBLISHED
        published_at = now
        if self.state.get("native_scheduled") and info.scheduled_at is not None and info.scheduled_at > now:
            published_at = info.scheduled_at  # handed to the network's scheduler: goes live at that time
        late = self._late_info(published_at)
        self.engine_meta = {}
        updates = self._take_account_updates()
        persisted = merge_state(self.state, self.engine_meta)
        if sent_to_app:
            message = result.message or f"Enviado a la app de {label}: termina allí la publicación."
        elif self.state.get("native_scheduled"):
            when = format_dt_es(info.scheduled_at, info.brand_tz) if info.scheduled_at else ""
            message = result.message or f"Programado en {label}" + (f" para el {when}." if when else ".")
        else:
            message = result.message or f"Publicado en {label}."

        def write(s: Session) -> None:
            self._apply_account_updates(s, updates)
            ok = _guarded_update(
                s,
                self.target_id,
                {
                    "status": status,
                    "provider_state": persisted,
                    "commit_phase": self.commit_phase,
                    "external_id": result.external_id,
                    "external_url": result.external_url,
                    "published_at": published_at,
                    "next_attempt_at": None,
                    "claimed_at": None,
                    "last_error": None,
                    "last_error_detail": None,
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            )
            if not ok:
                log.error(
                    "Target %s finished (%s, external id %s) but was no longer publishing",
                    self.target_id,
                    status,
                    result.external_id,
                )
            add_event(
                s,
                self.target_id,
                now,
                "reconciled_published" if reconciled else str(status),
                message,
                data={"external_id": result.external_id, "external_url": result.external_url},
            )
            if ok:  # earlier error/missed reminders about this target are moot now
                _resolve_notifications(s, self.target_id, now)
            if late is not None and ok:
                due_at, delay, reason = late
                when = format_dt_es(due_at, info.brand_tz)
                add_event(
                    s,
                    self.target_id,
                    now,
                    "published_late",
                    f"{'Enviada a la app' if sent_to_app else 'Publicada'} con retraso: estaba programada "
                    f"para el {when}; {_late_reason_text(reason)}.",
                    level=EventLevel.WARNING,
                    data={
                        "due_at": due_at.isoformat(),
                        "late_seconds": int(delay.total_seconds()),
                        "reason": reason,
                    },
                )
                if not sent_to_app:
                    title, body = _msg_published_late(
                        info.network, info.account_label, due_at, info.brand_tz, delay, reason
                    )
                    _notify(
                        s,
                        NotificationKind.INFO,
                        title,
                        body,
                        now=now,
                        target_id=self.target_id,
                        account_id=info.account_id,
                    )
            if sent_to_app and ok:
                title, body = _msg_sent_to_app(info.network, info.account_label)
                if late is not None:
                    body += (
                        f" Va con retraso: estaba programada para el {format_dt_es(late[0], info.brand_tz)} "
                        f"({_late_reason_text(late[2])})."
                    )
                _notify(
                    s,
                    NotificationKind.INFO,
                    title,
                    body,
                    now=now,
                    target_id=self.target_id,
                    account_id=info.account_id,
                )

        _write(self.rt, write)
        return str(status)

    def _late_info(self, published_at: datetime) -> tuple[datetime, timedelta, str | None] | None:
        """(due time, delay, reason) when this target goes out late because the PC/app or the internet was
        not available (engine bookkeeping ``late``), else ``None``."""
        reason = self.engine_meta.get("late")
        if not reason:
            return None
        due_at = _parse_iso(self.engine_meta.get("due_at")) or self.info.scheduled_at
        if due_at is None:
            return None
        delay = published_at - due_at
        if delay <= LATE_NOTE_AFTER:
            return None
        return due_at, delay, str(reason)

    def _fail(self, exc: ProviderError, *, exhausted: bool = False, attempts: int | None = None) -> str:
        if self._early_handoff():
            return self._handoff_failed(exc)
        now = self._now()
        info = self.info
        message = exc.message
        if exhausted and attempts:
            message = f"{exc.message} (sin éxito tras {attempts} intentos)"
        self.engine_meta = {}
        updates = self._take_account_updates()
        persisted = merge_state(self.state, self.engine_meta)

        def write(s: Session) -> None:
            self._apply_account_updates(s, updates)
            values: dict[str, Any] = {
                "status": TargetStatus.ERROR,
                "provider_state": persisted,
                "commit_phase": self.commit_phase,
                "next_attempt_at": None,
                "claimed_at": None,
                "last_error": message,
                "last_error_detail": exc.to_dict(),
                "updated_at": now,
            }
            if attempts is not None:
                values["attempts"] = attempts
            if _guarded_update(s, self.target_id, values, status_in=TargetStatus.PUBLISHING):
                add_event(
                    s, self.target_id, now, "error", message, level=EventLevel.ERROR, data=exc.to_dict()
                )
                title, body = _msg_error(info.network, info.account_label, message)
                _notify(
                    s,
                    NotificationKind.ERROR,
                    title,
                    body,
                    now=now,
                    target_id=self.target_id,
                    account_id=info.account_id,
                )

        _write(self.rt, write)
        return "error"

    def _retry(self, exc: ProviderError) -> str:
        attempts = self.info.attempts + 1
        if attempts > MAX_RETRIES:
            return self._fail(exc, exhausted=True, attempts=attempts)
        now = self._now()
        delay = retry_delay(attempts, exc.retry_after, self.rt.rng)
        next_at = now + delay
        self.engine_meta.pop("pending", None)
        updates = self._take_account_updates()
        persisted = merge_state(self.state, self.engine_meta)
        when = format_time_es(next_at, self.info.brand_tz)
        message = f"{exc.message} Reintento {attempts} de {MAX_RETRIES} a las {when}."

        def write(s: Session) -> None:
            self._apply_account_updates(s, updates)
            if _guarded_update(
                s,
                self.target_id,
                {
                    "provider_state": persisted,
                    "commit_phase": self.commit_phase,
                    "attempts": attempts,
                    "next_attempt_at": next_at,
                    "claimed_at": None,
                    "last_error": exc.message,
                    "last_error_detail": exc.to_dict(),
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            ):
                add_event(
                    s,
                    self.target_id,
                    now,
                    "retry_scheduled",
                    message,
                    level=EventLevel.WARNING,
                    data={
                        "attempt": attempts,
                        "delay_seconds": int(delay.total_seconds()),
                        "error": exc.to_dict(),
                    },
                )

        _write(self.rt, write)
        self.info.attempts = attempts
        return "retry"

    def _auth_error(self, exc: ProviderError) -> str:
        if self._early_handoff():
            return self._handoff_failed(exc)
        now = self._now()
        info = self.info
        self.engine_meta = {}
        persisted = merge_state(self.state, self.engine_meta)

        def write(s: Session) -> None:
            if not _guarded_update(
                s,
                self.target_id,
                {
                    "status": TargetStatus.ERROR,
                    "provider_state": persisted,
                    "commit_phase": self.commit_phase,
                    "next_attempt_at": None,
                    "claimed_at": None,
                    "last_error": exc.message,
                    "last_error_detail": exc.to_dict(),
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            ):
                return
            add_event(
                s, self.target_id, now, "auth_error", exc.message, level=EventLevel.ERROR, data=exc.to_dict()
            )
            changed = mark_account_needs_reconnect(
                s, info.account_id, exc.message, now, target_id=self.target_id
            )
            if not changed:  # account already flagged: still tell the user this post failed
                title, body = _msg_error(info.network, info.account_label, exc.message)
                _notify(
                    s,
                    NotificationKind.ERROR,
                    title,
                    body,
                    now=now,
                    target_id=self.target_id,
                    account_id=info.account_id,
                )

        _write(self.rt, write)
        return "error"

    def _early_handoff(self) -> bool:
        """This run hands the post to the network's own scheduler (Facebook/YouTube ``native_schedule``) ahead
        of its time, and nothing irreversible has been sent yet."""
        info = self.info
        return (
            self.commit_phase != CommitPhase.COMMIT_SENT
            and info.scheduled_at is not None
            and info.scheduled_at > self._now()
            and wants_native_schedule(info.network, info.options, info.format)
        )

    def _handoff_failed(self, exc: ProviderError) -> str:
        """An early native hand-off failed before its commit. The post is not due yet, so this is no reason to
        give up on it (an ``error`` target is never picked up again): back to ``scheduled``. Transient and
        account problems try the hand-off again later; anything else waits for the scheduled time, when the
        app publishes it itself (and only then reports an error if it still fails)."""
        now = self._now()
        info = self.info
        label = network_label(info.network)
        scheduled_at = info.scheduled_at
        assert scheduled_at is not None
        retry_later = exc.kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED, ErrorKind.AUTH)
        next_at = min(scheduled_at, now + NATIVE_HANDOFF_RETRY) if retry_later else scheduled_at
        if exc.kind == ErrorKind.AUTH:
            # The account needs reconnecting (``_process_native`` skips it until then).
            auth_message = exc.message
            account_id = info.account_id
            try:
                _write(
                    self.rt,
                    lambda s: mark_account_needs_reconnect(
                        s, account_id, auth_message, now, target_id=self.target_id
                    ),
                )
            except Exception:
                log.warning("Could not flag account %s", account_id, exc_info=True)
        first_report = self.engine_meta.get("handoff_error") != exc.code
        self.engine_meta = {"handoff_error": exc.code}
        updates = self._take_account_updates()
        persisted = merge_state({}, self.engine_meta)
        when = format_dt_es(scheduled_at, info.brand_tz)
        message = f"No se pudo dejar programada en {label}: {exc.message}"
        if next_at < scheduled_at:
            message += (
                f" Se volverá a intentar a las {format_time_es(next_at, info.brand_tz)}; si no se consigue, "
                f"la app la publicará a su hora ({when}) si el ordenador está encendido."
            )
        else:
            message += f" La app intentará publicarla a su hora ({when}) si el ordenador está encendido."

        def write(s: Session) -> None:
            self._apply_account_updates(s, updates)
            if not _guarded_update(
                s,
                self.target_id,
                {
                    "status": TargetStatus.SCHEDULED,
                    "provider_state": persisted,
                    "commit_phase": CommitPhase.NONE,
                    "attempts": 0,
                    "next_attempt_at": next_at,
                    "claimed_at": None,
                    "last_error": message,
                    "last_error_detail": exc.to_dict(),
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            ):
                return
            if not first_report:
                return
            add_event(
                s,
                self.target_id,
                now,
                "handoff_failed",
                message,
                level=EventLevel.WARNING,
                data=exc.to_dict(),
            )
            if not retry_later:
                _notify(
                    s,
                    NotificationKind.ERROR,
                    f"No se pudo programar en {label}",
                    f"{info.account_label}: {message}",
                    now=now,
                    target_id=self.target_id,
                    account_id=info.account_id,
                )

        _write(self.rt, write)
        return "handoff_failed"

    def _cancel_trashed(self) -> str:
        now = self._now()

        def write(s: Session) -> None:
            if _guarded_update(
                s,
                self.target_id,
                {
                    "status": TargetStatus.CANCELLED,
                    "next_attempt_at": None,
                    "claimed_at": None,
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            ):
                add_event(
                    s,
                    self.target_id,
                    now,
                    "cancelled",
                    "La publicación está en la papelera: se cancela el envío.",
                )

        _write(self.rt, write)
        return "cancelled"

    def _account_unavailable(self) -> str:
        info = self.info
        message = (
            f"La cuenta {info.account_label} necesita reconexión. Reconéctala en Cuentas conectadas y "
            "vuelve a intentarlo."
        )
        if info.account_status == AccountStatus.DISCONNECTED:
            message = f"La cuenta {info.account_label} está desconectada. Conéctala de nuevo y reintenta."
        return self._fail(ProviderError(ErrorKind.AUTH, "account_not_connected", message))

    def _unknown(self, message: str | None, *, detail: dict | None = None) -> str:
        now = self._now()
        info = self.info
        text = message or "No se pudo confirmar si la publicación salió. Revísalo en la red."
        self.engine_meta = {}
        updates = self._take_account_updates()
        persisted = merge_state(self.state, self.engine_meta)

        def write(s: Session) -> None:
            self._apply_account_updates(s, updates)
            if _guarded_update(
                s,
                self.target_id,
                {
                    "status": TargetStatus.UNKNOWN,
                    "provider_state": persisted,
                    "commit_phase": self.commit_phase,
                    "next_attempt_at": None,
                    "claimed_at": None,
                    "last_error": text,
                    "last_error_detail": detail,
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            ):
                add_event(s, self.target_id, now, "unknown", text, level=EventLevel.ERROR, data=detail)
                title, body = _msg_unknown(info.network, info.account_label)
                _notify(
                    s,
                    NotificationKind.UNKNOWN,
                    title,
                    body,
                    now=now,
                    target_id=self.target_id,
                    account_id=info.account_id,
                )

        _write(self.rt, write)
        return "unknown"

    # ---- error handling

    def _on_provider_error(self, exc: ProviderError, ctx: PublishContext) -> str:
        committed = self.commit_phase == CommitPhase.COMMIT_SENT
        if self._offline_instead(exc):
            return self._offline_wait(exc)
        if exc.kind == ErrorKind.AMBIGUOUS or (
            committed and exc.kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED)
        ):
            self._log_safe("ambiguous_error", exc.message, exc.to_dict(), EventLevel.WARNING)
            return self._reconcile(ctx, cause=exc)
        if exc.kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED):
            return self._retry(exc)
        if exc.kind == ErrorKind.AUTH:
            return self._auth_error(exc)
        return self._fail(exc)

    def _on_exception(self, exc: Exception, ctx: PublishContext) -> str:
        if self._offline_instead(exc):
            log.info("No internet while publishing target %s (%s)", self.target_id, type(exc).__name__)
            return self._offline_wait(
                ProviderError(
                    ErrorKind.TRANSIENT,
                    "network_unreachable",
                    "Sin conexión a internet.",
                    detail={"exception": type(exc).__name__, "text": scrub_text(str(exc))[:300]},
                )
            )
        log.error("Unexpected error publishing target %s", self.target_id, exc_info=exc)
        err = ProviderError(
            ErrorKind.TRANSIENT,
            "unexpected_error",
            "Error inesperado al publicar.",
            detail={"exception": type(exc).__name__, "text": str(exc)[:300]},
        )
        if self.commit_phase == CommitPhase.COMMIT_SENT:
            self._log_safe("ambiguous_error", err.message, err.to_dict(), EventLevel.WARNING)
            return self._reconcile(ctx, cause=err)
        return self._retry(err)

    def _log_safe(self, event: str, message: str, data: dict | None, level: str) -> None:
        now = self._now()
        try:
            _write(
                self.rt, lambda s: add_event(s, self.target_id, now, event, message, level=level, data=data)
            )
        except Exception:
            log.debug("Could not log %s", event, exc_info=True)

    # ---- reconcile

    def _reconcile(self, ctx: PublishContext, cause: ProviderError | None) -> str:
        try:
            result: ReconcileResult = self.provider.reconcile(ctx)
        except ProviderError as exc:
            if exc.kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED):
                return self._reconcile_later(exc)
            if exc.kind == ErrorKind.AUTH:
                now = self._now()
                auth_message = exc.message
                account_id = self.info.account_id
                _write(
                    self.rt,
                    lambda s: mark_account_needs_reconnect(
                        s, account_id, auth_message, now, target_id=self.target_id
                    ),
                )
            return self._unknown(
                f"No se pudo comprobar si la publicación salió: {exc.message}", detail=exc.to_dict()
            )
        except Exception as exc:
            log.error("Reconcile crashed for target %s", self.target_id, exc_info=exc)
            return self._unknown(None, detail={"exception": type(exc).__name__})

        outcome = getattr(result, "outcome", "unknown")
        if outcome == "published":
            return self._finish(
                StepResult(
                    "published",
                    external_id=result.external_id,
                    external_url=result.external_url,
                    message=result.message or "Confirmado: la publicación salió correctamente.",
                ),
                reconciled=True,
            )
        if outcome == "not_published":
            self.commit_phase = CommitPhase.NONE
            self.engine_meta = {k: v for k, v in self.engine_meta.items() if k in ("due_at", "late")}
            self._log_safe(
                "reconciled_not_published",
                result.message or "La red confirma que no se publicó; se puede volver a enviar.",
                None,
                EventLevel.INFO,
            )
            # Nothing went out. When the irreversible request was the run's first network call (Facebook
            # text/link/photo, TikTok inbox init) or an upload broke off, a connection failure lands here:
            # with no internet, wait for it instead of spending the retries.
            if cause is not None and self._offline_instead(cause):
                return self._offline_wait(cause)
            return self._retry(
                cause
                or ProviderError(
                    ErrorKind.TRANSIENT,
                    "not_published",
                    "La red indica que la publicación no salió.",
                )
            )
        return self._unknown(result.message)

    def _reconcile_later(self, exc: ProviderError) -> str:
        count = int(self.engine_meta.get("reconcile_attempts", 0)) + 1
        if count > MAX_RETRIES:
            return self._unknown(
                f"No se pudo comprobar si la publicación salió: {exc.message}", detail=exc.to_dict()
            )
        now = self._now()
        delay = retry_delay(count, exc.retry_after, self.rt.rng)
        next_at = now + delay
        self.engine_meta = {"pending": "reconcile", "reconcile_attempts": count}
        persisted = merge_state(self.state, self.engine_meta)

        def write(s: Session) -> None:
            if _guarded_update(
                s,
                self.target_id,
                {
                    "provider_state": persisted,
                    "commit_phase": self.commit_phase,
                    "next_attempt_at": next_at,
                    "claimed_at": None,
                    "last_error": exc.message,
                    "last_error_detail": exc.to_dict(),
                    "updated_at": now,
                },
                status_in=TargetStatus.PUBLISHING,
            ):
                add_event(
                    s,
                    self.target_id,
                    now,
                    "reconcile_retry",
                    f"No se pudo comprobar todavía si se publicó ({exc.message}). Se volverá a comprobar a "
                    f"las {format_time_es(next_at, self.info.brand_tz)}.",
                    level=EventLevel.WARNING,
                    data={"attempt": count},
                )

        _write(self.rt, write)
        return "reconcile_retry"


def run_target(target_id: int, runtime: EngineRuntime | None = None, *, reconcile: bool = False) -> str:
    """Run one claimed (``publishing``) target until it waits, finishes or fails. Returns the outcome
    (see :data:`OUTCOMES`). Never raises for provider problems; database errors propagate (the claim then
    goes stale and is recovered later)."""
    rt = runtime or default_runtime()
    with _active_lock:
        if target_id in _active_runs:
            log.warning("Target %s is already running in this process", target_id)
            return "skipped"
        _active_runs.add(target_id)
    own_http = rt.http is None
    http = rt.http or httpx.Client(timeout=HTTP_TIMEOUT)
    try:
        with keepawake.keep_awake():
            return _Runner(rt, target_id, http).run(reconcile=reconcile)
    finally:
        if own_http:
            http.close()
        with _active_lock:
            _active_runs.discard(target_id)


# ------------------------------------------------------------------------------- route-facing API


def _get_target(db: Session, target_id: int) -> PostTarget:
    # populate_existing: the caller's session may hold an older copy (the scheduler changes rows meanwhile).
    target = db.get(PostTarget, target_id, populate_existing=True)
    if target is None:
        raise EngineError("La publicación no existe.", code="not_found")
    return target


def _reset_values(target: PostTarget, now: datetime, *, keep_reconcile: bool) -> dict[str, Any]:
    """Values that put a target back into ``scheduled`` for the engine to pick it up now."""
    values: dict[str, Any] = {
        "status": TargetStatus.SCHEDULED,
        "attempts": 0,
        "next_attempt_at": now,
        "claimed_at": None,
        "last_error": None,
        "last_error_detail": None,
        "updated_at": now,
    }
    if keep_reconcile and target.commit_phase == CommitPhase.COMMIT_SENT:
        # The previous commit may have gone out: check before ever sending again.
        state, _meta = split_state(target.provider_state)
        values["provider_state"] = merge_state(state, {"pending": "reconcile"})
    else:
        values["provider_state"] = {}
        values["commit_phase"] = CommitPhase.NONE
    return values


def reset_for_schedule(target: PostTarget) -> None:
    """Prepare a target (ORM object; the caller commits) to be scheduled again after an edit or reschedule,
    e.g. by ``postsvc`` when an ``error``/``missed``/``draft`` target gets a new date. Clears attempts, errors
    and the provider's partial state — unless a commit may already have gone out, in which case the engine
    will reconcile before ever sending again. Refuses targets the engine is working on."""
    if target.status == TargetStatus.PUBLISHING:
        raise EngineError(
            "La publicación se está enviando en este momento; espera a que termine.", code="busy"
        )
    target.status = TargetStatus.SCHEDULED
    target.attempts = 0
    target.next_attempt_at = None
    target.claimed_at = None
    target.last_error = None
    target.last_error_detail = None
    if target.commit_phase == CommitPhase.COMMIT_SENT:
        state, meta = split_state(target.provider_state)
        target.provider_state = merge_state(state, {**meta, "pending": "reconcile"})
    else:
        target.provider_state = {}
        target.commit_phase = CommitPhase.NONE


def publish_now(db: Session, post_id: int, clock: Clock) -> None:
    """ "Publicar ahora": every pending target of the post goes to ``scheduled`` at ``now`` and the scheduler
    is woken. Targets with auto-publish off become ``manual_pending`` on the next tick (assisted flow)."""
    post = db.get(Post, post_id, populate_existing=True)
    if post is None or post.deleted_at is not None:
        raise EngineError("La publicación no existe o está en la papelera.", code="not_found")
    now = clock.now()
    targets = db.scalars(
        select(PostTarget)
        .where(PostTarget.post_id == post.id)
        .order_by(PostTarget.id)
        .execution_options(populate_existing=True)
    ).all()
    eligible = [t for t in targets if t.status in PUBLISH_NOW_FROM]
    if not eligible:
        raise EngineError(
            "No hay redes pendientes de publicar en esta publicación.", code="nothing_to_publish"
        )
    post.scheduled_at = now
    if post.kind == PostKind.IDEA:
        post.kind = PostKind.SCHEDULED
        post.idea_stage = None
    changed: list[PostTarget] = []
    for target in eligible:
        if target.status in (TargetStatus.ERROR, TargetStatus.MISSED):
            values = _reset_values(target, now, keep_reconcile=True)
        else:
            # draft/scheduled: nothing was sent; keep the provider state (it may carry a pending reconcile).
            values = {
                "status": TargetStatus.SCHEDULED,
                "attempts": 0,
                "claimed_at": None,
                "last_error": None,
                "last_error_detail": None,
                "updated_at": now,
            }
            if target.commit_phase == CommitPhase.COMMIT_SENT:
                state, meta = split_state(target.provider_state)
                values["provider_state"] = merge_state(state, {**meta, "pending": "reconcile"})
        values["scheduled_at"] = now
        values["next_attempt_at"] = None
        if _guarded_update(db, target.id, values, status_in=target.status):
            add_event(db, target.id, now, "publish_now", "Publicar ahora: se envía en este momento.")
            _resolve_notifications(db, target.id, now)
            changed.append(target)
    db.commit()
    for target in changed:
        _refresh_object(db, target)
    if changed:
        _wake_scheduler()


def retry_target(db: Session, target_id: int, clock: Clock, *, confirmed_not_published: bool = False) -> None:
    """ "Reintentar": ``error``/``missed`` → ``scheduled`` now; ``unknown`` only with
    ``confirmed_not_published=True`` (the user checked that it did NOT go out)."""
    target = _get_target(db, target_id)
    if target.post is not None and target.post.deleted_at is not None:
        raise EngineError(
            "La publicación está en la papelera; restáurala antes de reintentar.", code="deleted"
        )
    if target.status not in RETRY_FROM:
        raise EngineError(
            "Solo se pueden reintentar publicaciones con error, perdidas o sin confirmar.",
            code="invalid_state",
        )
    if target.status == TargetStatus.UNKNOWN and not confirmed_not_published:
        raise EngineError(
            "Comprueba en la red que no se publicó y confírmalo antes de reintentar.", code="confirm_required"
        )
    now = clock.now()
    values = _reset_values(target, now, keep_reconcile=not confirmed_not_published)
    previous = target.status
    if not _guarded_update(db, target.id, values, status_in=previous):
        db.rollback()
        raise EngineError("La publicación ha cambiado de estado; recarga la página.", code="conflict")
    if confirmed_not_published:
        message = "Reintento solicitado: confirmaste que no se había publicado."
    else:
        message = "Reintento solicitado."
    add_event(db, target.id, now, "retry_requested", message, data={"from": str(previous)})
    _resolve_notifications(db, target.id, now)
    db.commit()
    _refresh_object(db, target)
    _wake_scheduler()


def cancel_target(db: Session, target_id: int, clock: Clock, *, force: bool = False) -> None:
    """ "Cancelar" → ``cancelled``. A Facebook/YouTube post handed to the network's own scheduler
    (``native_scheduled``) is first deleted there. When that fails the post is still going to go live, so it
    is **not** cancelled: the target stays ``published`` (natively scheduled) and :class:`EngineError`
    (code ``native_cancel_failed``) says why and what to do — unless ``force=True`` (the user confirmed they
    deleted it on the network themselves), which cancels it here anyway with a warning event."""
    target = _get_target(db, target_id)
    now = clock.now()
    status = target.status
    if status == TargetStatus.CANCELLED:
        return
    extra: tuple[Any, ...] = ()
    native_note: str | None = None
    if status == TargetStatus.PUBLISHED:
        state, _meta = split_state(target.provider_state)
        pending_native = state.get("native_scheduled") and (
            target.scheduled_at is None or target.scheduled_at > now
        )
        if not pending_native:
            raise EngineError(
                "Esta publicación ya se ha publicado; bórrala directamente en la red si quieres retirarla.",
                code="already_published",
            )
        failure = _cancel_native(db, target, clock)
        if failure is not None:
            label = network_label(target.network)
            if force:
                native_note = (
                    f"Cancelada solo en la app: no se pudo retirar de la programación de {label}{failure}. "
                    f"Comprueba que la has borrado en {label}."
                )
            else:
                tz = target.post.brand.timezone if target.post is not None and target.post.brand else None
                when = f" para el {format_dt_es(target.scheduled_at, tz)}" if target.scheduled_at else ""
                message = (
                    f"No se ha podido retirar la publicación de la programación de {label}{failure}. Sigue "
                    f"programada allí{when}: vuelve a intentarlo o bórrala directamente en {label}."
                )
                db.rollback()  # nothing of the attempt is kept but the event below
                add_event(db, target_id, now, "native_cancel_failed", message, level=EventLevel.WARNING)
                db.commit()
                raise EngineError(message, code="native_cancel_failed")
    elif status == TargetStatus.PUBLISHING:
        if target.next_attempt_at is None or target.commit_phase == CommitPhase.COMMIT_SENT:
            raise EngineError(
                "La publicación se está enviando en este momento; espera a que termine.", code="busy"
            )
        extra = (PostTarget.next_attempt_at.is_not(None), PostTarget.commit_phase != CommitPhase.COMMIT_SENT)
    elif status not in CANCEL_FROM:
        raise EngineError("Esta publicación ya no se puede cancelar.", code="invalid_state")

    values = {
        "status": TargetStatus.CANCELLED,
        "next_attempt_at": None,
        "claimed_at": None,
        "updated_at": now,
    }
    if not _guarded_update(db, target.id, values, status_in=status, extra=extra):
        db.rollback()
        raise EngineError("La publicación ha cambiado de estado; recarga la página.", code="conflict")
    add_event(db, target.id, now, "cancelled", "Publicación cancelada.", data={"from": str(status)})
    if native_note:
        add_event(db, target.id, now, "native_cancel", native_note, level=EventLevel.WARNING)
    _resolve_notifications(db, target.id, now)
    db.commit()
    _refresh_object(db, target)


def _cancel_native(db: Session, target: PostTarget, clock: Clock) -> str | None:
    """Ask the provider to delete a natively scheduled post. Returns ``None`` when it was withdrawn, else why
    it could not be, as a parenthesised fragment (``" (Sin conexión)"``) for the caller's message."""
    from planificador.providers import registry

    label = network_label(target.network)
    unsupported = " (la app no puede retirarla automáticamente)"
    try:
        provider = registry.get_provider(str(target.network))
    except Exception:
        return unsupported
    cancel = getattr(provider, "cancel_native", None)
    delete_post = getattr(provider, "delete_post", None)
    if cancel is None and delete_post is None:
        return unsupported
    rt = default_runtime(clock)
    own_http = rt.http is None
    http = rt.http or httpx.Client(timeout=HTTP_TIMEOUT)
    state, _meta = split_state(target.provider_state)
    media, _missing = load_media(db, target, strict=False)
    account = target.account

    def save_state(new_state: dict) -> None:
        target.provider_state = merge_state(new_state, {})

    try:
        ctx = PublishContext(
            account=account.to_creds(),
            target=TargetSpec(
                network=str(target.network),
                format=target.format,
                text=target_text(target),
                media=media,
                options=dict(target.options or {}),
                scheduled_at=target.scheduled_at,
                auto_publish=bool(target.auto_publish),
            ),
            target_id=target.id,
            state=state,
            settings=config.provider_settings(db, base_url=rt.base_url),
            http=http,
            clock=clock,
            public_media=rt.get_public_media(),
            save_state=save_state,
            before_commit=save_state,
            log=lambda event, message, data=None: add_event(
                db, target.id, clock.now(), event, message, data=data
            ),
        )
        if cancel is not None:
            cancel(ctx)
        elif not target.external_id:
            return unsupported
        elif not delete_post(ctx.account, ctx.settings, http, target.external_id):
            return f" ({label} no permitió borrarla)"
    except ProviderError as exc:
        log.warning("cancel_native failed for target %s: %s", target.id, exc.code)
        return f" ({exc.message.rstrip('.')})"
    except Exception:
        log.exception("cancel_native crashed for target %s", target.id)
        return " (error inesperado)"
    finally:
        if own_http:
            http.close()
    add_event(
        db, target.id, clock.now(), "native_cancelled", f"Retirada también de la programación de {label}."
    )
    return None


def mark_done_manually(db: Session, target_id: int, clock: Clock, external_url: str | None = None) -> None:
    """ "Marcar como publicado": ``manual_pending``/``unknown``/``error``/``missed``/``sent_to_app`` →
    ``manual_done`` (optionally with the post's URL)."""
    target = _get_target(db, target_id)
    if target.status == TargetStatus.MANUAL_DONE and not external_url:
        return
    if target.status not in MARK_DONE_FROM and target.status != TargetStatus.MANUAL_DONE:
        raise EngineError("Esta publicación no se puede marcar como publicada.", code="invalid_state")
    url = (external_url or "").strip() or None
    if url is not None:
        if not url.lower().startswith(("https://", "http://")) or len(url) > 1024:
            raise EngineError(
                "El enlace debe ser una dirección web completa (https://…).", code="invalid_url"
            )
    now = clock.now()
    previous = target.status
    values: dict[str, Any] = {
        "status": TargetStatus.MANUAL_DONE,
        "next_attempt_at": None,
        "claimed_at": None,
        "last_error": None,
        "updated_at": now,
    }
    if previous != TargetStatus.MANUAL_DONE:
        values["published_at"] = now
    if url is not None:
        values["external_url"] = url
    if not _guarded_update(db, target.id, values, status_in=previous):
        db.rollback()
        raise EngineError("La publicación ha cambiado de estado; recarga la página.", code="conflict")
    add_event(
        db,
        target.id,
        now,
        "manual_done",
        "Marcada como publicada a mano." if previous != TargetStatus.MANUAL_DONE else "Enlace actualizado.",
        data={"from": str(previous), "external_url": url},
    )
    _resolve_notifications(db, target.id, now)
    db.commit()
    _refresh_object(db, target)


def _resolve_notifications(db: Session, target_id: int, now: datetime) -> None:
    try:
        from planificador import notify as notify_mod

        notify_mod.resolve_for_target(db, target_id, now=now, kinds=_RESOLVABLE_KINDS)
    except Exception:  # pragma: no cover
        log.debug("Could not resolve notifications for target %s", target_id, exc_info=True)


def due_reference(target: PostTarget) -> datetime | None:
    """The instant a ``scheduled`` target became due (an explicit retry time wins over the original)."""
    if target.scheduled_at is None:
        return target.next_attempt_at
    if target.next_attempt_at is not None and target.next_attempt_at > target.scheduled_at:
        return target.next_attempt_at
    return target.scheduled_at


def due_reference_sql() -> Any:
    """SQL twin of :func:`due_reference` for due targets (``scheduled_at`` set)."""
    return case(
        (PostTarget.next_attempt_at > PostTarget.scheduled_at, PostTarget.next_attempt_at),
        else_=PostTarget.scheduled_at,
    )


def due_filter(now: datetime) -> Any:
    """SQL condition for targets that are due (status scheduled, time reached, post not in the trash)."""
    return (
        (PostTarget.status == TargetStatus.SCHEDULED)
        & (PostTarget.scheduled_at <= now)
        & or_(PostTarget.next_attempt_at.is_(None), PostTarget.next_attempt_at <= now)
        & PostTarget.post.has(Post.deleted_at.is_(None))
    )


__all__ = [
    "CANCEL_FROM",
    "MARK_DONE_FROM",
    "MAX_RETRIES",
    "PUBLISH_NOW_FROM",
    "RETRY_DELAYS_S",
    "RETRY_FROM",
    "EngineError",
    "EngineRuntime",
    "active_runs",
    "cancel_target",
    "claim_due",
    "claim_stale",
    "claim_waiting",
    "default_runtime",
    "due_filter",
    "due_reference",
    "due_reference_sql",
    "end_offline_wait",
    "is_queued_after_outage",
    "settle_outage",
    "is_tiktok_inbox",
    "wants_native_schedule",
    "mark_account_needs_reconnect",
    "mark_done_manually",
    "publish_now",
    "reset_for_schedule",
    "retry_delay",
    "retry_target",
    "run_target",
    "set_manual_pending",
    "set_missed",
]

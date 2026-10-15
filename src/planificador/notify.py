"""Notifications: DB rows + desktop notifications (SPEC §11).

:func:`notify` inserts a :class:`~planificador.models.Notification` row (what the bell and the notification
centre show) and, unless disabled, queues a desktop notification (``desktop-notifier``) that is sent from a
background thread **after the row's transaction commits** (a rolled-back notification is never shown).
Desktop delivery never raises: any failure (no notification server, missing backend, timeout) is logged and
ignored.

Desktop notifications are skipped when:

- ``PLANIFICADOR_NO_DESKTOP_NOTIFY=1`` (tests),
- the user setting ``notifications_desktop`` is off,
- ``desktop=False`` is passed.

Bursts are coalesced: when more than :data:`MAX_INDIVIDUAL` notifications arrive within
:data:`BATCH_WINDOW_S` seconds (e.g. several posts marked "missed" at start-up) a single summary toast is
shown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import webbrowser
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import event, update
from sqlalchemy.orm import Session

from planificador.models import Notification, NotificationKind, utcnow

log = logging.getLogger(__name__)

ENV_NO_DESKTOP = "PLANIFICADOR_NO_DESKTOP_NOTIFY"
APP_NAME = "Planificador"
SETTING_DESKTOP = "notifications_desktop"
BATCH_WINDOW_S = 0.8
MAX_INDIVIDUAL = 3
SEND_TIMEOUT_S = 10.0
TITLE_MAX = 255
DESKTOP_BODY_MAX = 240

_PENDING_KEY = "_planificador_desktop_pending"
_HOOKED_KEY = "_planificador_desktop_hooked"

_TRUE = {"1", "true", "yes", "si", "sí", "on"}

#: ``async sender(title, body, url) -> None``; raising means "not delivered".
Sender = Callable[[str, str, str | None], Awaitable[None]]


# ------------------------------------------------------------------------------------------- public API


def notify(
    db: Session,
    kind: str,
    title: str,
    body: str,
    *,
    target_id: int | None = None,
    account_id: int | None = None,
    desktop: bool = True,
    commit: bool = True,
    now: datetime | None = None,
) -> Notification:
    """Create a notification row (+ desktop notification after commit).

    ``kind``: ``manual_publish`` | ``error`` | ``reconnect`` | ``unknown`` | ``missed`` | ``info`` (other
    strings are stored as-is). ``commit=False`` only flushes, so the caller can make it part of a larger
    transaction; the desktop notification then goes out when that transaction commits. ``now`` overrides
    ``created_at`` (the engine passes its clock).
    """
    notification = Notification(
        kind=kind,
        title=(title or "")[:TITLE_MAX],
        body=body or "",
        target_id=target_id,
        account_id=account_id,
    )
    if now is not None:
        notification.created_at = now
    db.add(notification)
    db.flush()
    if desktop and desktop_enabled(db):
        _queue_after_commit(
            db,
            DesktopItem(
                notification_id=notification.id,
                title=notification.title,
                body=_desktop_body(body),
                url=_deep_link(),
            ),
        )
    if commit:
        db.commit()
    return notification


def resolve_for_target(
    db: Session,
    target_id: int,
    *,
    now: datetime | None = None,
    kinds: Iterable[str] | None = None,
) -> int:
    """Mark the unread notifications of ``target_id`` as read (the situation they announced is resolved, e.g.
    a "Publicación asistida" reminder once the user marks the target as published). Does not commit."""
    stmt = (
        update(Notification)
        .where(Notification.target_id == target_id, Notification.read_at.is_(None))
        .values(read_at=now or utcnow())
        .execution_options(synchronize_session=False)
    )
    if kinds is not None:
        stmt = stmt.where(Notification.kind.in_([str(k) for k in kinds]))
    return int(db.execute(stmt).rowcount or 0)


def desktop_enabled(db: Session | None = None) -> bool:
    """Whether desktop notifications should be sent (env switch + user setting)."""
    if os.environ.get(ENV_NO_DESKTOP, "").strip().lower() in _TRUE:
        return False
    if db is None:
        return True
    try:
        from planificador import config

        return config.get_bool(db, SETTING_DESKTOP, True)
    except Exception:  # pragma: no cover - settings table unavailable
        log.debug("Could not read %s; assuming enabled", SETTING_DESKTOP, exc_info=True)
        return True


def shutdown(timeout: float = 2.0) -> None:
    """Stop the desktop notification thread (best effort; it is a daemon thread anyway)."""
    _dispatcher.shutdown(timeout)


def get_dispatcher() -> DesktopDispatcher:
    return _dispatcher


def set_dispatcher(dispatcher: DesktopDispatcher) -> DesktopDispatcher:
    """Replace the module dispatcher (tests). Returns the previous one."""
    global _dispatcher
    previous = _dispatcher
    _dispatcher = dispatcher
    return previous


# --------------------------------------------------------------------------------------- after commit


@dataclass(frozen=True)
class DesktopItem:
    notification_id: int | None
    title: str
    body: str
    url: str | None


def _queue_after_commit(db: Session, item: DesktopItem) -> None:
    pending: list[DesktopItem] = db.info.setdefault(_PENDING_KEY, [])
    pending.append(item)
    if not db.info.get(_HOOKED_KEY):
        event.listen(db, "after_commit", _after_commit)
        event.listen(db, "after_rollback", _after_rollback)
        db.info[_HOOKED_KEY] = True


def _after_commit(session: Session) -> None:
    items: list[DesktopItem] = session.info.pop(_PENDING_KEY, None) or []
    if not items:
        return
    try:
        bind = session.get_bind()
    except Exception:  # pragma: no cover - unbound session
        bind = None
    try:
        _dispatcher.submit(items, bind)
    except Exception:  # pragma: no cover - never let a toast break a commit
        log.debug("Desktop notification dispatch failed", exc_info=True)


def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


def _desktop_body(body: str | None) -> str:
    text = " ".join((body or "").split())
    if len(text) > DESKTOP_BODY_MAX:
        text = text[: DESKTOP_BODY_MAX - 1].rstrip() + "…"
    return text


def _deep_link() -> str | None:
    try:
        from planificador import config

        return f"{config.get_app_settings().base_url}/notificaciones"
    except Exception:  # pragma: no cover
        return None


# ------------------------------------------------------------------------------------------ dispatcher


class DesktopDispatcher:
    """Owns a daemon thread running an asyncio loop that delivers desktop notifications.

    ``sender`` (async ``(title, body, url)``) defaults to ``desktop-notifier``. The loop stays alive so that
    click callbacks (open the notification centre in the browser) keep working where the backend supports
    them.
    """

    def __init__(self, sender: Sender | None = None, *, batch_window: float = BATCH_WINDOW_S) -> None:
        self._sender = sender
        self.batch_window = batch_window
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._notifier: Any = None
        self._warned = False
        self.delivered: int = 0  # number of toasts handed to the backend (diagnostics/tests)

    # ---- producer side (any thread)

    def submit(self, items: list[DesktopItem], bind: Any = None) -> None:
        if not items:
            return
        loop = self._ensure_loop()
        queue = self._queue
        if loop is None or queue is None:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (list(items), bind))
        except RuntimeError:  # loop closed between the check and the call
            log.debug("Desktop notification loop closed; dropping %d item(s)", len(items))

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._lock:
            loop, thread, queue = self._loop, self._thread, self._queue
            self._loop = self._thread = self._queue = None
        if loop is None or thread is None:
            return
        try:
            if queue is not None:
                loop.call_soon_threadsafe(queue.put_nowait, None)
        except RuntimeError:
            pass
        thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop | None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            holder: dict[str, asyncio.Queue[Any]] = {}

            def run() -> None:
                asyncio.set_event_loop(loop)
                queue: asyncio.Queue[Any] = asyncio.Queue()
                holder["queue"] = queue
                consumer = loop.create_task(self._consume(queue))
                ready.set()
                try:
                    loop.run_until_complete(consumer)
                except Exception:  # pragma: no cover
                    log.debug("Desktop notification loop crashed", exc_info=True)
                finally:
                    try:
                        loop.close()
                    except Exception:  # pragma: no cover
                        pass

            thread = threading.Thread(target=run, name="planificador-desktop-notify", daemon=True)
            thread.start()
            if not ready.wait(5):  # pragma: no cover - thread failed to start
                return None
            self._loop, self._thread, self._queue = loop, thread, holder["queue"]
            return loop

    # ---- consumer side (loop thread)

    async def _consume(self, queue: asyncio.Queue[Any]) -> None:
        while True:
            first = await queue.get()
            if first is None:
                return
            batch = [first]
            await asyncio.sleep(self.batch_window)
            stop = False
            while not queue.empty():
                nxt = queue.get_nowait()
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
            try:
                await self._deliver(batch)
            except Exception:  # pragma: no cover - defensive
                log.debug("Desktop notification delivery failed", exc_info=True)
            if stop:
                return

    async def _deliver(self, batch: list[tuple[list[DesktopItem], Any]]) -> None:
        items = [item for chunk, _bind in batch for item in chunk]
        bind = next((b for _chunk, b in batch if b is not None), None)
        if not items:
            return
        delivered_ids: list[int] = []
        if len(items) > MAX_INDIVIDUAL:
            titles = "; ".join(i.title for i in items[:MAX_INDIVIDUAL])
            body = _desktop_body(f"{titles}…")
            if await self._send_one(f"Tienes {len(items)} avisos nuevos", body, items[0].url):
                delivered_ids = [i.notification_id for i in items if i.notification_id is not None]
        else:
            for item in items:
                if await self._send_one(item.title, item.body, item.url) and item.notification_id:
                    delivered_ids.append(item.notification_id)
        if delivered_ids and bind is not None:
            _mark_sent(bind, delivered_ids)

    async def _send_one(self, title: str, body: str, url: str | None) -> bool:
        sender = self._sender or self._desktop_notifier_send
        try:
            await asyncio.wait_for(sender(title, body, url), SEND_TIMEOUT_S)
        except Exception as exc:
            if not self._warned:
                log.warning("Desktop notifications unavailable: %s", type(exc).__name__)
                self._warned = True
            else:
                log.debug("Desktop notification failed", exc_info=True)
            return False
        self.delivered += 1
        return True

    async def _desktop_notifier_send(self, title: str, body: str, url: str | None) -> None:
        if self._notifier is None:
            # desktop-notifier swallows backend failures and logs a traceback per toast; we detect failures
            # via ``on_dispatched`` and report them once ourselves.
            logging.getLogger("desktop_notifier").setLevel(logging.ERROR)
            from desktop_notifier import DesktopNotifier

            self._notifier = DesktopNotifier(app_name=APP_NAME)

        dispatched: list[bool] = []

        def on_clicked() -> None:
            if url:
                try:
                    webbrowser.open(url)
                except Exception:  # pragma: no cover
                    log.debug("Could not open %s", url, exc_info=True)

        await self._notifier.send(
            title=title,
            message=body,
            on_clicked=on_clicked if url else None,
            on_dispatched=lambda: dispatched.append(True),
        )
        if not dispatched:
            raise RuntimeError("the notification server did not accept the notification")


def _mark_sent(bind: Any, ids: list[int]) -> None:
    try:
        with Session(bind=bind) as session:
            session.execute(
                update(Notification)
                .where(Notification.id.in_(ids))
                .values(desktop_sent_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            session.commit()
    except Exception:  # database closed (shutdown/tests) or locked: not worth failing over
        log.debug("Could not record desktop_sent_at", exc_info=True)


_dispatcher = DesktopDispatcher()


__all__ = [
    "APP_NAME",
    "ENV_NO_DESKTOP",
    "DesktopDispatcher",
    "DesktopItem",
    "NotificationKind",
    "desktop_enabled",
    "get_dispatcher",
    "notify",
    "resolve_for_target",
    "set_dispatcher",
    "shutdown",
]

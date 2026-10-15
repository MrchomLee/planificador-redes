"""Notification tests (SPEC §11): DB rows, desktop dispatch only after commit, switches, batching, failure
tolerance and the default desktop-notifier sender (faked: no notification server in tests)."""

from __future__ import annotations

import asyncio
import sys
import time
import types
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import select

from planificador import config, notify
from planificador.models import Notification, Post, PostTarget, TargetStatus
from planificador.notify import DesktopDispatcher, DesktopItem


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[list[DesktopItem]] = []
        self.binds: list[object] = []

    def submit(self, items, bind=None) -> None:
        self.calls.append(list(items))
        self.binds.append(bind)

    def shutdown(self, timeout: float = 2.0) -> None:
        pass


@pytest.fixture
def recorder():
    rec = RecordingDispatcher()
    previous = notify.set_dispatcher(rec)  # type: ignore[arg-type]
    yield rec
    notify.set_dispatcher(previous)


@pytest.fixture
def desktop_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(notify.ENV_NO_DESKTOP, raising=False)


@pytest.fixture
def desktop_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.ENV_NO_DESKTOP, "1")


def make_target(db, account) -> PostTarget:
    post = Post(brand_id=account.brand_id, text="Hola")
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id, account_id=account.id, network=account.network, status=TargetStatus.ERROR
    )
    db.add(target)
    db.commit()
    return target


# --------------------------------------------------------------------------------------------- rows


def test_notify_creates_a_row(db, clock, desktop_off, demo_account):
    target = make_target(db, demo_account)
    created = notify.notify(
        db,
        "error",
        "No se pudo publicar en Demo",
        "Cuenta Demo: algo falló.",
        target_id=target.id,
        account_id=demo_account.id,
        now=clock.now(),
    )
    db.expire_all()
    row = db.get(Notification, created.id)
    assert row.kind == "error"
    assert row.title == "No se pudo publicar en Demo"
    assert row.body == "Cuenta Demo: algo falló."
    assert row.target_id == target.id and row.account_id == demo_account.id
    assert row.created_at == clock.now()
    assert row.read_at is None and row.desktop_sent_at is None


def test_unknown_kinds_are_stored_and_titles_truncated(db, desktop_off):
    created = notify.notify(db, "custom_kind", "x" * 400, "")
    db.expire_all()
    row = db.get(Notification, created.id)
    assert row.kind == "custom_kind"
    assert len(row.title) == notify.TITLE_MAX


def test_commit_false_only_flushes(db, desktop_off):
    created = notify.notify(db, "info", "Aviso", "Texto", commit=False)
    assert created.id is not None
    db.rollback()
    assert db.scalars(select(Notification)).all() == []


def test_resolve_for_target_marks_unread_as_read(db, clock, desktop_off, demo_account):
    target = make_target(db, demo_account)
    notify.notify(db, "error", "a", "b", target_id=target.id)
    notify.notify(db, "manual_publish", "c", "d", target_id=target.id)
    notify.notify(db, "info", "e", "f", target_id=target.id)
    notify.notify(db, "error", "g", "h")  # other target
    count = notify.resolve_for_target(db, target.id, now=clock.now(), kinds=["error", "manual_publish"])
    db.commit()
    assert count == 2
    db.expire_all()
    rows = db.scalars(select(Notification).order_by(Notification.id)).all()
    assert [r.read_at is not None for r in rows] == [True, True, False, False]
    assert rows[0].read_at == clock.now()


# ---------------------------------------------------------------------------------- desktop switches


def test_desktop_disabled_by_environment(db, recorder, desktop_off):
    notify.notify(db, "error", "a", "b")
    assert recorder.calls == []
    assert notify.desktop_enabled(db) is False


def test_desktop_sent_only_after_commit(db, recorder, desktop_on):
    created = notify.notify(db, "manual_publish", "Es hora de publicar en Demo", "Cuerpo", commit=False)
    assert recorder.calls == []
    db.commit()
    assert len(recorder.calls) == 1
    (item,) = recorder.calls[0]
    assert item.notification_id == created.id
    assert item.title == "Es hora de publicar en Demo"
    assert item.body == "Cuerpo"
    assert item.url == "http://localhost:8765/notificaciones"
    assert recorder.binds[0] is not None

    # Later commits do not resend it.
    db.commit()
    assert len(recorder.calls) == 1


def test_desktop_dropped_when_rolled_back(db, recorder, desktop_on):
    notify.notify(db, "error", "a", "b", commit=False)
    db.rollback()
    db.commit()
    assert recorder.calls == []


def test_desktop_respects_user_setting_and_parameter(db, recorder, desktop_on):
    notify.notify(db, "error", "a", "b", desktop=False)
    config.set_setting(db, "notifications_desktop", False)
    notify.notify(db, "error", "c", "d")
    assert recorder.calls == []
    assert len(db.scalars(select(Notification)).all()) == 2  # rows are still created
    config.set_setting(db, "notifications_desktop", True)
    notify.notify(db, "error", "e", "f")
    assert len(recorder.calls) == 1


def test_desktop_body_is_compacted_and_truncated(db, recorder, desktop_on):
    notify.notify(db, "info", "t", "línea uno\n\nlínea   dos " + "x" * 400)
    (item,) = recorder.calls[0]
    assert item.body.startswith("línea uno línea dos x")
    assert len(item.body) == notify.DESKTOP_BODY_MAX and item.body.endswith("…")


# ------------------------------------------------------------------------------------- dispatcher


def test_dispatcher_delivers_and_records_desktop_sent_at(app, db, desktop_on):
    delivered: list[tuple[str, str, str | None]] = []

    async def sender(title: str, body: str, url: str | None) -> None:
        delivered.append((title, body, url))

    dispatcher = DesktopDispatcher(sender, batch_window=0.01)
    previous = notify.set_dispatcher(dispatcher)
    try:
        created = notify.notify(db, "missed", "Publicación perdida en Demo", "Estaba programada…")

        def sent() -> bool:
            db.expire_all()
            return db.get(Notification, created.id).desktop_sent_at is not None

        assert wait_for(sent)
        assert delivered == [
            ("Publicación perdida en Demo", "Estaba programada…", "http://localhost:8765/notificaciones")
        ]
        assert dispatcher.delivered == 1
    finally:
        notify.set_dispatcher(previous)
        dispatcher.shutdown()
    assert not dispatcher.running


def test_dispatcher_coalesces_bursts(desktop_on):
    delivered: list[tuple[str, str]] = []

    async def sender(title: str, body: str, url: str | None) -> None:
        delivered.append((title, body))

    dispatcher = DesktopDispatcher(sender, batch_window=0.05)
    try:
        items = [DesktopItem(None, f"Aviso {i}", "cuerpo", None) for i in range(5)]
        dispatcher.submit(items[:2])
        dispatcher.submit(items[2:])
        assert wait_for(lambda: dispatcher.delivered >= 1)
        time.sleep(0.1)
        assert delivered == [("Tienes 5 avisos nuevos", "Aviso 0; Aviso 1; Aviso 2…")]
    finally:
        dispatcher.shutdown()


def test_dispatcher_swallows_sender_failures(desktop_on, caplog):
    calls: list[str] = []

    async def flaky(title: str, body: str, url: str | None) -> None:
        calls.append(title)
        if title == "primero":
            raise RuntimeError("no notification server")

    dispatcher = DesktopDispatcher(flaky, batch_window=0.01)
    try:
        dispatcher.submit([DesktopItem(1, "primero", "x", None)], bind=None)
        assert wait_for(lambda: calls == ["primero"])
        dispatcher.submit([DesktopItem(2, "segundo", "y", None)], bind=None)
        assert wait_for(lambda: dispatcher.delivered == 1)
        assert calls == ["primero", "segundo"]
    finally:
        dispatcher.shutdown()


def test_dispatcher_times_out_slow_senders(desktop_on, monkeypatch):
    monkeypatch.setattr(notify, "SEND_TIMEOUT_S", 0.05)

    async def slow(title: str, body: str, url: str | None) -> None:
        await asyncio.sleep(5)

    dispatcher = DesktopDispatcher(slow, batch_window=0.01)
    try:
        dispatcher.submit([DesktopItem(None, "lento", "x", None)])
        time.sleep(0.4)
        assert dispatcher.delivered == 0
    finally:
        dispatcher.shutdown()


def test_default_sender_uses_desktop_notifier(desktop_on, monkeypatch):
    sent: list[dict] = []
    created: list[str] = []

    class FakeNotifier:
        def __init__(self, app_name: str = "Python", **kwargs) -> None:
            created.append(app_name)

        async def send(self, **kwargs) -> str:
            sent.append(kwargs)
            if kwargs["title"] != "Rechazado":
                kwargs["on_dispatched"]()  # the backend accepted it
            return "id-1"

    fake_module = types.ModuleType("desktop_notifier")
    fake_module.DesktopNotifier = FakeNotifier  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "desktop_notifier", fake_module)
    opened: list[str] = []
    monkeypatch.setattr(notify.webbrowser, "open", lambda url: opened.append(url))

    dispatcher = DesktopDispatcher(batch_window=0.01)
    try:
        dispatcher.submit([DesktopItem(None, "Título", "Cuerpo", "http://localhost:8765/notificaciones")])
        dispatcher.submit([DesktopItem(None, "Otro", "Cuerpo 2", None)])
        assert wait_for(lambda: dispatcher.delivered == 2)
        # desktop-notifier swallows backend failures: no on_dispatched callback means "not delivered".
        dispatcher.submit([DesktopItem(None, "Rechazado", "x", None)])
        assert wait_for(lambda: len(sent) == 3)
        time.sleep(0.05)
        assert dispatcher.delivered == 2
    finally:
        dispatcher.shutdown()
    assert created == ["Planificador"]  # one notifier, reused
    assert [s["title"] for s in sent] == ["Título", "Otro", "Rechazado"]
    assert sent[0]["message"] == "Cuerpo"
    sent[0]["on_clicked"]()
    assert opened == ["http://localhost:8765/notificaciones"]
    assert sent[1]["on_clicked"] is None


def test_engine_notifications_carry_the_clock_time(app, db, clock, demo_account, desktop_off):
    from planificador.scheduler import Scheduler

    post = Post(brand_id=demo_account.brand_id, text="Asistida", scheduled_at=clock.now())
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=demo_account.id,
        network=demo_account.network,
        auto_publish=False,
        status=TargetStatus.SCHEDULED,
        scheduled_at=clock.now(),
    )
    db.add(target)
    db.commit()
    clock.advance(minutes=1)
    sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    sched.tick()
    sched.close()
    row = db.scalars(select(Notification)).one()
    assert row.kind == "manual_publish"
    assert row.created_at == clock.now()
    assert row.created_at - post.scheduled_at == timedelta(minutes=1)

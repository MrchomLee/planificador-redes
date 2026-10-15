"""Scheduler tests (SPEC §10): worker pool limits, thread start/stop, app lifespan wiring, sleep/wake gap
detection, heartbeat persistence, periodic jobs registry and the jobs themselves."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config, scheduler
from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    Notification,
    OAuthState,
    Post,
    PostEvent,
    PostTarget,
    PublicMediaToken,
    TargetStatus,
)
from planificador.providers import registry
from planificador.providers.base import ErrorKind, ProviderError, ProviderSettings, StepResult
from planificador.providers.demo import DemoProvider
from planificador.scheduler import (
    HEARTBEAT_KEY,
    JOB_KEY_PREFIX,
    ONLINE_SINCE_KEY,
    Job,
    Scheduler,
    analytics_due,
    job_cleanup,
    job_record,
    job_token_refresh,
    job_trash_purge,
)
from planificador.timeutil import ManualClock

# --------------------------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANIFICADOR_NO_DESKTOP_NOTIFY", "1")


@pytest.fixture(autouse=True)
def _stop_module_scheduler():
    yield
    scheduler.stop(timeout=5)


def make_account(
    db: Session, *, brand: Brand | None = None, network: str = "demo", **token_kwargs
) -> Account:
    if brand is None:
        brand = Brand(name="Otra marca")
        db.add(brand)
        db.flush()
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=f"{network}-{brand.id}",
        name=f"Cuenta {brand.id}",
        account_type="demo",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens(
        token_kwargs.pop("access", "access-token"),
        token_kwargs.pop("refresh", "refresh-token"),
        **token_kwargs,
    )
    db.add(account)
    db.commit()
    return account


def make_target(db: Session, account: Account, *, at, options: dict | None = None) -> PostTarget:
    post = Post(brand_id=account.brand_id, text="Publicación de prueba", scheduled_at=at)
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=account.id,
        network=account.network,
        format="post",
        options=dict(options or {"demo_steps": 1}),
        status=TargetStatus.SCHEDULED,
        scheduled_at=at,
    )
    db.add(target)
    db.commit()
    return target


def fresh(db: Session, target: PostTarget) -> PostTarget:
    db.expire_all()
    loaded = db.get(PostTarget, target.id)
    assert loaded is not None
    return loaded


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class BlockingDemo(DemoProvider):
    """Blocks every step on a gate and records concurrency (overall and per account)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.started = threading.Semaphore(0)
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.per_account: dict[int, int] = defaultdict(int)
        self.max_per_account = 0

    def publish_step(self, ctx):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.per_account[ctx.account.id] += 1
            self.max_per_account = max(self.max_per_account, self.per_account[ctx.account.id])
        self.started.release()
        try:
            assert self.gate.wait(10), "test gate never opened"
            return super().publish_step(ctx)
        finally:
            with self._lock:
                self.active -= 1
                self.per_account[ctx.account.id] -= 1


# ------------------------------------------------------------------------------------- worker pool


def test_worker_pool_limits_two_concurrent_and_one_per_account(app, db, clock, brand):
    provider = registry.register(BlockingDemo())
    a1 = make_account(db, brand=brand)
    a2 = make_account(db)
    a3 = make_account(db)
    now = clock.now()
    t1 = make_target(db, a1, at=now - timedelta(minutes=3))
    t2 = make_target(db, a1, at=now - timedelta(minutes=2))  # same account as t1
    t3 = make_target(db, a2, at=now - timedelta(minutes=1))
    t4 = make_target(db, a3, at=now)

    sched = Scheduler(app.state.db, clock, enable_jobs=False, max_workers=2)
    try:
        report = sched.tick()
        assert report.claimed == [t1.id, t3.id]
        assert set(report.deferred) == {t2.id, t4.id}
        assert provider.started.acquire(timeout=5) and provider.started.acquire(timeout=5)
        running = sched.running_targets()
        assert set(running) == {t1.id, t3.id}
        assert fresh(db, t2).status == TargetStatus.SCHEDULED  # never claimed while waiting for a slot

        # While both slots are busy nothing else starts.
        assert sched.tick().claimed == []

        provider.gate.set()
        assert wait_for(lambda: not sched.running_targets())
        report = sched.tick()
        assert report.claimed == [t2.id, t4.id]
        assert wait_for(lambda: not sched.running_targets())
        for target in (t1, t2, t3, t4):
            assert fresh(db, target).status == TargetStatus.PUBLISHED
        assert provider.max_active == 2
        assert provider.max_per_account == 1
    finally:
        provider.gate.set()
        assert sched.stop(timeout=5)


# ---------------------------------------------------------------------------------- thread lifecycle


def test_thread_publishes_on_wake_and_stops_cleanly(app, db, clock, demo_account):
    sched = Scheduler(app.state.db, clock, tick_seconds=30, enable_jobs=False)
    sched.start()
    try:
        assert sched.is_running
        sched.start()  # single loop guard
        loops = [t for t in threading.enumerate() if t.name == "planificador-scheduler" and t.is_alive()]
        assert len(loops) == 1
        assert wait_for(lambda: sched.ticks >= 1)

        target = make_target(db, demo_account, at=clock.now())
        sched.wake()  # without the wake the next tick would be 30 s away
        assert wait_for(lambda: fresh(db, target).status == TargetStatus.PUBLISHED)
    finally:
        assert sched.stop(timeout=5) is True
    assert not sched.is_running
    assert sched.http.is_closed


def test_stop_pauses_a_running_publish_and_it_resumes_later(app, db, clock, demo_account):
    class SlowPrepare(DemoProvider):
        def __init__(self) -> None:
            super().__init__()
            self.gate = threading.Event()
            self.started = threading.Event()
            self.commits = 0

        def publish_step(self, ctx):
            if not ctx.state.get("prepared"):
                self.started.set()
                assert self.gate.wait(10)
                ctx.state["prepared"] = True
                ctx.save_state(ctx.state)
                return StepResult("continue")
            self.commits += 1
            return super().publish_step(ctx)

    provider = registry.register(SlowPrepare())
    target = make_target(db, demo_account, at=clock.now())
    sched = Scheduler(app.state.db, clock, tick_seconds=30, enable_jobs=False)
    sched.start()
    assert provider.started.wait(5)
    threading.Timer(0.2, provider.gate.set).start()
    assert sched.stop(timeout=5) is True  # waited for the worker, which paused at the step boundary

    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING
    assert t.next_attempt_at == clock.now()
    assert t.provider_state["prepared"] is True
    assert "paused" in db.scalars(select(PostEvent.event).where(PostEvent.target_id == target.id)).all()
    assert provider.commits == 0

    restarted = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    assert restarted.tick().outcomes[target.id] == "published"
    restarted.close()
    assert provider.commits == 1


def test_module_api_start_is_idempotent(app, monkeypatch):
    monkeypatch.setattr(scheduler, "default_jobs", lambda: [])
    first = scheduler.start(app)
    second = scheduler.start(app.state)
    assert first is second
    assert scheduler.get_scheduler() is first
    assert app.state.scheduler is first
    assert first.is_running
    scheduler.wake()
    assert wait_for(lambda: first.ticks >= 1)
    status = scheduler.status()
    assert status["running"] is True and status["online_since"] is not None

    scheduler.stop(app)
    assert scheduler.get_scheduler() is None
    assert not first.is_running
    scheduler.wake()  # no-op once stopped
    assert scheduler.status() == {"running": False}


def test_app_lifespan_starts_and_stops_the_scheduler(tmp_path, monkeypatch):
    from planificador.app import create_app

    monkeypatch.setattr(scheduler, "default_jobs", lambda: [])
    settings = config.load_app_settings(data_dir=tmp_path / "lifespan", scheduler_enabled=True)
    application = create_app(settings, clock=ManualClock(datetime(2026, 11, 3, 9, 0, tzinfo=UTC)))
    try:
        with TestClient(application, base_url="http://localhost:8765") as client:
            assert client.get("/healthz").status_code == 200
            sched = scheduler.get_scheduler()
            assert sched is not None and sched.is_running
        assert scheduler.get_scheduler() is None
        assert not sched.is_running
    finally:
        application.state.db.dispose()


# ------------------------------------------------------------------------------- heartbeat and gaps


def test_heartbeat_is_persisted_and_survives_a_quick_restart(app, db, clock):
    first = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    report = first.tick()
    assert report.first is True and report.gap is False
    assert config.get_timestamp(db, HEARTBEAT_KEY) == clock.now()
    online = first.online_since
    assert online == clock.now()

    clock.advance(seconds=40)  # restart within the gap threshold: continuity is kept
    second = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    report = second.tick()
    assert report.gap is False
    assert second.online_since == online
    assert config.get_timestamp(db, ONLINE_SINCE_KEY) == online

    clock.advance(minutes=10)  # app was closed for a while
    third = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    report = third.tick()
    assert report.gap is True and report.gap_seconds == 600
    assert third.online_since == clock.now()
    for s in (first, second, third):
        s.close()


def test_sleep_gap_forces_catch_up_jobs(app, db, clock):
    calls: list[datetime] = []
    jobs = [Job("token_refresh", "Renovación", lambda s, now: calls.append(now), interval=timedelta(hours=1))]
    sched = Scheduler(app.state.db, clock, inline=True, jobs=jobs)
    assert sched.tick().jobs == ["token_refresh"]
    clock.advance(minutes=1)
    report = sched.tick()
    assert report.jobs == [] and report.gap is False
    clock.advance(minutes=30)  # the PC slept
    report = sched.tick()
    assert report.gap is True
    assert report.jobs == ["token_refresh"]  # forced although its hour has not elapsed
    assert sched.online_since == clock.now()
    assert len(calls) == 2
    sched.close()


def test_sleep_seconds_tracks_the_next_deadline(app, db, clock, demo_account):
    sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, tick_seconds=15)
    assert sched._sleep_seconds() == 15
    target = make_target(db, demo_account, at=clock.now() + timedelta(seconds=4))
    assert sched._sleep_seconds() == pytest.approx(4)
    t = fresh(db, target)
    t.status = TargetStatus.PUBLISHING
    t.next_attempt_at = clock.now() + timedelta(seconds=2)
    db.commit()
    assert sched._sleep_seconds() == pytest.approx(2)
    t.next_attempt_at = clock.now() + timedelta(milliseconds=100)
    db.commit()
    assert sched._sleep_seconds() == scheduler.MIN_SLEEP_SECONDS
    # A past deadline (running, or waiting for a free worker) does not make the loop spin.
    t.next_attempt_at = clock.now() - timedelta(seconds=10)
    db.commit()
    assert sched._sleep_seconds() == 15
    sched.close()


# ---------------------------------------------------------------------------------- periodic jobs


def test_default_jobs_run_and_record_their_outcome(app, db, clock, monkeypatch):
    from planificador import analytics, inbox

    seen: dict = {}

    def fake_snapshot_all(db, http, clock, provider_settings):
        seen["analytics"] = (http, clock, provider_settings)
        return {"accounts": 0}

    def fake_poll_all(db, http, clock, provider_settings):
        seen.setdefault("inbox", 0)
        seen["inbox"] += 1
        return 3

    monkeypatch.setattr(analytics, "snapshot_all", fake_snapshot_all, raising=False)
    monkeypatch.setattr(inbox, "poll_all", fake_poll_all, raising=False)

    sched = Scheduler(app.state.db, clock, inline=True)
    report = sched.tick()
    assert set(report.jobs) == {j.name for j in scheduler.default_jobs()}
    http, used_clock, provider_settings = seen["analytics"]
    assert http is sched.http and used_clock is clock
    assert isinstance(provider_settings, ProviderSettings)
    for name in report.jobs:
        record = job_record(db, name)
        assert record["status"] == "ok", (name, record)
        assert record["last_run_at"] == clock.now().isoformat()
        assert record["last_ok_at"] == clock.now().isoformat()
    assert job_record(db, "inbox_poll")["result"] == 3
    assert job_record(db, "analytics_snapshot")["result"] == {"accounts": 0}

    ran: list[str] = []
    for _ in range(15):  # tick every minute (no gap) for a quarter of an hour
        clock.advance(minutes=1)
        ran += sched.tick().jobs
    assert ran == ["inbox_poll"]
    assert seen["inbox"] == 2
    sched.close()


def test_job_failures_are_recorded_and_scrubbed(app, db, clock, monkeypatch):
    def boom(sched, now):
        raise RuntimeError("fallo con access_token=abc123secret")

    def not_ready(sched, now):
        raise NotImplementedError

    jobs = [
        Job("boom", "Roto", boom, interval=timedelta(hours=1)),
        Job("pending", "Pendiente", not_ready, interval=timedelta(hours=1)),
    ]
    sched = Scheduler(app.state.db, clock, inline=True, jobs=jobs)
    report = sched.tick()  # never raises
    assert report.jobs == ["boom", "pending"]
    record = job_record(db, "boom")
    assert record["status"] == "error"
    assert "abc123secret" not in record["last_error"] and "RuntimeError" in record["last_error"]
    assert record["last_ok_at"] is None
    assert job_record(db, "pending")["status"] == "not_implemented"
    assert config.get_setting(db, JOB_KEY_PREFIX + "boom")["last_run_at"] == clock.now().isoformat()
    sched.close()


def test_missing_analytics_and_inbox_functions_are_tolerated(app, db, clock, monkeypatch):
    from planificador import analytics, inbox

    monkeypatch.delattr(analytics, "snapshot_all", raising=False)
    monkeypatch.delattr(inbox, "poll_all", raising=False)
    sched = Scheduler(app.state.db, clock, inline=True)
    sched.tick()
    assert job_record(db, "analytics_snapshot")["status"] == "not_implemented"
    assert job_record(db, "inbox_poll")["status"] == "not_implemented"
    sched.close()


def test_analytics_runs_daily_after_six_local(app, db, clock):
    sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    yesterday_8_local = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # Madrid is UTC+1 in November
    assert analytics_due(sched, db, datetime(2026, 11, 3, 4, 30, tzinfo=UTC), yesterday_8_local) is False
    assert analytics_due(sched, db, datetime(2026, 11, 3, 5, 30, tzinfo=UTC), yesterday_8_local) is True
    today_6_10_local = datetime(2026, 11, 3, 5, 10, tzinfo=UTC)
    assert analytics_due(sched, db, datetime(2026, 11, 3, 18, 0, tzinfo=UTC), today_6_10_local) is False
    assert analytics_due(sched, db, datetime(2026, 11, 3, 18, 0, tzinfo=UTC), None) is True
    stale = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    assert analytics_due(sched, db, datetime(2026, 11, 3, 4, 0, tzinfo=UTC), stale) is True  # > 24 h
    sched.close()


def test_trash_purge_deletes_posts_older_than_45_days(app, db, clock, demo_account):
    now = clock.now()
    old = make_target(db, demo_account, at=now - timedelta(days=50))
    recent = make_target(db, demo_account, at=now - timedelta(days=50))
    live = make_target(db, demo_account, at=now)
    db.get(Post, old.post_id).deleted_at = now - timedelta(days=46)
    db.get(Post, recent.post_id).deleted_at = now - timedelta(days=44)
    db.add(PostEvent(target_id=old.id, ts=now, event="x", message="y"))
    db.add(Notification(kind="error", title="t", body="b", target_id=old.id))
    db.commit()

    sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    assert job_trash_purge(sched, now) == {"purged": 1}
    sched.close()
    db.expunge_all()
    assert db.get(Post, old.post_id) is None
    assert db.get(PostTarget, old.id) is None
    assert db.scalars(select(PostEvent).where(PostEvent.target_id == old.id)).all() == []
    assert db.scalars(select(Notification)).one().target_id is None
    assert db.get(Post, recent.post_id) is not None
    assert db.get(Post, live.post_id) is not None


def test_cleanup_removes_expired_tokens_and_oauth_states(app, db, clock):
    now = clock.now()
    db.add_all(
        [
            PublicMediaToken(
                token="old", rel_path="a", mime="image/jpeg", expires_at=now - timedelta(days=2)
            ),
            PublicMediaToken(
                token="recent", rel_path="a", mime="image/jpeg", expires_at=now - timedelta(hours=1)
            ),
            PublicMediaToken(
                token="revoked",
                rel_path="a",
                mime="image/jpeg",
                expires_at=now + timedelta(hours=1),
                revoked_at=now - timedelta(days=2),
            ),
            PublicMediaToken(
                token="valid", rel_path="a", mime="image/jpeg", expires_at=now + timedelta(hours=1)
            ),
            OAuthState(state="stale", network="tiktok", created_at=now - timedelta(hours=2)),
            OAuthState(state="fresh", network="tiktok", created_at=now - timedelta(minutes=5)),
        ]
    )
    db.commit()
    sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    assert job_cleanup(sched, now) == {"public_media_tokens": 2, "oauth_states": 1}
    sched.close()
    db.expire_all()
    assert sorted(db.scalars(select(PublicMediaToken.token))) == ["recent", "valid"]
    assert db.scalars(select(OAuthState.state)).all() == ["fresh"]


def test_token_refresh_job(app, db, clock, brand):
    class RefreshProbe(DemoProvider):
        broken: set[int] = set()

        def refresh_credentials(self, account, settings, http, clock):
            if account.id in self.broken:
                raise ProviderError(ErrorKind.AUTH, "revoked", "Permiso revocado en la red.")
            return super().refresh_credentials(account, settings, http, clock)

    provider = registry.register(RefreshProbe())
    now = clock.now()
    expiring = make_account(db, brand=brand, token_expires_at=now + timedelta(minutes=30))
    broken = make_account(db)
    warn = make_account(db, refresh=None, token_expires_at=now + timedelta(days=3))
    provider.broken = {broken.id}

    sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    summary = job_token_refresh(sched, now)
    assert summary == {"checked": 3, "refreshed": 1, "reconnect": 1, "warnings": 1}
    db.expire_all()
    refreshed = db.get(Account, expiring.id)
    assert refreshed.access_token.startswith(f"demo-access-{expiring.id}-")
    assert refreshed.token_expires_at == now + timedelta(days=60)
    assert db.get(Account, broken.id).status == AccountStatus.NEEDS_RECONNECT
    notes = db.scalars(select(Notification).order_by(Notification.id)).all()
    assert [(n.kind, n.account_id) for n in notes] == [("reconnect", broken.id), ("reconnect", warn.id)]
    assert notes[1].title == "Tu conexión con Demo caduca pronto"
    assert "viernes 6 de noviembre" in notes[1].body

    # Second run: the broken account is skipped (not connected) and the warning is not repeated.
    summary = job_token_refresh(sched, now + timedelta(hours=1))
    assert summary["warnings"] == 0 and summary["checked"] == 2
    assert len(db.scalars(select(Notification)).all()) == 2
    sched.close()


def test_scheduler_thread_keeps_the_pc_awake_while_a_publish_waits(app, db, clock, demo_account, monkeypatch):
    from planificador import keepawake

    calls: list[tuple[str, int]] = []

    class FakeKernel32:
        def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802 - Windows API name
            calls.append((threading.current_thread().name, flags))
            return 1

    monkeypatch.setattr(keepawake, "_kernel32", lambda: FakeKernel32())
    required = keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED
    # The manual clock never advances, so the target stays "publishing" (waiting between steps).
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 3})
    sched = Scheduler(app.state.db, clock, tick_seconds=30, enable_jobs=False)
    sched.start()
    try:
        assert wait_for(lambda: ("planificador-scheduler", required) in calls)
        assert fresh(db, target).status == TargetStatus.PUBLISHING
    finally:
        assert sched.stop(timeout=5)
    loop_calls = [flags for name, flags in calls if name == "planificador-scheduler"]
    assert loop_calls == [required, keepawake.ES_CONTINUOUS]  # held once, released on stop
    worker_calls = [flags for name, flags in calls if name.startswith("planificador-publish-")]
    assert worker_calls == [required, keepawake.ES_CONTINUOUS]  # the run itself, on its own thread

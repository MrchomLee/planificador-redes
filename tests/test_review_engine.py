"""Regression tests for the publishing-correctness / data-integrity / UX review of the engine area
(``engine.py``, ``scheduler.py``, ``postsvc.py``). One section per review finding:

0. Resuming a target after its commit never fails it on pre-flight checks (account flagged, token refresh).
1. Waiting / paused / stale pre-commit targets follow the grace/missed rule.
2. Cancelling (or trashing) a natively scheduled post never reports success when the network kept it.
3. A failed early hand-off to Facebook's/YouTube's scheduler goes back to ``scheduled`` instead of ``error``.
4. Token refreshes of one account are serialised and always start from the stored tokens.
5. Publishing / editing a target resolves its stale notifications.
6. The reconnect notification does not repeat the advice.
7. postsvc writes are guarded against a concurrent scheduler claim.
8. Restoring a trashed post re-schedules its natively scheduled target.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import engine, postsvc
from planificador.engine import EngineError
from planificador.models import (
    Account,
    AccountStatus,
    CommitPhase,
    Notification,
    NotificationKind,
    Post,
    PostEvent,
    PostTarget,
    TargetStatus,
)
from planificador.postsvc import PostError
from planificador.providers import registry
from planificador.providers.base import (
    ErrorKind,
    ProviderError,
    PublishContext,
    ReconcileResult,
    StepResult,
    TokenUpdate,
)
from planificador.providers.demo import DemoProvider
from planificador.scheduler import Job, Scheduler, job_token_refresh
from test_engine import (
    CountingDemo,
    FakeTikTok,
    NativeFacebook,
    advance_to_next_attempt,
    events,
    fresh,
    make_account,
    make_target,
    notifications,
)
from test_postsvc import data, demo_target

# --------------------------------------------------------------------------------------------- helpers


@pytest.fixture
def sched(app, clock):
    scheduler = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(5))
    yield scheduler
    scheduler.close()


def restart(app, clock) -> Scheduler:
    """A new scheduler on the same database (the app was closed and opened again)."""
    return Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(9))


def set_account_status(db: Session, account: Account, status: AccountStatus) -> None:
    acc = db.get(Account, account.id)
    acc.status = status
    db.commit()


def unread(db: Session, target: PostTarget) -> list[str]:
    db.expire_all()
    return [
        n.kind
        for n in db.scalars(
            select(Notification).where(Notification.target_id == target.id, Notification.read_at.is_(None))
        )
    ]


class CommitThenProcess(DemoProvider):
    """Sends its irreversible request on the first step, then polls "processing" ``polls`` times (TikTok /
    YouTube / Facebook reels). ``refresh_error`` makes every credential refresh fail."""

    def __init__(self, polls: int = 1) -> None:
        super().__init__()
        self.polls = polls
        self.refresh_error: Exception | None = None
        self.refreshes = 0
        self.reconciles = 0

    def validate(self, target):
        return []

    def refresh_credentials(self, account, settings, http, clock):
        self.refreshes += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        return None

    def publish_step(self, ctx: PublishContext) -> StepResult:
        st = ctx.state
        if not st.get("committed"):
            ctx.before_commit(st)
            st["committed"] = True
            ctx.save_state(st)
            return StepResult("wait", wait_seconds=30, message="Procesando…")
        st["polled"] = int(st.get("polled", 0)) + 1
        ctx.save_state(st)
        if st["polled"] < self.polls:
            return StepResult("wait", wait_seconds=30, message="Procesando…")
        return StepResult("published", external_id="v-1")

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        self.reconciles += 1
        return ReconcileResult("unknown", message="Sigue procesando")


def event_message(db: Session, target: PostTarget, event: str) -> str:
    return db.scalars(
        select(PostEvent.message)
        .where(PostEvent.target_id == target.id, PostEvent.event == event)
        .order_by(PostEvent.id.desc())
        .limit(1)
    ).one()


def committed_and_waiting(db, clock, sched, account, provider) -> PostTarget:
    target = make_target(db, account, at=clock.now())
    assert sched.tick().outcomes[target.id] == "wait"
    assert fresh(db, target).commit_phase == CommitPhase.COMMIT_SENT
    return target


# ------------------------------------------------------- 0. resume after the commit: no pre-flight failures


def test_committed_target_keeps_polling_when_account_gets_flagged(db, clock, demo_account, sched):
    provider = registry.register(CommitThenProcess())
    target = committed_and_waiting(db, clock, sched, demo_account, provider)
    # E.g. the hourly token job (or another post of the account) flags it while the video is processing.
    set_account_status(db, demo_account, AccountStatus.NEEDS_RECONNECT)
    advance_to_next_attempt(db, clock, target)
    assert sched.tick().outcomes[target.id] == "published"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED and t.external_id == "v-1"
    assert provider.reconciles == 0
    assert notifications(db) == []  # no "No se pudo publicar" for a post that went out


def test_precommit_target_still_fails_fast_when_account_flagged(db, clock, demo_account, sched):
    provider = registry.register(CommitThenProcess())
    set_account_status(db, demo_account, AccountStatus.NEEDS_RECONNECT)
    target = make_target(db, demo_account, at=clock.now())
    assert sched.tick().outcomes[target.id] == "error"
    assert "necesita reconexión" in fresh(db, target).last_error
    assert provider.refreshes == 0


@pytest.mark.parametrize(
    "error",
    [
        ProviderError(ErrorKind.TRANSIENT, "oauth_503", "Fallo temporal del servidor de tokens."),
        ProviderError(ErrorKind.RATE_LIMITED, "oauth_429", "Demasiadas peticiones."),
        RuntimeError("boom"),
    ],
    ids=["transient", "rate_limited", "crash"],
)
def test_refresh_glitch_after_commit_is_not_an_ambiguous_outcome(db, clock, demo_account, sched, error):
    provider = registry.register(CommitThenProcess(polls=3))
    target = committed_and_waiting(db, clock, sched, demo_account, provider)
    provider.refresh_error = error
    for _ in range(2):  # two polls with a broken token endpoint: still waiting, never reconciled
        advance_to_next_attempt(db, clock, target)
        assert sched.tick().outcomes[target.id] == "wait"
    provider.refresh_error = None
    advance_to_next_attempt(db, clock, target)
    assert sched.tick().outcomes[target.id] == "published"
    assert provider.reconciles == 0
    assert fresh(db, target).status == TargetStatus.PUBLISHED
    assert events(db, target).count("token_refresh_failed") == 1  # logged once per error
    assert db.get(Account, demo_account.id).status == AccountStatus.CONNECTED


def test_auth_refresh_failure_after_commit_flags_account_but_keeps_the_post(db, clock, demo_account, sched):
    provider = registry.register(CommitThenProcess())
    target = committed_and_waiting(db, clock, sched, demo_account, provider)
    provider.refresh_error = ProviderError(ErrorKind.AUTH, "revoked", "El acceso fue revocado.")
    advance_to_next_attempt(db, clock, target)
    assert sched.tick().outcomes[target.id] == "published"
    db.expire_all()
    assert db.get(Account, demo_account.id).status == AccountStatus.NEEDS_RECONNECT
    assert fresh(db, target).status == TargetStatus.PUBLISHED
    assert [n.kind for n in notifications(db)] == ["reconnect"]


# ------------------------------------------------------------- 1. grace/missed for waiting/stale targets


def test_retry_wait_after_app_was_off_beyond_grace_is_missed(app, db, clock, demo_account, sched):
    demo = registry.register(CountingDemo())
    target = make_target(
        db,
        demo_account,
        at=clock.now(),
        options={"demo_steps": 1, "demo_fail": "transient", "demo_fail_times": 1},
    )
    assert sched.tick().outcomes[target.id] == "retry"
    steps = demo.steps
    clock.advance(hours=10)  # laptop suspended / app closed (grace: 120 min)
    report = restart(app, clock).tick()
    assert report.missed == [target.id] and target.id not in report.outcomes
    t = fresh(db, target)
    assert t.status == TargetStatus.MISSED
    assert t.provider_state == {} and t.commit_phase == CommitPhase.NONE and t.next_attempt_at is None
    assert demo.steps == steps and demo.commits == 0
    assert [n.kind for n in notifications(db)] == ["missed"]
    assert "terminar el envío" in event_message(db, target, "missed")
    # The usual way out ("Reintentar" / "Publicar ahora") starts from scratch.
    engine.retry_target(db, target.id, clock)
    assert fresh(db, target).status == TargetStatus.SCHEDULED


def test_stale_precommit_claim_after_app_was_off_beyond_grace_is_missed(app, db, clock, demo_account):
    demo = registry.register(CountingDemo())
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 2})
    assert engine.claim_due(db, target.id, clock.now())  # claimed, then the app died before running it
    clock.advance(hours=30)
    report = restart(app, clock).tick()
    assert report.missed == [target.id] and report.recovered == []
    assert fresh(db, target).status == TargetStatus.MISSED
    assert demo.steps == 0


def test_paused_target_reopened_within_grace_continues(app, db, clock, demo_account):
    registry.register(CountingDemo())
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 3})
    first = restart(app, clock)
    assert first.tick().outcomes[target.id] == "wait"
    first.runtime.stop_event.set()  # the app is closing: the next step pauses
    advance_to_next_attempt(db, clock, target)
    assert engine.run_target(target.id, first.runtime) == "paused"
    assert "quedará como perdida" in event_message(db, target, "paused")
    clock.advance(minutes=30)
    report = restart(app, clock).tick()
    assert report.continued == [target.id] and report.missed == []


def test_paused_target_reopened_days_later_is_missed(app, db, clock, demo_account):
    registry.register(CountingDemo())
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 3})
    first = restart(app, clock)
    assert first.tick().outcomes[target.id] == "wait"
    first.runtime.stop_event.set()
    advance_to_next_attempt(db, clock, target)
    assert engine.run_target(target.id, first.runtime) == "paused"
    clock.advance(days=2)
    report = restart(app, clock).tick()
    assert report.missed == [target.id]
    assert fresh(db, target).status == TargetStatus.MISSED


def test_committed_waiting_target_is_never_missed(app, db, clock, demo_account, sched):
    provider = registry.register(CommitThenProcess())
    target = committed_and_waiting(db, clock, sched, demo_account, provider)
    clock.advance(hours=10)
    report = restart(app, clock).tick()
    assert report.missed == [] and report.outcomes[target.id] == "published"


class LongRateLimit(CountingDemo):
    """Rate limited once with a 3-hour ``retry_after``, then publishes."""

    def publish_step(self, ctx: PublishContext) -> StepResult:
        if not ctx.state.get("limited"):
            ctx.state["limited"] = True
            ctx.save_state(ctx.state)
            raise ProviderError(ErrorKind.RATE_LIMITED, "limit", "Límite de la red.", retry_after=3 * 3600)
        return super().publish_step(ctx)


def test_late_retry_is_measured_from_its_own_time(app, db, clock, demo_account, sched):
    registry.register(LongRateLimit())
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    assert sched.tick().outcomes[target.id] == "retry"
    # The retry was due 3 h after the scheduled time; the app was closed and reopened 30 min after that:
    # within the grace window counted from the retry time, so it carries on.
    clock.set(fresh(db, target).next_attempt_at + timedelta(minutes=30))
    report = restart(app, clock).tick()
    assert report.missed == [] and report.outcomes[target.id] == "published"


# ------------------------------------------------------------------- 2. native cancel never fakes success


class FailingCancelFacebook(NativeFacebook):
    def cancel_native(self, ctx: PublishContext) -> None:
        raise ProviderError(ErrorKind.TRANSIENT, "net", "Sin conexión.")


def native_published(db, clock, account, *, days: int = 2) -> PostTarget:
    target = make_target(
        db,
        account,
        at=clock.now() + timedelta(days=days),
        options={"native_schedule": True},
        status=TargetStatus.PUBLISHED,
    )
    t = fresh(db, target)
    t.provider_state = {"native_scheduled": True, "result": {"external_id": "1_2"}}
    t.external_id = "1_2"
    t.commit_phase = CommitPhase.COMMIT_SENT
    t.published_at = t.scheduled_at
    db.commit()
    return target


def test_native_cancel_failure_is_refused(db, clock):
    registry.register(FailingCancelFacebook())
    account = make_account(db, network="facebook")
    target = native_published(db, clock, account)
    with pytest.raises(EngineError) as info:
        engine.cancel_target(db, target.id, clock)
    assert info.value.code == "native_cancel_failed"
    assert "Sin conexión" in info.value.message and "Sigue programada allí" in info.value.message
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED and t.provider_state["native_scheduled"] is True
    assert events(db, target) == ["native_cancel_failed"]


def test_cancel_route_reports_the_native_failure(csrf_client, db, clock):
    registry.register(FailingCancelFacebook())
    account = make_account(db, network="facebook")
    target = native_published(db, clock, account)
    r = csrf_client.post(f"/objetivos/{target.id}/cancelar", headers={"HX-Request": "true"})
    assert r.status_code == 409
    assert "No se ha podido retirar la publicación de la programación de Facebook" in r.text
    assert fresh(db, target).status == TargetStatus.PUBLISHED


def test_cancel_only_in_the_app_is_offered_after_a_failed_withdrawal(csrf_client, db, clock):
    """The user deleted the post on Facebook by hand: once the withdrawal failed, the drawer offers a
    confirmed "Cancelar solo en la app" (engine ``force=True``); it is not offered before."""
    registry.register(FailingCancelFacebook())
    account = make_account(db, network="facebook")
    target = native_published(db, clock, account)
    before = csrf_client.get(f"/publicaciones/{target.post_id}", headers={"HX-Request": "true"})
    assert before.status_code == 200 and "Cancelar solo en la app" not in before.text
    csrf_client.post(f"/objetivos/{target.id}/cancelar", headers={"HX-Request": "true"})
    after = csrf_client.get(f"/publicaciones/{target.post_id}", headers={"HX-Request": "true"})
    assert "Cancelar solo en la app" in after.text and "solo_app" in after.text
    r = csrf_client.post(
        f"/objetivos/{target.id}/cancelar", data={"solo_app": "1"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    assert fresh(db, target).status == TargetStatus.CANCELLED
    assert events(db, target)[-2:] == ["cancelled", "native_cancel"]


def test_trash_with_failed_native_withdrawal_is_refused(db, clock):
    registry.register(FailingCancelFacebook())
    account = make_account(db, network="facebook")
    target = native_published(db, clock, account)
    with pytest.raises(PostError) as info:
        postsvc.delete_post(db, target.post_id, clock)
    assert "no se ha movido a la papelera" in info.value.message
    db.expire_all()
    assert db.get(Post, target.post_id).deleted_at is None
    assert fresh(db, target).status == TargetStatus.PUBLISHED


# ------------------------------------------------- 3. failed early native hand-off goes back to scheduled


class ToggleNativeFacebook(CountingDemo):
    """Hands the post to "Facebook's scheduler" when there is lead time (else publishes); ``fail`` makes the
    hand-off raise that error."""

    network = "facebook"
    display_name = "Facebook"

    def __init__(self) -> None:
        super().__init__()
        self.fail: ProviderError | None = None

    def validate(self, target):
        return []

    def publish_step(self, ctx: PublishContext) -> StepResult:
        self.steps += 1
        native = ctx.target.scheduled_at - ctx.clock.now() >= timedelta(minutes=10)
        if native and self.fail is not None:
            raise self.fail
        if native:
            ctx.state["native_scheduled"] = True
        ctx.before_commit(ctx.state)
        self.commits += 1
        return StepResult("published", external_id="123_456")


def run_until_settled(db, clock, sched, target, limit: int = 10) -> PostTarget:
    for _ in range(limit):
        sched.tick()
        t = fresh(db, target)
        if t.status != TargetStatus.PUBLISHING:
            return t
        advance_to_next_attempt(db, clock, target)
    raise AssertionError("target never settled")


def test_early_native_transient_failure_returns_to_scheduled_and_retries_later(db, clock, sched):
    provider = registry.register(ToggleNativeFacebook())
    provider.fail = ProviderError(ErrorKind.TRANSIENT, "net", "Sin conexión.")
    account = make_account(db, network="facebook")
    due = clock.now() + timedelta(days=20)
    target = make_target(db, account, at=due, options={"native_schedule": True})
    t = run_until_settled(db, clock, sched, target)
    assert t.status == TargetStatus.SCHEDULED
    assert t.commit_phase == CommitPhase.NONE and t.attempts == 0
    assert t.next_attempt_at == clock.now() + engine.NATIVE_HANDOFF_RETRY
    assert "No se pudo dejar programada en Facebook" in t.last_error
    assert [n.kind for n in notifications(db)] == []  # nothing lost: no error yet
    assert events(db, target).count("handoff_failed") == 1
    # Not claimed again before the hand-off retry time.
    clock.advance(hours=1)
    assert sched.tick().claimed == []
    # Second failed attempt: no duplicate event.
    clock.set(fresh(db, target).next_attempt_at)
    t = run_until_settled(db, clock, sched, target)
    assert t.status == TargetStatus.SCHEDULED and events(db, target).count("handoff_failed") == 1
    # Network back: the next attempt hands it off.
    provider.fail = None
    clock.set(t.next_attempt_at)
    report = sched.tick()
    assert report.native == [target.id] and report.outcomes[target.id] == "published"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED and t.provider_state["native_scheduled"] is True
    assert t.last_error is None


def test_early_native_failure_still_publishes_at_its_time(db, clock, sched):
    provider = registry.register(ToggleNativeFacebook())
    provider.fail = ProviderError(ErrorKind.TRANSIENT, "net", "Sin conexión.")
    account = make_account(db, network="facebook")
    due = clock.now() + timedelta(hours=3)
    target = make_target(db, account, at=due, options={"native_schedule": True})
    assert run_until_settled(db, clock, sched, target).status == TargetStatus.SCHEDULED
    clock.set(due + timedelta(seconds=5))
    report = sched.tick()
    assert report.claimed == [target.id] and report.outcomes[target.id] == "published"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED and not t.provider_state.get("native_scheduled")


def test_early_native_permanent_rejection_waits_for_its_time_and_notifies(db, clock, sched):
    provider = registry.register(ToggleNativeFacebook())
    provider.fail = ProviderError(ErrorKind.PERMANENT, "fb_schedule", "Facebook no admite esa hora.")
    account = make_account(db, network="facebook")
    due = clock.now() + timedelta(days=5)
    target = make_target(db, account, at=due, options={"native_schedule": True})
    report = sched.tick()
    assert report.outcomes[target.id] == "handoff_failed"
    t = fresh(db, target)
    assert t.status == TargetStatus.SCHEDULED and t.next_attempt_at == due
    notes = notifications(db)
    assert [n.kind for n in notes] == ["error"] and notes[0].title == "No se pudo programar en Facebook"
    clock.advance(days=1)
    assert sched.tick().claimed == []
    clock.set(due)
    report = sched.tick()
    assert report.outcomes[target.id] == "published"
    assert unread(db, target) == []  # the hand-off warning is moot once it went out


def test_early_native_auth_failure_flags_account_and_keeps_the_post(db, clock, sched):
    provider = registry.register(ToggleNativeFacebook())
    provider.fail = ProviderError(ErrorKind.AUTH, "token", "El token de la página no es válido.")
    account = make_account(db, network="facebook")
    target = make_target(db, account, at=clock.now() + timedelta(days=5), options={"native_schedule": True})
    assert sched.tick().outcomes[target.id] == "handoff_failed"
    assert fresh(db, target).status == TargetStatus.SCHEDULED
    db.expire_all()
    assert db.get(Account, account.id).status == AccountStatus.NEEDS_RECONNECT
    assert [n.kind for n in notifications(db)] == ["reconnect"]


def test_native_targets_of_a_broken_account_are_not_claimed_early(db, clock, sched):
    provider = registry.register(ToggleNativeFacebook())
    account = make_account(db, network="facebook")
    natives = [
        make_target(db, account, at=clock.now() + timedelta(days=d), options={"native_schedule": True})
        for d in (5, 10, 20)
    ]
    set_account_status(db, account, AccountStatus.NEEDS_RECONNECT)
    for _ in range(3):
        assert sched.tick().claimed == []
        clock.advance(seconds=15)
    assert all(fresh(db, t).status == TargetStatus.SCHEDULED for t in natives)
    assert notifications(db) == [] and provider.steps == 0
    set_account_status(db, account, AccountStatus.CONNECTED)
    report = sched.tick()
    assert sorted(report.native) == sorted(t.id for t in natives)
    assert all(fresh(db, t).status == TargetStatus.PUBLISHED for t in natives)


# ---------------------------------------------------------------- 4. serialised, fresh token refreshes


class RotatingServer:
    """A token endpoint that rotates the refresh token and rejects superseded ones (TikTok may do this)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.valid = "R1"
        self.serial = 1
        self.calls: list[str] = []

    def refresh(self, token: str) -> str:
        time.sleep(0.2)  # network latency: lets a concurrent refresh overlap
        with self.lock:
            self.calls.append(token)
            if token != self.valid:
                raise ProviderError(ErrorKind.AUTH, "invalid_grant", "Reconecta TikTok.")
            self.serial += 1
            self.valid = f"R{self.serial}"
            return self.valid


class RotatingTikTok(FakeTikTok):
    def __init__(self, server: RotatingServer) -> None:
        super().__init__()
        self.server = server
        self.seen: list[str | None] = []

    def refresh_credentials(self, account, settings, http, clock):
        self.seen.append(account.refresh_token)
        now = clock.now()
        if account.token_expires_at is not None and account.token_expires_at - now > timedelta(hours=1):
            return None
        new = self.server.refresh(account.refresh_token)
        return TokenUpdate(
            access_token=f"A-{new}",
            token_expires_at=now + timedelta(hours=24),
            refresh_token=new,
            refresh_expires_at=now + timedelta(days=365),
        )


def expired_tiktok(db, clock) -> Account:
    account = make_account(db, network="tiktok")
    acc = db.get(Account, account.id)
    acc.set_tokens("A0", "R1", token_expires_at=clock.now() - timedelta(hours=2))
    db.commit()
    return account


def test_engine_refreshes_from_the_tokens_stored_meanwhile(app, db, clock, sched, monkeypatch):
    server = RotatingServer()
    provider = registry.register(RotatingTikTok(server))
    account = expired_tiktok(db, clock)
    target = make_target(db, account, at=clock.now(), options={"demo_steps": 1})
    original = engine._Runner._load

    def load_then_job_refreshes(self):
        info = original(self)  # this run now holds R1…
        with app.state.db.session() as s:  # …and the token job rotates it to R2 right then
            acc = s.get(Account, account.id)
            acc.set_tokens("A-R2", server.refresh("R1"), token_expires_at=clock.now() + timedelta(hours=24))
            s.commit()
        return info

    monkeypatch.setattr(engine._Runner, "_load", load_then_job_refreshes)
    assert sched.tick().outcomes[target.id] == "published"
    assert provider.seen == ["R2"]  # never refreshed again with the superseded R1
    assert server.calls == ["R1"]
    db.expire_all()
    acc = db.get(Account, account.id)
    assert acc.status == AccountStatus.CONNECTED and acc.refresh_token == "R2"


def test_token_job_and_publishing_run_never_refresh_with_the_same_token(app, db, clock):
    server = RotatingServer()
    registry.register(RotatingTikTok(server))
    account = expired_tiktok(db, clock)
    target = make_target(db, account, at=clock.now() - timedelta(minutes=5), options={"demo_steps": 1})
    jobs = [Job("token_refresh", "x", job_token_refresh, interval=timedelta(hours=1))]
    sched = Scheduler(app.state.db, clock, inline=False, enable_jobs=True, jobs=jobs, rng=random.Random(3))
    try:
        report = sched.tick()  # launches the publishing worker and the token job in the same tick
        assert report.claimed == [target.id] and report.jobs == ["token_refresh"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with sched._lock:
                busy = bool(sched._workers) or (
                    sched._jobs_thread is not None and sched._jobs_thread.is_alive()
                )
            if not busy:
                break
            time.sleep(0.05)
    finally:
        sched.close()
    assert server.calls == ["R1"]  # one refresh; the other caller saw the stored R2
    db.expire_all()
    acc = db.get(Account, account.id)
    assert acc.status == AccountStatus.CONNECTED
    assert acc.refresh_token == server.valid
    assert fresh(db, target).status == TargetStatus.PUBLISHED


def test_expiry_warning_keeps_account_meta_written_meanwhile(app, db, clock, brand):
    class MetaWritingDuringRefresh(DemoProvider):
        def refresh_credentials(self, account, settings, http, clock):
            # A publishing run merges YouTube's lock status into accounts.meta meanwhile.
            with app.state.db.session() as s:
                acc = s.get(Account, account.id)
                acc.meta = {**(acc.meta or {}), "youtube_lock_status": "unlocked"}
                s.commit()
            return None

    registry.register(MetaWritingDuringRefresh())
    account = make_account(db, brand=brand)
    acc = db.get(Account, account.id)
    acc.meta = {"keep": 1}
    acc.set_tokens("a", "r", refresh_expires_at=clock.now() + timedelta(days=2))
    db.commit()
    sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    try:
        assert job_token_refresh(sched, clock.now())["warnings"] == 1
    finally:
        sched.close()
    db.expire_all()
    meta = db.get(Account, account.id).meta
    assert meta["keep"] == 1 and meta["youtube_lock_status"] == "unlocked"
    assert "refresh_expiry_warned" in meta


# ------------------------------------------------------------------------- 5. stale notifications


def test_publish_now_resolves_the_missed_notification(db, clock, demo_account, sched):
    registry.register(CountingDemo())
    target = make_target(db, demo_account, at=clock.now() - timedelta(hours=5), options={"demo_steps": 1})
    assert engine.set_missed(db, fresh(db, target), clock.now(), grace=timedelta(minutes=120))
    assert unread(db, target) == ["missed"]
    engine.publish_now(db, target.post_id, clock)
    assert unread(db, target) == []
    assert sched.tick().outcomes[target.id] == "published"


def test_editing_an_errored_post_resolves_its_notifications(db, brand, demo_mode, demo_account, clock):
    future = clock.now() + timedelta(days=1)
    post = postsvc.create_post(
        db, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    target = post.targets[0]
    target.status = TargetStatus.ERROR
    target.last_error = "Fallo"
    db.add(Notification(kind=NotificationKind.ERROR, title="No se pudo publicar", target_id=target.id))
    db.add(Notification(kind=NotificationKind.RECONNECT, title="Reconecta", target_id=target.id))
    db.commit()
    postsvc.update_post(
        db, post.id, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    assert unread(db, target) == ["reconnect"]  # account problems are not the post's to resolve
    assert fresh(db, target).status == TargetStatus.SCHEDULED


def test_successful_run_resolves_earlier_error_notifications(db, clock, demo_account, sched):
    registry.register(CountingDemo())
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    db.add(Notification(kind=NotificationKind.ERROR, title="No se pudo publicar", target_id=target.id))
    db.commit()
    assert sched.tick().outcomes[target.id] == "published"
    assert unread(db, target) == []


# --------------------------------------------------------------------------- 6. reconnect message


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "La cuenta no tiene un acceso válido a Meta. Vuelve a conectarla en Cuentas conectadas.",
            "@cuenta: La cuenta no tiene un acceso válido a Meta. Vuelve a conectarla en Cuentas conectadas.",
        ),
        (
            "La sesión de TikTok ha caducado. Reconéctala.",
            "@cuenta: La sesión de TikTok ha caducado. Reconéctala.",
        ),
        (
            "Permiso revocado en la red.",
            "@cuenta: Permiso revocado en la red. Ve a Cuentas conectadas para volver a conectarla.",
        ),
        (
            "La cuenta se ha desconectado.",
            "@cuenta: La cuenta se ha desconectado. Ve a Cuentas conectadas para volver a conectarla.",
        ),
    ],
)
def test_reconnect_notification_does_not_repeat_the_advice(message, expected):
    title, body = engine._msg_reconnect("instagram", "@cuenta", message)
    assert title == "Reconecta tu cuenta de Instagram"
    assert body == expected


def test_youtube_reconnect_message_names_the_real_screen():
    from planificador.providers import youtube

    assert "Cuentas conectadas" in youtube.MSG_RECONNECT and "Conexiones" not in youtube.MSG_RECONNECT


# ------------------------------------------------------------ 7. postsvc writes vs a concurrent claim


@pytest.fixture
def claim_during(app, monkeypatch):
    """Make the scheduler claim ``target`` (in its own session) right after postsvc read the post."""

    def arm(target: PostTarget, *, waiting: bool = False) -> None:
        real_now = postsvc._now

        def now_and_claim(clock):
            now = real_now(clock)
            with app.state.db.session() as s:
                t = s.get(PostTarget, target.id)
                if waiting:
                    assert engine.claim_waiting(s, target.id, t.next_attempt_at, now)
                else:
                    assert engine.claim_due(s, target.id, now)
            monkeypatch.setattr(postsvc, "_now", real_now)  # once
            return now

        monkeypatch.setattr(postsvc, "_now", now_and_claim)

    return arm


def scheduled_post(db, brand, demo_account, clock) -> Post:
    return postsvc.create_post(
        db,
        data(brand, demo_target(demo_account), scheduled_at=clock.now() + timedelta(days=1)),
        "schedule",
        clock,
    )


def test_save_as_draft_does_not_overwrite_a_claim(db, brand, demo_mode, demo_account, clock, claim_during):
    post = scheduled_post(db, brand, demo_account, clock)
    target = post.targets[0]
    claim_during(target)
    with pytest.raises(PostError) as info:
        postsvc.update_post(db, post.id, data(brand, demo_target(demo_account)), "draft", clock)
    assert info.value.code == "conflict"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING and t.claimed_at is not None


def test_reschedule_does_not_move_a_claimed_post(db, brand, demo_mode, demo_account, clock, claim_during):
    post = scheduled_post(db, brand, demo_account, clock)
    target = post.targets[0]
    before = fresh(db, target).scheduled_at
    claim_during(target)
    with pytest.raises(PostError) as info:
        postsvc.reschedule_post(db, post.id, clock.now() + timedelta(days=3), clock)
    assert info.value.code == "conflict"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING and t.scheduled_at == before


def test_trash_does_not_cancel_a_target_claimed_meanwhile(
    db, brand, demo_mode, demo_account, clock, claim_during
):
    post = scheduled_post(db, brand, demo_account, clock)
    target = post.targets[0]
    t = fresh(db, target)  # waiting before a retry (nothing sent): normally trashable
    t.status = TargetStatus.PUBLISHING
    t.next_attempt_at = clock.now()
    db.commit()
    claim_during(target, waiting=True)
    with pytest.raises(PostError) as info:
        postsvc.delete_post(db, post.id, clock)
    assert info.value.code == "conflict"
    db.expire_all()
    assert db.get(Post, post.id).deleted_at is None
    assert fresh(db, target).status == TargetStatus.PUBLISHING


def test_guard_lets_unchanged_posts_through(db, brand, demo_mode, demo_account, clock):
    post = scheduled_post(db, brand, demo_account, clock)
    postsvc.update_post(db, post.id, data(brand, demo_target(demo_account)), "draft", clock)
    assert fresh(db, post.targets[0]).status == TargetStatus.DRAFT
    postsvc.delete_post(db, post.id, clock)
    db.expire_all()
    assert db.get(Post, post.id).deleted_at is not None


# --------------------------------------------------------------- 8. restore re-schedules native targets


def test_restore_after_trash_reschedules_the_native_target(db, brand, clock, sched):
    provider = registry.register(NativeFacebook())
    registry.register(CountingDemo())
    fb = make_account(db, network="facebook", brand=brand)
    demo = make_account(db, network="demo", brand=brand)
    due = clock.now() + timedelta(days=3)
    post = Post(brand_id=brand.id, text="Hola", scheduled_at=due)
    db.add(post)
    db.flush()
    native = PostTarget(
        post_id=post.id,
        account_id=fb.id,
        network="facebook",
        format="post",
        options={"native_schedule": True},
        status=TargetStatus.PUBLISHED,
        scheduled_at=due,
        provider_state={"native_scheduled": True, "result": {"external_id": "1_2"}},
        external_id="1_2",
        commit_phase=CommitPhase.COMMIT_SENT,
        published_at=due,
    )
    other = PostTarget(
        post_id=post.id,
        account_id=demo.id,
        network="demo",
        format="post",
        status=TargetStatus.SCHEDULED,
        scheduled_at=due,
    )
    db.add_all([native, other])
    db.commit()

    postsvc.delete_post(db, post.id, clock)
    assert provider.cancelled == [native.id]
    t = fresh(db, native)
    assert t.status == TargetStatus.CANCELLED
    assert t.provider_state[postsvc.TRASH_STATE_KEY] == postsvc.TRASH_NATIVE

    postsvc.restore_post(db, post.id, clock)
    t = fresh(db, native)
    assert t.status == TargetStatus.SCHEDULED
    assert t.provider_state == {} and t.commit_phase == CommitPhase.NONE
    assert t.external_id is None and t.published_at is None
    assert fresh(db, other).status == TargetStatus.SCHEDULED
    restored = [e for e in t.events if e.event == "restored"][-1]
    assert "Se volverá a programar en Facebook" in restored.message
    # The scheduler hands it to Facebook's scheduler again.
    report = sched.tick()
    assert report.native == [native.id]


def test_restore_native_target_after_its_time_becomes_a_draft(db, brand, clock):
    registry.register(NativeFacebook())
    fb = make_account(db, network="facebook", brand=brand)
    target = native_published(db, clock, fb, days=1)
    postsvc.delete_post(db, target.post_id, clock)
    clock.advance(days=2)
    postsvc.restore_post(db, target.post_id, clock)
    t = fresh(db, target)
    assert t.status == TargetStatus.DRAFT and t.provider_state == {} and t.external_id is None

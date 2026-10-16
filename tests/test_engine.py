"""Publishing engine tests (SPEC §10) with the demo provider and a manual clock.

Scenarios: happy path and waits, retries/backoff, rate limits, auth → reconnect, permanent errors, ambiguous
outcomes and reconcile (never a second commit unless the network confirms nothing went out), crash recovery
(stale claims), manual mode, TikTok inbox mode, missed vs late, token refresh, and the route-facing API
(publish_now / retry_target / cancel_target / mark_done_manually). Plus keepawake.
"""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config, engine, keepawake
from planificador.engine import EngineError
from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    CommitPhase,
    MediaAsset,
    MediaKind,
    Notification,
    Post,
    PostEvent,
    PostKind,
    PostTarget,
    TargetStatus,
)
from planificador.providers import registry
from planificador.providers.base import ErrorKind, ProviderError, PublishContext, StepResult
from planificador.providers.demo import DemoProvider
from planificador.scheduler import HEARTBEAT_KEY, ONLINE_SINCE_KEY, Scheduler

# --------------------------------------------------------------------------------------------- helpers


class SimulatedCrash(BaseException):
    """Stands in for the process dying (not an ``Exception``: nothing in the engine may swallow it)."""


class CountingDemo(DemoProvider):
    """Demo provider that counts steps, irreversible commits and reconciles."""

    def __init__(self) -> None:
        super().__init__()
        self.steps = 0
        self.commits = 0
        self.reconciles = 0
        self.crash_after_commit = False
        self.remote_accepts_before_crash = False

    def publish_step(self, ctx: PublishContext) -> StepResult:
        self.steps += 1
        original = ctx.before_commit

        def counting(state: dict) -> None:
            original(state)
            self.commits += 1
            if self.crash_after_commit:
                self.crash_after_commit = False
                if self.remote_accepts_before_crash:
                    # The network accepted the post, then the app died before hearing back.
                    ctx.state["remote_published"] = True
                    ctx.save_state(ctx.state)
                raise SimulatedCrash()

        ctx.before_commit = counting
        try:
            return super().publish_step(ctx)
        finally:
            ctx.before_commit = original

    def reconcile(self, ctx: PublishContext):
        self.reconciles += 1
        return super().reconcile(ctx)


class FakeTikTok(CountingDemo):
    network = "tiktok"
    display_name = "TikTok"

    def validate(self, target):
        return []


class NativeFacebook(CountingDemo):
    network = "facebook"
    display_name = "Facebook"

    def __init__(self, supports_cancel: bool = True) -> None:
        super().__init__()
        self.cancelled: list[int] = []
        if not supports_cancel:
            self.cancel_native = None  # type: ignore[assignment]

    def validate(self, target):
        return []

    def cancel_native(self, ctx: PublishContext) -> None:  # type: ignore[override]
        self.cancelled.append(ctx.target_id)


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANIFICADOR_NO_DESKTOP_NOTIFY", "1")


@pytest.fixture
def demo() -> CountingDemo:
    return registry.register(CountingDemo())  # type: ignore[return-value]


@pytest.fixture
def sched(app, clock):
    scheduler = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(7))
    yield scheduler
    scheduler.close()


def restart(app, clock) -> Scheduler:
    """A new scheduler instance on the same database (what a process restart looks like)."""
    return Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(11))


def make_account(db: Session, *, network: str = "demo", brand: Brand | None = None) -> Account:
    if brand is None:
        brand = Brand(name=f"Marca {network}")
        db.add(brand)
        db.flush()
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=f"{network}-{brand.id}",
        name=f"Cuenta {network}",
        username=f"user_{network}",
        account_type="demo",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens("access-token", "refresh-token")
    db.add(account)
    db.commit()
    return account


def make_target(
    db: Session,
    account: Account,
    *,
    at,
    options: dict | None = None,
    auto_publish: bool = True,
    status: TargetStatus = TargetStatus.SCHEDULED,
    text: str = "Hola mundo desde el planificador",
    fmt: str | None = None,
    kind: PostKind = PostKind.SCHEDULED,
) -> PostTarget:
    post = Post(brand_id=account.brand_id, text=text, scheduled_at=at, kind=kind)
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=account.id,
        network=account.network,
        format=fmt or ("video" if str(account.network) == "tiktok" else "post"),
        options=dict(options or {}),
        auto_publish=auto_publish,
        status=status,
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


def events(db: Session, target: PostTarget) -> list[str]:
    return list(
        db.scalars(select(PostEvent.event).where(PostEvent.target_id == target.id).order_by(PostEvent.id))
    )


def notifications(db: Session) -> list[Notification]:
    db.expire_all()
    return list(db.scalars(select(Notification).order_by(Notification.id)))


def advance_to_next_attempt(db: Session, clock, target: PostTarget) -> None:
    t = fresh(db, target)
    assert t.next_attempt_at is not None
    clock.set(t.next_attempt_at)


# -------------------------------------------------------------------------------------- happy paths


def test_happy_path_with_one_wait(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now())

    report = sched.tick()
    assert report.claimed == [target.id]
    assert report.outcomes[target.id] == "wait"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING
    assert t.next_attempt_at == clock.now() + timedelta(seconds=1)
    assert t.claimed_at is None

    # Not yet: the wait has not elapsed.
    assert sched.tick().continued == []

    clock.advance(seconds=1)
    report = sched.tick()
    assert report.continued == [target.id]
    assert report.outcomes[target.id] == "published"

    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED
    assert t.external_id == f"demo-{target.id}"
    assert t.external_url is None
    assert t.published_at == clock.now()
    assert t.commit_phase == CommitPhase.COMMIT_SENT
    assert t.next_attempt_at is None and t.last_error is None
    assert "_engine" not in t.provider_state
    assert demo.commits == 1
    assert events(db, target) == ["publishing", "demo_processing", "waiting", "commit_sent", "published"]
    assert notifications(db) == []  # successful publishes only leave an event


def test_single_step_publishes_in_one_tick(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    report = sched.tick()
    assert report.outcomes == {target.id: "published"}
    assert fresh(db, target).status == TargetStatus.PUBLISHED
    assert demo.commits == 1


def test_several_waits_and_deduplicated_wait_events(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 4})
    outcomes = []
    for _ in range(4):
        report = sched.tick()
        outcomes.append(report.outcomes.get(target.id))
        if fresh(db, target).status == TargetStatus.PUBLISHED:
            break
        advance_to_next_attempt(db, clock, target)
    assert outcomes == ["wait", "wait", "wait", "published"]
    assert events(db, target).count("waiting") == 1  # same message → one event
    assert demo.commits == 1


def test_future_and_trashed_posts_are_ignored(db, clock, demo_account, demo, sched):
    future = make_target(db, demo_account, at=clock.now() + timedelta(minutes=10))
    trashed = make_target(db, demo_account, at=clock.now() - timedelta(minutes=1))
    post = db.get(Post, trashed.post_id)
    post.deleted_at = clock.now()
    db.commit()
    draft = make_target(db, demo_account, at=clock.now(), status=TargetStatus.DRAFT)

    report = sched.tick()
    assert report.claimed == []
    assert fresh(db, future).status == TargetStatus.SCHEDULED
    assert fresh(db, trashed).status == TargetStatus.SCHEDULED
    assert fresh(db, draft).status == TargetStatus.DRAFT
    assert demo.steps == 0


def test_claim_is_atomic(app, db, clock, demo_account):
    target = make_target(db, demo_account, at=clock.now())
    other = app.state.db.session()
    try:
        assert engine.claim_due(db, target.id, clock.now()) is True
        assert engine.claim_due(other, target.id, clock.now()) is False
    finally:
        other.close()
    assert fresh(db, target).status == TargetStatus.PUBLISHING


def test_run_target_skips_targets_not_publishing(app, db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now())
    assert engine.run_target(target.id, sched.runtime) == "skipped"
    assert engine.run_target(999_999, sched.runtime) == "skipped"
    assert demo.steps == 0


# ------------------------------------------------------------------------------------ retries/backoff


def test_transient_errors_back_off_then_fail(db, clock, demo_account, demo, sched):
    target = make_target(
        db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "transient"}
    )

    for attempt, base in enumerate(engine.RETRY_DELAYS_S, start=1):
        report = sched.tick()
        assert report.outcomes[target.id] == "retry"
        t = fresh(db, target)
        assert t.status == TargetStatus.PUBLISHING
        assert t.attempts == attempt
        delay = (t.next_attempt_at - clock.now()).total_seconds()
        assert base * 0.8 <= delay <= base * 1.2, (attempt, delay)
        assert events(db, target)[-1] == "retry_scheduled"
        last_msg = db.scalars(
            select(PostEvent.message).where(PostEvent.target_id == target.id).order_by(PostEvent.id.desc())
        ).first()
        assert f"Reintento {attempt} de {engine.MAX_RETRIES} a las" in last_msg
        assert t.last_error == "Fallo temporal simulado en la red Demo. Se reintentará."
        # Nothing happens before the backoff elapses.
        clock.advance(seconds=delay / 2)
        assert target.id not in sched.tick().outcomes
        advance_to_next_attempt(db, clock, target)

    report = sched.tick()
    assert report.outcomes[target.id] == "error"
    t = fresh(db, target)
    assert t.status == TargetStatus.ERROR
    assert t.attempts == engine.MAX_RETRIES + 1
    assert "tras 5 intentos" in t.last_error
    assert t.last_error_detail["code"] == "demo_transient"
    assert demo.commits == 0
    kinds = [n.kind for n in notifications(db)]
    assert kinds == ["error"]
    assert notifications(db)[0].title == "No se pudo publicar en Demo"
    assert events(db, target).count("retry_scheduled") == 4


def test_transient_then_success(db, clock, demo_account, demo, sched):
    target = make_target(
        db,
        demo_account,
        at=clock.now(),
        options={"demo_steps": 1, "demo_fail": "transient", "demo_fail_times": 2},
    )
    sched.tick()
    advance_to_next_attempt(db, clock, target)
    sched.tick()
    advance_to_next_attempt(db, clock, target)
    report = sched.tick()
    assert report.outcomes[target.id] == "published"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED and t.attempts == 2 and t.last_error is None
    assert demo.commits == 1


def test_retry_delay_policy():
    rng = random.Random(3)
    for attempt, base in enumerate(engine.RETRY_DELAYS_S, start=1):
        for _ in range(50):
            seconds = engine.retry_delay(attempt, None, rng).total_seconds()
            assert base * 0.8 <= seconds <= base * 1.2
    assert engine.retry_delay(2, 120, rng) == timedelta(seconds=120)
    assert engine.retry_delay(9, None, rng).total_seconds() <= 1800 * 1.2
    assert engine.retry_delay(1, 10**9, rng) == timedelta(seconds=engine.MAX_RETRY_AFTER_S)


def test_rate_limited_uses_retry_after(db, clock, demo_account, demo, sched):
    target = make_target(
        db,
        demo_account,
        at=clock.now(),
        options={"demo_steps": 1, "demo_fail": "rate_limited", "demo_fail_times": 1},
    )
    report = sched.tick()
    assert report.outcomes[target.id] == "retry"
    t = fresh(db, target)
    assert t.next_attempt_at == clock.now() + timedelta(seconds=60)  # retry_after wins, no jitter
    clock.advance(seconds=60)
    assert sched.tick().outcomes[target.id] == "published"
    assert demo.commits == 1


def test_auth_error_flags_account_and_notifies(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "auth"})
    report = sched.tick()
    assert report.outcomes[target.id] == "error"
    t = fresh(db, target)
    assert t.status == TargetStatus.ERROR
    assert t.last_error == "La cuenta Demo necesita reconexión (simulado)."
    account = db.get(Account, demo_account.id)
    assert account.status == AccountStatus.NEEDS_RECONNECT
    assert account.last_error == t.last_error
    notes = notifications(db)
    assert [n.kind for n in notes] == ["reconnect"]
    assert notes[0].title == "Reconecta tu cuenta de Demo"
    assert notes[0].account_id == demo_account.id and notes[0].target_id == target.id
    assert demo.commits == 0

    # Another post for the same (now broken) account fails fast without calling the provider.
    steps = demo.steps
    second = make_target(db, demo_account, at=clock.now())
    report = sched.tick()
    assert report.outcomes[second.id] == "error"
    assert demo.steps == steps
    assert "necesita reconexión" in fresh(db, second).last_error
    assert [n.kind for n in notifications(db)] == ["reconnect", "error"]


def test_permanent_error_is_final(db, clock, demo_account, demo, sched):
    target = make_target(
        db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "permanent"}
    )
    assert sched.tick().outcomes[target.id] == "error"
    t = fresh(db, target)
    assert t.status == TargetStatus.ERROR
    assert t.attempts == 0 and t.next_attempt_at is None
    assert t.last_error == "La red Demo ha rechazado la publicación (simulado)."
    clock.advance(hours=2)
    assert sched.tick().outcomes == {}
    assert [n.kind for n in notifications(db)] == ["error"]


def test_unexpected_exception_before_commit_is_retried(db, clock, demo_account, sched):
    class Buggy(CountingDemo):
        def publish_step(self, ctx):
            self.steps += 1
            if self.steps == 1:
                raise KeyError("boom")
            return super().publish_step(ctx)

    provider = registry.register(Buggy())
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    assert sched.tick().outcomes[target.id] == "retry"
    assert fresh(db, target).last_error == "Error inesperado al publicar."
    advance_to_next_attempt(db, clock, target)
    assert sched.tick().outcomes[target.id] == "published"
    assert provider.commits == 1


# --------------------------------------------------------------------------- ambiguous / idempotency


def test_ambiguous_outcome_unknown_is_never_retried(db, clock, demo_account, demo, sched):
    target = make_target(
        db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "ambiguous"}
    )
    report = sched.tick()
    assert report.outcomes[target.id] == "unknown"
    t = fresh(db, target)
    assert t.status == TargetStatus.UNKNOWN
    assert t.commit_phase == CommitPhase.COMMIT_SENT
    assert t.last_error == "No se pudo confirmar en Demo si la publicación salió."
    assert demo.commits == 1 and demo.reconciles == 1
    notes = notifications(db)
    assert [n.kind for n in notes] == ["unknown"]
    assert notes[0].title == "Comprueba tu publicación en Demo"

    for _ in range(3):
        clock.advance(hours=1)
        assert sched.tick().outcomes == {}
    assert demo.commits == 1
    assert "ambiguous_error" in events(db, target) and events(db, target)[-1] == "unknown"


def test_ambiguous_then_reconcile_published(db, clock, demo_account, demo, sched):
    target = make_target(
        db,
        demo_account,
        at=clock.now(),
        options={"demo_steps": 1, "demo_fail": "ambiguous", "demo_reconcile": "published"},
    )
    assert sched.tick().outcomes[target.id] == "published"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED and t.external_id == f"demo-{target.id}"
    assert events(db, target)[-1] == "reconciled_published"
    assert demo.commits == 1
    assert notifications(db) == []


def test_ambiguous_not_published_allows_a_new_commit(db, clock, demo_account, demo, sched):
    target = make_target(
        db,
        demo_account,
        at=clock.now(),
        options={
            "demo_steps": 1,
            "demo_fail": "ambiguous",
            "demo_fail_times": 1,
            "demo_reconcile": "not_published",
        },
    )
    assert sched.tick().outcomes[target.id] == "retry"
    t = fresh(db, target)
    assert t.commit_phase == CommitPhase.NONE  # the network confirmed nothing went out
    assert t.attempts == 1
    advance_to_next_attempt(db, clock, target)
    assert sched.tick().outcomes[target.id] == "published"
    assert demo.commits == 2  # second commit only because reconcile said "not_published"
    assert "reconciled_not_published" in events(db, target)


def test_transient_after_commit_reconciles_instead_of_retrying(db, clock, demo_account, sched):
    class FlakyAfterCommit(CountingDemo):
        def publish_step(self, ctx):
            self.steps += 1
            ctx.before_commit(ctx.state)
            self.commits += 1
            raise ProviderError(ErrorKind.TRANSIENT, "timeout", "Tiempo de espera agotado.")

        def reconcile(self, ctx):
            self.reconciles += 1
            from planificador.providers.base import ReconcileResult

            return ReconcileResult("published", external_id="remote-1")

    provider = registry.register(FlakyAfterCommit())
    target = make_target(db, demo_account, at=clock.now())
    assert sched.tick().outcomes[target.id] == "published"
    assert provider.commits == 1 and provider.reconciles == 1
    assert fresh(db, target).external_id == "remote-1"


def test_reconcile_transient_is_retried_later_then_unknown(db, clock, demo_account, sched):
    class Unreachable(CountingDemo):
        def reconcile(self, ctx):
            self.reconciles += 1
            raise ProviderError(ErrorKind.TRANSIENT, "net", "Sin conexión con la red.")

    provider = registry.register(Unreachable())
    target = make_target(
        db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "ambiguous"}
    )
    assert sched.tick().outcomes[target.id] == "reconcile_retry"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING
    assert t.provider_state["_engine"]["pending"] == "reconcile"
    for _ in range(engine.MAX_RETRIES - 1):
        advance_to_next_attempt(db, clock, target)
        assert sched.tick().outcomes[target.id] == "reconcile_retry"
    advance_to_next_attempt(db, clock, target)
    assert sched.tick().outcomes[target.id] == "unknown"
    assert provider.commits == 1  # publish_step was never re-entered
    assert provider.reconciles == engine.MAX_RETRIES + 1


def test_crash_after_commit_is_reconciled_on_restart(app, db, clock, demo_account, demo, sched):
    demo.crash_after_commit = True
    demo.remote_accepts_before_crash = True
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    with pytest.raises(SimulatedCrash):
        sched.tick()
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING
    assert t.commit_phase == CommitPhase.COMMIT_SENT
    assert t.next_attempt_at is None
    assert not engine.active_runs()

    clock.advance(seconds=30)
    restarted = restart(app, clock)
    report = restarted.tick()
    restarted.close()
    assert report.recovered == [target.id]
    assert report.outcomes[target.id] == "published"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED
    assert demo.commits == 1  # never committed twice
    assert demo.reconciles == 1
    assert "recovered" in events(db, target)


def test_crash_after_commit_unknown_outcome(app, db, clock, demo_account, demo, sched):
    demo.crash_after_commit = True
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    with pytest.raises(SimulatedCrash):
        sched.tick()
    restarted = restart(app, clock)
    report = restarted.tick()
    restarted.close()
    assert report.outcomes[target.id] == "unknown"
    assert fresh(db, target).status == TargetStatus.UNKNOWN
    assert demo.commits == 1
    clock.advance(hours=1)
    assert restart(app, clock).tick().outcomes == {}
    assert demo.commits == 1


def test_stale_claim_before_commit_continues_after_ten_minutes(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now() - timedelta(minutes=5), options={"demo_steps": 2})
    sched.tick()  # first tick of this instance: nothing stale yet
    t = fresh(db, target)
    # Simulate a crash in the middle of a run: claimed, state saved, no wait scheduled.
    t.next_attempt_at = None
    t.claimed_at = clock.now()
    db.commit()

    clock.advance(minutes=5)
    report = sched.tick()
    assert report.recovered == [] and fresh(db, target).status == TargetStatus.PUBLISHING

    clock.advance(minutes=6)
    report = sched.tick()
    assert report.recovered == [target.id]
    assert report.outcomes[target.id] == "published"
    assert demo.commits == 1
    assert demo.reconciles == 0  # nothing had been committed: continue from saved state


def test_first_tick_after_restart_recovers_any_orphan(app, db, clock, demo_account, demo):
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    t = fresh(db, target)
    t.status = TargetStatus.PUBLISHING
    t.claimed_at = clock.now() - timedelta(seconds=20)  # recent, but its process is gone
    db.commit()
    scheduler = restart(app, clock)
    report = scheduler.tick()
    scheduler.close()
    assert report.recovered == [target.id]
    assert fresh(db, target).status == TargetStatus.PUBLISHED


# ------------------------------------------------------------------------------ manual / tiktok inbox


def test_manual_mode_notifies_and_never_calls_provider(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), auto_publish=False, text="Oferta de otoño 🍂")
    report = sched.tick()
    assert report.manual == [target.id] and report.claimed == []
    t = fresh(db, target)
    assert t.status == TargetStatus.MANUAL_PENDING
    assert demo.steps == 0
    notes = notifications(db)
    assert [n.kind for n in notes] == ["manual_publish"]
    assert notes[0].title == "Es hora de publicar en Demo"
    assert "Oferta de otoño" in notes[0].body and "publicación asistida" in notes[0].body
    assert notes[0].target_id == target.id
    assert events(db, target) == ["manual_pending"]
    assert sched.tick().manual == []  # only once


def test_manual_mode_even_when_late(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now() - timedelta(hours=5), auto_publish=False)
    assert sched.tick().manual == [target.id]


def test_tiktok_inbox_mode_goes_to_provider_even_without_autopublish(db, clock, sched):
    provider = registry.register(FakeTikTok())
    account = make_account(db, network="tiktok")
    target = make_target(
        db,
        account,
        at=clock.now(),
        auto_publish=False,
        options={
            "mode": "inbox",
            "demo_steps": 1,
            "demo_result": "sent_to_app",
            "privacy_level": "SELF_ONLY",
        },
    )
    report = sched.tick()
    assert report.claimed == [target.id]
    assert report.outcomes[target.id] == "sent_to_app"
    t = fresh(db, target)
    assert t.status == TargetStatus.SENT_TO_APP
    assert t.external_id == f"demo-{target.id}"
    assert provider.commits == 1
    notes = notifications(db)
    assert [n.kind for n in notes] == ["info"]
    assert notes[0].title == "Termina la publicación en TikTok"
    assert "Abre TikTok" in notes[0].body

    # TikTok direct mode with auto-publish off is a normal assisted post.
    direct = make_target(db, account, at=clock.now(), auto_publish=False, options={"mode": "direct"})
    assert sched.tick().manual == [direct.id]


# ------------------------------------------------------------------------------------- missed vs late


def _app_was_off_since(db: Session, last_tick) -> None:
    config.set_timestamp(db, HEARTBEAT_KEY, last_tick, commit=False)
    config.set_timestamp(db, ONLINE_SINCE_KEY, last_tick - timedelta(days=1), commit=False)
    db.commit()


def _old_late_rule(db: Session) -> None:
    """Turn late publishing off: the grace window / missed rule of before (Ajustes › Publicación)."""
    config.set_setting(db, "publish_late", False)


def test_missed_when_app_was_off_beyond_grace(db, clock, demo_account, demo, sched):
    _old_late_rule(db)
    due = clock.now()
    target = make_target(db, demo_account, at=due)
    _app_was_off_since(db, due - timedelta(hours=1))
    clock.set(due + timedelta(hours=3))

    report = sched.tick()
    assert report.gap is True
    assert report.missed == [target.id]
    t = fresh(db, target)
    assert t.status == TargetStatus.MISSED
    assert demo.steps == 0
    notes = notifications(db)
    assert [n.kind for n in notes] == ["missed"]
    assert notes[0].title == "Publicación perdida en Demo"
    assert "martes 3 de noviembre, 10:00" in notes[0].body  # brand time zone (Europe/Madrid)
    assert "publicarla ahora, reprogramarla o descartarla" in notes[0].body


def test_late_within_grace_is_published_with_a_note(db, clock, demo_account, demo, sched):
    _old_late_rule(db)
    due = clock.now()
    target = make_target(db, demo_account, at=due, options={"demo_steps": 1})
    _app_was_off_since(db, due - timedelta(hours=1))
    clock.set(due + timedelta(minutes=90))  # default grace: 120 min

    report = sched.tick()
    assert report.outcomes[target.id] == "published"
    assert "late" in events(db, target)
    late_msg = db.scalars(
        select(PostEvent.message).where(PostEvent.target_id == target.id, PostEvent.event == "late")
    ).one()
    assert late_msg == (
        "Se publica con 1 h 30 min de retraso: estaba programada para el martes 3 de noviembre, 10:00 y el "
        "equipo o la app no estaba disponible."
    )


def test_late_beyond_grace_while_app_was_running_is_not_missed(db, clock, demo_account, demo, sched):
    due = clock.now()
    target = make_target(db, demo_account, at=due, options={"demo_steps": 1})
    clock.set(due + timedelta(hours=3))
    # The app has been running continuously since before the due time (e.g. it was busy).
    config.set_timestamp(db, HEARTBEAT_KEY, clock.now() - timedelta(seconds=15), commit=False)
    config.set_timestamp(db, ONLINE_SINCE_KEY, due - timedelta(hours=1), commit=False)
    db.commit()

    report = sched.tick()
    assert report.gap is False
    assert report.missed == []
    assert report.outcomes[target.id] == "published"


def test_grace_minutes_setting(db, clock, demo_account, demo, sched):
    _old_late_rule(db)
    config.set_setting(db, "grace_minutes", 0)
    due = clock.now()
    target = make_target(db, demo_account, at=due)
    _app_was_off_since(db, due - timedelta(minutes=10))
    clock.set(due + timedelta(minutes=5))
    assert sched.tick().missed == [target.id]


# --------------------------------------------------------------------------------------- pre-flight


def test_token_is_refreshed_before_publishing(db, clock, demo_account, demo, sched):
    account = db.get(Account, demo_account.id)
    account.set_tokens("old-token", "refresh", token_expires_at=clock.now() + timedelta(minutes=30))
    db.commit()
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    assert sched.tick().outcomes[target.id] == "published"
    db.expire_all()
    account = db.get(Account, demo_account.id)
    assert account.access_token.startswith(f"demo-access-{account.id}-")
    assert account.access_token_enc.startswith("v1:")  # stored encrypted
    assert account.refresh_token == "refresh"  # kept
    assert account.token_expires_at == clock.now() + timedelta(days=60)
    assert "token_refreshed" in events(db, target)


def test_auth_error_during_refresh(db, clock, demo_account, sched):
    class BrokenRefresh(CountingDemo):
        def refresh_credentials(self, account, settings, http, clock):
            raise ProviderError(ErrorKind.AUTH, "revoked", "El acceso fue revocado.")

    provider = registry.register(BrokenRefresh())
    target = make_target(db, demo_account, at=clock.now())
    assert sched.tick().outcomes[target.id] == "error"
    db.expire_all()
    assert db.get(Account, demo_account.id).status == AccountStatus.NEEDS_RECONNECT
    assert provider.steps == 0


def test_validation_errors_fail_before_publishing(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), text="   ")
    assert sched.tick().outcomes[target.id] == "error"
    t = fresh(db, target)
    assert t.last_error == "Escribe un texto o añade al menos un archivo."
    assert t.last_error_detail["code"] == "validation"
    assert demo.steps == 0


def test_missing_media_file_is_an_error(db, clock, demo_account, demo, sched):
    asset = MediaAsset(
        kind=MediaKind.IMAGE,
        original_name="foto.jpg",
        rel_path="originals/ab/missing.jpg",
        sha256="a" * 64,
        mime="image/jpeg",
        size_bytes=10,
    )
    db.add(asset)
    db.commit()
    target = make_target(db, demo_account, at=clock.now())
    post = db.get(Post, target.post_id)
    post.set_media([asset])
    db.commit()
    assert sched.tick().outcomes[target.id] == "error"
    t = fresh(db, target)
    assert "foto.jpg" in t.last_error
    assert t.last_error_detail["code"] == "media_missing"


def test_media_info_and_text_reach_the_provider(app, db, clock, demo_account, sched):
    seen: dict = {}

    class Recorder(CountingDemo):
        def publish_step(self, ctx):
            seen["text"] = ctx.target.text
            seen["media"] = [(m.id, m.path, m.mime, m.width) for m in ctx.target.media]
            seen["options"] = ctx.target.options
            seen["token"] = ctx.account.access_token
            seen["public_media"] = ctx.public_media
            seen["base_url"] = ctx.settings.base_url
            return super().publish_step(ctx)

    registry.register(Recorder())
    media_dir: Path = app.state.paths.media_dir
    rel = "originals/cd/" + "c" * 64 + ".png"
    (media_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (media_dir / rel).write_bytes(b"\x89PNG fake")
    asset = MediaAsset(
        kind=MediaKind.IMAGE,
        original_name="logo.png",
        rel_path=rel,
        sha256="c" * 64,
        mime="image/png",
        size_bytes=9,
        width=64,
        height=64,
    )
    db.add(asset)
    db.commit()
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    t = fresh(db, target)
    t.text_override = "Texto propio de la red"
    t.media_override = [asset.id]
    db.commit()

    assert sched.tick().outcomes[target.id] == "published"
    assert seen["text"] == "Texto propio de la red"
    assert seen["media"] == [(asset.id, media_dir / rel, "image/png", 64)]
    assert seen["options"]["demo_steps"] == 1
    assert seen["token"] == "demo-access-token"
    assert seen["public_media"] is None
    assert seen["base_url"] == "http://localhost:8765"


def test_disconnected_account_fails(db, clock, demo_account, demo, sched):
    account = db.get(Account, demo_account.id)
    account.status = AccountStatus.DISCONNECTED
    db.commit()
    target = make_target(db, demo_account, at=clock.now())
    assert sched.tick().outcomes[target.id] == "error"
    assert "desconectada" in fresh(db, target).last_error
    assert demo.steps == 0


# --------------------------------------------------------------------------------------- public API


def test_publish_now_schedules_pending_targets_and_wakes(db, clock, demo_account, demo, sched, monkeypatch):
    woken = []
    monkeypatch.setattr("planificador.scheduler.wake", lambda: woken.append(True))
    draft = make_target(
        db,
        demo_account,
        at=clock.now() + timedelta(days=2),
        status=TargetStatus.DRAFT,
        kind=PostKind.IDEA,
        options={"demo_steps": 1},
    )
    engine.publish_now(db, draft.post_id, clock)
    assert woken == [True]
    t = fresh(db, draft)
    assert t.status == TargetStatus.SCHEDULED
    assert t.scheduled_at == clock.now()
    post = db.get(Post, draft.post_id)
    assert post.scheduled_at == clock.now()
    assert post.kind == PostKind.SCHEDULED and post.idea_stage is None
    assert "publish_now" in events(db, draft)

    report = sched.tick()
    assert report.outcomes[draft.id] == "published"


def test_publish_now_errors(db, clock, demo_account):
    published = make_target(db, demo_account, at=clock.now(), status=TargetStatus.PUBLISHED)
    with pytest.raises(EngineError) as info:
        engine.publish_now(db, published.post_id, clock)
    assert info.value.code == "nothing_to_publish"
    with pytest.raises(EngineError):
        engine.publish_now(db, 424242, clock)
    trashed = make_target(db, demo_account, at=clock.now(), status=TargetStatus.DRAFT)
    db.get(Post, trashed.post_id).deleted_at = clock.now()
    db.commit()
    with pytest.raises(EngineError) as info:
        engine.publish_now(db, trashed.post_id, clock)
    assert info.value.code == "not_found"


def test_publish_now_keeps_commit_sent_targets_safe(db, clock, demo_account, demo, sched):
    target = make_target(
        db, demo_account, at=clock.now(), status=TargetStatus.ERROR, options={"demo_steps": 1}
    )
    t = fresh(db, target)
    t.commit_phase = CommitPhase.COMMIT_SENT
    t.provider_state = {"remote_published": True, "commit_attempts": 1}
    db.commit()
    engine.publish_now(db, target.post_id, clock)
    t = fresh(db, target)
    assert t.provider_state["_engine"] == {"pending": "reconcile"}
    report = sched.tick()
    assert report.outcomes[target.id] == "published"
    assert demo.commits == 0 and demo.reconciles == 1  # confirmed, not re-sent


def test_retry_target_from_error(db, clock, demo_account, demo, sched):
    target = make_target(
        db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "permanent"}
    )
    sched.tick()
    assert fresh(db, target).status == TargetStatus.ERROR
    t = fresh(db, target)
    t.options = {"demo_steps": 1}  # the user fixed the problem
    db.commit()

    clock.advance(hours=4)
    engine.retry_target(db, target.id, clock)
    t = fresh(db, target)
    assert t.status == TargetStatus.SCHEDULED
    assert t.next_attempt_at == clock.now() and t.attempts == 0 and t.last_error is None
    assert t.provider_state == {}
    assert all(n.read_at is not None for n in notifications(db))  # the error notice is resolved

    report = sched.tick()  # 4 h after the original time, but an explicit retry is never "missed"
    assert report.missed == []
    assert report.outcomes[target.id] == "published"


def test_retry_target_from_missed(db, clock, demo_account, demo, sched):
    _old_late_rule(db)
    due = clock.now()
    target = make_target(db, demo_account, at=due, options={"demo_steps": 1})
    _app_was_off_since(db, due - timedelta(hours=1))
    clock.set(due + timedelta(hours=5))
    assert sched.tick().missed == [target.id]
    engine.retry_target(db, target.id, clock)
    assert sched.tick().outcomes[target.id] == "published"


def test_retry_unknown_requires_confirmation(db, clock, demo_account, demo, sched):
    target = make_target(
        db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "ambiguous"}
    )
    sched.tick()
    assert fresh(db, target).status == TargetStatus.UNKNOWN
    with pytest.raises(EngineError) as info:
        engine.retry_target(db, target.id, clock)
    assert info.value.code == "confirm_required"

    engine.retry_target(db, target.id, clock, confirmed_not_published=True)
    t = fresh(db, target)
    assert t.status == TargetStatus.SCHEDULED
    assert t.commit_phase == CommitPhase.NONE and t.provider_state == {}
    t.options = {"demo_steps": 1}
    db.commit()
    assert sched.tick().outcomes[target.id] == "published"
    assert demo.commits == 2


def test_retry_rejects_other_states(db, clock, demo_account):
    target = make_target(db, demo_account, at=clock.now())
    with pytest.raises(EngineError) as info:
        engine.retry_target(db, target.id, clock)
    assert info.value.code == "invalid_state"
    with pytest.raises(EngineError) as info:
        engine.retry_target(db, 5555, clock)
    assert info.value.code == "not_found"


def test_cancel_scheduled_and_idempotent(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now() + timedelta(hours=1))
    engine.cancel_target(db, target.id, clock)
    assert fresh(db, target).status == TargetStatus.CANCELLED
    engine.cancel_target(db, target.id, clock)  # no-op
    assert events(db, target) == ["cancelled"]
    clock.advance(hours=2)
    assert sched.tick().claimed == []


def test_cancel_waiting_target(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 3})
    sched.tick()
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING and t.next_attempt_at is not None
    engine.cancel_target(db, target.id, clock)
    assert fresh(db, target).status == TargetStatus.CANCELLED
    clock.advance(seconds=5)
    assert sched.tick().continued == []
    assert demo.commits == 0


def test_cancel_refused_while_committing_or_published(db, clock, demo_account):
    busy = make_target(db, demo_account, at=clock.now(), status=TargetStatus.PUBLISHING)
    with pytest.raises(EngineError) as info:
        engine.cancel_target(db, busy.id, clock)
    assert info.value.code == "busy"
    t = fresh(db, busy)
    t.next_attempt_at = clock.now() + timedelta(seconds=5)
    t.commit_phase = CommitPhase.COMMIT_SENT  # waiting for the network after the commit
    db.commit()
    with pytest.raises(EngineError):
        engine.cancel_target(db, busy.id, clock)

    done = make_target(db, demo_account, at=clock.now(), status=TargetStatus.PUBLISHED)
    with pytest.raises(EngineError) as info:
        engine.cancel_target(db, done.id, clock)
    assert info.value.code == "already_published"


def test_cancel_native_scheduled_post_deletes_it_on_the_network(db, clock):
    provider = registry.register(NativeFacebook())
    account = make_account(db, network="facebook")
    target = make_target(db, account, at=clock.now() + timedelta(days=1), status=TargetStatus.PUBLISHED)
    t = fresh(db, target)
    t.provider_state = {"native_scheduled": True, "post_id": "123_456"}
    t.external_id = "123_456"
    db.commit()

    engine.cancel_target(db, target.id, clock)
    assert provider.cancelled == [target.id]
    assert fresh(db, target).status == TargetStatus.CANCELLED
    assert events(db, target) == ["native_cancelled", "cancelled"]


def test_cancel_native_without_provider_support_is_refused(db, clock):
    registry.register(NativeFacebook(supports_cancel=False))
    account = make_account(db, network="facebook")
    target = make_target(db, account, at=clock.now() + timedelta(days=1), status=TargetStatus.PUBLISHED)
    t = fresh(db, target)
    t.provider_state = {"native_scheduled": True}
    db.commit()
    # Still scheduled on Facebook: never reported as cancelled.
    with pytest.raises(EngineError) as info:
        engine.cancel_target(db, target.id, clock)
    assert info.value.code == "native_cancel_failed"
    assert "bórrala directamente en Facebook" in info.value.message
    assert fresh(db, target).status == TargetStatus.PUBLISHED
    assert events(db, target) == ["native_cancel_failed"]
    # The user confirmed they deleted it on Facebook themselves: cancelled here, with a warning.
    engine.cancel_target(db, target.id, clock, force=True)
    assert fresh(db, target).status == TargetStatus.CANCELLED
    assert events(db, target) == ["native_cancel_failed", "cancelled", "native_cancel"]

    # Once its time has passed, a natively scheduled post is live: not cancellable.
    live = make_target(db, account, at=clock.now() - timedelta(minutes=1), status=TargetStatus.PUBLISHED)
    t = fresh(db, live)
    t.provider_state = {"native_scheduled": True}
    db.commit()
    with pytest.raises(EngineError):
        engine.cancel_target(db, live.id, clock)


def test_mark_done_manually(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), auto_publish=False)
    sched.tick()
    assert fresh(db, target).status == TargetStatus.MANUAL_PENDING

    with pytest.raises(EngineError) as info:
        engine.mark_done_manually(db, target.id, clock, external_url="javascript:alert(1)")
    assert info.value.code == "invalid_url"

    clock.advance(minutes=3)
    engine.mark_done_manually(db, target.id, clock, external_url="https://example.com/p/1")
    t = fresh(db, target)
    assert t.status == TargetStatus.MANUAL_DONE
    assert t.external_url == "https://example.com/p/1"
    assert t.published_at == clock.now()
    assert all(n.read_at is not None for n in notifications(db))
    # Updating the link later keeps the original publication time.
    clock.advance(minutes=3)
    engine.mark_done_manually(db, target.id, clock, external_url="https://example.com/p/2")
    t = fresh(db, target)
    assert t.external_url == "https://example.com/p/2" and t.published_at == clock.now() - timedelta(
        minutes=3
    )

    scheduled = make_target(db, demo_account, at=clock.now() + timedelta(hours=1))
    with pytest.raises(EngineError):
        engine.mark_done_manually(db, scheduled.id, clock)


def test_stop_event_pauses_between_steps(app, db, clock, demo_account):
    class TwoSteps(CountingDemo):
        def publish_step(self, ctx):
            self.steps += 1
            if not ctx.state.get("prepared"):
                ctx.state["prepared"] = True
                ctx.save_state(ctx.state)
                scheduler.runtime.stop_event.set()  # app shutting down while we work
                return StepResult("continue")
            return super().publish_step(ctx)

    provider = registry.register(TwoSteps())
    scheduler = Scheduler(app.state.db, clock, inline=True, enable_jobs=False)
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    assert scheduler.tick().outcomes[target.id] == "paused"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING and t.next_attempt_at == clock.now()
    assert t.provider_state["prepared"] is True
    scheduler.close()

    again = restart(app, clock)
    assert again.tick().outcomes[target.id] == "published"
    again.close()
    assert provider.commits == 1


# ------------------------------------------------------------------------------------------ keepawake


class FakeKernel32:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802 - Windows API name
        self.calls.append(flags)
        return 0x80000000


def test_keep_awake_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert keepawake.is_supported() is False
    with keepawake.keep_awake() as active:
        assert active is False
        assert keepawake.depth() == 1
    assert keepawake.depth() == 0


def test_keep_awake_sets_and_restores_execution_state(monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(keepawake, "_kernel32", lambda: fake)
    with keepawake.keep_awake() as active:
        assert active is True
        with keepawake.keep_awake() as nested:  # nested blocks do not toggle the flags again
            assert nested is True
        assert fake.calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED]
    assert fake.calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED, keepawake.ES_CONTINUOUS]


def test_keep_awake_survives_api_failures(monkeypatch):
    class Broken:
        def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802
            raise OSError("nope")

    monkeypatch.setattr(keepawake, "_kernel32", lambda: Broken())
    with keepawake.keep_awake() as active:
        assert active is False


def test_engine_keeps_the_pc_awake_while_publishing(db, clock, demo_account, sched, monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(keepawake, "_kernel32", lambda: fake)
    depths: list[int] = []

    class Probe(CountingDemo):
        def publish_step(self, ctx):
            depths.append(keepawake.depth())
            return super().publish_step(ctx)

    registry.register(Probe())
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1})
    assert sched.tick().outcomes[target.id] == "published"
    assert depths == [1]
    assert fake.calls[-1] == keepawake.ES_CONTINUOUS


# ------------------------------------------------------------------------- native scheduling (FB/YT)


class NativeAwareFacebook(CountingDemo):
    """Hands the post to "Facebook's scheduler" when there is enough lead time, like the real provider."""

    network = "facebook"
    display_name = "Facebook"

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[int] = []

    def validate(self, target):
        return []

    def publish_step(self, ctx: PublishContext) -> StepResult:
        self.steps += 1
        lead = ctx.target.scheduled_at - ctx.clock.now()
        if engine.wants_native_schedule("facebook", ctx.target.options) and lead >= timedelta(minutes=10):
            ctx.state["native_scheduled"] = True
        ctx.before_commit(ctx.state)
        self.commits += 1
        return StepResult("published", external_id="123_456")

    def cancel_native(self, ctx: PublishContext) -> None:
        self.cancelled.append(ctx.target_id)


def test_native_schedule_is_handed_off_ahead_of_time(db, clock, sched):
    provider = registry.register(NativeAwareFacebook())
    account = make_account(db, network="facebook")
    soon = clock.now() + timedelta(days=2)
    native = make_target(db, account, at=soon, options={"native_schedule": True})
    not_native = make_target(db, account, at=soon, options={"native_schedule": "false"})
    too_far = make_target(db, account, at=clock.now() + timedelta(days=40), options={"native_schedule": True})
    manual = make_target(db, account, at=soon, options={"native_schedule": True}, auto_publish=False)

    report = sched.tick()
    assert report.native == [native.id] and report.claimed == [native.id]
    assert report.outcomes[native.id] == "published"
    t = fresh(db, native)
    assert t.status == TargetStatus.PUBLISHED
    assert t.provider_state["native_scheduled"] is True
    assert t.published_at == soon  # goes live at the scheduled time, not at hand-off
    message = db.scalars(
        select(PostEvent.message).where(PostEvent.target_id == native.id, PostEvent.event == "published")
    ).one()
    assert message == "Programado en Facebook para el jueves 5 de noviembre, 10:00."
    for other in (not_native, too_far, manual):
        assert fresh(db, other).status == TargetStatus.SCHEDULED
    assert provider.commits == 1

    # Cancelling it before its time also removes it from Facebook's schedule.
    engine.cancel_target(db, native.id, clock)
    assert provider.cancelled == [native.id]
    assert fresh(db, native).status == TargetStatus.CANCELLED


def test_wants_native_schedule():
    assert engine.wants_native_schedule("facebook", {"native_schedule": True})
    assert engine.wants_native_schedule("youtube", {"native_schedule": "1"})
    assert not engine.wants_native_schedule("facebook", {"native_schedule": "no"})
    assert not engine.wants_native_schedule("facebook", {})
    assert not engine.wants_native_schedule("instagram", {"native_schedule": True})


def test_post_trashed_while_waiting_is_cancelled(db, clock, demo_account, demo, sched):
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 3})
    assert sched.tick().outcomes[target.id] == "wait"
    db.get(Post, target.post_id).deleted_at = clock.now()
    db.commit()
    advance_to_next_attempt(db, clock, target)
    assert sched.tick().outcomes[target.id] == "cancelled"
    assert fresh(db, target).status == TargetStatus.CANCELLED
    assert demo.commits == 0


def test_thread_hold_is_idempotent(monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(keepawake, "_kernel32", lambda: fake)
    hold = keepawake.ThreadHold()
    assert hold.update(True) is True
    assert hold.update(True) is True
    assert fake.calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED]
    hold.release()
    hold.release()
    assert fake.calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED, keepawake.ES_CONTINUOUS]
    monkeypatch.setattr(keepawake, "_kernel32", lambda: None)
    assert keepawake.ThreadHold().update(True) is False


def test_reset_for_schedule(db, clock, demo_account):
    errored = make_target(db, demo_account, at=clock.now(), status=TargetStatus.ERROR)
    t = fresh(db, errored)
    t.attempts, t.last_error, t.provider_state = 3, "fallo", {"container_id": "c1"}
    engine.reset_for_schedule(t)
    db.commit()
    t = fresh(db, errored)
    assert t.status == TargetStatus.SCHEDULED and t.attempts == 0 and t.last_error is None
    assert t.provider_state == {} and t.commit_phase == CommitPhase.NONE

    maybe_sent = make_target(db, demo_account, at=clock.now(), status=TargetStatus.ERROR)
    t = fresh(db, maybe_sent)
    t.commit_phase, t.provider_state = CommitPhase.COMMIT_SENT, {"publish_id": "p1"}
    engine.reset_for_schedule(t)
    db.commit()
    t = fresh(db, maybe_sent)
    assert t.provider_state == {"publish_id": "p1", "_engine": {"pending": "reconcile"}}

    busy = make_target(db, demo_account, at=clock.now(), status=TargetStatus.PUBLISHING)
    with pytest.raises(EngineError):
        engine.reset_for_schedule(fresh(db, busy))


# ------------------------------------------------------------------- integration hooks (providers → engine)


def test_wants_native_schedule_is_strict_about_format_and_privacy():
    assert engine.wants_native_schedule("facebook", {"native_schedule": True}, "post")
    assert not engine.wants_native_schedule("facebook", {"native_schedule": True}, "reel")
    assert not engine.wants_native_schedule("facebook", {"native_schedule": True}, "story")
    assert engine.wants_native_schedule("youtube", {"native_schedule": True}, "video")
    assert engine.wants_native_schedule(
        "youtube", {"native_schedule": True, "privacy_status": "public"}, "short"
    )
    assert not engine.wants_native_schedule(
        "youtube", {"native_schedule": True, "privacy_status": "unlisted"}, "video"
    )


def test_native_option_left_on_a_reel_is_not_claimed_early(db, clock, sched):
    registry.register(NativeAwareFacebook())
    account = make_account(db, network="facebook")
    reel = make_target(
        db, account, at=clock.now() + timedelta(days=2), options={"native_schedule": True}, fmt="reel"
    )
    report = sched.tick()
    assert report.claimed == [] and report.native == []
    assert fresh(db, reel).status == TargetStatus.SCHEDULED


class MetaUpdatingDemo(CountingDemo):
    """Publishes in one step and asks the engine to merge a value into ``accounts.meta`` (like YouTube's
    lock detection), after refreshing its token mid-run through ``ctx.update_tokens``."""

    def publish_step(self, ctx: PublishContext) -> StepResult:
        from planificador.providers.base import TokenUpdate

        assert callable(ctx.update_tokens)
        ctx.update_tokens(TokenUpdate(access_token="fresh-token", token_expires_at=None, scopes=["a", "b"]))
        assert ctx.account.access_token == "fresh-token" and ctx.account.scopes == ["a", "b"]
        ctx.state["account_meta_updates"] = {"youtube_lock_status": "unlocked"}
        ctx.save_state(ctx.state)
        ctx.before_commit(ctx.state)
        return StepResult("published", external_id="x-1")


def test_provider_account_meta_updates_and_mid_run_tokens_are_persisted(db, clock, demo_account, sched):
    registry.register(MetaUpdatingDemo())
    demo_account.meta = {"keep": 1}
    db.commit()
    target = make_target(db, demo_account, at=clock.now())
    assert sched.tick().outcomes[target.id] == "published"
    db.expire_all()
    account = db.get(Account, demo_account.id)
    assert account.meta == {"keep": 1, "youtube_lock_status": "unlocked"}
    assert account.access_token == "fresh-token" and account.scopes == ["a", "b"]
    t = fresh(db, target)
    assert "account_meta_updates" not in t.provider_state  # applied once, not kept in the state
    assert "token_refreshed" in events(db, target)


class DeletePostFacebook(CountingDemo):
    """A provider exposing only ``delete_post`` (Facebook's helper), not ``cancel_native``."""

    network = "facebook"
    display_name = "Facebook"

    def __init__(self, ok: bool = True) -> None:
        super().__init__()
        self.ok = ok
        self.deleted: list[str] = []

    def delete_post(self, account, settings, http, external_id: str) -> bool:
        self.deleted.append(external_id)
        return self.ok


@pytest.mark.parametrize("ok", [True, False])
def test_cancel_native_falls_back_to_delete_post(db, clock, ok):
    provider = registry.register(DeletePostFacebook(ok=ok))
    account = make_account(db, network="facebook")
    target = make_target(db, account, at=clock.now() + timedelta(days=1), status=TargetStatus.PUBLISHED)
    t = fresh(db, target)
    t.provider_state = {"native_scheduled": True}
    t.external_id = "123_456"
    db.commit()
    if ok:
        engine.cancel_target(db, target.id, clock)
    else:
        with pytest.raises(EngineError):
            engine.cancel_target(db, target.id, clock)
    assert provider.deleted == ["123_456"]
    expected_status = TargetStatus.CANCELLED if ok else TargetStatus.PUBLISHED
    assert fresh(db, target).status == expected_status
    expected = ["native_cancelled", "cancelled"] if ok else ["native_cancel_failed"]
    assert events(db, target) == expected

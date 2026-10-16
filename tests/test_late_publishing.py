"""Late publishing, catch-up spacing and offline-aware retries (SPEC §10), with the demo provider and a manual
clock:

- the app/PC was down for hours → overdue posts are published (not ``missed``), per account one at a time, in
  their original order and spaced by ``catchup_spacing_minutes``; the spacing survives restarts and nothing is
  published twice;
- ``publish_late`` off keeps the old grace/missed rule; ``publish_late_max_hours`` bounds the delay;
- no internet before the commit point → the target waits for the connection without spending retries, then
  goes out once; after the commit point the reconcile path is unchanged; genuine API errors keep the backoff;
- on-time posts are never delayed; settings page save/validation; drawer labels.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planificador import config, engine
from planificador import scheduler as scheduler_mod
from planificador.connectivity import Connectivity, default_probe
from planificador.models import (
    Account,
    CommitPhase,
    Notification,
    NotificationKind,
    PostEvent,
    PostTarget,
    TargetStatus,
)
from planificador.providers import registry, tiktok
from planificador.providers.base import ErrorKind, ProviderError
from planificador.scheduler import HEARTBEAT_KEY, ONLINE_SINCE_KEY, Scheduler
from test_engine import CountingDemo, events, fresh, make_account, make_target, notifications
from test_review_providers import U1 as TIKTOK_U1
from test_review_providers import UPLOAD_HOST as TIKTOK_UPLOAD_HOST
from test_review_providers import _ok as tiktok_ok
from test_review_providers import _tiktok_target as tiktok_target

HX = {"HX-Request": "true"}
HX_DRAWER = {"HX-Request": "true", "HX-Target": "drawer-root"}
SPACING = timedelta(minutes=15)


# --------------------------------------------------------------------------------------------- helpers


class Net:
    """A fake internet connection for the scheduler's connectivity probe (counts the probes)."""

    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.probes = 0

    def __call__(self, network: str, http: Any) -> bool:
        self.probes += 1
        return self.online


@pytest.fixture
def demo() -> CountingDemo:
    return registry.register(CountingDemo())  # type: ignore[return-value]


@pytest.fixture
def net() -> Net:
    return Net()


def new_scheduler(app, clock, net: Net | None = None, seed: int = 7) -> Scheduler:
    """A scheduler instance on the app database (a new one = what a process restart looks like)."""
    return Scheduler(
        app.state.db,
        clock,
        inline=True,
        enable_jobs=False,
        rng=random.Random(seed),
        connectivity_probe=net,
    )


@pytest.fixture
def sched(app, clock, net):
    scheduler = new_scheduler(app, clock, net)
    yield scheduler
    scheduler.close()


def app_was_off_since(db: Session, last_tick: datetime) -> None:
    """The app last ticked at ``last_tick`` (then the PC was switched off / the app closed)."""
    config.set_timestamp(db, HEARTBEAT_KEY, last_tick, commit=False)
    config.set_timestamp(db, ONLINE_SINCE_KEY, last_tick - timedelta(days=1), commit=False)
    db.commit()


def published_at(db: Session, target: PostTarget) -> datetime | None:
    t = fresh(db, target)
    return t.published_at if t.status == TargetStatus.PUBLISHED else None


def commit_count(db: Session, target: PostTarget) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(PostEvent)
            .where(PostEvent.target_id == target.id, PostEvent.event == "commit_sent")
        )
        or 0
    )


def event_messages(db: Session, target: PostTarget, event: str) -> list[str]:
    return list(
        db.scalars(
            select(PostEvent.message)
            .where(PostEvent.target_id == target.id, PostEvent.event == event)
            .order_by(PostEvent.id)
        )
    )


def run_minutes(sched: Scheduler, clock, minutes: int) -> list:
    """Tick once a minute for ``minutes`` minutes; returns the reports."""
    reports = []
    for _ in range(minutes):
        clock.advance(minutes=1)
        reports.append(sched.tick())
    return reports


# ---------------------------------------------------------------------- app down for 18 h: catch-up


def test_app_down_18h_publishes_every_overdue_post_in_order_spaced_per_account(app, db, clock, demo, sched):
    start = clock.now()
    a = make_account(db, network="demo")
    b = make_account(db, network="demo")
    # One post every 6 h (as in a CSV import), starting 1 h after the app was closed.
    a_posts = [
        make_target(db, a, at=start + timedelta(hours=h), options={"demo_steps": 1}) for h in (1, 7, 13)
    ]
    b_posts = [make_target(db, b, at=start + timedelta(hours=h), options={"demo_steps": 1}) for h in (4, 10)]
    app_was_off_since(db, start)
    clock.set(start + timedelta(hours=18))  # the PC is switched on again 18 h later

    first = sched.tick()
    assert first.gap is True and first.missed == []
    # Catch-up starts right away: the first overdue post of each account, in their original order.
    assert first.catchup == [a_posts[0].id, b_posts[0].id]
    assert set(first.spaced) == {a_posts[1].id, a_posts[2].id, b_posts[1].id}
    assert sched._release_hint == clock.now() + SPACING  # the loop wakes up for the next release

    reports = run_minutes(sched, clock, 45)
    assert all(not r.missed for r in reports)
    for posts in (a_posts, b_posts):
        times = [published_at(db, t) for t in posts]
        assert all(times), "every overdue post is published"
        assert times == sorted(times), "in their original order"
        gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
        assert all(gap >= SPACING for gap in gaps), gaps
        assert all(gap <= SPACING + timedelta(minutes=1) for gap in gaps), gaps
    # Accounts do not wait for each other.
    assert published_at(db, a_posts[0]) == published_at(db, b_posts[0]) == start + timedelta(hours=18)
    # Exactly one irreversible send per post.
    assert demo.commits == 5 and all(commit_count(db, t) == 1 for t in a_posts + b_posts)

    # Each one says it went out late and why, in the timeline and in a notification.
    late = event_messages(db, a_posts[0], "published_late")
    assert late == [
        "Publicada con retraso: estaba programada para el martes 3 de noviembre, 11:00; el equipo o la app "
        "no estaba disponible."
    ]
    assert (
        "estaba programada para el martes 3 de noviembre, 11:00" in event_messages(db, a_posts[0], "late")[0]
    )
    infos = [n for n in notifications(db) if n.kind == NotificationKind.INFO]
    assert len(infos) == 5 and all(n.title == "Publicada con retraso en Demo" for n in infos)
    assert "17 h" in infos[0].body and "el equipo o la app no estaba disponible" in infos[0].body
    assert not [n for n in notifications(db) if n.kind == NotificationKind.MISSED]


def test_spacing_survives_restarts_and_never_publishes_twice(app, db, clock, demo, net):
    start = clock.now()
    account = make_account(db, network="demo")
    posts = [
        make_target(db, account, at=start + timedelta(hours=h), options={"demo_steps": 1}) for h in (1, 2, 3)
    ]
    app_was_off_since(db, start)
    clock.set(start + timedelta(hours=10))

    assert new_scheduler(app, clock, net).tick().catchup == [posts[0].id]
    # The app is restarted 5 minutes later: the spacing comes from the database, not from memory.
    clock.advance(minutes=5)
    report = new_scheduler(app, clock, net, seed=1).tick()
    assert report.claimed == [] and report.spaced == [posts[1].id, posts[2].id]
    # Restarted again once the gap has passed: the second one goes out, the third still waits.
    clock.advance(minutes=10)
    report = new_scheduler(app, clock, net, seed=2).tick()
    assert report.catchup == [posts[1].id] and report.spaced == [posts[2].id]
    assert new_scheduler(app, clock, net, seed=3).tick().claimed == []  # an immediate restart changes nothing

    # The app dies right after claiming the third one (before running it): the next start resumes that same
    # claim (a stale one), it does not start a second publication.
    clock.advance(minutes=15)
    assert engine.claim_due(db, posts[2].id, clock.now(), catchup=True)
    clock.advance(minutes=1)
    report = new_scheduler(app, clock, net, seed=4).tick()
    assert report.recovered == [posts[2].id] and report.outcomes[posts[2].id] == "published"
    assert new_scheduler(app, clock, net, seed=5).tick().claimed == []

    assert all(fresh(db, t).status == TargetStatus.PUBLISHED for t in posts)
    assert demo.commits == 3 and [commit_count(db, t) for t in posts] == [1, 1, 1]


def test_on_time_posts_are_not_delayed_by_the_catchup_spacing(app, db, clock, demo, sched):
    start = clock.now()
    account = make_account(db, network="demo")
    overdue = make_target(db, account, at=start + timedelta(hours=1), options={"demo_steps": 1})
    queued = make_target(db, account, at=start + timedelta(hours=2), options={"demo_steps": 1})
    back = start + timedelta(hours=5)
    on_time = make_target(db, account, at=back + timedelta(minutes=1), options={"demo_steps": 1})
    app_was_off_since(db, start)
    clock.set(back)
    assert sched.tick().catchup == [overdue.id]

    clock.advance(minutes=1)  # the on-time post is due while the app is running
    report = sched.tick()
    assert report.outcomes == {on_time.id: "published"} and report.spaced == [queued.id]
    assert published_at(db, on_time) == back + timedelta(minutes=1)
    assert not event_messages(db, on_time, "late") and not event_messages(db, on_time, "published_late")


def test_posts_due_at_the_same_time_while_running_are_not_spaced(db, clock, demo, sched):
    account = make_account(db, network="demo")
    now = clock.now()
    first = make_target(db, account, at=now, options={"demo_steps": 1})
    second = make_target(db, account, at=now, options={"demo_steps": 1})
    report = sched.tick()
    assert report.outcomes == {first.id: "published", second.id: "published"}
    assert report.spaced == [] and report.catchup == []


# ---------------------------------------------------------------------- old behaviour / max delay


def test_publish_late_off_keeps_the_grace_window_rule(app, db, clock, demo, sched):
    config.set_setting(db, "publish_late", False)
    start = clock.now()
    account = make_account(db, network="demo")
    old = make_target(db, account, at=start + timedelta(hours=1), options={"demo_steps": 1})
    recent = make_target(db, account, at=start + timedelta(hours=9), options={"demo_steps": 1})
    app_was_off_since(db, start)
    clock.set(start + timedelta(hours=10))  # 9 h late (> 120 min grace) and 1 h late

    report = sched.tick()
    assert report.missed == [old.id]
    assert report.outcomes == {recent.id: "published"}
    assert fresh(db, old).status == TargetStatus.MISSED and demo.commits == 1
    missed = [n for n in notifications(db) if n.kind == NotificationKind.MISSED]
    assert len(missed) == 1
    assert "publicarla ahora, reprogramarla o descartarla" in missed[0].body
    assert (
        "Publicar las publicaciones atrasadas en cuanto se pueda" in missed[0].body
    )  # points to the setting
    assert "Ajustes › Publicación" in missed[0].body


def test_max_late_hours_bound(app, db, clock, demo, sched):
    config.set_setting(db, "publish_late_max_hours", 6)
    start = clock.now()
    account = make_account(db, network="demo")
    too_late = [
        make_target(db, account, at=start + timedelta(hours=h), options={"demo_steps": 1}) for h in (1, 5)
    ]
    within = make_target(db, account, at=start + timedelta(hours=13), options={"demo_steps": 1})
    app_was_off_since(db, start)
    clock.set(start + timedelta(hours=18))  # 17 h, 13 h and 5 h late

    report = sched.tick()
    assert report.missed == [t.id for t in too_late]
    assert report.outcomes == {within.id: "published"}
    note = [n for n in notifications(db) if n.kind == NotificationKind.MISSED][0]
    assert "límite de retraso (6 h)" in note.body and "Publicar las publicaciones atrasadas" not in note.body
    assert "límite de retraso de 6 h" in event_messages(db, too_late[0], "missed")[0]


def test_existing_missed_targets_are_never_published_automatically(app, db, clock, demo, sched):
    account = make_account(db, network="demo")
    target = make_target(db, account, at=clock.now() - timedelta(days=1), status=TargetStatus.MISSED)
    clock.advance(hours=1)
    report = sched.tick()
    assert report.claimed == [] and fresh(db, target).status == TargetStatus.MISSED and demo.steps == 0


def test_waiting_target_after_the_app_was_closed_resumes_late(app, db, clock, demo, net):
    """A retry wait interrupted by closing the app is resumed (not missed) when it reopens hours later."""
    account = make_account(db, network="demo")
    target = make_target(
        db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "transient", "demo_fail_times": 1}
    )
    assert new_scheduler(app, clock, net).tick().outcomes[target.id] == "retry"
    clock.advance(hours=10)
    report = new_scheduler(app, clock, net, seed=3).tick()
    assert report.missed == [] and report.catchup == [target.id]
    assert report.outcomes[target.id] == "published"
    assert "el equipo o la app no estaba disponible" in event_messages(db, target, "published_late")[0]


def test_paused_message_follows_the_policy(app, db, clock, net):
    registry.register(CountingDemo())
    account = make_account(db, network="demo")
    target = make_target(db, account, at=clock.now(), options={"demo_steps": 3})
    first = new_scheduler(app, clock, net)
    assert first.tick().outcomes[target.id] == "wait"
    first.runtime.stop_event.set()
    clock.set(fresh(db, target).next_attempt_at)
    assert engine.run_target(target.id, first.runtime) == "paused"
    assert event_messages(db, target, "paused")[-1] == (
        "Publicación en pausa: la app se está cerrando. Continuará al volver a abrirla."
    )


# ---------------------------------------------------------------------------- offline-aware retries


def test_offline_for_3h_spends_no_retry_and_publishes_once_when_back(app, db, clock, demo, net, sched):
    net.online = False
    account = make_account(db, network="demo")
    due = clock.now()
    target = make_target(
        db, account, at=due, options={"demo_steps": 1, "demo_fail": "offline", "demo_fail_times": 1}
    )
    assert sched.tick().outcomes[target.id] == "offline"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING and t.attempts == 0 and t.last_error is None
    assert t.next_attempt_at == due + engine.OFFLINE_RECHECK
    assert engine.is_offline_waiting(t)
    steps = demo.steps

    reports = run_minutes(sched, clock, 180)
    assert all(r.offline == [target.id] for r in reports), "re-checked every minute, quietly"
    assert demo.steps == steps, "the provider is not called while there is no internet"
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHING and t.attempts == 0
    assert events(db, target).count("offline") == 1  # one event per outage, not per check
    outage = [n for n in notifications(db) if n.title == engine.OUTAGE_TITLE]
    assert len(outage) == 1 and outage[0].read_at is None  # one notification per outage
    assert config.get_setting(db, engine.OUTAGE_KEY, None)["notified"] is True

    net.online = True
    clock.advance(minutes=1)
    report = sched.tick()
    assert report.outcomes == {target.id: "published"} and report.catchup == [target.id]
    assert demo.commits == 1 and commit_count(db, target) == 1
    assert fresh(db, target).attempts == 0
    assert "no había conexión a internet" in event_messages(db, target, "published_late")[0]
    assert event_messages(db, target, "catchup") == [
        "Ha vuelto la conexión a internet: se retoma la publicación."
    ]
    assert config.get_setting(db, engine.OUTAGE_KEY, None) is None
    db.expire_all()
    assert db.get(Notification, outage[0].id).read_at is not None  # the outage notice is resolved
    assert not [n for n in notifications(db) if n.kind == NotificationKind.ERROR]


def test_due_while_offline_waits_then_goes_out(db, clock, demo, net, sched):
    """A post that becomes due while the internet is down is not sent to the provider at all (the probe says
    the connection is down) and is published as soon as it comes back."""
    net.online = False
    account = make_account(db, network="demo")
    first = make_target(db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    assert sched.tick().outcomes[first.id] == "offline"
    clock.advance(seconds=20)
    second = make_target(db, make_account(db, network="demo"), at=clock.now(), options={"demo_steps": 1})
    steps = demo.steps
    assert sched.tick().outcomes[second.id] == "offline"  # known offline: no doomed request
    assert demo.steps == steps
    net.online = True
    clock.advance(minutes=2)
    report = sched.tick()
    assert report.outcomes[second.id] == "published"


def test_offline_targets_of_one_account_are_spaced_when_the_connection_returns(db, clock, demo, net, sched):
    net.online = False
    account = make_account(db, network="demo")
    posts = []
    for _ in range(3):
        posts.append(
            make_target(
                db,
                account,
                at=clock.now(),
                options={"demo_steps": 1, "demo_fail": "offline", "demo_fail_times": 1},
            )
        )
        sched.tick()
        clock.advance(hours=1)
    assert all(engine.is_offline_waiting(fresh(db, t)) for t in posts)
    net.online = True
    reports = run_minutes(sched, clock, 40)
    times = [fresh(db, t).published_at for t in posts]
    assert all(times) and times == sorted(times)
    assert all(b - a >= SPACING for a, b in zip(times, times[1:], strict=False))
    assert sum(len(r.catchup) for r in reports) == 3 and demo.commits == 3


def test_genuine_api_errors_keep_the_backoff(db, clock, demo, net, sched):
    account = make_account(db, network="demo")
    # A network-looking failure while the network's host IS reachable: a real API problem → retry budget.
    flaky = make_target(db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    assert sched.tick().outcomes[flaky.id] == "retry"
    assert fresh(db, flaky).attempts == 1 and net.probes == 1
    # A 5xx-style transient error never triggers a probe.
    other = make_account(db, network="demo")
    server = make_target(db, other, at=clock.now(), options={"demo_steps": 1, "demo_fail": "transient"})
    assert sched.tick().outcomes[server.id] == "retry"
    assert fresh(db, server).attempts == 1 and net.probes == 1


def test_offline_after_the_commit_point_keeps_the_reconcile_path(db, clock, demo, net, sched):
    net.online = False
    account = make_account(db, network="demo")
    sent = make_target(
        db,
        account,
        at=clock.now(),
        options={"demo_steps": 1, "demo_fail": "offline_after_commit", "demo_reconcile": "published"},
    )
    assert sched.tick().outcomes[sent.id] == "published"  # reconciled, never re-sent
    assert demo.commits == 1 and demo.reconciles == 1
    assert "offline" not in events(db, sent) and "ambiguous_error" in events(db, sent)

    unknown = make_target(
        db,
        make_account(db, network="demo"),
        at=clock.now(),
        options={"demo_steps": 1, "demo_fail": "offline_after_commit"},
    )
    assert sched.tick().outcomes[unknown.id] == "unknown"
    t = fresh(db, unknown)
    assert t.status == TargetStatus.UNKNOWN and t.commit_phase == CommitPhase.COMMIT_SENT
    assert demo.commits == 2


def test_offline_beyond_grace_is_missed_when_late_publishing_is_off(db, clock, demo, net, sched):
    config.set_setting(db, "publish_late", False)
    net.online = False
    account = make_account(db, network="demo")
    target = make_target(db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    assert sched.tick().outcomes[target.id] == "offline"
    reports = run_minutes(sched, clock, 121)
    assert reports[-1].missed == [target.id]
    assert fresh(db, target).status == TargetStatus.MISSED
    assert "no había conexión a internet" in event_messages(db, target, "missed")[0]
    note = [n for n in notifications(db) if n.kind == NotificationKind.MISSED][0]
    assert "no había conexión a internet" in note.body


def test_offline_wait_respects_the_max_late_bound(db, clock, demo, net, sched):
    config.set_setting(db, "publish_late_max_hours", 2)
    net.online = False
    account = make_account(db, network="demo")
    target = make_target(db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    sched.tick()
    reports = run_minutes(sched, clock, 121)
    assert reports[-1].missed == [target.id] and demo.commits == 0


def test_connection_cache_is_dropped_after_a_sleep_gap(db, clock, sched):
    sched.connectivity.check("demo")  # cache an answer
    assert sched.connectivity._cache
    sched.tick()
    clock.advance(hours=3)
    assert sched.tick().gap is True and sched.connectivity._cache == {}
    assert sched.connectivity.outage_active is False  # looked up again: no outage recorded


# ------------------------------------------------------------------------------ unit: classification


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderError(ErrorKind.TRANSIENT, "meta_connect_failed", "x"), True),
        (ProviderError(ErrorKind.TRANSIENT, "youtube_timeout", "x"), True),
        (ProviderError(ErrorKind.TRANSIENT, "tiktok_network", "x", detail={"error": "ConnectError"}), True),
        (ProviderError(ErrorKind.TRANSIENT, "x", "x", detail={"error": "ReadTimeout"}), True),
        (ProviderError(ErrorKind.TRANSIENT, "meta_server_error", "x", detail={"http_status": 503}), False),
        (ProviderError(ErrorKind.RATE_LIMITED, "x", "x", detail={"error": "ConnectError"}), False),
        (ProviderError(ErrorKind.AUTH, "x", "x"), False),
        (httpx.ConnectError("[Errno -2] Name or service not known"), True),
        (httpx.ConnectTimeout("timed out"), True),
        (RuntimeError("boom"), False),
    ],
)
def test_is_network_failure(error: BaseException, expected: bool) -> None:
    assert engine.is_network_failure(error) is expected


def test_is_network_failure_follows_the_cause_chain() -> None:
    try:
        try:
            raise httpx.ReadTimeout("read timed out")
        except httpx.ReadTimeout as exc:
            raise ProviderError(ErrorKind.TRANSIENT, "custom", "Se reintentará.") from exc
    except ProviderError as error:
        assert engine.is_network_failure(error)


def test_default_probe_and_cache(clock) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "tiktok" in request.url.host:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(404)  # any answer means "reachable"

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert default_probe("facebook", http) is True
    assert default_probe("tiktok", http) is False
    assert default_probe("demo", http) is True and len(calls) == 2  # the demo network is never probed

    conn = Connectivity(clock, probe=lambda network, client: default_probe(network, http))
    assert conn.check("tiktok") is False and conn.known_offline("tiktok")
    conn.check("tiktok")
    assert len(calls) == 3  # cached
    conn.check("tiktok", fresh=True)
    assert len(calls) == 4
    clock.advance(minutes=1)
    assert not conn.known_offline("tiktok")  # expired
    conn.forget()
    assert not conn.known_offline("instagram")


def test_late_policy_defaults_for_installs_that_never_saved_it(db) -> None:
    policy = engine.LatePolicy.load(db)
    assert policy.publish_late is True and policy.max_late is None and policy.spacing == SPACING
    config.set_setting(db, "publish_late", False)
    config.set_setting(db, "publish_late_max_hours", 12)
    config.set_setting(db, "catchup_spacing_minutes", 60)
    policy = engine.LatePolicy.load(db)
    assert policy.publish_late is False and policy.max_late == timedelta(hours=12)
    assert policy.spacing == timedelta(minutes=60)


# ----------------------------------------------------------------------------------- UI: settings


def test_settings_page_shows_the_late_publishing_controls(client: TestClient) -> None:
    html = client.get("/ajustes").text
    assert (
        "Publicar las publicaciones atrasadas en cuanto se pueda (PC encendido, app abierta e internet)"
        in html
    )
    assert 'id="sw-publish-late"' in html and 'name="publish_late" value="0"' in html
    assert 'id="sw-publish-late" name="publish_late" value="1" checked' in html  # on by default
    assert "Límite de retraso" in html and '<option value="0" selected>Sin límite</option>' in html
    assert "Separación entre publicaciones atrasadas de una misma cuenta" in html
    assert '<option value="15" selected>15 minutos</option>' in html
    assert "el ordenador tiene que estar encendido" in html


def test_save_late_publishing_settings(csrf_client: Any, db: Session, brand) -> None:
    data = {
        "default_timezone": "Europe/Madrid",
        "grace_minutes": "120",
        "publish_late": ["0", "1"],
        "publish_late_max_hours": "12",
        "catchup_spacing_minutes": "30",
    }
    r = csrf_client.post("/ajustes/publicacion", data=data, headers=HX)
    assert r.status_code == 200
    assert config.get_bool(db, "publish_late") is True
    assert config.get_int(db, "publish_late_max_hours") == 12
    assert config.get_int(db, "catchup_spacing_minutes") == 30
    assert '<option value="12" selected>12 horas</option>' in r.text

    data["publish_late"] = "0"  # switch off: only the hidden field is sent
    assert csrf_client.post("/ajustes/publicacion", data=data, headers=HX).status_code == 200
    db.expire_all()
    assert config.get_bool(db, "publish_late") is False

    # A form without the new fields (an older page) keeps what is stored.
    old_form = {"default_timezone": "Europe/Madrid", "grace_minutes": "90"}
    assert csrf_client.post("/ajustes/publicacion", data=old_form, headers=HX).status_code == 200
    db.expire_all()
    assert (
        config.get_bool(db, "publish_late") is False and config.get_int(db, "catchup_spacing_minutes") == 30
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("publish_late_max_hours", "5", "Elige un límite de retraso de la lista."),
        ("publish_late_max_hours", "mucho", "Elige un límite de retraso de la lista."),
        ("catchup_spacing_minutes", "7", "Elige una separación de la lista."),
        ("catchup_spacing_minutes", "-15", "Elige una separación de la lista."),
    ],
)
def test_late_publishing_settings_validation(
    csrf_client: Any, db: Session, brand, field: str, value: str, message: str
) -> None:
    data = {"default_timezone": "Europe/Madrid", "grace_minutes": "120", field: value}
    r = csrf_client.post("/ajustes/publicacion", data=data, headers=HX)
    assert r.status_code == 422 and message in r.text
    assert config.get_int(db, "publish_late_max_hours") == 0
    assert config.get_int(db, "catchup_spacing_minutes") == 15


# ------------------------------------------------------------------------------------- UI: drawer


def test_drawer_shows_waiting_for_internet(
    client: TestClient, db: Session, clock, demo, net, sched, demo_account: Account
):
    net.online = False
    target = make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    assert sched.tick().outcomes[target.id] == "offline"
    html = client.get(f"/publicaciones/{target.post_id}", headers=HX_DRAWER).text
    assert "Esperando conexión a internet desde el martes 3 de noviembre, 10:00" in html
    assert "se publicará sola en cuanto vuelva" in html
    assert "Esperando conexión a internet: la publicación saldrá en cuanto vuelva" in html  # timeline event


def test_drawer_shows_overdue_targets(client: TestClient, db: Session, clock, demo_account: Account):
    target = make_target(db, demo_account, at=clock.now() - timedelta(hours=3))
    html = client.get(f"/publicaciones/{target.post_id}", headers=HX_DRAWER).text
    assert "Atrasada: estaba programada para el martes 3 de noviembre, 07:00" in html
    assert "saldrá en cuanto sea posible" in html


def test_late_tiktok_draft_note_goes_in_its_own_notification(app, db, clock, demo, sched):
    start = clock.now()
    account = make_account(db, network="demo")
    target = make_target(
        db, account, at=start + timedelta(hours=1), options={"demo_steps": 1, "demo_result": "sent_to_app"}
    )
    app_was_off_since(db, start)
    clock.set(start + timedelta(hours=4))
    assert sched.tick().outcomes[target.id] == "sent_to_app"
    notes = notifications(db)
    assert len(notes) == 1 and notes[0].title == "Termina la publicación en Demo"
    assert "Va con retraso: estaba programada para el martes 3 de noviembre, 11:00" in notes[0].body
    assert event_messages(db, target, "published_late")[0].startswith("Enviada a la app con retraso")


def test_catch_up_runs_right_after_the_app_starts(app, db, clock, demo, net):
    """The scheduler thread's first tick releases the first overdue post of each account at once."""
    start = clock.now()
    account = make_account(db, network="demo")
    posts = [
        make_target(db, account, at=start + timedelta(hours=h), options={"demo_steps": 1}) for h in (1, 2)
    ]
    app_was_off_since(db, start)
    clock.set(start + timedelta(hours=8))
    sched = Scheduler(app.state.db, clock, tick_seconds=30, enable_jobs=False, connectivity_probe=net)
    sched.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and published_at(db, posts[0]) is None:
            time.sleep(0.02)
        assert published_at(db, posts[0]) == clock.now()
        assert fresh(db, posts[1]).status == TargetStatus.SCHEDULED  # spaced, not missed
    finally:
        assert sched.stop(timeout=5)


# ------------------------------------------- the irreversible request is the run's first network call


def test_offline_failure_confirmed_not_published_waits_for_the_connection(db, clock, demo, net, sched):
    """A connection failure on the irreversible request that the network confirms did not go out
    (``not_published``) waits for the connection like any other offline failure: no retry is spent."""
    net.online = False
    account = make_account(db, network="demo")
    target = make_target(
        db,
        account,
        at=clock.now(),
        options={
            "demo_steps": 1,
            "demo_fail": "offline_after_commit",
            "demo_fail_times": 1,
            "demo_reconcile": "not_published",
        },
    )
    assert sched.tick().outcomes[target.id] == "offline"
    t = fresh(db, target)
    assert engine.is_offline_waiting(t) and t.commit_phase == CommitPhase.NONE
    assert t.attempts == 0 and t.last_error is None
    assert events(db, target)[-2:] == ["reconciled_not_published", "offline"]

    run_minutes(sched, clock, 90)
    assert fresh(db, target).status == TargetStatus.PUBLISHING and demo.commits == 1
    net.online = True
    clock.advance(minutes=1)
    assert sched.tick().outcomes[target.id] == "published"
    assert fresh(db, target).attempts == 0 and demo.commits == 2
    assert "retry_scheduled" not in events(db, target)


def facebook_graph(net: Net, sent: list[str]):
    """Facebook Graph API mock: no answer while ``net`` is offline; one feed POST = one post."""

    def handler(request: httpx.Request) -> httpx.Response:
        if not net.online:
            raise httpx.ConnectError("[Errno 101] Network is unreachable", request=request)
        if request.method == "POST" and request.url.path.endswith("/feed"):
            sent.append(request.url.path)
            return httpx.Response(200, json={"id": "111_222"})
        return httpx.Response(200, json={"permalink_url": "https://www.facebook.com/111/posts/222"})

    return handler


def test_facebook_text_post_without_internet_waits_and_goes_out_once(db, clock, net, sched):
    """A Facebook text post sends its irreversible request first. With no internet at its time it must wait
    for the connection (3 h here) instead of spending the 1/5/15/30-min retries and ending in ``error``."""
    net.online = False
    account = make_account(db, network="facebook")
    target = make_target(db, account, at=clock.now() + timedelta(minutes=1), text="Hola sin internet")
    sent: list[str] = []
    with respx.mock(assert_all_called=False) as router:
        router.route(host="graph.facebook.com").mock(side_effect=facebook_graph(net, sent))
        reports = run_minutes(sched, clock, 180)
        assert reports[0].outcomes[target.id] == "offline"
        t = fresh(db, target)
        assert t.status == TargetStatus.PUBLISHING and t.attempts == 0 and t.last_error is None
        assert engine.is_offline_waiting(t) and t.commit_phase == CommitPhase.NONE
        assert "retry_scheduled" not in events(db, target) and events(db, target).count("offline") == 1

        net.online = True
        run_minutes(sched, clock, 2)
    t = fresh(db, target)
    assert t.status == TargetStatus.PUBLISHED and t.attempts == 0 and len(sent) == 1
    assert "no había conexión a internet" in event_messages(db, target, "published_late")[0]


def test_facebook_connection_error_with_a_reachable_host_keeps_the_backoff(db, clock, net, sched):
    account = make_account(db, network="facebook")
    target = make_target(db, account, at=clock.now(), text="Hola")
    with respx.mock(assert_all_called=False) as router:
        router.route(host="graph.facebook.com").mock(side_effect=httpx.ConnectError("reset", request=None))
        assert sched.tick().outcomes[target.id] == "retry"
    t = fresh(db, target)
    assert t.attempts == 1 and not engine.is_offline_waiting(t) and net.probes == 1


def tiktok_api(router, net: Net, calls: dict[str, int]) -> None:
    """TikTok Content Posting API mock (3 chunks: 300, 300, 500 bytes); every host fails while ``net`` is
    offline; ``calls`` counts the init requests that got through and the chunk PUTs."""

    def answer(handler):
        def side_effect(request: httpx.Request) -> httpx.Response:
            if not net.online:
                raise httpx.ConnectError("[Errno 101] Network is unreachable", request=request)
            return handler(request)

        return side_effect

    def init(request: httpx.Request) -> httpx.Response:
        calls["init"] += 1
        return httpx.Response(200, json=tiktok_ok({"publish_id": "PUB1", "upload_url": TIKTOK_U1}))

    def put(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Content-Range"].split(" ")[1].split("-")[0])
        calls["put"] += 1
        if calls.get("offline_at_chunk") == first:
            calls.pop("offline_at_chunk")
            net.online = False  # the connection drops in the middle of the upload
            raise httpx.ConnectError("[Errno 101] Network is unreachable", request=request)
        return httpx.Response(201 if first == 600 else 206)

    creator = {
        "creator_username": "tt",
        "creator_nickname": "TT",
        "privacy_level_options": ["SELF_ONLY"],
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 600,
    }
    router.post(tiktok.CREATOR_INFO_URL).mock(
        side_effect=answer(lambda r: httpx.Response(200, json=tiktok_ok(creator)))
    )
    router.post(tiktok.VIDEO_INIT_URL).mock(side_effect=answer(init))
    router.post(tiktok.INBOX_INIT_URL).mock(side_effect=answer(init))
    router.put(url__startswith=TIKTOK_UPLOAD_HOST).mock(side_effect=answer(put))
    status = "SEND_TO_USER_INBOX" if calls.get("inbox") else "PUBLISH_COMPLETE"
    router.post(tiktok.STATUS_URL).mock(
        side_effect=answer(lambda r: httpx.Response(200, json=tiktok_ok({"status": status})))
    )


@pytest.fixture
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tiktok, "SINGLE_CHUNK_MAX", 1000)
    monkeypatch.setattr(tiktok, "CHUNK_SIZE", 300)
    monkeypatch.setattr(tiktok, "UPLOAD_BLOCK", 64)
    monkeypatch.setattr(tiktok, "_sleep", lambda s: None)


def test_tiktok_inbox_draft_without_internet_waits_and_goes_out_once(
    app, db, clock, net, sched, small_chunks
):
    """Inbox mode: the init (first request of the run) is the commit point."""
    tid = tiktok_target(app, db, clock)
    row = db.get(PostTarget, tid)
    row.options = {"mode": "inbox"}
    db.commit()
    net.online = False
    calls = {"init": 0, "put": 0, "inbox": 1}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        tiktok_api(router, net, calls)
        reports = run_minutes(sched, clock, 120)
        assert reports[0].outcomes[tid] == "offline"
        db.expire_all()
        row = db.get(PostTarget, tid)
        assert row.status == TargetStatus.PUBLISHING and row.attempts == 0 and engine.is_offline_waiting(row)

        net.online = True
        run_minutes(sched, clock, 2)
    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.SENT_TO_APP and row.attempts == 0
    assert calls["init"] == 1 and calls["put"] == 3


def test_tiktok_upload_cut_by_an_outage_waits_and_resumes_the_same_upload(
    app, db, clock, net, sched, small_chunks
):
    tid = tiktok_target(app, db, clock)
    calls = {"init": 0, "put": 0, "offline_at_chunk": 300}
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        tiktok_api(router, net, calls)
        outcomes = []
        while "offline" not in outcomes and len(outcomes) < 10:
            outcomes.append(sched.tick().outcomes.get(tid))
            db.expire_all()
            clock.set(db.get(PostTarget, tid).next_attempt_at)
        assert outcomes[-1] == "offline" and "retry" not in outcomes
        row = db.get(PostTarget, tid)
        assert engine.is_offline_waiting(row) and row.attempts == 0 and row.commit_phase == CommitPhase.NONE
        assert "reconciled_not_published" in events(db, row)
        run_minutes(sched, clock, 60)
        assert calls["put"] == 2  # nothing is sent while there is no internet

        net.online = True
        run_minutes(sched, clock, 2)
    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.PUBLISHED and row.attempts == 0 and row.external_id == "PUB1"
    assert calls["init"] == 1 and calls["put"] == 4  # the same upload resumed: chunk 2 again, then chunk 3


# --------------------------------------------------------- held-back overdue posts never fill the batch


def test_overdue_posts_held_by_the_spacing_do_not_starve_other_due_posts(
    app, db, clock, demo, sched, monkeypatch
):
    monkeypatch.setattr(scheduler_mod, "DUE_BATCH", 5)
    start = clock.now()
    a = make_account(db, network="demo")
    c = make_account(db, network="demo")
    # Account A: 12 posts every 5 min while the PC was off (a CSV import), then a Publicación asistida one.
    a_posts = [
        make_target(db, a, at=start + timedelta(minutes=5 * (i + 1)), options={"demo_steps": 1})
        for i in range(12)
    ]
    a_manual = make_target(db, a, at=start + timedelta(hours=2), auto_publish=False)
    c_post = make_target(db, c, at=start + timedelta(hours=3), options={"demo_steps": 1})
    app_was_off_since(db, start)
    clock.set(start + timedelta(hours=10))

    first = sched.tick()
    # A's first page holds it back; the next pages skip A's other overdue posts and reach the rest.
    assert first.catchup == [a_posts[0].id, c_post.id]
    assert first.spaced == [t.id for t in a_posts[1:5]]
    assert first.manual == [a_manual.id]

    # An on-time post of another account while A's backlog (more than a batch) is still being spaced.
    b = make_account(db, network="demo")
    on_time = make_target(db, b, at=clock.now() + timedelta(minutes=1), options={"demo_steps": 1})
    clock.advance(minutes=1)
    report = sched.tick()
    assert report.outcomes == {on_time.id: "published"}
    assert published_at(db, on_time) == clock.now()
    assert report.spaced == [t.id for t in a_posts[1:6]]

    # A's backlog still goes out one at a time, in order.
    run_minutes(sched, clock, 15)
    assert published_at(db, a_posts[1]) == start + timedelta(hours=10, minutes=15)
    assert fresh(db, a_posts[2]).status == TargetStatus.SCHEDULED


# ------------------------------------------------------------------------------ the outage record


def outage_notices(db: Session) -> list[Notification]:
    return [n for n in notifications(db) if n.title == engine.OUTAGE_TITLE]


def test_outage_record_ends_when_nothing_waits_for_the_connection(db, clock, demo, net, sched):
    net.online = False
    account = make_account(db, network="demo")
    first = make_target(db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    assert sched.tick().outcomes[first.id] == "offline"
    run_minutes(sched, clock, 7)
    assert len(outage_notices(db)) == 1 and outage_notices(db)[0].read_at is None

    engine.cancel_target(db, first.id, clock)  # e.g. it was posted from the phone; still no internet
    clock.advance(minutes=1)
    sched.tick()
    db.expire_all()
    assert config.get_setting(db, engine.OUTAGE_KEY, None) is None
    assert outage_notices(db)[0].read_at is not None

    # The next outage (the next day) is tracked and notified again.
    net.online = True
    clock.advance(hours=20)
    sched.tick()
    net.online = False
    second = make_target(
        db, account, at=clock.now() + timedelta(minutes=1), options={"demo_steps": 1, "demo_fail": "offline"}
    )
    run_minutes(sched, clock, 30)
    assert fresh(db, second).status == TargetStatus.PUBLISHING
    notices = outage_notices(db)
    assert len(notices) == 2 and notices[1].read_at is None
    assert (
        config.get_setting(db, engine.OUTAGE_KEY, None)["since"]
        == (clock.now() - timedelta(minutes=29)).isoformat()
    )


def test_outage_record_ends_when_the_waiting_post_is_missed(db, clock, demo, net, sched):
    config.set_setting(db, "publish_late", False)
    config.set_setting(db, "grace_minutes", 30)
    net.online = False
    account = make_account(db, network="demo")
    target = make_target(db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    sched.tick()
    reports = run_minutes(sched, clock, 31)
    assert reports[-1].missed == [target.id]
    db.expire_all()
    assert config.get_setting(db, engine.OUTAGE_KEY, None) is None
    assert [n.read_at is not None for n in outage_notices(db)] == [True]


def test_outage_record_is_kept_while_a_post_waits(db, clock, demo, net, sched):
    net.online = False
    account = make_account(db, network="demo")
    make_target(db, account, at=clock.now(), options={"demo_steps": 1, "demo_fail": "offline"})
    sched.tick()
    clock.advance(seconds=40)  # the post re-checks in a minute: nothing is probed in this tick
    assert sched.tick().offline == []
    since = config.get_setting(db, engine.OUTAGE_KEY, None)["since"]
    run_minutes(sched, clock, 10)
    db.expire_all()
    assert config.get_setting(db, engine.OUTAGE_KEY, None)["since"] == since
    assert len(outage_notices(db)) == 1


def test_a_failed_probe_after_a_long_silence_starts_a_new_outage(db, clock, sched):
    rt = sched.runtime
    start = clock.now()
    for minute in range(6):
        engine.note_connectivity(rt, False, start + timedelta(minutes=minute))
    first = outage_notices(db)
    assert len(first) == 1 and first[0].read_at is None

    later = start + timedelta(hours=2)  # nothing probed for 2 h (PC asleep, nothing waiting)
    engine.note_connectivity(rt, False, later)
    db.expire_all()
    record = config.get_setting(db, engine.OUTAGE_KEY, None)
    assert record == {"since": later.isoformat(), "last_failed_at": later.isoformat(), "notified": False}
    assert outage_notices(db)[0].read_at is not None  # the old outage's notice is resolved
    for minute in range(1, 6):
        engine.note_connectivity(rt, False, later + timedelta(minutes=minute))
    notices = outage_notices(db)
    assert len(notices) == 2 and notices[1].read_at is None


# ------------------------------------------------------- back online, but waiting for its turn


def test_spaced_posts_stop_waiting_for_internet_once_it_is_back(
    client: TestClient, db: Session, clock, demo, net, sched, demo_account: Account
):
    net.online = False
    posts = []
    for hour in range(3):
        # The first one fails on its request; the others are not even sent (the probe already said offline).
        failing = {"demo_fail": "offline", "demo_fail_times": 1} if hour == 0 else {}
        posts.append(make_target(db, demo_account, at=clock.now(), options={"demo_steps": 1, **failing}))
        assert sched.tick().outcomes[posts[-1].id] == "offline"
        clock.advance(hours=1)
    net.online = True
    clock.advance(minutes=1)
    back = clock.now()
    report = sched.tick()
    assert report.catchup == [posts[0].id] and report.spaced == [posts[1].id, posts[2].id]
    for target in posts[1:]:
        t = fresh(db, target)
        assert not engine.is_offline_waiting(t) and engine.is_queued_after_outage(t)
        assert "Saldrá en su turno" in event_messages(db, target, "online")[0]

    html = client.get(f"/publicaciones/{posts[2].post_id}", headers=HX_DRAWER).text
    assert "Esperando conexión a internet desde" not in html and "se publicará sola" not in html
    assert "Atrasada: estaba programada para el martes 3 de noviembre, 12:00" in html
    assert "Ha vuelto la conexión a internet. Saldrá en su turno" in html

    reports = run_minutes(sched, clock, 31)
    assert not any(r.offline for r in reports)
    assert [published_at(db, t) for t in posts] == [back, back + SPACING, back + 2 * SPACING]
    assert event_messages(db, posts[1], "catchup") == [
        "Le llega el turno: se retoma la publicación atrasada."
    ]
    assert "no había conexión a internet" in event_messages(db, posts[2], "published_late")[0]
    assert demo.commits == 3

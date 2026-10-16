"""Analytics core (SPEC §12): periods, aggregation math, best times, snapshots and CSV."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import analytics
from planificador.analytics import Period
from planificador.models import (
    Account,
    AccountStatus,
    AnalyticsSnapshot,
    Network,
    Post,
    PostMetric,
    PostTarget,
    TargetStatus,
)
from planificador.providers import registry
from planificador.providers.base import (
    AccountSnapshot,
    ErrorKind,
    FormatSpec,
    PostMetricData,
    Provider,
    ProviderError,
    ProviderSettings,
    StepResult,
)

# --------------------------------------------------------------------------------------------- helpers


def settings(demo: bool = True) -> ProviderSettings:
    return ProviderSettings(
        meta_app_id=None,
        meta_app_secret=None,
        meta_api_version="v25.0",
        tiktok_client_key=None,
        tiktok_client_secret=None,
        tiktok_pkce_encoding="hex",
        base_url="http://localhost:8765",
        demo_mode=demo,
    )


def make_account(db: Session, brand, network: str, **kw) -> Account:
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=kw.pop("external_id", f"{network}-1"),
        name=kw.pop("name", f"Cuenta {network}"),
        username=kw.pop("username", f"{network}_user"),
        status=kw.pop("status", AccountStatus.CONNECTED),
        **kw,
    )
    account.set_tokens("tok")
    db.add(account)
    db.commit()
    return account


def add_snapshot(db: Session, account: Account, day: date, **metrics) -> None:
    db.add(AnalyticsSnapshot(account_id=account.id, day=day, metrics=metrics))


def add_post_metric(
    db: Session, account: Account, ext: str, when: datetime, target_id=None, **metrics
) -> PostMetric:
    row = PostMetric(
        account_id=account.id,
        external_id=ext,
        published_at=when,
        caption=f"Texto {ext}",
        media_type="IMAGE",
        metrics=metrics,
        target_id=target_id,
    )
    db.add(row)
    return row


def published_target(
    db: Session, brand, account: Account, when: datetime, ext: str, status=TargetStatus.PUBLISHED
):
    post = Post(brand_id=brand.id, text=f"Post {ext}", scheduled_at=when)
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=account.id,
        network=account.network,
        status=status,
        scheduled_at=when,
        published_at=when,
        external_id=ext,
    )
    db.add(target)
    db.flush()
    return target


class FakeProvider(Provider):
    """Configurable analytics provider for one network."""

    display_name = "Fake"
    formats = [FormatSpec("post", "Post", "images_or_video")]

    def __init__(self, network: str, *, snapshot=None, posts=None, error: Exception | None = None) -> None:
        self.network = network
        self.snapshot = snapshot
        self.posts = posts or []
        self.error = error
        self.calls: list[date] = []

    def publish_step(self, ctx):  # pragma: no cover - not used
        return StepResult("published")

    def fetch_account_snapshot(self, account, settings, http, day):
        self.calls.append(day)
        if self.error is not None:
            raise self.error
        if callable(self.snapshot):
            return self.snapshot(day)
        return self.snapshot or AccountSnapshot(day=day, metrics={"followers": 10})

    def fetch_recent_posts(self, account, settings, http, since):
        return self.posts


@pytest.fixture
def http():
    def handler(request: httpx.Request) -> httpx.Response:  # no network in tests
        raise AssertionError(f"unexpected HTTP call {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        yield client


# --------------------------------------------------------------------------------------------- periods


def test_period_previous_and_dates() -> None:
    p = Period(date(2026, 10, 1), date(2026, 10, 28))
    assert p.days == 28
    prev = p.previous()
    assert prev == Period(date(2026, 9, 3), date(2026, 9, 30))
    assert prev.days == 28
    assert p.dates()[0] == date(2026, 10, 1) and p.dates()[-1] == date(2026, 10, 28)
    with pytest.raises(ValueError):
        Period(date(2026, 10, 2), date(2026, 10, 1))


def test_resolve_period_presets_and_custom() -> None:
    today = date(2026, 10, 15)
    assert analytics.resolve_period(today, "7") == (Period(date(2026, 10, 9), today), "7")
    assert analytics.resolve_period(today, None) == (Period(date(2026, 9, 18), today), "28")
    assert analytics.resolve_period(today, "90")[0].days == 90
    assert analytics.resolve_period(today, "13")[1] == "28"  # unknown preset → default
    assert analytics.resolve_period(today, "abc")[1] == "28"
    custom, key = analytics.resolve_period(today, "personalizado", "2026-09-01", "2026-09-10")
    assert key == "personalizado" and custom == Period(date(2026, 9, 1), date(2026, 9, 10))
    # swapped and future dates are fixed
    custom, _ = analytics.resolve_period(today, "personalizado", "2026-10-20", "2026-10-01")
    assert custom == Period(date(2026, 10, 1), today)
    # too long ranges are clamped
    custom, _ = analytics.resolve_period(today, "personalizado", "2020-01-01", "2026-10-15")
    assert custom.days == analytics.MAX_CUSTOM_DAYS
    # personalised without dates → default preset
    assert analytics.resolve_period(today, "personalizado")[1] == "28"


# ------------------------------------------------------------------------------------------- formulas


def test_change_pct() -> None:
    assert analytics.change_pct(120, 100) == 20.0
    assert analytics.change_pct(80, 100) == -20.0
    assert analytics.change_pct(5, -10) == 150.0
    assert analytics.change_pct(10, 0) is None
    assert analytics.change_pct(None, 10) is None
    assert analytics.change_pct(10, None) is None
    assert analytics.change_pct(1, 3) == -66.7


def test_engagement_formula() -> None:
    assert analytics.engagement({"interactions": 50, "reach": 1000}) == 5.0
    assert analytics.engagement({"interactions": 50, "views": 2000}) == 2.5  # no reach → views
    assert analytics.engagement({"likes": 30, "comments": 10, "reach": 800}) == 5.0  # parts summed
    assert analytics.engagement({"interactions": 50}) is None
    assert analytics.engagement({"interactions": 50, "reach": 0}) is None
    assert analytics.engagement({"interactions": 50, "reach": 1000}, "youtube") is None


def test_normalize_post_metrics() -> None:
    m = analytics.normalize_post_metrics({"likes": 8, "comments": "2", "reach": 100, "bad": "x"}, "instagram")
    assert m == {"likes": 8, "comments": 2, "reach": 100, "interactions": 10, "engagement": 10.0}
    yt = analytics.normalize_post_metrics(
        {"views": 10, "likes": 3, "interactions": 3, "engagement": 30}, "youtube"
    )
    assert yt == {"views": 10, "likes": 3}


# ---------------------------------------------------------------------------------------- aggregations


def test_kpis_with_previous_period(db: Session, brand) -> None:
    account = make_account(db, brand, Network.FACEBOOK)
    start = date(2026, 10, 1)
    # previous period (Sep 24-30): followers end 100, views 7×10
    for i in range(7):
        d = date(2026, 9, 24) + timedelta(days=i)
        add_snapshot(db, account, d, followers=94 + i, views=10)
    # current period (Oct 1-7): followers 101..107, views 7×20, reach only some days
    for i in range(7):
        d = start + timedelta(days=i)
        metrics = {"followers": 101 + i, "views": 20}
        if i < 3:
            metrics["reach"] = 15
        add_snapshot(db, account, d, **metrics)
    when = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    add_post_metric(db, account, "p1", when, interactions=10, reach=100)
    add_post_metric(db, account, "p2", when + timedelta(days=1), interactions=30, reach=300, likes=25)
    add_post_metric(db, account, "old", datetime(2026, 9, 25, 10, tzinfo=UTC), interactions=5, reach=100)
    db.commit()

    k = analytics.kpis(db, account, start, start + timedelta(days=6))
    assert k["followers"] == {"value": 107, "previous": 100, "change_pct": 7.0}
    assert k["views"] == {"value": 140, "previous": 70, "change_pct": 100.0}
    assert k["reach"]["value"] == 45 and k["reach"]["previous"] is None and k["reach"]["change_pct"] is None
    assert k["posts"] == {"value": 2, "previous": 1, "change_pct": 100.0}
    assert k["engagement"]["value"] == 10.0  # (10 + 30) / (100 + 300)
    assert k["engagement"]["previous"] == 5.0
    assert k["post_likes"]["value"] == 25
    # balance: 107 at the end vs 100 at the end of the previous period
    assert k["followers_net"]["value"] == 7
    assert k["followers_net"]["previous"] == 6  # 100 - 94 (first value of that period is the baseline)

    no_cmp = analytics.kpis(db, account, start, start + timedelta(days=6), compare=False)
    assert no_cmp["followers"] == {"value": 107, "previous": None, "change_pct": None}


def test_kpis_do_not_compare_flows_with_an_incomplete_previous_period(db: Session, brand) -> None:
    """Account connected 2 days before the end of the previous week: 2 days of views there vs 7 now must not
    read as +600 % (ui review [11]); stock metrics still compare, and a complete baseline still does."""
    account = make_account(db, brand, Network.INSTAGRAM)
    start = date(2026, 10, 1)
    for d in (date(2026, 9, 29), date(2026, 9, 30)):
        add_snapshot(db, account, d, followers=100, views=10, reach=8)
    for i in range(7):
        add_snapshot(db, account, start + timedelta(days=i), followers=110, views=10, reach=8)
    db.commit()
    k = analytics.kpis(db, account, start, start + timedelta(days=6))
    assert k["views"] == {"value": 70, "previous": None, "change_pct": None, "previous_incomplete": True}
    assert k["reach"]["previous_incomplete"] is True and k["reach"]["change_pct"] is None
    assert k["followers"] == {"value": 110, "previous": 100, "change_pct": 10.0}
    later = analytics.kpis(db, account, date(2026, 10, 8), date(2026, 10, 14))  # nothing yet: no flows
    assert "views" not in later
    full = analytics.kpis(db, account, date(2026, 10, 5), date(2026, 10, 7))  # Oct 2-4 fully covered
    assert full["views"] == {"value": 30, "previous": 30, "change_pct": 0.0}


def test_follower_balance_from_consecutive_snapshots(db: Session, brand) -> None:
    account = make_account(db, brand, Network.INSTAGRAM)
    add_snapshot(db, account, date(2026, 9, 28), followers=200)  # before the period: baseline
    add_snapshot(db, account, date(2026, 10, 1), followers=205)
    add_snapshot(db, account, date(2026, 10, 2), followers=203)
    # Oct 3 missing (app off) → the change shows up on Oct 4
    add_snapshot(db, account, date(2026, 10, 4), followers=210)
    add_snapshot(db, account, date(2026, 10, 5), views=3)  # snapshot without followers
    db.commit()
    bal = analytics.follower_balance(db, account, date(2026, 10, 1), date(2026, 10, 5))
    nets = [d["net"] for d in bal["days"]]
    assert nets == [5, -2, None, 7, None]
    assert bal["net"] == 10 and bal["gained"] == 12 and bal["lost"] == 2
    assert bal["start_value"] == 200 and bal["end_value"] == 210

    empty = analytics.follower_balance(db, account, date(2026, 8, 1), date(2026, 8, 5))
    assert empty["net"] is None and empty["gained"] is None


def test_follower_balance_youtube_uses_native_metrics(db: Session, brand) -> None:
    account = make_account(db, brand, Network.YOUTUBE)
    add_snapshot(db, account, date(2026, 10, 1), followers=1000, subs_gained=4, subs_lost=1)
    add_snapshot(db, account, date(2026, 10, 2), followers=1000, subs_gained=2, subs_lost=0)
    db.commit()
    bal = analytics.follower_balance(db, account, date(2026, 10, 1), date(2026, 10, 2))
    assert bal["native"] is True and bal["net"] is None
    assert bal["gained"] == 6 and bal["lost"] == 1
    k = analytics.kpis(db, account, date(2026, 10, 1), date(2026, 10, 2))
    assert "engagement" not in k and "post_interactions" not in k
    assert k["subs_gained"]["value"] == 6


def test_series_has_one_point_per_day(db: Session, brand) -> None:
    account = make_account(db, brand, Network.FACEBOOK)
    add_snapshot(db, account, date(2026, 10, 1), views=5)
    add_snapshot(db, account, date(2026, 10, 3), views=7)
    db.commit()
    s = analytics.series(db, account, "views", date(2026, 10, 1), date(2026, 10, 4))
    assert s == [
        (date(2026, 10, 1), 5.0),
        (date(2026, 10, 2), None),
        (date(2026, 10, 3), 7.0),
        (date(2026, 10, 4), None),
    ]


def test_posts_per_day_uses_brand_timezone(db: Session, brand) -> None:
    account = make_account(db, brand, Network.INSTAGRAM)
    # 23:30 UTC on Oct 1 is Oct 2 in Madrid (UTC+2)
    add_post_metric(db, account, "late", datetime(2026, 10, 1, 23, 30, tzinfo=UTC), likes=1)
    add_post_metric(db, account, "early", datetime(2026, 10, 1, 8, 0, tzinfo=UTC), likes=1)
    db.commit()
    counts = dict(analytics.posts_per_day(db, account, date(2026, 10, 1), date(2026, 10, 2)))
    assert counts == {date(2026, 10, 1): 1, date(2026, 10, 2): 1}


def test_top_posts_and_rows(db: Session, brand) -> None:
    account = make_account(db, brand, Network.INSTAGRAM)
    base = datetime(2026, 10, 2, 10, tzinfo=UTC)
    add_post_metric(db, account, "a", base, likes=10, reach=100)  # 10 %
    add_post_metric(db, account, "b", base + timedelta(hours=1), likes=30, reach=100)  # 30 %
    add_post_metric(db, account, "c", base + timedelta(hours=2), views=500)  # no engagement
    db.commit()
    top = analytics.top_posts(db, account, date(2026, 10, 1), date(2026, 10, 3))
    assert [r["external_id"] for r in top] == ["b", "a"]
    assert top[0]["engagement"] == 30.0 and top[0]["interactions"] == 30
    by_views = analytics.top_posts(db, account, date(2026, 10, 1), date(2026, 10, 3), metric="views")
    assert [r["external_id"] for r in by_views] == ["c"]
    rows = analytics.post_rows(db, account, date(2026, 10, 1), date(2026, 10, 3))
    assert [r["external_id"] for r in rows] == ["c", "b", "a"]  # newest first
    assert rows[0]["media_type_label"] == "Imagen"
    assert rows[0]["local"].hour == 14  # 12:00 UTC → 14:00 Madrid


# ------------------------------------------------------------------------------------------ best times


def test_best_times_falls_back_to_general_pattern(db: Session, brand, clock) -> None:
    account = make_account(db, brand, Network.INSTAGRAM)
    for i in range(analytics.BEST_TIMES_MIN_POSTS - 1):
        when = clock.now() - timedelta(days=i + 1)
        t = published_target(db, brand, account, when, f"ig-{i}")
        add_post_metric(db, account, f"ig-{i}", when, target_id=t.id, engagement=5.0)
    db.commit()
    result = analytics.best_times(db, brand.id, "instagram", clock)
    assert result["source"] == "patron_general"
    assert result["posts"] == analytics.BEST_TIMES_MIN_POSTS - 1
    assert result["matrix"] == analytics.general_pattern("instagram")
    matrix = result["matrix"]
    assert len(matrix) == 7 and all(len(row) == 24 for row in matrix)
    assert max(max(row) for row in matrix) == 1.0
    assert min(min(row) for row in matrix) >= 0.0
    # each network has its own pattern; nights are worse than evenings
    assert analytics.general_pattern("tiktok") != analytics.general_pattern("facebook")
    assert matrix[2][3] < matrix[2][20]


def test_best_times_from_our_posts(db: Session, brand, clock) -> None:
    account = make_account(db, brand, Network.INSTAGRAM)
    other = make_account(db, brand, Network.YOUTUBE)
    now = clock.now()  # Tuesday 2026-11-03 09:00 UTC
    # 6 great posts on Tuesdays 17:00 Madrid, 6 poor ones on Mondays 09:00 Madrid (across the DST change).
    madrid = ZoneInfo("Europe/Madrid")
    for i in range(6):
        great = (datetime(2026, 10, 27, 17, 0, tzinfo=madrid) - timedelta(weeks=i)).astimezone(UTC)
        poor = (datetime(2026, 10, 26, 9, 0, tzinfo=madrid) - timedelta(weeks=i)).astimezone(UTC)
        t1 = published_target(db, brand, account, great, f"g{i}")
        t2 = published_target(db, brand, account, poor, f"p{i}", status=TargetStatus.MANUAL_DONE)
        add_post_metric(db, account, f"g{i}", great, target_id=t1.id, interactions=90, reach=1000)
        add_post_metric(db, account, f"p{i}", poor, target_id=t2.id, engagement=1.0)
    # not ours / not published / too old / YouTube: ignored for "all networks"
    add_post_metric(db, account, "foreign", now - timedelta(days=2), engagement=99.0)
    t_err = published_target(db, brand, account, now - timedelta(days=3), "err", status=TargetStatus.ERROR)
    add_post_metric(db, account, "err", now - timedelta(days=3), target_id=t_err.id, engagement=99.0)
    t_old = published_target(db, brand, account, now - timedelta(days=500), "old")
    add_post_metric(db, account, "old", now - timedelta(days=500), target_id=t_old.id, engagement=99.0)
    t_yt = published_target(db, brand, other, now - timedelta(days=4), "yt")
    add_post_metric(db, other, "yt", now - timedelta(days=4), target_id=t_yt.id, views=5000)
    db.commit()

    result = analytics.best_times(db, brand.id, None, clock)
    assert result["source"] == "datos" and result["posts"] == 12
    m = result["matrix"]
    assert m[1][17] == 1.0  # Tuesday 17:00 local is the best slot
    assert m[0][9] < 0.4  # Monday 09:00 performed badly
    assert all(0.0 <= v <= 1.0 for row in m for v in row)
    best = max((v, d, h) for d, row in enumerate(m) for h, v in enumerate(row))
    assert (best[1], best[2]) == (1, 17)

    # per network: Instagram has the data; YouTube alone has too few posts
    assert analytics.best_times(db, brand.id, "instagram", clock)["source"] == "datos"
    yt = analytics.best_times(db, brand.id, "youtube", clock)
    assert yt == {"matrix": analytics.general_pattern("youtube"), "source": "patron_general", "posts": 1}


def test_best_times_matrix_unexplored_slots_stay_below_observed() -> None:
    samples = [(4, 12, 4.0)] * 10
    m = analytics.best_times_matrix(samples, "facebook")
    assert m[4][12] == 1.0
    assert max(v for d, row in enumerate(m) for h, v in enumerate(row) if abs(d - 4) > 0 and h != 12) < 1.0
    assert analytics.best_times_matrix([], "facebook") == analytics.general_pattern("facebook")


# ------------------------------------------------------------------------------------------- snapshots


def test_snapshot_all_with_demo_provider(db: Session, brand, demo_account, clock, http) -> None:
    summary = analytics.snapshot_all(db, http, clock, settings(demo=True))
    assert summary["accounts"] == 1 and summary["ok"] == 1 and summary["errors"] == 0
    days = list(
        db.scalars(select(AnalyticsSnapshot.day).where(AnalyticsSnapshot.account_id == demo_account.id))
    )
    assert len(days) == analytics.DEMO_BACKFILL_DAYS  # demo history backfilled on the first run
    today = date(2026, 11, 3)
    assert max(days) == today
    posts = list(db.scalars(select(PostMetric).where(PostMetric.account_id == demo_account.id)))
    assert posts and summary["posts"] == len(posts)
    assert all(p.caption.startswith("[Demo]") for p in posts)
    assert all(p.metrics.get("engagement") is not None for p in posts)
    db.refresh(demo_account)
    assert demo_account.meta["analytics_last_ok"] == clock.now().isoformat()
    assert "analytics_error" not in demo_account.meta

    # second run: only today is fetched again (upsert, no duplicates)
    clock.advance(hours=3)
    summary2 = analytics.snapshot_all(db, http, clock, settings(demo=True))
    assert summary2["snapshots"] == 1
    snaps = list(db.scalars(select(AnalyticsSnapshot).where(AnalyticsSnapshot.account_id == demo_account.id)))
    assert len(snaps) == analytics.DEMO_BACKFILL_DAYS
    assert len(list(db.scalars(select(PostMetric).where(PostMetric.account_id == demo_account.id)))) == len(
        posts
    )


def test_snapshot_all_skips_demo_without_demo_mode_and_disconnected(
    db: Session, brand, demo_account, clock, http
):
    make_account(db, brand, Network.TIKTOK, status=AccountStatus.NEEDS_RECONNECT)
    fake = registry.register(FakeProvider("tiktok"))
    summary = analytics.snapshot_all(db, http, clock, settings(demo=False))
    assert summary["accounts"] == 0
    assert fake.calls == []
    assert db.scalar(select(AnalyticsSnapshot.id)) is None


def test_snapshot_all_captures_errors_per_account(db: Session, brand, demo_account, clock, http) -> None:
    fb = make_account(db, brand, Network.FACEBOOK)
    ig = make_account(db, brand, Network.INSTAGRAM)
    registry.register(
        FakeProvider(
            "facebook",
            error=ProviderError(ErrorKind.AUTH, "token_expired", "La sesión de Facebook ha caducado."),
        )
    )
    registry.register(FakeProvider("instagram", error=RuntimeError("boom access_token=SECRET123")))
    summary = analytics.snapshot_all(db, http, clock, settings(demo=True))
    assert summary["accounts"] == 3 and summary["ok"] == 1 and summary["errors"] == 2
    db.refresh(fb)
    db.refresh(ig)
    db.refresh(demo_account)
    assert fb.meta["analytics_error"]["message"] == "La sesión de Facebook ha caducado."
    assert fb.meta["analytics_error"]["code"] == "token_expired"
    assert fb.meta["analytics_error"]["kind"] == "auth"
    assert fb.status == AccountStatus.CONNECTED  # recording only; reconnect flags belong to the token job
    assert "SECRET123" not in str(ig.meta["analytics_error"])
    assert ig.meta["analytics_error"]["kind"] == "internal"
    assert "analytics_error" not in demo_account.meta
    errors = {d["network"]: d for d in summary["details"] if d["status"] == "error"}
    assert set(errors) == {"facebook", "instagram"}

    # the next successful run clears the error and records the provider notice
    registry.register(
        FakeProvider("facebook", snapshot=_NoticeSnapshot(day=date(2026, 11, 3), metrics={"followers": 50}))
    )
    analytics.snapshot_all(db, http, clock, settings(demo=True), account_ids=[fb.id])
    db.refresh(fb)
    assert "analytics_error" not in fb.meta
    assert fb.meta["analytics_notice"] == "Falta un permiso."


class _NoticeSnapshot(AccountSnapshot):
    notice = "Falta un permiso."


def test_snapshot_all_links_targets_and_merges_history(db: Session, brand, clock, http) -> None:
    fb = make_account(db, brand, Network.FACEBOOK, external_id="PAGE")
    yt = make_account(db, brand, Network.YOUTUBE)
    when = clock.now() - timedelta(days=2)
    t_fb = published_target(db, brand, fb, when, "POST1")  # our id without the page prefix
    t_yt = published_target(db, brand, yt, when, "vid-1")
    db.commit()

    registry.register(
        FakeProvider(
            "facebook",
            snapshot=AccountSnapshot(
                day=date(2026, 11, 3), metrics={"followers": 321, "views": 40, "x": "n/a"}
            ),
            posts=[
                PostMetricData(
                    "PAGE_POST1", when, "https://fb/p/1", "Hola", "PHOTO", None, {"likes": 5, "reach": 50}
                ),
                PostMetricData("PAGE_OTHER", when, None, "Ajeno", "TEXT", None, {"likes": 1}),
            ],
        )
    )

    class _YtSnap(AccountSnapshot):
        pass

    yt_snap = _YtSnap(day=date(2026, 11, 3), metrics={"followers": 1200, "posts_count": 9})
    yt_snap.history = [AccountSnapshot(day=date(2026, 10, 31), metrics={"views": 77, "subs_gained": 3})]
    registry.register(
        FakeProvider(
            "youtube",
            snapshot=yt_snap,
            posts=[
                PostMetricData("vid-1", when, None, "Vídeo", "video", None, {"views": 90, "interactions": 9})
            ],
        )
    )
    # a pre-existing YouTube day keeps its other metrics when history is merged in
    add_snapshot(db, yt, date(2026, 10, 31), followers=1190)
    db.commit()

    summary = analytics.snapshot_all(db, http, clock, settings(demo=False))
    assert summary["ok"] == 2

    rows = {p.external_id: p for p in db.scalars(select(PostMetric))}
    assert rows["PAGE_POST1"].target_id == t_fb.id
    assert rows["PAGE_POST1"].metrics == {"likes": 5, "reach": 50, "interactions": 5, "engagement": 10.0}
    assert rows["PAGE_OTHER"].target_id is None
    assert rows["vid-1"].target_id == t_yt.id
    assert rows["vid-1"].metrics == {"views": 90}  # native only

    fb_snap = db.scalar(
        select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.account_id == fb.id, AnalyticsSnapshot.day == date(2026, 11, 3)
        )
    )
    assert fb_snap.metrics == {"followers": 321, "views": 40}
    old = db.scalar(
        select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.account_id == yt.id, AnalyticsSnapshot.day == date(2026, 10, 31)
        )
    )
    assert old.metrics == {"followers": 1190, "views": 77, "subs_gained": 3}


def test_snapshot_day_is_brand_local(db: Session, brand, clock, http) -> None:
    brand.timezone = "America/Mexico_City"
    db.commit()
    account = make_account(db, brand, Network.TIKTOK)
    fake = registry.register(FakeProvider("tiktok"))
    clock.set(datetime(2026, 11, 4, 3, 0, tzinfo=UTC))  # still Nov 3 in Mexico City
    analytics.snapshot_all(db, http, clock, settings(demo=False))
    assert fake.calls == [date(2026, 11, 3)]
    assert db.scalar(select(AnalyticsSnapshot.day).where(AnalyticsSnapshot.account_id == account.id)) == date(
        2026, 11, 3
    )


def test_background_refresh(app, db: Session, brand, demo_account, clock, http) -> None:
    started = analytics.start_background_refresh(
        app.state.db.session, clock, settings(demo=True), http_factory=lambda: http, wait=True
    )
    assert started is True
    status = analytics.refresh_status()
    assert status["running"] is False and status["summary"]["ok"] == 1 and status["error"] is None
    assert db.scalar(select(AnalyticsSnapshot.id).where(AnalyticsSnapshot.account_id == demo_account.id))


# ------------------------------------------------------------------------------------------------ CSV


def test_posts_csv_format(db: Session, brand) -> None:
    account = make_account(db, brand, Network.INSTAGRAM)
    add_post_metric(
        db, account, "a", datetime(2026, 10, 2, 10, tzinfo=UTC), likes=3, comments=1, reach=30, views=60
    ).caption = "Línea 1\nLínea 2; con punto y coma"
    db.commit()
    rows = analytics.post_rows(db, account, date(2026, 10, 1), date(2026, 10, 3))
    text = analytics.posts_csv(rows, "instagram")
    assert text.startswith("﻿")
    parsed = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    header, row = parsed[0], parsed[1]
    assert header[:4] == ["fecha", "hora", "texto", "tipo"]
    rec = dict(zip(header, row, strict=True))
    assert rec["fecha"] == "2026-10-02" and rec["hora"] == "12:00"
    assert rec["texto"] == "Línea 1 Línea 2; con punto y coma"
    assert rec["engagement_%"] == "13,33" and rec["interacciones"] == "4"
    assert rec["publicado_desde_la_app"] == "no"

    yt_header = next(csv.reader(io.StringIO(analytics.posts_csv([], "youtube").lstrip("﻿")), delimiter=";"))
    assert "engagement_%" not in yt_header and "minutos_vistos" in yt_header


class _TitledVideo(PostMetricData):
    """Like ``YouTubePostMetricData``: the provider's post rows carry the video title."""

    def __init__(self, *args, title: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.title = title


def test_assisted_youtube_upload_is_linked_by_title(db: Session, brand, clock, http) -> None:
    """Publicación asistida on YouTube: the user uploaded it in YouTube Studio; the next snapshot links the
    video to our target by exact title within ±48 h (and marks a pending one as done)."""
    from planificador.models import Notification, PostEvent

    yt = make_account(db, brand, Network.YOUTUBE)
    when = clock.now() - timedelta(hours=5)
    post = Post(brand_id=brand.id, text="Descripción del vídeo", scheduled_at=when)
    db.add(post)
    db.flush()
    pending = PostTarget(
        post_id=post.id,
        account_id=yt.id,
        network=Network.YOUTUBE,
        format="video",
        status=TargetStatus.MANUAL_PENDING,
        auto_publish=False,
        scheduled_at=when,
        options={"title": "Cómo preparamos el café"},
    )
    other = PostTarget(
        post_id=post.id,
        account_id=yt.id,
        network=Network.YOUTUBE,
        format="video",
        status=TargetStatus.MANUAL_DONE,
        auto_publish=False,
        scheduled_at=when - timedelta(days=10),  # same title but outside the ±48 h window
        options={"title": "Cómo preparamos el café"},
    )
    db.add_all([pending, other])
    db.flush()
    db.add(
        Notification(
            kind="manual_publish", title="Es hora de publicar en YouTube", body="", target_id=pending.id
        )
    )
    db.commit()

    uploaded = when + timedelta(hours=2)
    registry.register(
        FakeProvider(
            "youtube",
            posts=[
                _TitledVideo(
                    "VID123",
                    uploaded,
                    "https://youtu.be/VID123",
                    "",
                    "video",
                    None,
                    {"views": 5},
                    title="  cómo preparamos el CAFÉ ",
                ),
                _TitledVideo("VID999", uploaded, None, "", "video", None, {"views": 1}, title="Otro vídeo"),
            ],
        )
    )
    analytics.snapshot_all(db, http, clock, settings(demo=False))
    db.expire_all()
    rows = {p.external_id: p for p in db.scalars(select(PostMetric))}
    assert rows["VID123"].target_id == pending.id and rows["VID999"].target_id is None
    target = db.get(PostTarget, pending.id)
    assert target.status == TargetStatus.MANUAL_DONE and target.external_id == "VID123"
    assert target.external_url == "https://youtu.be/VID123" and target.published_at == uploaded
    assert db.get(PostTarget, other.id).external_id is None
    assert db.scalar(select(PostEvent.event).where(PostEvent.target_id == pending.id)) == "youtube_linked"
    assert db.scalar(select(Notification.read_at).where(Notification.target_id == pending.id)) is not None

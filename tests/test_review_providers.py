"""Regression tests for the providers review (engine + scheduler + real provider, HTTP mocked)."""

from __future__ import annotations

import random
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from planificador import config
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
from planificador.providers import followups, tiktok, youtube
from planificador.scheduler import Scheduler, default_jobs

UPLOAD_HOST = "https://open-upload.tiktokapis.com/"
U1 = "https://open-upload.tiktokapis.com/video/?upload_id=111"
U2 = "https://open-upload.tiktokapis.com/video/?upload_id=222"


class SimulatedCrash(BaseException):
    """The app dies in the middle of an HTTP request (not an Exception: nothing catches it)."""


def _ok(data: dict) -> dict:
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "x"}}


@pytest.fixture
def small_tiktok_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tiktok, "SINGLE_CHUNK_MAX", 1000)
    monkeypatch.setattr(tiktok, "CHUNK_SIZE", 300)
    monkeypatch.setattr(tiktok, "UPLOAD_BLOCK", 64)
    monkeypatch.setattr(tiktok, "_sleep", lambda s: None)


def _tiktok_target(app, db, clock) -> int:
    brand = Brand(name="M")
    db.add(brand)
    db.flush()
    acc = Account(
        brand_id=brand.id,
        network="tiktok",
        external_id="oid",
        name="TT",
        username="tt",
        account_type="creator",
        status=AccountStatus.CONNECTED,
    )
    acc.set_tokens(
        "acc",
        "ref",
        token_expires_at=clock.now() + timedelta(days=5),
        refresh_expires_at=clock.now() + timedelta(days=300),
    )
    db.add(acc)
    db.commit()
    rel = "originals/aa/" + "a" * 64 + ".mp4"
    path = app.state.paths.media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(i % 251 for i in range(1100)))  # 3 chunks: 300, 300, 500
    asset = MediaAsset(
        kind=MediaKind.VIDEO,
        original_name="v.mp4",
        rel_path=rel,
        sha256="a" * 64,
        mime="video/mp4",
        size_bytes=1100,
        width=1080,
        height=1920,
        duration_s=12.0,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        bitrate=4_000_000,
    )
    db.add(asset)
    db.commit()
    post = Post(brand_id=brand.id, text="hola", scheduled_at=clock.now(), kind=PostKind.SCHEDULED)
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=acc.id,
        network="tiktok",
        format="video",
        options={"privacy_level": "SELF_ONLY", "consent": True},
        auto_publish=True,
        status=TargetStatus.SCHEDULED,
        scheduled_at=clock.now(),
        media_override=[asset.id],
    )
    db.add(target)
    db.commit()
    return target.id


def test_tiktok_crash_during_resent_final_chunk_never_posts_twice(app, db, clock, small_tiktok_chunks):
    """Chunk 2 fails 5 times (TRANSIENT → reconcile "not_published" → retry), then the app dies during the
    final-chunk PUT after TikTok got the bytes. On restart the engine must reconcile (COMMIT_SENT was
    re-asserted when the upload resumed) and status/fetch decides: one init, one post."""
    tid = _tiktok_target(app, db, clock)
    state = {"mode": "net", "net_errors": 0}

    def put_side(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Content-Range"].split(" ")[1].split("-")[0])
        if str(request.url).startswith(U1):
            if first == 300 and state["net_errors"] < 5:
                state["net_errors"] += 1
                raise httpx.ConnectError("boom")
            if first == 600:
                if state["mode"] == "net":
                    state["mode"] = "crashed"
                    request.read()
                    raise SimulatedCrash()
                return httpx.Response(400)  # duplicate PUT after the upload completed
        return httpx.Response(201 if first == 600 else 206)

    with respx.mock(assert_all_mocked=True) as r:
        r.post(tiktok.CREATOR_INFO_URL).mock(
            return_value=httpx.Response(
                200,
                json=_ok(
                    {
                        "creator_username": "tt",
                        "creator_nickname": "TT",
                        "privacy_level_options": ["SELF_ONLY"],
                        "comment_disabled": False,
                        "duet_disabled": False,
                        "stitch_disabled": False,
                        "max_video_post_duration_sec": 600,
                    }
                ),
            )
        )
        init = r.post(tiktok.VIDEO_INIT_URL).mock(
            side_effect=[
                httpx.Response(200, json=_ok({"publish_id": "PUB1", "upload_url": U1})),
                httpx.Response(200, json=_ok({"publish_id": "PUB2", "upload_url": U2})),
            ]
        )
        r.put(url__startswith=UPLOAD_HOST).mock(side_effect=put_side)
        r.post(tiktok.STATUS_URL).mock(
            return_value=httpx.Response(200, json=_ok({"status": "PUBLISH_COMPLETE"}))
        )

        sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(7))
        crashed = False
        for _ in range(40):
            try:
                sched.tick()
            except SimulatedCrash:
                crashed = True
                break
            db.expire_all()
            row = db.get(PostTarget, tid)
            if row.next_attempt_at:
                clock.set(row.next_attempt_at)
        sched.close()
        assert crashed
        db.expire_all()
        row = db.get(PostTarget, tid)
        assert row.status == TargetStatus.PUBLISHING
        assert row.commit_phase == CommitPhase.COMMIT_SENT  # re-asserted when the upload resumed

        clock.advance(seconds=30)
        sched2 = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(11))
        for _ in range(30):
            sched2.tick()
            db.expire_all()
            row = db.get(PostTarget, tid)
            if row.status != TargetStatus.PUBLISHING:
                break
            if row.next_attempt_at:
                clock.set(row.next_attempt_at)
        sched2.close()

    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.PUBLISHED and row.external_id == "PUB1"
    assert init.call_count == 1
    assert not (row.provider_state or {}).get("abandoned")
    events = list(db.scalars(select(PostEvent.event).where(PostEvent.target_id == tid)))
    assert "tiktok_upload_restart" not in events


# ------------------------------------------------------------------------------ follow-up job (followups)


def _published_target(db, clock, network: str, state: dict, **account_kw) -> int:
    brand = Brand(name="F")
    db.add(brand)
    db.flush()
    acc = Account(
        brand_id=brand.id,
        network=network,
        external_id=account_kw.pop("external_id", "acc-ext"),
        name="Cuenta",
        username=account_kw.pop("username", "tt"),
        account_type="creator",
        status=AccountStatus.CONNECTED,
        meta=account_kw.pop("meta", {}),
    )
    acc.set_tokens(
        "acc",
        "ref",
        token_expires_at=clock.now() + timedelta(days=5),
        refresh_expires_at=clock.now() + timedelta(days=300),
    )
    db.add(acc)
    db.flush()
    post = Post(brand_id=brand.id, text="hola", scheduled_at=clock.now(), kind=PostKind.SCHEDULED)
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=acc.id,
        network=network,
        format="video",
        options={"privacy_level": "PUBLIC_TO_EVERYONE", "title": "Mi vídeo", "privacy_status": "public"},
        auto_publish=True,
        status=TargetStatus.PUBLISHED,
        scheduled_at=clock.now() - timedelta(hours=2),
        published_at=clock.now() - timedelta(hours=2),
        commit_phase=CommitPhase.COMMIT_SENT,
        provider_state=state,
        external_id=state.get("external_id"),
    )
    db.add(target)
    db.commit()
    return target.id


def test_followup_job_is_registered_hourly():
    job = next(j for j in default_jobs() if j.name == "provider_followups")
    assert job.interval == timedelta(hours=1)


def test_followups_resolve_tiktok_public_id(app, db, clock):
    pid = "v_pub_file~v2-1.7300000000000000001"
    state = {
        "phase": "done",
        "result": "published",
        "mode": "direct",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "publish_id": pid,
        "external_id": pid,
        "creator_username": "tt",
        "finished_at": (clock.now() - timedelta(hours=2)).isoformat(),
        "followup": {"kind": "public_id", "next_at": (clock.now() - timedelta(minutes=1)).isoformat()},
        "_engine": {"keep": 1},
    }
    tid = _published_target(db, clock, "tiktok", state)
    with respx.mock(assert_all_mocked=True) as r, httpx.Client() as http:
        r.post(tiktok.STATUS_URL).respond(
            200,
            json=_ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000000000777]}),
        )
        summary = followups.run_due(db, http, clock, config.provider_settings(db))
    assert summary["checked"] == 1 and summary["updated"] == 1
    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.PUBLISHED
    assert row.external_id == "7300000000000000777"
    assert row.external_url == "https://www.tiktok.com/@tt/video/7300000000000000777"
    assert "followup" not in row.provider_state and row.provider_state["_engine"] == {"keep": 1}
    events = list(db.scalars(select(PostEvent.event).where(PostEvent.target_id == tid)))
    assert "followup" in events


def test_followups_not_due_are_left_alone(app, db, clock):
    state = {
        "publish_id": "x",
        "followup": {"kind": "public_id", "next_at": (clock.now() + timedelta(minutes=30)).isoformat()},
    }
    tid = _published_target(db, clock, "tiktok", state)
    with respx.mock(assert_all_mocked=True), httpx.Client() as http:
        summary = followups.run_due(db, http, clock, config.provider_settings(db))
    assert summary["checked"] == 0
    db.expire_all()
    assert db.get(PostTarget, tid).provider_state["followup"]["kind"] == "public_id"


def test_followups_youtube_rejected_after_publish_at_fails_the_target(app, db, clock):
    state = {
        "phase": "done",
        "result": "published",
        "video_id": "dQw4w9WgXcQ",
        "external_id": "dQw4w9WgXcQ",
        "requested_privacy": "public",
        "native_scheduled": True,
        "publish_at": (clock.now() - timedelta(hours=1)).isoformat(),
        "followup": {"kind": "publish_at", "next_at": (clock.now() - timedelta(minutes=5)).isoformat()},
    }
    tid = _published_target(
        db,
        clock,
        "youtube",
        state,
        external_id="UCabcdefghijklmnopqrstuv",
        meta={"granted_scopes": list(youtube.SCOPES_YT)},
    )
    with respx.mock(assert_all_mocked=True) as r, httpx.Client() as http:
        r.get(f"{youtube.API_BASE}/videos").respond(
            200,
            json={
                "items": [
                    {
                        "id": "dQw4w9WgXcQ",
                        "status": {
                            "privacyStatus": "private",
                            "uploadStatus": "rejected",
                            "rejectionReason": "duplicate",
                        },
                    }
                ]
            },
        )
        summary = followups.run_due(db, http, clock, config.provider_settings(db))
    assert summary["failed"] == 1
    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.ERROR and row.last_error
    assert row.provider_state["phase"] == "failed" and "followup" not in row.provider_state
    kinds = list(db.scalars(select(Notification.kind).where(Notification.target_id == tid)))
    assert "error" in kinds


def test_followups_youtube_lock_after_publish_at_updates_the_account(app, db, clock):
    publish_at = clock.now() - timedelta(hours=1)
    state = {
        "phase": "done",
        "result": "published",
        "video_id": "dQw4w9WgXcQ",
        "requested_privacy": "public",
        "native_scheduled": True,
        "publish_at": publish_at.isoformat(),
        "followup": {"kind": "publish_at", "next_at": (clock.now() - timedelta(minutes=5)).isoformat()},
    }
    tid = _published_target(
        db, clock, "youtube", state, external_id="UCx", meta={"granted_scopes": list(youtube.SCOPES_YT)}
    )
    with respx.mock(assert_all_mocked=True) as r, httpx.Client() as http:
        r.get(f"{youtube.API_BASE}/videos").respond(
            200,
            json={
                "items": [
                    {
                        "id": "dQw4w9WgXcQ",
                        "status": {
                            "privacyStatus": "private",
                            "uploadStatus": "processed",
                            "publishAt": publish_at.isoformat().replace("+00:00", "Z"),
                        },
                        "processingDetails": {"processingStatus": "succeeded"},
                    }
                ]
            },
        )
        followups.run_due(db, http, clock, config.provider_settings(db))
    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.PUBLISHED
    account = db.get(Account, row.account_id)
    assert account.meta[youtube.META_LOCK_STATUS] == "locked"
    assert account.meta[youtube.META_PUBLISH_MODE] == "assisted"
    assert "account_meta_updates" not in row.provider_state
    notes = list(db.scalars(select(Notification).where(Notification.target_id == tid)))
    assert notes and notes[0].kind == "error"


def test_followups_confirm_youtube_lock_test_after_24h(app, db, clock):
    brand = Brand(name="Y")
    db.add(brand)
    db.flush()
    finished = clock.now() - timedelta(hours=25)
    acc = Account(
        brand_id=brand.id,
        network="youtube",
        external_id="UCx",
        name="Canal",
        account_type="channel",
        status=AccountStatus.CONNECTED,
        meta={
            "uploads_playlist_id": "UUx",
            youtube.META_LOCK_STATUS: "unlocked",
            youtube.META_PUBLISH_MODE: "api",
            "youtube_lock_test": {"state": "done", "result": "unlocked", "finished_at": finished.isoformat()},
        },
    )
    acc.set_tokens("acc", "ref", token_expires_at=clock.now() + timedelta(days=1))
    db.add(acc)
    db.commit()
    with respx.mock(assert_all_mocked=True) as r, httpx.Client() as http:
        r.get(f"{youtube.API_BASE}/playlistItems").respond(
            200, json={"items": [{"contentDetails": {"videoId": "lockvid0001"}}]}
        )
        r.get(f"{youtube.API_BASE}/videos").respond(
            200,
            json={
                "items": [
                    {
                        "id": "lockvid0001",
                        "snippet": {"title": youtube.LOCK_TEST_TITLE, "publishedAt": finished.isoformat()},
                        "status": {"privacyStatus": "private"},
                    }
                ]
            },
        )
        delete = r.delete(f"{youtube.API_BASE}/videos").respond(204)
        summary = followups.run_due(db, http, clock, config.provider_settings(db))
    assert summary["lock_checks"] == 1 and delete.called
    db.expire_all()
    meta = db.get(Account, acc.id).meta
    assert meta[youtube.META_LOCK_STATUS] == "locked" and meta[youtube.META_PUBLISH_MODE] == "assisted"
    assert meta["youtube_lock_test"]["result"] == "locked" and meta["youtube_lock_test"]["confirmed_at"]
    with respx.mock(assert_all_mocked=True), httpx.Client() as http:  # confirmed once: no more calls
        assert followups.run_due(db, http, clock, config.provider_settings(db))["lock_checks"] == 0


def test_youtube_reconcile_network_error_is_rechecked_later_not_unknown(app, db, clock):
    """Engine path: a TRANSIENT error after the final chunk → reconcile; videos.list fails too (Wi-Fi not up
    yet). The video id came from YouTube, so the engine must check again later instead of "unknown"."""
    brand = Brand(name="Y")
    db.add(brand)
    db.flush()
    acc = Account(
        brand_id=brand.id,
        network="youtube",
        external_id="UCx",
        name="Canal",
        account_type="channel",
        status=AccountStatus.CONNECTED,
        meta={"granted_scopes": list(youtube.SCOPES_YT)},
    )
    acc.set_tokens("acc", "ref", token_expires_at=clock.now() + timedelta(days=1))
    db.add(acc)
    db.flush()
    post = Post(brand_id=brand.id, text="hola", scheduled_at=clock.now(), kind=PostKind.SCHEDULED)
    db.add(post)
    db.flush()
    target = PostTarget(
        post_id=post.id,
        account_id=acc.id,
        network="youtube",
        format="video",
        options={"title": "Mi vídeo", "privacy_status": "public", "made_for_kids": False},
        auto_publish=True,
        status=TargetStatus.PUBLISHING,
        scheduled_at=clock.now(),
        commit_phase=CommitPhase.COMMIT_SENT,
        provider_state={
            "phase": "processing",
            "video_id": "dQw4w9WgXcQ",
            "requested_privacy": "public",
            "final_chunk_at": clock.now().isoformat(),
        },
    )
    db.add(target)
    db.commit()
    tid = target.id
    with respx.mock(assert_all_mocked=True) as r:
        r.get(f"{youtube.API_BASE}/videos").mock(side_effect=httpx.ConnectError("no wifi"))
        sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(3))
        sched.tick()
        sched.close()
    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.PUBLISHING and row.next_attempt_at is not None
    assert row.provider_state["_engine"]["pending"] == "reconcile"
    events = list(db.scalars(select(PostEvent.event).where(PostEvent.target_id == tid)))
    assert "reconcile_retry" in events and "unknown" not in events


def test_followups_crash_is_postponed_not_retried_every_run(app, db, clock):
    from planificador.providers import registry

    class Boom(tiktok.TikTokProvider):
        def follow_up(self, ctx):  # type: ignore[override]
            raise RuntimeError("boom")

    registry.register(Boom())
    state = {"publish_id": "x", "followup": {"kind": "public_id", "next_at": clock.now().isoformat()}}
    tid = _published_target(db, clock, "tiktok", state)
    with httpx.Client() as http:
        summary = followups.run_due(db, http, clock, config.provider_settings(db))
    assert summary["errors"] == 1
    db.expire_all()
    info = db.get(PostTarget, tid).provider_state["followup"]
    assert info["errors"] == 1
    assert followups._parse(info["next_at"]) == clock.now() + followups.FOLLOWUP_INTERVAL
    registry.unregister("tiktok")


def test_tiktok_direct_post_after_video_publish_was_lost_fails_permanently(app, db, clock):
    """A Direct Post scheduled while the account had video.publish, reconnected later without it (sandbox app
    without Direct Post): the engine fails it once, with a clear Spanish error, without calling TikTok."""
    tid = _tiktok_target(app, db, clock)
    row = db.get(PostTarget, tid)
    row.account.scopes = ["user.info.basic", "video.upload"]
    db.commit()
    with respx.mock(assert_all_mocked=True) as r:  # no route: any request to TikTok would fail the test
        sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(3))
        sched.tick()
        sched.close()
        assert not r.calls
    db.expire_all()
    row = db.get(PostTarget, tid)
    assert row.status == TargetStatus.ERROR and row.attempts <= 1
    assert "video.publish" in row.last_error and "borrador" in row.last_error
    assert row.commit_phase == CommitPhase.NONE
    detail = row.last_error_detail or {}
    assert detail["code"] == "tiktok_mode_not_granted" and detail["kind"] == "permanent"

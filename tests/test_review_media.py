"""Regression tests for the media-area review: TikTok music consent, public media rate limiter, library
usage counts and deletion, and the wording of the media validation messages."""

from __future__ import annotations

import io
import random
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from planificador import csvio, engine, media, postsvc, rules
from planificador.media import MediaInUseError
from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    MediaAsset,
    MediaKind,
    Post,
    PostKind,
    PostTarget,
    TargetStatus,
)
from planificador.providers import tiktok
from planificador.providers.base import MediaInfo, TargetSpec
from planificador.publicmedia import PublicMediaApp, _RateLimiter
from planificador.scheduler import Scheduler
from test_csvio import make_csv, row
from test_media import make_image
from test_postsvc import make_account

CONSENT_TEXT = "Confirmación de uso de música"


def _codes(issues: list) -> set[str]:
    return {i.code for i in issues if i.level == "error"}


def _info(i: int, kind: str = "video", **kw: Any) -> MediaInfo:
    video = kind == "video"
    values: dict[str, Any] = {
        "id": i,
        "kind": kind,
        "path": Path(f"/nope/{i}"),
        "mime": "video/mp4" if video else "image/jpeg",
        "size_bytes": 1_000_000,
        "width": 1080,
        "height": 1920 if video else 1350,
        "duration_s": 12.0 if video else None,
        "fps": 30.0 if video else None,
        "video_codec": "h264" if video else None,
        "audio_codec": "aac" if video else None,
        "bitrate": 4_000_000 if video else None,
        "is_hdr": False,
        "original_name": f"m{i}.{'mp4' if video else 'jpg'}",
    }
    values.update(kw)
    return MediaInfo(**values)


# ================================================================= [0] TikTok music consent (direct post)


def _ok(data: dict) -> dict:
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "x"}}


def _published_tiktok_post(app: Any, db: Session, clock: Any) -> Post:
    """A TikTok direct post already published with the music consent given."""
    brand = Brand(name="TT")
    db.add(brand)
    db.flush()
    account = Account(
        brand_id=brand.id,
        network="tiktok",
        external_id="oid",
        name="TT",
        username="tt",
        account_type="creator",
        status=AccountStatus.CONNECTED,
    )
    account.set_tokens(
        "acc",
        "ref",
        token_expires_at=clock.now() + timedelta(days=5),
        refresh_expires_at=clock.now() + timedelta(days=300),
    )
    db.add(account)
    rel = "originals/bb/" + "b" * 64 + ".mp4"
    path = app.state.paths.media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(i % 251 for i in range(1100)))
    asset = MediaAsset(
        kind=MediaKind.VIDEO,
        original_name="v.mp4",
        rel_path=rel,
        sha256="b" * 64,
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
    db.flush()
    post = Post(brand_id=brand.id, text="hola", scheduled_at=clock.now(), kind=PostKind.SCHEDULED)
    post.set_media([asset.id])
    post.targets.append(
        PostTarget(
            account_id=account.id,
            network="tiktok",
            format="video",
            options={"mode": "direct", "privacy_level": "SELF_ONLY", "consent": True},
            auto_publish=True,
            status=TargetStatus.PUBLISHED,
            scheduled_at=clock.now(),
        )
    )
    db.add(post)
    db.commit()
    return post


def _spec(db: Session, target: PostTarget) -> TargetSpec:
    assets = [media.to_media_info(a) for a in postsvc.target_media(db, target)]
    return TargetSpec(
        network=str(target.network),
        format=target.format,
        text=postsvc.target_text(target),
        media=assets,
        options=dict(target.options or {}),
        scheduled_at=target.scheduled_at,
        auto_publish=target.auto_publish,
    )


def test_duplicated_tiktok_post_needs_the_consent_again_before_publish_now(app, db, clock) -> None:
    src = _published_tiktok_post(app, db, clock)
    copy_post = postsvc.duplicate_post(db, src.id, clock)
    [target] = copy_post.targets
    assert target.status == TargetStatus.DRAFT and target.options.get("consent") is not True

    # Every gate now refuses the copy until the consent is given again.
    spec = _spec(db, target)
    assert "tiktok_consent" in _codes(rules.validate_target(spec))
    assert "tiktok_consent" in _codes(tiktok.check_post_settings(spec))
    result = postsvc.validate_post(
        db, postsvc.post_input_from_post(copy_post), clock=clock, action="schedule"
    )
    assert "tiktok_consent" in {i.code for i in result.get(target.account_id, [])}

    # "Publicar ahora" from the copy's drawer: the engine's pre-flight stops it before any TikTok call.
    engine.publish_now(db, copy_post.id, clock)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as r:
        creator = r.post(tiktok.CREATOR_INFO_URL).mock(
            return_value=httpx.Response(200, json=_ok({"privacy_level_options": ["SELF_ONLY"]}))
        )
        init = r.post(tiktok.VIDEO_INIT_URL).mock(
            return_value=httpx.Response(200, json=_ok({"publish_id": "P", "upload_url": "https://x/"}))
        )
        sched = Scheduler(app.state.db, clock, inline=True, enable_jobs=False, rng=random.Random(3))
        try:
            for _ in range(3):
                sched.tick()
        finally:
            sched.close()
    assert init.call_count == 0 and creator.call_count == 0
    db.expire_all()
    row_ = db.get(PostTarget, target.id)
    assert row_.status == TargetStatus.ERROR
    assert CONSENT_TEXT in (row_.last_error or "")


def test_missing_consent_is_not_a_consent_but_inbox_mode_needs_none() -> None:
    clip = _info(1)

    def spec(**opts: Any) -> TargetSpec:
        return TargetSpec("tiktok", "video", "hola", [clip], opts, None, True)

    for check in (rules.validate_target, tiktok.check_post_settings):
        assert "tiktok_consent" in _codes(check(spec(privacy_level="SELF_ONLY")))
        assert "tiktok_consent" in _codes(check(spec(privacy_level="SELF_ONLY", consent=False)))
        assert "tiktok_consent" not in _codes(check(spec(privacy_level="SELF_ONLY", consent=True)))
        assert "tiktok_consent" not in _codes(check(spec(mode="inbox")))


def test_csv_preview_still_asks_for_the_consent_in_its_footer(db, brand, clock) -> None:
    make_account(db, brand, "tiktok", account_type="creator")
    raw = make_csv(row(tiktok="si", privacidad_tiktok="solo yo", medios=""))
    preview = csvio.build_preview(db, brand, raw, clock)
    [r] = preview.data_rows
    assert not any(CONSENT_TEXT in i.message for i in r.issues)  # not asked yet: no error in the preview
    refused = csvio.build_preview(db, brand, raw, clock, tiktok_consent=False).data_rows[0]
    assert any(CONSENT_TEXT in i.message for i in refused.errors)
    given = csvio.build_preview(db, brand, raw, clock, tiktok_consent=True).data_rows[0]
    assert not any(CONSENT_TEXT in i.message for i in given.issues)


# ======================================================================= [1] public media rate limiter


class _FakeMonotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_rate_limiter_state_stays_bounded_with_made_up_tokens() -> None:
    limiter = _RateLimiter(60, max_keys=128)
    for _ in range(5000):
        assert limiter.allow(secrets.token_urlsafe(16))
    assert len(limiter) <= 128


def test_rate_limiter_forgets_expired_windows_and_still_limits() -> None:
    clock = _FakeMonotonic()
    limiter = _RateLimiter(3, window_s=60, max_keys=1000, clock=clock)
    for i in range(500):
        limiter.allow(f"token-{i:04d}-abcdefghij")
    assert len(limiter) == 500
    assert [limiter.allow("hot-token-0123456789") for _ in range(4)] == [True, True, True, False]
    clock.t += 61
    assert limiter.allow("hot-token-0123456789")  # a new window
    assert len(limiter) == 1  # every expired window was dropped, not just the requested key's


def test_public_app_with_random_tokens_keeps_bounded_state(app, db, clock) -> None:
    papp = PublicMediaApp(app.state.db.session, app.state.settings.data_dir, clock=clock.now)
    papp.limiter = _RateLimiter(60, max_keys=16)
    with TestClient(papp) as client:
        for _ in range(60):
            assert client.get(f"/m/{secrets.token_urlsafe(16)}").status_code == 404
    assert len(papp.limiter) <= 16


# ============================================================== [2] usage counted per post, not network


def test_detail_counts_posts_not_networks(client: TestClient, db: Session, brand: Brand) -> None:
    asset = media.ingest(db, io.BytesIO(make_image("PNG", (20, 20))), "reel.png", brand.id)
    accounts = [make_account(db, brand, n) for n in ("facebook", "instagram", "tiktok", "youtube")]
    for _ in range(2):
        post = Post(brand_id=brand.id, text="Hola")
        post.set_media([asset])
        for account in accounts:  # one post sent to four networks
            post.targets.append(
                PostTarget(
                    account_id=account.id,
                    network=account.network,
                    format="post",
                    status=TargetStatus.SCHEDULED,
                )
            )
        db.add(post)
    db.commit()
    usage = media.pending_usage(db, asset.id)
    assert usage.total == 2 and len(usage.posts) == 2
    hx = {"HX-Request": "true", "HX-Target": "media-detail"}
    detail = client.get(f"/medios/{asset.id}", headers=hx).text
    assert "Se usa en 2 publicaciones pendientes" in detail
    assert 'title="Se usa en 2 publicaciones pendientes"' in detail
    assert "8 publicaciones" not in detail
    assert "2 publicaciones</dd>" in detail  # "Usado en" agrees
    with pytest.raises(MediaInUseError) as exc:
        media.delete_asset(db, asset.id)
    assert "2 publicaciones pendientes" in exc.value.message and exc.value.count == 2


# ================================================================= [3] media validation message wording


def _target(network: str, fmt: str, media_: list[MediaInfo]) -> TargetSpec:
    return TargetSpec(network, fmt, "hola", media_, rules.default_options(network, fmt), None, True)


def _photo(i: int) -> MediaInfo:
    return _info(i, "image")


def _message(issues: list, code: str) -> str:
    return next(i.message for i in issues if i.code == code)


def test_media_messages_are_not_circular_nor_badly_pluralised() -> None:
    tt = rules.validate_target(_target("tiktok", "video", []))
    assert _message(tt, "media_required") == "Añade un vídeo para publicar en TikTok."
    yt = rules.validate_target(_target("youtube", "video", []))
    assert _message(yt, "media_required") == "Añade un vídeo para publicar en YouTube."
    reel = rules.validate_target(_target("facebook", "reel", []))
    assert _message(reel, "media_required") == "El reel de Facebook necesita un vídeo."

    photos = [_photo(1), _photo(2)]
    tt2 = rules.validate_target(_target("tiktok", "video", photos))
    assert _message(tt2, "media_too_many") == "TikTok solo admite un archivo por publicación (hay 2)."
    short = rules.validate_target(_target("youtube", "short", photos))
    assert _message(short, "media_too_many") == "El Short de YouTube solo admite un archivo (hay 2)."
    clip = _info(3)
    two = rules.validate_target(_target("tiktok", "video", [clip, clip]))
    assert _message(two, "media_one_video") == "TikTok solo admite un vídeo por publicación."
    many = rules.validate_target(_target("facebook", "post", [_photo(i) for i in range(11)]))
    assert (
        _message(many, "media_too_many") == "La publicación de Facebook admite como máximo 10 fotos (hay 11)."
    )

    every = [
        i.message
        for net, fmt in rules.FORMAT_RULES
        for media_ in ([], photos, [clip, clip])
        for i in rules.validate_target(_target(net, fmt, media_))
    ]
    assert not [m for m in every if " 1 archivos" in m or " 1 fotos" in m]
    assert not [m for m in every if "El vídeo de" in m and "necesita un vídeo" in m]


# ============================================================ [4] deleting media still used by posts


def _asset(db: Session, brand: Brand, name: str) -> MediaAsset:
    return media.ingest(db, io.BytesIO(make_image("PNG", (21, 21), (len(name), 9, 9))), name, brand.id)


def test_delete_media_used_by_targetless_draft(db: Session, brand: Brand, clock) -> None:
    asset = _asset(db, brand, "borrador.png")
    draft = postsvc.create_post(
        db,
        postsvc.PostInput(brand_id=brand.id, text="Sin redes", media_ids=[asset.id], scheduled_at=None),
        "draft",
        clock,
    )
    assert not draft.targets
    with pytest.raises(MediaInUseError) as exc:
        media.delete_asset(db, asset.id, clock)
    assert "1 publicación pendiente" in exc.value.message
    assert media.get_visible(db, asset.id) is not None
    postsvc.update_post(  # the draft can still be saved: its file is still in the library
        db,
        draft.id,
        postsvc.PostInput(brand_id=brand.id, text="Sin redes 2", media_ids=[asset.id], scheduled_at=None),
        "draft",
        clock,
    )


def test_delete_media_used_by_idea(db: Session, brand: Brand, clock) -> None:
    asset = _asset(db, brand, "idea.png")
    idea = postsvc.create_post(
        db,
        postsvc.PostInput(
            brand_id=brand.id, text="Idea", media_ids=[asset.id], scheduled_at=None, kind="idea"
        ),
        "idea",
        clock,
    )
    usage = media.pending_usage(db, asset.id)
    assert usage == media.MediaUsage(ideas=(idea.id,))
    with pytest.raises(MediaInUseError) as exc:
        media.delete_asset(db, asset.id, clock)
    assert exc.value.message == "No se puede borrar: se usa en 1 idea. Quítalo de ella (o bórrala) antes."
    assert media.usage_counts(db, [asset.id]) == {asset.id: 1}


def test_delete_media_used_by_trashed_post_then_restore(
    db: Session, brand: Brand, demo_account: Account, demo_mode, clock
) -> None:
    asset = _asset(db, brand, "papelera.png")
    post = postsvc.create_post(
        db,
        postsvc.PostInput(
            brand_id=brand.id,
            text="Programada",
            media_ids=[asset.id],
            scheduled_at=clock.now() + timedelta(days=2),
            targets=[postsvc.TargetInput(demo_account.id, "post")],
        ),
        "schedule",
        clock,
    )
    postsvc.delete_post(db, post.id, clock)  # to the trash: its target is cancelled
    db.expire_all()
    assert db.get(Post, post.id).targets[0].status == TargetStatus.CANCELLED
    usage = media.pending_usage(db, asset.id)
    assert usage == media.MediaUsage(trashed=(post.id,))
    with pytest.raises(MediaInUseError) as exc:
        media.delete_asset(db, asset.id, clock)
    assert "1 publicación de la papelera" in exc.value.message
    assert "definitivamente" in exc.value.message

    # Restoring brings it back scheduled with its media still in the library.
    postsvc.restore_post(db, post.id, clock)
    db.expire_all()
    restored = db.get(Post, post.id)
    assert restored.targets[0].status == TargetStatus.SCHEDULED
    result = postsvc.validate_post(db, postsvc.post_input_from_post(restored), clock=clock)
    assert not [i for issues in result.values() for i in issues if i.code == "media_missing"]
    assert media.pending_usage(db, asset.id) == media.MediaUsage(posts=(post.id,))

    # Once the trashed post is gone for good, the file can be deleted.
    postsvc.delete_post(db, post.id, clock)
    db.delete(db.get(Post, post.id))
    db.commit()
    assert not media.pending_usage(db, asset.id)
    assert media.delete_asset(db, asset.id, clock).deleted_at is not None


def test_trashed_post_already_published_does_not_block(
    db: Session, brand: Brand, demo_account: Account, clock
) -> None:
    asset = _asset(db, brand, "hecha.png")
    post = Post(brand_id=brand.id, text="Hecha", deleted_at=clock.now())
    post.set_media([asset])
    post.targets.append(
        PostTarget(account_id=demo_account.id, network="demo", format="post", status=TargetStatus.PUBLISHED)
    )
    db.add(post)
    db.commit()
    assert not media.pending_usage(db, asset.id)
    assert media.delete_asset(db, asset.id, clock).deleted_at is not None


def test_mixed_usage_message() -> None:
    usage = media.MediaUsage(posts=(1, 2), ideas=(3,), trashed=(4,))
    assert usage.summary == "2 publicaciones pendientes, 1 idea y 1 publicación de la papelera"
    assert usage.advice == ("Quítalo de ellas (o bórralas); la de la papelera, elimínala definitivamente")
    assert media.MediaUsage(trashed=(1, 2)).advice == (
        "Elimínalas definitivamente de la papelera (o restáuralas y quítalo)"
    )

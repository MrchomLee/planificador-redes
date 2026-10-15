"""Post service tests (composer contract): create with every action, per-target overrides and options,
validation (errors block schedule/publish but not drafts/ideas), update rules (locked posts, removed/added
targets, error targets), duplicate, trash/restore, reschedule and the publish-now hand-off to the engine."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planificador import config, postsvc
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
from planificador.postsvc import GENERAL, PostError, PostInput, TargetInput

# --------------------------------------------------------------------------------------------- helpers


def make_account(db: Session, brand: Brand, network: str, **kw) -> Account:
    account = Account(
        brand_id=brand.id,
        network=network,
        external_id=kw.pop("external_id", f"{network}-1"),
        name=kw.pop("name", f"Cuenta {network}"),
        username=kw.pop("username", network),
        account_type=kw.pop("account_type", None),
        status=kw.pop("status", AccountStatus.CONNECTED),
        meta=kw.pop("meta", {}),
    )
    db.add(account)
    db.commit()
    return account


def make_media(
    db: Session, brand: Brand | None, name: str = "foto.jpg", kind: str = "image", **kw
) -> MediaAsset:
    sha = kw.pop("sha", name.encode().hex().ljust(64, "0")[:64])
    asset = MediaAsset(
        brand_id=brand.id if brand else None,
        kind=MediaKind(kind),
        original_name=name,
        rel_path=f"originals/{sha[:2]}/{sha}.{'jpg' if kind == 'image' else 'mp4'}",
        sha256=sha,
        size_bytes=kw.pop("size_bytes", 200_000),
        mime=kw.pop("mime", "image/jpeg" if kind == "image" else "video/mp4"),
        width=kw.pop("width", 1080),
        height=kw.pop("height", 1350 if kind == "image" else 1920),
        duration_s=kw.pop("duration_s", None if kind == "image" else 12.0),
        fps=kw.pop("fps", None if kind == "image" else 30.0),
        video_codec=kw.pop("video_codec", None if kind == "image" else "h264"),
        audio_codec=kw.pop("audio_codec", None if kind == "image" else "aac"),
        **kw,
    )
    db.add(asset)
    db.commit()
    return asset


def data(brand: Brand, *targets: TargetInput, **kw) -> PostInput:
    return PostInput(
        brand_id=brand.id,
        text=kw.pop("text", "Hola mundo #prueba"),
        media_ids=kw.pop("media_ids", []),
        scheduled_at=kw.pop("scheduled_at", None),
        targets=list(targets),
        **kw,
    )


def demo_target(account: Account, **kw) -> TargetInput:
    return TargetInput(account_id=account.id, format=kw.pop("format", "post"), **kw)


def codes(result: dict, key: int | None = None, level: str | None = None) -> set[str]:
    groups = [result.get(key, [])] if key is not None else list(result.values())
    return {i.code for issues in groups for i in issues if level is None or i.level == level}


@pytest.fixture
def future(clock):
    return clock.now() + timedelta(days=1)


@pytest.fixture
def woken(monkeypatch):
    calls: list[int] = []
    from planificador import scheduler

    monkeypatch.setattr(scheduler, "wake", lambda: calls.append(1))
    return calls


@pytest.fixture
def public_media_on(db):
    config.set_setting(db, "public_media_mode", "base_url")


# ---------------------------------------------------------------------------------------------- create


def test_create_schedule(db, brand, demo_mode, demo_account, clock, future, woken):
    post = postsvc.create_post(
        db, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    db.expire_all()
    post = db.get(Post, post.id)
    assert post.kind == PostKind.SCHEDULED
    assert post.scheduled_at == future.replace(microsecond=0)
    assert post.source == "composer"
    [target] = post.targets
    assert target.status == TargetStatus.SCHEDULED
    assert target.scheduled_at == post.scheduled_at
    assert target.network == "demo"
    assert target.auto_publish is True
    assert [e.event for e in target.events] == ["created"]
    assert "Programada para el" in target.events[0].message
    assert woken  # the scheduler is woken after a schedule


def test_create_schedule_rejects_past_date_and_rolls_back(db, brand, demo_mode, demo_account, clock):
    with pytest.raises(PostError) as exc:
        postsvc.create_post(
            db,
            data(brand, demo_target(demo_account), scheduled_at=clock.now() - timedelta(minutes=1)),
            "schedule",
            clock,
        )
    assert exc.value.status_code == 422
    assert exc.value.code == "validation"
    assert exc.value.message == "La fecha de publicación está en el pasado."
    assert "date_past" in codes(exc.value.issues, GENERAL)
    assert db.scalar(select(func.count()).select_from(Post)) == 0


def test_create_schedule_requires_targets_and_date(db, brand, clock):
    with pytest.raises(PostError) as exc:
        postsvc.create_post(db, data(brand), "schedule", clock)
    assert {"no_targets", "date_required"} <= codes(exc.value.issues, GENERAL)
    assert "2 errores" in exc.value.message


def test_draft_and_idea_may_have_errors(db, brand, demo_mode, clock):
    past = clock.now() - timedelta(days=2)
    draft = postsvc.create_post(db, data(brand, scheduled_at=past, text=""), "draft", clock)
    assert draft.kind == PostKind.SCHEDULED and draft.scheduled_at == past.replace(microsecond=0)
    assert draft.display_status == TargetStatus.DRAFT

    idea = postsvc.create_post(
        db, data(brand, text="Idea", idea_stage="en_progreso", notes="  nota  "), "idea", clock
    )
    assert idea.kind == PostKind.IDEA
    assert idea.idea_stage == "en_progreso"
    assert idea.notes == "nota"

    other = postsvc.create_post(db, data(brand, idea_stage="nope"), "idea", clock)
    assert other.idea_stage == "idea"


def test_draft_targets_are_draft(db, brand, demo_mode, demo_account, clock, woken):
    tiktok = make_account(db, brand, "tiktok")
    post = postsvc.create_post(
        db,
        data(brand, demo_target(demo_account), TargetInput(tiktok.id, "video"), text="Borrador"),
        "draft",
        clock,
    )
    assert {t.status for t in post.targets} == {TargetStatus.DRAFT}
    assert not woken


def test_create_publish_now_hands_over_to_engine(db, brand, demo_mode, demo_account, clock, woken):
    past = clock.now() - timedelta(days=1)  # the date is irrelevant for "Publicar ahora"
    post = postsvc.create_post(
        db, data(brand, demo_target(demo_account), scheduled_at=past), "publish_now", clock
    )
    db.expire_all()
    post = db.get(Post, post.id)
    [target] = post.targets
    assert target.status == TargetStatus.SCHEDULED
    assert target.scheduled_at == clock.now()
    assert post.scheduled_at == clock.now()
    assert "publish_now" in [e.event for e in target.events]


def test_publish_now_calls_engine(db, brand, demo_mode, demo_account, clock, monkeypatch):
    from planificador import engine

    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(engine, "publish_now", lambda db_, post_id, clock_: calls.append((post_id, clock_)))
    post = postsvc.create_post(db, data(brand, demo_target(demo_account)), "publish_now", clock)
    assert calls == [(post.id, clock)]


def test_publish_now_engine_error_becomes_post_error(db, brand, demo_mode, demo_account, clock, monkeypatch):
    from planificador import engine

    def boom(*_a):
        raise engine.EngineError("No hay redes pendientes.", code="nothing_to_publish")

    monkeypatch.setattr(engine, "publish_now", boom)
    with pytest.raises(PostError) as exc:
        postsvc.create_post(db, data(brand, demo_target(demo_account)), "publish_now", clock)
    assert exc.value.code == "nothing_to_publish"
    assert "borrador" in exc.value.message


def test_publish_now_blocked_by_errors(db, brand, demo_mode, demo_account, clock):
    with pytest.raises(PostError) as exc:
        postsvc.create_post(db, data(brand, demo_target(demo_account), text=""), "publish_now", clock)
    assert "demo_empty" in codes(exc.value.issues, demo_account.id)
    assert exc.value.message.startswith("Escribe un texto")


def test_invalid_action(db, brand, clock):
    with pytest.raises(PostError) as exc:
        postsvc.create_post(db, data(brand), "publicar", clock)
    assert exc.value.code == "invalid_action"


def test_overrides_options_and_effective_content(
    db, brand, demo_mode, demo_account, clock, future, public_media_on
):
    ig = make_account(db, brand, "instagram", account_type="business")
    img1 = make_media(db, brand, "a.jpg")
    img2 = make_media(db, None, "b.jpg")  # shared asset
    payload = data(
        brand,
        demo_target(demo_account, text_override="Hola mundo #prueba", media_override=[img1.id, img2.id]),
        TargetInput(
            ig.id,
            "post",
            text_override="Texto solo para Instagram",
            media_override=[img2.id],
            options={
                "collaborators": "@ana, luis\n@ana",
                "demo_fail": "permanent",
                "_secret": 1,
                "alt_text": " x ",
            },
        ),
        media_ids=[img1.id, img2.id],
        scheduled_at=future,
    )
    post = postsvc.create_post(db, payload, "schedule", clock)
    demo_t, ig_t = post.targets
    # Same as the common content → no override stored.
    assert demo_t.text_override is None and demo_t.media_override is None
    assert ig_t.text_override == "Texto solo para Instagram"
    assert ig_t.media_override == [img2.id]
    assert ig_t.options["collaborators"] == ["ana", "luis"]
    assert ig_t.options["alt_text"] == "x"
    assert "demo_fail" not in ig_t.options and "_secret" not in ig_t.options
    assert ig_t.options["first_comment"] == ""  # defaults merged
    assert postsvc.target_text(demo_t) == "Hola mundo #prueba"
    assert postsvc.target_text(ig_t) == "Texto solo para Instagram"
    assert [m.id for m in postsvc.target_media(db, demo_t)] == [img1.id, img2.id]
    assert [m.id for m in postsvc.target_media(db, ig_t)] == [img2.id]
    assert [m.id for m in post.media] == [img1.id, img2.id]


def test_demo_options_kept_for_demo(db, brand, demo_mode, demo_account, clock, future):
    post = postsvc.create_post(
        db,
        data(
            brand,
            demo_target(demo_account, options={"demo_fail": "transient", "demo_steps": 3}),
            scheduled_at=future,
        ),
        "schedule",
        clock,
    )
    assert post.targets[0].options == {"demo_fail": "transient", "demo_steps": 3}


def test_crlf_text_normalised(db, brand, clock):
    post = postsvc.create_post(db, data(brand, text="a\r\nb\rc"), "draft", clock)
    assert post.text == "a\nb\nc"


# --------------------------------------------------------------------------------------- validation


def test_validate_tiktok_privacy_blocks_schedule_not_draft(db, brand, clock, future):
    tiktok = make_account(
        db,
        brand,
        "tiktok",
        meta={
            "creator_info": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "nickname": "Ana"}
        },
    )
    video = make_media(db, brand, "v.mp4", "video")
    target = TargetInput(tiktok.id, "video", options={"consent": True})
    payload = data(brand, target, media_ids=[video.id], scheduled_at=future)
    result = postsvc.validate_post(db, payload, clock=clock)
    assert "tiktok_privacy_required" in codes(result, tiktok.id, "error")
    assert GENERAL not in result
    with pytest.raises(PostError):
        postsvc.create_post(db, payload, "schedule", clock)
    draft = postsvc.create_post(db, payload, "draft", clock)
    assert draft.targets[0].options["privacy_level"] is None

    target.options = {"consent": True, "privacy_level": "PUBLIC_TO_EVERYONE"}
    ok = postsvc.create_post(db, payload, "schedule", clock)
    assert ok.targets[0].status == TargetStatus.SCHEDULED


def test_validate_tiktok_consent_and_commercial(db, brand, clock, future):
    tiktok = make_account(db, brand, "tiktok")
    video = make_media(db, brand, "v.mp4", "video")
    opts = {"privacy_level": "PUBLIC_TO_EVERYONE", "consent": False, "commercial_content": True}
    result = postsvc.validate_post(
        db,
        data(brand, TargetInput(tiktok.id, "video", options=opts), media_ids=[video.id], scheduled_at=future),
    )
    assert {"tiktok_consent", "tiktok_commercial_option"} <= codes(result, tiktok.id, "error")
    opts.update(consent=True, brand_organic_toggle=True)
    result = postsvc.validate_post(
        db,
        data(brand, TargetInput(tiktok.id, "video", options=opts), media_ids=[video.id], scheduled_at=future),
    )
    assert not codes(result, tiktok.id, "error")
    # Inbox mode skips the direct-post checks.
    inbox = {"mode": "inbox", "commercial_content": True}
    result = postsvc.validate_post(
        db,
        data(
            brand, TargetInput(tiktok.id, "video", options=inbox), media_ids=[video.id], scheduled_at=future
        ),
    )
    assert not codes(result, tiktok.id, "error")


def test_validate_account_states(db, brand, clock, future, demo_mode_off):
    fb = make_account(db, brand, "facebook", status=AccountStatus.DISCONNECTED)
    ig = make_account(db, brand, "instagram", status=AccountStatus.NEEDS_RECONNECT, account_type="business")
    demo = make_account(db, brand, "demo")
    payload = data(
        brand,
        TargetInput(fb.id, "post"),
        TargetInput(ig.id, "post"),
        TargetInput(demo.id, "post"),
        scheduled_at=future,
    )
    result = postsvc.validate_post(db, payload, clock=clock)
    assert "account_disconnected" in codes(result, fb.id, "error")
    assert "account_needs_reconnect" in codes(result, ig.id, "warning")
    assert "demo_mode_off" in codes(result, demo.id, "error")
    now_result = postsvc.validate_post(db, payload, clock=clock, action="publish_now")
    assert "account_needs_reconnect" in codes(now_result, ig.id, "error")


def test_validate_youtube_lock_and_defaults(db, brand, clock, future):
    yt = make_account(db, brand, "youtube")
    video = make_media(db, brand, "v.mp4", "video", width=1920, height=1080, duration_s=200.0)
    opts = {"title": "Vídeo", "made_for_kids": False}
    auto = data(
        brand,
        TargetInput(yt.id, "video", options=opts, auto_publish=True),
        media_ids=[video.id],
        scheduled_at=future,
    )
    assert "youtube_api_locked" in codes(postsvc.validate_post(db, auto, clock=clock), yt.id, "warning")
    assisted = data(
        brand,
        TargetInput(yt.id, "video", options=opts, auto_publish=False),
        media_ids=[video.id],
        scheduled_at=future,
    )
    assert not codes(postsvc.validate_post(db, assisted, clock=clock), yt.id)
    assert postsvc.default_auto_publish(yt) is False
    yt.meta = {"youtube_lock_status": "unlocked"}
    db.commit()
    assert postsvc.default_auto_publish(yt) is True
    assert postsvc.youtube_unlocked(yt) is True
    assert "youtube_api_locked" not in codes(postsvc.validate_post(db, auto, clock=clock))
    fb = make_account(db, brand, "facebook")
    assert postsvc.default_auto_publish(fb) is True


def test_validate_missing_things(db, brand, clock, future):
    other = Brand(name="Otra", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    foreign = make_account(db, other, "facebook")
    foreign_media = make_media(db, other, "ajena.jpg")
    payload = data(
        brand, TargetInput(foreign.id, "post"), media_ids=[foreign_media.id, 999], scheduled_at=future
    )
    result = postsvc.validate_post(db, payload, clock=clock)
    assert "account_missing" in codes(result, foreign.id)
    assert "media_missing" in codes(result, GENERAL)
    fb = make_account(db, brand, "facebook")
    bad_format = postsvc.validate_post(db, data(brand, TargetInput(fb.id, "reelz"), scheduled_at=future))
    assert "format_unknown" in codes(bad_format, fb.id)
    idea = postsvc.validate_post(db, data(brand, kind="idea"))
    assert GENERAL not in idea


def test_create_refuses_bad_references(db, brand, demo_mode, demo_account, clock):
    other = Brand(name="Otra", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    foreign = make_account(db, other, "facebook")
    with pytest.raises(PostError) as exc:
        postsvc.create_post(db, data(brand, TargetInput(foreign.id, "post")), "draft", clock)
    assert exc.value.code == "account_missing"
    with pytest.raises(PostError) as exc:
        postsvc.create_post(
            db, data(brand, demo_target(demo_account), demo_target(demo_account)), "draft", clock
        )
    assert exc.value.code == "account_duplicate"
    with pytest.raises(PostError) as exc:
        postsvc.create_post(db, data(brand, demo_target(demo_account, format="reel")), "draft", clock)
    assert exc.value.code == "format_invalid"
    with pytest.raises(PostError) as exc:
        postsvc.create_post(db, data(brand, media_ids=[12345]), "draft", clock)
    assert exc.value.code == "media_missing"
    deleted = make_media(db, brand, "borrada.jpg", deleted_at=clock.now())
    with pytest.raises(PostError):
        postsvc.create_post(db, data(brand, media_ids=[deleted.id]), "draft", clock)
    with pytest.raises(PostError) as exc:
        postsvc.create_post(
            db, PostInput(brand_id=9999, text="", media_ids=[], scheduled_at=None), "draft", clock
        )
    assert exc.value.status_code == 404
    assert db.scalar(select(func.count()).select_from(Post)) == 0


def test_has_blocking_errors_rules():
    from planificador.providers.base import Issue

    result = {GENERAL: [Issue("error", "date_past", "pasado")], 3: [Issue("warning", "w", "aviso")]}
    assert postsvc.has_blocking_errors(result, "schedule")
    assert not postsvc.has_blocking_errors(result, "publish_now")
    assert not postsvc.has_blocking_errors(result, "draft")
    assert not postsvc.has_blocking_errors(result, "idea")


# ---------------------------------------------------------------------------------------------- update


def test_update_replaces_content_and_targets(db, brand, demo_mode, demo_account, clock, future, woken):
    fb = make_account(db, brand, "facebook")
    post = postsvc.create_post(
        db,
        data(brand, demo_target(demo_account), TargetInput(fb.id, "post"), scheduled_at=future),
        "schedule",
        clock,
    )
    demo_t = post.targets[0]
    ig = make_account(db, brand, "instagram", account_type="business")
    later = future + timedelta(hours=3)
    updated = postsvc.update_post(
        db,
        post.id,
        data(
            brand,
            demo_target(demo_account),
            TargetInput(ig.id, "post"),
            text="Nuevo texto",
            scheduled_at=later,
        ),
        "draft",
        clock,
    )
    db.expire_all()
    updated = db.get(Post, post.id)
    assert updated.text == "Nuevo texto"
    assert updated.scheduled_at == later.replace(microsecond=0)
    assert {str(t.network) for t in updated.targets} == {"demo", "instagram"}
    kept = next(t for t in updated.targets if t.network == "demo")
    assert kept.id == demo_t.id  # updated in place
    assert kept.status == TargetStatus.DRAFT and kept.scheduled_at == updated.scheduled_at
    assert [e.event for e in kept.events] == ["created", "updated"]
    assert db.scalar(select(func.count()).select_from(PostTarget).where(PostTarget.account_id == fb.id)) == 0


def test_update_media_order(db, brand, clock):
    a, b = make_media(db, brand, "a.jpg"), make_media(db, brand, "b.jpg")
    post = postsvc.create_post(db, data(brand, media_ids=[a.id, b.id]), "draft", clock)
    postsvc.update_post(db, post.id, data(brand, media_ids=[b.id, a.id]), "draft", clock)
    db.expire_all()
    assert [m.id for m in db.get(Post, post.id).media] == [b.id, a.id]
    postsvc.update_post(db, post.id, data(brand, media_ids=[a.id]), "draft", clock)
    db.expire_all()
    assert [m.id for m in db.get(Post, post.id).media] == [a.id]


def test_update_to_idea_and_back(db, brand, demo_mode, demo_account, clock, future):
    post = postsvc.create_post(db, data(brand, demo_target(demo_account)), "idea", clock)
    assert post.kind == PostKind.IDEA
    post = postsvc.update_post(
        db, post.id, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    assert post.kind == PostKind.SCHEDULED and post.idea_stage is None
    assert post.targets[0].status == TargetStatus.SCHEDULED


def test_update_error_target_reschedules_and_keeps_reconcile(
    db, brand, demo_mode, demo_account, clock, future
):
    post = postsvc.create_post(
        db, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    target = post.targets[0]
    target.status = TargetStatus.ERROR
    target.last_error = "Fallo"
    target.attempts = 4
    target.commit_phase = CommitPhase.COMMIT_SENT
    target.provider_state = {"publish_id": "x"}
    db.commit()
    postsvc.update_post(
        db, post.id, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    db.expire_all()
    target = db.get(PostTarget, target.id)
    assert target.status == TargetStatus.SCHEDULED
    assert target.attempts == 0 and target.last_error is None
    assert target.provider_state["publish_id"] == "x"
    assert target.provider_state["_engine"]["pending"] == "reconcile"


def test_update_clears_partial_state_when_nothing_was_sent(db, brand, demo_mode, demo_account, clock, future):
    post = postsvc.create_post(
        db, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    target = post.targets[0]
    target.status = TargetStatus.MISSED
    target.provider_state = {"container_id": "c1"}
    target.commit_phase = CommitPhase.PREPARED
    db.commit()
    postsvc.update_post(
        db, post.id, data(brand, demo_target(demo_account), scheduled_at=future), "draft", clock
    )
    db.expire_all()
    target = db.get(PostTarget, target.id)
    assert target.status == TargetStatus.DRAFT
    assert target.provider_state == {} and target.commit_phase == CommitPhase.NONE


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (TargetStatus.PUBLISHED, "ya se ha publicado"),
        (TargetStatus.MANUAL_DONE, "ya se ha publicado"),
        (TargetStatus.PUBLISHING, "enviando"),
        (TargetStatus.SENT_TO_APP, "app de TikTok"),
        (TargetStatus.UNKNOWN, "confirmar"),
    ],
)
def test_update_locked_posts(db, brand, demo_mode, demo_account, clock, future, status, fragment):
    post = postsvc.create_post(
        db, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    post.targets[0].status = status
    db.commit()
    assert not postsvc.is_editable(post)
    assert fragment in postsvc.edit_lock_reason(post)
    with pytest.raises(PostError) as exc:
        postsvc.update_post(db, post.id, data(brand, demo_target(demo_account), text="x"), "draft", clock)
    assert exc.value.code == "locked" and exc.value.status_code == 409
    db.expire_all()
    assert db.get(Post, post.id).text == "Hola mundo #prueba"


def test_update_refusals(db, brand, demo_mode, demo_account, clock, future):
    post = postsvc.create_post(db, data(brand, demo_target(demo_account)), "draft", clock)
    with pytest.raises(PostError) as exc:
        postsvc.update_post(db, 4242, data(brand), "draft", clock)
    assert exc.value.status_code == 404
    other = Brand(name="Otra", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    with pytest.raises(PostError) as exc:
        postsvc.update_post(db, post.id, data(other), "draft", clock)
    assert exc.value.code == "brand_mismatch"
    with pytest.raises(PostError) as exc:
        postsvc.update_post(
            db, post.id, data(brand, demo_target(demo_account), scheduled_at=None), "schedule", clock
        )
    assert exc.value.code == "validation"
    postsvc.delete_post(db, post.id, clock)
    with pytest.raises(PostError) as exc:
        postsvc.update_post(db, post.id, data(brand), "draft", clock)
    assert exc.value.code == "deleted"
    assert "papelera" in postsvc.edit_lock_reason(db.get(Post, post.id))


def test_update_publish_now(db, brand, demo_mode, demo_account, clock):
    post = postsvc.create_post(db, data(brand, demo_target(demo_account)), "draft", clock)
    postsvc.update_post(db, post.id, data(brand, demo_target(demo_account), text="Ya"), "publish_now", clock)
    db.expire_all()
    target = db.get(Post, post.id).targets[0]
    assert target.status == TargetStatus.SCHEDULED and target.scheduled_at == clock.now()


def test_commit_false_only_flushes(db, brand, clock):
    post = postsvc.create_post(db, data(brand, text="tmp"), "draft", clock, commit=False)
    assert post.id is not None
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Post)) == 0


# ------------------------------------------------------------------------------------------- duplicate


def test_duplicate_post(db, brand, clock, future):
    tiktok = make_account(db, brand, "tiktok")
    fb = make_account(db, brand, "facebook")
    video = make_media(db, brand, "v.mp4", "video")
    src = postsvc.create_post(
        db,
        data(
            brand,
            TargetInput(
                tiktok.id, "video", text_override="Solo TikTok", options={"consent": True, "is_aigc": True}
            ),
            TargetInput(fb.id, "reel"),
            media_ids=[video.id],
            scheduled_at=future,
            notes="nota",
        ),
        "draft",
        clock,
    )
    src.targets[1].status = TargetStatus.CANCELLED
    db.commit()
    copy_post = postsvc.duplicate_post(db, src.id, clock)
    assert copy_post.id != src.id
    assert copy_post.source == "duplicate"
    assert copy_post.text == src.text and copy_post.notes == "nota"
    assert copy_post.scheduled_at == src.scheduled_at
    assert [m.id for m in copy_post.media] == [video.id]
    [t] = copy_post.targets  # cancelled target not copied
    assert t.network == "tiktok" and t.status == TargetStatus.DRAFT
    assert t.text_override == "Solo TikTok"
    assert t.options["is_aigc"] is True and "consent" not in t.options
    assert [e.event for e in t.events] == ["duplicated"]

    clock.advance(days=2)  # the date has passed: the copy has no date
    again = postsvc.duplicate_post(db, src.id, clock)
    assert again.scheduled_at is None


def test_duplicate_missing(db, clock):
    with pytest.raises(PostError) as exc:
        postsvc.duplicate_post(db, 777, clock)
    assert exc.value.status_code == 404


# ------------------------------------------------------------------------------------- trash / restore


def _post_with_statuses(db, brand, clock, future, *statuses):
    accounts = [make_account(db, brand, n) for n in ("facebook", "instagram", "tiktok", "youtube", "demo")]
    post = postsvc.create_post(
        db,
        data(
            brand,
            *[TargetInput(a.id, rulesfmt(a.network)) for a in accounts[: len(statuses)]],
            scheduled_at=future,
        ),
        "draft",
        clock,
    )
    for target, status in zip(post.targets, statuses, strict=True):
        target.status = status
    db.commit()
    return post


def rulesfmt(network: str) -> str:
    return {"tiktok": "video", "youtube": "video"}.get(str(network), "post")


def test_delete_cancels_pending_and_keeps_history(db, brand, clock, future):
    post = _post_with_statuses(
        db,
        brand,
        clock,
        future,
        TargetStatus.SCHEDULED,
        TargetStatus.PUBLISHED,
        TargetStatus.ERROR,
        TargetStatus.MANUAL_PENDING,
    )
    note = Notification(kind="manual_publish", title="Publica", target_id=post.targets[3].id)
    db.add(note)
    db.commit()
    postsvc.delete_post(db, post.id, clock)
    db.expire_all()
    post = db.get(Post, post.id)
    assert post.deleted_at == clock.now()
    statuses = [t.status for t in post.targets]
    assert statuses == [
        TargetStatus.CANCELLED,
        TargetStatus.PUBLISHED,
        TargetStatus.CANCELLED,
        TargetStatus.CANCELLED,
    ]
    assert post.targets[0].provider_state[postsvc.TRASH_STATE_KEY] == "scheduled"
    assert db.get(Notification, note.id).read_at == clock.now()
    assert "trashed" in [e.event for e in post.targets[0].events]
    postsvc.delete_post(db, post.id, clock)  # idempotent


def test_delete_refuses_busy_post(db, brand, clock, future):
    post = _post_with_statuses(db, brand, clock, future, TargetStatus.PUBLISHING)
    with pytest.raises(PostError) as exc:
        postsvc.delete_post(db, post.id, clock)
    assert exc.value.code == "busy"
    # Waiting before a retry (nothing sent) is not busy: it is cancelled.
    target = post.targets[0]
    target.next_attempt_at = clock.now() + timedelta(minutes=5)
    db.commit()
    postsvc.delete_post(db, post.id, clock)
    db.expire_all()
    assert db.get(PostTarget, target.id).status == TargetStatus.CANCELLED


def test_delete_withdraws_native_schedule(db, brand, clock, future, monkeypatch):
    from planificador import engine

    post = _post_with_statuses(db, brand, clock, future, TargetStatus.PUBLISHED)
    target = post.targets[0]
    target.provider_state = {"native_scheduled": True}
    target.scheduled_at = future
    db.commit()
    calls: list[int] = []
    monkeypatch.setattr(engine, "cancel_target", lambda db_, tid, clock_: calls.append(tid))
    postsvc.delete_post(db, post.id, clock)
    assert calls == [target.id]


def test_restore_post(db, brand, clock, future, woken):
    post = _post_with_statuses(
        db,
        brand,
        clock,
        future,
        TargetStatus.SCHEDULED,
        TargetStatus.DRAFT,
        TargetStatus.ERROR,
        TargetStatus.PUBLISHED,
    )
    for t in post.targets:
        t.scheduled_at = future
    db.commit()
    postsvc.delete_post(db, post.id, clock)
    postsvc.restore_post(db, post.id, clock)
    db.expire_all()
    post = db.get(Post, post.id)
    assert post.deleted_at is None
    assert [t.status for t in post.targets] == [
        TargetStatus.SCHEDULED,
        TargetStatus.DRAFT,
        TargetStatus.ERROR,
        TargetStatus.PUBLISHED,
    ]
    assert all(postsvc.TRASH_STATE_KEY not in (t.provider_state or {}) for t in post.targets)
    assert "restored" in [e.event for e in post.targets[0].events]
    postsvc.restore_post(db, post.id, clock)  # not in the trash: no-op


def test_restore_past_scheduled_becomes_draft(db, brand, clock, future):
    post = _post_with_statuses(db, brand, clock, future, TargetStatus.SCHEDULED)
    post.targets[0].scheduled_at = future
    db.commit()
    postsvc.delete_post(db, post.id, clock)
    clock.advance(days=3)
    postsvc.restore_post(db, post.id, clock)
    db.expire_all()
    assert db.get(Post, post.id).targets[0].status == TargetStatus.DRAFT


# ------------------------------------------------------------------------------------------ reschedule


def test_reschedule_post(db, brand, demo_mode, demo_account, clock, future, woken):
    fb = make_account(db, brand, "facebook")
    post = postsvc.create_post(
        db,
        data(brand, demo_target(demo_account), TargetInput(fb.id, "post"), scheduled_at=future),
        "schedule",
        clock,
    )
    post.targets[1].status = TargetStatus.DRAFT
    db.commit()
    new = future + timedelta(days=2, hours=1)
    moved = postsvc.reschedule_post(db, post.id, new, clock)
    db.expire_all()
    moved = db.get(Post, moved.id)
    assert moved.scheduled_at == new.replace(microsecond=0)
    assert [t.status for t in moved.targets] == [TargetStatus.SCHEDULED, TargetStatus.DRAFT]
    assert all(t.scheduled_at == moved.scheduled_at for t in moved.targets)
    assert "rescheduled" in [e.event for e in moved.targets[0].events]
    assert woken


def test_reschedule_refusals(db, brand, demo_mode, demo_account, clock, future):
    post = postsvc.create_post(
        db, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock
    )
    with pytest.raises(PostError) as exc:
        postsvc.reschedule_post(db, post.id, clock.now() - timedelta(minutes=5), clock)
    assert exc.value.code == "date_past" and exc.value.status_code == 422
    post.targets[0].status = TargetStatus.ERROR
    db.commit()
    with pytest.raises(PostError) as exc:
        postsvc.reschedule_post(db, post.id, future, clock)
    assert exc.value.code == "not_movable"
    post.targets[0].status = TargetStatus.CANCELLED
    db.commit()
    with pytest.raises(PostError) as exc:
        postsvc.reschedule_post(db, post.id, future, clock)
    assert exc.value.code == "cancelled"
    idea = postsvc.create_post(db, data(brand), "idea", clock)
    with pytest.raises(PostError) as exc:
        postsvc.reschedule_post(db, idea.id, future, clock)
    assert exc.value.code == "idea"
    db.expire_all()
    assert db.get(Post, post.id).scheduled_at == future.replace(microsecond=0)


# --------------------------------------------------------------------------------------------- misc


def test_post_input_roundtrip(db, brand, demo_mode, demo_account, clock, future):
    img = make_media(db, brand, "a.jpg")
    post = postsvc.create_post(
        db,
        data(
            brand,
            demo_target(demo_account, text_override="otro", options={"demo_steps": 2}),
            media_ids=[img.id],
            scheduled_at=future,
            notes="n",
        ),
        "schedule",
        clock,
    )
    form = postsvc.post_input_from_post(post)
    assert form.text == post.text and form.media_ids == [img.id] and form.notes == "n"
    assert form.targets[0].text_override == "otro"
    assert form.targets[0].options == {"demo_steps": 2}
    assert form.scheduled_at == post.scheduled_at and form.kind == "scheduled"


def test_ensure_demo_account(db, brand):
    first = postsvc.ensure_demo_account(db, brand.id)
    assert first.network == "demo" and first.status == AccountStatus.CONNECTED
    first.status = AccountStatus.DISCONNECTED
    db.commit()
    again = postsvc.ensure_demo_account(db, brand.id)
    assert again.id == first.id and again.status == AccountStatus.CONNECTED


def test_events_have_messages(db, brand, demo_mode, demo_account, clock, future):
    postsvc.create_post(db, data(brand, demo_target(demo_account), scheduled_at=future), "schedule", clock)
    event = db.scalars(select(PostEvent)).one()
    assert event.ts == clock.now() and event.level == "info"

"""Rules (SPEC §7): formats, options, char limits and validation edge cases per network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from planificador import rules
from planificador.providers.base import Issue, MediaInfo, TargetSpec

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

_next_id = iter(range(1, 10_000))


def image(
    w: int = 1080, h: int = 1350, *, mime: str = "image/jpeg", size: int = 400_000, name: str = "foto.jpg"
) -> MediaInfo:
    return MediaInfo(
        id=next(_next_id),
        kind="image",
        path=Path("/tmp") / name,
        mime=mime,
        size_bytes=size,
        width=w,
        height=h,
        duration_s=None,
        fps=None,
        video_codec=None,
        audio_codec=None,
        bitrate=None,
        is_hdr=False,
        original_name=name,
    )


def video(
    duration: float | None = 30.0,
    w: int = 1080,
    h: int = 1920,
    *,
    mime: str = "video/mp4",
    codec: str = "h264",
    fps: float = 30.0,
    size: int = 20_000_000,
    bitrate: int = 5_000_000,
    hdr: bool = False,
    name: str = "clip.mp4",
) -> MediaInfo:
    return MediaInfo(
        id=next(_next_id),
        kind="video",
        path=Path("/tmp") / name,
        mime=mime,
        size_bytes=size,
        width=w,
        height=h,
        duration_s=duration,
        fps=fps,
        video_codec=codec,
        audio_codec="aac",
        bitrate=bitrate,
        is_hdr=hdr,
        original_name=name,
    )


def target(
    network: str,
    fmt: str,
    *,
    text: str = "Hola mundo",
    media: list[MediaInfo] | None = None,
    options: dict[str, Any] | None = None,
    defaults: bool = True,
    scheduled_at: datetime | None = None,
) -> TargetSpec:
    opts = rules.default_options(network, fmt) if defaults else {}
    opts.update(options or {})
    return TargetSpec(network, fmt, text, list(media or []), opts, scheduled_at, True)


def codes(issues: list[Issue], level: str | None = None) -> set[str]:
    return {i.code for i in issues if level is None or i.level == level}


def errors(issues: list[Issue]) -> set[str]:
    return codes(issues, "error")


def warnings(issues: list[Issue]) -> set[str]:
    return codes(issues, "warning")


CTX = rules.RuleContext(public_media_mode="tunnel", now=NOW)


def check(t: TargetSpec, ctx: rules.RuleContext | None = None) -> list[Issue]:
    return rules.validate_target(t, ctx or CTX)


# ------------------------------------------------------------------------------------------ formats


def test_formats_match_providers() -> None:
    from planificador.providers import registry

    for provider in registry.all_providers():
        mine = [(f.key, f.label, f.media) for f in rules.FORMATS[provider.network]]
        theirs = [(f.key, f.label, f.media) for f in provider.formats]
        assert mine == theirs, provider.network
    assert set(rules.FORMATS) == set(rules.NETWORKS)
    for network, specs in rules.FORMATS.items():
        for spec in specs:
            assert (network, spec.key) in rules.FORMAT_RULES
            assert rules.FORMAT_RULES[(network, spec.key)].media == spec.media


def test_disabled_formats_listed_but_rejected() -> None:
    assert [f.key for f in rules.enabled_formats("tiktok")] == ["video"]
    assert "photo" in [f.key for f in rules.FORMATS["tiktok"]]
    issues = check(target("tiktok", "photo", media=[image()], options={"privacy_level": "SELF_ONLY"}))
    assert errors(issues) == {"tiktok_photo_unsupported"}
    assert "dominio público verificado" in issues[0].message


def test_unknown_network_and_format() -> None:
    assert errors(check(TargetSpec("x", "post", "", [], {}, None, True))) == {"network_unknown"}
    issues = check(TargetSpec("facebook", "carousel", "hola", [], {}, None, True))
    assert errors(issues) == {"format_unknown"}
    assert "Publicación, Reel, Historia" in issues[0].message


def test_default_options_contract() -> None:
    tt = rules.default_options("tiktok", "video")
    assert tt["privacy_level"] is None and tt["mode"] == "direct"
    assert tt["disable_comment"] is True and tt["disable_duet"] is True and tt["disable_stitch"] is True
    yt = rules.default_options("youtube", "video")
    assert yt["made_for_kids"] is None and yt["category_id"] == "22" and yt["notify_subscribers"] is True
    assert "thumbnail_media_id" not in rules.default_options("youtube", "short")
    assert rules.default_options("instagram", "reel")["share_to_feed"] is True
    assert rules.default_options("facebook", "post")["native_schedule"] is False
    assert rules.default_options("nope", "x") == {}
    # Fresh copies: mutating one result does not leak into the next.
    first = rules.default_options("instagram", "post")
    first["collaborators"].append("x")
    assert rules.default_options("instagram", "post")["collaborators"] == []


# ------------------------------------------------------------------------------------- char limits


def test_char_limits_and_units() -> None:
    assert rules.char_limit("facebook", "post") == 63_206
    assert rules.char_limit("instagram", "reel") == 2_200
    assert rules.char_limit("tiktok", "video") == 2_200
    assert rules.char_limit("youtube", "short") == 5_000
    assert rules.char_limit("facebook", "nope") == 63_206  # unknown format → network default
    with pytest.raises(ValueError):
        rules.char_limit("myspace", "post")
    assert rules.char_unit("tiktok") == "utf16"
    assert rules.char_unit("youtube") == "bytes"
    assert rules.char_unit("instagram") == "chars"
    assert rules.text_length("tiktok", "😀a") == 3  # emoji = 2 UTF-16 units
    assert rules.text_length("youtube", "ñ") == 2  # 2 UTF-8 bytes
    assert rules.text_length("instagram", "😀a") == 2


def test_tiktok_counts_utf16_units() -> None:
    clip = [video(30)]
    ok = target("tiktok", "video", text="😀" * 1100, media=clip, options={"privacy_level": "SELF_ONLY"})
    assert "text_too_long" not in errors(check(ok))
    over = target("tiktok", "video", text="😀" * 1101, media=clip, options={"privacy_level": "SELF_ONLY"})
    issues = check(over)
    assert "text_too_long" in errors(issues)
    assert "2202" in next(i.message for i in issues if i.code == "text_too_long")


def test_youtube_description_counts_bytes() -> None:
    opts = {"title": "Vídeo", "made_for_kids": False}
    ok = target("youtube", "video", text="ñ" * 2500, media=[video(30, 1920, 1080)], options=opts)
    assert "text_too_long" not in errors(check(ok))
    over = target("youtube", "video", text="ñ" * 2501, media=[video(30, 1920, 1080)], options=opts)
    issues = check(over)
    assert "text_too_long" in errors(issues)
    assert "5002 bytes" in next(i.message for i in issues if i.code == "text_too_long")


def test_instagram_caption_hashtags_and_mentions() -> None:
    pic = [image()]
    assert "text_too_long" in errors(check(target("instagram", "post", text="a" * 2201, media=pic)))
    assert not errors(check(target("instagram", "post", text="a" * 2200, media=pic)))
    tags = " ".join(f"#tag{i}" for i in range(31))
    assert "ig_too_many_hashtags" in errors(check(target("instagram", "post", text=tags, media=pic)))
    tags30 = " ".join(f"#tag{i}" for i in range(30))
    assert not errors(check(target("instagram", "post", text=tags30, media=pic)))
    mentions = " ".join(f"@user{i}" for i in range(21))
    assert "ig_too_many_mentions" in errors(check(target("instagram", "post", text=mentions, media=pic)))
    assert rules.count_hashtags("hola #uno #dos y C# no, x#y no") == 2
    assert rules.count_mentions("@ana y @bob.cat; correo a@b.com") == 2


# ------------------------------------------------------------------------------------------ facebook


def test_facebook_post_shapes() -> None:
    assert errors(check(target("facebook", "post", text=""))) == {"post_empty"}
    assert not errors(check(target("facebook", "post", text="Solo texto")))
    assert not errors(check(target("facebook", "post", media=[image() for _ in range(10)])))
    assert "media_too_many" in errors(check(target("facebook", "post", media=[image() for _ in range(11)])))
    assert "media_no_mix" in errors(check(target("facebook", "post", media=[image(), video()])))
    assert "media_one_video" in errors(check(target("facebook", "post", media=[video(), video()])))
    # Text missing with media → warning only.
    assert warnings(check(target("facebook", "post", text="", media=[image()]))) == {"text_missing"}


def test_facebook_link_option() -> None:
    assert not errors(check(target("facebook", "post", options={"link": "https://ejemplo.com/a"})))
    # A link alone is a valid post (no text).
    assert not errors(check(target("facebook", "post", text="", options={"link": "https://ejemplo.com"})))
    assert "fb_link_invalid" in errors(check(target("facebook", "post", options={"link": "ejemplo.com"})))
    issues = check(target("facebook", "post", media=[image()], options={"link": "https://ejemplo.com"}))
    assert "fb_link_with_media" in errors(issues)
    assert "fb_link_ignored" in warnings(
        check(target("facebook", "reel", media=[video(10)], options={"link": "https://ejemplo.com"}))
    )
    assert "fb_title_too_long" in errors(
        check(target("facebook", "reel", media=[video(10)], options={"title": "x" * 256}))
    )


def test_facebook_native_schedule_hints_are_warnings() -> None:
    opts = {"native_schedule": True}
    soon = target("facebook", "post", options=opts, scheduled_at=NOW + timedelta(minutes=10))
    assert warnings(check(soon)) == {"fb_native_too_soon"} and not errors(check(soon))
    ok = target("facebook", "post", options=opts, scheduled_at=NOW + timedelta(days=2))
    assert not check(ok)
    far = target("facebook", "post", options=opts, scheduled_at=NOW + timedelta(days=40))
    assert warnings(check(far)) == {"fb_native_too_far"}
    assert "fb_native_no_date" in warnings(check(target("facebook", "post", options=opts)))
    reel = target("facebook", "reel", media=[video(10)], options=opts, scheduled_at=NOW + timedelta(days=1))
    assert "fb_native_format" in warnings(check(reel))
    # "0"/"false" strings from forms are off.
    assert not check(target("facebook", "post", options={"native_schedule": "0"}))


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(2.0, "video_too_short"), (3.0, None), (90.0, None), (90.04, None), (91.0, "video_too_long")],
)
def test_facebook_reel_duration(duration: float, expected: str | None) -> None:
    issues = check(target("facebook", "reel", media=[video(duration)]))
    if expected:
        assert expected in errors(issues)
    else:
        assert not errors(issues)


def test_facebook_reel_media_quality_warnings() -> None:
    assert "media_required" in errors(check(target("facebook", "reel")))
    assert "media_video_only" in errors(check(target("facebook", "reel", media=[image()])))
    issues = check(target("facebook", "reel", media=[video(30, 1920, 1080, fps=15.0)]))
    assert {"video_not_vertical", "video_fps"} <= warnings(issues)
    assert not errors(issues)
    assert "video_resolution_low" in warnings(check(target("facebook", "reel", media=[video(30, 480, 854)])))


def test_facebook_story_rules() -> None:
    assert not errors(check(target("facebook", "story", text="", media=[image(1080, 1920)])))
    assert "video_too_long" in errors(check(target("facebook", "story", media=[video(61)])))
    assert "media_one_item" in errors(check(target("facebook", "story", media=[image(), image()])))
    assert "text_ignored" in warnings(
        check(target("facebook", "story", text="Hola", media=[image(1080, 1920)]))
    )
    assert "image_not_vertical" in warnings(
        check(target("facebook", "story", text="", media=[image(1600, 900)]))
    )


def test_facebook_large_video_warnings() -> None:
    big = video(20 * 60, 1920, 1080, size=1_500_000_000)
    issues = check(target("facebook", "post", media=[big]))
    assert "video_large" in warnings(issues) and not errors(issues)
    long = video(50 * 60, 1920, 1080)
    assert "video_long" in warnings(check(target("facebook", "post", media=[long])))


# ----------------------------------------------------------------------------------------- instagram


def test_instagram_feed_image_aspect() -> None:
    for w, h in ((1080, 1350), (1080, 1080), (1910, 1000)):
        assert not check(target("instagram", "post", media=[image(w, h)])), (w, h)
    for w, h in ((1080, 1920), (2000, 1000)):
        issues = check(target("instagram", "post", media=[image(w, h)]))
        assert warnings(issues) == {"image_aspect"} and not errors(issues)
    assert "image_small" in warnings(check(target("instagram", "post", media=[image(300, 300)])))
    assert "image_gif" in warnings(check(target("instagram", "post", media=[image(mime="image/gif")])))


def test_instagram_carousel() -> None:
    assert not errors(check(target("instagram", "post", media=[image(), video(30), image()])))
    issues = check(target("instagram", "post", media=[image() for _ in range(11)]))
    assert "media_too_many" in errors(issues)
    long_item = check(target("instagram", "post", media=[image(), video(70, size=150_000_000)]))
    assert {"video_too_long", "video_too_large"} <= warnings(long_item)
    assert not errors(long_item)


def test_instagram_single_video_is_a_reel() -> None:
    assert not errors(check(target("instagram", "post", media=[video(14 * 60)])))
    assert "video_too_long" in errors(check(target("instagram", "post", media=[video(16 * 60)])))
    assert "video_too_short" in errors(check(target("instagram", "post", media=[video(2)])))
    webm = video(10, mime="video/webm", name="c.webm")
    issues = check(target("instagram", "reel", media=[webm]))
    assert "video_container" in errors(issues)
    assert "WEBM" in next(i.message for i in issues if i.code == "video_container")


def test_instagram_reel_limits() -> None:
    assert "video_too_large" in errors(
        check(target("instagram", "reel", media=[video(60, size=301_000_000)]))
    )
    issues = check(target("instagram", "reel", media=[video(60, 2160, 3840, bitrate=40_000_000)]))
    assert {"video_too_wide", "video_bitrate"} <= warnings(issues)
    assert "video_not_vertical" in warnings(check(target("instagram", "reel", media=[video(30, 1920, 1080)])))


def test_instagram_story_rules_and_business_only() -> None:
    story = target("instagram", "story", text="", media=[video(30)])
    assert not check(story)
    assert "video_too_long" in errors(check(target("instagram", "story", text="", media=[video(61)])))
    assert "video_too_large" in errors(
        check(target("instagram", "story", text="", media=[video(30, size=120_000_000)]))
    )
    creator = rules.RuleContext(account_type="creator", public_media_mode="tunnel", now=NOW)
    assert "ig_story_business_only" in errors(check(story, creator))
    business = rules.RuleContext(account_type="business", public_media_mode="tunnel", now=NOW)
    assert not errors(check(story, business))


def test_instagram_options() -> None:
    pic = [image()]
    six = ["ana", "bea", "carla", "dani", "eva", "fran"]
    assert "ig_too_many_collaborators" in errors(
        check(target("instagram", "post", media=pic, options={"collaborators": six}))
    )
    assert not errors(check(target("instagram", "post", media=pic, options={"collaborators": "@ana, bea"})))
    assert "ig_collaborator_invalid" in errors(
        check(target("instagram", "post", media=pic, options={"collaborators": ["no válido!"]}))
    )
    story = target("instagram", "story", text="", media=pic, options={"collaborators": ["ana"]})
    assert "ig_collaborators_story" in warnings(check(story))
    assert "ig_trial_invalid" in errors(
        check(target("instagram", "reel", media=[video(10)], options={"trial_reel": "SIEMPRE"}))
    )
    assert not errors(check(target("instagram", "reel", media=[video(10)], options={"trial_reel": "MANUAL"})))
    assert "ig_thumb_offset" in errors(
        check(target("instagram", "reel", media=[video(10)], options={"thumb_offset_ms": -5}))
    )
    assert "ig_thumb_offset_beyond" in warnings(
        check(target("instagram", "reel", media=[video(10)], options={"thumb_offset_ms": 20_000}))
    )


def test_instagram_images_need_public_media() -> None:
    off = rules.RuleContext(public_media_mode="off", now=NOW)
    issues = check(target("instagram", "post", media=[image()]), off)
    assert "public_media_off" in errors(issues)
    assert "Activa la URL pública en Ajustes" in next(
        i.message for i in issues if i.code == "public_media_off"
    )
    # Videos use resumable upload: no public URL needed.
    assert not errors(check(target("instagram", "reel", media=[video(10)]), off))
    for mode in ("tunnel", "base_url"):
        ctx = rules.RuleContext(public_media_mode=mode, now=NOW)
        assert not errors(check(target("instagram", "post", media=[image()]), ctx))


def test_public_media_mode_read_from_database(db) -> None:
    from planificador import config

    t = target("instagram", "post", media=[image()])
    assert "public_media_off" in errors(rules.validate_target(t))  # default setting is "off"
    config.set_setting(db, "public_media_mode", "tunnel")
    assert not errors(rules.validate_target(t))


def test_public_media_check_skipped_without_database() -> None:
    assert not errors(rules.validate_target(target("instagram", "post", media=[image()])))


# -------------------------------------------------------------------------------------------- tiktok


def tt(options: dict[str, Any] | None = None, media: list[MediaInfo] | None = None, **kw: Any) -> TargetSpec:
    """A TikTok video target; the music consent is given unless ``options`` says otherwise."""
    options = {"consent": True, **(options or {})}
    return target("tiktok", "video", media=media if media is not None else [video(30)], options=options, **kw)


def test_tiktok_privacy_is_required_without_default() -> None:
    issues = check(tt())
    assert errors(issues) == {"tiktok_privacy_required"}
    assert "no hay valor por defecto" in issues[0].message
    assert not errors(check(tt({"privacy_level": "PUBLIC_TO_EVERYONE"})))
    assert "tiktok_privacy_invalid" in errors(check(tt({"privacy_level": "EVERYBODY"})))


def test_tiktok_privacy_against_creator_info() -> None:
    meta = {
        "creator_info": {
            "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
            "max_video_post_duration_sec": 180,
            "comment_disabled": True,
        }
    }
    ctx = rules.RuleContext(account_meta=meta, now=NOW)
    issues = check(tt({"privacy_level": "FOLLOWER_OF_CREATOR"}), ctx)
    assert "tiktok_privacy_not_allowed" in errors(issues)
    assert "«Público», «Solo yo»" in next(i.message for i in issues if i.code == "tiktok_privacy_not_allowed")
    assert not errors(check(tt({"privacy_level": "SELF_ONLY"}), ctx))
    # Creator turned comments off in the app: allowing them here only warns.
    allow = check(tt({"privacy_level": "SELF_ONLY", "disable_comment": False}), ctx)
    assert "tiktok_comment_disabled" in warnings(allow)
    # Creator-specific max duration.
    assert "tiktok_too_long" in errors(check(tt({"privacy_level": "SELF_ONLY"}, [video(200)]), ctx))


def test_tiktok_unaudited_and_branded_content() -> None:
    ctx = rules.RuleContext(account_meta={"creator_info": {"privacy_level_options": ["SELF_ONLY"]}}, now=NOW)
    issues = check(tt({"privacy_level": "SELF_ONLY"}), ctx)
    assert warnings(issues) == {"tiktok_unaudited"} and not errors(issues)
    branded = check(tt({"privacy_level": "SELF_ONLY", "brand_content_toggle": True}), ctx)
    assert "tiktok_branded_private" in errors(branded)
    assert not errors(check(tt({"privacy_level": "SELF_ONLY", "brand_organic_toggle": True}), ctx))


def test_tiktok_consent_must_not_be_refused() -> None:
    assert "tiktok_consent" in errors(check(tt({"privacy_level": "SELF_ONLY", "consent": False})))
    assert not errors(check(tt({"privacy_level": "SELF_ONLY", "consent": True})))


def test_tiktok_inbox_mode_needs_no_privacy() -> None:
    issues = check(tt({"mode": "inbox"}, text=""))
    assert not errors(issues)
    assert warnings(issues) == {"tiktok_inbox_text"}
    assert "tiktok_mode_invalid" in errors(check(tt({"mode": "borrador", "privacy_level": "SELF_ONLY"})))


def test_tiktok_mode_must_match_the_granted_scopes() -> None:
    inbox_only = rules.RuleContext(granted_scopes=frozenset({"user.info.basic", "video.upload"}), now=NOW)
    issues = check(tt({"privacy_level": "SELF_ONLY"}), inbox_only)
    assert errors(issues) == {"tiktok_mode_unavailable"}
    issue = next(i for i in issues if i.code == "tiktok_mode_unavailable")
    assert issue.field == "mode" and "video.publish" in issue.message and "borrador" in issue.message
    # The draft mode works and needs none of the Direct Post fields (privacy, consent, interactions).
    draft = check(tt({"mode": "inbox", "consent": False}, text=""), inbox_only)
    assert not errors(draft)

    direct_only = rules.RuleContext(granted_scopes=frozenset({"user.info.basic", "video.publish"}), now=NOW)
    issues = check(tt({"mode": "inbox"}), direct_only)
    assert errors(issues) == {"tiktok_mode_unavailable"} and "video.upload" in issues[0].message
    assert not errors(check(tt({"privacy_level": "SELF_ONLY"}), direct_only))

    nothing = rules.RuleContext(granted_scopes=frozenset({"user.info.basic"}), now=NOW)
    assert errors(check(tt({"mode": "inbox"}), nothing)) == {"tiktok_no_publish_scope"}
    # Unknown grants: no scope check at all.
    assert not errors(check(tt({"privacy_level": "SELF_ONLY"}), rules.RuleContext(now=NOW)))
    assert rules.tiktok_modes(None) == ("direct", "inbox")
    assert rules.tiktok_modes(inbox_only) == ("inbox",)


def test_context_for_account_reads_granted_scopes(db, brand) -> None:
    from planificador.models import Account, AccountStatus, Network

    account = Account(
        brand_id=brand.id,
        network=Network.TIKTOK,
        external_id="tt-2",
        name="Sandbox",
        status=AccountStatus.CONNECTED,
        scopes=["user.info.basic", "video.upload", "video.list"],
    )
    db.add(account)
    db.commit()
    ctx = rules.context_for_account(db, account)
    assert ctx.granted_scopes == frozenset({"user.info.basic", "video.upload", "video.list"})
    issues = rules.validate_for_account(db, account, tt({"privacy_level": "SELF_ONLY"}))
    assert "tiktok_mode_unavailable" in errors(issues)
    assert not errors(rules.validate_for_account(db, account, tt({"mode": "inbox"})))
    legacy = Account(brand_id=brand.id, network=Network.TIKTOK, external_id="tt-3", name="Antigua")
    assert rules.context_for_account(db, legacy).granted_scopes is None


@pytest.mark.parametrize(
    ("clip", "code"),
    [
        (video(700), "video_too_long"),
        (video(30, fps=15.0), "video_fps"),
        (video(30, fps=120.0), "video_fps"),
        (video(30, 200, 356), "video_resolution_low"),
        (video(30, 2160, 5000), "video_resolution_high"),
        (video(30, codec="mpeg4"), "video_codec"),
        (video(30, mime="video/x-msvideo", name="a.avi"), "video_container"),
        (video(30, size=4_100_000_000), "video_too_large"),
    ],
)
def test_tiktok_video_hard_limits(clip: MediaInfo, code: str) -> None:
    assert code in errors(check(tt({"privacy_level": "SELF_ONLY"}, [clip])))


def test_tiktok_soft_limits_and_text() -> None:
    issues = check(tt({"privacy_level": "SELF_ONLY"}, [video(2)]))
    assert "video_too_short" in warnings(issues) and not errors(issues)
    long = check(tt({"privacy_level": "SELF_ONLY"}, [video(250)]))  # creator max unknown
    assert "tiktok_duration_unverified" in warnings(long) and not errors(long)
    lines = check(tt({"privacy_level": "SELF_ONLY"}, text="uno\ndos"))
    assert "tiktok_newlines" in warnings(lines)
    assert "media_one_video" in errors(check(tt({"privacy_level": "SELF_ONLY"}, [video(), video()])))
    assert "media_video_only" in errors(check(tt({"privacy_level": "SELF_ONLY"}, [image()])))
    webm = video(30, mime="video/webm", codec="vp9", name="c.webm")
    assert not errors(check(tt({"privacy_level": "SELF_ONLY"}, [webm])))


def test_hevc_and_hdr_warn() -> None:
    clip = video(30, codec="hevc", hdr=True)
    issues = check(tt({"privacy_level": "SELF_ONLY"}, [clip]))
    assert {"video_hevc", "video_hdr"} <= warnings(issues)
    assert not errors(issues)  # TikTok accepts H.265
    assert {"video_hevc", "video_hdr"} <= warnings(check(target("instagram", "reel", media=[clip])))


# ------------------------------------------------------------------------------------------- youtube


def yt(fmt: str = "video", options: dict[str, Any] | None = None, **kw: Any) -> TargetSpec:
    base = {"title": "Mi vídeo", "made_for_kids": False}
    base.update(options or {})
    kw.setdefault("media", [video(120, 1920, 1080)])
    return target("youtube", fmt, options=base, **kw)


def test_youtube_required_title_and_made_for_kids() -> None:
    assert not check(yt())
    bare = target("youtube", "video", text="", media=[video(120, 1920, 1080)])
    assert {"youtube_title_required", "youtube_made_for_kids_required"} <= errors(check(bare))
    from_text = check(yt(options={"title": ""}, text="Primera línea\nresto"))
    assert "youtube_title_from_text" in warnings(from_text) and not errors(from_text)
    assert "youtube_title_too_long" in errors(check(yt(options={"title": "x" * 101})))
    assert not errors(check(yt(options={"title": "x" * 100})))
    assert "youtube_title_chars" in errors(check(yt(options={"title": "a <b>"})))
    for value in ("false", "no", "0", True, "si"):
        assert not errors(check(yt(options={"made_for_kids": value}))), value
    assert "youtube_made_for_kids_required" in errors(check(yt(options={"made_for_kids": "quizá"})))


def test_youtube_description_tags_and_options() -> None:
    assert "youtube_description_chars" in errors(check(yt(text="a > b")))
    assert "text_missing" in warnings(check(yt(text="")))
    tags = ["x" * 99] * 5  # 495 + 4 commas = 499
    assert rules.youtube_tags_length(tags) == 499
    assert not errors(check(yt(options={"tags": tags})))
    assert "youtube_tags_too_long" in errors(check(yt(options={"tags": tags + ["ab"]})))
    assert rules.youtube_tags_length(["dos palabras", "uno"]) == 12 + 2 + 3 + 1
    assert "youtube_privacy" in errors(check(yt(options={"privacy_status": "secreto"})))
    assert not errors(check(yt(options={"privacy_status": "oculto"})))
    assert "youtube_license" in errors(check(yt(options={"license": "gpl"})))
    assert "youtube_category" in errors(check(yt(options={"category_id": "música"})))
    assert "youtube_language" in errors(check(yt(options={"language": "español"})))
    assert not errors(check(yt(options={"language": "es-419"})))
    kids = check(yt(options={"made_for_kids": True, "first_comment": "¡Hola!"}))
    assert "youtube_kids_comment" in errors(kids)


def test_youtube_native_schedule() -> None:
    later = NOW + timedelta(days=1)
    assert not check(yt(options={"native_schedule": True}, scheduled_at=later))
    private = check(yt(options={"native_schedule": True, "privacy_status": "private"}, scheduled_at=later))
    assert "youtube_native_privacy" in errors(private)
    soon = check(yt(options={"native_schedule": True}, scheduled_at=NOW + timedelta(minutes=5)))
    assert "youtube_native_too_soon" in warnings(soon) and not errors(soon)


def test_youtube_short_rules() -> None:
    assert not check(yt("short", media=[video(45)]))
    assert "video_too_long" in errors(check(yt("short", media=[video(181)])))
    assert "video_not_vertical" in errors(check(yt("short", media=[video(30, 1920, 1080)])))
    assert not errors(check(yt("short", media=[video(30, 1080, 1080)])))  # square is fine
    assert "youtube_short_content_id" in warnings(check(yt("short", media=[video(90)])))
    thumb = check(yt("short", options={"thumbnail_media_id": 5}, media=[video(30)]))
    assert "youtube_short_thumbnail" in warnings(thumb)


def test_youtube_long_uploads() -> None:
    long = [video(20 * 60, 1920, 1080)]
    assert "youtube_long_upload_unknown" in warnings(check(yt(media=long)))
    blocked = rules.RuleContext(account_meta={"long_uploads_status": "eligible"}, now=NOW)
    assert "youtube_long_upload" in errors(check(yt(media=long), blocked))
    allowed = rules.RuleContext(account_meta={"long_uploads_status": "allowed"}, now=NOW)
    assert not check(yt(media=long), allowed)
    assert "video_too_long" in errors(check(yt(media=[video(13 * 3600, 1920, 1080)]), allowed))
    vertical = check(yt(media=[video(60)]))
    assert "youtube_will_be_short" in warnings(vertical)


# ---------------------------------------------------------------------------------------------- demo


def test_demo_accepts_anything_reasonable() -> None:
    assert not check(target("demo", "post", text=""))
    assert not check(target("demo", "post", media=[image(), video(5000, codec="hevc", hdr=True)]))
    assert "media_too_many" in errors(check(target("demo", "post", media=[image() for _ in range(11)])))
    assert "text_too_long" in errors(check(target("demo", "post", text="x" * 5001)))


def test_errors_come_first_and_are_deduplicated() -> None:
    issues = check(target("instagram", "story", text="Hola", media=[video(90, 1920, 1080), video(5)]))
    levels = [i.level for i in issues]
    assert levels == sorted(levels, key=lambda lv: lv != "error")
    assert len({(i.code, i.message) for i in issues}) == len(issues)


def test_missing_metadata_only_warns() -> None:
    clip = video(None)
    issues = check(target("facebook", "reel", media=[clip]))
    assert "video_duration_unknown" in warnings(issues) and not errors(issues)


def test_tolerates_none_fields() -> None:
    t = TargetSpec("facebook", "post", None, None, None, None, True)  # type: ignore[arg-type]
    assert errors(rules.validate_target(t, CTX)) == {"post_empty"}


def test_validate_for_account_uses_account_meta(db, brand) -> None:
    from planificador import config
    from planificador.models import Account, AccountStatus, Network

    config.set_setting(db, "public_media_mode", "off")
    account = Account(
        brand_id=brand.id,
        network=Network.TIKTOK,
        external_id="tt-1",
        name="Creadora",
        account_type="creator",
        status=AccountStatus.CONNECTED,
        meta={"creator_info": {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 60}},
    )
    db.add(account)
    db.commit()
    ctx = rules.context_for_account(db, account)
    assert ctx.public_media_mode == "off" and ctx.account_type == "creator"
    issues = rules.validate_for_account(db, account, tt({"privacy_level": "PUBLIC_TO_EVERYONE"}, [video(90)]))
    assert {"tiktok_privacy_not_allowed", "tiktok_too_long"} <= errors(issues)


# ------------------------------------------------------------------------------------- compatibility


def test_network_compat_for_a_feed_image() -> None:
    compat = rules.network_compat(image(1080, 1350), CTX)
    assert compat["facebook"] == [] and compat["instagram"] == []
    assert [i.code for i in compat["tiktok"]] == ["kind_unsupported"]
    assert "solo admite vídeos" in compat["tiktok"][0].message
    assert [i.code for i in compat["youtube"]] == ["kind_unsupported"]


def test_network_compat_for_a_horizontal_long_video() -> None:
    clip = video(5 * 60, 1920, 1080)
    compat = rules.network_compat(clip, CTX)
    fb = compat["facebook"]
    assert rules.compat_status(fb) == "warning"
    assert {i.code for i in fb} == {"format_unavailable"}
    assert any("reel" in i.message for i in fb) and any("historia" in i.message for i in fb)
    assert rules.compat_status(compat["tiktok"]) == "warning"  # 5 min: depends on the creator
    yt_codes = {i.code for i in compat["youtube"]}
    assert yt_codes == {"format_unavailable"}  # fine as video, not as Short


def test_network_compat_errors_when_no_format_fits() -> None:
    compat = rules.network_compat(video(20 * 60, 1080, 1920), CTX)
    assert rules.compat_status(compat["instagram"]) == "error"
    assert rules.compat_status(compat["tiktok"]) == "error"
    assert rules.compat_status(compat["facebook"]) == "warning"  # a post accepts it
    off = rules.RuleContext(public_media_mode="off", now=NOW)
    assert [i.code for i in rules.network_compat(image(), off)["instagram"]] == ["public_media_off"]


def test_compat_summary_json_shape() -> None:
    summary = rules.compat_summary(video(30, codec="hevc"), CTX)
    assert set(summary) == set(rules.REAL_NETWORKS)
    tiktok = summary["tiktok"]
    assert tiktok["label"] == "TikTok" and tiktok["status"] == "warning"
    assert tiktok["formats"] == {"video": {"label": "Vídeo", "status": "warning"}}
    assert {"level", "code", "message", "field"} == set(tiktok["issues"][0])
    assert rules.compat_status([]) == "ok"

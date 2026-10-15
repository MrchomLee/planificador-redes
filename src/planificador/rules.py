"""Per-network formats, options and validation rules (SPEC §7).

Everything network-specific that the composer, the CSV importer, the media library badges and the engine's
pre-publish check need lives here, driven by the limit tables at the top of the module. Every limit cites the
research report it comes from (``docs/research/0x-*.md``); where a report's "Verificación" section marks a
value UNVERIFIED the rule only emits a **warning**, so a doubtful limit never blocks a post.

Contract (SPEC §7 + cross-module contracts):

- :data:`FORMATS` — ``network -> list[FormatSpec]`` (same keys/labels as the providers).
- :func:`validate_target` ``(target, context=None) -> list[Issue]`` — Spanish messages, errors first. Drafts
  and ideas may be saved with errors; scheduling/publishing needs none (the server re-validates).
- :func:`default_options` ``(network, format) -> dict``.
- :func:`char_limit` ``(network, format) -> int`` — in the network's own unit (:func:`char_unit`): Python
  characters for Facebook/Instagram, **UTF-16 code units** for TikTok (= JavaScript ``String.length``),
  **UTF-8 bytes** for the YouTube description. :func:`text_length` counts a text in that unit.
- :func:`network_compat` ``(media_info, context=None) -> dict[network, list[Issue]]`` — can this single media
  file be published on each real network (library badges)? :func:`compat_summary` is the JSON form.

The optional :class:`RuleContext` (built from the DB with :func:`context_for_account`) enables the
account-dependent checks: TikTok ``creator_info`` (privacy options, max duration), YouTube
``long_uploads_status``, Instagram stories (business accounts only) and the public-media requirement for
Instagram photos (read from the ``public_media_mode`` setting when the context does not say).

Time-dependent checks (native scheduling windows) are warnings only: the engine re-validates when a post is
due, and the providers fall back to publishing directly when the native window has passed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from planificador.providers.base import FormatSpec, Issue, MediaInfo, TargetSpec

log = logging.getLogger(__name__)

Level = Literal["error", "warning"]

NETWORKS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube", "demo")
REAL_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube")
NETWORK_LABELS: dict[str, str] = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "demo": "Demo",
}

# Networks quote "MB"/"GB" without saying which; the decimal reading is the stricter one.
MB = 1_000_000
GB = 1_000_000_000
GIB = 1024**3
#: Float tolerance for durations (containers round the last frame).
DURATION_TOLERANCE_S = 0.05

# ===================================================================================== limits tables
# ---- Facebook — docs/research/02-meta-api.md §3 (verificación §11) ---------------------------------
FB_TEXT_MAX = 63_206  # §3.1 message length
FB_POST_MAX_IMAGES = 10  # §3.2 multi-photo posts (attached_media), Brightbean max 10
FB_REEL_DURATION_S = (3.0, 90.0)  # §3.3 (verif. #11: UNVERIFIED but enforced by Brightbean)
FB_REEL_MIN_SIDE = 540  # §3.3 540×960 minimum
FB_REEL_FPS = (24.0, 60.0)  # §3.3
FB_STORY_VIDEO_DURATION_S = (3.0, 60.0)  # §3.5
FB_VIDEO_SOFT_MAX_BYTES = 1 * GB  # §3.4 1 GB non-resumable / 1.5 GB chunked (2026 limits UNVERIFIED)
FB_VIDEO_SOFT_MAX_S = 45 * 60.0  # §3.4 45 min chunked (UNVERIFIED)
FB_TITLE_MAX = 255
FB_NATIVE_WINDOW = (timedelta(minutes=15), timedelta(days=29))  # SPEC §7 / §3.7 (10 min – 30 days, safe)

# ---- Instagram — docs/research/02-meta-api.md §4.2 / §4.4 (verificación #18–#20) -------------------
IG_CAPTION_MAX = 2_200  # §4.2
IG_MAX_HASHTAGS = 30  # §4.2
IG_MAX_MENTIONS = 20  # §4.2
IG_CAROUSEL_MAX = 10  # §4.2 children 2–10 (verif. #18 CONFIRMED)
IG_FEED_ASPECT = (4 / 5, 1.91)  # §4.4 4:5 … 1.91:1 (verif. #18/#23 CONFIRMED)
IG_MIN_IMAGE_WIDTH = 320  # §4.4 width 320–1440 (scaled)
IG_REEL_DURATION_S = (3.0, 15 * 60.0)  # §4.4 3 s – 15 min (verif. #20: duration from ig-publish only)
IG_REEL_MAX_BYTES = 300 * MB  # §4.4 (verif. #20 PARTLY: Postiz error text)
IG_REEL_MAX_WIDTH = 1920  # §4.4
IG_MAX_BITRATE = 25_000_000  # §4.4 ≤ 25 Mbps
IG_STORY_VIDEO_DURATION_S = (3.0, 60.0)  # §4.4 story video 3–60 s
IG_STORY_VIDEO_MAX_BYTES = 100 * MB  # §4.4
IG_CAROUSEL_VIDEO_MAX_S = 60.0  # §4.4 carousel video items ≈ 60 s (UNVERIFIED → warning)
IG_CAROUSEL_VIDEO_MAX_BYTES = 100 * MB  # §4.4 (UNVERIFIED → warning)
IG_COLLABORATORS_MAX = 5  # SPEC §7 (02 §4.2 says 3, UNVERIFIED)
IG_VIDEO_MIMES = ("video/mp4", "video/quicktime", "video/x-m4v")  # §4.4 MOV/MP4
IG_TRIAL_STRATEGIES = ("MANUAL", "SS_PERFORMANCE")  # §4.2 trial_params.graduation_strategy

# ---- TikTok — docs/research/03-tiktok-api.md §4, §5.4 (verificación §14) ---------------------------
TT_CAPTION_MAX_UTF16 = 2_200  # §5.1 / verif. #12 (UTF-16 code units)
TT_PHOTO_DESCRIPTION_MAX_UTF16 = 4_000  # §7 (photo posts, disabled in v1)
TT_VIDEO_MIMES = ("video/mp4", "video/quicktime", "video/webm", "video/x-m4v")  # §5.4 MP4/WebM/MOV
TT_VIDEO_CODECS = ("h264", "hevc", "vp8", "vp9")  # §5.4 official
TT_FPS = (23.0, 60.0)  # §5.4 official (fail_reason frame_rate_check_failed)
TT_MIN_SIDE = 360  # §5.4 official minimum
TT_MAX_SIDE = 4096  # §5.4 (via Ayrshare)
TT_MIN_DURATION_S = 3.0  # §5.4 (UNVERIFIED → warning)
TT_DEFAULT_MAX_DURATION_S = 180.0  # §4.1 every creator may post ≥ 180 s
TT_ABSOLUTE_MAX_DURATION_S = 600.0  # §4.1 largest creator limit seen (180/300/600)
TT_MAX_BYTES = 4 * GB  # §5.4 / verif. #22 enforce 4 GB
TT_PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
TT_PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Público",
    "MUTUAL_FOLLOW_FRIENDS": "Amigos mutuos",
    "FOLLOWER_OF_CREATOR": "Seguidores",
    "SELF_ONLY": "Solo yo",
}
TT_MODES = ("direct", "inbox")

# ---- YouTube — docs/research/06-youtube-api.md §4.3, §5, §13 (verificación §17) -------------------
YT_TITLE_MAX = 100  # §4.3 (verif. #11 CONFIRMED)
YT_DESCRIPTION_MAX_BYTES = 5_000  # §4.3 UTF-8 bytes
YT_TAGS_MAX = 500  # §4.3 commas count, tags with spaces +2
YT_VIDEO_MAX_S = 12 * 3600.0  # §13.1 ≤ 12 h
YT_VIDEO_MAX_BYTES = 256 * GIB  # §4.1 maxSize 256 GiB (verif. #10)
YT_LONG_UPLOAD_S = 15 * 60.0  # §13.1 > 15 min needs long_uploads_status == allowed
YT_SHORT_MAX_S = 180.0  # §5 (verif. #17 CONFIRMED)
YT_SHORT_CONTENT_ID_S = 60.0  # §5 Shorts > 1 min with a Content ID claim are blocked
YT_NATIVE_MIN = timedelta(minutes=15)  # §13.2 native_schedule only ≥ 15 min ahead
YT_PRIVACY_ALIASES = {
    "public": "public",
    "publica": "public",
    "pública": "public",
    "publico": "public",
    "público": "public",
    "unlisted": "unlisted",
    "oculto": "unlisted",
    "no listado": "unlisted",
    "private": "private",
    "privado": "private",
    "privada": "private",
}
YT_LICENSES = ("youtube", "creativeCommon")
YT_DEFAULT_CATEGORY = "22"  # People & Blogs / "Gente y blogs"

# ---- Demo + generic -----------------------------------------------------------------------------
DEMO_TEXT_MAX = 5_000
DEMO_MAX_ITEMS = 10
LARGE_FILE_BYTES = 1 * GB  # SPEC §7 "very large files" warning

# ---- Formats (same keys and labels as the providers) ---------------------------------------------
FORMATS: dict[str, list[FormatSpec]] = {
    "facebook": [
        FormatSpec("post", "Publicación", "images_or_video"),
        FormatSpec("reel", "Reel", "video"),
        FormatSpec("story", "Historia", "image_or_video"),
    ],
    "instagram": [
        FormatSpec("post", "Publicación", "images_or_video"),
        FormatSpec("reel", "Reel", "video"),
        FormatSpec("story", "Historia", "image_or_video"),
    ],
    "tiktok": [
        FormatSpec("video", "Vídeo", "video"),
        FormatSpec("photo", "Fotos", "images"),
    ],
    "youtube": [
        FormatSpec("video", "Vídeo", "video"),
        FormatSpec("short", "Short", "video"),
    ],
    "demo": [FormatSpec("post", "Publicación", "images_or_video")],
}

MSG_TIKTOK_PHOTO = (
    "Las publicaciones de fotos en TikTok necesitan un dominio público verificado y no están disponibles en "
    "esta versión. Publica un vídeo o usa la Publicación asistida."
)
#: Formats listed (shown greyed) but not publishable in v1: (network, format) -> Spanish reason.
DISABLED_FORMATS: dict[tuple[str, str], str] = {("tiktok", "photo"): MSG_TIKTOK_PHOTO}

MSG_PUBLIC_MEDIA_OFF = "Activa la URL pública en Ajustes para publicar fotos en Instagram."

# Spanish nouns per format: (article, noun).
_FORMAT_NOUNS: dict[str, tuple[str, str]] = {
    "post": ("la", "publicación"),
    "reel": ("el", "reel"),
    "story": ("la", "historia"),
    "video": ("el", "vídeo"),
    "short": ("el", "Short"),
    "photo": ("la", "publicación de fotos"),
}


# ========================================================================================= rule types


@dataclass(frozen=True)
class VideoRule:
    """Limits for one video in a format. ``None`` = no check. Levels say whether a breach blocks."""

    min_s: float | None = None
    min_level: Level = "error"
    max_s: float | None = None
    max_level: Level = "error"
    soft_max_s: float | None = None  # warning only
    max_bytes: int | None = None
    max_bytes_level: Level = "error"
    soft_max_bytes: int | None = None  # warning only
    min_side: int | None = None  # shorter side
    min_side_level: Level = "warning"
    max_side: int | None = None  # longer side (error)
    max_width: int | None = None  # warning
    fps: tuple[float, float] | None = None
    fps_level: Level = "warning"
    max_bitrate: int | None = None  # warning
    mimes: tuple[str, ...] | None = None  # container (error)
    codecs: tuple[str, ...] | None = None  # video codec (error)
    orientation: Literal["vertical_recommended", "portrait_or_square"] | None = None


@dataclass(frozen=True)
class ImageRule:
    """Limits for one image. Facebook/Instagram images are converted to JPEG (sRGB, size-capped) at publish
    time, so size and container are not checked for them."""

    aspect: tuple[float, float] | None = None  # (min, max) of width/height → warning outside
    vertical_recommended: bool = False
    min_width: int | None = None  # warning


@dataclass(frozen=True)
class FormatRule:
    media: str  # FormatSpec.media
    max_items: int = 1
    allow_empty: bool = False  # text-only allowed (Facebook post)
    mix: bool = False  # images and videos together (Instagram carousel)
    image: ImageRule | None = None  # None → images not allowed
    video: VideoRule | None = None  # None → videos not allowed
    multi_video: VideoRule | None = None  # rules for videos inside a carousel
    text_max: int = 0
    text_shown: bool = True  # stories show no caption
    source: str = ""


_FB_VIDEO = VideoRule(soft_max_bytes=FB_VIDEO_SOFT_MAX_BYTES, soft_max_s=FB_VIDEO_SOFT_MAX_S)
_IG_REEL_VIDEO = VideoRule(
    min_s=IG_REEL_DURATION_S[0],
    max_s=IG_REEL_DURATION_S[1],
    max_bytes=IG_REEL_MAX_BYTES,
    max_width=IG_REEL_MAX_WIDTH,
    max_bitrate=IG_MAX_BITRATE,
    mimes=IG_VIDEO_MIMES,
)

FORMAT_RULES: dict[tuple[str, str], FormatRule] = {
    # 02 §3.1–§3.4: text and/or 1–10 photos, or 1 video.
    ("facebook", "post"): FormatRule(
        "images_or_video",
        max_items=FB_POST_MAX_IMAGES,
        allow_empty=True,
        image=ImageRule(),
        video=_FB_VIDEO,
        text_max=FB_TEXT_MAX,
        source="02 §3.1–§3.4",
    ),
    # 02 §3.3: Reels 3–90 s, 9:16, ≥ 540×960, 24–60 fps.
    ("facebook", "reel"): FormatRule(
        "video",
        video=VideoRule(
            min_s=FB_REEL_DURATION_S[0],
            max_s=FB_REEL_DURATION_S[1],
            soft_max_bytes=FB_VIDEO_SOFT_MAX_BYTES,
            min_side=FB_REEL_MIN_SIDE,
            fps=FB_REEL_FPS,
            orientation="vertical_recommended",
        ),
        text_max=FB_TEXT_MAX,
        source="02 §3.3",
    ),
    # 02 §3.5: one photo or one 3–60 s video, 9:16.
    ("facebook", "story"): FormatRule(
        "image_or_video",
        image=ImageRule(vertical_recommended=True),
        video=VideoRule(
            min_s=FB_STORY_VIDEO_DURATION_S[0],
            max_s=FB_STORY_VIDEO_DURATION_S[1],
            orientation="vertical_recommended",
        ),
        text_max=FB_TEXT_MAX,
        text_shown=False,
        source="02 §3.5",
    ),
    # 02 §4.2/§4.4: 1 image, 1 video (published as REELS) or a 2–10 item carousel (photos and videos).
    ("instagram", "post"): FormatRule(
        "images_or_video",
        max_items=IG_CAROUSEL_MAX,
        mix=True,
        image=ImageRule(aspect=IG_FEED_ASPECT, min_width=IG_MIN_IMAGE_WIDTH),
        video=_IG_REEL_VIDEO,
        multi_video=VideoRule(
            max_s=IG_CAROUSEL_VIDEO_MAX_S,
            max_level="warning",
            max_bytes=IG_CAROUSEL_VIDEO_MAX_BYTES,
            max_bytes_level="warning",
            mimes=IG_VIDEO_MIMES,
        ),
        text_max=IG_CAPTION_MAX,
        source="02 §4.2, §4.4",
    ),
    ("instagram", "reel"): FormatRule(
        "video",
        video=VideoRule(
            min_s=IG_REEL_DURATION_S[0],
            max_s=IG_REEL_DURATION_S[1],
            max_bytes=IG_REEL_MAX_BYTES,
            max_width=IG_REEL_MAX_WIDTH,
            max_bitrate=IG_MAX_BITRATE,
            mimes=IG_VIDEO_MIMES,
            orientation="vertical_recommended",
        ),
        text_max=IG_CAPTION_MAX,
        source="02 §4.4",
    ),
    ("instagram", "story"): FormatRule(
        "image_or_video",
        image=ImageRule(vertical_recommended=True),
        video=VideoRule(
            min_s=IG_STORY_VIDEO_DURATION_S[0],
            max_s=IG_STORY_VIDEO_DURATION_S[1],
            max_bytes=IG_STORY_VIDEO_MAX_BYTES,
            mimes=IG_VIDEO_MIMES,
            orientation="vertical_recommended",
        ),
        text_max=IG_CAPTION_MAX,
        text_shown=False,
        source="02 §4.2, §4.4",
    ),
    # 03 §5.4: MP4/WebM/MOV, H.264/H.265/VP8/VP9, 23–60 fps, 360–4096 px, ≤ creator max, ≤ 4 GB.
    ("tiktok", "video"): FormatRule(
        "video",
        video=VideoRule(
            min_s=TT_MIN_DURATION_S,
            min_level="warning",
            max_s=TT_ABSOLUTE_MAX_DURATION_S,  # refined with creator_info in _tiktok_checks
            max_bytes=TT_MAX_BYTES,
            min_side=TT_MIN_SIDE,
            min_side_level="error",
            max_side=TT_MAX_SIDE,
            fps=TT_FPS,
            fps_level="error",
            mimes=TT_VIDEO_MIMES,
            codecs=TT_VIDEO_CODECS,
        ),
        text_max=TT_CAPTION_MAX_UTF16,
        source="03 §5.4",
    ),
    # 03 §7: listed but disabled in v1 (PULL_FROM_URL from a verified domain only).
    ("tiktok", "photo"): FormatRule(
        "images", max_items=35, image=ImageRule(), text_max=TT_PHOTO_DESCRIPTION_MAX_UTF16, source="03 §7"
    ),
    # 06 §13.1: one video ≤ 12 h / 256 GB.
    ("youtube", "video"): FormatRule(
        "video",
        video=VideoRule(max_s=YT_VIDEO_MAX_S, max_bytes=YT_VIDEO_MAX_BYTES),
        text_max=YT_DESCRIPTION_MAX_BYTES,
        source="06 §4.3, §13.1",
    ),
    # 06 §5: ≤ 180 s, vertical or square.
    ("youtube", "short"): FormatRule(
        "video",
        video=VideoRule(max_s=YT_SHORT_MAX_S, max_bytes=YT_VIDEO_MAX_BYTES, orientation="portrait_or_square"),
        text_max=YT_DESCRIPTION_MAX_BYTES,
        source="06 §5, §13.1",
    ),
    ("demo", "post"): FormatRule(
        "images_or_video",
        max_items=DEMO_MAX_ITEMS,
        allow_empty=True,
        mix=True,
        image=ImageRule(),
        video=VideoRule(),
        multi_video=VideoRule(),
        text_max=DEMO_TEXT_MAX,
        source="SPEC §5 (demo)",
    ),
}

_TEXT_UNITS: dict[str, Literal["chars", "utf16", "bytes"]] = {
    "facebook": "chars",
    "instagram": "chars",
    "tiktok": "utf16",
    "youtube": "bytes",
    "demo": "chars",
}

_DEFAULT_OPTIONS: dict[tuple[str, str], dict[str, Any]] = {
    ("facebook", "post"): {"title": "", "link": "", "native_schedule": False},
    ("facebook", "reel"): {"title": ""},
    ("facebook", "story"): {},
    ("instagram", "post"): {"first_comment": "", "collaborators": [], "alt_text": "", "location_id": ""},
    ("instagram", "reel"): {
        "first_comment": "",
        "collaborators": [],
        "share_to_feed": True,
        "thumb_offset_ms": None,
        "audio_name": "",
        "trial_reel": None,
        "location_id": "",
    },
    ("instagram", "story"): {},
    # TikTok: privacy has NO default (Content Sharing Guidelines, 03 §4.2); interactions unchecked.
    ("tiktok", "video"): {
        "mode": "direct",
        "privacy_level": None,
        "disable_comment": True,
        "disable_duet": True,
        "disable_stitch": True,
        "is_aigc": False,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
        "video_cover_timestamp_ms": None,
    },
    ("tiktok", "photo"): {"mode": "direct", "privacy_level": None, "disable_comment": True},
    # YouTube (06 §13.2): made_for_kids has NO default.
    ("youtube", "video"): {
        "title": "",
        "privacy_status": "public",
        "native_schedule": False,
        "category_id": YT_DEFAULT_CATEGORY,
        "tags": [],
        "made_for_kids": None,
        "synthetic_media": False,
        "paid_promotion": False,
        "notify_subscribers": True,
        "thumbnail_media_id": None,
        "playlist_id": None,
        "first_comment": "",
        "language": None,
        "embeddable": True,
        "license": "youtube",
    },
    ("demo", "post"): {},
}
_DEFAULT_OPTIONS[("youtube", "short")] = {
    k: v for k, v in _DEFAULT_OPTIONS[("youtube", "video")].items() if k != "thumbnail_media_id"
}


# ============================================================================================ context


@dataclass
class RuleContext:
    """Optional facts about the target account/app that refine validation.

    ``account_type`` and ``account_meta`` come from the ``accounts`` row (TikTok ``creator_info`` cache,
    YouTube ``long_uploads_status``...); ``public_media_mode`` is the ``public_media_mode`` setting
    (``None`` = read it from the current database when needed); ``now`` (aware UTC) feeds the
    native-scheduling hints.
    """

    account_type: str | None = None
    account_meta: Mapping[str, Any] = field(default_factory=dict)
    public_media_mode: str | None = None
    now: datetime | None = None

    def current_time(self) -> datetime:
        return self.now or datetime.now(UTC)


def context_for_account(db: Any, account: Any, clock: Any = None) -> RuleContext:
    """:class:`RuleContext` for an ``Account`` row (reads the public-media setting from ``db``)."""
    mode: str | None = None
    try:
        from planificador import config

        mode = str(config.get_setting(db, "public_media_mode") or "off")
    except Exception:  # pragma: no cover - settings table unavailable
        log.debug("public_media_mode unavailable", exc_info=True)
    return RuleContext(
        account_type=getattr(account, "account_type", None) if account is not None else None,
        account_meta=dict(getattr(account, "meta", None) or {}) if account is not None else {},
        public_media_mode=mode,
        now=clock.now() if clock is not None else None,
    )


def validate_for_account(db: Any, account: Any, target: TargetSpec, clock: Any = None) -> list[Issue]:
    """:func:`validate_target` with the context of ``account`` (composer/CSV helper)."""
    return validate_target(target, context_for_account(db, account, clock))


def _public_media_mode(context: RuleContext) -> str | None:
    if context.public_media_mode is not None:
        return context.public_media_mode
    try:
        from planificador import config
        from planificador import db as dbmod

        database = dbmod.get_current_database()
        with database.session() as session:
            return str(config.get_setting(session, "public_media_mode") or "off")
    except Exception:
        return None  # no database (pure unit use): skip the check


# ============================================================================================ helpers


def network_label(network: str) -> str:
    return NETWORK_LABELS.get(str(network), str(network).capitalize())


def format_spec(network: str, fmt: str) -> FormatSpec | None:
    for spec in FORMATS.get(str(network), []):
        if spec.key == fmt:
            return spec
    return None


def format_label(network: str, fmt: str) -> str:
    spec = format_spec(network, fmt)
    return spec.label if spec else str(fmt)


def enabled_formats(network: str) -> list[FormatSpec]:
    """Formats that can be published in v1 (the disabled ones are still listed in :data:`FORMATS`)."""
    return [f for f in FORMATS.get(str(network), []) if (str(network), f.key) not in DISABLED_FORMATS]


def disabled_reason(network: str, fmt: str) -> str | None:
    return DISABLED_FORMATS.get((str(network), str(fmt)))


def _subject(network: str, fmt: str, *, capital: bool = True) -> str:
    article, noun = _FORMAT_NOUNS.get(fmt, ("la", "publicación"))
    text = f"{article} {noun} de {network_label(network)}"
    return text[0].upper() + text[1:] if capital else text


def fmt_seconds(value: float) -> str:
    """Spanish duration: '45 s', '3 min', '1 min 30 s', '1 h 5 min'."""
    seconds = round(value)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} min" if not rest else f"{minutes} min {rest} s"
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h" if rest < 60 else f"{hours} h {rest // 60} min"


def _fmt_bytes(value: float) -> str:
    if value >= GB:
        return (f"{value / GB:.1f}".rstrip("0").rstrip(".") + " GB").replace(".", ",")
    if value >= MB:
        return f"{value / MB:.0f} MB"
    return f"{value / 1000:.0f} KB"


_KNOWN_RATIOS: tuple[tuple[float, str], ...] = (
    (9 / 16, "9:16"),
    (2 / 3, "2:3"),
    (3 / 4, "3:4"),
    (4 / 5, "4:5"),
    (1.0, "1:1"),
    (4 / 3, "4:3"),
    (3 / 2, "3:2"),
    (16 / 9, "16:9"),
    (1.91, "1,91:1"),
)


def fmt_ratio(width: int, height: int) -> str:
    """Aspect ratio label ('9:16', '4:5', '2,35:1')."""
    ratio = width / height
    for value, label in _KNOWN_RATIOS:
        if abs(ratio - value) < 0.01:
            return label
    if ratio >= 1:
        return (f"{ratio:.2f}".rstrip("0").rstrip(".") + ":1").replace(".", ",")
    return ("1:" + f"{1 / ratio:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


def _name(m: MediaInfo) -> str:
    return m.original_name or f"archivo {m.id}"


def _kind(m: MediaInfo) -> str:
    return str(getattr(m, "kind", "") or "")


def _as_bool(value: Any, default: bool | None = False) -> bool | None:
    """Bools and form-ish strings ("1", "si", "on"...). Unknown/absent → ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "si", "sí", "on", "y", "s"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, list | tuple) else re.split(r"[,\n]+", str(value))
    return [str(i).strip() for i in items if str(i).strip()]


def _dedupe(issues: Iterable[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Issue] = []
    for issue in issues:
        key = (issue.level, issue.code, issue.message)
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return out


def issue_dict(issue: Issue) -> dict[str, Any]:
    return {"level": issue.level, "code": issue.code, "message": issue.message, "field": issue.field}


def has_errors(issues: Iterable[Issue]) -> bool:
    return any(i.level == "error" for i in issues)


def errors_only(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]


def compat_status(issues: Iterable[Issue]) -> Literal["ok", "warning", "error"]:
    levels = {i.level for i in issues}
    if "error" in levels:
        return "error"
    if "warning" in levels:
        return "warning"
    return "ok"


# =================================================================================== text counting


def char_unit(network: str) -> Literal["chars", "utf16", "bytes"]:
    """Unit of :func:`char_limit` for ``network``: ``chars`` (Python ``len``), ``utf16`` (TikTok counts
    UTF-16 code units, like JavaScript ``String.length``) or ``bytes`` (YouTube description, UTF-8)."""
    return _TEXT_UNITS.get(str(network), "chars")


def text_length(network: str, text: str | None) -> int:
    """Length of ``text`` in the unit ``network`` counts (see :func:`char_unit`)."""
    text = text or ""
    unit = char_unit(network)
    if unit == "utf16":
        return len(text.encode("utf-16-le")) // 2
    if unit == "bytes":
        return len(text.encode("utf-8"))
    return len(text)


def char_limit(network: str, format: str) -> int:  # noqa: A002 - contract signature
    """Maximum text length for ``network``/``format`` in :func:`char_unit` units. Unknown format → the
    network's first format; unknown network → ``ValueError``."""
    network = str(network)
    rule = FORMAT_RULES.get((network, str(format)))
    if rule is None:
        specs = FORMATS.get(network)
        if not specs:
            raise ValueError(f"Unknown network: {network!r}")
        rule = FORMAT_RULES[(network, specs[0].key)]
    return rule.text_max


def default_options(network: str, format: str) -> dict:  # noqa: A002 - contract signature
    """Initial options for a new target (a fresh copy; unknown network/format → ``{}``)."""
    base = _DEFAULT_OPTIONS.get((str(network), str(format)), {})
    return {k: (list(v) if isinstance(v, list) else v) for k, v in base.items()}


_HASHTAG_RE = re.compile(r"(?<![\w&])#(\w+)")
_MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9._]+)")
_IG_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def count_hashtags(text: str | None) -> int:
    return len(_HASHTAG_RE.findall(text or ""))


def count_mentions(text: str | None) -> int:
    return len(_MENTION_RE.findall(text or ""))


def youtube_title(target: TargetSpec) -> tuple[str, bool]:
    """(title, explicit): the ``title`` option, else the first non-empty line of the text (prefill rule)."""
    explicit = _str((target.options or {}).get("title"))
    if explicit:
        return explicit, True
    for line in (target.text or "").splitlines():
        line = line.replace("<", "").replace(">", "").strip()
        if line:
            return line[:YT_TITLE_MAX], False
    return "", False


def youtube_tags_length(tags: list[str]) -> int:
    """YouTube's count: commas between tags count, a tag containing a space counts +2 (06 §4.3)."""
    if not tags:
        return 0
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1


# ===================================================================================== media checks


def _check_shape(network: str, fmt: str, rule: FormatRule, media: list[MediaInfo]) -> list[Issue]:
    issues: list[Issue] = []
    subject = _subject(network, fmt)
    images = [m for m in media if _kind(m) == "image"]
    videos = [m for m in media if _kind(m) == "video"]
    if len(images) + len(videos) != len(media):
        issues.append(Issue("error", "media_kind", "Solo se pueden publicar fotos y vídeos.", "media"))
    if not media:
        if not rule.allow_empty:
            need = {
                "video": "un vídeo",
                "image_or_video": "una foto o un vídeo",
                "images": "al menos una foto",
            }.get(rule.media, "al menos una foto o un vídeo")
            issues.append(Issue("error", "media_required", f"{subject} necesita {need}.", "media"))
        return issues
    if images and rule.image is None:
        issues.append(
            Issue(
                "error",
                "media_video_only",
                f"{network_label(network)} no admite fotos en el formato {format_label(network, fmt)}.",
                "media",
            )
        )
    if videos and rule.video is None:
        issues.append(
            Issue(
                "error",
                "media_image_only",
                f"{network_label(network)} no admite vídeos en el formato {format_label(network, fmt)}.",
                "media",
            )
        )
    if rule.media == "image_or_video" and len(media) > 1:
        issues.append(
            Issue("error", "media_one_item", f"{subject} lleva una sola foto o un solo vídeo.", "media")
        )
    elif rule.media == "video" and len(videos) > 1:
        issues.append(Issue("error", "media_one_video", f"{subject} lleva un solo vídeo.", "media"))
    elif rule.media == "images_or_video":
        if images and videos and not rule.mix:
            issues.append(
                Issue(
                    "error",
                    "media_no_mix",
                    f"{network_label(network)} no permite mezclar fotos y vídeo en una misma publicación.",
                    "media",
                )
            )
        elif len(videos) > 1 and not rule.mix:
            issues.append(Issue("error", "media_one_video", f"{subject} admite un solo vídeo.", "media"))
        if len(media) > rule.max_items:
            what = "archivos" if rule.mix else "fotos"
            issues.append(
                Issue(
                    "error",
                    "media_too_many",
                    f"{subject} admite como máximo {rule.max_items} {what} (hay {len(media)}).",
                    "media",
                )
            )
    elif len(media) > rule.max_items:
        issues.append(
            Issue(
                "error",
                "media_too_many",
                f"{subject} admite como máximo {rule.max_items} archivos (hay {len(media)}).",
                "media",
            )
        )
    return issues


def _container_label(mime: str) -> str:
    sub = (mime.split("/")[-1] or "?").lower()
    return {"quicktime": "MOV", "x-m4v": "M4V", "x-matroska": "MKV", "x-msvideo": "AVI"}.get(sub, sub.upper())


def _check_video(network: str, fmt: str, m: MediaInfo, rule: VideoRule) -> list[Issue]:
    issues: list[Issue] = []
    net = network_label(network)
    subject = _subject(network, fmt, capital=False)
    name = _name(m)
    if rule.mimes and m.mime and m.mime not in rule.mimes:
        issues.append(
            Issue(
                "error",
                "video_container",
                f"«{name}»: {net} no admite vídeos {_container_label(m.mime)} en este formato. "
                "Usa MP4 (H.264).",
                "media",
            )
        )
    codec = (m.video_codec or "").lower()
    if rule.codecs and codec and codec not in rule.codecs:
        issues.append(
            Issue(
                "error",
                "video_codec",
                f"«{name}»: {net} no admite el códec de vídeo {codec.upper()}. Usa H.264.",
                "media",
            )
        )
    duration = m.duration_s
    if duration is None:
        if rule.min_s is not None or rule.max_s is not None:
            issues.append(
                Issue(
                    "warning",
                    "video_duration_unknown",
                    f"«{name}»: no se ha podido leer la duración del vídeo; no se comprueban sus límites.",
                    "media",
                )
            )
    else:
        if rule.min_s is not None and duration < rule.min_s - DURATION_TOLERANCE_S:
            issues.append(
                Issue(
                    rule.min_level,
                    "video_too_short",
                    f"«{name}» dura {fmt_seconds(duration)} y {subject} necesita al menos "
                    f"{fmt_seconds(rule.min_s)}.",
                    "media",
                )
            )
        if rule.max_s is not None and duration > rule.max_s + DURATION_TOLERANCE_S:
            issues.append(
                Issue(
                    rule.max_level,
                    "video_too_long",
                    f"«{name}» dura {fmt_seconds(duration)} y {subject} admite como máximo "
                    f"{fmt_seconds(rule.max_s)}.",
                    "media",
                )
            )
        elif rule.soft_max_s is not None and duration > rule.soft_max_s + DURATION_TOLERANCE_S:
            issues.append(
                Issue(
                    "warning",
                    "video_long",
                    f"«{name}» dura {fmt_seconds(duration)}: {net} podría rechazar vídeos de más de "
                    f"{fmt_seconds(rule.soft_max_s)}.",
                    "media",
                )
            )
    size = int(m.size_bytes or 0)
    if rule.max_bytes is not None and size > rule.max_bytes:
        issues.append(
            Issue(
                rule.max_bytes_level,
                "video_too_large",
                f"«{name}» ocupa {_fmt_bytes(size)} y {subject} admite como máximo "
                f"{_fmt_bytes(rule.max_bytes)}.",
                "media",
            )
        )
    elif rule.soft_max_bytes is not None and size > rule.soft_max_bytes:
        issues.append(
            Issue(
                "warning",
                "video_large",
                f"«{name}» ocupa {_fmt_bytes(size)}: {net} podría rechazar archivos tan grandes.",
                "media",
            )
        )
    w, h = m.width, m.height
    if w and h:
        if rule.min_side is not None and min(w, h) < rule.min_side:
            issues.append(
                Issue(
                    rule.min_side_level,
                    "video_resolution_low",
                    f"«{name}» mide {w}×{h} px: {subject} necesita al menos {rule.min_side} px por lado.",
                    "media",
                )
            )
        if rule.max_side is not None and max(w, h) > rule.max_side:
            issues.append(
                Issue(
                    "error",
                    "video_resolution_high",
                    f"«{name}» mide {w}×{h} px: {net} admite como máximo {rule.max_side} px por lado.",
                    "media",
                )
            )
        elif rule.max_width is not None and w > rule.max_width:
            issues.append(
                Issue(
                    "warning",
                    "video_too_wide",
                    f"«{name}» mide {w}×{h} px: {net} recomienda como máximo {rule.max_width} px de ancho.",
                    "media",
                )
            )
        if rule.orientation == "portrait_or_square" and h < w:
            issues.append(
                Issue(
                    "error",
                    "video_not_vertical",
                    f"«{name}» es horizontal ({fmt_ratio(w, h)}): {subject} debe ser vertical o cuadrado.",
                    "media",
                )
            )
        elif rule.orientation == "vertical_recommended" and w / h > 0.75:
            issues.append(
                Issue(
                    "warning",
                    "video_not_vertical",
                    f"«{name}» es {fmt_ratio(w, h)}: {subject} se ve mejor en vertical 9:16 (1080×1920).",
                    "media",
                )
            )
    if rule.fps is not None and m.fps:
        low, high = rule.fps
        if m.fps < low - 0.5 or m.fps > high + 0.5:
            issues.append(
                Issue(
                    rule.fps_level,
                    "video_fps",
                    f"«{name}» tiene {m.fps:.0f} fotogramas por segundo y {net} admite entre {low:.0f} y "
                    f"{high:.0f}.",
                    "media",
                )
            )
    if rule.max_bitrate is not None and m.bitrate and m.bitrate > rule.max_bitrate:
        issues.append(
            Issue(
                "warning",
                "video_bitrate",
                f"«{name}» tiene una tasa de bits de {m.bitrate / 1_000_000:.0f} Mbps: {net} recomienda como "
                f"máximo {rule.max_bitrate / 1_000_000:.0f} Mbps.",
                "media",
            )
        )
    if network != "demo":
        if codec in ("hevc", "h265"):
            issues.append(
                Issue(
                    "warning",
                    "video_hevc",
                    f"«{name}» usa HEVC (H.265): {net} puede rechazarlo o tardar más en procesarlo. H.264 es "
                    "lo más compatible.",
                    "media",
                )
            )
        if m.is_hdr:
            issues.append(
                Issue(
                    "warning",
                    "video_hdr",
                    f"«{name}» es HDR: {net} puede rechazarlo o mostrar los colores apagados.",
                    "media",
                )
            )
    return issues


def _check_image(network: str, fmt: str, m: MediaInfo, rule: ImageRule) -> list[Issue]:
    issues: list[Issue] = []
    net = network_label(network)
    name = _name(m)
    w, h = m.width, m.height
    if w and h:
        ratio = w / h
        if rule.aspect is not None:
            low, high = rule.aspect
            if ratio < low - 0.01 or ratio > high + 0.01:
                issues.append(
                    Issue(
                        "warning",
                        "image_aspect",
                        f"«{name}» es {fmt_ratio(w, h)}: {net} acepta en el feed fotos entre 4:5 (vertical) "
                        "y 1,91:1 (horizontal). Recórtala o añádele bordes antes de publicar.",
                        "media",
                    )
                )
        if rule.vertical_recommended and ratio > 0.75:
            issues.append(
                Issue(
                    "warning",
                    "image_not_vertical",
                    f"«{name}» es {fmt_ratio(w, h)}: {_subject(network, fmt, capital=False)} se ve mejor en "
                    "vertical 9:16.",
                    "media",
                )
            )
        if rule.min_width is not None and w < rule.min_width:
            issues.append(
                Issue(
                    "warning",
                    "image_small",
                    f"«{name}» mide {w}×{h} px: {net} la ampliará a {rule.min_width} px de ancho y puede "
                    "verse borrosa.",
                    "media",
                )
            )
    if m.mime == "image/gif" and network in ("facebook", "instagram"):
        issues.append(
            Issue(
                "warning",
                "image_gif",
                f"«{name}» es un GIF: en {net} se publicará solo el primer fotograma, como imagen fija.",
                "media",
            )
        )
    return issues


def _check_media(network: str, fmt: str, rule: FormatRule, media: list[MediaInfo]) -> list[Issue]:
    issues = _check_shape(network, fmt, rule, media)
    single = len(media) == 1
    for m in media:
        kind = _kind(m)
        item: list[Issue] = []
        if kind == "video" and rule.video is not None:
            video_rule = rule.video if (single or rule.multi_video is None) else rule.multi_video
            item = _check_video(network, fmt, m, video_rule)
        elif kind == "image" and rule.image is not None:
            item = _check_image(network, fmt, m, rule.image)
        if (
            network != "demo"
            and int(m.size_bytes or 0) > LARGE_FILE_BYTES
            and not any(i.code in ("video_too_large", "video_large") for i in item)
        ):
            item.append(
                Issue(
                    "warning",
                    "file_very_large",
                    f"«{_name(m)}» ocupa {_fmt_bytes(m.size_bytes)}: la subida a {network_label(network)} "
                    "puede tardar bastante.",
                    "media",
                )
            )
        issues.extend(item)
    return issues


# ====================================================================================== text checks


def _check_text(network: str, fmt: str, rule: FormatRule, target: TargetSpec) -> list[Issue]:
    issues: list[Issue] = []
    text = target.text or ""
    net = network_label(network)
    length = text_length(network, text)
    if length > rule.text_max:
        if char_unit(network) == "bytes":
            message = f"El texto para {net} ocupa {length} bytes y el máximo es {rule.text_max}."
        else:
            message = f"El texto para {net} tiene {length} caracteres y el máximo es {rule.text_max}."
        issues.append(Issue("error", "text_too_long", message, "text"))
    stripped = text.strip()
    if not rule.text_shown:
        if stripped:
            issues.append(
                Issue(
                    "warning",
                    "text_ignored",
                    f"Las historias de {net} no muestran texto: el texto no se publicará.",
                    "text",
                )
            )
        return issues
    if network == "instagram":
        tags = count_hashtags(text)
        if tags > IG_MAX_HASHTAGS:
            issues.append(
                Issue(
                    "error",
                    "ig_too_many_hashtags",
                    f"Instagram admite como máximo {IG_MAX_HASHTAGS} hashtags (hay {tags}).",
                    "text",
                )
            )
        mentions = count_mentions(text)
        if mentions > IG_MAX_MENTIONS:
            issues.append(
                Issue(
                    "error",
                    "ig_too_many_mentions",
                    f"Instagram admite como máximo {IG_MAX_MENTIONS} menciones (hay {mentions}).",
                    "text",
                )
            )
    if network == "youtube" and ("<" in text or ">" in text):
        issues.append(
            Issue(
                "error",
                "youtube_description_chars",
                "La descripción de YouTube no puede contener < ni >.",
                "text",
            )
        )
    inbox = network == "tiktok" and _tiktok_mode(target.options) == "inbox"
    if network == "tiktok" and "\n" in stripped and not inbox:
        issues.append(
            Issue(
                "warning",
                "tiktok_newlines",
                "TikTok puede juntar los saltos de línea del texto en una sola línea.",
                "text",
            )
        )
    if not stripped and network != "demo":
        link = _str((target.options or {}).get("link"))
        if network == "facebook" and fmt == "post" and not target.media and not link:
            issues.append(
                Issue(
                    "error",
                    "post_empty",
                    "La publicación de Facebook está vacía: escribe un texto o añade fotos o un vídeo.",
                    "text",
                )
            )
        elif not inbox:
            what = "descripción" if network == "youtube" else "texto"
            issues.append(
                Issue("warning", "text_missing", f"{_subject(network, fmt)} no tiene {what}.", "text")
            )
    return issues


# =================================================================================== option checks


def _facebook_checks(fmt: str, target: TargetSpec, context: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    opts = target.options or {}
    link = _str(opts.get("link"))
    if link:
        if fmt != "post":
            issues.append(
                Issue(
                    "warning",
                    "fb_link_ignored",
                    "El enlace solo se usa en publicaciones; se ignorará.",
                    "link",
                )
            )
        elif target.media:
            issues.append(
                Issue(
                    "error",
                    "fb_link_with_media",
                    "El enlace solo se puede añadir a publicaciones de Facebook sin fotos ni vídeo.",
                    "link",
                )
            )
        elif not _URL_RE.match(link):
            issues.append(
                Issue("error", "fb_link_invalid", "El enlace debe empezar por http:// o https://.", "link")
            )
    title = _str(opts.get("title"))
    if title and len(title) > FB_TITLE_MAX:
        issues.append(
            Issue(
                "error",
                "fb_title_too_long",
                f"El título del vídeo admite como máximo {FB_TITLE_MAX} caracteres.",
                "title",
            )
        )
    if _as_bool(opts.get("native_schedule"), False):
        if fmt != "post":
            issues.append(
                Issue(
                    "warning",
                    "fb_native_format",
                    "«Programar en Facebook» solo funciona con publicaciones; este formato se publicará "
                    "desde "
                    "la app.",
                    "native_schedule",
                )
            )
        elif target.scheduled_at is None:
            issues.append(
                Issue(
                    "warning",
                    "fb_native_no_date",
                    "«Programar en Facebook» necesita una fecha de publicación; sin ella se publica al "
                    "momento.",
                    "native_schedule",
                )
            )
        else:
            delta = target.scheduled_at - context.current_time()
            low, high = FB_NATIVE_WINDOW
            if delta < low:
                issues.append(
                    Issue(
                        "warning",
                        "fb_native_too_soon",
                        "Faltan menos de 15 minutos: Facebook no acepta programarla, así que se publicará "
                        "desde la app a la hora indicada.",
                        "native_schedule",
                    )
                )
            elif delta > high:
                issues.append(
                    Issue(
                        "warning",
                        "fb_native_too_far",
                        "Facebook solo programa hasta 29 días vista: se le enviará cuando falten 29 días "
                        "(la app tiene que estar abierta ese día).",
                        "native_schedule",
                    )
                )
    return issues


def _instagram_checks(fmt: str, target: TargetSpec, context: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    opts = target.options or {}
    collaborators = [c.lstrip("@") for c in _list(opts.get("collaborators"))]
    if collaborators:
        if fmt == "story":
            issues.append(
                Issue(
                    "warning",
                    "ig_collaborators_story",
                    "Las historias de Instagram no admiten colaboradores; se ignorarán.",
                    "collaborators",
                )
            )
        else:
            if len(collaborators) > IG_COLLABORATORS_MAX:
                issues.append(
                    Issue(
                        "error",
                        "ig_too_many_collaborators",
                        f"Instagram admite como máximo {IG_COLLABORATORS_MAX} colaboradores.",
                        "collaborators",
                    )
                )
            bad = [c for c in collaborators if not _IG_USERNAME_RE.match(c)]
            if bad:
                issues.append(
                    Issue(
                        "error",
                        "ig_collaborator_invalid",
                        f"Nombre de usuario de Instagram no válido: «{bad[0]}».",
                        "collaborators",
                    )
                )
    if _str(opts.get("first_comment")) and fmt == "story":
        issues.append(
            Issue(
                "warning",
                "ig_first_comment_story",
                "Las historias no admiten primer comentario; se ignorará.",
                "first_comment",
            )
        )
    trial = _str(opts.get("trial_reel"))
    if trial and trial.lower() not in ("none", "null", "no"):
        if trial not in IG_TRIAL_STRATEGIES:
            issues.append(
                Issue("error", "ig_trial_invalid", "Opción de reel de prueba no válida.", "trial_reel")
            )
        elif fmt != "reel":
            issues.append(
                Issue(
                    "warning",
                    "ig_trial_ignored",
                    "El reel de prueba solo se aplica al formato Reel; se ignorará.",
                    "trial_reel",
                )
            )
    offset = opts.get("thumb_offset_ms")
    if offset not in (None, ""):
        value = _int(offset)
        if value is None or value < 0:
            issues.append(
                Issue(
                    "error",
                    "ig_thumb_offset",
                    "La portada debe ser un momento válido del vídeo.",
                    "thumb_offset_ms",
                )
            )
        else:
            videos = [m for m in target.media if _kind(m) == "video"]
            if videos and videos[0].duration_s and value > videos[0].duration_s * 1000:
                issues.append(
                    Issue(
                        "warning",
                        "ig_thumb_offset_beyond",
                        "El fotograma de portada está después del final del vídeo; se usará el último.",
                        "thumb_offset_ms",
                    )
                )
    if fmt == "story":
        account_type = (context.account_type or "").lower()
        if account_type and account_type not in ("business", "empresa"):
            issues.append(
                Issue(
                    "error",
                    "ig_story_business_only",
                    "Las historias de Instagram solo se pueden publicar desde cuentas de empresa (no de "
                    "creador).",
                    "format",
                )
            )
    if any(_kind(m) == "image" for m in target.media) and _public_media_mode(context) == "off":
        issues.append(Issue("error", "public_media_off", MSG_PUBLIC_MEDIA_OFF, "media"))
    return issues


def _tiktok_mode(options: Mapping[str, Any] | None) -> str:
    mode = str((options or {}).get("mode") or "direct").strip().lower()
    return "inbox" if mode == "inbox" else "direct"


def _creator_info(context: RuleContext) -> Mapping[str, Any] | None:
    info = (context.account_meta or {}).get("creator_info")
    return info if isinstance(info, Mapping) else None


def _tiktok_duration_checks(video: MediaInfo | None, info: Mapping[str, Any] | None) -> list[Issue]:
    """Creator-specific maximum duration (03 §4.1): known → exact; unknown → 180 s is safe for everyone."""
    if video is None or video.duration_s is None or video.duration_s > TT_ABSOLUTE_MAX_DURATION_S:
        return []  # the absolute maximum is reported by the format rule
    limit = _int((info or {}).get("max_video_post_duration_sec"))
    if limit:
        if video.duration_s > limit + DURATION_TOLERANCE_S:
            return [
                Issue(
                    "error",
                    "tiktok_too_long",
                    f"El vídeo dura {fmt_seconds(video.duration_s)} y esta cuenta de TikTok permite como "
                    f"máximo {fmt_seconds(limit)}.",
                    "media",
                )
            ]
        return []
    if video.duration_s > TT_DEFAULT_MAX_DURATION_S + DURATION_TOLERANCE_S:
        return [
            Issue(
                "warning",
                "tiktok_duration_unverified",
                f"El vídeo dura {fmt_seconds(video.duration_s)}: todas las cuentas de TikTok admiten 3 min, "
                "pero no todas admiten más. Se comprobará al publicar.",
                "media",
            )
        ]
    return []


def _tiktok_checks(fmt: str, target: TargetSpec, context: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    opts = target.options or {}
    raw_mode = _str(opts.get("mode"))
    if raw_mode and raw_mode.lower() not in TT_MODES:
        issues.append(
            Issue("error", "tiktok_mode_invalid", "Modo de publicación de TikTok no válido.", "mode")
        )
    mode = _tiktok_mode(opts)
    info = _creator_info(context)
    videos = [m for m in target.media if _kind(m) == "video"]
    issues.extend(_tiktok_duration_checks(videos[0] if len(videos) == 1 else None, info))

    if mode == "inbox":
        issues.append(
            Issue(
                "warning",
                "tiktok_inbox_text",
                "En «Terminar en la app de TikTok» el texto no se envía: cópialo y pégalo en la app al "
                "terminar.",
                "mode",
            )
        )
        return issues

    privacy = _str(opts.get("privacy_level"))
    options_list = [str(o) for o in ((info or {}).get("privacy_level_options") or [])]
    if privacy is None:
        issues.append(
            Issue(
                "error",
                "tiktok_privacy_required",
                "Elige quién puede ver la publicación en TikTok (no hay valor por defecto).",
                "privacy_level",
            )
        )
    elif privacy not in TT_PRIVACY_LEVELS:
        issues.append(
            Issue(
                "error",
                "tiktok_privacy_invalid",
                "La visibilidad elegida para TikTok no es válida.",
                "privacy_level",
            )
        )
    elif options_list and privacy not in options_list:
        allowed = ", ".join(f"«{TT_PRIVACY_LABELS.get(o, o)}»" for o in options_list)
        issues.append(
            Issue(
                "error",
                "tiktok_privacy_not_allowed",
                f"La visibilidad «{TT_PRIVACY_LABELS[privacy]}» no está disponible para esta cuenta de "
                f"TikTok "
                f"(opciones: {allowed}).",
                "privacy_level",
            )
        )
    if options_list == ["SELF_ONLY"]:
        issues.append(
            Issue(
                "warning",
                "tiktok_unaudited",
                "Tu app de TikTok no está auditada: TikTok solo permite publicar como «Solo yo». Cambia la "
                "visibilidad después en la app de TikTok, o usa «Terminar en la app de TikTok».",
                "privacy_level",
            )
        )
    if _as_bool(opts.get("brand_content_toggle"), False) and privacy == "SELF_ONLY":
        issues.append(
            Issue(
                "error",
                "tiktok_branded_private",
                "El contenido de marca no se puede publicar como «Solo yo». Elige otra visibilidad o "
                "desactiva «Contenido de marca».",
                "brand_content_toggle",
            )
        )
    consent = opts.get("consent")
    if consent is not None and not _as_bool(consent, False):
        issues.append(
            Issue(
                "error",
                "tiktok_consent",
                "Para publicar directamente en TikTok tienes que aceptar la Confirmación de uso de música de "
                "TikTok.",
                "consent",
            )
        )
    if info:
        for key, flag, label in (
            ("disable_comment", "comment_disabled", "los comentarios"),
            ("disable_duet", "duet_disabled", "el dúo"),
            ("disable_stitch", "stitch_disabled", "pegar (Stitch)"),
        ):
            if info.get(flag) and _as_bool(opts.get(key), True) is False:
                issues.append(
                    Issue(
                        "warning",
                        f"tiktok_{flag}",
                        f"Tienes desactivado {label} en la app de TikTok; no se puede activar desde aquí.",
                        key,
                    )
                )
    cover = opts.get("video_cover_timestamp_ms")
    if cover not in (None, ""):
        value = _int(cover)
        if value is None or value < 0:
            issues.append(
                Issue(
                    "error",
                    "tiktok_cover_invalid",
                    "El fotograma de portada debe ser un momento válido del vídeo.",
                    "video_cover_timestamp_ms",
                )
            )
    return issues


def _youtube_duration_checks(fmt: str, video: MediaInfo | None, context: RuleContext) -> list[Issue]:
    if video is None or video.duration_s is None:
        return []
    issues: list[Issue] = []
    d = video.duration_s
    if fmt == "video" and YT_LONG_UPLOAD_S + DURATION_TOLERANCE_S < d <= YT_VIDEO_MAX_S:
        status = (context.account_meta or {}).get("long_uploads_status")
        if status and status != "allowed":
            issues.append(
                Issue(
                    "error",
                    "youtube_long_upload",
                    "Tu canal solo permite vídeos de hasta 15 min; verifica tu canal en youtube.com/verify.",
                    "media",
                )
            )
        elif not status:
            issues.append(
                Issue(
                    "warning",
                    "youtube_long_upload_unknown",
                    "Para subir vídeos de más de 15 min tu canal debe estar verificado (youtube.com/verify).",
                    "media",
                )
            )
    if (
        fmt == "video"
        and d <= YT_SHORT_MAX_S + DURATION_TOLERANCE_S
        and video.width
        and video.height
        and video.height >= video.width
    ):
        issues.append(
            Issue(
                "warning",
                "youtube_will_be_short",
                "El vídeo es vertical o cuadrado y dura 3 min o menos: YouTube lo mostrará como Short.",
                "format",
            )
        )
    if (
        fmt == "short"
        and YT_SHORT_CONTENT_ID_S + DURATION_TOLERANCE_S < d <= YT_SHORT_MAX_S + DURATION_TOLERANCE_S
    ):
        issues.append(
            Issue(
                "warning",
                "youtube_short_content_id",
                "Los Shorts de más de 1 min con música reclamada (Content ID) se bloquean en todo el mundo.",
                "media",
            )
        )
    return issues


def _youtube_checks(fmt: str, target: TargetSpec, context: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    opts = target.options or {}
    title, explicit = youtube_title(target)
    if not title:
        issues.append(
            Issue("error", "youtube_title_required", "Escribe el título del vídeo de YouTube.", "title")
        )
    elif explicit:
        if len(title) > YT_TITLE_MAX:
            issues.append(
                Issue(
                    "error",
                    "youtube_title_too_long",
                    f"El título de YouTube tiene {len(title)} caracteres (máximo {YT_TITLE_MAX}).",
                    "title",
                )
            )
        if "<" in title or ">" in title:
            issues.append(
                Issue(
                    "error", "youtube_title_chars", "El título de YouTube no puede contener < ni >.", "title"
                )
            )
    else:
        issues.append(
            Issue(
                "warning",
                "youtube_title_from_text",
                f"Se usará la primera línea del texto como título: «{title}».",
                "title",
            )
        )
    made_for_kids = _as_bool(opts.get("made_for_kids"), None)
    if made_for_kids is None:
        issues.append(
            Issue(
                "error",
                "youtube_made_for_kids_required",
                "Indica si el vídeo es contenido creado para niños (obligatorio en YouTube).",
                "made_for_kids",
            )
        )
    raw_privacy = (_str(opts.get("privacy_status")) or "").lower()
    privacy = YT_PRIVACY_ALIASES.get(raw_privacy) if raw_privacy else "public"
    if privacy is None:
        issues.append(
            Issue("error", "youtube_privacy", "Elige la visibilidad del vídeo de YouTube.", "privacy_status")
        )
    tags = [t.lstrip("#").strip() for t in _list(opts.get("tags"))]
    tags = [t for t in tags if t]
    total = youtube_tags_length(tags)
    if total > YT_TAGS_MAX:
        issues.append(
            Issue(
                "error",
                "youtube_tags_too_long",
                f"Las etiquetas de YouTube suman {total} caracteres (máximo {YT_TAGS_MAX}).",
                "tags",
            )
        )
    if any("<" in t or ">" in t for t in tags):
        issues.append(
            Issue("error", "youtube_tags_chars", "Las etiquetas no pueden contener < ni >.", "tags")
        )
    category = _str(opts.get("category_id"))
    if category and not category.isdigit():
        issues.append(Issue("error", "youtube_category", "Categoría de YouTube no válida.", "category_id"))
    license_ = _str(opts.get("license"))
    if license_ and license_ not in YT_LICENSES:
        issues.append(Issue("error", "youtube_license", "Licencia de YouTube no válida.", "license"))
    language = _str(opts.get("language"))
    if language and not _LANG_RE.match(language):
        issues.append(
            Issue("error", "youtube_language", "Código de idioma no válido (por ejemplo «es»).", "language")
        )
    if made_for_kids is True and _str(opts.get("first_comment")):
        issues.append(
            Issue(
                "error",
                "youtube_kids_comment",
                "Los vídeos para niños no admiten comentarios: quita el primer comentario.",
                "first_comment",
            )
        )
    if fmt == "short" and opts.get("thumbnail_media_id") not in (None, ""):
        issues.append(
            Issue(
                "warning",
                "youtube_short_thumbnail",
                "La miniatura personalizada puede no verse en el feed de Shorts.",
                "thumbnail_media_id",
            )
        )
    if _as_bool(opts.get("native_schedule"), False):
        if target.scheduled_at is None:
            issues.append(
                Issue(
                    "warning",
                    "youtube_native_no_date",
                    "«Programar en YouTube» necesita una fecha de publicación; sin ella se publica al "
                    "momento.",
                    "native_schedule",
                )
            )
        else:
            if privacy not in (None, "public"):
                issues.append(
                    Issue(
                        "error",
                        "youtube_native_privacy",
                        "«Programar en YouTube» solo funciona con visibilidad Pública.",
                        "native_schedule",
                    )
                )
            if target.scheduled_at - context.current_time() < YT_NATIVE_MIN:
                issues.append(
                    Issue(
                        "warning",
                        "youtube_native_too_soon",
                        "Faltan menos de 15 minutos: se subirá y publicará desde la app a la hora indicada.",
                        "native_schedule",
                    )
                )
    videos = [m for m in target.media if _kind(m) == "video"]
    issues.extend(_youtube_duration_checks(fmt, videos[0] if len(videos) == 1 else None, context))
    return issues


_NETWORK_CHECKS: dict[str, Callable[[str, TargetSpec, RuleContext], list[Issue]]] = {
    "facebook": _facebook_checks,
    "instagram": _instagram_checks,
    "tiktok": _tiktok_checks,
    "youtube": _youtube_checks,
}


# ======================================================================================= validation


def _normalized(target: TargetSpec) -> TargetSpec:
    if isinstance(target.options, dict) and isinstance(target.text, str) and isinstance(target.media, list):
        return target
    return TargetSpec(
        network=target.network,
        format=target.format,
        text=target.text or "",
        media=list(target.media or []),
        options=dict(target.options or {}),
        scheduled_at=target.scheduled_at,
        auto_publish=target.auto_publish,
    )


def validate_target(target: TargetSpec, context: RuleContext | None = None) -> list[Issue]:
    """All issues (errors first, then warnings; Spanish messages) for one target."""
    context = context or RuleContext()
    network = str(target.network)
    fmt = str(target.format or "")
    if network not in FORMATS:
        return [Issue("error", "network_unknown", f"Red social no admitida: {network}.", "network")]
    rule = FORMAT_RULES.get((network, fmt))
    if rule is None or format_spec(network, fmt) is None:
        valid = ", ".join(f.label for f in enabled_formats(network))
        return [
            Issue(
                "error",
                "format_unknown",
                f"Formato no válido para {network_label(network)}. Elige uno de: {valid}.",
                "format",
            )
        ]
    reason = disabled_reason(network, fmt)
    if reason:
        return [Issue("error", f"{network}_{fmt}_unsupported", reason, "format")]
    target = _normalized(target)
    issues: list[Issue] = []
    issues.extend(_check_media(network, fmt, rule, list(target.media)))
    issues.extend(_check_text(network, fmt, rule, target))
    check = _NETWORK_CHECKS.get(network)
    if check is not None:
        try:
            issues.extend(check(fmt, target, context))
        except Exception:  # pragma: no cover - defensive: a rule bug must not break the composer
            log.exception("Rule check failed for %s/%s", network, fmt)
    issues = _dedupe(issues)
    return [i for i in issues if i.level == "error"] + [i for i in issues if i.level != "error"]


# ==================================================================================== compatibility


def _media_issues_for(network: str, fmt: str, info: MediaInfo, context: RuleContext) -> list[Issue]:
    """Issues of using ``info`` alone in ``network``/``fmt`` (media checks only: no text/option rules)."""
    issues = _check_media(network, fmt, FORMAT_RULES[(network, fmt)], [info])
    if _kind(info) == "video":
        if network == "tiktok":
            issues.extend(_tiktok_duration_checks(info, _creator_info(context)))
        elif network == "youtube":
            issues.extend(
                i
                for i in _youtube_duration_checks(fmt, info, context)
                if i.code in ("youtube_long_upload", "youtube_long_upload_unknown")
            )
    elif _kind(info) == "image" and network == "instagram" and context.public_media_mode == "off":
        issues.append(Issue("error", "public_media_off", MSG_PUBLIC_MEDIA_OFF, "media"))
    return _dedupe(issues)


def compat_details(info: MediaInfo, context: RuleContext | None = None) -> dict[str, dict[str, list[Issue]]]:
    """``network -> format -> issues`` for one file (enabled formats of the real networks)."""
    context = context or RuleContext()
    return {
        network: {f.key: _media_issues_for(network, f.key, info, context) for f in enabled_formats(network)}
        for network in REAL_NETWORKS
    }


_KIND_CODES = ("media_video_only", "media_image_only", "media_kind")


def _summarize(network: str, per_format: dict[str, list[Issue]]) -> list[Issue]:
    # Formats that cannot take this kind of file at all (a photo for a Reel) are not worth mentioning.
    usable = {
        fmt: issues for fmt, issues in per_format.items() if not any(i.code in _KIND_CODES for i in issues)
    }
    if not usable:
        kinds = {i.code for issues in per_format.values() for i in issues if i.code in _KIND_CODES}
        what = "vídeos" if "media_video_only" in kinds else "fotos"
        if "media_kind" in kinds:
            return [Issue("error", "kind_unsupported", "Tipo de archivo no admitido.", "media")]
        return [
            Issue(
                "error",
                "kind_unsupported",
                f"{network_label(network)} solo admite {what} en esta versión.",
                "media",
            )
        ]
    ok = [fmt for fmt, issues in usable.items() if not has_errors(issues)]
    if not ok:
        best = min(usable.values(), key=lambda iss: len(errors_only(iss)))
        return errors_only(best)
    issues = [i for i in usable[ok[0]] if i.level == "warning"]
    for fmt, fmt_issues in usable.items():
        if fmt in ok:
            continue
        first = errors_only(fmt_issues)[0]
        issues.append(
            Issue(
                "warning",
                "format_unavailable",
                f"No sirve como {format_label(network, fmt).lower()}: {first.message}",
                "format",
            )
        )
    return _dedupe(issues)


def network_compat(media_info: MediaInfo, context: RuleContext | None = None) -> dict[str, list[Issue]]:
    """Can this single file be published on each real network (facebook, instagram, tiktok, youtube)?

    Formats that never take this kind of file (a photo for a Reel) are ignored. When at least one format
    accepts the file, the issues are the warnings of the first such format plus one ``format_unavailable``
    warning per format whose limits it breaks; when none does, the errors of the closest format (fewest
    errors), or one ``kind_unsupported`` error (a photo for TikTok/YouTube). An empty list = fully compatible.
    """
    details = compat_details(media_info, context)
    return {network: _summarize(network, per_format) for network, per_format in details.items()}


def compat_summary(media_info: MediaInfo, context: RuleContext | None = None) -> dict[str, dict[str, Any]]:
    """JSON-friendly compatibility for the library UI/API:
    ``{network: {"label", "status": ok|warning|error, "formats": {fmt: {"label", "status"}},
    "issues": [...]}}``.
    """
    details = compat_details(media_info, context)
    out: dict[str, dict[str, Any]] = {}
    for network, per_format in details.items():
        issues = _summarize(network, per_format)
        out[network] = {
            "label": network_label(network),
            "status": compat_status(issues),
            "formats": {
                fmt: {"label": format_label(network, fmt), "status": compat_status(iss)}
                for fmt, iss in per_format.items()
            },
            "issues": [issue_dict(i) for i in issues],
        }
    return out


__all__ = [
    "DISABLED_FORMATS",
    "FORMATS",
    "FORMAT_RULES",
    "NETWORKS",
    "REAL_NETWORKS",
    "FormatRule",
    "ImageRule",
    "RuleContext",
    "VideoRule",
    "char_limit",
    "char_unit",
    "compat_details",
    "compat_status",
    "compat_summary",
    "context_for_account",
    "count_hashtags",
    "count_mentions",
    "default_options",
    "disabled_reason",
    "enabled_formats",
    "errors_only",
    "fmt_ratio",
    "fmt_seconds",
    "format_label",
    "format_spec",
    "has_errors",
    "issue_dict",
    "network_compat",
    "text_length",
    "validate_for_account",
    "validate_target",
    "youtube_tags_length",
    "youtube_title",
]

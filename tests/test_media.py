"""Media library backend (SPEC §8): ingest, sniffing, metadata, thumbnails, dedupe, derivatives, usage.

Images are generated with Pillow and videos with PyAV (tiny clips encoded on the fly), fully offline.
``make_image`` / ``make_video`` are reused by the route tests.
"""

from __future__ import annotations

import hashlib
import io
import struct
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planificador import media
from planificador.media import MediaError, MediaInUseError
from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    MediaAsset,
    Network,
    Post,
    PostTarget,
    TargetStatus,
)

# ------------------------------------------------------------------------------------------ generators


def make_image(
    fmt: str = "PNG",
    size: tuple[int, int] = (640, 480),
    color: tuple[int, ...] = (91, 76, 240),
    *,
    mode: str = "RGB",
    orientation: int | None = None,
    noise: bool = False,
) -> bytes:
    """Image bytes. ``orientation`` writes an EXIF Orientation tag; ``noise`` makes it incompressible."""
    if noise:
        img = Image.frombytes("RGB", size, hashlib.shake_256(b"seed").digest(size[0] * size[1] * 3))
    else:
        img = Image.new(mode, size, color)
        if mode == "RGB":
            img.paste((255, 200, 0), (0, 0, size[0] // 2, size[1] // 4))  # asymmetric mark
    buffer = io.BytesIO()
    kwargs: dict[str, Any] = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    img.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def make_gif(frames: int = 3, size: tuple[int, int] = (40, 30)) -> bytes:
    images = [Image.new("P", size, i * 40) for i in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
    return buffer.getvalue()


def make_video(
    path: Path,
    *,
    codec: str = "libx264",
    size: tuple[int, int] = (64, 48),
    seconds: float = 1.0,
    fps: int = 24,
    rotation: int | None = None,
    hdr: bool = False,
    audio: bool = False,
    container: str = "mp4",
    colors: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None,
) -> Path:
    """Encode a tiny test clip. ``rotation`` = display-matrix rotation (counter-clockwise degrees, like
    phones: -90 for a portrait recording); ``hdr`` tags the stream as PQ/BT.2020; ``colors`` = colour of the
    first and second half."""
    import av

    w, h = size
    out = av.open(str(path), "w", format=container)
    options = {"x265-params": "log-level=none"} if codec == "libx265" else {}
    stream = out.add_stream(codec, rate=fps, options=options)
    stream.width, stream.height = w, h
    stream.pix_fmt = "yuv420p"
    if hdr:
        stream.codec_context.color_trc = 16  # SMPTE ST 2084 (PQ)
        stream.codec_context.color_primaries = 9  # BT.2020
        stream.codec_context.colorspace = 9
    if rotation is not None:
        stream.set_display_rotation(rotation)
    astream = out.add_stream("aac", rate=44100) if audio else None
    total = max(1, int(seconds * fps))
    first, second = colors or ((220, 40, 40), (40, 40, 220))
    for i in range(total):
        img = Image.new("RGB", (w, h), first if i < total / 2 else second)
        img.paste((255, 255, 255), (0, 0, w // 4, h // 4))  # top-left marker
        for packet in stream.encode(av.VideoFrame.from_image(img)):
            out.mux(packet)
    for packet in stream.encode():
        out.mux(packet)
    if astream is not None:
        samples = 1024
        for i in range(int(seconds * 44100 / samples)):
            frame = av.AudioFrame(format="fltp", layout="mono", samples=samples)
            frame.planes[0].update(bytes(samples * 4))
            frame.sample_rate = 44100
            frame.pts = i * samples
            for packet in astream.encode(frame):
                out.mux(packet)
        for packet in astream.encode():
            out.mux(packet)
    out.close()
    return path


def make_audio_only(path: Path) -> Path:
    import av

    out = av.open(str(path), "w", format="mp4")
    stream = out.add_stream("aac", rate=44100)
    for i in range(10):
        frame = av.AudioFrame(format="fltp", layout="mono", samples=1024)
        frame.planes[0].update(bytes(1024 * 4))
        frame.sample_rate = 44100
        frame.pts = i * 1024
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode():
        out.mux(packet)
    out.close()
    return path


def ingest_bytes(db: Session, data: bytes, name: str, brand_id: int | None = None) -> MediaAsset:
    return media.ingest(db, io.BytesIO(data), name, brand_id)


@pytest.fixture
def videos(tmp_path: Path) -> Path:
    directory = tmp_path / "videos"
    directory.mkdir()
    return directory


def tmp_leftovers(app: Any) -> list[Path]:
    tmp = Path(app.state.paths.tmp_dir)
    return [p for p in tmp.iterdir()] if tmp.exists() else []


# ---------------------------------------------------------------------------------------------- images


def test_ingest_png_stores_by_hash_with_thumbnail(app, db: Session, brand: Brand) -> None:
    data = make_image("PNG", (1200, 900))
    asset = ingest_bytes(db, data, "Portada verano.png", brand.id)
    sha = hashlib.sha256(data).hexdigest()
    assert asset.id and asset.sha256 == sha
    assert asset.kind == "image" and asset.mime == "image/png"
    assert (asset.width, asset.height, asset.rotation) == (1200, 900, 0)
    assert asset.size_bytes == len(data)
    assert asset.rel_path == f"originals/{sha[:2]}/{sha}.png"
    assert asset.original_name == "Portada verano.png" and asset.brand_id == brand.id
    path = media.asset_path(asset)
    assert path.read_bytes() == data
    assert path.is_relative_to(Path(app.state.paths.media_dir))
    thumb = media.thumb_path(asset)
    assert thumb is not None and thumb.name == f"{sha}.jpg"
    with Image.open(thumb) as t:
        assert t.format == "JPEG" and max(t.size) == 480 and t.size == (480, 360)
    assert tmp_leftovers(app) == []


def test_jpeg_exif_orientation_swaps_dimensions(app, db: Session) -> None:
    data = make_image("JPEG", (400, 300), orientation=6)
    asset = ingest_bytes(db, data, "movil.jpg")
    assert (asset.width, asset.height, asset.rotation) == (300, 400, 90)
    with Image.open(media.thumb_path(asset)) as t:
        assert t.size[0] < t.size[1]  # thumbnail displayed upright (portrait)


def test_gif_first_frame_thumbnail_and_webp(app, db: Session) -> None:
    gif = ingest_bytes(db, make_gif(), "anim.gif")
    assert gif.kind == "image" and gif.mime == "image/gif" and (gif.width, gif.height) == (40, 30)
    assert media.thumb_path(gif) is not None
    webp = ingest_bytes(db, make_image("WEBP", (50, 50)), "x.webp")
    assert webp.mime == "image/webp" and webp.rel_path.endswith(".webp")


def test_type_is_sniffed_not_taken_from_the_name(app, db: Session) -> None:
    asset = ingest_bytes(db, make_image("PNG", (10, 10)), "engañoso.mp4")
    assert asset.kind == "image" and asset.mime == "image/png" and asset.rel_path.endswith(".png")
    with pytest.raises(MediaError) as exc:
        ingest_bytes(db, b"esto no es una imagen, solo texto" * 10, "foto.jpg")
    assert exc.value.code == "unsupported" and exc.value.status == 415
    assert "JPEG, PNG, WebP, GIF, HEIC, MP4, MOV, M4V o WebM" in exc.value.message
    assert tmp_leftovers(app) == []


@pytest.mark.parametrize(
    ("head", "code"),
    [
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", "heic_unavailable"),
        (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf", "unsupported"),
        (b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00M4A mp42isom", "unsupported"),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x88matroska", "unsupported"),
    ],
)
def test_sniff_rejects_types_outside_the_allow_list(head: bytes, code: str, monkeypatch) -> None:
    monkeypatch.setattr(media, "_heif_available", lambda: False)
    with pytest.raises(MediaError) as exc:
        media.sniff(head + bytes(64))
    assert exc.value.code == code


def test_sniff_known_signatures() -> None:
    assert media.sniff(b"\xff\xd8\xff\xe0" + bytes(60)).mime == "image/jpeg"
    assert media.sniff(b"\x00\x00\x00\x14ftypqt  " + bytes(52)).mime == "video/quicktime"
    assert media.sniff(b"\x00\x00\x00\x14ftypM4V " + bytes(52)).mime == "video/x-m4v"
    assert media.sniff(b"\x00\x00\x00\x14ftypisom" + bytes(52)).mime == "video/mp4"
    assert media.sniff(b"\x00\x00\x00\x08wide" + bytes(56)).mime == "video/quicktime"
    assert media.sniff(b"\x1a\x45\xdf\xa3\x01\x42\x82\x84webm" + bytes(50)).mime == "video/webm"


def test_empty_and_corrupt_files(app, db: Session) -> None:
    with pytest.raises(MediaError) as exc:
        ingest_bytes(db, b"", "vacio.png")
    assert exc.value.code == "empty"
    truncated = make_image("PNG", (300, 300), noise=True)[:2000]
    with pytest.raises(MediaError) as exc:
        ingest_bytes(db, truncated, "rota.png")
    assert exc.value.code == "corrupt" and exc.value.status == 422
    assert db.scalar(select(func.count()).select_from(MediaAsset)) == 0
    assert tmp_leftovers(app) == []


def test_upload_limit_is_enforced_while_streaming(app, db: Session, monkeypatch) -> None:
    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", 1500)
    with pytest.raises(MediaError) as exc:
        ingest_bytes(db, make_image("PNG", (300, 300), noise=True), "grande.png")
    assert exc.value.code == "too_large" and exc.value.status == 413
    assert tmp_leftovers(app) == []
    incoming = media.IncomingFile(max_bytes=10)
    with pytest.raises(MediaError):
        incoming.write(b"x" * 11)
    assert not incoming.path.exists()


def test_ingest_reads_in_bounded_chunks(app, db: Session) -> None:
    data = make_image("PNG", (900, 900), noise=True)

    class Tracking(io.BytesIO):
        sizes: list[int] = []

        def read(self, size: int | None = -1) -> bytes:  # type: ignore[override]
            self.sizes.append(size if size is not None else -1)
            return super().read(size)

    stream = Tracking(data)
    media.ingest(db, stream, "ruido.png", None)
    assert stream.sizes and all(0 < s <= media.CHUNK_SIZE for s in stream.sizes)


def test_clean_name() -> None:
    assert media.clean_name("C:\\Users\\ana\\Fotos\\playa.jpg") == "playa.jpg"
    assert media.clean_name("../../etc/passwd") == "passwd"
    assert media.clean_name("ho\x00la\n.png") == "hola.png"
    assert media.clean_name("", ".mp4") == "archivo.mp4"
    long = media.clean_name("a" * 300 + ".jpeg")
    assert len(long) <= 200 and long.endswith(".jpeg")


# ----------------------------------------------------------------------------------------------- dedupe


def test_dedupe_returns_existing_asset(app, db: Session, brand: Brand) -> None:
    data = make_image("JPEG", (320, 240))
    first = ingest_bytes(db, data, "uno.jpg", brand.id)
    second = ingest_bytes(db, data, "dos.jpg", brand.id)
    assert second.id == first.id and second.original_name == "uno.jpg"
    assert db.scalar(select(func.count()).select_from(MediaAsset)) == 1
    originals = list(Path(app.state.paths.originals_dir).rglob("*.jpg"))
    assert len(originals) == 1
    assert tmp_leftovers(app) == []


def test_dedupe_restores_a_deleted_asset(app, db: Session, brand: Brand) -> None:
    data = make_image("PNG", (30, 30))
    asset = ingest_bytes(db, data, "viejo.png", brand.id)
    media.delete_asset(db, asset.id)
    assert asset.deleted_at is not None
    again = ingest_bytes(db, data, "nuevo.png", brand.id)
    assert again.id == asset.id and again.deleted_at is None and again.original_name == "nuevo.png"


def test_same_file_in_two_brands_becomes_shared(app, db: Session, brand: Brand) -> None:
    other = Brand(name="Otra", color="#e8590c", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    data = make_image("PNG", (20, 20))
    asset = ingest_bytes(db, data, "logo.png", brand.id)
    again = ingest_bytes(db, data, "logo.png", other.id)
    assert again.id == asset.id and again.brand_id is None
    assert media.get_visible(db, asset.id, other.id) is not None


def test_missing_original_is_restored_on_reupload(app, db: Session) -> None:
    data = make_image("PNG", (25, 25))
    asset = ingest_bytes(db, data, "a.png")
    media.asset_path(asset).unlink()
    again = ingest_bytes(db, data, "a.png")
    assert again.id == asset.id and media.asset_path(again).read_bytes() == data


# ----------------------------------------------------------------------------------------------- videos


def test_h264_video_metadata_rotation_and_poster(app, db: Session, videos: Path) -> None:
    path = make_video(videos / "movil.mp4", size=(64, 48), seconds=1.0, fps=24, rotation=-90, audio=True)
    asset = media.ingest_path(db, path)
    assert asset.kind == "video" and asset.mime == "video/mp4" and asset.rel_path.endswith(".mp4")
    assert asset.video_codec == "h264" and asset.audio_codec == "aac"
    assert asset.rotation == 90
    assert (asset.width, asset.height) == (48, 64)  # displayed size (portrait)
    assert asset.duration_s == pytest.approx(1.0, abs=0.1)
    assert asset.fps == pytest.approx(24.0)
    assert asset.bitrate and asset.bitrate > 0
    assert asset.is_hdr is False
    assert asset.original_name == "movil.mp4"
    with Image.open(media.thumb_path(asset)) as thumb:
        assert thumb.size[0] < thumb.size[1]  # poster rotated upright


def test_hevc_hdr_is_detected(app, db: Session, videos: Path) -> None:
    path = make_video(videos / "hdr.mp4", codec="libx265", hdr=True)
    asset = media.ingest_path(db, path)
    assert asset.video_codec == "hevc" and asset.is_hdr is True
    from planificador import rules

    info = media.to_media_info(asset)
    compat = rules.network_compat(info, rules.RuleContext(public_media_mode="tunnel"))
    assert {"video_hevc", "video_hdr"} <= {i.code for i in compat["facebook"]}


def test_webm_and_mov_containers(app, db: Session, videos: Path) -> None:
    webm = media.ingest_path(db, make_video(videos / "c.webm", codec="libvpx", container="webm"))
    assert webm.mime == "video/webm" and webm.video_codec == "vp8" and webm.duration_s
    mov = media.ingest_path(db, make_video(videos / "c.mov", container="mov"))
    assert mov.mime == "video/quicktime" and mov.rel_path.endswith(".mov")


def test_audio_only_file_is_rejected(app, db: Session, videos: Path) -> None:
    with pytest.raises(MediaError) as exc:
        media.ingest_path(db, make_audio_only(videos / "voz.mp4"))
    assert exc.value.code in ("no_video", "unsupported")
    assert tmp_leftovers(app) == []


def test_poster_frame_is_taken_at_min_1s_or_10_percent(app, db: Session, videos: Path) -> None:
    path = make_video(videos / "dos.mp4", size=(64, 64), seconds=4.0, fps=10)
    early = media.video_frame(path)  # min(1 s, 0.4 s) = 0.4 s → first half (red)
    late = media.video_frame(path, at=3.0)  # second half (blue)
    r, g, b = early.getpixel((48, 48))
    assert r > 150 and b < 100
    r, g, b = late.getpixel((48, 48))
    assert b > 150 and r < 100


def test_display_matrix_parsing() -> None:
    def matrix(a: float, b: float, c: float, d: float) -> bytes:
        fp = [int(a * 65536), int(b * 65536), 0, int(c * 65536), int(d * 65536), 0, 0, 0, 1 << 30]
        return struct.pack("<9i", *fp)

    assert media._display_matrix_rotation(matrix(0, -1, 1, 0)) == 90
    assert media._display_matrix_rotation(matrix(0, 1, -1, 0)) == -90
    assert media._display_matrix_rotation(matrix(-1, 0, 0, -1)) in (180, -180)
    assert media._display_matrix_rotation(matrix(1, 0, 0, 1)) == 0
    assert media._display_matrix_rotation(b"short") is None


# ----------------------------------------------------------------------------------------------- derived


def test_derived_jpeg_respects_max_bytes_and_is_cached(app, db: Session) -> None:
    asset = ingest_bytes(db, make_image("PNG", (1500, 1500), noise=True), "ruido.png")
    assert asset.size_bytes > 2_000_000
    path = media.derived_jpeg(asset, max_bytes=200_000)
    assert path.stat().st_size <= 200_000
    assert path.parent == Path(app.state.paths.derived_dir)
    with Image.open(path) as img:
        assert img.format == "JPEG" and img.mode == "RGB"
        assert not img.info.get("exif")
    mtime = path.stat().st_mtime_ns
    assert media.derived_jpeg(asset, max_bytes=200_000) == path
    assert path.stat().st_mtime_ns == mtime
    assert media.derived_jpeg(asset, max_bytes=8_000_000) != path  # cached per size


def test_derived_jpeg_flattens_alpha_and_applies_orientation(app, db: Session) -> None:
    rgba = ingest_bytes(db, make_image("PNG", (40, 40), (0, 0, 0, 0), mode="RGBA"), "transparente.png")
    with Image.open(media.derived_jpeg(rgba)) as img:
        assert img.getpixel((20, 20)) == pytest.approx((255, 255, 255), abs=3)
    rotated = ingest_bytes(db, make_image("JPEG", (400, 300), orientation=6), "girada.jpg")
    with Image.open(media.derived_jpeg(rotated)) as img:
        assert img.size == (300, 400)


def test_derived_jpeg_converts_icc_profile(app, db: Session) -> None:
    from PIL import ImageCms

    img = Image.new("RGB", (32, 32), (10, 200, 30))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", icc_profile=profile)
    asset = ingest_bytes(db, buffer.getvalue(), "perfil.jpg")
    with Image.open(media.derived_jpeg(asset)) as out:
        assert out.getpixel((16, 16)) == pytest.approx((10, 200, 30), abs=6)


def test_derived_jpeg_rejects_videos(app, db: Session, videos: Path) -> None:
    asset = media.ingest_path(db, make_video(videos / "v.mp4"))
    with pytest.raises(MediaError):
        media.derived_jpeg(asset)


# ----------------------------------------------------------------------------------------- media info


def test_to_media_info_and_paths(app, db: Session, videos: Path) -> None:
    asset = media.ingest_path(db, make_video(videos / "info.mp4", audio=True))
    info = media.to_media_info(asset)
    assert info.id == asset.id and info.kind == "video" and info.mime == "video/mp4"
    assert info.path == media.asset_path(asset) and info.path.is_file()
    assert info.duration_s == asset.duration_s and info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.original_name == "info.mp4" and info.is_hdr is False
    thumb = media.thumb_path(asset)
    thumb.unlink()
    assert media.thumb_path(asset) is None
    assert media.ensure_thumbnail(asset) == thumb and thumb.is_file()


def test_asset_path_never_escapes_media_dir(app, db: Session) -> None:
    asset = ingest_bytes(db, make_image("PNG", (5, 5)), "a.png")
    asset.rel_path = "../../secret.key"
    with pytest.raises(MediaError):
        media.asset_path(asset)
    asset.thumb_rel_path = "../planificador.sqlite3"
    assert media.thumb_path(asset) is None


# --------------------------------------------------------------------------------------------- library


def test_query_assets_filters(app, db: Session, brand: Brand) -> None:
    other = Brand(name="Otra marca", color="#e8590c", timezone="Europe/Madrid")
    db.add(other)
    db.commit()
    a = ingest_bytes(db, make_image("PNG", (10, 10), (1, 2, 3)), "Playa 100%.png", brand.id)
    b = ingest_bytes(db, make_image("PNG", (10, 10), (4, 5, 6)), "montaña.png", None)  # shared
    c = ingest_bytes(db, make_image("PNG", (10, 10), (7, 8, 9)), "playa privada.png", other.id)
    v = media.ingest_path(
        db, make_video(Path(app.state.paths.tmp_dir).parent / "clip.mp4"), "Playa.mp4", brand.id
    )
    items, total = media.query_assets(db, brand.id)
    assert {x.id for x in items} == {a.id, b.id, v.id} and total == 3
    assert [x.id for x in media.query_assets(db, brand.id, kind="video")[0]] == [v.id]
    assert {x.id for x in media.query_assets(db, brand.id, q="PLAYA")[0]} == {a.id, v.id}
    assert [x.id for x in media.query_assets(db, brand.id, q="100%")[0]] == [a.id]
    assert media.query_assets(db, brand.id, q="_")[1] == 0
    page, total = media.query_assets(db, brand.id, limit=2)
    assert len(page) == 2 and total == 3
    assert media.get_visible(db, c.id, brand.id) is None
    assert media.get_visible(db, c.id, other.id) is not None


def _post_with(
    db: Session,
    brand: Brand,
    account: Account,
    asset: MediaAsset,
    status: TargetStatus,
    *,
    base: bool = True,
    override: list[int] | None = None,
    options: dict | None = None,
) -> tuple[Post, PostTarget]:
    post = Post(brand_id=brand.id, text="Hola")
    if base:
        post.set_media([asset])
    target = PostTarget(
        account_id=account.id,
        network=account.network,
        format="post",
        status=status,
        media_override=override,
        options=options or {},
    )
    post.targets.append(target)
    db.add(post)
    db.commit()
    return post, target


def test_delete_is_blocked_while_pending_targets_use_it(
    app, db: Session, brand: Brand, demo_account: Account, clock
) -> None:
    asset = ingest_bytes(db, make_image("PNG", (12, 12)), "usada.png", brand.id)
    post, target = _post_with(db, brand, demo_account, asset, TargetStatus.SCHEDULED)
    assert media.pending_usage(db, asset.id) == media.MediaUsage(posts=(post.id,))
    with pytest.raises(MediaInUseError) as exc:
        media.delete_asset(db, asset.id, clock)
    assert exc.value.status == 409 and "1 publicación pendiente" in exc.value.message
    target.status = TargetStatus.PUBLISHED
    db.commit()
    assert not media.pending_usage(db, asset.id)
    assert media.usage_counts(db, [asset.id]) == {asset.id: 1}
    deleted = media.delete_asset(db, asset.id, clock)
    assert deleted.deleted_at == clock.now()
    assert media.get_visible(db, asset.id) is None
    assert media.asset_path(deleted).is_file()  # soft delete keeps the file
    with pytest.raises(LookupError):
        media.delete_asset(db, asset.id, clock)


def test_usage_via_override_thumbnail_and_trash(
    app, db: Session, brand: Brand, demo_account: Account
) -> None:
    asset = ingest_bytes(db, make_image("PNG", (13, 13)), "o.png", brand.id)
    other = ingest_bytes(db, make_image("PNG", (14, 14)), "base.png", brand.id)
    # Base media replaced by an override: only the override counts.
    _post_with(db, brand, demo_account, other, TargetStatus.DRAFT, override=[asset.id])
    assert media.pending_usage(db, asset.id).total == 1
    assert not media.pending_usage(db, other.id)
    yt = Account(
        brand_id=brand.id,
        network=Network.YOUTUBE,
        external_id="UC1",
        name="Canal",
        status=AccountStatus.CONNECTED,
    )
    db.add(yt)
    db.commit()
    thumb_post, _ = _post_with(
        db,
        brand,
        yt,
        other,
        TargetStatus.SCHEDULED,
        base=False,
        options={"thumbnail_media_id": str(other.id)},
    )
    assert media.pending_usage(db, other.id).posts == (thumb_post.id,)
    thumb_post.deleted_at = thumb_post.created_at + timedelta(minutes=1)  # moved to the trash
    db.commit()
    # Still needed: restoring the post brings it back (see test_review_media for the real trash flow).
    assert media.pending_usage(db, other.id) == media.MediaUsage(trashed=(thumb_post.id,))


def test_usage_counts_include_ideas_without_targets(app, db: Session, brand: Brand) -> None:
    asset = ingest_bytes(db, make_image("PNG", (15, 15)), "idea.png", brand.id)
    idea = Post(brand_id=brand.id, text="Idea", kind="idea")
    idea.set_media([asset])
    db.add(idea)
    db.commit()
    assert media.usage_counts(db, [asset.id]) == {asset.id: 1}


def test_rename(app, db: Session) -> None:
    asset = ingest_bytes(db, make_image("PNG", (5, 6)), "a.png")
    media.rename_asset(db, asset, "  carpeta/Nuevo nombre.png ")
    assert asset.original_name == "Nuevo nombre.png"
    with pytest.raises(MediaError):
        media.rename_asset(db, asset, "")

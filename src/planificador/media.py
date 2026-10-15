"""Media library backend: ingest, metadata, thumbnails and derivatives (SPEC §8).

Storage (relative to ``<data>/media``):

- ``originals/<sha[:2]>/<sha256>.<ext>`` — the uploaded bytes, never modified, canonical extension from the
  sniffed type (the user's file name only lives in the DB);
- ``thumbs/<sha256>.jpg`` — ≤ 480 px JPEG thumbnail (video: poster frame at ``min(1 s, 10 %)``);
- ``derived/<sha256>-<max_bytes>.jpg`` — sRGB JPEG renditions (Instagram/Facebook), cached.

Ingest streams the upload to ``<data>/tmp`` while hashing (never holding the file in memory, 4 GiB cap),
de-duplicates on SHA-256 (an existing asset is returned; a soft-deleted one is restored), sniffs the type from
the magic bytes (the client's name/extension/MIME are ignored), validates it by actually opening it with
Pillow / PyAV, extracts metadata (display width/height after rotation, duration, fps, codecs, bitrate, HDR,
EXIF orientation) and makes the thumbnail.

Contract (cross-module): :func:`ingest`, :func:`to_media_info`, :func:`asset_path`, :func:`thumb_path`,
:func:`derived_jpeg`. Library helpers: :func:`query_assets`, :func:`pending_usage`, :func:`usage_counts`,
:func:`delete_asset`, :func:`rename_asset`, :class:`IncomingFile` (used by the streaming upload route).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import math
import os
import re
import struct
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from planificador.models import MediaAsset, MediaKind, Post, PostMedia, PostTarget, TargetStatus, utcnow
from planificador.providers.base import MediaInfo

log = logging.getLogger(__name__)

#: SPEC §8: max upload 4 GB (streamed, never read whole into memory).
MAX_UPLOAD_BYTES = 4 * 1024**3
CHUNK_SIZE = 1024 * 1024
THUMB_MAX_PX = 480
THUMB_QUALITY = 82
DERIVED_QUALITIES = (92, 88, 84, 80, 75, 70, 65)
DERIVED_MAX_SIDE = 4096  # renditions never need more (IG scales to 1440, FB to 2048)
HEAD_BYTES = 64
#: Statuses whose targets still need their media (soft delete is blocked while any exists).
PENDING_STATUSES: tuple[TargetStatus, ...] = (
    TargetStatus.DRAFT,
    TargetStatus.SCHEDULED,
    TargetStatus.PUBLISHING,
    TargetStatus.MANUAL_PENDING,
    TargetStatus.ERROR,
    TargetStatus.MISSED,
)

# FFmpeg colour transfers that mean HDR (AVCOL_TRC_SMPTE2084 = PQ 16, AVCOL_TRC_ARIB_STD_B67 = HLG 18).
HDR_TRC_VALUES = {16, 18}
HDR_TRC_NAMES = {"smpte2084", "arib-std-b67"}

ALLOWED_DESCRIPTION = "JPEG, PNG, WebP, GIF, HEIC, MP4, MOV, M4V o WebM"


@dataclass(frozen=True)
class FileType:
    kind: str  # "image" | "video"
    mime: str
    ext: str
    label: str


TYPES: dict[str, FileType] = {
    "jpeg": FileType("image", "image/jpeg", ".jpg", "JPEG"),
    "png": FileType("image", "image/png", ".png", "PNG"),
    "webp": FileType("image", "image/webp", ".webp", "WebP"),
    "gif": FileType("image", "image/gif", ".gif", "GIF"),
    "heic": FileType("image", "image/heic", ".heic", "HEIC"),
    "mp4": FileType("video", "video/mp4", ".mp4", "MP4"),
    "mov": FileType("video", "video/quicktime", ".mov", "MOV"),
    "m4v": FileType("video", "video/x-m4v", ".m4v", "M4V"),
    "webm": FileType("video", "video/webm", ".webm", "WebM"),
}
MIME_EXTENSIONS = {t.mime: t.ext for t in TYPES.values()}
TYPES_BY_MIME = {t.mime: t for t in TYPES.values()}

_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_MOV_ATOMS = {b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}


class MediaError(Exception):
    """User-facing media problem (Spanish ``message``) with an HTTP-ish ``status``."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class MediaInUseError(MediaError):
    def __init__(self, count: int) -> None:
        plural = "publicación pendiente" if count == 1 else "publicaciones pendientes"
        super().__init__(
            "media_in_use",
            f"No se puede borrar: se usa en {count} {plural}. Quítalo de ellas (o bórralas) antes.",
            409,
        )
        self.count = count


# ============================================================================================== paths


def _paths() -> Any:
    from planificador import config

    return config.get_app_settings().paths


def media_dir() -> Path:
    return Path(_paths().media_dir)


def tmp_dir() -> Path:
    directory = Path(_paths().tmp_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _inside_media(rel: str | None) -> Path | None:
    if not rel:
        return None
    root = media_dir().resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        log.warning("Media path escapes the media dir: %r", rel)
        return None
    return candidate


def asset_path(asset: MediaAsset) -> Path:
    """Absolute path of the original file (inside ``<data>/media``)."""
    path = _inside_media(asset.rel_path)
    if path is None:
        raise MediaError("media_path_invalid", "Ruta de archivo no válida.", 500)
    return path


def thumb_path(asset: MediaAsset) -> Path | None:
    """Absolute path of the thumbnail, or ``None`` when there is none on disk."""
    path = _inside_media(asset.thumb_rel_path)
    return path if path is not None and path.is_file() else None


def to_media_info(asset: MediaAsset) -> MediaInfo:
    return MediaInfo(
        id=asset.id,
        kind=str(asset.kind),
        path=asset_path(asset),
        mime=asset.mime,
        size_bytes=int(asset.size_bytes or 0),
        width=asset.width,
        height=asset.height,
        duration_s=asset.duration_s,
        fps=asset.fps,
        video_codec=asset.video_codec,
        audio_codec=asset.audio_codec,
        bitrate=asset.bitrate,
        is_hdr=bool(asset.is_hdr),
        original_name=asset.original_name or "",
    )


def _replace(src: Path, dest: Path) -> None:
    """``os.replace`` with a few retries (antivirus/indexers briefly lock new files on Windows)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    for attempt in range(3):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
        except OSError:
            return


# ============================================================================================ upload


class IncomingFile:
    """An upload being streamed to ``<data>/tmp`` while it is hashed. Enforces ``max_bytes``.

    Usage: ``write()`` chunks, then :func:`ingest_incoming` (which consumes it) or :meth:`discard`.
    """

    def __init__(self, max_bytes: int | None = None, directory: Path | None = None) -> None:
        directory = directory or tmp_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="subida-", suffix=".part", dir=directory)
        self.path = Path(name)
        self._fh: BinaryIO | None = os.fdopen(fd, "wb")
        self._hash = hashlib.sha256()
        self.size = 0
        self.head = b""
        self.max_bytes = max_bytes if max_bytes is not None else MAX_UPLOAD_BYTES

    def write(self, chunk: bytes) -> None:
        if self._fh is None:
            raise ValueError("IncomingFile is closed")
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.max_bytes:
            self.discard()
            raise MediaError(
                "too_large",
                f"El archivo supera el tamaño máximo permitido ({_human_size(self.max_bytes)}).",
                413,
            )
        if len(self.head) < HEAD_BYTES:
            self.head += chunk[: HEAD_BYTES - len(self.head)]
        self._hash.update(chunk)
        self._fh.write(chunk)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def discard(self) -> None:
        self.close()
        _unlink(self.path)

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def __enter__(self) -> IncomingFile:
        return self

    def __exit__(self, *exc: Any) -> None:
        if exc[0] is not None:
            self.discard()


def _human_size(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.0f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return f"{max(1, round(n / 1024))} KB"


def clean_name(name: str | None, ext: str = "") -> str:
    """User file name for display: basename only, no control characters, ≤ 200 characters."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = re.split(r"[\\/]", text)[-1]
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C").strip().strip(".")
    if len(text) > 200:
        stem, dot, suffix = text.rpartition(".")
        text = (stem[:190] + dot + suffix[:9]) if dot and len(suffix) <= 9 else text[:200]
    return text or f"archivo{ext}"


def ingest(db: Session, file_obj: BinaryIO, original_name: str, brand_id: int | None) -> MediaAsset:
    """Store an uploaded file (a readable binary object) in the library and return its asset.

    Raises :class:`MediaError` (Spanish message) for empty, too large, unsupported or corrupt files.
    """
    incoming = IncomingFile()
    try:
        while True:
            chunk = file_obj.read(CHUNK_SIZE)
            if not chunk:
                break
            incoming.write(chunk)
    except BaseException:
        incoming.discard()
        raise
    return ingest_incoming(db, incoming, original_name, brand_id)


def ingest_path(
    db: Session, path: Path, original_name: str | None = None, brand_id: int | None = None
) -> MediaAsset:
    """Ingest a local file (copied, the source is left untouched). Handy for seeding and tests."""
    with open(path, "rb") as fh:
        return ingest(db, fh, original_name or Path(path).name, brand_id)


def ingest_incoming(
    db: Session, incoming: IncomingFile, original_name: str, brand_id: int | None
) -> MediaAsset:
    """Second half of :func:`ingest` for an already streamed :class:`IncomingFile` (always consumed)."""
    incoming.close()
    try:
        if incoming.size == 0:
            raise MediaError("empty", "El archivo está vacío.", 400)
        sha = incoming.sha256
        existing = db.scalar(select(MediaAsset).where(MediaAsset.sha256 == sha))
        if existing is not None and existing.deleted_at is None and _original_ok(existing):
            if existing.brand_id is not None and existing.brand_id != brand_id:
                existing.brand_id = None  # same file uploaded in two brands: share it with both
            ensure_thumbnail(existing)
            db.commit()
            return existing

        ftype = sniff(incoming.head, incoming.path)
        meta = probe(incoming.path, ftype)
        rel = f"originals/{sha[:2]}/{sha}{ftype.ext}"
        dest = media_dir() / rel
        if dest.is_file() and dest.stat().st_size == incoming.size:
            _unlink(incoming.path)
        else:
            _replace(incoming.path, dest)

        asset = existing or MediaAsset(sha256=sha)
        asset.brand_id = brand_id
        asset.kind = MediaKind(ftype.kind)
        asset.original_name = clean_name(original_name, ftype.ext)
        asset.rel_path = rel
        asset.size_bytes = incoming.size
        asset.mime = ftype.mime
        asset.width = meta.get("width")
        asset.height = meta.get("height")
        asset.duration_s = meta.get("duration_s")
        asset.fps = meta.get("fps")
        asset.video_codec = meta.get("video_codec")
        asset.audio_codec = meta.get("audio_codec")
        asset.bitrate = meta.get("bitrate")
        asset.is_hdr = bool(meta.get("is_hdr"))
        asset.rotation = int(meta.get("rotation") or 0)
        asset.deleted_at = None
        if existing is None:
            asset.created_at = utcnow()
        asset.thumb_rel_path = make_thumbnail(dest, ftype.kind, sha, rotation_ccw=meta.get("rotation_ccw", 0))
        db.add(asset)
        try:
            db.commit()
        except IntegrityError:
            # The same file was uploaded concurrently: return the row the other request created.
            db.rollback()
            other = db.scalar(select(MediaAsset).where(MediaAsset.sha256 == sha))
            if other is None:  # pragma: no cover - unexpected
                raise
            return other
        log.info("Media %s stored (%s, %s bytes)", asset.id, ftype.label, incoming.size)
        return asset
    finally:
        _unlink(incoming.path)


def _original_ok(asset: MediaAsset) -> bool:
    try:
        return asset_path(asset).is_file()
    except MediaError:
        return False


# ============================================================================================ sniffing


def _heif_available() -> bool:
    return _heif_opener() is not None


_heif_lock = threading.Lock()
_heif_state: list[Any] = []


def _heif_opener() -> Any:
    """Register pillow-heif's opener once (optional extra ``heic``); ``None`` when not installed."""
    with _heif_lock:
        if not _heif_state:
            try:
                import pillow_heif  # type: ignore[import-not-found]

                pillow_heif.register_heif_opener()
                _heif_state.append(pillow_heif)
            except Exception:
                _heif_state.append(None)
        return _heif_state[0]


def sniff(head: bytes, path: Path | None = None) -> FileType:
    """File type from the magic bytes (never from the name). Raises :class:`MediaError` if not allowed."""
    if head.startswith(b"\xff\xd8\xff"):
        return TYPES["jpeg"]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return TYPES["png"]
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return TYPES["gif"]
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return TYPES["webp"]
    if head[4:8] == b"ftyp":
        major = head[8:12]
        brands = {head[i : i + 4] for i in range(16, min(len(head), 64) - 3, 4)} | {major}
        if brands & _HEIF_BRANDS or (major in (b"mif1", b"msf1") and not brands & {b"avif", b"avis"}):
            if not _heif_available():
                raise MediaError(
                    "heic_unavailable",
                    "Las fotos HEIC necesitan el complemento pillow-heif (instala el extra «heic») o "
                    "conviértelas a JPEG.",
                    415,
                )
            return TYPES["heic"]
        if brands & {b"avif", b"avis"}:
            raise MediaError(
                "unsupported", f"Las imágenes AVIF no están admitidas. Usa {ALLOWED_DESCRIPTION}.", 415
            )
        if major == b"qt  ":
            return TYPES["mov"]
        if major.startswith(b"M4V"):
            return TYPES["m4v"]
        if major.startswith((b"M4A", b"M4B", b"M4P")):
            raise MediaError("unsupported", "Los archivos de solo audio no están admitidos.", 415)
        return TYPES["mp4"]
    if head[4:8] in _MOV_ATOMS:
        return TYPES["mov"]
    if head[:4] == b"\x1a\x45\xdf\xa3":
        data = head
        if path is not None:
            with contextlib.suppress(OSError), open(path, "rb") as fh:
                data = fh.read(4096)
        if b"webm" in data:
            return TYPES["webm"]
        raise MediaError("unsupported", f"Los vídeos MKV no están admitidos. Usa {ALLOWED_DESCRIPTION}.", 415)
    raise MediaError(
        "unsupported", f"Tipo de archivo no admitido. Formatos válidos: {ALLOWED_DESCRIPTION}.", 415
    )


# ============================================================================================= probing

_EXIF_ORIENTATION = 0x0112
#: EXIF orientation → clockwise rotation needed to display, and whether width/height swap.
_ORIENTATION_ROTATION = {1: 0, 2: 0, 3: 180, 4: 180, 5: 90, 6: 90, 7: 270, 8: 270}


def probe(path: Path, ftype: FileType) -> dict[str, Any]:
    """Metadata dict (``width``, ``height`` as displayed, ``rotation`` clockwise degrees, video fields)."""
    if ftype.kind == "image":
        return probe_image(path)
    return probe_video(path)


def _open_image(path: Path) -> Any:
    from PIL import Image

    if path.suffix.lower() in (".heic", ".heif") or _is_heif(path):
        _heif_opener()
    try:
        return Image.open(path)
    except Image.DecompressionBombError as exc:
        raise MediaError("image_too_big", "La imagen tiene demasiados píxeles para procesarla.", 413) from exc


def _is_heif(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return False
    return head[4:8] == b"ftyp" and (head[8:12] in _HEIF_BRANDS or head[8:12] in (b"mif1", b"msf1"))


def probe_image(path: Path) -> dict[str, Any]:
    from PIL import Image, UnidentifiedImageError

    try:
        with _open_image(path) as img:
            width, height = img.size
            orientation = 1
            with contextlib.suppress(Exception):
                orientation = int(img.getexif().get(_EXIF_ORIENTATION) or 1)
            # Decode (a reduced draft for JPEG) to catch truncated or corrupt data.
            with contextlib.suppress(Exception):
                img.draft("RGB", (256, 256))
            img.load()
    except MediaError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise MediaError("corrupt", "La imagen está dañada o no se puede leer.", 422) from exc
    rotation = _ORIENTATION_ROTATION.get(orientation, 0)
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    return {"width": width, "height": height, "rotation": rotation}


def _display_matrix_rotation(blob: bytes) -> int | None:
    """Counter-clockwise rotation (degrees) from an FFmpeg display matrix (9 × int32, 16.16 fixed point)."""
    if not blob or len(blob) < 36:
        return None
    m = struct.unpack("<9i", blob[:36])
    a, b, c, d = (m[0] / 65536.0, m[1] / 65536.0, m[3] / 65536.0, m[4] / 65536.0)
    sx, sy = math.hypot(a, c), math.hypot(b, d)
    if not sx or not sy:
        return None
    angle = -math.degrees(math.atan2(b / sy, a / sx))
    return int(round(angle))


def _video_rotation_ccw(stream: Any) -> int:
    with contextlib.suppress(Exception):
        side = stream.codec_context.coded_side_data or {}
        blob = side.get("display_matrix") if hasattr(side, "get") else None
        if blob:
            value = _display_matrix_rotation(bytes(blob))
            if value is not None:
                return value
    with contextlib.suppress(Exception):
        tag = (stream.metadata or {}).get("rotate")
        if tag not in (None, ""):
            return -int(float(tag))  # the legacy tag is clockwise
    return 0


def _normalize_rotation(cw: int) -> int:
    return int(round(cw / 90.0)) * 90 % 360


def _trc_is_hdr(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return value in HDR_TRC_VALUES
    name = str(getattr(value, "name", value)).lower().replace("_", "-")
    return name in HDR_TRC_NAMES or any(n.replace("-", "") in name.replace("-", "") for n in HDR_TRC_NAMES)


def _fraction(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if number > 0 and math.isfinite(number) else None


def probe_video(path: Path) -> dict[str, Any]:
    import av

    try:
        container = av.open(str(path))
    except Exception as exc:
        raise MediaError("corrupt", "El vídeo está dañado o no se puede leer.", 422) from exc
    try:
        if not container.streams.video:
            raise MediaError("no_video", "El archivo no contiene ninguna pista de vídeo.", 422)
        stream = container.streams.video[0]
        cc = stream.codec_context
        duration = None
        if container.duration:
            duration = container.duration / 1_000_000
        elif stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        fps = _fraction(stream.average_rate) or _fraction(stream.guessed_rate) or _fraction(stream.base_rate)
        bitrate = container.bit_rate or cc.bit_rate or None
        if not bitrate and duration:
            with contextlib.suppress(OSError):
                bitrate = int(path.stat().st_size * 8 / duration)
        audio_codec = None
        if container.streams.audio:
            audio_codec = container.streams.audio[0].codec_context.name
        rotation_ccw = _video_rotation_ccw(stream)
        hdr = _trc_is_hdr(getattr(cc, "color_trc", None))
        width, height = cc.width or None, cc.height or None
        if not hdr or not width:
            # Some containers only reveal colour info / size on the first decoded frame.
            with contextlib.suppress(Exception):
                for frame in container.decode(stream):
                    hdr = hdr or _trc_is_hdr(getattr(frame, "color_trc", None))
                    width = width or frame.width
                    height = height or frame.height
                    if not rotation_ccw:
                        rotation_ccw = int(getattr(frame, "rotation", 0) or 0)
                    break
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError("corrupt", "El vídeo está dañado o no se puede leer.", 422) from exc
    finally:
        container.close()
    rotation = _normalize_rotation(-rotation_ccw)
    if rotation in (90, 270) and width and height:
        width, height = height, width
    return {
        "width": width,
        "height": height,
        "duration_s": round(duration, 3) if duration else None,
        "fps": round(fps, 3) if fps else None,
        "video_codec": (cc.name or None),
        "audio_codec": audio_codec,
        "bitrate": int(bitrate) if bitrate else None,
        "is_hdr": hdr,
        "rotation": rotation,
        "rotation_ccw": rotation_ccw,
    }


# ========================================================================================== thumbnails


def _flatten_rgb(img: Any) -> Any:
    """RGB image with transparency composited on white."""
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _to_srgb(img: Any) -> Any:
    """Convert an embedded ICC profile (e.g. Display P3 iPhone photos) to sRGB; best effort."""
    icc = img.info.get("icc_profile")
    if not icc:
        return img
    try:
        from PIL import ImageCms

        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        dst = ImageCms.createProfile("sRGB")
        mode = "RGBA" if img.mode in ("RGBA", "LA") else "RGB"
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert(mode)
        converted = ImageCms.profileToProfile(img, src, dst, outputMode=mode)
        return converted if converted is not None else img
    except Exception:
        log.debug("ICC → sRGB conversion failed; keeping pixels as they are", exc_info=True)
        return img


def _save_jpeg_atomic(img: Any, dest: Path, quality: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=".jpg", dir=dest.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        img.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True)
        _replace(tmp, dest)
    finally:
        _unlink(tmp)


def image_thumbnail(path: Path, max_px: int = THUMB_MAX_PX) -> Any:
    """Pillow RGB thumbnail of an image file (EXIF orientation applied, first GIF frame)."""
    from PIL import Image, ImageOps

    with _open_image(path) as img:
        with contextlib.suppress(Exception):
            img.seek(0)
        with contextlib.suppress(Exception):
            img.draft("RGB", (max_px * 2, max_px * 2))
        img = ImageOps.exif_transpose(img)
        img = _flatten_rgb(_to_srgb(img))
        img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        return img.copy()


def video_frame(path: Path, at: float | None = None, rotation_ccw: int | None = None) -> Any:
    """Pillow RGB image of the frame at ``at`` seconds (default: ``min(1 s, 10 %)``), rotation applied."""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        duration = container.duration / 1_000_000 if container.duration else None
        if at is None:
            at = min(1.0, duration * 0.1) if duration else 0.0
        frame = None
        if at > 0 and stream.time_base:
            with contextlib.suppress(Exception):
                container.seek(int(at / stream.time_base), stream=stream, backward=True, any_frame=False)
                for candidate in container.decode(stream):
                    frame = candidate
                    if candidate.time is None or candidate.time >= at - 0.05:
                        break
        if frame is None:
            container.seek(0)
            for candidate in container.decode(stream):
                frame = candidate
                break
        if frame is None:
            raise MediaError("no_frames", "No se ha podido leer ningún fotograma del vídeo.", 422)
        image = frame.to_image()
        ccw = rotation_ccw if rotation_ccw is not None else int(getattr(frame, "rotation", 0) or 0)
        if ccw % 360:
            image = image.rotate(ccw, expand=True)
        return image.convert("RGB")


def make_thumbnail(path: Path, kind: str, sha: str, *, rotation_ccw: int | None = None) -> str | None:
    """Write ``thumbs/<sha>.jpg``; returns its path relative to the media dir (``None`` on failure)."""
    from PIL import Image

    rel = f"thumbs/{sha}.jpg"
    try:
        if kind == "image":
            img = image_thumbnail(path)
        else:
            img = video_frame(path, rotation_ccw=rotation_ccw)
            img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.Resampling.LANCZOS)
        _save_jpeg_atomic(img, media_dir() / rel, THUMB_QUALITY)
        return rel
    except Exception:
        log.warning("Could not make a thumbnail for %s", path.name, exc_info=True)
        return None


def ensure_thumbnail(asset: MediaAsset) -> Path | None:
    """The thumbnail path, regenerating it when missing (e.g. thumbs dir cleaned). Updates the row."""
    existing = thumb_path(asset)
    if existing is not None:
        return existing
    try:
        original = asset_path(asset)
    except MediaError:
        return None
    if not original.is_file():
        return None
    rotation_ccw = -int(asset.rotation or 0) if asset.is_video else None
    rel = make_thumbnail(original, str(asset.kind), asset.sha256, rotation_ccw=rotation_ccw)
    asset.thumb_rel_path = rel
    return thumb_path(asset)


# ========================================================================================== derivatives


def derived_jpeg(asset: MediaAsset, max_bytes: int = 8_000_000) -> Path:
    """sRGB JPEG rendition of an image asset no larger than ``max_bytes`` (cached in ``media/derived``).

    EXIF orientation is applied and metadata (EXIF/GPS) is dropped; transparency is flattened on white.
    Quality steps down first, then the image is scaled down. Raises :class:`MediaError` for videos or when
    the size cannot be reached.
    """
    if not asset.is_image:
        raise MediaError("not_an_image", "Solo las imágenes se pueden convertir a JPEG.", 400)
    max_bytes = int(max_bytes)
    dest = media_dir() / "derived" / f"{asset.sha256}-{max_bytes}.jpg"
    if dest.is_file() and 0 < dest.stat().st_size <= max_bytes:
        return dest
    source = asset_path(asset)
    if not source.is_file():
        raise MediaError("media_missing", f"No se encuentra el archivo «{asset.original_name}».", 404)
    from PIL import Image, ImageOps

    with _open_image(source) as opened:
        with contextlib.suppress(Exception):
            opened.seek(0)
        img = ImageOps.exif_transpose(opened)
        img = _flatten_rgb(_to_srgb(img))
        img.load()
    if max(img.size) > DERIVED_MAX_SIDE:
        img.thumbnail((DERIVED_MAX_SIDE, DERIVED_MAX_SIDE), Image.Resampling.LANCZOS)
    for _ in range(12):
        for quality in DERIVED_QUALITIES:
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
            if buffer.tell() <= max_bytes:
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=".jpg", dir=dest.parent)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(buffer.getvalue())
                _replace(Path(name), dest)
                return dest
        if min(img.size) <= 64:
            break
        img = img.resize(
            (max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))), Image.Resampling.LANCZOS
        )
    raise MediaError(
        "derived_too_large",
        f"No se ha podido reducir «{asset.original_name}» a {max_bytes // 1_000_000} MB.",
        422,
    )


# ============================================================================================ library


def visible_filter(brand_id: int | None) -> Any:
    """Assets of ``brand_id`` plus shared ones (``brand_id`` NULL), not deleted."""
    cond = MediaAsset.deleted_at.is_(None)
    if brand_id is None:
        return cond
    return cond & or_(MediaAsset.brand_id == brand_id, MediaAsset.brand_id.is_(None))


def query_assets(
    db: Session,
    brand_id: int | None,
    *,
    q: str | None = None,
    kind: str | None = None,
    limit: int = 60,
    offset: int = 0,
) -> tuple[list[MediaAsset], int]:
    """(page of assets newest first, total matching) for the library grid/picker."""
    cond = visible_filter(brand_id)
    if kind in ("image", "video"):
        cond = cond & (MediaAsset.kind == MediaKind(kind))
    text = (q or "").strip()
    if text:
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond = cond & MediaAsset.original_name.ilike(pattern, escape="\\")
    total = int(db.scalar(select(func.count()).select_from(MediaAsset).where(cond)) or 0)
    rows = db.scalars(
        select(MediaAsset)
        .where(cond)
        .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
        .limit(max(1, min(limit, 500)))
        .offset(max(0, offset))
    ).all()
    return list(rows), total


def get_visible(db: Session, asset_id: int, brand_id: int | None = None) -> MediaAsset | None:
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.deleted_at is not None:
        return None
    if brand_id is not None and asset.brand_id not in (None, brand_id):
        return None
    return asset


def _targets_using(db: Session, asset_ids: Iterable[int], statuses: Iterable[TargetStatus] | None) -> dict:
    """``asset_id -> set(target ids)`` of non-deleted posts' targets using each asset (base media, per-network
    override or YouTube custom thumbnail)."""
    ids = {int(i) for i in asset_ids}
    out: dict[int, set[int]] = {i: set() for i in ids}
    if not ids:
        return out
    status_cond = PostTarget.status.in_(list(statuses)) if statuses is not None else True
    rows = db.execute(
        select(PostMedia.media_id, PostTarget.id, PostTarget.media_override)
        .join(Post, Post.id == PostMedia.post_id)
        .join(PostTarget, PostTarget.post_id == Post.id)
        .where(PostMedia.media_id.in_(ids), Post.deleted_at.is_(None), status_cond)
    ).all()
    for media_id, target_id, override in rows:
        if override is None:  # an override replaces the base list
            out[media_id].add(target_id)
    candidates = db.execute(
        select(PostTarget.id, PostTarget.media_override, PostTarget.options)
        .join(Post, Post.id == PostTarget.post_id)
        .where(Post.deleted_at.is_(None), status_cond)
        .where(or_(PostTarget.media_override.is_not(None), PostTarget.network == "youtube"))
    ).all()
    for target_id, override, options in candidates:
        for media_id in override or []:
            with contextlib.suppress(TypeError, ValueError):
                if int(media_id) in ids:
                    out[int(media_id)].add(target_id)
        thumb = (options or {}).get("thumbnail_media_id")
        with contextlib.suppress(TypeError, ValueError):
            if thumb not in (None, "") and int(thumb) in ids:
                out[int(thumb)].add(target_id)
    return out


def pending_usage(db: Session, asset_id: int) -> list[int]:
    """Ids of targets that still need this asset (draft/scheduled/publishing/assisted/error/missed)."""
    return sorted(_targets_using(db, [asset_id], PENDING_STATUSES)[int(asset_id)])


def usage_counts(db: Session, asset_ids: Iterable[int]) -> dict[int, int]:
    """``asset_id -> number of posts (not in the trash) that use it``."""
    ids = [int(i) for i in asset_ids]
    if not ids:
        return {}
    by_target = _targets_using(db, ids, None)
    target_ids = {t for targets in by_target.values() for t in targets}
    post_of = dict(
        db.execute(select(PostTarget.id, PostTarget.post_id).where(PostTarget.id.in_(target_ids))).all()
    )
    counts = {i: len({post_of[t] for t in by_target[i] if t in post_of}) for i in ids}
    # Posts without targets yet (ideas) still use their base media.
    rows = db.execute(
        select(PostMedia.media_id, func.count(func.distinct(PostMedia.post_id)))
        .join(Post, Post.id == PostMedia.post_id)
        .where(PostMedia.media_id.in_(ids), Post.deleted_at.is_(None), ~Post.targets.any())
        .group_by(PostMedia.media_id)
    ).all()
    for media_id, n in rows:
        counts[media_id] = counts.get(media_id, 0) + int(n)
    return counts


def delete_asset(db: Session, asset_id: int, clock: Any = None, *, brand_id: int | None = None) -> MediaAsset:
    """Soft delete (``deleted_at``). Raises :class:`MediaInUseError` while pending targets use it and
    ``LookupError`` when it does not exist. Files stay on disk (published posts may still reference them)."""
    asset = get_visible(db, asset_id, brand_id)
    if asset is None:
        raise LookupError(asset_id)
    users = pending_usage(db, asset.id)
    if users:
        raise MediaInUseError(len(users))
    now: datetime = clock.now() if clock is not None else utcnow()
    asset.deleted_at = now
    db.commit()
    log.info("Media %s moved out of the library", asset.id)
    return asset


def rename_asset(db: Session, asset: MediaAsset, name: str) -> MediaAsset:
    cleaned = clean_name(name) if (name or "").strip() else ""
    if not cleaned:
        raise MediaError("name_required", "Escribe un nombre.", 422)
    asset.original_name = cleaned
    db.commit()
    return asset


__all__ = [
    "CHUNK_SIZE",
    "MAX_UPLOAD_BYTES",
    "PENDING_STATUSES",
    "TYPES",
    "FileType",
    "IncomingFile",
    "MediaError",
    "MediaInUseError",
    "asset_path",
    "clean_name",
    "delete_asset",
    "derived_jpeg",
    "ensure_thumbnail",
    "get_visible",
    "ingest",
    "ingest_incoming",
    "ingest_path",
    "make_thumbnail",
    "pending_usage",
    "probe",
    "query_assets",
    "rename_asset",
    "sniff",
    "thumb_path",
    "to_media_info",
    "usage_counts",
    "video_frame",
]

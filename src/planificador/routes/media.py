"""Media library ("Biblioteca", SPEC §8) and the media picker used by the composer.

Pages and fragments:

- ``GET /biblioteca`` — library page (search, type filter, upload, grid, detail side panel). An htmx request
  aimed at ``#media-results`` gets only the results fragment (search/filter/"Cargar más").
- ``GET /biblioteca/selector?multiple=1&kind=image|video|any`` — media picker modal fragment for
  ``#modal-root``. On confirm it dispatches ``window`` CustomEvent ``media:selected`` with
  ``detail = {items: [{id, kind, name, thumb_url, url, width, height, duration_s}]}`` and closes.
- ``GET /medios/{id}`` — detail panel fragment (htmx) or the library page with that panel open.

Files and API:

- ``POST /biblioteca/subir`` — multipart field ``files`` (one or many), **streamed** to disk while hashing
  (never buffered in memory). ``Accept: application/json`` → JSON list (one entry per file:
  ``{"ok": true, ...item}`` or ``{"ok": false, "name", "error", "code"}``; HTTP 200 when any file was stored,
  else the first error's status). Plain form posts are redirected back to ``/biblioteca``.
- ``GET /medios/{id}/archivo`` (``?descargar=1`` for an attachment), ``GET /medios/{id}/miniatura``.
- ``GET /api/medios/{id}`` — JSON metadata with per-network compatibility; ``GET /api/medios`` — list.
- ``POST /medios/{id}/renombrar``, ``POST /medios/{id}/borrar`` / ``DELETE /medios/{id}`` (soft delete;
  409 while pending posts use the file).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy.orm import Session

from planificador import media, rules
from planificador.deps import ActiveBrand, ClockDep, DbSession
from planificador.models import MediaAsset

log = logging.getLogger(__name__)

router = APIRouter()

PAGE_SIZE = 48
PICKER_PAGE_SIZE = 36
MAX_FILES_PER_REQUEST = 50
FILE_FIELD = "files"

KIND_FILTERS = {"": "Todo", "image": "Fotos", "video": "Vídeos"}
KIND_LABELS = {"image": "Foto", "video": "Vídeo"}
STATUS_LABELS = {"ok": "Compatible", "warning": "Con avisos", "error": "No compatible"}
STATUS_TONES = {"ok": "success", "warning": "warning", "error": "danger"}
CODEC_LABELS = {
    "h264": "H.264",
    "hevc": "HEVC (H.265)",
    "vp8": "VP8",
    "vp9": "VP9",
    "av1": "AV1",
    "mpeg4": "MPEG-4",
    "prores": "ProRes",
    "aac": "AAC",
    "mp3": "MP3",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "ac3": "AC-3",
    "eac3": "E-AC-3",
    "alac": "ALAC",
    "pcm_s16le": "PCM",
}


# ============================================================================================= views


def _filesize(n: Any) -> str:
    from planificador.templating import filesize

    return filesize(n)


def _duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _decimal(value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def thumb_url(asset: MediaAsset) -> str:
    return f"/medios/{asset.id}/miniatura"


def file_url(asset: MediaAsset) -> str:
    return f"/medios/{asset.id}/archivo"


def picker_item(asset: MediaAsset) -> dict[str, Any]:
    """Item of the ``media:selected`` event (composer contract)."""
    return {
        "id": asset.id,
        "kind": str(asset.kind),
        "name": asset.original_name,
        "thumb_url": thumb_url(asset),
        "url": file_url(asset),
        "width": asset.width,
        "height": asset.height,
        "duration_s": asset.duration_s,
    }


def compat_for(asset: MediaAsset) -> dict[str, dict[str, Any]]:
    try:
        return rules.compat_summary(media.to_media_info(asset))
    except Exception:  # pragma: no cover - defensive: a broken row must not break the page
        log.exception("Compatibility check failed for media %s", asset.id)
        return {}


def asset_json(asset: MediaAsset, *, usage: int | None = None, compat: bool = True) -> dict[str, Any]:
    data = picker_item(asset)
    ratio = rules.fmt_ratio(asset.width, asset.height) if asset.width and asset.height else None
    data.update(
        {
            "mime": asset.mime,
            "size_bytes": int(asset.size_bytes or 0),
            "fps": asset.fps,
            "video_codec": asset.video_codec,
            "audio_codec": asset.audio_codec,
            "bitrate": asset.bitrate,
            "is_hdr": bool(asset.is_hdr),
            "rotation": int(asset.rotation or 0),
            "aspect": ratio,
            "sha256": asset.sha256,
            "brand_id": asset.brand_id,
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
            "deleted": asset.deleted_at is not None,
            "download_url": f"{file_url(asset)}?descargar=1",
        }
    )
    if usage is not None:
        data["usage"] = usage
    if compat:
        data["compat"] = compat_for(asset)
    return data


def asset_view(asset: MediaAsset, *, compat: bool = True, usage: int | None = None) -> dict[str, Any]:
    """Display strings for templates (Spanish)."""
    w, h = asset.width, asset.height
    codecs = [CODEC_LABELS.get(c, c.upper()) for c in (asset.video_codec, asset.audio_codec) if c]
    container = media.TYPES_BY_MIME.get(asset.mime)
    summary = compat_for(asset) if compat else {}
    view = {
        "asset": asset,
        "id": asset.id,
        "name": asset.original_name or f"Archivo {asset.id}",
        "kind": str(asset.kind),
        "kind_label": KIND_LABELS.get(str(asset.kind), "Archivo"),
        "format": container.label
        if container
        else (asset.mime.split("/")[-1].upper() if asset.mime else "—"),
        "dims": f"{w} × {h}" if w and h else None,
        "ratio": rules.fmt_ratio(w, h) if w and h else None,
        "orientation": ("Vertical" if h > w else "Horizontal" if w > h else "Cuadrada") if w and h else None,
        "duration": _duration(asset.duration_s),
        "fps": f"{_decimal(asset.fps, 2)} fps" if asset.fps else None,
        "bitrate": f"{_decimal(asset.bitrate / 1_000_000, 1)} Mbps" if asset.bitrate else None,
        "codecs": " · ".join(codecs) if codecs else None,
        "size": _filesize(asset.size_bytes),
        "hdr": bool(asset.is_hdr),
        "rotation": int(asset.rotation or 0),
        "created_at": asset.created_at,
        "thumb_url": thumb_url(asset),
        "file_url": file_url(asset),
        "download_url": f"{file_url(asset)}?descargar=1",
        "item": picker_item(asset),
        "compat": summary,
        "compat_status": {net: info["status"] for net, info in summary.items()},
        "usage": usage,
    }
    return view


def _public_media_off(db: Session) -> bool:
    from planificador import config

    try:
        return str(config.get_setting(db, "public_media_mode") or "off") == "off"
    except Exception:  # pragma: no cover
        return False


def _render(request: Request, name: str, **ctx: Any) -> Response:
    from planificador import templating

    ctx.setdefault("STATUS_LABELS_MEDIA", STATUS_LABELS)
    ctx.setdefault("STATUS_TONES_MEDIA", STATUS_TONES)
    ctx.setdefault("KIND_FILTERS", KIND_FILTERS)
    ctx.setdefault("MAX_UPLOAD_BYTES", media.MAX_UPLOAD_BYTES)
    ctx.setdefault("ACCEPT_TYPES", ACCEPT_ATTR)
    return templating.render(request, name, **ctx)


ACCEPT_ATTR = (
    "image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif,.heic,.heif,"
    "video/mp4,video/quicktime,video/x-m4v,video/webm,.mp4,.mov,.m4v,.webm"
)


def _hx_target(request: Request) -> str:
    if request.headers.get("hx-request") != "true":
        return ""
    return request.headers.get("hx-target", "")


def _page_number(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _kind(value: str | None) -> str:
    return value if value in ("image", "video") else ""


def _results_context(
    db: Session, brand_id: int, q: str, kind: str, page: int, page_size: int, *, compat: bool = True
) -> dict[str, Any]:
    items, total = media.query_assets(
        db, brand_id, q=q, kind=kind or None, limit=page_size, offset=(page - 1) * page_size
    )
    usage = media.usage_counts(db, [a.id for a in items]) if compat else {}
    return {
        "views": [asset_view(a, compat=compat, usage=usage.get(a.id)) for a in items],
        "total": total,
        "page": page,
        "has_more": page * page_size < total,
        "q": q,
        "kind": kind,
    }


def _counts(db: Session, brand_id: int) -> dict[str, int]:
    _, images = media.query_assets(db, brand_id, kind="image", limit=1)
    _, videos = media.query_assets(db, brand_id, kind="video", limit=1)
    return {"": images + videos, "image": images, "video": videos}


# ============================================================================================== pages


@router.get("/biblioteca")
def library_page(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    q: str = "",
    tipo: str = "",
    pagina: int = 1,
    medio: int | None = None,
) -> Response:
    kind = _kind(tipo)
    page = _page_number(pagina)
    ctx = _results_context(db, brand.id, q.strip(), kind, page, PAGE_SIZE)
    target = _hx_target(request)
    if target in ("media-results", "media-more"):
        return _render(
            request,
            "media/_results.html",
            append=target == "media-more",
            oob=True,
            counts=_counts(db, brand.id),
            **ctx,
        )
    detail = None
    if medio:
        asset = media.get_visible(db, medio, brand.id)
        if asset is not None:
            detail = _detail_context(db, asset)
    return _render(
        request,
        "media/index.html",
        page_title="Biblioteca",
        counts=_counts(db, brand.id),
        detail=detail,
        public_media_off=_public_media_off(db),
        **ctx,
    )


@router.get("/biblioteca/selector")
def picker(
    request: Request,
    db: DbSession,
    brand: ActiveBrand,
    multiple: int = 1,
    kind: str = "any",
    q: str = "",
    tipo: str = "",
    pagina: int = 1,
) -> Response:
    fixed_kind = kind if kind in ("image", "video") else ""
    current = fixed_kind or _kind(tipo)
    page = _page_number(pagina)
    ctx = _results_context(db, brand.id, q.strip(), current, page, PICKER_PAGE_SIZE, compat=False)
    target = _hx_target(request)
    picker_ctx = {"multiple": bool(multiple), "fixed_kind": fixed_kind, "picker_kind": kind or "any"}
    if target in ("picker-results", "picker-more"):
        return _render(
            request, "media/_picker_results.html", append=target == "picker-more", **ctx, **picker_ctx
        )
    return _render(request, "media/selector.html", **ctx, **picker_ctx)


def _detail_context(db: Session, asset: MediaAsset) -> dict[str, Any]:
    pending = media.pending_usage(db, asset.id)
    usage = media.usage_counts(db, [asset.id]).get(asset.id, 0)
    view = asset_view(asset, usage=usage)
    view["pending"] = pending.total  # posts (not networks) that block the delete
    view["pending_summary"] = pending.summary
    view["pending_advice"] = pending.advice
    return view


@router.get("/medios/{asset_id}")
def media_detail(request: Request, asset_id: int, db: DbSession, brand: ActiveBrand) -> Response:
    asset = media.get_visible(db, asset_id, brand.id)
    if asset is None:
        raise HTTPException(404, "Ese archivo no existe o se ha borrado.")
    if _hx_target(request) == "media-detail":
        return _render(
            request,
            "media/_detail.html",
            detail=_detail_context(db, asset),
            public_media_off=_public_media_off(db),
        )
    return RedirectResponse(f"/biblioteca?medio={asset.id}", status_code=303)


# ============================================================================================== files


def _any_asset(db: Session, asset_id: int) -> MediaAsset:
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(404, "Archivo no encontrado.")
    return asset


@router.get("/medios/{asset_id}/archivo")
def media_file(asset_id: int, db: DbSession, descargar: int = 0) -> Response:
    asset = _any_asset(db, asset_id)
    try:
        path = media.asset_path(asset)
    except media.MediaError:
        raise HTTPException(404, "Archivo no encontrado.") from None
    if not path.is_file():
        raise HTTPException(404, "El archivo ya no está en el disco.")
    disposition = "attachment" if descargar else "inline"
    return FileResponse(
        path,
        media_type=asset.mime,
        filename=asset.original_name or path.name,
        content_disposition_type=disposition,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/medios/{asset_id}/miniatura")
def media_thumb(asset_id: int, db: DbSession) -> Response:
    asset = _any_asset(db, asset_id)
    path = media.thumb_path(asset)
    if path is None:
        path = media.ensure_thumbnail(asset)
        if path is not None:
            db.commit()
    if path is None:
        raise HTTPException(404, "Sin miniatura.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/medios/{asset_id}")
def media_api(asset_id: int, db: DbSession) -> JSONResponse:
    asset = _any_asset(db, asset_id)
    usage = media.usage_counts(db, [asset.id]).get(asset.id, 0)
    return JSONResponse(asset_json(asset, usage=usage))


@router.get("/api/medios")
def media_api_list(
    db: DbSession, brand: ActiveBrand, q: str = "", tipo: str = "", pagina: int = 1, por_pagina: int = 60
) -> JSONResponse:
    page = _page_number(pagina)
    size = max(1, min(int(por_pagina or 60), 200))
    items, total = media.query_assets(
        db, brand.id, q=q, kind=_kind(tipo) or None, limit=size, offset=(page - 1) * size
    )
    return JSONResponse(
        {
            "items": [asset_json(a, compat=False) for a in items],
            "total": total,
            "page": page,
            "has_more": page * size < total,
        }
    )


# ============================================================================================= upload


class _StreamedUpload:
    """python-multipart callbacks writing every ``files`` part straight into an :class:`IncomingFile`."""

    def __init__(self) -> None:
        self.results: list[tuple[str, media.IncomingFile | media.MediaError]] = []
        self._header_field = b""
        self._header_value = b""
        self._headers: dict[bytes, bytes] = {}
        self._current: media.IncomingFile | None = None
        self._current_name = ""
        self._current_error: media.MediaError | None = None
        self._in_file = False

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }

    def on_part_begin(self) -> None:
        self._headers = {}
        self._current = None
        self._current_error = None
        self._in_file = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._header_field.lower()] = self._header_value
        self._header_field = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", "replace")
        filename = params.get(b"filename")
        if name != FILE_FIELD or filename is None:
            return  # csrf_token and other fields are ignored here (the middleware already checked it)
        self._in_file = True
        self._current_name = filename.decode("utf-8", "replace")
        if len(self.results) >= MAX_FILES_PER_REQUEST:
            self._current_error = media.MediaError(
                "too_many_files", f"Sube como máximo {MAX_FILES_PER_REQUEST} archivos a la vez.", 413
            )
            return
        self._current = media.IncomingFile()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current is None:
            return
        try:
            self._current.write(data[start:end])
        except media.MediaError as exc:
            self._current = None
            self._current_error = exc

    def on_part_end(self) -> None:
        if not self._in_file:
            return
        if self._current is not None:
            self._current.close()
            self.results.append((self._current_name, self._current))
        elif self._current_error is not None:
            self.results.append((self._current_name, self._current_error))
        self._current = None
        self._in_file = False

    def discard_all(self) -> None:
        if self._current is not None:
            self._current.discard()
        for _, item in self.results:
            if isinstance(item, media.IncomingFile):
                item.discard()


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _store(app_state: Any, incoming: media.IncomingFile, name: str, brand_id: int) -> dict[str, Any]:
    session = app_state.db.session()
    try:
        asset = media.ingest_incoming(session, incoming, name, brand_id)
        return {"ok": True, **asset_json(asset)}
    except media.MediaError as exc:
        session.rollback()
        return {
            "ok": False,
            "name": media.clean_name(name),
            "error": exc.message,
            "code": exc.code,
            "status": exc.status,
        }
    except Exception:
        session.rollback()
        log.exception("Upload of %r failed", name)
        return {
            "ok": False,
            "name": media.clean_name(name),
            "error": "No se ha podido guardar el archivo. Revisa el registro de la aplicación.",
            "code": "internal",
            "status": 500,
        }
    finally:
        session.close()


@router.post("/biblioteca/subir")
async def upload(request: Request, brand: ActiveBrand) -> Response:
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    if content_type != b"multipart/form-data" or not params.get(b"boundary"):
        raise HTTPException(415, "Envía los archivos como multipart/form-data (campo «files»).")
    sink = _StreamedUpload()
    parser = MultipartParser(params[b"boundary"], sink.callbacks())
    try:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(parser.write, chunk)
        await run_in_threadpool(parser.finalize)
    except BaseException:
        sink.discard_all()
        raise
    results: list[dict[str, Any]] = []
    for name, item in sink.results:
        if isinstance(item, media.MediaError):
            results.append(
                {
                    "ok": False,
                    "name": media.clean_name(name),
                    "error": item.message,
                    "code": item.code,
                    "status": item.status,
                }
            )
            continue
        results.append(await run_in_threadpool(_store, request.app.state, item, name, brand.id))
    if not results:
        results.append(
            {
                "ok": False,
                "name": "",
                "error": "No se ha recibido ningún archivo.",
                "code": "no_files",
                "status": 400,
            }
        )
    ok = [r for r in results if r["ok"]]
    status = 200 if ok else int(results[0].get("status") or 400)
    if _wants_json(request):
        return JSONResponse(results, status_code=status)
    from planificador.templating import flash

    if ok:
        added = "1 archivo añadido" if len(ok) == 1 else f"{len(ok)} archivos añadidos"
        flash(request, f"{added} a la biblioteca.", "success")
    for failed in [r for r in results if not r["ok"]][:3]:
        flash(request, f"«{failed['name']}»: {failed['error']}", "error")
    return RedirectResponse("/biblioteca", status_code=303)


# ============================================================================================ actions


def _toast_headers(message: str, kind: str = "success", **events: Any) -> dict[str, str]:
    from planificador.templating import toast_headers

    return toast_headers(message, kind, **events)


def _error_response(request: Request, message: str, status: int) -> Response:
    if _wants_json(request) and request.headers.get("hx-request") != "true":
        return JSONResponse({"detail": message}, status_code=status)
    return Response(
        message, status_code=status, media_type="text/plain", headers={"X-Error-Message": quote(message)}
    )


@router.post("/medios/{asset_id}/renombrar")
def rename(
    request: Request, asset_id: int, db: DbSession, brand: ActiveBrand, name: str = Form("")
) -> Response:
    asset = media.get_visible(db, asset_id, brand.id)
    if asset is None:
        return _error_response(request, "Ese archivo no existe o se ha borrado.", 404)
    try:
        media.rename_asset(db, asset, name)
    except media.MediaError as exc:
        return _error_response(request, exc.message, exc.status)
    if _wants_json(request) and request.headers.get("hx-request") != "true":
        return JSONResponse(asset_json(asset, compat=False))
    return _render(
        request,
        "media/_detail.html",
        detail=_detail_context(db, asset),
        public_media_off=_public_media_off(db),
        headers=_toast_headers(
            "Nombre actualizado", **{"media:changed": {"id": asset.id, "name": asset.original_name}}
        ),
    )


def _delete(request: Request, asset_id: int, db: Session, brand_id: int, clock: Any) -> Response:
    try:
        asset = media.delete_asset(db, asset_id, clock, brand_id=brand_id)
    except LookupError:
        return _error_response(request, "Ese archivo no existe o ya se había borrado.", 404)
    except media.MediaInUseError as exc:
        return _error_response(request, exc.message, 409)
    message = f"«{asset.original_name}» se ha quitado de la biblioteca."
    if _wants_json(request) and request.headers.get("hx-request") != "true":
        return JSONResponse({"ok": True, "id": asset.id, "message": message})
    return HTMLResponse("", headers=_toast_headers(message, **{"media:deleted": {"id": asset.id}}))


@router.post("/medios/{asset_id}/borrar")
def delete_post(
    request: Request, asset_id: int, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    return _delete(request, asset_id, db, brand.id, clock)


@router.delete("/medios/{asset_id}")
def delete_method(
    request: Request, asset_id: int, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    return _delete(request, asset_id, db, brand.id, clock)


__all__ = ["asset_json", "asset_view", "picker_item", "router"]

"""CSV import/export ("Importar o exportar CSV", SPEC §14).

Routes:
    GET  /importar                              page: template download, upload, export form (+ last result)
    GET  /importar/plantilla.csv                the template (UTF-8 with BOM, ";")
    POST /importar/revisar                      upload (multipart "archivo") → stored → 303 to the preview
    GET  /importar/revisar/{token}              preview table with per-row validation
    POST /importar/revisar/{token}/confirmar    create the posts (form: filas, consentimiento_tiktok)
    GET  /exportar.csv?desde=&hasta=            posts of the active brand in a date range

Uploaded files wait in ``<data>/tmp/csv-imports/<token>.csv`` between the preview and the confirmation
(removed after importing; leftovers older than a day are cleaned on the next upload).
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from planificador import csvio, templating
from planificador.deps import ActiveBrand, ClockDep, DbSession
from planificador.models import Brand
from planificador.timeutil import Clock, to_brand_local

log = logging.getLogger(__name__)

router = APIRouter()

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
UPLOAD_TTL_SECONDS = 24 * 3600
MAX_EXPORT_DAYS = 3 * 366
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


# ============================================================================================ helpers


def _uploads_dir(request: Request) -> Path:
    directory = Path(request.app.state.settings.paths.tmp_dir) / "csv-imports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cleanup(directory: Path) -> None:
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    for path in directory.glob("*.csv"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:  # pragma: no cover - best effort
            pass


def _upload_path(request: Request, token: str) -> Path:
    if not TOKEN_RE.match(token or ""):
        raise HTTPException(status_code=404, detail="Importación no encontrada.")
    return _uploads_dir(request) / f"{token}.csv"


def _load_upload(request: Request, token: str) -> tuple[bytes, str]:
    path = _upload_path(request, token)
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="Esta importación ya no está disponible. Vuelve a subir el archivo."
        )
    name_file = path.with_suffix(".name")
    name = name_file.read_text(encoding="utf-8") if name_file.is_file() else "archivo.csv"
    return path.read_bytes(), name


def _discard_upload(request: Request, token: str) -> None:
    path = _upload_path(request, token)
    for p in (path, path.with_suffix(".name")):
        try:
            p.unlink()
        except OSError:
            pass


def _today(brand: Brand, clock: Clock) -> date:
    local = to_brand_local(clock.now(), brand.timezone)
    assert local is not None
    return local.date()


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _export_range(brand: Brand, clock: Clock, desde: str | None, hasta: str | None) -> tuple[date, date]:
    today = _today(brand, clock)
    start = _parse_day(desde) or today - timedelta(days=30)
    end = _parse_day(hasta) or today + timedelta(days=90)
    if end < start:
        start, end = end, start
    if (end - start).days > MAX_EXPORT_DAYS:
        end = start + timedelta(days=MAX_EXPORT_DAYS)
    return start, end


def _download(content: bytes, filename: str) -> Response:
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=CSV_MEDIA_TYPE,
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


def _int_or_zero(value: str | None) -> int:
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


# ============================================================================================= routes


@router.get("/importar")
def csv_page(
    request: Request,
    brand: ActiveBrand,
    clock: ClockDep,
    programadas: str | None = None,
    borradores: str | None = None,
    fallidas: str | None = None,
) -> Response:
    today = _today(brand, clock)
    result = None
    if programadas is not None or borradores is not None:
        result = {
            "scheduled": _int_or_zero(programadas),
            "drafts": _int_or_zero(borradores),
            "failed": _int_or_zero(fallidas),
        }
    return templating.render(
        request,
        "csv/index.html",
        page_title="Importar o exportar CSV",
        brand=brand,
        columns=csvio.TEMPLATE_COLUMNS,
        column_help=csvio.COLUMN_HELP,
        export_from=(today - timedelta(days=30)).isoformat(),
        export_to=(today + timedelta(days=90)).isoformat(),
        result=result,
        max_rows=csvio.MAX_ROWS,
    )


@router.get("/importar/plantilla.csv")
def csv_template(brand: ActiveBrand, clock: ClockDep) -> Response:
    return _download(csvio.template_csv(brand, clock), csvio.TEMPLATE_FILENAME)


@router.post("/importar/revisar")
async def csv_upload(request: Request, archivo: Annotated[UploadFile | None, File()] = None) -> Response:
    if archivo is None or not archivo.filename:
        templating.flash(request, "Elige un archivo CSV para importar.", "warning")
        return RedirectResponse("/importar", status_code=303)
    raw = await archivo.read(csvio.MAX_BYTES + 1)
    await archivo.close()
    name = Path(archivo.filename.replace("\\", "/")).name[:120] or "archivo.csv"
    if len(raw) > csvio.MAX_BYTES:
        templating.flash(
            request, f"El archivo es demasiado grande (máximo {csvio.MAX_BYTES // 1_000_000} MB).", "error"
        )
        return RedirectResponse("/importar", status_code=303)
    if not raw.strip():
        templating.flash(request, "El archivo está vacío.", "error")
        return RedirectResponse("/importar", status_code=303)
    directory = _uploads_dir(request)
    _cleanup(directory)
    token = secrets.token_urlsafe(18)
    (directory / f"{token}.csv").write_bytes(raw)
    (directory / f"{token}.name").write_text(name, encoding="utf-8")
    return RedirectResponse(f"/importar/revisar/{token}", status_code=303)


@router.get("/importar/revisar/{token}")
def csv_preview(request: Request, token: str, db: DbSession, brand: ActiveBrand, clock: ClockDep) -> Response:
    raw, name = _load_upload(request, token)
    preview = csvio.build_preview(db, brand, raw, clock)
    return templating.render(
        request,
        "csv/preview.html",
        page_title="Revisar importación",
        brand=brand,
        preview=preview,
        token=token,
        filename=name,
        network_label=templating.network_label,
    )


@router.post("/importar/revisar/{token}/confirmar")
async def csv_confirm(
    request: Request, token: str, db: DbSession, brand: ActiveBrand, clock: ClockDep
) -> Response:
    form = await request.form()
    raw, _name = _load_upload(request, token)
    lines = {int(v) for v in form.getlist("filas") if str(v).isdigit()}
    consent = str(form.get("consentimiento_tiktok") or "") in ("1", "on", "si", "true")
    if not lines:
        templating.flash(request, "No has marcado ninguna fila para importar.", "warning")
        return RedirectResponse(f"/importar/revisar/{token}", status_code=303)
    result = csvio.import_rows(db, brand, raw, clock, lines=lines, tiktok_consent=consent)
    _discard_upload(request, token)
    for line, message in result.failed[:3]:
        where = f"Fila {line}: " if line else ""
        templating.flash(request, f"{where}{message}", "error")
    return RedirectResponse(
        f"/importar?programadas={result.scheduled}&borradores={result.drafts}&fallidas={len(result.failed)}",
        status_code=303,
    )


@router.get("/exportar.csv")
def csv_export(
    db: DbSession,
    brand: ActiveBrand,
    clock: ClockDep,
    desde: str | None = None,
    hasta: str | None = None,
) -> Response:
    start, end = _export_range(brand, clock, desde, hasta)
    return _download(csvio.export_csv(db, brand, start, end), csvio.export_filename(brand, start, end))


__all__ = ["router"]

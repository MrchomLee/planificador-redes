"""CSV import/export of posts (SPEC §14, YouTube columns from report 06 §13.3).

- :func:`template_csv` — ``plantilla.csv``: UTF-8 **with BOM** (Excel opens it with the right accents), ``;``
  separator, the :data:`TEMPLATE_COLUMNS` header and two example rows.
- :func:`build_preview` — decode (UTF-8 with/without BOM, falling back to Windows-1252), detect the separator
  (``;`` ``,`` or tab), normalise the header (case/accents/spaces), parse every row (dates ``AAAA-MM-DD`` or
  ``DD/MM/AAAA``, times ``HH:MM`` in the brand's time zone, ``si``/``no`` flags, formats by key or Spanish
  label,
  library media looked up by file name) and validate it with :func:`planificador.postsvc.validate_post`.
- :func:`import_rows` — create the posts through :func:`planificador.postsvc.create_post` (``source="csv"``):
  ``schedule`` when the row has no errors, otherwise ``draft``.
- :func:`export_csv` — the brand's posts in a date range, same columns (re-importable) plus
  ``primer_comentario_youtube`` (each network keeps its own first comment), ``estado``, ``enlaces`` and
  ``id``.
  ``autopublicar`` is «si»/«no» when all networks agree, empty when each is on its import default (YouTube
  is assisted until unlocked) and otherwise the list of networks that publish automatically.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import config, rules
from planificador.csvsafe import protect_cell, unprotect_cell
from planificador.models import (
    TARGET_STATUS_LABELS,
    Account,
    Brand,
    MediaAsset,
    Post,
    PostKind,
    PostTarget,
    TargetStatus,
)
from planificador.timeutil import (
    Clock,
    ensure_utc,
    from_brand_local,
    local_day_bounds,
    to_brand_local,
)

log = logging.getLogger(__name__)

#: Template columns, in order (SPEC §14).
TEMPLATE_COLUMNS: tuple[str, ...] = (
    "fecha",
    "hora",
    "texto",
    "facebook",
    "instagram",
    "tiktok",
    "youtube",
    "formato_facebook",
    "formato_instagram",
    "formato_tiktok",
    "medios",
    "privacidad_tiktok",
    "primer_comentario",
    "autopublicar",
    "formato_youtube",
    "titulo_youtube",
    "privacidad_youtube",
    "ninos_youtube",
)
#: Optional column written by the export so each network keeps its own first comment on re-import. When the
#: header has it, it is YouTube's first comment (empty = none) and «primer_comentario» is Instagram's only.
YT_FIRST_COMMENT_COLUMN = "primer_comentario_youtube"
#: Extra informative columns written by the export (ignored on import).
EXPORT_EXTRA_COLUMNS: tuple[str, ...] = ("estado", "enlaces", "id")
#: Header of the export (re-importable).
EXPORT_COLUMNS: tuple[str, ...] = (*TEMPLATE_COLUMNS, YT_FIRST_COMMENT_COLUMN, *EXPORT_EXTRA_COLUMNS)
CSV_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube")
#: Optional column ``demo`` (si/no): only read while demo mode is on and the brand has a Demo account, so the
#: import can be tried without real accounts. Not part of the template.
DEMO_COLUMN = "demo"
FORMAT_COLUMNS: dict[str, str] = {net: f"formato_{net}" for net in CSV_NETWORKS}
DEFAULT_FORMATS: dict[str, str] = {
    "facebook": "post",
    "instagram": "post",
    "tiktok": "video",
    "youtube": "video",
    "demo": "post",
}
#: Header aliases (after normalisation) → canonical column.
HEADER_ALIASES: dict[str, str] = {
    "dia": "fecha",
    "date": "fecha",
    "time": "hora",
    "contenido": "texto",
    "mensaje": "texto",
    "text": "texto",
    "descripcion": "texto",
    "medio": "medios",
    "archivos": "medios",
    "archivo": "medios",
    "auto_publicar": "autopublicar",
    "publicacion_automatica": "autopublicar",
    "titulo": "titulo_youtube",
    "ninos": "ninos_youtube",
    "para_ninos_youtube": "ninos_youtube",
    "primer_comentario_instagram": "primer_comentario",
}

TEMPLATE_FILENAME = "plantilla.csv"
TEMPLATE_DELIMITER = ";"
MAX_BYTES = 2_000_000
MAX_ROWS = 500
MEDIA_SEPARATOR = "|"

TRUE_WORDS = frozenset({"si", "s", "yes", "y", "x", "1", "true", "verdadero", "v", "ok"})
FALSE_WORDS = frozenset({"no", "n", "0", "false", "falso", "f"})

#: Spanish/API words → TikTok ``privacy_level``.
TIKTOK_PRIVACY: dict[str, str] = {
    "publico": "PUBLIC_TO_EVERYONE",
    "publica": "PUBLIC_TO_EVERYONE",
    "todos": "PUBLIC_TO_EVERYONE",
    "public": "PUBLIC_TO_EVERYONE",
    "public_to_everyone": "PUBLIC_TO_EVERYONE",
    "amigos": "MUTUAL_FOLLOW_FRIENDS",
    "amigos mutuos": "MUTUAL_FOLLOW_FRIENDS",
    "mutual_follow_friends": "MUTUAL_FOLLOW_FRIENDS",
    "seguidores": "FOLLOWER_OF_CREATOR",
    "follower_of_creator": "FOLLOWER_OF_CREATOR",
    "solo yo": "SELF_ONLY",
    "solo_yo": "SELF_ONLY",
    "privado": "SELF_ONLY",
    "privada": "SELF_ONLY",
    "self_only": "SELF_ONLY",
}
TIKTOK_PRIVACY_EXPORT: dict[str, str] = dict(rules.TT_PRIVACY_LABELS)
YOUTUBE_PRIVACY_EXPORT: dict[str, str] = {"public": "publica", "unlisted": "oculto", "private": "privado"}
#: Extra format words (after folding) per network.
FORMAT_WORDS: dict[str, dict[str, str]] = {
    "facebook": {
        "publicacion": "post",
        "post": "post",
        "foto": "post",
        "fotos": "post",
        "imagen": "post",
        "video": "post",
        "texto": "post",
        "reel": "reel",
        "reels": "reel",
        "historia": "story",
        "historias": "story",
        "story": "story",
        "stories": "story",
    },
    "instagram": {
        "publicacion": "post",
        "post": "post",
        "foto": "post",
        "fotos": "post",
        "imagen": "post",
        "carrusel": "post",
        "carousel": "post",
        "video": "post",
        "reel": "reel",
        "reels": "reel",
        "historia": "story",
        "historias": "story",
        "story": "story",
        "stories": "story",
    },
    "tiktok": {"video": "video", "foto": "photo", "fotos": "photo", "photo": "photo", "carrusel": "photo"},
    "youtube": {"video": "video", "largo": "video", "short": "short", "shorts": "short", "corto": "short"},
}

#: Column help shown on the import page: (column, description, example).
COLUMN_HELP: tuple[tuple[str, str, str], ...] = (
    ("fecha", "Día de publicación (AAAA-MM-DD; también vale DD/MM/AAAA).", "2026-11-03"),
    ("hora", "Hora en la zona horaria de la marca (HH:MM, 24 h).", "10:30"),
    ("texto", "Texto de la publicación (descripción en YouTube). Puede tener varias líneas.", "¡Nuevo menú!"),
    (
        "facebook · instagram · tiktok · youtube",
        "«si» en las redes donde quieras publicar; «no» o vacío en el resto.",
        "si",
    ),
    ("formato_facebook", "post (publicación), reel o historia. Vacío = post.", "post"),
    ("formato_instagram", "post (foto, carrusel o vídeo), reel o historia. Vacío = post.", "reel"),
    ("formato_tiktok", "video (las fotos de TikTok aún no se pueden publicar).", "video"),
    (
        "medios",
        f"Nombres de archivos de la Biblioteca, separados por «{MEDIA_SEPARATOR}».",
        "foto1.jpg|foto2.jpg",
    ),
    ("privacidad_tiktok", "Quién lo ve en TikTok: público, amigos, seguidores o solo yo.", "solo yo"),
    (
        "primer_comentario",
        "Primer comentario automático en Instagram (y en YouTube si no usas «primer_comentario_youtube»).",
        "#cafe #otoño",
    ),
    (
        "autopublicar",
        "«no» = Publicación asistida (te avisamos y publicas tú). Vacío = si (en YouTube, asistida hasta "
        "desbloquearla). Para mezclar, escribe las redes que se publican solas: «facebook, instagram».",
        "si",
    ),
    ("formato_youtube", "video o short. Vacío = video.", "short"),
    (
        "titulo_youtube",
        "Título del vídeo (máx. 100 caracteres). Vacío = primera línea del texto.",
        "Receta de otoño",
    ),
    ("privacidad_youtube", "publica, oculto o privado. Vacío = publica.", "oculto"),
    ("ninos_youtube", "¿Es contenido creado para niños? Obligatorio si youtube = si.", "no"),
    (
        "primer_comentario_youtube",
        "Opcional (la añade la exportación): primer comentario en YouTube; vacío = ninguno. Sin esta "
        "columna, YouTube usa «primer_comentario» (salvo en vídeos para niños).",
        "¡Gracias por verlo!",
    ),
)


# ============================================================================================ helpers


def fold(value: Any) -> str:
    """Lowercase without accents, trimmed (``"Sí "`` → ``"si"``)."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().lower()


def normalize_header(name: str) -> str:
    key = fold(name.replace("﻿", ""))
    key = re.sub(r"[\s\-./]+", "_", key).strip("_")
    return HEADER_ALIASES.get(key, key)


def parse_bool(value: Any) -> bool | None:
    """``si``/``no`` (and friends) → bool; empty → ``None``; anything else raises ``ValueError``."""
    word = fold(value).rstrip(".")
    if not word:
        return None
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ValueError(word)


_DATE_ISO = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DATE_EU = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})$")
_TIME = re.compile(r"^(\d{1,2})(?:[:.hH](\d{2}))?(?::(\d{2}))?\s*(?:h|hrs?)?$")


def parse_date(value: str) -> date:
    text = (value or "").strip()
    text = re.split(r"[ T]", text, maxsplit=1)[0] if re.match(r"^\d{4}-\d{2}-\d{2}[ T]", text) else text
    m = _DATE_ISO.match(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _DATE_EU.match(text)
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000
        return date(year, int(m.group(2)), int(m.group(1)))
    raise ValueError(text)


def parse_time(value: str) -> time:
    text = (value or "").strip().lower()
    m = _TIME.match(text)
    if not m:
        raise ValueError(text)
    hour, minute, second = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
    return time(hour, minute, second)


def _clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # Undo the export's protection against spreadsheet formulas ('=…, '+…, '@…, '-…).
    return unprotect_cell(text)


def decode_csv(raw: bytes) -> tuple[str, str]:
    """Bytes → (text, encoding label). UTF-8 (with or without BOM), else Windows-1252 (old Excel)."""
    try:
        return raw.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "Windows-1252"


def detect_delimiter(text: str) -> str:
    """Pick ``;``, ``,`` or tab from the header line (outside quotes); default ``;``."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    counts = {";": 0, ",": 0, "\t": 0}
    quoted = False
    for ch in first:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in counts:
            counts[ch] += 1
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] else TEMPLATE_DELIMITER


# ============================================================================================ preview


@dataclass
class RowIssue:
    level: str  # "error" | "warning"
    message: str
    network: str | None = None


@dataclass
class ImportRow:
    line: int
    values: dict[str, str]
    empty: bool = False
    scheduled_at: datetime | None = None
    text: str = ""
    networks: list[str] = field(default_factory=list)
    formats: dict[str, str] = field(default_factory=dict)
    media: list[MediaAsset] = field(default_factory=list)
    data: Any = None  # postsvc.PostInput
    issues: list[RowIssue] = field(default_factory=list)
    tiktok_direct: bool = False  # has a TikTok target published directly (needs the music consent)
    day: date | None = None  # parsed "fecha" (even when "hora" is missing or wrong)
    date_problem: bool = False  # "fecha"/"hora" missing or invalid (already reported)

    @property
    def errors(self) -> list[RowIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[RowIssue]:
        return [i for i in self.issues if i.level != "error"]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def action(self) -> str:
        return "draft" if self.has_errors else "schedule"

    def add(self, level: str, message: str, network: str | None = None) -> None:
        if not any(i.message == message and i.network == network for i in self.issues):
            self.issues.append(RowIssue(level, message, network))


@dataclass
class ImportPreview:
    rows: list[ImportRow] = field(default_factory=list)
    delimiter: str = TEMPLATE_DELIMITER
    encoding: str = "UTF-8"
    file_errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def data_rows(self) -> list[ImportRow]:
        return [r for r in self.rows if not r.empty]

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.data_rows if not r.has_errors)

    @property
    def draft_count(self) -> int:
        return sum(1 for r in self.data_rows if r.has_errors)

    @property
    def empty_count(self) -> int:
        return sum(1 for r in self.rows if r.empty)

    @property
    def has_tiktok_direct(self) -> bool:
        return any(r.tiktok_direct for r in self.data_rows)

    @property
    def delimiter_label(self) -> str:
        return {";": "punto y coma", ",": "coma", "\t": "tabulador"}.get(self.delimiter, self.delimiter)


def _brand_accounts(db: Session, brand_id: int) -> dict[str, Account]:
    accounts = db.scalars(select(Account).where(Account.brand_id == brand_id).order_by(Account.id))
    return {str(a.network): a for a in accounts}


def _library(db: Session, brand_id: int) -> list[MediaAsset]:
    stmt = (
        select(MediaAsset)
        .where(MediaAsset.deleted_at.is_(None))
        .where((MediaAsset.brand_id == brand_id) | (MediaAsset.brand_id.is_(None)))
        .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
    )
    return list(db.scalars(stmt))


def find_media(library: list[MediaAsset], name: str) -> tuple[MediaAsset | None, str | None]:
    """Library asset by file name (case/accent-insensitive; also without the extension).
    Returns ``(asset, warning)``; several matches → the most recent one plus a warning."""
    wanted = fold(PurePath(name.replace("\\", "/")).name)
    if not wanted:
        return None, None
    exact = [a for a in library if fold(a.original_name) == wanted]
    if not exact:
        stem = fold(PurePath(wanted).stem) if "." in wanted else wanted
        exact = [a for a in library if fold(PurePath(a.original_name or "").stem) == stem]
    if not exact:
        return None, None
    warning = None
    if len(exact) > 1:
        warning = f"Hay {len(exact)} archivos llamados «{name}» en la Biblioteca: se usará el más reciente."
    return exact[0], warning


def _format_for(network: str, raw: str) -> str | None:
    word = fold(raw)
    if not word:
        return DEFAULT_FORMATS[network]
    for spec in rules.FORMATS.get(network, []):
        if word in (fold(spec.key), fold(spec.label)):
            return spec.key
    return FORMAT_WORDS.get(network, {}).get(word)


def _bool_cell(row: ImportRow, column: str, label: str | None = None) -> bool | None:
    try:
        return parse_bool(row.values.get(column, ""))
    except ValueError:
        shown = row.values.get(column, "")
        row.add("error", f"Valor no válido en «{label or column}»: «{shown}». Escribe «si» o «no».")
        return None


def _auto_cell(row: ImportRow, networks: Iterable[str]) -> tuple[bool | None, frozenset[str] | None]:
    """``autopublicar``: «si»/«no»/empty for every network (``(bool | None, None)``), or the list of networks
    that publish automatically, e.g. «facebook, instagram» (``(None, {...})``; the others are assisted)."""
    raw = row.values.get("autopublicar", "")
    try:
        return parse_bool(raw), None
    except ValueError:
        pass
    allowed = set(networks) | set(CSV_NETWORKS)
    words = [w for w in re.split(r"[\s,;|/+]+", fold(raw)) if w]
    if words and all(w in allowed for w in words):
        return None, frozenset(words)
    row.add(
        "error",
        f"Valor no válido en «autopublicar»: «{raw}». Escribe «si», «no» o las redes que se publican solas "
        "(por ejemplo «facebook, instagram»).",
    )
    return None, None


def _tiktok_privacy(row: ImportRow, account: Account) -> str | None:
    raw = row.values.get("privacidad_tiktok", "")
    word = fold(raw)
    if word:
        value = TIKTOK_PRIVACY.get(word) or TIKTOK_PRIVACY.get(word.replace(" ", "_"))
        if value is None and raw.strip().upper() in rules.TT_PRIVACY_LEVELS:
            value = raw.strip().upper()
        if value is None:
            row.add(
                "error",
                f"Privacidad de TikTok no válida: «{raw}». Usa público, amigos, seguidores o solo yo.",
                "tiktok",
            )
        return value
    info = (account.meta or {}).get("creator_info") or {}
    options = [str(o) for o in info.get("privacy_level_options") or []]
    return options[0] if len(options) == 1 else None


def _youtube_privacy(row: ImportRow) -> str | None:
    raw = row.values.get("privacidad_youtube", "")
    word = fold(raw)
    if not word:
        return "public"
    value = rules.YT_PRIVACY_ALIASES.get(word) or {
        fold(k): v for k, v in rules.YT_PRIVACY_ALIASES.items()
    }.get(word)
    if value is None:
        row.add(
            "error", f"Privacidad de YouTube no válida: «{raw}». Usa publica, oculto o privado.", "youtube"
        )
        return "public"
    return value


def _parse_row(
    row: ImportRow,
    brand: Brand,
    accounts: Mapping[str, Account],
    library: list[MediaAsset],
    tiktok_consent: bool | None,
    demo: bool = False,
) -> None:
    from planificador import postsvc

    v = row.values
    row.text = v.get("texto", "")
    # --- date & time (brand time zone)
    raw_date, raw_time = v.get("fecha", ""), v.get("hora", "")
    day: date | None = None
    at: time | None = None
    if raw_date:
        try:
            day = parse_date(raw_date)
        except ValueError:
            row.add("error", f"Fecha no válida: «{raw_date}». Usa AAAA-MM-DD (por ejemplo 2026-11-03).")
    else:
        row.add("error", "Falta la fecha.")
    if raw_time:
        try:
            at = parse_time(raw_time)
        except ValueError:
            row.add("error", f"Hora no válida: «{raw_time}». Usa HH:MM (por ejemplo 10:30).")
    elif raw_date:
        row.add("error", "Falta la hora.")
    row.day = day
    row.date_problem = day is None or at is None
    if day is not None and at is not None:
        row.scheduled_at = from_brand_local(datetime.combine(day, at), brand.timezone)

    # --- media
    media_ids: list[int] = []
    for name in [n.strip() for n in v.get("medios", "").split(MEDIA_SEPARATOR) if n.strip()]:
        asset, warning = find_media(library, name)
        if asset is None:
            row.add("error", f"No se encuentra «{name}» en la Biblioteca. Súbelo primero o revisa el nombre.")
            continue
        if warning:
            row.add("warning", warning)
        if asset.id not in media_ids:
            media_ids.append(asset.id)
            row.media.append(asset)

    # --- networks
    first_comment = v.get("primer_comentario", "")
    # A file with the YouTube column (our export, the current template) keeps both comments apart.
    yt_column = YT_FIRST_COMMENT_COLUMN in v
    yt_first_comment = v.get(YT_FIRST_COMMENT_COLUMN, "") if yt_column else first_comment
    networks = CSV_NETWORKS + ((DEMO_COLUMN,) if demo and DEMO_COLUMN in accounts else ())
    auto_all, auto_networks = _auto_cell(row, networks)
    targets: list[Any] = []
    for net in networks:
        wanted = _bool_cell(row, net)
        if not wanted:
            if fold(v.get(FORMAT_COLUMNS.get(net, ""), "")) and wanted is None:
                row.add(
                    "warning",
                    f"Hay formato de {rules.network_label(net)} pero la columna «{net}» no dice «si»: "
                    "no se publicará ahí.",
                    net,
                )
            continue
        row.networks.append(net)
        account = accounts.get(net)
        if account is None:
            row.add(
                "error",
                f"Esta marca no tiene una cuenta de {rules.network_label(net)} conectada. Conéctala en "
                f"«Cuentas conectadas» o pon «no» en la columna «{net}».",
                net,
            )
            continue
        raw_format = v.get(FORMAT_COLUMNS.get(net, ""), "")
        fmt = _format_for(net, raw_format)
        if fmt is None:
            valid = ", ".join(f.key for f in rules.enabled_formats(net))
            row.add(
                "error",
                f"Formato de {rules.network_label(net)} no válido: «{raw_format}» (usa {valid}).",
                net,
            )
            fmt = DEFAULT_FORMATS[net]
        row.formats[net] = fmt
        options: dict[str, Any] = {}
        if auto_networks is not None:
            auto_publish = net in auto_networks
        else:
            auto_publish = True if auto_all is None else auto_all
        if net == "instagram" and first_comment:
            if fmt in ("post", "reel"):
                options["first_comment"] = first_comment
            else:
                row.add("warning", "Las historias de Instagram no admiten primer comentario: se ignora.", net)
        if net == "tiktok":
            from planificador.providers import tiktok as tiktok_provider

            if tiktok_provider.account_modes(account) == ("inbox",):
                # No Direct Post permission (video.publish): the video goes to the TikTok app as a draft.
                options["mode"] = "inbox"
                row.add(
                    "warning",
                    "Tu app de TikTok no tiene el permiso de publicación directa (video.publish): el vídeo "
                    "se enviará como borrador a la app de TikTok y la visibilidad se elige allí.",
                    net,
                )
            else:
                row.tiktok_direct = True
                options["mode"] = "direct"
                options["privacy_level"] = _tiktok_privacy(row, account)
                if tiktok_consent is not None:
                    options["consent"] = bool(tiktok_consent)
        if net == "youtube":
            title = v.get("titulo_youtube", "")
            if title:
                options["title"] = title
            options["privacy_status"] = _youtube_privacy(row)
            options["made_for_kids"] = _bool_cell(row, "ninos_youtube")
            if yt_first_comment:
                if options["made_for_kids"] and not yt_column:
                    row.add(
                        "warning",
                        "Los vídeos para niños no admiten comentarios: el primer comentario no se usa en "
                        "YouTube.",
                        net,
                    )
                else:
                    options["first_comment"] = yt_first_comment
            if auto_all is None and auto_networks is None:
                auto_publish = postsvc.default_auto_publish(account)
        targets.append(
            postsvc.TargetInput(account_id=account.id, format=fmt, options=options, auto_publish=auto_publish)
        )
    comment_networks = ("instagram",) if yt_column else ("instagram", "youtube")
    if first_comment and not any(n in comment_networks for n in row.networks):
        where = "Instagram" if yt_column else "Instagram y YouTube"
        row.add("warning", f"El primer comentario solo se usa en {where}: se ignora.")
    if yt_column and yt_first_comment and "youtube" not in row.networks:
        row.add("warning", "Hay primer comentario de YouTube pero no se publica en YouTube: se ignora.")

    row.data = postsvc.PostInput(
        brand_id=brand.id,
        text=row.text,
        media_ids=media_ids,
        scheduled_at=row.scheduled_at,
        targets=targets,
        source="csv",
    )


def _validate_row(db: Session, row: ImportRow, accounts: Mapping[str, Account], clock: Clock) -> None:
    from planificador import postsvc

    if row.data is None:
        return
    by_id = {a.id: str(a.network) for a in accounts.values()}
    # TikTok music consent not asked yet (preview): the confirmation footer asks for it on import.
    consent_pending = {t.account_id for t in row.data.targets if "consent" not in (t.options or {})}
    try:
        result = postsvc.validate_post(db, row.data, clock=clock, action="schedule")
    except Exception:  # pragma: no cover - a validation bug must not break the preview
        log.exception("CSV row validation failed")
        row.add("error", "No se ha podido validar esta fila.")
        return
    for account_id, issues in result.items():
        network = by_id.get(account_id)
        for issue in issues:
            if account_id == postsvc.GENERAL and issue.code == "date_required" and row.date_problem:
                continue
            if issue.code == "tiktok_consent" and account_id in consent_pending:
                continue
            row.add(issue.level, issue.message, network)
    row.issues.sort(key=lambda i: 0 if i.level == "error" else 1)


def read_table(text: str) -> tuple[list[str], list[tuple[int, dict[str, str]]], str]:
    """Parse CSV text → (normalised header, [(line number, {column: value})], delimiter)."""
    delimiter = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header: list[str] | None = None
    rows: list[tuple[int, dict[str, str]]] = []
    for record in reader:
        if header is None:
            if not any(cell.strip() for cell in record):
                continue
            header = [normalize_header(c) for c in record]
            continue
        line = reader.line_num
        values: dict[str, str] = {}
        for i, cell in enumerate(record):
            if i < len(header) and header[i] and header[i] not in values:
                values[header[i]] = _clean_cell(cell)
        rows.append((line, values))
    return header or [], rows, delimiter


def _is_blank_record(values: Mapping[str, str]) -> bool:
    return not any(values.get(c, "") for c in TEMPLATE_COLUMNS)


def build_preview(
    db: Session,
    brand: Brand,
    raw: bytes,
    clock: Clock,
    *,
    tiktok_consent: bool | None = None,
) -> ImportPreview:
    """Parse and validate an uploaded CSV (nothing is written)."""
    preview = ImportPreview()
    if not raw or not raw.strip():
        preview.file_errors.append("El archivo está vacío.")
        return preview
    if len(raw) > MAX_BYTES:
        preview.file_errors.append(f"El archivo es demasiado grande (máximo {MAX_BYTES // 1_000_000} MB).")
        return preview
    text, preview.encoding = decode_csv(raw)
    try:
        header, records, preview.delimiter = read_table(text)
    except csv.Error as exc:
        preview.file_errors.append(f"No se ha podido leer el CSV: {exc}.")
        return preview
    demo = config.is_demo_mode(db)
    known = set(EXPORT_COLUMNS) | ({DEMO_COLUMN} if demo else set())
    if not header or not (set(header) & set(TEMPLATE_COLUMNS)):
        preview.file_errors.append(
            "No se reconocen las columnas. Usa la plantilla: la primera fila debe tener los nombres de las "
            "columnas (fecha, hora, texto, facebook…)."
        )
        return preview
    missing = [c for c in ("fecha", "hora", "texto") if c not in header]
    if missing:
        preview.notices.append("Faltan las columnas: " + ", ".join(missing) + ".")
    if not any(n in header for n in CSV_NETWORKS + ((DEMO_COLUMN,) if demo else ())):
        preview.notices.append("No hay columnas de redes (facebook, instagram, tiktok, youtube).")
    unknown = [c for c in header if c and c not in known]
    if unknown:
        preview.notices.append("Se ignoran las columnas: " + ", ".join(unknown) + ".")
    # Excel keeps ";;;;" lines for rows that once held content or formatting: drop the trailing ones and never
    # count blank rows toward the limit.
    while records and _is_blank_record(records[-1][1]):
        records.pop()
    data_count = sum(1 for _, values in records if not _is_blank_record(values))
    if data_count > MAX_ROWS:
        preview.file_errors.append(f"Demasiadas filas ({data_count}). El máximo es {MAX_ROWS} por archivo.")
        return preview

    accounts = _brand_accounts(db, brand.id)
    library = _library(db, brand.id)
    for line, values in records:
        row = ImportRow(line=line, values=values)
        if _is_blank_record(values):
            row.empty = True
            preview.rows.append(row)
            continue
        _parse_row(row, brand, accounts, library, tiktok_consent, demo)
        _validate_row(db, row, accounts, clock)
        preview.rows.append(row)
    if not preview.data_rows:
        preview.file_errors.append("El archivo no tiene filas con publicaciones.")
    return preview


# ============================================================================================= import


@dataclass
class ImportResult:
    post_ids: list[int] = field(default_factory=list)
    scheduled: int = 0
    drafts: int = 0
    failed: list[tuple[int, str]] = field(default_factory=list)  # (line, message)

    @property
    def created(self) -> int:
        return len(self.post_ids)


def import_rows(
    db: Session,
    brand: Brand,
    raw: bytes,
    clock: Clock,
    *,
    lines: Iterable[int] | None = None,
    tiktok_consent: bool = False,
) -> ImportResult:
    """Re-parse/validate ``raw`` and create one post per selected row (``lines``; ``None`` = all):
    scheduled when valid, otherwise a draft. Never raises for one bad row."""
    from planificador import postsvc

    wanted = set(lines) if lines is not None else None
    preview = build_preview(db, brand, raw, clock, tiktok_consent=tiktok_consent)
    result = ImportResult()
    if preview.file_errors:
        result.failed.extend((0, msg) for msg in preview.file_errors)
        return result
    for row in preview.data_rows:
        if wanted is not None and row.line not in wanted:
            continue
        if row.data is None:  # pragma: no cover - data rows are always parsed
            continue
        action = row.action
        try:
            post = postsvc.create_post(db, row.data, action, clock)
        except postsvc.PostError as exc:
            if action != "schedule":
                result.failed.append((row.line, exc.message))
                continue
            try:
                post = postsvc.create_post(db, row.data, "draft", clock)
                action = "draft"
            except postsvc.PostError as exc2:
                result.failed.append((row.line, exc2.message))
                continue
        result.post_ids.append(post.id)
        if action == "schedule":
            result.scheduled += 1
        else:
            result.drafts += 1
    return result


# ========================================================================================= templates


def _write_csv(header: Iterable[str], rows: Iterable[Iterable[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=TEMPLATE_DELIMITER, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(list(header))
    for row in rows:
        writer.writerow([protect_cell(str(v)) for v in row])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def template_csv(brand: Brand | None, clock: Clock) -> bytes:
    """``plantilla.csv`` (UTF-8 with BOM, ``;``): header + two example rows (next Monday and Tuesday)."""
    tz = brand.timezone if brand is not None else None
    today = (to_brand_local(clock.now(), tz) or ensure_utc(clock.now())).date()
    monday = today + timedelta(days=(7 - today.weekday()) or 7)
    example_1 = {
        "fecha": monday.isoformat(),
        "hora": "10:30",
        "texto": "¡Ya tenemos la carta de otoño! 🍂 Ven a probarla esta semana.",
        "facebook": "si",
        "instagram": "si",
        "tiktok": "no",
        "youtube": "no",
        "formato_facebook": "post",
        "formato_instagram": "post",
        "medios": "carta-otono.jpg",
        "primer_comentario": "#otoño #carta",
        "autopublicar": "si",
    }
    example_2 = {
        "fecha": (monday + timedelta(days=1)).isoformat(),
        "hora": "18:00",
        "texto": "Así preparamos nuestro café de especialidad ☕",
        "facebook": "no",
        "instagram": "si",
        "tiktok": "si",
        "youtube": "si",
        "formato_instagram": "reel",
        "formato_tiktok": "video",
        "medios": "video-cafe.mp4",
        "privacidad_tiktok": "solo yo",
        "autopublicar": "si",
        "formato_youtube": "short",
        "titulo_youtube": "Cómo preparamos el café",
        "privacidad_youtube": "publica",
        "ninos_youtube": "no",
    }
    rows = [[ex.get(c, "") for c in TEMPLATE_COLUMNS] for ex in (example_1, example_2)]
    return _write_csv(TEMPLATE_COLUMNS, rows)


# ============================================================================================= export


def _yes(value: bool) -> str:
    return "si" if value else "no"


def _import_default_auto(target: PostTarget) -> bool:
    """What an empty «autopublicar» gives this target on import."""
    if str(target.network) != "youtube":
        return True
    from planificador import postsvc

    account = target.account
    return postsvc.default_auto_publish(account) if account is not None else False


def _auto_export(targets: list[PostTarget]) -> str:
    """«si»/«no» when every network agrees; empty when each one is on its import default (YouTube is
    assisted until unlocked); otherwise the networks that publish automatically («facebook, instagram»)."""
    if not targets:
        return ""
    autos = [bool(t.auto_publish) for t in targets]
    if all(autos):
        return "si"
    if not any(autos):
        return "no"
    if all(bool(t.auto_publish) == _import_default_auto(t) for t in targets):
        return ""
    return ", ".join(sorted({str(t.network) for t in targets if t.auto_publish}, key=_network_order))


def _network_order(net: str) -> tuple[int, str]:
    order = (*CSV_NETWORKS, DEMO_COLUMN)
    return (order.index(net) if net in order else len(order), net)


def post_row(post: Post, tz: str | None) -> list[str]:
    """One export row (template columns + estado, enlaces, id)."""
    local = to_brand_local(post.scheduled_at, tz) if post.scheduled_at else None
    targets = [t for t in post.targets if t.status != TargetStatus.CANCELLED] or list(post.targets)
    by_net = {str(t.network): t for t in targets}
    values: dict[str, str] = {
        "fecha": local.date().isoformat() if local else "",
        "hora": local.strftime("%H:%M") if local else "",
        "texto": post.text or "",
        "medios": MEDIA_SEPARATOR.join(m.original_name for m in post.media if m is not None),
        "autopublicar": _auto_export(targets),
    }
    for net in CSV_NETWORKS:
        target = by_net.get(net)
        values[net] = _yes(target is not None)
        if target is not None:
            values[FORMAT_COLUMNS[net]] = str(target.format)
    tt = by_net.get("tiktok")
    if tt is not None:
        level = (tt.options or {}).get("privacy_level")
        values["privacidad_tiktok"] = TIKTOK_PRIVACY_EXPORT.get(str(level), "") if level else ""
    ig, yt = by_net.get("instagram"), by_net.get("youtube")
    ig_first = str((ig.options or {}).get("first_comment") or "") if ig else ""
    yt_first = str((yt.options or {}).get("first_comment") or "") if yt else ""
    # Each network keeps its own first comment on re-import (YouTube has its own column).
    values["primer_comentario"] = ig_first if ig is not None else yt_first
    if yt is not None:
        values[YT_FIRST_COMMENT_COLUMN] = yt_first
        opts = yt.options or {}
        values["titulo_youtube"] = str(opts.get("title") or "")
        privacy = rules.YT_PRIVACY_ALIASES.get(str(opts.get("privacy_status") or "").lower(), "")
        values["privacidad_youtube"] = YOUTUBE_PRIVACY_EXPORT.get(privacy, "")
        kids = opts.get("made_for_kids")
        values["ninos_youtube"] = "" if kids is None else _yes(bool(kids))
    status = post.display_status
    values["estado"] = TARGET_STATUS_LABELS.get(status, str(status))
    values["enlaces"] = " | ".join(t.external_url for t in targets if t.external_url)
    values["id"] = str(post.id)
    return [values.get(c, "") for c in EXPORT_COLUMNS]


def export_posts(db: Session, brand: Brand, start: date, end: date) -> list[Post]:
    """Non-deleted, dated posts of ``brand`` whose local date is within [start, end]."""
    begin, _ = local_day_bounds(start, brand.timezone)
    _, finish = local_day_bounds(end, brand.timezone)
    stmt = (
        select(Post)
        .where(
            Post.brand_id == brand.id,
            Post.deleted_at.is_(None),
            Post.kind == PostKind.SCHEDULED,
            Post.scheduled_at.is_not(None),
            Post.scheduled_at >= begin,
            Post.scheduled_at < finish,
        )
        .order_by(Post.scheduled_at, Post.id)
    )
    return list(db.scalars(stmt))


def export_csv(db: Session, brand: Brand, start: date, end: date) -> bytes:
    rows = [post_row(p, brand.timezone) for p in export_posts(db, brand, start, end)]
    return _write_csv(EXPORT_COLUMNS, rows)


def export_filename(brand: Brand, start: date, end: date) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(brand.name)).strip("-") or "marca"
    return f"publicaciones-{slug}-{start.isoformat()}-{end.isoformat()}.csv"


__all__ = [
    "COLUMN_HELP",
    "EXPORT_COLUMNS",
    "EXPORT_EXTRA_COLUMNS",
    "TEMPLATE_COLUMNS",
    "TEMPLATE_FILENAME",
    "ImportPreview",
    "ImportResult",
    "ImportRow",
    "build_preview",
    "decode_csv",
    "detect_delimiter",
    "export_csv",
    "export_filename",
    "export_posts",
    "find_media",
    "import_rows",
    "normalize_header",
    "parse_bool",
    "parse_date",
    "parse_time",
    "post_row",
    "read_table",
    "template_csv",
]

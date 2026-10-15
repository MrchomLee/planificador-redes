"""Jinja2 environment, Spanish formatting filters and the global page context.

Usage from a route::

    from planificador.templating import render

    @router.get("/planificacion")
    def planner(request: Request):
        return render(request, "planner/index.html", page_title="Planificación", posts=posts)

``render`` adds the globals every page needs (``csrf_token``, ``active_brand``, ``brands``,
``unread_notifications``, ``nav_active``, ``app_version``, ``demo_mode``, ``flashes``). Values passed
explicitly by the route win. Database access is failure tolerant: if the database or its tables are not
ready, the page renders with empty globals.

Filters: ``dt_es``, ``date_es``, ``time_es``, ``timeago_es``, ``iso_local``, ``iso_utc``, ``filesize``,
``number_es``, ``pct``, ``css_color``, ``status_label``, ``status_tone``, ``network_label``.
Globals: ``static_url``, ``static_exists``, ``icon``, ``net_icon``, ``net_glyph``, ``STATUS_LABELS``,
``STATUS_TONES``, ``STATUS_ICONS``, ``NETWORK_LABELS``, ``NETWORKS``, ``ACCOUNT_STATUS_LABELS``,
``NOTIFICATION_ICONS``, ``NAV_ITEMS``, ``MENU_ITEMS``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, date, datetime, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jinja2
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.requests import Request

from planificador.models import ACCOUNT_STATUS_LABELS as _ACCOUNT_STATUS_LABELS
from planificador.models import TARGET_STATUS_LABELS as _TARGET_STATUS_LABELS

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
STATIC_PREFIX = "/static/"
LUCIDE_SPRITE = STATIC_DIR / "vendor" / "lucide-static" / "sprite.svg"
LUCIDE_ALIASES = STATIC_DIR / "vendor" / "lucide-static" / "aliases.json"
SIMPLE_ICONS_DIR = STATIC_DIR / "vendor" / "simple-icons"

DEFAULT_TZ = "Europe/Madrid"

# --------------------------------------------------------------------------------------------- labels

DAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
DAYS_SHORT_ES = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
MONTHS_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
MONTHS_SHORT_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov", "dic")

NETWORKS = ("facebook", "instagram", "tiktok", "youtube", "demo")
NETWORK_LABELS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "demo": "Demo",
}
# Web URLs used by "Abrir red" buttons (Publicación asistida).
NETWORK_WEB_URLS = {
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "tiktok": "https://www.tiktok.com/upload",
    "youtube": "https://studio.youtube.com/",
    "demo": None,
}

# TargetStatus -> Spanish label / tone (CSS modifier) / lucide icon. Order = display order in filters.
# Labels come from models (single source shared with ``PostTarget.status_label``).
STATUS_LABELS = {str(k): v for k, v in _TARGET_STATUS_LABELS.items()}
STATUS_TONES = {
    "draft": "muted",
    "scheduled": "primary",
    "publishing": "info",
    "published": "success",
    "manual_pending": "warning",
    "manual_done": "success",
    "sent_to_app": "warning",
    "error": "danger",
    "missed": "danger",
    "unknown": "danger",
    "cancelled": "muted",
}
STATUS_ICONS = {
    "draft": "file-pen-line",
    "scheduled": "clock",
    "publishing": "loader-circle",
    "published": "circle-check",
    "manual_pending": "hand",
    "manual_done": "circle-check",
    "sent_to_app": "smartphone",
    "error": "circle-alert",
    "missed": "clock-alert",
    "unknown": "circle-question-mark",
    "cancelled": "ban",
}
ACCOUNT_STATUS_LABELS = {str(k): v for k, v in _ACCOUNT_STATUS_LABELS.items()}
ACCOUNT_STATUS_TONES = {"connected": "success", "needs_reconnect": "warning", "disconnected": "muted"}
NOTIFICATION_ICONS = {
    "manual_publish": "hand",
    "error": "circle-alert",
    "reconnect": "plug-zap",
    "unknown": "circle-question-mark",
    "missed": "clock-alert",
    "info": "info",
}
NOTIFICATION_TONES = {
    "manual_publish": "warning",
    "error": "danger",
    "reconnect": "warning",
    "unknown": "danger",
    "missed": "danger",
    "info": "info",
}

# Main navigation: (key, label, href, icon).
NAV_ITEMS = (
    ("planificacion", "Planificación", "/planificacion", "calendar-days"),
    ("estadisticas", "Estadísticas", "/estadisticas", "chart-column"),
    ("comentarios", "Comentarios", "/comentarios", "message-circle"),
    ("biblioteca", "Biblioteca", "/biblioteca", "images"),
)
# ☰ menu: (key, label, href, icon).
MENU_ITEMS = (
    ("conexiones", "Cuentas conectadas", "/conexiones", "plug"),
    ("marcas", "Marcas", "/marcas", "building"),
    ("importar", "Importar o exportar CSV", "/importar", "file-spreadsheet"),
    ("ajustes", "Ajustes", "/ajustes", "settings"),
)
# First path segment -> nav key.
_NAV_PREFIXES = {
    "": "planificacion",
    "planificacion": "planificacion",
    "publicaciones": "planificacion",
    "ideas": "planificacion",
    "papelera": "planificacion",
    "objetivos": "planificacion",
    "estadisticas": "estadisticas",
    "comentarios": "comentarios",
    "biblioteca": "biblioteca",
    "medios": "biblioteca",
    "conexiones": "conexiones",
    "marcas": "marcas",
    "importar": "importar",
    "exportar": "importar",
    "ajustes": "ajustes",
    "notificaciones": "notificaciones",
}


def status_label(status: Any) -> str:
    key = str(status or "")
    return STATUS_LABELS.get(key, key.replace("_", " ").capitalize())


def status_tone(status: Any) -> str:
    return STATUS_TONES.get(str(status or ""), "muted")


def network_label(network: Any) -> str:
    key = str(network or "")
    return NETWORK_LABELS.get(key, key.capitalize())


# --------------------------------------------------------------------------------------------- time


def _zone(tz: Any) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    if tz:
        try:
            return ZoneInfo(str(tz))
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("Unknown timezone %r, falling back to %s", tz, DEFAULT_TZ)
    try:
        return ZoneInfo(DEFAULT_TZ)
    except ZoneInfoNotFoundError:  # pragma: no cover - tzdata missing
        return UTC


def _context_tz(ctx: Any) -> Any:
    """Timezone for display: explicit ``tz`` in context, else active brand's, else app default."""
    if ctx is None:
        return None
    tz = ctx.get("tz")
    if tz:
        return tz
    brand = ctx.get("active_brand")
    brand_tz = getattr(brand, "timezone", None) if brand is not None else None
    return brand_tz or ctx.get("app_timezone") or None


def _as_datetime(value: Any) -> datetime | date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime | date):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _localize(value: Any, tz: Any) -> datetime | date | None:
    """Convert to the display timezone. Naive datetimes are treated as UTC (DB convention)."""
    v = _as_datetime(value)
    if v is None or not isinstance(v, datetime):
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.astimezone(_zone(tz))


def _now_in(tz: Any) -> datetime:
    return datetime.now(UTC).astimezone(_zone(tz))


def format_time(v: datetime) -> str:
    return f"{v.hour:02d}:{v.minute:02d}"


def format_date(v: date, fmt: str = "long", *, today: date | None = None) -> str:
    """Spanish date. fmt: long ("lunes 3 de noviembre"), full (with year), short ("3 nov"),
    day ("3 de noviembre"), month ("noviembre de 2026"), numeric ("03/11/2026"), weekday ("lun 3")."""
    today = today or date.today()
    other_year = v.year != today.year
    if fmt == "numeric":
        return f"{v.day:02d}/{v.month:02d}/{v.year}"
    if fmt == "month":
        return f"{MONTHS_ES[v.month - 1]} de {v.year}"
    if fmt == "short":
        s = f"{v.day} {MONTHS_SHORT_ES[v.month - 1]}"
        return f"{s} {v.year}" if other_year else s
    if fmt == "weekday":
        return f"{DAYS_SHORT_ES[v.weekday()]} {v.day}"
    if fmt == "day":
        s = f"{v.day} de {MONTHS_ES[v.month - 1]}"
        return f"{s} de {v.year}" if other_year else s
    s = f"{DAYS_ES[v.weekday()]} {v.day} de {MONTHS_ES[v.month - 1]}"
    if fmt == "full" or other_year:
        s += f" de {v.year}"
    return s


@jinja2.pass_context
def dt_es(ctx: Any, value: Any, tz: Any = None, fmt: str = "long") -> str:
    """'lunes 3 de noviembre, 10:30' in the brand timezone (year added when not the current one).

    fmt: long | full | short ('3 nov, 10:30') | numeric ('03/11/2026 10:30').
    """
    zone = tz or _context_tz(ctx)
    v = _localize(value, zone)
    if v is None:
        return ""
    if not isinstance(v, datetime):
        return format_date(v, fmt)
    today = _now_in(zone).date()
    if fmt == "numeric":
        return f"{format_date(v.date(), 'numeric')} {format_time(v)}"
    return f"{format_date(v.date(), fmt, today=today)}, {format_time(v)}"


@jinja2.pass_context
def date_es(ctx: Any, value: Any, tz: Any = None, fmt: str = "long") -> str:
    zone = tz or _context_tz(ctx)
    v = _localize(value, zone)
    if v is None:
        return ""
    d = v.date() if isinstance(v, datetime) else v
    return format_date(d, fmt, today=_now_in(zone).date())


@jinja2.pass_context
def time_es(ctx: Any, value: Any, tz: Any = None) -> str:
    v = _localize(value, tz or _context_tz(ctx))
    if not isinstance(v, datetime):
        return ""
    return format_time(v)


@jinja2.pass_context
def iso_local(ctx: Any, value: Any, tz: Any = None, seconds: bool = False) -> str:
    """Wall time in the display tz without offset ('2026-11-03T10:30'), for datetime-local inputs."""
    v = _localize(value, tz or _context_tz(ctx))
    if not isinstance(v, datetime):
        return v.isoformat() if isinstance(v, date) else ""
    return v.strftime("%Y-%m-%dT%H:%M:%S" if seconds else "%Y-%m-%dT%H:%M")


def iso_utc(value: Any) -> str:
    """ISO 8601 in UTC with 'Z' (for <time datetime> and JS). Naive values are treated as UTC."""
    v = _as_datetime(value)
    if v is None:
        return ""
    if not isinstance(v, datetime):
        return v.isoformat()
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def timeago_es(value: Any, now: datetime | None = None) -> str:
    """Relative time in Spanish: 'hace un momento', 'hace 5 min', 'hace 3 h', 'ayer', 'en 2 días'...

    Mirrored in app.js (timeAgo) so <time data-timeago> elements stay fresh.
    """
    v = _as_datetime(value)
    if v is None:
        return ""
    if not isinstance(v, datetime):
        v = datetime(v.year, v.month, v.day, tzinfo=UTC)
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    seconds = (now - v).total_seconds()
    future = seconds < 0
    s = abs(seconds)
    minutes = s / 60
    hours = minutes / 60
    days = hours / 24

    def rel(text: str) -> str:
        return f"en {text}" if future else f"hace {text}"

    if s < 45:
        return "en un momento" if future else "hace un momento"
    if minutes < 60:
        return rel(f"{max(1, round(minutes))} min")
    if hours < 24:
        return rel(f"{max(1, round(hours))} h")
    if days < 2:
        return "mañana" if future else "ayer"
    if days < 30:
        return rel(f"{int(days)} días")
    if days < 365:
        months = max(1, int(days // 30))
        return rel("1 mes" if months == 1 else f"{months} meses")
    years = max(1, int(days // 365))
    return rel("1 año" if years == 1 else f"{years} años")


# --------------------------------------------------------------------------------------------- numbers


def _group(value: float, decimals: int) -> str:
    s = f"{value:,.{decimals}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def number_es(value: Any, decimals: int = 0, compact: bool = False) -> str:
    """12345 -> '12.345'; decimals use ','; compact: 12345 -> '12,3 mil', 1234567 -> '1,2 M'."""
    if value is None or value == "":
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if compact:
        a = abs(n)
        if a >= 1_000_000_000:
            return _strip_zero(_group(n / 1_000_000_000, 1)) + " mil M"
        if a >= 1_000_000:
            return _strip_zero(_group(n / 1_000_000, 1)) + " M"
        if a >= 10_000:
            return _strip_zero(_group(n / 1_000, 1)) + " mil"
    if decimals == 0:
        return _group(round(n), 0)
    return _group(n, decimals)


def _strip_zero(s: str) -> str:
    return s[:-2] if s.endswith(",0") else s


def pct(value: Any, decimals: int = 1, signed: bool = False, ratio: bool = False) -> str:
    """Percentage in Spanish style: 12.34 -> '12,3 %'. ``ratio=True`` multiplies by 100 first.
    ``signed=True`` adds '+' / '−' (for period-over-period changes)."""
    if value is None or value == "":
        return "—"
    try:
        n = float(value) * (100 if ratio else 1)
    except (TypeError, ValueError):
        return str(value)
    body = _strip_zero(_group(abs(n), decimals)) if decimals else _group(abs(round(n)), 0)
    if body in ("0", "0,0"):
        return "0 %"
    sign = ""
    if n < 0:
        sign = "−"
    elif signed and n > 0:
        sign = "+"
    return f"{sign}{body} %"


def filesize(value: Any) -> str:
    """Bytes -> '1,5 MB' (binary multiples, Windows-style unit names)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            decimals = 0 if n >= 100 else 1
            return _strip_zero(_group(n, decimals)) + f" {unit}"
    return f"{n} TB"  # pragma: no cover


_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def css_color(value: Any, default: str = "#5b4cf0") -> str:
    """Only hex colours are allowed into inline styles (brand colours are user input)."""
    v = str(value or "").strip()
    return v if _HEX_COLOR_RE.match(v) else default


# --------------------------------------------------------------------------------------------- static


def _static_path(path: str) -> tuple[str, Path | None]:
    rel = str(path).lstrip("/")
    if rel.startswith("static/"):
        rel = rel[len("static/") :]
    candidate = (STATIC_DIR / rel).resolve()
    if STATIC_DIR.resolve() not in candidate.parents:
        return rel, None
    return rel, candidate


def static_url(path: str) -> str:
    """'/static/<path>?v=<mtime>' so browsers refetch a file whenever it changes."""
    rel, file = _static_path(path)
    try:
        version = int(file.stat().st_mtime) if file else None
    except OSError:
        version = None
    url = STATIC_PREFIX + rel
    return f"{url}?v={version}" if version else url


def static_exists(path: str) -> bool:
    _, file = _static_path(path)
    return bool(file and file.is_file())


# --------------------------------------------------------------------------------------------- icons

_SYMBOL_RE = re.compile(r'<symbol id="([^"]+)" viewBox="([^"]+)">(.*?)</symbol>', re.S)
_icon_lock = threading.Lock()
_icons: dict[str, tuple[str, str]] | None = None
_aliases: dict[str, str] = {}
_missing_logged: set[str] = set()


def _load_icons() -> dict[str, tuple[str, str]]:
    global _icons, _aliases
    if _icons is not None:
        return _icons
    with _icon_lock:
        if _icons is not None:
            return _icons
        icons: dict[str, tuple[str, str]] = {}
        try:
            text = LUCIDE_SPRITE.read_text(encoding="utf-8")
            for name, viewbox, inner in _SYMBOL_RE.findall(text):
                icons[name] = (viewbox, re.sub(r"\s*\n\s*", "", inner).strip())
        except OSError:
            log.warning("Lucide sprite not found at %s; run scripts/vendor_assets.py", LUCIDE_SPRITE)
        try:
            _aliases = json.loads(LUCIDE_ALIASES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _aliases = {}
        _icons = icons
        return icons


def _icon_body(name: str) -> tuple[str, str] | None:
    icons = _load_icons()
    found = icons.get(name) or icons.get(_aliases.get(name, ""))
    if found is None and name not in _missing_logged:
        _missing_logged.add(name)
        log.warning("Unknown icon %r", name)
    return found


@lru_cache(maxsize=2048)
def _render_icon(name: str, size: int | str, cls: str, label: str | None, stroke: float | None) -> str:
    body = _icon_body(name) or _icon_body("circle-dashed") or ("0 0 24 24", "")
    viewbox, inner = body
    a11y = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true" focusable="false"'
    classes = f"icon icon-{escape(name)}" + (f" {escape(cls)}" if cls else "")
    return (
        f'<svg class="{classes}" width="{escape(size)}" height="{escape(size)}" viewBox="{viewbox}" '
        f'fill="none" stroke="currentColor" stroke-width="{stroke or 2}" stroke-linecap="round" '
        f'stroke-linejoin="round" {a11y}>{inner}</svg>'
    )


def icon(
    name: str, size: int | str = 18, cls: str = "", label: str | None = None, stroke: float | None = None
) -> Markup:
    """Inline lucide icon as <svg>. ``label`` makes it meaningful (role=img); otherwise aria-hidden."""
    return Markup(_render_icon(str(name), size, cls, label, stroke))


_SI_PATH_RE = re.compile(r'<path d="([^"]+)"')
_SIMPLE_ICON_NETWORKS = ("facebook", "instagram", "tiktok", "youtube")


@lru_cache(maxsize=8)
def _simple_icon_path(network: str) -> str | None:
    try:
        text = (SIMPLE_ICONS_DIR / f"{network}.svg").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _SI_PATH_RE.search(text)
    return m.group(1) if m else None


def _net_svg(network: str, glyph_size: str) -> str:
    path = _simple_icon_path(network) if network in _SIMPLE_ICON_NETWORKS else None
    if path:
        return (
            f'<svg viewBox="0 0 24 24" width="{glyph_size}" height="{glyph_size}" fill="currentColor" '
            f'aria-hidden="true" focusable="false"><path d="{path}"/></svg>'
        )
    name = "flask-conical" if network == "demo" else "globe"
    return _render_icon(name, glyph_size, "", None, 2.25)


@lru_cache(maxsize=256)
def _render_net_icon(network: str, size: int, cls: str, title: bool) -> str:
    label = escape(network_label(network))
    a11y = f'role="img" aria-label="{label}" title="{label}"' if title else 'aria-hidden="true"'
    extra = f" {escape(cls)}" if cls else ""
    glyph = "58%" if network != "demo" else "62%"
    return (
        f'<span class="net-icon net-icon--{escape(network)}{extra}" style="--net-size:{int(size)}px" {a11y}>'
        f"{_net_svg(network, glyph)}</span>"
    )


def net_icon(network: Any, size: int = 24, cls: str = "", title: bool = True) -> Markup:
    """Network logo (simple-icons) in a circle with the network colour. 'demo' uses a lucide flask."""
    return Markup(_render_net_icon(str(network or ""), int(size), cls, title))


def net_glyph(network: Any, size: int = 18, cls: str = "") -> Markup:
    """Bare network logo in the network colour (no circle), e.g. for tabs."""
    net = str(network or "")
    extra = f" {escape(cls)}" if cls else ""
    return Markup(
        f'<span class="net-glyph net-glyph--{escape(net)}{extra}" style="--net-size:{int(size)}px" '
        f'aria-hidden="true">{_net_svg(net, f"{int(size)}px")}</span>'
    )


# --------------------------------------------------------------------------------------------- env

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
    extensions=["jinja2.ext.do"],
)
templates = Jinja2Templates(env=env)

env.filters.update(
    dt_es=dt_es,
    date_es=date_es,
    time_es=time_es,
    iso_local=iso_local,
    iso_utc=iso_utc,
    timeago_es=timeago_es,
    css_color=css_color,
    filesize=filesize,
    number_es=number_es,
    pct=pct,
    status_label=status_label,
    status_tone=status_tone,
    network_label=network_label,
)
env.globals.update(
    static_url=static_url,
    static_exists=static_exists,
    icon=icon,
    net_icon=net_icon,
    net_glyph=net_glyph,
    status_label=status_label,
    status_tone=status_tone,
    network_label=network_label,
    STATUS_LABELS=STATUS_LABELS,
    STATUS_TONES=STATUS_TONES,
    STATUS_ICONS=STATUS_ICONS,
    NETWORKS=NETWORKS,
    NETWORK_LABELS=NETWORK_LABELS,
    NETWORK_WEB_URLS=NETWORK_WEB_URLS,
    ACCOUNT_STATUS_LABELS=ACCOUNT_STATUS_LABELS,
    ACCOUNT_STATUS_TONES=ACCOUNT_STATUS_TONES,
    NOTIFICATION_ICONS=NOTIFICATION_ICONS,
    NOTIFICATION_TONES=NOTIFICATION_TONES,
    NAV_ITEMS=NAV_ITEMS,
    MENU_ITEMS=MENU_ITEMS,
    # Defaults so templates also render through templates.TemplateResponse (e.g. error handlers).
    csrf_token="",
    active_brand=None,
    brands=[],
    brand_networks={},
    unread_notifications=0,
    nav_active="",
    demo_mode=False,
    password_enabled=False,
    notifications_browser=True,
    flashes=[],
    pop_flashes=None,
    app_timezone=DEFAULT_TZ,
)


# --------------------------------------------------------------------------------------------- context


@lru_cache(maxsize=1)
def app_version() -> str:
    try:
        from planificador import __version__  # type: ignore[attr-defined]

        return str(__version__)
    except Exception:  # noqa: BLE001
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version

        for dist in ("planificador-redes", "planificador"):
            try:
                return version(dist)
            except PackageNotFoundError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return "dev"


def nav_key(path: str) -> str:
    segment = path.strip("/").split("/", 1)[0]
    return _NAV_PREFIXES.get(segment, segment)


def _session(request: Request) -> dict | None:
    if "session" not in request.scope:
        return None
    try:
        return request.session
    except AssertionError:  # SessionMiddleware not installed
        return None


def csrf_token_for(request: Request) -> str:
    token = getattr(request.state, "csrf_token", None)
    if not token:
        session = _session(request)
        token = session.get("csrf_token") if session is not None else None
    return token or ""


def flash(request: Request, message: str, kind: str = "info") -> None:
    """Queue a toast shown on the next full page render (kind: info | success | warning | error)."""
    session = _session(request)
    if session is None:
        return
    queued = list(session.get("_flashes", []))
    queued.append({"message": message, "kind": kind})
    session["_flashes"] = queued[-5:]


def _pop_flashes(request: Request) -> list[dict]:
    session = _session(request)
    if session is None:
        return []
    return list(session.pop("_flashes", []) or [])


def toast_headers(message: str, kind: str = "success", **events: Any) -> dict[str, str]:
    """Headers for an htmx response that shows a toast (and optionally triggers other events).

    Example: ``return HTMLResponse(html, headers=toast_headers("Guardado", closeModal=True))``.
    """
    payload: dict[str, Any] = {"toast": {"message": message, "kind": kind}}
    payload.update(events)
    return {"HX-Trigger": json.dumps(payload, ensure_ascii=True)}


def _active_brand_id(request: Request) -> int | None:
    """Active brand id from request.state (deps.get_active_brand) or the session; else None."""
    raw = getattr(request.state, "active_brand_id", None)
    if raw is None:
        session = _session(request)
        if session is not None:
            raw = session.get("active_brand_id", session.get("brand_id"))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _db_context(request: Request) -> dict[str, Any]:
    """Brands, active brand, unread count, demo mode, default tz, password flag. Never raises."""
    out: dict[str, Any] = {
        "brands": [],
        "active_brand": getattr(request.state, "active_brand", None),
        "brand_networks": {},
        "unread_notifications": 0,
        "demo_mode": False,
        "app_timezone": DEFAULT_TZ,
        "notifications_browser": True,
        "password_enabled": bool(getattr(request.state, "password_enabled", False)),
    }
    try:
        from sqlalchemy import func, select

        from planificador import models
        from planificador.db import SessionLocal
    except Exception as exc:  # noqa: BLE001 - backend not available yet
        log.debug("Template globals without DB: %s", exc)
        return out

    try:
        # Prefer the database of the app serving this request; the module-level current database is a
        # fallback for renders outside a mounted app.
        app = request.scope.get("app")
        database = getattr(getattr(app, "state", None), "db", None)
        db = database.session() if database is not None else SessionLocal()
    except Exception as exc:  # noqa: BLE001
        log.debug("Template globals: cannot open session: %s", exc)
        return out

    def attempt(label: str, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - tables may not exist yet
            log.debug("Template globals: %s failed: %s", label, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None

    try:
        brands = attempt(
            "brands",
            lambda: list(db.scalars(select(models.Brand).where(models.Brand.archived_at.is_(None))).all()),
        )
        brands = sorted(brands or [], key=lambda b: (str(getattr(b, "name", "")).casefold(), b.id))

        if out["active_brand"] is None:
            # Same resolution as the ActiveBrand dependency (setting ``active_brand_id``; creates the
            # default "Mi marca" on first run), so pages without that dependency agree with the others.
            active = attempt("active brand", lambda: _ensure_active_brand(db))
            if active is not None:
                if all(b.id != active.id for b in brands):
                    brands = sorted(
                        [*brands, active], key=lambda b: (str(getattr(b, "name", "")).casefold(), b.id)
                    )
                out["active_brand"] = active
            elif brands:
                wanted = _active_brand_id(request)
                out["active_brand"] = next((b for b in brands if str(b.id) == str(wanted)), brands[0])
        out["brands"] = brands

        rows = attempt(
            "brand networks",
            lambda: db.execute(
                select(models.Account.brand_id, models.Account.network).where(
                    models.Account.status != "disconnected"
                )
            ).all(),
        )
        networks: dict[int, list[str]] = {}
        for brand_id, network in rows or []:
            networks.setdefault(brand_id, []).append(str(network))
        out["brand_networks"] = {k: sorted(v, key=_network_order) for k, v in networks.items()}

        unread = attempt(
            "unread notifications",
            lambda: db.scalar(
                select(func.count())
                .select_from(models.Notification)
                .where(models.Notification.read_at.is_(None))
            ),
        )
        out["unread_notifications"] = int(unread or 0)

        def settings() -> tuple[bool, str, bool, bool]:
            return (
                bool(_setting(db, "demo_mode", False)),
                str(_setting(db, "default_timezone", None) or DEFAULT_TZ),
                bool(_setting(db, "notifications_browser", True)),
                bool(_setting(db, "password_hash", None)),
            )

        got = attempt("settings", settings)
        if got:
            out["demo_mode"], out["app_timezone"], out["notifications_browser"], has_password = got
            out["password_enabled"] = out["password_enabled"] or has_password
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _setting(db: Any, key: str, default: Any) -> Any:
    from planificador.config import get_setting

    return get_setting(db, key, default)


def _ensure_active_brand(db: Any) -> Any:
    from planificador.deps import ensure_active_brand

    return ensure_active_brand(db)


def _network_order(network: str) -> int:
    try:
        return NETWORKS.index(network)
    except ValueError:
        return len(NETWORKS)


def page_context(request: Request) -> dict[str, Any]:
    """Globals shared by every full page render."""
    ctx: dict[str, Any] = {
        "request": request,
        "csrf_token": csrf_token_for(request),
        "nav_active": nav_key(request.url.path),
        "app_version": app_version(),
        # Flash messages are popped lazily by base.html, so htmx fragments never swallow them.
        "pop_flashes": lambda: _pop_flashes(request),
    }
    ctx.update(_db_context(request))
    return ctx


def render(
    request: Request,
    name: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    media_type: str | None = None,
    **ctx: Any,
):
    """Render ``name`` with the global page context; keyword arguments override the globals."""
    context = page_context(request)
    context.update(ctx)
    return templates.TemplateResponse(
        request, name, context, status_code=status_code, headers=headers, media_type=media_type
    )


def render_fragment(request: Request, name: str, **ctx: Any) -> str:
    """Render a template to a string (no DB globals) - handy for htmx out-of-band snippets."""
    context = {"request": request, "csrf_token": csrf_token_for(request), **ctx}
    return env.get_template(name).render(context)

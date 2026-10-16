"""Clock abstraction, time zone helpers and Spanish date formatting.

All instants are handled as **aware UTC** datetimes inside the app. Brand-local "wall times" (what the user
sees and picks in the calendar) are converted at the edges with :func:`to_brand_local` and
:func:`from_brand_local`.

Spanish names are hard-coded so formatting never depends on the OS locale (Windows locales are unreliable).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Madrid"
#: Overrides the detected time zone of this computer (IANA name, e.g. ``America/Mexico_City``).
ENV_TIMEZONE = "PLANIFICADOR_TIMEZONE"
#: What servers, containers and unconfigured VMs report: treated as "unknown" (keep the app default).
_UNCONFIGURED_ZONES = frozenset(
    {
        "UTC",
        "Etc/UTC",
        "Etc/UCT",
        "UCT",
        "Etc/Universal",
        "Universal",
        "Etc/Zulu",
        "Zulu",
        "GMT",
        "Etc/GMT",
        "Etc/GMT0",
        "Etc/GMT+0",
        "Etc/GMT-0",
        "GMT0",
        "Etc/Greenwich",
        "Greenwich",
    }
)

DAY_NAMES_ES: tuple[str, ...] = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
DAY_ABBR_ES: tuple[str, ...] = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
MONTH_NAMES_ES: tuple[str, ...] = (
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
MONTH_ABBR_ES: tuple[str, ...] = (
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sept",
    "oct",
    "nov",
    "dic",
)


# --------------------------------------------------------------------------------------------- clocks


@runtime_checkable
class Clock(Protocol):
    """Source of the current instant. ``now()`` always returns an aware UTC datetime."""

    def now(self) -> datetime: ...


class SystemClock:
    """Real wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def __repr__(self) -> str:
        return "SystemClock()"


class FixedClock:
    """Always returns the same instant (handy for pure unit tests)."""

    def __init__(self, at: datetime) -> None:
        self._at = ensure_utc(at)

    def now(self) -> datetime:
        return self._at

    def __repr__(self) -> str:
        return f"FixedClock({self._at.isoformat()})"


class ManualClock:
    """A clock tests move by hand. Thread-safe so a background engine can read it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._lock = threading.Lock()
        self._now = ensure_utc(start) if start is not None else datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, at: datetime) -> datetime:
        with self._lock:
            self._now = ensure_utc(at)
            return self._now

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        """Move forward by ``delta`` or by ``timedelta(**kwargs)`` (e.g. ``advance(minutes=5)``)."""
        step = delta if delta is not None else timedelta(**kwargs)
        with self._lock:
            self._now = self._now + step
            return self._now

    def __repr__(self) -> str:
        return f"ManualClock({self.now().isoformat()})"


def utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------------ time zones


def is_valid_timezone(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return True


def system_timezone() -> str | None:
    """IANA time zone of this computer (the browser runs on the same PC), or ``None`` when unknown.

    Uses ``tzlocal`` (maps Windows zone names such as "Central Standard Time (Mexico)" to IANA). UTC-like
    answers count as unknown (servers/containers). :data:`ENV_TIMEZONE` overrides the detection with an IANA
    name, or turns it off with ``off`` (tests)."""
    override = os.environ.get(ENV_TIMEZONE, "").strip()
    if override:
        if override.lower() in ("off", "no", "0", "false"):
            return None
        if is_valid_timezone(override):
            return override
        log.warning("%s=%r is not a valid time zone; ignoring it", ENV_TIMEZONE, override)
        return None
    try:
        from tzlocal import get_localzone_name

        name = get_localzone_name()
    except Exception as exc:  # noqa: BLE001 - detection is best effort
        log.debug("Could not detect the system time zone: %s", exc)
        return None
    if not name or name in _UNCONFIGURED_ZONES or not is_valid_timezone(name):
        return None
    return str(name)


def get_tz(name: str | tzinfo | None = None) -> tzinfo:
    """Resolve an IANA name to a tzinfo; invalid/empty names fall back to ``Europe/Madrid``."""
    if isinstance(name, tzinfo):
        return name
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            pass
    return ZoneInfo(DEFAULT_TIMEZONE)


def ensure_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime. Naive values are assumed to already be UTC."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_brand_local(dt: datetime | None, tz: str | tzinfo | None) -> datetime | None:
    """UTC (aware, or naive-UTC) instant → aware datetime in the brand time zone."""
    if dt is None:
        return None
    return ensure_utc(dt).astimezone(get_tz(tz))


WallStatus = Literal["ok", "nonexistent", "ambiguous"]


def wall_time_status(wall: datetime, tz: str | tzinfo | None) -> WallStatus:
    """Classify a naive wall time: ``nonexistent`` (DST gap), ``ambiguous`` (DST fold) or ``ok``."""
    zone = get_tz(tz)
    naive = wall.replace(tzinfo=None)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    off0, off1 = first.utcoffset(), second.utcoffset()
    if off0 == off1:
        return "ok"
    # Round-trip through UTC: in a gap neither fold maps back to the same wall time.
    back = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if back != naive:
        return "nonexistent"
    return "ambiguous"


def from_brand_local(wall: datetime, tz: str | tzinfo | None) -> datetime:
    """Brand wall time → aware UTC.

    - Naive input is interpreted as wall time in ``tz``. Aware input is simply converted to UTC.
    - Non-existent times (spring-forward gap, e.g. 02:30 on the last Sunday of March in Madrid) are shifted
      forward by the gap length (02:30 → 03:30 CEST), which is what the user most likely meant.
    - Ambiguous times (autumn fold, e.g. 02:30 on the last Sunday of October) resolve to the **first**
      occurrence (summer time).
    """
    if wall.tzinfo is not None and wall.tzinfo.utcoffset(wall) is not None:
        return wall.astimezone(UTC)
    zone = get_tz(tz)
    # fold=0 picks the first occurrence for ambiguous times; for non-existent times it applies the offset in
    # force *before* the transition, so the resulting instant reads as the wall time pushed forward by the
    # gap.
    return wall.replace(tzinfo=zone, fold=0).astimezone(UTC)


def local_date(dt: datetime, tz: str | tzinfo | None) -> date:
    local = to_brand_local(dt, tz)
    assert local is not None
    return local.date()


def local_day_bounds(day: date, tz: str | tzinfo | None) -> tuple[datetime, datetime]:
    """UTC instants [start, end) covering the local calendar day ``day`` in ``tz`` (DST-safe)."""
    start = from_brand_local(datetime.combine(day, time.min), tz)
    end = from_brand_local(datetime.combine(day + timedelta(days=1), time.min), tz)
    return start, end


def local_wall_iso(dt: datetime | None, tz: str | tzinfo | None) -> str | None:
    """UTC instant → brand wall time ISO string **without offset** (FullCalendar floating time)."""
    local = to_brand_local(dt, tz)
    if local is None:
        return None
    return local.replace(tzinfo=None).isoformat(timespec="seconds")


def parse_wall_iso(value: str) -> datetime:
    """Parse a wall-time ISO string into a naive datetime.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DDTHH:MM[:SS[.fff]]`` and the same with a space separator. Any trailing
    ``Z`` or UTC offset is **ignored**: in the planner FullCalendar runs with ``timeZone: 'UTC'`` and floating
    wall times, so the digits are the brand wall time.
    """
    s = value.strip()
    if not s:
        raise ValueError("Fecha vacía")
    if s.endswith(("Z", "z")):
        s = s[:-1]
    if len(s) > 10:
        # Strip a trailing "+HH:MM" / "-HH:MM" offset (after the time part only).
        for sign in ("+", "-"):
            idx = s.rfind(sign)
            if idx > 10:
                s = s[:idx]
                break
    s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=None)


def combine_local(day: date, hhmm: str | time, tz: str | tzinfo | None) -> datetime:
    """Local date + ``"HH:MM"`` (or a ``time``) in ``tz`` → aware UTC."""
    t = hhmm if isinstance(hhmm, time) else time.fromisoformat(hhmm.strip())
    return from_brand_local(datetime.combine(day, t), tz)


# ---------------------------------------------------------------------------------- Spanish formatting


def _localize(dt: datetime, tz: str | tzinfo | None) -> datetime:
    if tz is None:
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(get_tz(tz))


def format_time_es(dt: datetime | None, tz: str | tzinfo | None = None) -> str:
    """``10:30`` (24 h)."""
    if dt is None:
        return ""
    local = _localize(dt, tz)
    return f"{local.hour:02d}:{local.minute:02d}"


def format_date_es(
    d: date | datetime | None,
    tz: str | tzinfo | None = None,
    *,
    weekday: bool = True,
    year: bool = False,
) -> str:
    """``lunes 3 de noviembre`` (``year=True`` → ``lunes 3 de noviembre de 2026``)."""
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = _localize(d, tz).date()
    text = f"{d.day} de {MONTH_NAMES_ES[d.month - 1]}"
    if year:
        text += f" de {d.year}"
    if weekday:
        text = f"{DAY_NAMES_ES[d.weekday()]} {text}"
    return text


def format_dt_es(dt: datetime | None, tz: str | tzinfo | None = None, *, year: bool = False) -> str:
    """``lunes 3 de noviembre, 10:30`` — ``dt`` is converted to ``tz`` when given (naive = UTC)."""
    if dt is None:
        return ""
    local = _localize(dt, tz)
    return f"{format_date_es(local.date(), weekday=True, year=year)}, {format_time_es(local)}"


def format_short_date_es(d: date | datetime | None, tz: str | tzinfo | None = None) -> str:
    """``3 nov`` (compact, for tables and chart axes)."""
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = _localize(d, tz).date()
    return f"{d.day} {MONTH_ABBR_ES[d.month - 1]}"


def format_short_dt_es(dt: datetime | None, tz: str | tzinfo | None = None) -> str:
    """``3 nov, 10:30``."""
    if dt is None:
        return ""
    local = _localize(dt, tz)
    return f"{format_short_date_es(local.date())}, {format_time_es(local)}"


def format_month_year_es(d: date | datetime) -> str:
    """``noviembre de 2026``."""
    return f"{MONTH_NAMES_ES[d.month - 1]} de {d.year}"


def capitalize_es(text: str) -> str:
    """Capitalize only the first letter (``lunes 3`` → ``Lunes 3``)."""
    return text[:1].upper() + text[1:] if text else text


def format_relative_es(dt: datetime | None, now: datetime) -> str:
    """Human relative time: ``hace 5 min``, ``dentro de 2 h``, ``hace 3 días``, ``ahora mismo``."""
    if dt is None:
        return ""
    delta = (ensure_utc(dt) - ensure_utc(now)).total_seconds()
    future = delta > 0
    secs = abs(delta)
    if secs < 45:
        return "ahora mismo"
    if secs < 3600:
        amount = f"{max(1, round(secs / 60))} min"
    elif secs < 86400:
        amount = f"{round(secs / 3600)} h"
    else:
        days = round(secs / 86400)
        amount = "1 día" if days == 1 else f"{days} días"
    return f"dentro de {amount}" if future else f"hace {amount}"


def format_duration_es(seconds: float | None) -> str:
    """``45 s``, ``1 min 30 s``, ``1 h 5 min``."""
    if seconds is None:
        return ""
    total = int(round(seconds))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"

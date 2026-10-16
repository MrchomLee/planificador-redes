"""Bulk scheduling — "Carga masiva" (many videos, each with its own text and hashtags).

The user drops many short videos, picks the networks and their common options once, lets the app spread the
dates over the calendar and writes one caption per video. Everything is kept server side while they work
(autosave per brand), and "Programar todo" creates one post per video through :mod:`planificador.postsvc`.

Pieces (pure functions unless they take ``db``):

- :func:`distribute` — dates for N posts from a :class:`DistributionSpec`: start day, either *N per day* or
  *one every N days*, a list of wall-clock times (used in turns when a day takes fewer posts than there are
  times), optional weekend skipping, all in the brand's time zone. Times that do not exist (spring-forward
  gap) move forward like everywhere else in the app (:func:`timeutil.from_brand_local`); instants already
  past (or closer than :data:`MIN_LEAD`) are skipped. :func:`schedule_summary` is the mini summary.
- :func:`compose_caption` — row text + row hashtags + "Hashtags comunes" (deduplicated, never repeating a tag
  already in the text). ``static/js/bulk.js`` mirrors it for the live counters.
- :func:`caps_warnings` — daily network limits hit by the distribution (TikTok inbox drafts, TikTok/Instagram
  /Facebook Reels/YouTube API posts per rolling 24 h).
- Batch persistence: settings key ``bulk_batch:<brand_id>`` (:func:`load_batch`, :func:`save_batch`,
  :func:`discard_batch`); :func:`sanitize_batch` is the server-side schema of what the browser sends. A batch
  carries the ``brand_id`` it was made for: :func:`ensure_brand` refuses it (409 ``brand_changed``) once the
  active brand changed in another tab, so it never overwrites another brand's pending batch.
- :func:`check_batch` — per-row validation (:func:`postsvc.validate_post`), issues of the common settings
  (step 2), caps (counting the posts already scheduled on the same accounts) and summary.
  :func:`submit_batch` — "Programar todo" / "Guardar todo como borradores": rows with errors are saved as
  drafts; the batch token is **claimed once** (settings key ``bulk_submit:<token>``, INSERT … ON CONFLICT DO
  NOTHING) so a double click or a resubmit never creates duplicates: the second request gets the stored
  result. A submit cut short (app closed or crashed, unexpected error) is resumed by the next one, skipping
  the rows it had already created (:func:`claim_is_stale`, :func:`interrupted_submission`).

The TikTok music-usage consent is never stored with the batch: it is given (again) when scheduling.
"""

from __future__ import annotations

import copy
import logging
import re
import secrets
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import PurePath
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from planificador import config, rules
from planificador.models import (
    Account,
    AccountStatus,
    Brand,
    MediaAsset,
    Network,
    Post,
    PostSource,
    PostTarget,
    Setting,
    TargetStatus,
)
from planificador.providers.base import Issue
from planificador.timeutil import (
    Clock,
    ensure_utc,
    format_date_es,
    format_dt_es,
    format_short_date_es,
    from_brand_local,
    to_brand_local,
    utcnow,
)

log = logging.getLogger(__name__)

# ======================================================================================= constants

VERSION = 1
MAX_ROWS = 200
MAX_TEXT = 65_000  # Facebook's caption limit is the longest (rules.FB_TEXT_MAX)
MAX_TITLE = 200
MAX_TAGS = 60
MAX_TAG_LEN = 80
MAX_SLOTS = 12
MAX_PER_DAY = 12
MAX_EVERY_DAYS = 30
MAX_DAYS_AHEAD = 3 * 366
MAX_OPTION_STR = 2_000
MAX_LIST_ITEMS = 50
#: The first distributed post is at least this far from now (time to review and save).
MIN_LEAD = timedelta(minutes=10)
DEFAULT_SLOTS = ("12:00", "19:00")
#: Settings keys.
BATCH_KEY = "bulk_batch:{brand_id}"
SUBMIT_KEY = "bulk_submit:{token}"
LAST_KEY = "bulk_last:{brand_id}"
SUBMIT_RETENTION = timedelta(days=30)
#: A submit that made no progress for this long was abandoned: the next one resumes it.
STALE_CLAIM = timedelta(minutes=10)
#: This process. The app runs as a single instance (``__main__.InstanceLock``), so a submit claimed under
#: another boot id was cut short by a crash or a restart and is resumed at once (no :data:`STALE_CLAIM` wait).
BOOT_ID = secrets.token_hex(8)
TITLE_SOURCES = ("text", "filename")
MODES = ("per_day", "every_days")

#: Option fields set once for the whole batch (step 2): their issues are reported once, in the settings
#: card, and block "Programar todo" (every row would fail the same way).
COMMON_FIELDS = frozenset(
    {
        "account",
        "auto_publish",
        "mode",
        "privacy_level",
        "consent",
        "commercial_content",
        "brand_content_toggle",
        "brand_organic_toggle",
        "disable_comment",
        "disable_duet",
        "disable_stitch",
        "made_for_kids",
        "privacy_status",
        "category_id",
        "native_schedule",
    }
)
#: Composer-only option keys kept with the batch besides each format's defaults.
_EXTRA_OPTION_KEYS = frozenset({"commercial_content"})
#: Never stored (given again when scheduling).
_UNSTORED_OPTION_KEYS = frozenset({"consent", "title", "thumbnail_media_id"})

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ROW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_KEY_RE = re.compile(r"^(?:\d{1,12}|demo)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")
_HASHTAG_IN_TEXT = re.compile(r"(?<![\w&])#(\w+)")
_TAG_SPLIT = re.compile(r"[\s,;]+")
_TAG_INVALID = re.compile(r"[^\w]", re.UNICODE)


class BatchError(ValueError):
    """Invalid batch/distribution input. ``message`` is Spanish and user-facing."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid",
        status_code: int = 422,
        report: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.report = report  # check_batch() result when the common settings block scheduling


def new_token() -> str:
    return secrets.token_urlsafe(18)


# ======================================================================================== hashtags


def normalize_hashtags(value: Any, *, limit: int = MAX_TAGS) -> list[str]:
    """``"#viaje, #Playa verano"`` (or a list) → ``["viaje", "Playa", "verano"]``: without ``#``, only word
    characters (``#hola-mundo`` → ``holamundo``), case-insensitively unique, at most ``limit``."""
    if value is None or value == "":
        return []
    items: Iterable[Any] = value if isinstance(value, list | tuple) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str | int):
            continue
        for part in _TAG_SPLIT.split(str(item)):
            tag = _TAG_INVALID.sub("", unicodedata.normalize("NFC", part.lstrip("#")))[:MAX_TAG_LEN]
            if not tag or tag.lower() in seen:
                continue
            seen.add(tag.lower())
            out.append(tag)
            if len(out) >= limit:
                return out
    return out


def hashtag_line(tags: Iterable[str]) -> str:
    return " ".join(f"#{t}" for t in tags)


def compose_caption(text: str | None, hashtags: Any = None, common_hashtags: Any = None) -> str:
    """Final caption of a row: its text, then the row's hashtags followed by the common ones — tags already
    written in the text (any case) or repeated are left out. After a one-line text the tags follow on the
    same line (a line break the user never typed would make TikTok warn about joined lines); after a text of
    several lines they go below a blank line."""
    base = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    present = {t.lower() for t in _HASHTAG_IN_TEXT.findall(base)}
    tags: list[str] = []
    for tag in normalize_hashtags(hashtags, limit=MAX_TAGS) + normalize_hashtags(common_hashtags):
        if tag.lower() in present:
            continue
        present.add(tag.lower())
        tags.append(tag)
    if not tags:
        return base
    line = hashtag_line(tags)
    if not base.strip():
        return line
    return f"{base}\n\n{line}" if "\n" in base else f"{base} {line}"


def clean_filename(name: str | None) -> str:
    """``"mi_video-final_02.mp4"`` → ``"mi video final 02"`` (file name as a text or title)."""
    stem = PurePath(str(name or "")).stem if name else ""
    text = re.sub(r"[_\-.]+", " ", stem)
    return re.sub(r"\s+", " ", text).strip()


def first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        clean = line.replace("<", "").replace(">", "").strip()
        if clean:
            return clean
    return ""


def youtube_title(row: Mapping[str, Any], source: str, media_name: str | None) -> str:
    """Title of a row's YouTube video: the one typed in the row, else per ``source`` the first line of the
    text (``"text"``, falling back to the file name when there is no text) or the file name."""
    explicit = str(row.get("yt_title") or "").replace("<", "").replace(">", "").strip()
    if explicit:
        return explicit
    name = clean_filename(media_name)
    if source == "filename":
        title = name or first_line(row.get("text"))
    else:
        title = first_line(row.get("text")) or name
    return title[: rules.YT_TITLE_MAX]


# ==================================================================================== distribution


@dataclass(frozen=True)
class DistributionSpec:
    start_date: date
    mode: str = "per_day"  # per_day | every_days
    per_day: int = 1
    every_days: int = 1
    slots: tuple[time, ...] = (time(12, 0), time(19, 0))
    skip_weekends: bool = False


@dataclass(frozen=True)
class SlotPick:
    day: date  # brand-local date actually used
    time: time  # brand-local wall time actually used (after a DST gap shift)
    when: datetime  # aware UTC
    shifted: bool = False  # the requested wall time did not exist (spring-forward gap)
    requested: time | None = None

    @property
    def date_iso(self) -> str:
        return self.day.isoformat()

    @property
    def time_hm(self) -> str:
        return f"{self.time.hour:02d}:{self.time.minute:02d}"


def parse_slot(value: Any) -> time:
    """``"9"``, ``"9:05"``, ``"19.30"``, ``"19h"`` → ``time`` (minutes only)."""
    text = str(value or "").strip().lower().replace(".", ":").replace("h", "")
    m = _TIME_RE.match(text)
    if not m:
        raise BatchError(f"La hora «{value}» no es válida. Escríbela como 12:00.", code="slot_invalid")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise BatchError(f"La hora «{value}» no es válida. Escríbela como 12:00.", code="slot_invalid")
    return time(hour, minute)


def parse_slots(values: Any) -> tuple[time, ...]:
    items = values if isinstance(values, list | tuple) else re.split(r"[,;\s]+", str(values or ""))
    out = sorted({parse_slot(v) for v in items if str(v).strip()})
    if len(out) > MAX_SLOTS:
        raise BatchError(f"Puedes usar como máximo {MAX_SLOTS} horas distintas.", code="slots_too_many")
    return tuple(out)


def _int_in(value: Any, default: int, low: int, high: int, message: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BatchError(message, code="number_invalid") from None
    if number < low or number > high:
        raise BatchError(message, code="number_invalid")
    return number


def parse_spec(raw: Mapping[str, Any], *, today: date) -> DistributionSpec:
    """Distribution settings sent by the browser → :class:`DistributionSpec` (Spanish :class:`BatchError`)."""
    start_raw = str(raw.get("start_date") or "").strip()
    if start_raw:
        try:
            start = date.fromisoformat(start_raw[:10])
        except ValueError:
            raise BatchError("La fecha de inicio no es válida.", code="start_invalid") from None
    else:
        start = today
    if start < today:
        start = today
    mode = str(raw.get("mode") or "per_day")
    if mode not in MODES:
        raise BatchError("Elige cómo repartir las publicaciones.", code="mode_invalid")
    per_day = _int_in(
        raw.get("per_day"),
        1,
        1,
        MAX_PER_DAY,
        f"Las publicaciones por día deben ser un número entre 1 y {MAX_PER_DAY}.",
    )
    every = _int_in(
        raw.get("every_days"),
        1,
        1,
        MAX_EVERY_DAYS,
        f"Los días entre publicaciones deben ser un número entre 1 y {MAX_EVERY_DAYS}.",
    )
    slots = parse_slots(raw.get("slots") or [])
    if not slots:
        raise BatchError("Añade al menos una hora de publicación.", code="slots_required")
    if mode == "per_day" and per_day > len(slots):
        raise BatchError(
            f"Para {per_day} publicaciones al día añade al menos {per_day} horas distintas.",
            code="slots_short",
        )
    return DistributionSpec(
        start_date=start,
        mode=mode,
        per_day=per_day,
        every_days=every,
        slots=slots,
        skip_weekends=bool(raw.get("skip_weekends")),
    )


def distribute(
    count: int,
    spec: DistributionSpec,
    tz: str,
    now: datetime,
    *,
    min_lead: timedelta = MIN_LEAD,
) -> list[SlotPick]:
    """``count`` publishing instants following ``spec`` in the brand time zone ``tz``.

    Publishing days start at ``spec.start_date``: every day (``per_day``) or every ``every_days`` days
    (one post each), skipping Saturdays and Sundays when asked (an "every N days" day that falls on a
    weekend moves to Monday and the cadence continues from there). Each day takes the next ``per_day``
    times of ``spec.slots`` in turns (fewer posts per day than times → the times alternate). Instants
    before ``now + min_lead`` are skipped; a wall time inside a DST gap moves forward (``shifted``)."""
    if count <= 0:
        return []
    slots = sorted(set(spec.slots))
    if not slots:
        raise BatchError("Añade al menos una hora de publicación.", code="slots_required")
    per_day = spec.per_day if spec.mode == "per_day" else 1
    if per_day > len(slots):
        raise BatchError(
            f"Para {per_day} publicaciones al día añade al menos {per_day} horas distintas.",
            code="slots_short",
        )
    step = 1 if spec.mode == "per_day" else max(1, spec.every_days)
    earliest = ensure_utc(now) + min_lead
    limit_day = spec.start_date + timedelta(days=MAX_DAYS_AHEAD)
    picks: list[SlotPick] = []
    seen: set[datetime] = set()
    cursor = 0
    day = spec.start_date
    while len(picks) < count:
        if day > limit_day:
            raise BatchError(
                "Con esta frecuencia las publicaciones irían a más de 3 años vista. Publica más a menudo.",
                code="too_far",
            )
        if spec.skip_weekends and day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        chosen = sorted(slots[(cursor + i) % len(slots)] for i in range(per_day))
        cursor += per_day
        for wall in chosen:
            requested = datetime.combine(day, wall)
            when = from_brand_local(requested, tz)
            if when < earliest or when in seen:
                continue
            local = to_brand_local(when, tz)
            assert local is not None
            actual = local.replace(tzinfo=None, second=0, microsecond=0)
            seen.add(when)
            picks.append(
                SlotPick(
                    day=actual.date(),
                    time=actual.time(),
                    when=when,
                    shifted=actual != requested,
                    requested=wall,
                )
            )
            if len(picks) >= count:
                break
        day += timedelta(days=step)
    return picks


def pick_json(pick: SlotPick, tz: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "date": pick.date_iso,
        "time": pick.time_hm,
        "utc": pick.when.isoformat(timespec="seconds"),
        "label": format_dt_es(pick.when, tz),
    }
    if pick.shifted and pick.requested is not None:
        out["note"] = (
            f"El {format_date_es(pick.day, weekday=False)} a las {pick.requested.strftime('%H:%M')} "
            f"no existe por el cambio de hora: se usa {pick.time_hm}."
        )
    return out


def parse_row_when(row: Mapping[str, Any], tz: str) -> datetime | None:
    """A row's ``date`` + ``time`` (brand wall time) → aware UTC, or ``None`` when incomplete/invalid."""
    day_s, time_s = str(row.get("date") or ""), str(row.get("time") or "")
    if not _DATE_RE.match(day_s) or not time_s:
        return None
    try:
        day = date.fromisoformat(day_s)
        wall = parse_slot(time_s)
    except (ValueError, BatchError):
        return None
    return from_brand_local(datetime.combine(day, wall), tz)


def schedule_summary(instants: Iterable[datetime | None], tz: str) -> dict[str, Any]:
    """Mini summary of the dates of a batch: first/last instant, number of days, posts per day
    (``undated`` counts the ``None`` items)."""
    items = list(instants)
    dated = sorted(ensure_utc(w) for w in items if w is not None)
    undated = sum(1 for w in items if w is None)
    per_day: dict[date, int] = {}
    for when in dated:
        local = to_brand_local(when, tz)
        assert local is not None
        per_day[local.date()] = per_day.get(local.date(), 0) + 1
    days = [
        {
            "date": d.isoformat(),
            "label": format_short_date_es(d),
            "weekday": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")[d.weekday()],
            "weekend": d.weekday() >= 5,
            "count": n,
        }
        for d, n in sorted(per_day.items())
    ]
    return {
        "count": len(dated),
        "undated": undated,
        "first": dated[0].isoformat(timespec="seconds") if dated else None,
        "last": dated[-1].isoformat(timespec="seconds") if dated else None,
        "first_label": format_dt_es(dated[0], tz, year=True) if dated else None,
        "last_label": format_dt_es(dated[-1], tz, year=True) if dated else None,
        "first_date": days[0]["date"] if days else None,
        "last_date": days[-1]["date"] if days else None,
        "days": len(days),
        "max_per_day": max((d["count"] for d in days), default=0),
        "per_day": days,
    }


def suggested_slots(matrix: Any, *, count: int = 2, min_gap: int = 3) -> list[str]:
    """Best hours of a 7×24 best-times matrix (weekday average), at least ``min_gap`` hours apart, sorted."""
    try:
        rows = [[float(v) for v in row] for row in matrix][:7]
        if len(rows) < 5 or any(len(r) != 24 for r in rows):
            raise ValueError
    except (TypeError, ValueError):
        return list(DEFAULT_SLOTS)
    weekdays = rows[:5]
    score = [sum(r[h] for r in weekdays) / len(weekdays) for h in range(24)]
    chosen: list[int] = []
    for hour in sorted(range(7, 24), key=lambda h: (-score[h], h)):
        if all(abs(hour - c) >= min_gap for c in chosen):
            chosen.append(hour)
        if len(chosen) >= count:
            break
    return [f"{h:02d}:00" for h in sorted(chosen)] or list(DEFAULT_SLOTS)


# ============================================================================================ caps


@dataclass(frozen=True)
class CapEntry:
    """One post that a network will receive at ``when`` (from one row × one target)."""

    account_key: str
    network: str
    when: datetime
    format: str = ""
    mode: str = "direct"  # TikTok: direct | inbox
    auto: bool = True  # the app sends it itself (auto-publish, or TikTok inbox)
    account_label: str = ""
    existing: bool = False  # already scheduled/sent before this batch (not one of its rows)


@dataclass(frozen=True)
class CapRule:
    code: str
    network: str
    limit: int
    window: timedelta
    message: str  # format fields: {count}, {limit}, {start}, {end}, {account}

    def applies(self, entry: CapEntry) -> bool:
        if entry.network != self.network or not entry.auto:
            return False
        if self.code == "tiktok_inbox":
            return entry.mode == "inbox"
        if self.code == "tiktok_direct":
            return entry.mode != "inbox"
        if self.code == "facebook_reels":
            return entry.format == "reel"
        return True


DAY = timedelta(hours=24)
#: Daily network limits (research 03 §6/§9, 02 §4.6/§9, 06 §9). Warnings only: the networks decide.
CAP_RULES: tuple[CapRule, ...] = (
    CapRule(
        "tiktok_inbox",
        "tiktok",
        5,
        DAY,
        "TikTok solo admite 5 borradores pendientes en la bandeja de la app cada 24 h y vas a enviar "
        "{count} entre el {start} y el {end}. Termina cada borrador en la app antes de que llegue el "
        "siguiente o repártelos en más días.",
    ),
    CapRule(
        "tiktok_direct",
        "tiktok",
        15,
        DAY,
        "TikTok permite unas {limit} publicaciones al día por cuenta desde aplicaciones y vas a publicar "
        "{count} entre el {start} y el {end}: las que pasen del límite fallarán. Repártelas en más días.",
    ),
    CapRule(
        "instagram",
        "instagram",
        50,
        DAY,
        "Instagram limita las publicaciones automáticas por cuenta cada 24 h y vas a publicar {count} "
        "entre el {start} y el {end}. Repártelas en más días para no superar el límite.",
    ),
    CapRule(
        "facebook_reels",
        "facebook",
        30,
        DAY,
        "Facebook admite como máximo 30 reels publicados desde aplicaciones cada 24 h por página y vas a "
        "publicar {count} entre el {start} y el {end}. Repártelos en más días.",
    ),
    CapRule(
        "youtube_uploads",
        "youtube",
        10,
        DAY,
        "Vas a subir {count} vídeos a YouTube entre el {start} y el {end}. YouTube limita las subidas "
        "diarias de cada canal (no publica la cifra y es más baja en canales nuevos o sin verificar) y tu "
        "proyecto de Google admite unas 100 subidas al día: si YouTube rechaza alguna, tendrás que "
        "reprogramarla.",
    ),
)


def caps_warnings(entries: Iterable[CapEntry], tz: str) -> list[dict[str, Any]]:
    """Daily limits exceeded in any rolling window: one warning per account and rule, for the busiest
    window (``count`` posts between ``start`` and ``end``). Entries marked ``existing`` (posts scheduled or
    sent before this batch) count towards the limit, but a window needs at least one row of the batch to warn
    (``existing`` in the warning says how many of the ``count`` were there already)."""
    groups: dict[tuple[str, str], list[CapEntry]] = {}
    for entry in entries:
        for rule in CAP_RULES:
            if rule.applies(entry):
                groups.setdefault((entry.account_key, rule.code), []).append(entry)
    rules_by_code = {r.code: r for r in CAP_RULES}
    out: list[dict[str, Any]] = []
    for (account_key, code), items in groups.items():
        rule = rules_by_code[code]
        if len(items) <= rule.limit or all(e.existing for e in items):
            continue
        ordered = sorted(items, key=lambda e: ensure_utc(e.when))
        times = [ensure_utc(e.when) for e in ordered]
        old = [0]  # old[k]: existing entries among the first k
        for e in ordered:
            old.append(old[-1] + (1 if e.existing else 0))
        best = (0, 0, 0)  # count, i, j
        i = 0
        for j, when in enumerate(times):
            while when - times[i] >= rule.window:
                i += 1
            count = j - i + 1
            if count > best[0] and old[j + 1] - old[i] < count:  # the window has a row of this batch
                best = (count, i, j)
        count, i, j = best
        if count <= rule.limit:
            continue
        start, end = times[i], times[j]
        existing = old[j + 1] - old[i]
        new_items = [e for e in ordered if not e.existing]
        label = (new_items or items)[0].account_label or rules.network_label(rule.network)
        message = rule.message.format(
            count=count,
            limit=rule.limit,
            start=format_dt_es(start, tz),
            end=format_dt_es(end, tz),
            account=label,
        )
        if existing:
            message += (
                " Incluye 1 publicación que ya tenías programada o enviada en esas horas."
                if existing == 1
                else f" Incluye {existing} publicaciones que ya tenías programadas o enviadas en esas horas."
            )
        out.append(
            {
                "level": "warning",
                "code": f"cap_{code}",
                "network": rule.network,
                "network_label": rules.network_label(rule.network),
                "account_key": account_key,
                "account_label": label,
                "count": count,
                "existing": existing,
                "limit": rule.limit,
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "message": message,
            }
        )
    order = {r.code: n for n, r in enumerate(CAP_RULES)}
    out.sort(key=lambda w: (order.get(w["code"][4:], 99), w["account_key"]))
    return out


# =================================================================================== batch schema


def empty_batch(token: str | None = None, *, brand_id: int | None = None) -> dict[str, Any]:
    return {
        "version": VERSION,
        "token": token or new_token(),
        "brand_id": brand_id,
        "step": 1,
        "accounts": {},
        "common_hashtags": [],
        "yt_title_source": "text",
        "distribution": {
            "start_date": "",
            "mode": "per_day",
            "per_day": 2,
            "every_days": 1,
            "slots": list(DEFAULT_SLOTS),
            "skip_weekends": False,
        },
        "rows": [],
    }


def _str(value: Any, limit: int, *, what: str = "texto") -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise BatchError(f"El {what} no es válido.")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > limit:
        raise BatchError(f"El {what} es demasiado largo (máximo {limit:,} caracteres).".replace(",", "."))
    return text


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "si", "sí", "on", "yes")


def _option_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:MAX_OPTION_STR]
    if isinstance(value, list | tuple):
        return [str(v)[:MAX_OPTION_STR] for v in list(value)[:MAX_LIST_ITEMS] if isinstance(v, str | int)]
    return None


def _clean_options(network: str | None, fmt: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    allowed: set[str] | None = None
    if network:
        allowed = set(rules.default_options(network, fmt)) | _EXTRA_OPTION_KEYS
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or len(key) > 40 or key in _UNSTORED_OPTION_KEYS:
            continue
        if key.startswith("demo_"):
            if network not in (None, Network.DEMO):
                continue
        elif allowed is not None and key not in allowed:
            continue
        out[key] = _option_value(value)
    return out


def sanitize_batch(raw: Any, *, networks: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The browser's batch → a clean dict (types, sizes, known keys only). ``networks`` maps account keys
    to networks (to keep only each format's option keys). Raises :class:`BatchError`."""
    if not isinstance(raw, Mapping):
        raise BatchError("Los datos de la carga masiva no son válidos.")
    token = str(raw.get("token") or "")
    if not _TOKEN_RE.match(token):
        raise BatchError("Falta el identificador de la carga masiva. Recarga la página.", code="token")
    batch = empty_batch(token, brand_id=batch_brand_id(raw))
    try:
        batch["step"] = max(1, min(4, int(raw.get("step") or 1)))
    except (TypeError, ValueError):
        batch["step"] = 1

    accounts: dict[str, Any] = {}
    raw_accounts = raw.get("accounts") or {}
    if not isinstance(raw_accounts, Mapping):
        raise BatchError("La lista de redes no es válida.")
    for key, value in list(raw_accounts.items())[:20]:
        key = str(key)
        if not _KEY_RE.match(key) or not isinstance(value, Mapping):
            continue
        network = (networks or {}).get(key)
        fmt = str(value.get("format") or "")[:16]
        if network and rules.format_spec(network, fmt) is None:
            fmt = (rules.enabled_formats(network) or rules.FORMATS[network])[0].key
        accounts[key] = {
            "selected": _bool(value.get("selected")),
            "format": fmt,
            "auto_publish": _bool(value.get("auto_publish"), True),
            "options": _clean_options(network, fmt, value.get("options")),
        }
    batch["accounts"] = accounts
    batch["common_hashtags"] = normalize_hashtags(raw.get("common_hashtags"))
    source = str(raw.get("yt_title_source") or "text")
    batch["yt_title_source"] = source if source in TITLE_SOURCES else "text"

    dist = raw.get("distribution") or {}
    if isinstance(dist, Mapping):
        target = batch["distribution"]
        start = str(dist.get("start_date") or "")[:10]
        target["start_date"] = start if _DATE_RE.match(start) else ""
        target["mode"] = dist.get("mode") if dist.get("mode") in MODES else "per_day"
        for key, high in (("per_day", MAX_PER_DAY), ("every_days", MAX_EVERY_DAYS)):
            try:
                target[key] = max(1, min(high, int(dist.get(key) or 1)))
            except (TypeError, ValueError):
                target[key] = 1
        slots: list[str] = []
        for value in list(dist.get("slots") or [])[:MAX_SLOTS] if isinstance(dist.get("slots"), list) else []:
            try:
                hm = parse_slot(value).strftime("%H:%M")
            except BatchError:
                continue
            if hm not in slots:
                slots.append(hm)
        target["slots"] = sorted(slots)
        target["skip_weekends"] = _bool(dist.get("skip_weekends"))

    raw_rows = raw.get("rows") or []
    if not isinstance(raw_rows, list):
        raise BatchError("La lista de vídeos no es válida.")
    if len(raw_rows) > MAX_ROWS:
        raise BatchError(
            f"Una carga masiva admite como máximo {MAX_ROWS} vídeos. Divídela en varias.", code="too_many"
        )
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_rows):
        if not isinstance(item, Mapping):
            raise BatchError("La lista de vídeos no es válida.")
        row_id = str(item.get("id") or "")
        if not _ROW_ID_RE.match(row_id) or row_id in seen_ids:
            row_id = f"r{index + 1}x{secrets.token_hex(3)}"
        seen_ids.add(row_id)
        try:
            media_id = int(item.get("media_id"))
        except (TypeError, ValueError):
            raise BatchError("Uno de los vídeos de la lista no es válido.") from None
        day = str(item.get("date") or "")[:10]
        hm = str(item.get("time") or "")[:5]
        try:
            hm = parse_slot(hm).strftime("%H:%M") if hm else ""
        except BatchError:
            hm = ""
        nets_raw = item.get("networks") or {}
        nets = (
            {str(k): bool(v) for k, v in nets_raw.items() if _KEY_RE.match(str(k))}
            if isinstance(nets_raw, Mapping)
            else {}
        )
        rows.append(
            {
                "id": row_id,
                "media_id": media_id,
                "text": _str(item.get("text"), MAX_TEXT, what="texto de un vídeo"),
                "hashtags": normalize_hashtags(item.get("hashtags")),
                "yt_title": _str(item.get("yt_title"), MAX_TITLE, what="título de YouTube"),
                "date": day if _DATE_RE.match(day) else "",
                "time": hm,
                "networks": nets,
            }
        )
    batch["rows"] = rows
    return batch


def batch_brand_id(raw: Any) -> int | None:
    """The ``brand_id`` a batch (or a request body) was made for, ``None`` when missing or invalid."""
    value = raw.get("brand_id") if isinstance(raw, Mapping) else None
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def ensure_brand(raw: Any, brand: Brand) -> None:
    """Refuse a batch or request made for another brand than the active one (the brand was switched in another
    tab): 409 ``brand_changed``, so a stale tab never writes into, checks against or schedules for the other
    brand. Bodies without ``brand_id`` are accepted (pages opened before it existed)."""
    brand_id = batch_brand_id(raw)
    if brand_id is not None and brand_id != brand.id:
        raise BatchError(
            f"La marca activa ha cambiado a «{brand.name}» (en otra pestaña) y esta carga masiva es de otra "
            "marca. Recarga la página para seguir.",
            code="brand_changed",
            status_code=409,
        )


# ===================================================================================== persistence


def batch_key(brand_id: int) -> str:
    return BATCH_KEY.format(brand_id=int(brand_id))


def submit_key(token: str) -> str:
    return SUBMIT_KEY.format(token=token)


def load_batch(db: Session, brand_id: int) -> dict[str, Any] | None:
    value = config.get_setting(db, batch_key(brand_id), None)
    if not isinstance(value, dict) or not value.get("token"):
        return None
    return value


def save_batch(
    db: Session, brand_id: int, batch: dict[str, Any], clock: Clock | None = None
) -> dict[str, Any]:
    """Store ``batch`` (already sanitized) as the brand's pending batch. Refused once its token has been
    scheduled (a late autosave must not bring a scheduled batch back); while a submit runs or after one was
    interrupted it is still stored (resuming skips the rows already created)."""
    token = str(batch.get("token") or "")
    state = submission_state(db, token)
    if state is not None and state.get("status") == "done":
        raise BatchError(
            "Esta carga masiva ya se ha programado. Recarga la página para empezar otra.",
            code="submitted",
            status_code=409,
        )
    previous = load_batch(db, brand_id) or {}
    stored = dict(batch)
    stored["brand_id"] = int(brand_id)
    now = ensure_utc(clock.now()) if clock is not None else utcnow()
    stored["updated_at"] = now.isoformat(timespec="seconds")
    stored["created_at"] = (
        previous.get("created_at")
        if previous.get("token") == token and previous.get("created_at")
        else stored["updated_at"]
    )
    stored["revision"] = int(previous.get("revision") or 0) + 1
    config.set_setting(db, batch_key(brand_id), stored)
    return stored


def discard_batch(db: Session, brand_id: int) -> None:
    config.delete_setting(db, batch_key(brand_id))


def submission_state(db: Session, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    value = config.get_setting(db, submit_key(token), None)
    return value if isinstance(value, dict) else None


def claim_is_stale(state: Mapping[str, Any], updated_at: datetime | None, now: datetime) -> bool:
    """Whether an unfinished submit can be taken over and resumed: it was ``interrupted`` (an unexpected
    error), claimed by an earlier process (:data:`BOOT_ID`: the app was closed or crashed mid-way) or made no
    progress for :data:`STALE_CLAIM`."""
    status = state.get("status")
    if status == "interrupted":
        return True
    if status != "running":
        return False
    if state.get("boot") != BOOT_ID:
        return True
    return updated_at is None or ensure_utc(now) - ensure_utc(updated_at) >= STALE_CLAIM


def interrupted_submission(db: Session, token: str, now: datetime) -> dict[str, Any] | None:
    """State of the unfinished submit of ``token`` that the next "Programar todo" resumes, else ``None``."""
    if not token:
        return None
    row = db.get(Setting, submit_key(token))
    if row is None:
        return None
    db.refresh(row)
    if not isinstance(row.value, dict) or not claim_is_stale(row.value, row.updated_at, now):
        return None
    return dict(row.value)


def claim_submission(db: Session, token: str, brand_id: int, now: datetime) -> tuple[bool, dict[str, Any]]:
    """Atomically take ``token`` for submitting. Returns ``(True, state)`` when this call owns it — a fresh
    claim, or the takeover of an unfinished one (:func:`claim_is_stale`; its ``done`` rows are skipped) — else
    ``(False, state)`` with the state of the earlier submit (``running`` or ``done`` with its ``result``)."""
    key = submit_key(token)
    value = {
        "status": "running",
        "brand_id": int(brand_id),
        "boot": BOOT_ID,
        "started_at": now.isoformat(timespec="seconds"),
        "done": {},
    }
    stmt = (
        sqlite_insert(Setting)
        .values(key=key, value=value, updated_at=ensure_utc(now))
        .on_conflict_do_nothing(index_elements=[Setting.key])
    )
    inserted = db.execute(stmt).rowcount == 1
    db.commit()
    if inserted:
        return True, value
    row = db.get(Setting, key)
    if row is not None:
        db.refresh(row)
    state = dict(row.value) if row is not None and isinstance(row.value, dict) else {"status": "running"}
    if row is not None and row.updated_at is not None and claim_is_stale(state, row.updated_at, now):
        previous_at = ensure_utc(row.updated_at)
        # The takeover changes the timestamp it checks, so two concurrent takeovers cannot both win.
        taken_at = max(ensure_utc(now), previous_at + timedelta(microseconds=1))
        state.update(status="running", boot=BOOT_ID, resumed_at=now.isoformat(timespec="seconds"))
        state.setdefault("done", {})
        taken = db.execute(
            update(Setting)
            .where(Setting.key == key, Setting.updated_at == row.updated_at)
            .values(value=state, updated_at=taken_at)
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if taken == 1:
            return True, state
        state = submission_state(db, token) or state
    return False, state


def _interrupt_submission(db: Session, token: str, state: Mapping[str, Any]) -> None:
    """An unexpected error stopped a submit: mark it ``interrupted`` (with the rows created so far) so the
    next attempt resumes it at once."""
    try:
        db.rollback()
        value = copy.deepcopy(dict(state))
        value["status"] = "interrupted"
        db.execute(
            update(Setting)
            .where(Setting.key == submit_key(token))
            .values(value=value)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    except Exception:  # noqa: BLE001 - the stale-claim timeout still frees it
        db.rollback()
        log.exception("Could not mark bulk submit %s as interrupted", token)


def _record_progress(db: Session, token: str, state: dict[str, Any], now: datetime) -> None:
    """Store the rows created so far in the claim (same transaction as the row's post: never lost)."""
    db.execute(
        update(Setting)
        .where(Setting.key == submit_key(token))
        .values(value=copy.deepcopy(state), updated_at=ensure_utc(now))
        .execution_options(synchronize_session=False)
    )


def _finish_submission(db: Session, token: str, brand_id: int, result: dict[str, Any], now: datetime) -> None:
    db.execute(
        update(Setting)
        .where(Setting.key == submit_key(token))
        .values(
            value={
                "status": "done",
                "brand_id": int(brand_id),
                "finished_at": now.isoformat(timespec="seconds"),
                "result": result,
            },
            updated_at=ensure_utc(now),
        )
        .execution_options(synchronize_session=False)
    )
    db.execute(
        delete(Setting).where(
            Setting.key.like("bulk_submit:%"),
            Setting.updated_at < ensure_utc(now) - SUBMIT_RETENTION,
        )
    )
    db.commit()


# ======================================================================================== accounts


@dataclass
class BatchAccount:
    key: str
    account: Account | None  # None: the Demo account does not exist yet (created when saving)
    network: str
    label: str
    format: str
    auto_publish: bool
    options: dict[str, Any]
    modes: tuple[str, ...] = ("direct", "inbox")

    @property
    def mode(self) -> str:
        if self.network != Network.TIKTOK:
            return "direct"
        mode = "inbox" if str(self.options.get("mode") or "direct") == "inbox" else "direct"
        if self.modes and mode not in self.modes:
            return self.modes[0]
        return mode

    @property
    def sends_itself(self) -> bool:
        return self.auto_publish or (self.network == Network.TIKTOK and self.mode == "inbox")


def tiktok_modes(account: Account | None) -> tuple[str, ...]:
    if account is None:
        return ("direct", "inbox")
    try:
        from planificador.providers import tiktok

        return tuple(tiktok.available_modes(tiktok.granted_scopes(account)))
    except Exception:  # pragma: no cover - provider unavailable
        return ("direct", "inbox")


def brand_accounts(db: Session, brand: Brand) -> dict[str, Account]:
    """The brand's accounts by key (the account id, as in the composer). The Demo account only with demo mode
    on; it is also reachable as ``"demo"`` (the composer's key while it does not exist yet)."""
    demo_mode = config.is_demo_mode(db)
    out: dict[str, Account] = {}
    for account in db.scalars(select(Account).where(Account.brand_id == brand.id).order_by(Account.id)):
        if account.network == Network.DEMO:
            if not demo_mode:
                continue
            out.setdefault("demo", account)
        out[str(account.id)] = account
    return out


def remap_demo_key(db: Session, brand: Brand, batch: dict[str, Any]) -> dict[str, Any]:
    """A batch started before the Demo account existed uses the composer's pseudo key ``"demo"``: once the
    account exists (created by the first validation) its settings and per-row choices move to its id."""
    accounts = batch.get("accounts") or {}
    if "demo" not in accounts:
        return batch
    demo = brand_accounts(db, brand).get("demo")
    if demo is None or demo.id is None:
        return batch
    key = str(demo.id)
    conf = accounts.pop("demo")
    accounts.setdefault(key, conf)
    for row in batch.get("rows") or []:
        nets = row.get("networks") or {}
        if "demo" in nets:
            nets.setdefault(key, nets.pop("demo"))
    return batch


def account_networks(db: Session, brand: Brand) -> dict[str, str]:
    nets = {k: str(a.network) for k, a in brand_accounts(db, brand).items()}
    if config.is_demo_mode(db):
        nets.setdefault("demo", Network.DEMO.value)
    return nets


def selected_accounts(
    db: Session, brand: Brand, batch: Mapping[str, Any], *, create_demo: bool = False
) -> tuple[list[BatchAccount], list[dict[str, Any]]]:
    """Accounts chosen in step 2 (in network order) + issues about the ones that cannot be used."""
    available = brand_accounts(db, brand)
    demo_mode = config.is_demo_mode(db)
    issues: list[dict[str, Any]] = []
    out: list[BatchAccount] = []
    for key, conf in (batch.get("accounts") or {}).items():
        if not conf.get("selected"):
            continue
        account = available.get(key)
        if key == "demo" or (account is not None and account.network == Network.DEMO):
            if not demo_mode:
                issues.append(_issue("error", "demo_mode_off", "Activa el modo prueba para usar Demo.", None))
                continue
            if account is None and create_demo:
                from planificador import postsvc

                account = postsvc.ensure_demo_account(db, brand.id)
            network = Network.DEMO.value
        else:
            if account is None:
                stale = db.get(Account, int(key)) if key.isdigit() else None
                if stale is not None and stale.brand_id == brand.id and stale.network == Network.DEMO:
                    issues.append(
                        _issue("error", "demo_mode_off", "Activa el modo prueba para usar Demo.", None)
                    )
                    continue
                issues.append(
                    _issue(
                        "error",
                        "account_missing",
                        "Una de las cuentas elegidas ya no existe en esta marca: quítala en el paso 2.",
                        None,
                    )
                )
                continue
            network = str(account.network)
            if account.status == AccountStatus.DISCONNECTED:
                issues.append(
                    _issue(
                        "error",
                        "account_disconnected",
                        f"La cuenta de {rules.network_label(network)} está desconectada. Vuelve a "
                        "conectarla en «Cuentas conectadas» o quítala en el paso 2.",
                        network,
                    )
                )
                continue
        fmt = str(conf.get("format") or "")
        if rules.format_spec(network, fmt) is None or rules.disabled_reason(network, fmt):
            fmt = rules.enabled_formats(network)[0].key
        label = rules.network_label(network)
        if account is not None and account.network != Network.DEMO:
            label = f"{label} · {account.display_name}"
        out.append(
            BatchAccount(
                key=key,
                account=account,
                network=network,
                label=label,
                format=fmt,
                auto_publish=bool(conf.get("auto_publish", True)),
                options=dict(conf.get("options") or {}),
                modes=tiktok_modes(account) if network == Network.TIKTOK else ("direct", "inbox"),
            )
        )
    order = {n: i for i, n in enumerate(rules.NETWORKS)}
    out.sort(key=lambda a: (order.get(a.network, 99), a.key))
    return out, issues


def _issue(level: str, code: str, message: str, network: str | None, field: str | None = None) -> dict:
    return {
        "level": level,
        "code": code,
        "message": message,
        "field": field,
        "network": network,
        "network_label": rules.network_label(network) if network else None,
    }


# ======================================================================================= row → post


def media_names(db: Session, brand: Brand, rows: Iterable[Mapping[str, Any]]) -> dict[int, MediaAsset]:
    out: dict[int, MediaAsset] = {}
    for row in rows:
        media_id = int(row.get("media_id") or 0)
        if media_id in out:
            continue
        asset = db.get(MediaAsset, media_id)
        if asset is not None and asset.deleted_at is None and asset.brand_id in (None, brand.id):
            out[media_id] = asset
    return out


def row_accounts(row: Mapping[str, Any], accounts: list[BatchAccount]) -> list[BatchAccount]:
    nets = row.get("networks") or {}
    return [a for a in accounts if nets.get(a.key, True) is not False]


def row_post_input(
    brand: Brand,
    batch: Mapping[str, Any],
    row: Mapping[str, Any],
    accounts: list[BatchAccount],
    media: Mapping[int, MediaAsset],
    *,
    tiktok_consent: bool,
):
    """:class:`postsvc.PostInput` of one row (``source="bulk"``). Accounts not created yet (Demo) are
    skipped — :func:`selected_accounts` with ``create_demo=True`` creates them first."""
    from planificador import postsvc

    tz = brand.timezone or "Europe/Madrid"
    asset = media.get(int(row.get("media_id") or 0))
    caption = compose_caption(row.get("text"), row.get("hashtags"), batch.get("common_hashtags"))
    targets: list[postsvc.TargetInput] = []
    for acc in row_accounts(row, accounts):
        if acc.account is None or acc.account.id is None:
            continue
        options = copy.deepcopy(acc.options)
        if acc.network == Network.TIKTOK:
            options["mode"] = acc.mode
            if acc.mode == "direct":
                options["consent"] = bool(tiktok_consent)
        if acc.network == Network.YOUTUBE:
            options["title"] = youtube_title(
                row, str(batch.get("yt_title_source") or "text"), asset.original_name if asset else None
            )
        targets.append(
            postsvc.TargetInput(
                account_id=acc.account.id,
                format=acc.format,
                options=options,
                auto_publish=acc.auto_publish,
            )
        )
    return postsvc.PostInput(
        brand_id=brand.id,
        text=caption,
        media_ids=[int(row.get("media_id") or 0)],
        scheduled_at=parse_row_when(row, tz),
        targets=targets,
        source=PostSource.BULK.value,
    )


def _issue_json(issue: Issue, account: BatchAccount | None) -> dict[str, Any]:
    out = _issue(issue.level, issue.code, issue.message, account.network if account else None, issue.field)
    out["account_key"] = account.key if account else None
    return out


@dataclass
class RowCheck:
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "error" if self.errors else ("warning" if self.warnings else "ok")

    def as_json(self) -> dict[str, Any]:
        return {"status": self.status, "errors": self.errors, "warnings": self.warnings}


def _validate_row(
    db: Session,
    brand: Brand,
    batch: Mapping[str, Any],
    row: Mapping[str, Any],
    accounts: list[BatchAccount],
    media: Mapping[int, MediaAsset],
    clock: Clock,
    *,
    tiktok_consent: bool,
    common: dict[tuple[str | None, str], dict[str, Any]],
) -> RowCheck:
    """Row issues (``row_post_input`` + :func:`postsvc.validate_post`); issues of the common settings go to
    ``common`` instead (once per account and code)."""
    from planificador import postsvc

    check = RowCheck()
    if int(row.get("media_id") or 0) not in media:
        check.errors.append(
            _issue(
                "error",
                "media_missing",
                "Este vídeo ya no está en la biblioteca: quita la fila o vuelve a subirlo.",
                None,
                "media",
            )
        )
    data = row_post_input(brand, batch, row, accounts, media, tiktok_consent=tiktok_consent)
    by_id = {a.account.id: a for a in accounts if a.account is not None and a.account.id is not None}
    result = postsvc.validate_post(db, data, clock=clock, action="schedule")
    for key, issues in result.items():
        acc = by_id.get(key) if key != postsvc.GENERAL else None
        for issue in issues:
            if issue.code == "media_missing" and check.errors:
                continue
            if issue.code == "no_targets" and not accounts:
                continue  # reported once: "Elige al menos una red en el paso 2"
            if issue.code == "tiktok_consent" and not tiktok_consent:
                continue  # asked once for the whole batch (see check_batch)
            entry = _issue_json(issue, acc)
            if acc is not None and issue.field in COMMON_FIELDS:
                common.setdefault((acc.key, issue.code), entry)
                continue
            (check.errors if issue.level == "error" else check.warnings).append(entry)
    return check


def consent_needed(accounts: Iterable[BatchAccount]) -> bool:
    """Whether some selected TikTok account publishes directly (needs TikTok's music-usage consent)."""
    return any(a.network == Network.TIKTOK and a.mode == "direct" for a in accounts)


def cap_entries(batch: Mapping[str, Any], accounts: list[BatchAccount], tz: str) -> list[CapEntry]:
    entries: list[CapEntry] = []
    for row in batch.get("rows") or []:
        when = parse_row_when(row, tz)
        if when is None:
            continue
        for acc in row_accounts(row, accounts):
            entries.append(
                CapEntry(
                    account_key=acc.key,
                    network=acc.network,
                    when=when,
                    format=acc.format,
                    mode=acc.mode,
                    auto=acc.sends_itself,
                    account_label=acc.label,
                )
            )
    return entries


#: Targets already on their way (or sent) that use the same daily quota as the rows of a batch.
_PENDING_STATUSES = (TargetStatus.SCHEDULED, TargetStatus.PUBLISHING)
_SENT_STATUSES = (TargetStatus.PUBLISHED, TargetStatus.SENT_TO_APP)


def existing_cap_entries(
    db: Session,
    accounts: list[BatchAccount],
    instants: Iterable[datetime],
    *,
    exclude_post_ids: Iterable[int] = (),
) -> list[CapEntry]:
    """Posts the selected accounts already have scheduled (or sent) within 24 h of the batch's first and last
    instant — they count towards the same daily limits (same mode, format and "sends itself" rules as the rows
    of the batch). ``exclude_post_ids``: posts created by this very batch (an interrupted submit)."""
    dated = sorted(ensure_utc(w) for w in instants)
    by_id = {a.account.id: a for a in accounts if a.account is not None and a.account.id is not None}
    if not dated or not by_id:
        return []
    low, high = dated[0] - DAY, dated[-1] + DAY
    excluded = {int(i) for i in exclude_post_ids}
    query = (
        select(PostTarget)
        .join(Post, Post.id == PostTarget.post_id)
        .where(
            PostTarget.account_id.in_(list(by_id)),
            Post.deleted_at.is_(None),
            or_(
                and_(
                    PostTarget.status.in_(_PENDING_STATUSES),
                    PostTarget.scheduled_at >= low,
                    PostTarget.scheduled_at <= high,
                ),
                and_(
                    PostTarget.status.in_(_SENT_STATUSES),
                    or_(
                        and_(PostTarget.published_at >= low, PostTarget.published_at <= high),
                        and_(
                            PostTarget.published_at.is_(None),
                            PostTarget.scheduled_at >= low,
                            PostTarget.scheduled_at <= high,
                        ),
                    ),
                ),
            ),
        )
    )
    entries: list[CapEntry] = []
    for target in db.scalars(query):
        if target.post_id in excluded:
            continue
        acc = by_id[target.account_id]
        sent = target.status in _SENT_STATUSES
        when = (target.published_at or target.scheduled_at) if sent else target.scheduled_at
        if when is None:
            continue
        network = str(target.network)
        options = target.options or {}
        inbox = network == Network.TIKTOK and str(options.get("mode") or "") == "inbox"
        mode = "inbox" if inbox else "direct"
        entries.append(
            CapEntry(
                account_key=acc.key,
                network=network,
                when=ensure_utc(when),
                format=str(target.format or ""),
                mode=mode,
                auto=bool(target.auto_publish) or inbox,
                account_label=acc.label,
                existing=True,
            )
        )
    return entries


def check_batch(
    db: Session,
    brand: Brand,
    batch: Mapping[str, Any],
    clock: Clock,
    *,
    tiktok_consent: bool = False,
    row_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Everything the page shows about a batch: per-row issues (``row_ids`` limits which rows are validated),
    issues of the common settings, daily-limit warnings, the date summary and what blocks "Programar todo"."""
    tz = brand.timezone or "Europe/Madrid"
    # Like the composer's validation, a Demo account chosen in demo mode is created here.
    accounts, account_issues = selected_accounts(db, brand, batch, create_demo=True)
    rows = list(batch.get("rows") or [])
    media = media_names(db, brand, rows)
    wanted = set(row_ids) if row_ids is not None else None
    if wanted is not None and rows and not wanted & {r["id"] for r in rows}:
        wanted.add(rows[0]["id"])  # the common-settings issues come from validating at least one row
    common: dict[tuple[str | None, str], dict[str, Any]] = {}
    checks: dict[str, Any] = {}
    for row in rows:
        if wanted is not None and row["id"] not in wanted:
            continue
        checks[row["id"]] = _validate_row(
            db, brand, batch, row, accounts, media, clock, tiktok_consent=tiktok_consent, common=common
        ).as_json()
    common_errors = [i for i in account_issues if i["level"] == "error"]
    common_errors += [i for i in common.values() if i["level"] == "error"]
    common_warnings = [i for i in account_issues if i["level"] != "error"]
    common_warnings += [i for i in common.values() if i["level"] != "error"]
    needs_consent = consent_needed(accounts)
    if not accounts and not account_issues:
        common_errors.insert(0, _issue("error", "no_networks", "Elige al menos una red en el paso 2.", None))
    if needs_consent and not tiktok_consent:
        common_errors.append(
            _issue(
                "error",
                "tiktok_consent",
                "Para publicar directamente en TikTok acepta la Confirmación de uso de música de TikTok "
                "(casilla junto a «Programar todo» o en el paso 2).",
                Network.TIKTOK.value,
                "consent",
            )
        )
    instants = [parse_row_when(r, tz) for r in rows]
    entries = cap_entries(batch, accounts, tz)
    done_posts = _done_post_ids(db, str(batch.get("token") or ""))
    entries += existing_cap_entries(db, accounts, [e.when for e in entries], exclude_post_ids=done_posts)
    summary = schedule_summary([w for w in instants if w is not None], tz)
    summary["undated"] = sum(1 for w in instants if w is None)
    auto_rows = sum(
        1
        for row, when in zip(rows, instants, strict=True)
        if when is not None and any(a.sends_itself for a in row_accounts(row, accounts))
    )
    return {
        "ok": True,
        "rows": checks,
        "common": {"errors": common_errors, "warnings": common_warnings},
        "caps": caps_warnings(entries, tz),
        "summary": summary,
        "consent_needed": needs_consent,
        "auto_rows": auto_rows,
        "can_schedule": bool(rows) and not common_errors,
    }


# ========================================================================================== submit


def _done_post_ids(db: Session, token: str) -> list[int]:
    """Posts already created by an unfinished submit of ``token``."""
    state = submission_state(db, token)
    if state is None or state.get("status") == "done":
        return []
    out: list[int] = []
    for entry in (state.get("done") or {}).values():
        if isinstance(entry, Mapping) and isinstance(entry.get("post_id"), int):
            out.append(entry["post_id"])
    return out


def _when_label(post: Any, tz: str) -> str:
    return format_dt_es(post.scheduled_at, tz) if post.scheduled_at else "Sin fecha"


def submit_batch(
    db: Session,
    brand: Brand,
    batch: dict[str, Any],
    action: str,
    clock: Clock,
    *,
    tiktok_consent: bool = False,
) -> dict[str, Any]:
    """Create one post per row. ``action`` ``schedule``: rows without errors are scheduled, the others saved
    as drafts (with the reasons); ``draft``: every row is a draft. Idempotent per batch token (a repeated
    submit returns the first result with ``duplicate: true``). Raises :class:`BatchError` for a batch that
    cannot be scheduled at all (no rows, common settings with errors, already being saved). A submit that was
    cut short (see :func:`claim_is_stale`) is resumed: the rows it created are reported, not created again."""
    if action not in ("schedule", "draft"):
        raise BatchError("Acción no válida.", code="invalid_action")
    token = str(batch.get("token") or "")
    previous = submission_state(db, token)
    if previous is not None and previous.get("status") == "done":
        return _previous_result(previous)
    rows = list(batch.get("rows") or [])
    # An interrupted submit may have created every row already: the page then only asks to finish it.
    resuming = previous is not None and bool(previous.get("done"))
    if not rows and not resuming:
        raise BatchError("Añade al menos un vídeo antes de guardar.", code="no_rows")
    tz = brand.timezone or "Europe/Madrid"
    report = check_batch(db, brand, batch, clock, tiktok_consent=tiktok_consent)
    if action == "schedule" and report["common"]["errors"]:
        errors = report["common"]["errors"]
        message = (
            errors[0]["message"]
            if len(errors) == 1
            else (f"Hay {len(errors)} problemas en los ajustes comunes (paso 2) que impiden programar.")
        )
        raise BatchError(message, code="common_errors", report=report)

    now = ensure_utc(clock.now())
    owned, state = claim_submission(db, token, brand.id, now)
    if not owned:
        return _previous_result(state)

    try:
        posts, failures = _create_rows(
            db, brand, batch, rows, action, report, state, clock, tiktok_consent=tiktok_consent
        )
    except Exception:
        _interrupt_submission(db, token, state)
        raise
    _wake_scheduler()

    dated = sorted(p["when"] for p in posts if p["when"])
    first_local = to_brand_local(datetime.fromisoformat(dated[0]), tz) if dated else None
    last_local = to_brand_local(datetime.fromisoformat(dated[-1]), tz) if dated else None
    links = {"calendar": "/planificacion", "list": "/planificacion/lista"}
    if first_local is not None and last_local is not None:
        links["calendar"] = f"/planificacion?fecha={first_local.date().isoformat()}"
        links["list"] = (
            f"/planificacion/lista?desde={first_local.date().isoformat()}&hasta={last_local.date().isoformat()}"
        )
    result = {
        "ok": True,
        "action": action,
        "token": token,
        "scheduled": sum(1 for p in posts if p["status"] == "scheduled"),
        "drafts": sum(1 for p in posts if p["status"] == "draft"),
        "failed": len(failures),
        "posts": posts,
        "failures": failures,
        "first_label": format_dt_es(first_local, tz, year=True) if first_local else None,
        "last_label": format_dt_es(last_local, tz, year=True) if last_local else None,
        "links": links,
        "auto_rows": report["auto_rows"] if action == "schedule" else 0,
        "finished_at": now.isoformat(timespec="seconds"),
    }
    _finish_submission(db, token, brand.id, result, now)
    remaining = [r for r in rows if r["id"] in {f["row_id"] for f in failures}]
    if remaining:
        leftover = copy.deepcopy(batch)
        leftover["token"] = new_token()
        leftover["rows"] = remaining
        leftover["step"] = 4
        save_batch(db, brand.id, leftover, clock)
        result["remaining_token"] = leftover["token"]
    else:
        discard_batch(db, brand.id)
    config.set_setting(db, LAST_KEY.format(brand_id=brand.id), _last_summary(result))
    return result


def _create_rows(
    db: Session,
    brand: Brand,
    batch: Mapping[str, Any],
    rows: list[dict[str, Any]],
    action: str,
    report: Mapping[str, Any],
    state: dict[str, Any],
    clock: Clock,
    *,
    tiktok_consent: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One post per row (each committed with the claim's progress). Rows an earlier, interrupted submit of
    this batch created are not created again: they are reported as they were (also those no longer in
    ``rows``, e.g. left out of the page restored after the interruption)."""
    from planificador import postsvc

    token = str(batch.get("token") or "")
    tz = brand.timezone or "Europe/Madrid"
    accounts, _ = selected_accounts(db, brand, batch, create_demo=True)
    media = media_names(db, brand, rows)
    done: dict[str, Any] = dict(state.get("done") or {})
    row_ids = {r["id"] for r in rows}
    posts: list[dict[str, Any]] = [entry for rid, entry in done.items() if rid not in row_ids]
    failures: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if row["id"] in done:  # created by an interrupted earlier submit of this batch
            posts.append(done[row["id"]])
            continue
        asset = media.get(int(row.get("media_id") or 0))
        name = asset.original_name if asset is not None else f"Vídeo {index + 1}"
        thumb = f"/medios/{asset.id}/miniatura" if asset is not None else None
        reasons = [e["message"] for e in report["rows"].get(row["id"], {}).get("errors", [])]
        row_action = "schedule" if action == "schedule" and not reasons else "draft"
        data = row_post_input(brand, batch, row, accounts, media, tiktok_consent=tiktok_consent)
        post = None
        for attempt in (row_action, "draft"):
            try:
                post = postsvc.create_post(db, data, attempt, clock, commit=False)
            except postsvc.PostError as exc:
                if attempt == "schedule":
                    reasons = [exc.message]
                    continue
                failures.append(
                    {"row_id": row["id"], "name": name, "thumb_url": thumb, "message": exc.message}
                )
            except Exception:
                log.exception("Bulk row %s failed", row.get("id"))
                failures.append(
                    {
                        "row_id": row["id"],
                        "name": name,
                        "thumb_url": thumb,
                        "message": "No se ha podido guardar. Revisa el registro de la aplicación.",
                    }
                )
            else:
                row_action = attempt
            break
        if post is None:
            continue
        entry = {
            "row_id": row["id"],
            "post_id": post.id,
            "name": name,
            "thumb_url": thumb,
            "status": "scheduled" if row_action == "schedule" else "draft",
            "when": post.scheduled_at.isoformat(timespec="seconds") if post.scheduled_at else None,
            "when_label": _when_label(post, tz),
            "reasons": reasons if row_action == "draft" and action == "schedule" else [],
            "networks": [str(t.network) for t in post.targets],
        }
        done[row["id"]] = entry
        state["done"] = done
        try:
            _record_progress(db, token, state, ensure_utc(clock.now()))
            db.commit()
        except Exception:
            db.rollback()
            done.pop(row["id"], None)
            log.exception("Bulk row %s could not be committed", row.get("id"))
            failures.append(
                {
                    "row_id": row["id"],
                    "name": name,
                    "thumb_url": thumb,
                    "message": "No se ha podido guardar. Revisa el registro de la aplicación.",
                }
            )
            continue
        posts.append(entry)
    return posts, failures


def _wake_scheduler() -> None:
    try:
        from planificador import scheduler

        scheduler.wake()
    except Exception:  # pragma: no cover - scheduler not running
        log.debug("Scheduler wake failed", exc_info=True)


def _previous_result(state: Mapping[str, Any]) -> dict[str, Any]:
    if state.get("status") == "done" and isinstance(state.get("result"), dict):
        result = dict(state["result"])
        result["duplicate"] = True
        return result
    raise BatchError(
        "Esta carga masiva ya se está guardando. Espera unos segundos y recarga la página.",
        code="in_progress",
        status_code=409,
    )


def _last_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scheduled": result.get("scheduled", 0),
        "drafts": result.get("drafts", 0),
        "failed": result.get("failed", 0),
        "first_label": result.get("first_label"),
        "last_label": result.get("last_label"),
        "links": result.get("links") or {},
        "finished_at": result.get("finished_at"),
    }


def last_result(db: Session, brand_id: int) -> dict[str, Any] | None:
    value = config.get_setting(db, LAST_KEY.format(brand_id=brand_id), None)
    return value if isinstance(value, dict) else None


__all__ = [
    "CAP_RULES",
    "MAX_ROWS",
    "BatchError",
    "CapEntry",
    "DistributionSpec",
    "SlotPick",
    "caps_warnings",
    "check_batch",
    "claim_submission",
    "compose_caption",
    "discard_batch",
    "distribute",
    "empty_batch",
    "load_batch",
    "normalize_hashtags",
    "parse_spec",
    "sanitize_batch",
    "save_batch",
    "schedule_summary",
    "submit_batch",
    "suggested_slots",
    "youtube_title",
]

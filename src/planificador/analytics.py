"""Analytics: daily snapshots, aggregations and best times (SPEC §12).

Data flow
---------
:func:`snapshot_all` runs daily (scheduler job) and on demand ("Actualizar ahora"). For every connected
account it asks the provider for today's :class:`~planificador.providers.base.AccountSnapshot` (upserted into
``analytics_snapshots``; YouTube also returns a short ``history`` of daily Analytics rows that lag 2-3 days,
which is merged into the previous days) and for the posts of the last 30 days (upserted into ``post_metrics``,
linked to our ``post_targets`` by external id). Failures are recorded per account in
``account.meta["analytics_error"]`` and never propagate.

Metric kinds
------------
- *Stock* metrics are counters read at a point in time (``followers``, ``posts_count``, ``likes_total``…):
  the value of a period is the **last** snapshot inside it.
- *Flow* metrics are per-day amounts (``views``, ``reach``, ``interactions``…): the value of a period is the
  **sum** of its days.

YouTube rule (report 06 §10.3): only native metrics are shown — no ``interactions``/``engagement`` are
derived for YouTube rows.

Best times
----------
:func:`best_times` returns a 7×24 matrix (Monday = 0, brand-local hours) of how well our own published posts
performed at each slot. It needs at least :data:`BEST_TIMES_MIN_POSTS` posts with metrics; otherwise it
returns a general audience pattern (``source = "patron_general"``).
"""

from __future__ import annotations

import csv
import io
import logging
import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador.models import (
    Account,
    AccountStatus,
    AnalyticsSnapshot,
    Brand,
    PostMetric,
    PostTarget,
    TargetStatus,
)
from planificador.providers.base import ProviderError, ProviderSettings, scrub_text
from planificador.timeutil import Clock, ensure_utc, get_tz, to_brand_local

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------- constants

RECENT_POSTS_DAYS = 30
DEMO_BACKFILL_DAYS = 90  # demo accounts get a deterministic history on their first snapshot
BEST_TIMES_MIN_POSTS = 10
BEST_TIMES_LOOKBACK_DAYS = 365
PERIOD_PRESETS: tuple[int, ...] = (7, 28, 90)
DEFAULT_PRESET = 28
MAX_CUSTOM_DAYS = 400

#: Networks whose metrics are shown natively only (no derived interactions/engagement).
NATIVE_ONLY_NETWORKS = frozenset({"youtube"})

STOCK_METRICS = frozenset(
    {"followers", "following", "posts_count", "likes_total", "video_count", "views_total"}
)
FLOW_METRICS = frozenset(
    {
        "views",
        "reach",
        "interactions",
        "profile_views",
        "watch_minutes",
        "likes",
        "comments",
        "shares",
        "saves",
        "subs_gained",
        "subs_lost",
    }
)
POST_METRIC_KEYS: tuple[str, ...] = (
    "views",
    "reach",
    "likes",
    "comments",
    "shares",
    "saves",
    "interactions",
    "engagement",
)
INTERACTION_PARTS: tuple[str, ...] = ("likes", "comments", "shares", "saves")
PUBLISHED_STATUSES = (TargetStatus.PUBLISHED, TargetStatus.MANUAL_DONE)

METRIC_LABELS: dict[str, str] = {
    "followers": "Seguidores",
    "following": "Seguidos",
    "posts_count": "Publicaciones totales",
    "likes_total": "Me gusta totales",
    "video_count": "Vídeos",
    "views_total": "Visualizaciones totales",
    "views": "Visualizaciones",
    "reach": "Alcance",
    "interactions": "Interacciones",
    "profile_views": "Visitas al perfil",
    "watch_minutes": "Minutos vistos",
    "likes": "Me gusta",
    "comments": "Comentarios",
    "shares": "Compartidos",
    "saves": "Guardados",
    "subs_gained": "Suscriptores ganados",
    "subs_lost": "Suscriptores perdidos",
    "engagement": "Engagement",
    "posts": "Posts",
    "followers_net": "Balance de seguidores",
}

MEDIA_TYPE_LABELS: dict[str, str] = {
    "IMAGE": "Imagen",
    "PHOTO": "Foto",
    "VIDEO": "Vídeo",
    "CAROUSEL_ALBUM": "Carrusel",
    "CAROUSEL": "Carrusel",
    "REEL": "Reel",
    "REELS": "Reel",
    "STORY": "Historia",
    "TEXT": "Texto",
    "LINK": "Enlace",
    "SHORT": "Short",
    "ALBUM": "Álbum",
}


def metric_label(key: str, network: str | None = None) -> str:
    if key == "followers" and network == "youtube":
        return "Suscriptores"
    return METRIC_LABELS.get(key, key.replace("_", " ").capitalize())


def media_type_label(value: str | None) -> str:
    if not value:
        return "—"
    return MEDIA_TYPE_LABELS.get(str(value).upper(), str(value).replace("_", " ").capitalize())


# --------------------------------------------------------------------------------------------- periods


@dataclass(frozen=True)
class Period:
    """Inclusive range of brand-local days."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("Period end before start")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> Period:
        """The period of the same length that ends the day before this one starts."""
        end = self.start - timedelta(days=1)
        return Period(end - timedelta(days=self.days - 1), end)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def preset_period(today: date, days: int) -> Period:
    """Last ``days`` days, today included."""
    days = max(1, int(days))
    return Period(today - timedelta(days=days - 1), today)


def _parse_day(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def resolve_period(
    today: date, preset: str | int | None = None, desde: Any = None, hasta: Any = None
) -> tuple[Period, str]:
    """Period from the UI parameters → ``(period, preset_key)``.

    ``preset``: ``"7"``, ``"28"``, ``"90"`` or ``"personalizado"`` (uses ``desde``/``hasta``, ISO dates).
    Invalid input falls back to the default preset; custom ranges are clamped to today and
    :data:`MAX_CUSTOM_DAYS`.
    """
    key = str(preset or "").strip().lower()
    start, end = _parse_day(desde), _parse_day(hasta)
    if key in ("personalizado", "custom") or (not key and (start or end)):
        if start or end:
            end = end or today
            start = start or end - timedelta(days=DEFAULT_PRESET - 1)
            if start > end:
                start, end = end, start
            end = min(end, today)
            start = min(start, end)
            if (end - start).days + 1 > MAX_CUSTOM_DAYS:
                start = end - timedelta(days=MAX_CUSTOM_DAYS - 1)
            return Period(start, end), "personalizado"
        key = ""
    try:
        days = int(key) if key else DEFAULT_PRESET
    except ValueError:
        days = DEFAULT_PRESET
    if days not in PERIOD_PRESETS:
        days = DEFAULT_PRESET
    return preset_period(today, days), str(days)


def brand_tz(account_or_brand: Account | Brand | None) -> str | None:
    if account_or_brand is None:
        return None
    if isinstance(account_or_brand, Account):
        brand = account_or_brand.brand
        return brand.timezone if brand is not None else None
    return account_or_brand.timezone


def local_today(clock: Clock, tz: str | None) -> date:
    local = to_brand_local(clock.now(), tz)
    assert local is not None
    return local.date()


# ---------------------------------------------------------------------------------------- calculations


def change_pct(current: float | None, previous: float | None) -> float | None:
    """% change vs the previous period; ``None`` when it cannot be computed (no data or previous = 0)."""
    if current is None or previous is None:
        return None
    try:
        cur, prev = float(current), float(previous)
    except (TypeError, ValueError):
        return None
    if prev == 0 or math.isnan(prev) or math.isnan(cur):
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(n) or math.isinf(n) else n


def _clean(n: float | None) -> int | float | None:
    """Integers stay integers (JSON/CSV friendly)."""
    if n is None:
        return None
    return int(n) if float(n).is_integer() else round(n, 4)


clean_number = _clean


def engagement(metrics: Mapping[str, Any], network: str | None = None) -> float | None:
    """interactions / reach × 100 (or / views when there is no reach), 2 decimals. ``None`` for YouTube
    (native metrics only) or when it cannot be computed."""
    if network in NATIVE_ONLY_NETWORKS:
        return None
    interactions = _num(metrics.get("interactions"))
    if interactions is None:
        parts = [_num(metrics.get(k)) for k in INTERACTION_PARTS]
        if all(p is None for p in parts):
            return None
        interactions = sum(p or 0 for p in parts)
    base = _num(metrics.get("reach")) or _num(metrics.get("views"))
    if not base:
        return None
    return round(interactions / base * 100, 2)


def normalize_post_metrics(metrics: Mapping[str, Any], network: str | None) -> dict[str, Any]:
    """Provider post metrics → stored metrics: numbers only; ``interactions``/``engagement`` filled in for
    networks that allow derived metrics, removed for YouTube."""
    out: dict[str, Any] = {}
    for key, value in (metrics or {}).items():
        n = _num(value)
        if n is not None:
            out[str(key)] = _clean(n)
    if network in NATIVE_ONLY_NETWORKS:
        out.pop("interactions", None)
        out.pop("engagement", None)
        return out
    if "interactions" not in out and any(k in out for k in INTERACTION_PARTS):
        out["interactions"] = _clean(sum(float(out.get(k) or 0) for k in INTERACTION_PARTS))
    if "engagement" not in out:
        eng = engagement(out, network)
        if eng is not None:
            out["engagement"] = eng
    return out


# ------------------------------------------------------------------------------------------- snapshots


def _upsert_snapshot(
    db: Session, account_id: int, day: date, metrics: Mapping[str, Any], now: datetime
) -> bool:
    clean = {str(k): _clean(n) for k, v in (metrics or {}).items() if (n := _num(v)) is not None}
    if not clean:
        return False
    row = db.scalar(
        select(AnalyticsSnapshot).where(
            AnalyticsSnapshot.account_id == account_id, AnalyticsSnapshot.day == day
        )
    )
    if row is None:
        db.add(AnalyticsSnapshot(account_id=account_id, day=day, metrics=clean, fetched_at=now))
    else:
        merged = dict(row.metrics or {})
        merged.update(clean)
        row.metrics = merged
        row.fetched_at = now
    db.flush()
    return True


def _target_index(db: Session, account_id: int) -> dict[str, int]:
    """External ids (and ids saved in ``provider_state`` / ``external_url``) of our targets → target id."""
    index: dict[str, int] = {}
    rows = db.execute(
        select(
            PostTarget.id, PostTarget.external_id, PostTarget.external_url, PostTarget.provider_state
        ).where(
            PostTarget.account_id == account_id,
            PostTarget.status.in_([*PUBLISHED_STATUSES, TargetStatus.SENT_TO_APP]),
        )
    ).all()
    for target_id, external_id, external_url, state in rows:
        candidates: list[str] = []
        if external_id:
            candidates.append(str(external_id))
        if external_url:
            candidates.append(str(external_url))
        if isinstance(state, dict):
            for key in ("post_id", "video_id", "media_id", "publish_id", "remote_id", "published_id"):
                value = state.get(key)
                if isinstance(value, str | int) and not isinstance(value, bool) and str(value):
                    candidates.append(str(value))
        for cand in candidates:
            index.setdefault(cand, target_id)
            if "_" in cand:  # Facebook "<page>_<post>" ↔ "<post>"
                index.setdefault(cand.split("_", 1)[1], target_id)
    return index


def _match_target(index: Mapping[str, int], external_id: str, permalink: str | None) -> int | None:
    if external_id in index:
        return index[external_id]
    if "_" in external_id and external_id.split("_", 1)[1] in index:
        return index[external_id.split("_", 1)[1]]
    if permalink and permalink in index:
        return index[permalink]
    return None


#: Assisted YouTube uploads are matched to our target by exact title within this window around its date.
ASSISTED_LINK_WINDOW = timedelta(hours=48)


def _norm_title(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _link_assisted_youtube(
    db: Session, account: Account, data: Any, external_id: str, published_at: datetime | None, now: datetime
) -> int | None:
    """Publicación asistida on YouTube: the user uploaded the video in YouTube Studio, so we never got its id.
    Link it to our target when the title matches exactly and the dates are within ±48 h — only when exactly
    one assisted target qualifies. The target gets the video id/URL (a pending one is marked as done)."""
    title = _norm_title(getattr(data, "title", None))
    if not title or published_at is None:
        return None
    from planificador.engine import add_event, target_text

    candidates = db.scalars(
        select(PostTarget).where(
            PostTarget.account_id == account.id,
            PostTarget.status.in_([TargetStatus.MANUAL_DONE, TargetStatus.MANUAL_PENDING]),
            PostTarget.scheduled_at >= published_at - ASSISTED_LINK_WINDOW,
            PostTarget.scheduled_at <= published_at + ASSISTED_LINK_WINDOW,
        )
    ).all()
    matches = []
    for target in candidates:
        if target.external_id and target.external_id != external_id:
            continue
        options = target.options or {}
        own = options.get("title") or (target_text(target).strip().splitlines() or [""])[0]
        if _norm_title(own) == title:
            matches.append(target)
    if len(matches) != 1:
        return None
    target = matches[0]
    if target.external_id == external_id:
        return target.id
    target.external_id = external_id
    permalink = getattr(data, "permalink", None)
    if permalink and not target.external_url:
        target.external_url = permalink
    message = "Vinculada automáticamente con el vídeo de YouTube del mismo título."
    if target.status == TargetStatus.MANUAL_PENDING:
        target.status = TargetStatus.MANUAL_DONE
        target.published_at = published_at
        message = "Encontrada en YouTube con el mismo título: marcada como publicada."
        try:
            from planificador import notify

            notify.resolve_for_target(db, target.id, now=now)
        except Exception:  # pragma: no cover - notifications are best effort here
            log.debug("Could not resolve notifications for target %s", target.id, exc_info=True)
    target.updated_at = now
    add_event(db, target.id, now, "youtube_linked", message, data={"external_id": external_id})
    return target.id


def _upsert_posts(db: Session, account: Account, posts: Iterable[Any], now: datetime) -> int:
    index = _target_index(db, account.id)
    network = str(account.network)
    count = 0
    for data in posts:
        external_id = str(getattr(data, "external_id", "") or "").strip()
        if not external_id:
            continue
        metrics = normalize_post_metrics(getattr(data, "metrics", None) or {}, network)
        published_at = getattr(data, "published_at", None)
        published_at = ensure_utc(published_at) if isinstance(published_at, datetime) else None
        permalink = getattr(data, "permalink", None)
        row = db.scalar(
            select(PostMetric).where(
                PostMetric.account_id == account.id, PostMetric.external_id == external_id
            )
        )
        if row is None:
            row = PostMetric(account_id=account.id, external_id=external_id)
            db.add(row)
        target_id = _match_target(index, external_id, permalink)
        if target_id is None and network == "youtube":
            target_id = _link_assisted_youtube(db, account, data, external_id, published_at, now)
        if target_id is not None:
            row.target_id = target_id
        row.published_at = published_at or row.published_at
        row.permalink = permalink or row.permalink
        caption = getattr(data, "caption", None)
        row.caption = caption if caption is not None else row.caption
        row.media_type = getattr(data, "media_type", None) or row.media_type
        row.thumbnail_url = getattr(data, "thumbnail_url", None) or row.thumbnail_url
        merged = dict(row.metrics or {})
        merged.update(metrics)
        if network in NATIVE_ONLY_NETWORKS:
            merged.pop("interactions", None)
            merged.pop("engagement", None)
        row.metrics = merged
        row.fetched_at = now
        count += 1
    db.flush()
    return count


def _set_meta(account: Account, **changes: Any) -> None:
    meta = dict(account.meta or {})
    for key, value in changes.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    account.meta = meta


def _error_info(exc: BaseException, now: datetime) -> dict[str, Any]:
    if isinstance(exc, ProviderError):
        return {
            "message": exc.message,
            "code": exc.code,
            "kind": str(exc.kind),
            "at": now.isoformat(),
        }
    return {
        "message": "No se pudieron actualizar las estadísticas de esta cuenta. Se reintentará más tarde.",
        "code": type(exc).__name__,
        "kind": "internal",
        "detail": scrub_text(str(exc))[:300],
        "at": now.isoformat(),
    }


def snapshot_account(
    db: Session,
    account: Account,
    http: httpx.Client,
    clock: Clock,
    settings: ProviderSettings,
) -> dict[str, Any]:
    """Snapshot one account (no error handling, no commit). Returns counts and notices."""
    from planificador.providers import registry

    provider = registry.get_provider(str(account.network))
    if not getattr(provider, "supports_analytics", True):
        return {"snapshots": 0, "posts": 0, "notices": [], "skipped": True}
    now = clock.now()
    today = local_today(clock, brand_tz(account))
    creds = account.to_creds()
    notices: list[str] = []
    snapshots = 0

    days = [today]
    if str(account.network) == "demo" and settings.demo_mode:
        has_any = db.scalar(
            select(AnalyticsSnapshot.id).where(AnalyticsSnapshot.account_id == account.id).limit(1)
        )
        if has_any is None:
            days = [today - timedelta(days=i) for i in range(DEMO_BACKFILL_DAYS - 1, -1, -1)]
    for day in days:
        snap = provider.fetch_account_snapshot(creds, settings, http, day)
        if _upsert_snapshot(db, account.id, getattr(snap, "day", None) or day, snap.metrics, now):
            snapshots += 1
        for extra in getattr(snap, "history", None) or []:
            extra_day = getattr(extra, "day", None)
            if isinstance(extra_day, date) and _upsert_snapshot(
                db, account.id, extra_day, extra.metrics, now
            ):
                snapshots += 1
        notice = getattr(snap, "notice", None)
        if notice and notice not in notices:
            notices.append(str(notice))

    posts = provider.fetch_recent_posts(creds, settings, http, now - timedelta(days=RECENT_POSTS_DAYS))
    notice = getattr(posts, "notice", None)
    if notice and notice not in notices:
        notices.append(str(notice))
    count = _upsert_posts(db, account, posts or [], now)
    return {"snapshots": snapshots, "posts": count, "notices": notices, "skipped": False}


def snapshot_all(
    db: Session,
    http: httpx.Client,
    clock: Clock,
    provider_settings: ProviderSettings | None = None,
    *,
    account_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Snapshot every connected account (SPEC §12). Never raises because of one account: errors are stored
    in ``account.meta["analytics_error"]`` (``{"message", "code", "kind", "at"}``) and cleared on the next
    success. Commits once per account. Returns a summary dict."""
    if provider_settings is None:
        from planificador import config

        provider_settings = config.provider_settings(db)
    # The daily job and "Actualizar ahora" may overlap: serialise them (upserts race on (account, day)).
    with _snapshot_lock:
        return _snapshot_all(db, http, clock, provider_settings, account_ids)


_snapshot_lock = threading.Lock()


def _snapshot_all(
    db: Session,
    http: httpx.Client,
    clock: Clock,
    provider_settings: ProviderSettings,
    account_ids: Iterable[int] | None,
) -> dict[str, Any]:
    stmt = select(Account).where(Account.status == AccountStatus.CONNECTED).order_by(Account.id)
    if account_ids is not None:
        stmt = stmt.where(Account.id.in_(list(account_ids)))
    accounts = list(db.scalars(stmt))
    summary: dict[str, Any] = {"accounts": 0, "ok": 0, "errors": 0, "snapshots": 0, "posts": 0, "details": []}
    for account in accounts:
        if str(account.network) == "demo" and not provider_settings.demo_mode:
            continue
        account_id, network = account.id, str(account.network)
        summary["accounts"] += 1
        now = clock.now()
        try:
            result = snapshot_account(db, account, http, clock, provider_settings)
            _set_meta(
                account,
                analytics_error=None,
                analytics_notice=" ".join(result["notices"]) or None,
                analytics_last_ok=now.isoformat(),
            )
            db.commit()
            summary["ok"] += 1
            summary["snapshots"] += result["snapshots"]
            summary["posts"] += result["posts"]
            summary["details"].append(
                {"account_id": account_id, "network": network, "status": "ok", **_counts(result)}
            )
        except Exception as exc:  # noqa: BLE001 - recorded per account, never propagated
            if isinstance(exc, ProviderError):
                log.warning("Analytics snapshot failed for account %s: %s", account_id, exc.code)
            else:
                log.exception("Analytics snapshot failed for account %s", account_id)
            db.rollback()
            info = _error_info(exc, now)
            try:
                fresh = db.get(Account, account_id)
                if fresh is not None:
                    _set_meta(fresh, analytics_error=info)
                    db.commit()
            except Exception:  # noqa: BLE001
                log.exception("Could not record the analytics error of account %s", account_id)
                db.rollback()
            summary["errors"] += 1
            summary["details"].append(
                {"account_id": account_id, "network": network, "status": "error", "error": info["message"]}
            )
    return summary


def _counts(result: Mapping[str, Any]) -> dict[str, int]:
    return {"snapshots": int(result.get("snapshots") or 0), "posts": int(result.get("posts") or 0)}


# ------------------------------------------------------------------------------- background refresh

_refresh_lock = threading.Lock()
_refresh_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
}


def _new_http_client() -> httpx.Client:
    from planificador import __version__

    return httpx.Client(
        timeout=httpx.Timeout(60.0, connect=15.0), headers={"User-Agent": f"Planificador/{__version__}"}
    )


def refresh_status() -> dict[str, Any]:
    with _refresh_lock:
        return dict(_refresh_state)


def start_background_refresh(
    session_factory: Callable[[], Session],
    clock: Clock,
    provider_settings: ProviderSettings,
    *,
    http_factory: Callable[[], httpx.Client] | None = None,
    account_ids: Iterable[int] | None = None,
    wait: bool = False,
) -> bool:
    """Run :func:`snapshot_all` in a daemon thread ("Actualizar ahora"). Returns ``False`` when a refresh is
    already running. ``wait=True`` joins the thread (tests)."""
    with _refresh_lock:
        if _refresh_state["running"]:
            return False
        _refresh_state.update(
            running=True, started_at=clock.now().isoformat(), finished_at=None, summary=None, error=None
        )
    ids = list(account_ids) if account_ids is not None else None

    def run() -> None:
        summary: dict[str, Any] | None = None
        error: str | None = None
        try:
            http = http_factory() if http_factory else _new_http_client()
            try:
                session = session_factory()
                try:
                    summary = snapshot_all(session, http, clock, provider_settings, account_ids=ids)
                finally:
                    session.close()
            finally:
                http.close()
        except Exception as exc:  # noqa: BLE001
            log.exception("Background analytics refresh failed")
            error = scrub_text(f"{type(exc).__name__}: {exc}")[:300]
        with _refresh_lock:
            _refresh_state.update(
                running=False, finished_at=clock.now().isoformat(), summary=summary, error=error
            )

    thread = threading.Thread(target=run, name="planificador-analytics", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return True


# ---------------------------------------------------------------------------------------- aggregations


def snapshots_in(db: Session, account_id: int, start: date, end: date) -> list[AnalyticsSnapshot]:
    return list(
        db.scalars(
            select(AnalyticsSnapshot)
            .where(
                AnalyticsSnapshot.account_id == account_id,
                AnalyticsSnapshot.day >= start,
                AnalyticsSnapshot.day <= end,
            )
            .order_by(AnalyticsSnapshot.day)
        )
    )


def _last_before(db: Session, account_id: int, day: date, metric: str) -> tuple[date, float] | None:
    """Last ``metric`` value strictly before ``day`` (looks back over the 60 previous snapshots)."""
    rows = db.scalars(
        select(AnalyticsSnapshot)
        .where(AnalyticsSnapshot.account_id == account_id, AnalyticsSnapshot.day < day)
        .order_by(AnalyticsSnapshot.day.desc())
        .limit(60)
    )
    for row in rows:
        value = _num((row.metrics or {}).get(metric))
        if value is not None:
            return row.day, value
    return None


def metric_value(rows: Iterable[AnalyticsSnapshot], metric: str) -> float | None:
    """Period value of ``metric``: last value for stock metrics, sum for flow metrics, ``None`` if absent."""
    values = [v for r in rows if (v := _num((r.metrics or {}).get(metric))) is not None]
    if not values:
        return None
    if metric in STOCK_METRICS:
        return values[-1]
    return sum(values)


def series(
    db: Session, account: Account, metric: str, start: date, end: date
) -> list[tuple[date, float | None]]:
    """One value per day of ``[start, end]`` (``None`` where there is no snapshot/metric)."""
    by_day = {r.day: _num((r.metrics or {}).get(metric)) for r in snapshots_in(db, account.id, start, end)}
    return [(d, by_day.get(d)) for d in Period(start, end).dates()]


def follower_balance(db: Session, account: Account, start: date, end: date) -> dict[str, Any]:
    """Follower balance from consecutive snapshots.

    Each day with a ``followers`` value gets ``net = followers - previous known followers`` (the previous
    known value may come from before ``start``). Totals: ``net`` (end value − baseline), ``gained`` (sum of
    positive days), ``lost`` (sum of negative days, as a positive number), ``start_value``/``end_value``.
    YouTube uses its native ``subs_gained``/``subs_lost`` daily metrics instead (``net`` stays ``None``).
    """
    network = str(account.network)
    rows = snapshots_in(db, account.id, start, end)
    by_day = {r.day: r.metrics or {} for r in rows}
    days: list[dict[str, Any]] = []
    if network in NATIVE_ONLY_NETWORKS:
        gained_total = lost_total = 0.0
        has_any = False
        for d in Period(start, end).dates():
            m = by_day.get(d, {})
            g, lost = _num(m.get("subs_gained")), _num(m.get("subs_lost"))
            if g is not None or lost is not None:
                has_any = True
            gained_total += g or 0
            lost_total += lost or 0
            days.append({"day": d, "net": None, "gained": _clean(g), "lost": _clean(lost)})
        followers = [v for d in sorted(by_day) if (v := _num(by_day[d].get("followers"))) is not None]
        return {
            "days": days,
            "net": None,
            "gained": _clean(gained_total) if has_any else None,
            "lost": _clean(lost_total) if has_any else None,
            "start_value": _clean(followers[0]) if followers else None,
            "end_value": _clean(followers[-1]) if followers else None,
            "native": True,
        }

    before = _last_before(db, account.id, start, "followers")
    prev_value = before[1] if before else None
    baseline = prev_value
    gained = lost = 0.0
    last_value: float | None = None
    first_value: float | None = None
    for d in Period(start, end).dates():
        value = _num(by_day.get(d, {}).get("followers"))
        net: float | None = None
        if value is not None:
            if first_value is None:
                first_value = value
            if prev_value is not None:
                net = value - prev_value
                if net > 0:
                    gained += net
                elif net < 0:
                    lost += -net
            prev_value = value
            last_value = value
        days.append({"day": d, "net": _clean(net), "gained": None, "lost": None})
    if baseline is None:
        baseline = first_value
    total_net = (last_value - baseline) if last_value is not None and baseline is not None else None
    return {
        "days": days,
        "net": _clean(total_net),
        "gained": _clean(gained) if last_value is not None else None,
        "lost": _clean(lost) if last_value is not None else None,
        "start_value": _clean(baseline),
        "end_value": _clean(last_value),
        "native": False,
    }


def posts_in(db: Session, account: Account, start: date, end: date) -> list[PostMetric]:
    """Post metrics whose publication falls in ``[start, end]`` (brand-local days), newest first."""
    tz = brand_tz(account)
    zone = get_tz(tz)
    lo = datetime(start.year, start.month, start.day, tzinfo=zone) - timedelta(hours=2)
    hi = datetime(end.year, end.month, end.day, tzinfo=zone) + timedelta(days=1, hours=2)
    rows = db.scalars(
        select(PostMetric)
        .where(
            PostMetric.account_id == account.id,
            PostMetric.published_at.is_not(None),
            PostMetric.published_at >= lo.astimezone(UTC),
            PostMetric.published_at < hi.astimezone(UTC),
        )
        .order_by(PostMetric.published_at.desc())
    )
    out = []
    for row in rows:
        local = to_brand_local(row.published_at, tz)
        if local is not None and start <= local.date() <= end:
            out.append(row)
    return out


def posts_per_day(db: Session, account: Account, start: date, end: date) -> list[tuple[date, int]]:
    tz = brand_tz(account)
    counts: dict[date, int] = {}
    for row in posts_in(db, account, start, end):
        local = to_brand_local(row.published_at, tz)
        assert local is not None
        counts[local.date()] = counts.get(local.date(), 0) + 1
    return [(d, counts.get(d, 0)) for d in Period(start, end).dates()]


def posts_engagement(rows: Iterable[PostMetric], network: str | None) -> float | None:
    """Weighted engagement of a set of posts: Σ interactions / Σ reach (or views) × 100."""
    if network in NATIVE_ONLY_NETWORKS:
        return None
    interactions = base = 0.0
    for row in rows:
        m = row.metrics or {}
        inter = _num(m.get("interactions"))
        if inter is None:
            parts = [_num(m.get(k)) for k in INTERACTION_PARTS]
            if all(p is None for p in parts):
                continue
            inter = sum(p or 0 for p in parts)
        b = _num(m.get("reach")) or _num(m.get("views"))
        if not b:
            continue
        interactions += inter
        base += b
    if not base:
        return None
    return round(interactions / base * 100, 2)


def _posts_sum(rows: Iterable[PostMetric], key: str) -> float | None:
    values = [v for r in rows if (v := _num((r.metrics or {}).get(key))) is not None]
    return sum(values) if values else None


def _kpi(value: float | None, previous: float | None, compare: bool) -> dict[str, Any]:
    return {
        "value": _clean(value),
        "previous": _clean(previous) if compare else None,
        "change_pct": change_pct(value, previous) if compare else None,
    }


def kpis(db: Session, account: Account, start: date, end: date, *, compare: bool = True) -> dict[str, Any]:
    """KPIs of ``[start, end]`` and, with ``compare``, of the previous period of the same length.

    Keys: every snapshot metric present (stock → last value, flow → sum), ``posts`` (posts published in the
    period), ``post_<metric>`` (sums over those posts), ``followers_net`` (follower balance) and
    ``engagement`` (weighted, not for YouTube). Each value is ``{"value", "previous", "change_pct"}``.
    """
    network = str(account.network)
    period = Period(start, end)
    prev = period.previous()
    cur_rows = snapshots_in(db, account.id, period.start, period.end)
    prev_rows = snapshots_in(db, account.id, prev.start, prev.end) if compare else []
    keys: set[str] = set()
    for row in cur_rows:
        keys.update(k for k, v in (row.metrics or {}).items() if _num(v) is not None)
    if network in NATIVE_ONLY_NETWORKS:
        keys.discard("interactions")
    out: dict[str, Any] = {}
    for key in sorted(keys):
        out[key] = _kpi(metric_value(cur_rows, key), metric_value(prev_rows, key), compare)

    cur_posts = posts_in(db, account, period.start, period.end)
    prev_posts = posts_in(db, account, prev.start, prev.end) if compare else []
    out["posts"] = _kpi(float(len(cur_posts)), float(len(prev_posts)) if compare else None, compare)
    for key in ("views", "reach", "likes", "comments", "shares", "interactions"):
        if network in NATIVE_ONLY_NETWORKS and key == "interactions":
            continue
        out["post_" + key] = _kpi(_posts_sum(cur_posts, key), _posts_sum(prev_posts, key), compare)

    balance = follower_balance(db, account, period.start, period.end)
    prev_balance = follower_balance(db, account, prev.start, prev.end) if compare else {"net": None}
    out["followers_net"] = _kpi(_num(balance["net"]), _num(prev_balance["net"]), compare)
    if network not in NATIVE_ONLY_NETWORKS:
        out["engagement"] = _kpi(
            posts_engagement(cur_posts, network), posts_engagement(prev_posts, network), compare
        )
    return out


def post_row(row: PostMetric, network: str, tz: str | None) -> dict[str, Any]:
    m = row.metrics or {}
    local = to_brand_local(row.published_at, tz) if row.published_at else None
    values: dict[str, Any] = {k: _clean(_num(m.get(k))) for k in POST_METRIC_KEYS}
    if network in NATIVE_ONLY_NETWORKS:
        values["interactions"] = None
        values["engagement"] = None
    else:
        if values["interactions"] is None:
            values["interactions"] = _clean(_num(normalize_post_metrics(m, network).get("interactions")))
        if values["engagement"] is None:
            values["engagement"] = engagement(m, network)
    post_id = None
    media_id = None
    target = row.target
    if target is not None:
        post_id = target.post_id
        ids = target.effective_media_ids if target.post is not None else []
        media_id = ids[0] if ids else None
    for key in ("watch_minutes", "avg_view_pct", "avg_view_duration", "subs_gained"):
        values[key] = _clean(_num(m.get(key)))
    return {
        "id": row.id,
        "external_id": row.external_id,
        "published_at": row.published_at,
        "local": local,
        "day": local.date() if local else None,
        "caption": (row.caption or "").strip(),
        "media_type": row.media_type,
        "media_type_label": media_type_label(row.media_type),
        "thumbnail_url": row.thumbnail_url,
        "media_id": media_id,
        "permalink": row.permalink,
        "target_id": row.target_id,
        "post_id": post_id,
        "ours": row.target_id is not None,
        **values,
    }


def post_rows(db: Session, account: Account, start: date, end: date) -> list[dict[str, Any]]:
    tz = brand_tz(account)
    network = str(account.network)
    return [post_row(r, network, tz) for r in posts_in(db, account, start, end)]


def top_posts(
    db: Session, account: Account, start: date, end: date, metric: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Best posts of the period by ``metric`` (default: engagement; views for YouTube)."""
    network = str(account.network)
    metric = metric or ("views" if network in NATIVE_ONLY_NETWORKS else "engagement")
    rows = [r for r in post_rows(db, account, start, end) if r.get(metric) is not None]
    rows.sort(
        key=lambda r: (float(r[metric]), r["published_at"] or datetime.min.replace(tzinfo=UTC)), reverse=True
    )
    return rows[: max(0, limit)]


# ------------------------------------------------------------------------------------------------ CSV

CSV_DELIMITER = ";"


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return str(value)


def _csv_text(header: list[str], rows: Iterable[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=CSV_DELIMITER, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_value(v) for v in row])
    return "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 correctly


def posts_csv(rows: Iterable[Mapping[str, Any]], network: str) -> str:
    """Posts table as CSV (UTF-8 BOM, ``;`` separator, decimal comma)."""
    header = [
        "fecha",
        "hora",
        "texto",
        "tipo",
        "visualizaciones",
        "alcance",
        "me_gusta",
        "comentarios",
        "compartidos",
        "guardados",
        "interacciones",
        "engagement_%",
        "enlace",
        "publicado_desde_la_app",
    ]
    if network in NATIVE_ONLY_NETWORKS:
        header = [h for h in header if h not in ("interacciones", "engagement_%", "alcance", "guardados")]
        pos = header.index("compartidos") + 1
        header[pos:pos] = ["minutos_vistos"]
    out: list[list[Any]] = []
    for r in rows:
        local = r.get("local")
        full = {
            "fecha": local.date().isoformat() if local else "",
            "hora": local.strftime("%H:%M") if local else "",
            "texto": " ".join(str(r.get("caption") or "").split()),
            "tipo": r.get("media_type_label") or "",
            "visualizaciones": r.get("views"),
            "alcance": r.get("reach"),
            "me_gusta": r.get("likes"),
            "comentarios": r.get("comments"),
            "compartidos": r.get("shares"),
            "guardados": r.get("saves"),
            "interacciones": r.get("interactions"),
            "engagement_%": r.get("engagement"),
            "minutos_vistos": r.get("watch_minutes"),
            "enlace": r.get("permalink") or "",
            "publicado_desde_la_app": bool(r.get("ours")),
        }
        out.append([full[h] for h in header])
    return _csv_text(header, out)


SNAPSHOT_CSV_METRICS: tuple[str, ...] = (
    "followers",
    "following",
    "posts_count",
    "views",
    "reach",
    "interactions",
    "profile_views",
    "likes_total",
    "video_count",
    "watch_minutes",
    "likes",
    "comments",
    "shares",
    "subs_gained",
    "subs_lost",
)


def snapshots_csv(db: Session, accounts: Iterable[Account], start: date, end: date) -> str:
    """Daily snapshots of ``accounts`` in ``[start, end]`` (one row per account and day)."""
    from planificador.models import NETWORK_LABELS

    header = ["fecha", "red", "cuenta", *SNAPSHOT_CSV_METRICS]
    out: list[list[Any]] = []
    for account in accounts:
        for row in snapshots_in(db, account.id, start, end):
            m = row.metrics or {}
            values = [_clean(_num(m.get(k))) for k in SNAPSHOT_CSV_METRICS]
            if str(account.network) in NATIVE_ONLY_NETWORKS:
                values[SNAPSHOT_CSV_METRICS.index("interactions")] = None
            out.append(
                [
                    row.day.isoformat(),
                    NETWORK_LABELS.get(str(account.network), str(account.network)),
                    account.display_name,
                    *values,
                ]
            )
    out.sort(key=lambda r: (r[0], r[1]))
    return _csv_text(header, out)


# ------------------------------------------------------------------------------------------ best times

# General audience-activity pattern (0..1 per brand-local hour) used until the brand has enough of its own
# published posts with metrics. Hand-tuned curves: low at night, morning ramp, lunch and evening peaks.
_PATTERN_WORKDAY = (
    0.08, 0.05, 0.03, 0.03, 0.04, 0.08, 0.2, 0.38, 0.55, 0.62, 0.6, 0.64,
    0.74, 0.78, 0.63, 0.55, 0.57, 0.66, 0.78, 0.88, 0.92, 0.82, 0.58, 0.3,
)  # fmt: skip
_PATTERN_WEEKEND = (
    0.12, 0.07, 0.05, 0.03, 0.03, 0.05, 0.09, 0.17, 0.32, 0.48, 0.62, 0.7,
    0.74, 0.72, 0.64, 0.58, 0.57, 0.62, 0.7, 0.78, 0.84, 0.78, 0.58, 0.34,
)  # fmt: skip
_PATTERN_DAY_WEIGHT = (0.95, 1.0, 1.0, 0.98, 0.92, 0.8, 0.84)
_PATTERN_TWEAKS: dict[str, tuple[dict[int, float], float]] = {
    # network: ({hour: additive tweak}, weekend weight bonus)
    "facebook": ({8: 0.05, 9: 0.08, 10: 0.07, 12: 0.05, 13: 0.05, 22: -0.08, 23: -0.06}, 0.0),
    "instagram": ({11: 0.04, 12: 0.07, 13: 0.07, 19: 0.06, 20: 0.07, 21: 0.05, 7: -0.04}, 0.04),
    "tiktok": ({18: 0.05, 19: 0.08, 20: 0.1, 21: 0.12, 22: 0.1, 23: 0.06, 9: -0.07, 10: -0.05}, 0.1),
    "youtube": ({14: 0.04, 15: 0.06, 16: 0.08, 17: 0.08, 18: 0.06, 9: -0.05}, 0.12),
}


def general_pattern(network: str | None = None) -> list[list[float]]:
    """7×24 general pattern ("Patrón general"), Monday = 0, normalised so the best slot is 1.0."""
    tweaks, weekend_bonus = _PATTERN_TWEAKS.get(network or "", ({}, 0.0))
    raw: list[list[float]] = []
    for day in range(7):
        weekend = day >= 5
        curve = _PATTERN_WEEKEND if weekend else _PATTERN_WORKDAY
        weight = _PATTERN_DAY_WEIGHT[day] + (weekend_bonus if weekend else 0.0)
        raw.append([max(0.0, (curve[h] + tweaks.get(h, 0.0)) * weight) for h in range(24)])
    top = max(max(row) for row in raw) or 1.0
    return [[round(v / top, 3) for v in row] for row in raw]


# Kernel that spreads each observation to nearby slots of the 168-hour week ring.
_KERNEL: tuple[tuple[int, float], ...] = ((0, 1.0), (-1, 0.5), (1, 0.5), (-2, 0.2), (2, 0.2))
_SAME_HOUR_WEIGHT = 0.12  # the same hour on the other days of the week
_PRIOR_STRENGTH = 1.0
_PRIOR_FLOOR = 0.35
_PRIOR_CEIL = 0.85  # unexplored slots stay below the data mean


def best_times_matrix(
    samples: Iterable[tuple[int, int, float]], network: str | None = None
) -> list[list[float]]:
    """Matrix from ``(weekday, hour, score)`` samples: kernel-smoothed averages shrunk towards the general
    pattern (scaled below the data mean, so unexplored slots never beat good observed ones), normalised so
    the best slot is 1.0."""
    data = [(int(d) % 7, int(h) % 24, float(s)) for d, h, s in samples]
    if not data:
        return general_pattern(network)
    pattern = general_pattern(network)
    mean = sum(s for _, _, s in data) / len(data)
    wsum = [0.0] * 168
    wscore = [0.0] * 168
    for d, h, s in data:
        i = d * 24 + h
        for offset, w in _KERNEL:
            j = (i + offset) % 168
            wsum[j] += w
            wscore[j] += w * s
        for other in range(7):
            if other != d:
                j = other * 24 + h
                wsum[j] += _SAME_HOUR_WEIGHT
                wscore[j] += _SAME_HOUR_WEIGHT * s
    values: list[float] = []
    for i in range(168):
        d, h = divmod(i, 24)
        prior = mean * (_PRIOR_FLOOR + (_PRIOR_CEIL - _PRIOR_FLOOR) * pattern[d][h])
        values.append((wscore[i] + _PRIOR_STRENGTH * prior) / (wsum[i] + _PRIOR_STRENGTH))
    top = max(values)
    if top <= 0:
        return general_pattern(network)
    return [[round(max(0.0, values[d * 24 + h] / top), 3) for h in range(24)] for d in range(7)]


def best_times_samples(
    db: Session, brand_id: int, network: str | None, clock: Clock
) -> list[tuple[int, int, float]]:
    """``(weekday, hour, score)`` of our published targets with metrics (brand-local, last year).
    Score: engagement; for YouTube (native metrics only) views. "All networks" leaves YouTube out."""
    brand = db.get(Brand, brand_id)
    tz = brand.timezone if brand is not None else None
    since = clock.now() - timedelta(days=BEST_TIMES_LOOKBACK_DAYS)
    stmt = (
        select(PostMetric, PostTarget.published_at, Account.network)
        .join(PostTarget, PostMetric.target_id == PostTarget.id)
        .join(Account, PostMetric.account_id == Account.id)
        .where(Account.brand_id == brand_id, PostTarget.status.in_(list(PUBLISHED_STATUSES)))
    )
    if network:
        stmt = stmt.where(Account.network == network)
    samples: list[tuple[int, int, float]] = []
    for metric, target_published, acc_network in db.execute(stmt).all():
        net = str(acc_network)
        when = target_published or metric.published_at
        if when is None or ensure_utc(when) < since:
            continue
        m = metric.metrics or {}
        if net in NATIVE_ONLY_NETWORKS:
            if network != net:
                continue  # YouTube views are not comparable with other networks' engagement
            score = _num(m.get("views"))
        else:
            score = _num(m.get("engagement"))
            if score is None:
                score = engagement(m, net)
        if score is None:
            continue
        local = to_brand_local(when, tz)
        assert local is not None
        samples.append((local.weekday(), local.hour, score))
    return samples


def best_times(db: Session, brand_id: int, network: str | None, clock: Clock) -> dict[str, Any]:
    """Best publishing times for a brand (and optionally one network).

    Returns ``{"matrix": 7×24 floats 0..1 (Mon = 0, brand-local hours), "source": "datos" | "patron_general",
    "posts": int}``. ``source == "datos"`` needs ≥ :data:`BEST_TIMES_MIN_POSTS` of our published posts with
    metrics in the last year; otherwise the general pattern of the network is returned.
    """
    samples = best_times_samples(db, brand_id, network, clock)
    if len(samples) < BEST_TIMES_MIN_POSTS:
        return {"matrix": general_pattern(network), "source": "patron_general", "posts": len(samples)}
    return {"matrix": best_times_matrix(samples, network), "source": "datos", "posts": len(samples)}


__all__ = [
    "BEST_TIMES_MIN_POSTS",
    "FLOW_METRICS",
    "METRIC_LABELS",
    "NATIVE_ONLY_NETWORKS",
    "PERIOD_PRESETS",
    "STOCK_METRICS",
    "Period",
    "best_times",
    "best_times_matrix",
    "change_pct",
    "clean_number",
    "engagement",
    "follower_balance",
    "general_pattern",
    "kpis",
    "metric_label",
    "metric_value",
    "normalize_post_metrics",
    "post_rows",
    "posts_csv",
    "posts_per_day",
    "preset_period",
    "refresh_status",
    "resolve_period",
    "series",
    "snapshot_all",
    "snapshots_csv",
    "start_background_refresh",
    "top_posts",
]

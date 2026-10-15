"""Background scheduler: due posts, retries, crash recovery, sleep/wake catch-up and periodic jobs (SPEC §10).

One daemon thread ticks every :data:`TICK_SECONDS` (sooner when a wait/backoff or a scheduled time is closer,
or when :func:`wake` is called after "Publicar ahora"/save). Each tick:

1. **Heartbeat** — persists ``scheduler_last_tick_at`` and ``scheduler_online_since`` in ``settings``. A
   wall-clock gap > 2 min since the previous tick (PC asleep, app closed) starts a new "online since" period
   and triggers a catch-up (token refresh check now).
2. **Stale claims** — ``publishing`` targets without ``next_attempt_at`` whose claim is older than 10 min (or
   any, on the first tick after start: the single-instance lock means they belong to a dead process) are
   re-claimed: reconcile if ``commit_phase == commit_sent``, else continue from the saved provider state.
3. **Waiting targets** — ``publishing`` with ``next_attempt_at <= now`` (provider waits and retry backoffs).
4. **Due targets** — ``scheduled`` with ``scheduled_at <= now`` (post not in the trash):
   auto-publish off (and not TikTok inbox mode) → ``manual_pending``; due before the current "online since"
   period and later than the grace window (``grace_minutes``) → ``missed``; otherwise claimed and run.
   Facebook/YouTube targets with ``native_schedule`` are claimed **ahead of time** (within 28 days) so the
   provider can hand them to the network's own scheduler.
5. **Periodic jobs** in their own thread (never blocking publishing): token refresh (hourly), analytics
   snapshot (daily after 06:00 local, or when older than 24 h), comment polling (15 min), trash purge
   (> 45 days, daily), public-media token / OAuth state cleanup (hourly), WAL checkpoint (daily). Last-run
   info is stored in ``settings`` under ``scheduler_job:<name>``.

Publishing runs in worker threads: at most :data:`DEFAULT_MAX_WORKERS` at once and one per account.
``inline=True`` runs everything synchronously inside :meth:`Scheduler.tick` (tests, CLI).

Module API used by the app: :func:`start` (``app`` or ``app.state``), :func:`stop`, :func:`wake`,
:func:`get_scheduler`, :func:`status`.
"""

from __future__ import annotations

import inspect
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from planificador import __version__, config, engine, keepawake
from planificador.models import (
    Account,
    AccountStatus,
    CommitPhase,
    NotificationKind,
    OAuthState,
    Post,
    PostTarget,
    PublicMediaToken,
    TargetStatus,
)
from planificador.providers.base import ErrorKind, ProviderError, scrub_text
from planificador.timeutil import Clock, SystemClock, ensure_utc, format_dt_es, to_brand_local

log = logging.getLogger(__name__)

TICK_SECONDS = 15.0
MIN_SLEEP_SECONDS = 0.5
GAP_THRESHOLD = timedelta(minutes=2)
DEFAULT_MAX_WORKERS = 2
DUE_BATCH = 100
STOP_TIMEOUT = 10.0

HEARTBEAT_KEY = "scheduler_last_tick_at"
ONLINE_SINCE_KEY = "scheduler_online_since"
JOB_KEY_PREFIX = "scheduler_job:"

TRASH_RETENTION = timedelta(days=45)
PUBLIC_TOKEN_RETENTION = timedelta(days=1)
OAUTH_STATE_RETENTION = timedelta(hours=1)
ANALYTICS_LOCAL_HOUR = 6
NATIVE_HANDOFF_WINDOW = timedelta(days=28)  # Facebook accepts scheduled_publish_time up to ~29-30 days ahead
TOKEN_WARN_WINDOW = timedelta(days=7)
REFRESH_WARN_WINDOW = timedelta(days=3)


# --------------------------------------------------------------------------------------------- types


@dataclass
class TickReport:
    """What one tick did (returned by :meth:`Scheduler.tick`; handy for tests and diagnostics)."""

    now: datetime
    first: bool = False
    gap: bool = False
    gap_seconds: float = 0.0
    recovered: list[int] = field(default_factory=list)
    continued: list[int] = field(default_factory=list)
    claimed: list[int] = field(default_factory=list)
    manual: list[int] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)
    deferred: list[int] = field(default_factory=list)
    native: list[int] = field(default_factory=list)  # handed to the network's scheduler ahead of time
    outcomes: dict[int, str] = field(default_factory=dict)  # inline mode only
    jobs: list[str] = field(default_factory=list)


JobFn = Callable[["Scheduler", datetime], Any]
DueFn = Callable[["Scheduler", Session, datetime, datetime | None], bool]


@dataclass
class Job:
    """A periodic job. Due when never run, ``interval`` elapsed, forced, or ``due(...)`` says so."""

    name: str
    label: str  # Spanish, for a diagnostics page
    run: JobFn
    interval: timedelta | None = None
    due: DueFn | None = None


# ----------------------------------------------------------------------------------------- scheduler


class Scheduler:
    def __init__(
        self,
        database: Any,
        clock: Clock | None = None,
        *,
        base_url: str | None = None,
        public_media: Any = None,
        http: httpx.Client | None = None,
        tick_seconds: float = TICK_SECONDS,
        max_workers: int = DEFAULT_MAX_WORKERS,
        inline: bool = False,
        jobs: list[Job] | None = None,
        enable_jobs: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self.database = database
        self.session_factory: Callable[[], Session] = getattr(database, "session", database)
        self.clock: Clock = clock or SystemClock()
        self.base_url = base_url
        self.tick_seconds = float(tick_seconds)
        self.max_workers = max(1, int(max_workers))
        self.inline = inline
        self.enable_jobs = enable_jobs
        self.jobs: list[Job] = list(jobs) if jobs is not None else default_jobs()
        self._own_http = http is None
        self.http: httpx.Client = http or _new_http_client()

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()  # running sets
        self._tick_lock = threading.Lock()  # one tick at a time
        self._thread: threading.Thread | None = None
        self._workers: dict[int, threading.Thread] = {}
        self._running: dict[int, int] = {}  # target id -> account id
        self._jobs_thread: threading.Thread | None = None
        self._force_jobs: set[str] = set()
        self._first_tick = True

        self.online_since: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.ticks = 0
        self.runtime = engine.EngineRuntime(
            session_factory=self.session_factory,
            clock=self.clock,
            http=self.http,
            public_media=public_media,
            base_url=base_url,
            write_lock=getattr(database, "write_lock", None),
            stop_event=self._stop,
            rng=rng or random.Random(),
        )

    # ---- lifecycle

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the loop thread (no-op when already running)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                log.warning("Scheduler loop already running; start() ignored")
                return
            if self.http.is_closed and self._own_http:
                self.http = _new_http_client()
                self.runtime.http = self.http
            self._stop.clear()
            self._wake.clear()
            self._first_tick = True
            self._thread = threading.Thread(target=self._loop, name="planificador-scheduler", daemon=True)
            self._thread.start()
        log.info("Scheduler started (tick %.0f s, %d workers)", self.tick_seconds, self.max_workers)

    def stop(self, timeout: float = STOP_TIMEOUT) -> bool:
        """Stop the loop, let running publishes pause at their next step and wait up to ``timeout``.
        Returns True when everything finished (then the HTTP client is closed)."""
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + max(0.0, timeout)
        threads: list[threading.Thread] = []
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, deadline - time.monotonic()))
            threads.append(self._thread)
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        threads.extend(workers)
        if self._jobs_thread is not None:
            self._jobs_thread.join(max(0.0, deadline - time.monotonic()))
            threads.append(self._jobs_thread)
        clean = not any(t.is_alive() for t in threads)
        if clean:
            self.close()
            log.info("Scheduler stopped")
        else:
            log.warning("Scheduler stopped with work still running; it resumes on the next start")
        return clean

    def close(self) -> None:
        if self._own_http and not self.http.is_closed:
            self.http.close()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        hold = keepawake.ThreadHold()  # per-thread flags: owned by this long-lived thread
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("Scheduler tick failed")
                hold.update(self._has_publishing())
                if self._stop.is_set():
                    break
                self._wake.wait(self._sleep_seconds())
                self._wake.clear()
        finally:
            hold.release()

    def _has_publishing(self) -> bool:
        """Whether any target is mid-publish (running or waiting between steps): keep the PC awake."""
        if self.running_targets():
            return True
        try:
            with self.session() as s:
                return (
                    s.scalar(
                        select(PostTarget.id).where(PostTarget.status == TargetStatus.PUBLISHING).limit(1)
                    )
                    is not None
                )
        except Exception:
            log.debug("Could not check for running publishes", exc_info=True)
            return False

    def _sleep_seconds(self) -> float:
        try:
            now = self.clock.now()
            with self.session() as s:
                next_wait = s.scalar(
                    select(func.min(PostTarget.next_attempt_at)).where(
                        PostTarget.status.in_([TargetStatus.PUBLISHING, TargetStatus.SCHEDULED]),
                        PostTarget.next_attempt_at.is_not(None),
                    )
                )
                next_due = s.scalar(
                    select(func.min(PostTarget.scheduled_at)).where(
                        PostTarget.status == TargetStatus.SCHEDULED, PostTarget.scheduled_at > now
                    )
                )
            seconds = [self.tick_seconds]
            for at in (next_wait, next_due):
                # Past deadlines are running or waiting for a free worker; a finishing worker wakes us.
                if at is not None and ensure_utc(at) > now:
                    seconds.append((ensure_utc(at) - now).total_seconds())
            return max(MIN_SLEEP_SECONDS, min(seconds))
        except Exception:
            log.debug("Could not compute the next wake-up", exc_info=True)
            return self.tick_seconds

    def session(self) -> Session:
        return self.session_factory()

    # ---- tick

    def tick(self) -> TickReport:
        """One scheduling pass (thread-safe; ticks never overlap)."""
        with self._tick_lock:
            now = self.clock.now()
            report = TickReport(now=now, first=self._first_tick)
            try:
                self._heartbeat(now, report)
            except Exception:
                log.exception("Scheduler heartbeat failed")
                if self.online_since is None:
                    self.online_since = now
            for phase in (
                self._recover_stale,
                self._continue_waiting,
                self._process_due,
                self._process_native,
            ):
                try:
                    phase(now, report)
                except Exception:
                    log.exception("Scheduler phase %s failed", phase.__name__)
            try:
                self._schedule_jobs(now, report)
            except Exception:
                log.exception("Scheduler jobs failed")
            self._first_tick = False
            self.ticks += 1
            return report

    def _heartbeat(self, now: datetime, report: TickReport) -> None:
        with self.session() as s:
            prev = config.get_timestamp(s, HEARTBEAT_KEY)
            online = config.get_timestamp(s, ONLINE_SINCE_KEY)
            gap = prev is not None and (now - prev > GAP_THRESHOLD or prev - now > GAP_THRESHOLD)
            if prev is None or online is None or gap:
                online = now
            config.set_timestamp(s, HEARTBEAT_KEY, now, commit=False)
            config.set_timestamp(s, ONLINE_SINCE_KEY, online, commit=False)
            s.commit()
        self.online_since = online
        self.last_tick_at = now
        if gap and prev is not None:
            report.gap = True
            report.gap_seconds = (now - prev).total_seconds()
            log.info(
                "Wall-clock gap of %.0f s since the last tick (sleep or app closed): catching up",
                report.gap_seconds,
            )
            self._force_jobs.add("token_refresh")

    # ---- capacity

    def _capacity(self, account_id: int) -> bool:
        with self._lock:
            if len(self._running) >= self.max_workers:
                return False
            return account_id not in self._running.values()

    def _busy(self, target_id: int) -> bool:
        with self._lock:
            if target_id in self._running:
                return True
        return engine.is_running(target_id)

    def running_targets(self) -> dict[int, int]:
        with self._lock:
            return dict(self._running)

    def _launch(self, target_id: int, account_id: int, *, reconcile: bool, report: TickReport) -> None:
        with self._lock:
            self._running[target_id] = account_id
        if self.inline:
            try:
                report.outcomes[target_id] = engine.run_target(target_id, self.runtime, reconcile=reconcile)
            except Exception:
                log.exception("Publishing run crashed for target %s", target_id)
                report.outcomes[target_id] = "crashed"
            finally:
                with self._lock:
                    self._running.pop(target_id, None)
            return
        thread = threading.Thread(
            target=self._worker,
            args=(target_id, reconcile),
            name=f"planificador-publish-{target_id}",
            daemon=True,
        )
        with self._lock:
            self._workers[target_id] = thread
        thread.start()

    def _worker(self, target_id: int, reconcile: bool) -> None:
        try:
            engine.run_target(target_id, self.runtime, reconcile=reconcile)
        except Exception:
            log.exception("Publishing worker crashed for target %s", target_id)
        finally:
            with self._lock:
                self._running.pop(target_id, None)
                self._workers.pop(target_id, None)
            self._wake.set()

    # ---- phases

    def _recover_stale(self, now: datetime, report: TickReport) -> None:
        threshold = now - engine.STALE_CLAIM_AFTER
        with self.session() as s:
            rows = s.execute(
                select(
                    PostTarget.id, PostTarget.account_id, PostTarget.claimed_at, PostTarget.commit_phase
                ).where(PostTarget.status == TargetStatus.PUBLISHING, PostTarget.next_attempt_at.is_(None))
            ).all()
            for target_id, account_id, claimed_at, phase in rows:
                if self._busy(target_id):
                    continue
                if not (self._first_tick or claimed_at is None or claimed_at <= threshold):
                    continue
                if not self._capacity(account_id):
                    report.deferred.append(target_id)
                    continue
                if engine.claim_stale(s, target_id, claimed_at, now):
                    report.recovered.append(target_id)
                    log.warning(
                        "Recovering abandoned publish of target %s (commit phase %s)", target_id, phase
                    )
                    self._launch(
                        target_id, account_id, reconcile=phase == CommitPhase.COMMIT_SENT, report=report
                    )

    def _continue_waiting(self, now: datetime, report: TickReport) -> None:
        with self.session() as s:
            rows = s.execute(
                select(PostTarget.id, PostTarget.account_id, PostTarget.next_attempt_at)
                .where(
                    PostTarget.status == TargetStatus.PUBLISHING,
                    PostTarget.next_attempt_at.is_not(None),
                    PostTarget.next_attempt_at <= now,
                )
                .order_by(PostTarget.next_attempt_at, PostTarget.id)
            ).all()
            for target_id, account_id, next_attempt_at in rows:
                if self._busy(target_id):
                    continue
                if not self._capacity(account_id):
                    report.deferred.append(target_id)
                    continue
                if engine.claim_waiting(s, target_id, next_attempt_at, now):
                    report.continued.append(target_id)
                    self._launch(target_id, account_id, reconcile=False, report=report)

    def _process_due(self, now: datetime, report: TickReport) -> None:
        with self.session() as s:
            grace = timedelta(minutes=max(0, config.get_int(s, "grace_minutes")))
            online_since = self.online_since or now
            targets = list(
                s.scalars(
                    select(PostTarget)
                    .where(engine.due_filter(now))
                    .order_by(PostTarget.scheduled_at, PostTarget.id)
                    .limit(DUE_BATCH)
                )
            )
            for target in targets:
                if self._busy(target.id):
                    continue
                if not target.auto_publish and not engine.is_tiktok_inbox(target.network, target.options):
                    if engine.set_manual_pending(s, target, now):
                        report.manual.append(target.id)
                    continue
                reference = engine.due_reference(target) or now
                late = now - reference
                if late > grace and reference < online_since:
                    if engine.set_missed(s, target, now, grace=grace):
                        report.missed.append(target.id)
                    continue
                if not self._capacity(target.account_id):
                    report.deferred.append(target.id)
                    continue
                if engine.claim_due(s, target.id, now, late_by=late):
                    report.claimed.append(target.id)
                    self._launch(target.id, target.account_id, reconcile=False, report=report)

    def _process_native(self, now: datetime, report: TickReport) -> None:
        """Facebook/YouTube targets with ``native_schedule`` are handed to the network's own scheduler ahead
        of time (so they go out even if the PC is off), once within :data:`NATIVE_HANDOFF_WINDOW`. The
        provider decides whether the lead time allows it; otherwise it waits and publishes at the time."""
        with self.session() as s:
            targets = list(
                s.scalars(
                    select(PostTarget)
                    .where(
                        PostTarget.status == TargetStatus.SCHEDULED,
                        PostTarget.network.in_(engine.NATIVE_SCHEDULE_NETWORKS),
                        PostTarget.auto_publish.is_(True),
                        PostTarget.scheduled_at > now,
                        PostTarget.scheduled_at <= now + NATIVE_HANDOFF_WINDOW,
                        or_(PostTarget.next_attempt_at.is_(None), PostTarget.next_attempt_at <= now),
                        PostTarget.post.has(Post.deleted_at.is_(None)),
                    )
                    .order_by(PostTarget.scheduled_at, PostTarget.id)
                    .limit(DUE_BATCH)
                )
            )
            for target in targets:
                if self._busy(target.id) or not engine.wants_native_schedule(
                    target.network, target.options, target.format
                ):
                    continue
                if not self._capacity(target.account_id):
                    report.deferred.append(target.id)
                    continue
                if engine.claim_due(s, target.id, now):
                    report.claimed.append(target.id)
                    report.native.append(target.id)
                    self._launch(target.id, target.account_id, reconcile=False, report=report)

    # ---- periodic jobs

    def force_job(self, name: str) -> None:
        """Run ``name`` on the next tick regardless of its schedule (e.g. "Actualizar ahora")."""
        self._force_jobs.add(name)
        self.wake()

    def _schedule_jobs(self, now: datetime, report: TickReport) -> None:
        if not self.enable_jobs or not self.jobs or self._stop.is_set():
            return
        with self._lock:
            if self._jobs_thread is not None and self._jobs_thread.is_alive():
                return
        forced = set(self._force_jobs)
        self._force_jobs.difference_update(forced)
        with self.session() as s:
            due = [job for job in self.jobs if job.name in forced or self._job_due(s, job, now)]
        if not due:
            return
        report.jobs = [job.name for job in due]
        if self.inline:
            self._run_jobs(due, now)
            return
        thread = threading.Thread(
            target=self._run_jobs, args=(due, now), name="planificador-jobs", daemon=True
        )
        with self._lock:
            self._jobs_thread = thread
        thread.start()

    def _job_due(self, s: Session, job: Job, now: datetime) -> bool:
        last = _parse_ts(job_record(s, job.name).get("last_run_at"))
        if job.due is not None:
            return bool(job.due(self, s, now, last))
        if last is None or now < last:
            return True
        return job.interval is not None and now - last >= job.interval

    def _run_jobs(self, jobs: list[Job], now: datetime) -> None:
        for job in jobs:
            if self._stop.is_set():
                break
            self.run_job(job, now)

    def run_job(self, job: Job, now: datetime | None = None) -> dict[str, Any]:
        """Run one job now and record its outcome. Never raises."""
        now = now or self.clock.now()
        started = time.monotonic()
        status, error, result = "ok", None, None
        try:
            result = job.run(self, now)
        except NotImplementedError:
            status = "not_implemented"
        except Exception as exc:
            status = "error"
            error = scrub_text(f"{type(exc).__name__}: {exc}")[:300]
            log.exception("Periodic job %s failed", job.name)
        record: dict[str, Any] = {
            "last_run_at": now.isoformat(),
            "status": status,
            "last_error": error,
            "duration_s": round(time.monotonic() - started, 3),
            "result": _json_safe(result),
        }
        try:
            with self.session() as s:
                previous = job_record(s, job.name)
                record["last_ok_at"] = now.isoformat() if status == "ok" else previous.get("last_ok_at")
                config.set_setting(s, JOB_KEY_PREFIX + job.name, record)
        except Exception:
            log.exception("Could not record the run of job %s", job.name)
        return record

    # ---- diagnostics

    def status(self) -> dict[str, Any]:
        jobs: dict[str, Any] = {}
        try:
            with self.session() as s:
                for job in self.jobs:
                    jobs[job.name] = {"label": job.label, **job_record(s, job.name)}
        except Exception:
            log.debug("Could not read job records", exc_info=True)
        return {
            "running": self.is_running,
            "online_since": self.online_since.isoformat() if self.online_since else None,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "ticks": self.ticks,
            "publishing": sorted(self.running_targets()),
            "jobs": jobs,
        }


def _new_http_client() -> httpx.Client:
    return httpx.Client(timeout=engine.HTTP_TIMEOUT, headers={"User-Agent": f"Planificador/{__version__}"})


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)[:300]


def job_record(s: Session, name: str) -> dict[str, Any]:
    value = config.get_setting(s, JOB_KEY_PREFIX + name, None)
    return dict(value) if isinstance(value, dict) else {}


def _call_compat(fn: Callable[..., Any], *args: Any) -> Any:
    """Call ``fn`` with as many leading ``args`` as its signature accepts (contract drift tolerance)."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args)
    for count in range(len(args), -1, -1):
        try:
            signature.bind(*args[:count])
        except TypeError:
            continue
        return fn(*args[:count])
    return fn(*args)


# -------------------------------------------------------------------------------------------- jobs


def job_token_refresh(sched: Scheduler, now: datetime) -> dict[str, int]:
    """Refresh expiring tokens of connected accounts; flag broken ones; warn before Meta/TikTok/YouTube
    authorisations expire."""
    from planificador.providers import registry

    summary = {"checked": 0, "refreshed": 0, "reconnect": 0, "warnings": 0}
    with sched.session() as s:
        settings = config.provider_settings(s, base_url=sched.base_url)
        accounts = list(
            s.scalars(select(Account).where(Account.status == AccountStatus.CONNECTED).order_by(Account.id))
        )
        for account in accounts:
            summary["checked"] += 1
            try:
                provider = registry.get_provider(str(account.network))
            except Exception:
                continue
            try:
                update_ = provider.refresh_credentials(account.to_creds(), settings, sched.http, sched.clock)
            except NotImplementedError:
                update_ = None
            except ProviderError as exc:
                if exc.kind == ErrorKind.AUTH:
                    if engine.mark_account_needs_reconnect(s, account.id, exc.message, now):
                        summary["reconnect"] += 1
                    s.commit()
                else:
                    log.warning("Token refresh for account %s failed: %s", account.id, exc.code)
                continue
            except Exception:
                log.exception("Token refresh crashed for account %s", account.id)
                continue
            if update_ is not None:
                account.apply_token_update(update_)
                account.last_checked_at = now
                s.commit()
                summary["refreshed"] += 1
            summary["warnings"] += _expiry_warnings(s, account, now)
    return summary


def _expiry_warnings(s: Session, account: Account, now: datetime) -> int:
    from planificador import notify

    checks: list[tuple[str, datetime, timedelta]] = []
    if account.token_expires_at is not None and not account.refresh_token_enc:
        checks.append(("token_expiry_warned", account.token_expires_at, TOKEN_WARN_WINDOW))
    if account.refresh_expires_at is not None:
        checks.append(("refresh_expiry_warned", account.refresh_expires_at, REFRESH_WARN_WINDOW))
    count = 0
    for key, expires, window in checks:
        if expires - now > window:
            continue
        stamp = expires.isoformat()
        if (account.meta or {}).get(key) == stamp:
            continue
        label = engine.network_label(account.network)
        tz = account.brand.timezone if account.brand is not None else None
        when = format_dt_es(expires, tz)
        if expires <= now:
            title = f"Tu conexión con {label} ha caducado"
            body = f"La autorización de {account.display_name} caducó el {when}."
        else:
            title = f"Tu conexión con {label} caduca pronto"
            body = f"La autorización de {account.display_name} caduca el {when}."
        body += " Reconéctala en Cuentas conectadas para que tus publicaciones sigan saliendo."
        notify.notify(
            s, NotificationKind.RECONNECT, title, body, account_id=account.id, commit=False, now=now
        )
        meta = dict(account.meta or {})
        meta[key] = stamp
        account.meta = meta
        s.commit()
        count += 1
    return count


def analytics_due(sched: Scheduler, s: Session, now: datetime, last: datetime | None) -> bool:
    """Daily after 06:00 (default time zone), and whenever the last snapshot is older than 24 h."""
    if last is None or now < last or now - last >= timedelta(hours=24):
        return True
    local_now = to_brand_local(now, config.default_timezone(s))
    assert local_now is not None
    cutoff_local = local_now.replace(hour=ANALYTICS_LOCAL_HOUR, minute=0, second=0, microsecond=0)
    if local_now < cutoff_local:
        return False
    return last < cutoff_local.astimezone(UTC)


def job_analytics_snapshot(sched: Scheduler, now: datetime) -> Any:
    from planificador import analytics

    fn = getattr(analytics, "snapshot_all", None)
    if fn is None:
        raise NotImplementedError("analytics.snapshot_all")
    with sched.session() as s:
        settings = config.provider_settings(s, base_url=sched.base_url)
        result = _call_compat(fn, s, sched.http, sched.clock, settings)
        s.commit()
    return result


def job_inbox_poll(sched: Scheduler, now: datetime) -> Any:
    from planificador import inbox

    fn = getattr(inbox, "poll_all", None)
    if fn is None:
        raise NotImplementedError("inbox.poll_all")
    with sched.session() as s:
        settings = config.provider_settings(s, base_url=sched.base_url)
        result = _call_compat(fn, s, sched.http, sched.clock, settings)
        s.commit()
    return result


def job_trash_purge(sched: Scheduler, now: datetime) -> dict[str, int]:
    """Delete posts that have been in the trash for more than 45 days (targets/events cascade)."""
    cutoff = now - TRASH_RETENTION
    with sched.session() as s:
        posts = list(s.scalars(select(Post).where(Post.deleted_at.is_not(None), Post.deleted_at < cutoff)))
        for post in posts:
            s.delete(post)
        s.commit()
    if posts:
        log.info("Purged %d post(s) from the trash", len(posts))
    return {"purged": len(posts)}


def job_cleanup(sched: Scheduler, now: datetime) -> dict[str, int]:
    """Forget expired/revoked public media tokens and abandoned OAuth states."""
    with sched.session() as s:
        token_cutoff = now - PUBLIC_TOKEN_RETENTION
        tokens = s.execute(
            delete(PublicMediaToken).where(
                or_(PublicMediaToken.expires_at < token_cutoff, PublicMediaToken.revoked_at < token_cutoff)
            )
        ).rowcount
        states = s.execute(
            delete(OAuthState).where(OAuthState.created_at < now - OAUTH_STATE_RETENTION)
        ).rowcount
        s.commit()
    return {"public_media_tokens": int(tokens or 0), "oauth_states": int(states or 0)}


def job_db_checkpoint(sched: Scheduler, now: datetime) -> str:
    checkpoint = getattr(sched.database, "checkpoint", None)
    if checkpoint is None:
        return "skipped"
    checkpoint()
    return "ok"


def default_jobs() -> list[Job]:
    return [
        Job("token_refresh", "Renovación de accesos", job_token_refresh, interval=timedelta(hours=1)),
        Job("analytics_snapshot", "Estadísticas diarias", job_analytics_snapshot, due=analytics_due),
        Job("inbox_poll", "Comentarios", job_inbox_poll, interval=timedelta(minutes=15)),
        Job("trash_purge", "Vaciado de la papelera", job_trash_purge, interval=timedelta(hours=24)),
        Job("cleanup", "Limpieza de enlaces temporales", job_cleanup, interval=timedelta(hours=1)),
        Job(
            "db_checkpoint",
            "Mantenimiento de la base de datos",
            job_db_checkpoint,
            interval=timedelta(hours=24),
        ),
    ]


# ---------------------------------------------------------------------------------------- module API

_instance_lock = threading.Lock()
_instance: Scheduler | None = None


def _public_media_getter(app: Any) -> Callable[[], Any]:
    def get() -> Any:
        state = getattr(app, "state", app)
        service = getattr(state, "public_media", None)
        if service is not None:
            return service
        try:
            from planificador import publicmedia

            fn = getattr(publicmedia, "get_service", None)
            if fn is not None:
                return fn(app)
        except NotImplementedError:
            return None
        except Exception:
            log.debug("publicmedia.get_service failed", exc_info=True)
        return None

    return get


def start(app: Any) -> Scheduler:
    """Start the process-wide scheduler for ``app`` (a FastAPI app or its ``app.state``). Idempotent: a second
    call while it runs returns the running instance (single loop guard)."""
    global _instance
    state = getattr(app, "state", app)
    with _instance_lock:
        if _instance is not None and _instance.is_running:
            log.warning("Scheduler already running; start() ignored")
            return _instance
        settings = getattr(state, "settings", None)
        sched = Scheduler(
            state.db,
            getattr(state, "clock", None),
            base_url=getattr(settings, "base_url", None),
            public_media=_public_media_getter(app),
        )
        sched.start()
        _instance = sched
        try:
            state.scheduler = sched
        except Exception:  # pragma: no cover - read-only state object
            pass
    return sched


def stop(app: Any = None, timeout: float = STOP_TIMEOUT) -> None:
    """Stop the scheduler (clean shutdown: running publishes pause at their next step)."""
    global _instance
    with _instance_lock:
        sched, _instance = _instance, None
    if sched is not None:
        sched.stop(timeout)
    try:
        from planificador import notify

        notify.shutdown(1.0)
    except Exception:  # pragma: no cover
        log.debug("Desktop notifier shutdown failed", exc_info=True)


def wake() -> None:
    """Ask the scheduler to tick now (after "Publicar ahora" / save). No-op when it is not running."""
    sched = _instance
    if sched is not None:
        sched.wake()


def get_scheduler() -> Scheduler | None:
    return _instance


def status() -> dict[str, Any]:
    sched = _instance
    if sched is None:
        return {"running": False}
    return sched.status()


__all__ = [
    "GAP_THRESHOLD",
    "HEARTBEAT_KEY",
    "JOB_KEY_PREFIX",
    "ONLINE_SINCE_KEY",
    "TICK_SECONDS",
    "Job",
    "Scheduler",
    "TickReport",
    "default_jobs",
    "get_scheduler",
    "job_record",
    "start",
    "status",
    "stop",
    "wake",
]

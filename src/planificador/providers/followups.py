"""Follow-up checks after the engine has finished a target (SPEC §5 extension).

Some outcomes are only known some time after a publish "finished":

- TikTok public posts get their public id only after moderation, which can take hours (research
  ``03-tiktok-api.md`` §5.5 / §11.4): ``status/fetch`` hourly for 24 h, then daily up to 30 days, so per-post
  analytics and the video link can be matched.
- Videos handed to YouTube's scheduler (``publishAt``) must be read again after that time (research
  ``06-youtube-api.md`` §7 / §14.2): failed or rejected processing, or the "private lock".
- The YouTube lock test is confirmed 24 h after an ``unlocked`` result, then its clip is deleted (§3.7).

A provider asks for a check by putting ``state[FOLLOWUP_KEY] = {"next_at": iso, ...}`` into the target's
``provider_state`` and implements :meth:`Provider.follow_up`. :func:`run_due` runs from the scheduler job
``provider_followups`` (hourly) and applies the :class:`FollowUpResult`: new external id / URL, an event, a
notification, ``accounts.meta`` updates (``state["account_meta_updates"]``) or — when the post turned out not
to be live — the ``error`` status, so "Reintentar" is offered.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador.models import (
    Account,
    AccountStatus,
    EventLevel,
    NotificationKind,
    PostTarget,
    TargetStatus,
)
from planificador.providers.base import (
    FOLLOWUP_KEY,
    FollowUpResult,
    ProviderError,
    ProviderSettings,
    PublishContext,
    TargetSpec,
    TokenUpdate,
)
from planificador.timeutil import Clock

log = logging.getLogger(__name__)

FOLLOWUP_INTERVAL = timedelta(hours=1)
FOLLOWUP_WINDOW = timedelta(days=31)  # targets published longer ago are never checked again
MAX_PER_RUN = 50
ACCOUNT_META_UPDATES_KEY = "account_meta_updates"  # same key the engine applies (engine.py)

LOCK_TEST_META = "youtube_lock_test"
LOCK_RECHECK_WINDOW = timedelta(days=7)  # older lock tests are not re-checked
LOCK_RECHECK_MAX_ATTEMPTS = 6


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def job(sched: Any, now: datetime) -> dict[str, int]:
    """Scheduler job (``scheduler.default_jobs``): run the due follow-ups in one session."""
    from planificador import config

    with sched.session() as s:
        settings = config.provider_settings(s, base_url=sched.base_url)
        return run_due(s, sched.http, sched.clock, settings)


def run_due(db: Session, http: httpx.Client, clock: Clock, settings: ProviderSettings) -> dict[str, int]:
    """Run every due follow-up (targets, then YouTube lock-test confirmations). Commits after each item and
    never raises because of one item."""
    summary = {"checked": 0, "updated": 0, "failed": 0, "errors": 0, "lock_checks": 0}
    _run_targets(db, http, clock, settings, summary)
    _run_lock_rechecks(db, http, clock, settings, summary)
    return summary


# ------------------------------------------------------------------------------------------- targets


def _due_targets(db: Session, now: datetime) -> list[int]:
    rows = db.execute(
        select(PostTarget.id, PostTarget.provider_state).where(
            PostTarget.status == TargetStatus.PUBLISHED,
            PostTarget.published_at >= now - FOLLOWUP_WINDOW,
        )
    ).all()
    due: list[tuple[datetime, int]] = []
    for target_id, state in rows:
        info = state.get(FOLLOWUP_KEY) if isinstance(state, dict) else None
        if not isinstance(info, dict):
            continue
        next_at = _parse(info.get("next_at"))
        if next_at is not None and next_at <= now:
            due.append((next_at, target_id))
    due.sort()
    return [target_id for _, target_id in due[:MAX_PER_RUN]]


def _run_targets(
    db: Session, http: httpx.Client, clock: Clock, settings: ProviderSettings, summary: dict[str, int]
) -> None:
    from planificador.providers import registry

    now = clock.now()
    for target_id in _due_targets(db, now):
        try:
            target = db.get(PostTarget, target_id, populate_existing=True)
            if target is None or target.status != TargetStatus.PUBLISHED:
                continue
            provider = registry.get_provider(str(target.network))
            summary["checked"] += 1
            outcome = _follow_up_target(db, provider, target, http, clock, settings)
            if outcome == "failed":
                summary["failed"] += 1
            elif outcome == "updated":
                summary["updated"] += 1
            db.commit()
        except Exception:
            db.rollback()
            summary["errors"] += 1
            log.exception("Follow-up of target %s failed", target_id)
            _postpone(db, target_id, clock.now() + FOLLOWUP_INTERVAL)


def _postpone(db: Session, target_id: int, when: datetime) -> None:
    """After an unexpected error, try again later instead of every run."""
    try:
        target = db.get(PostTarget, target_id, populate_existing=True)
        if target is None:
            return
        state = copy.deepcopy(target.provider_state or {})
        info = state.get(FOLLOWUP_KEY)
        if not isinstance(info, dict):
            return
        errors = int(info.get("errors") or 0) + 1
        if errors >= 5:
            state.pop(FOLLOWUP_KEY, None)
        else:
            state[FOLLOWUP_KEY] = {**info, "errors": errors, "next_at": when.isoformat()}
        target.provider_state = state
        db.commit()
    except Exception:
        db.rollback()
        log.debug("Could not postpone the follow-up of target %s", target_id, exc_info=True)


def _target_spec(target: PostTarget) -> TargetSpec:
    from planificador.engine import target_text

    return TargetSpec(
        network=str(target.network),
        format=target.format,
        text=target_text(target),
        media=[],
        options=dict(target.options or {}),
        scheduled_at=_aware(target.scheduled_at),
        auto_publish=bool(target.auto_publish),
    )


def _token_saver(account: Account) -> Any:
    """Persist tokens a provider refreshed during the check (committed with the follow-up)."""

    def save(update: TokenUpdate) -> None:
        account.apply_token_update(update)

    return save


def _follow_up_target(
    db: Session,
    provider: Any,
    target: PostTarget,
    http: httpx.Client,
    clock: Clock,
    settings: ProviderSettings,
) -> str:
    from planificador import engine, notify

    now = clock.now()
    raw_state, engine_meta = engine.split_state(copy.deepcopy(target.provider_state or {}))
    state = dict(raw_state)  # a deep copy: in-place changes must not alias the loaded value
    account = db.get(Account, target.account_id)
    if account is None or account.status != AccountStatus.CONNECTED:
        state.pop(FOLLOWUP_KEY, None)  # nothing can be checked without a working account
        target.provider_state = engine.merge_state(state, engine_meta)
        return "skipped"

    def no_commit(_state: dict) -> None:
        raise RuntimeError("A follow-up check must never send a publication")

    events: list[tuple[str, str, dict | None]] = []
    ctx = PublishContext(
        account=account.to_creds(),
        target=_target_spec(target),
        target_id=target.id,
        state=state,
        settings=settings,
        http=http,
        clock=clock,
        public_media=None,
        save_state=lambda _s: None,  # everything is written once below
        before_commit=no_commit,
        log=lambda event, message, data: events.append((event, message, data)),
        update_tokens=_token_saver(account),
    )
    try:
        result: FollowUpResult | None = provider.follow_up(ctx)
    except ProviderError as exc:
        log.info("Follow-up of target %s: %s", target.id, exc.code)
        result = FollowUpResult(next_at=now + FOLLOWUP_INTERVAL)
    state = ctx.state
    result = result or FollowUpResult()
    if result.next_at is not None:
        info = state.get(FOLLOWUP_KEY) if isinstance(state.get(FOLLOWUP_KEY), dict) else {}
        state[FOLLOWUP_KEY] = {**info, "next_at": _aware(result.next_at).isoformat()}  # type: ignore[union-attr]
    else:
        state.pop(FOLLOWUP_KEY, None)

    updates = state.pop(ACCOUNT_META_UPDATES_KEY, None)
    if isinstance(updates, dict) and updates:
        account.meta = {**(account.meta or {}), **updates}

    for event, message, data in events:
        engine.add_event(db, target.id, now, event, message, data=data)
    label = engine.network_label(target.network)
    account_label = account.display_name
    outcome = "checked"
    if result.failed is not None:
        err = result.failed
        target.status = TargetStatus.ERROR
        target.last_error = err.message
        target.last_error_detail = err.to_dict()
        target.updated_at = now
        engine.add_event(
            db, target.id, now, "followup_failed", err.message, level=EventLevel.ERROR, data=err.to_dict()
        )
        notify.notify(
            db,
            NotificationKind.ERROR,
            f"Error al publicar en {label}",
            f"{account_label}: {err.message}",
            target_id=target.id,
            account_id=account.id,
            commit=False,
            now=now,
        )
        outcome = "failed"
    else:
        if result.external_id and result.external_id != target.external_id:
            target.external_id = result.external_id
            outcome = "updated"
        if result.external_url and result.external_url != target.external_url:
            target.external_url = result.external_url
            outcome = "updated"
        if result.message:
            engine.add_event(db, target.id, now, "followup", result.message, level=result.level)
        if result.notify:
            notify.notify(
                db,
                NotificationKind.ERROR if result.level == "error" else NotificationKind.INFO,
                f"Revisa tu publicación en {label}",
                f"{account_label}: {result.notify}",
                target_id=target.id,
                account_id=account.id,
                commit=False,
                now=now,
            )
    target.provider_state = engine.merge_state(state, engine_meta)
    return outcome


# --------------------------------------------------------------------------- YouTube lock test (§3.7)


def _run_lock_rechecks(
    db: Session, http: httpx.Client, clock: Clock, settings: ProviderSettings, summary: dict[str, int]
) -> None:
    from planificador.providers import youtube

    now = clock.now()
    accounts = db.scalars(
        select(Account).where(Account.network == "youtube", Account.status == AccountStatus.CONNECTED)
    ).all()
    for account in accounts:
        test = (account.meta or {}).get(LOCK_TEST_META)
        if not isinstance(test, dict) or test.get("result") != "unlocked" or test.get("confirmed_at"):
            continue
        finished = _parse(test.get("finished_at"))
        if finished is None or now - finished < youtube.LOCK_RECHECK_AFTER:
            continue
        if now - finished > LOCK_RECHECK_WINDOW:
            continue
        summary["lock_checks"] += 1
        try:
            result = youtube.confirm_lock_test(
                account.to_creds(),
                settings,
                http,
                clock,
                test.get("video_id"),
                since=finished - timedelta(hours=1),
                on_tokens=_token_saver(account),
            )
        except ProviderError as exc:
            attempts = int(test.get("recheck_attempts") or 0) + 1
            test = {**test, "recheck_attempts": attempts}
            if attempts >= LOCK_RECHECK_MAX_ATTEMPTS:
                test["confirmed_at"] = now.isoformat()
                test["recheck"] = "error"
            account.meta = {**(account.meta or {}), LOCK_TEST_META: test}
            db.commit()
            log.info("YouTube lock test re-check failed: %s", exc.code)
            continue
        except Exception:
            db.rollback()
            summary["errors"] += 1
            log.exception("YouTube lock test re-check crashed for account %s", account.id)
            continue
        _apply_lock_recheck(db, account, test, result, now)
        db.commit()


def _apply_lock_recheck(db: Session, account: Account, test: dict, result: Any, now: datetime) -> None:
    from planificador import notify
    from planificador.providers import youtube

    test = {**test, "confirmed_at": now.isoformat(), "recheck": result.status}
    meta = dict(account.meta or {})
    if result.status == "locked":
        meta[youtube.META_LOCK_STATUS] = "locked"
        meta[youtube.META_PUBLISH_MODE] = "assisted"
        test.update(result="locked", message=result.message)
        notify.notify(
            db,
            NotificationKind.ERROR,
            "YouTube bloquea las subidas automáticas",
            f"{account.display_name}: {result.message}",
            account_id=account.id,
            commit=False,
            now=now,
        )
    meta[LOCK_TEST_META] = test
    account.meta = meta


__all__ = ["FOLLOWUP_INTERVAL", "job", "run_due"]

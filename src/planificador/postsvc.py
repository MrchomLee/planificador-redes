"""Post service: the single place where posts and their per-network targets are created and changed.

Used by the composer routes, the planner (drag & drop, duplicate, trash/restore), the post detail drawer and
the CSV importer. Every mutating function validates its input, applies the change in **one transaction** and
commits it (pass ``commit=False`` to only flush, e.g. to import many posts atomically); on any error the
session is rolled back and :class:`PostError` (Spanish, user-facing message) is raised.

Contract (shared with other modules):

- :class:`TargetInput` / :class:`PostInput` — what the composer/CSV build. ``scheduled_at`` is an aware UTC
  instant (or ``None``); naive values are taken as UTC.
- :func:`create_post` / :func:`update_post` with ``action`` in :data:`ACTIONS`:

  ``schedule``     every target → ``scheduled`` at ``scheduled_at`` (must be in the future; validation errors
                   block it — the server re-validates whatever the browser said).
  ``draft``        targets → ``draft`` (errors allowed; the date is optional and kept).
  ``idea``         post kind ``idea`` (Ideas board, stage ``idea_stage`` or ``idea``), targets ``draft``.
  ``publish_now``  validated as if published now, saved, then handed to ``engine.publish_now`` (which commits
                   and wakes the scheduler).

- :func:`validate_post` ``(db, data) -> {account_id: [Issue]}`` — per-target issues from :mod:`rules` (with
  the account context: TikTok ``creator_info``, YouTube lock status, public-media mode...) plus account and
  composer checks. Post-level issues (no networks, missing/past date, missing media) are under key
  :data:`GENERAL` (``0``), present only when there is something to report.
- :func:`duplicate_post`, :func:`delete_post` (trash: pending targets are cancelled and remembered),
  :func:`restore_post`, :func:`reschedule_post` (only ``draft``/``scheduled`` targets, future only).
- :func:`target_text`, :func:`target_media` — the effective text/media of a target ("Común" + overrides).

Editing is refused while any target is :data:`LOCKED_STATUSES` (being published, published, sent to the
TikTok app or with an unknown outcome): those posts are shown read-only and can be duplicated instead. Edits,
trash and reschedule re-check under the write lock that no target changed since it was read (the scheduler
may claim one at any moment) and raise :class:`PostError` (``conflict``) otherwise.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from planificador import config, rules
from planificador.models import (
    NETWORK_LABELS,
    Account,
    AccountStatus,
    Brand,
    CommitPhase,
    EventLevel,
    IdeaStage,
    MediaAsset,
    Network,
    Notification,
    Post,
    PostEvent,
    PostKind,
    PostSource,
    PostTarget,
    TargetStatus,
)
from planificador.providers.base import Issue, MediaInfo, TargetSpec
from planificador.timeutil import Clock, ensure_utc, format_dt_es

log = logging.getLogger(__name__)

ACTIONS: tuple[str, ...] = ("schedule", "draft", "idea", "publish_now")
#: Key of post-level issues in :func:`validate_post` results.
GENERAL = 0

#: A post with any target in these states cannot be edited (only viewed / duplicated).
LOCKED_STATUSES = frozenset(
    {
        TargetStatus.PUBLISHING,
        TargetStatus.PUBLISHED,
        TargetStatus.MANUAL_DONE,
        TargetStatus.SENT_TO_APP,
        TargetStatus.UNKNOWN,
    }
)
#: Targets cancelled when their post goes to the trash (their previous status is remembered for restore).
TRASH_CANCEL_FROM = frozenset(
    {
        TargetStatus.DRAFT,
        TargetStatus.SCHEDULED,
        TargetStatus.MANUAL_PENDING,
        TargetStatus.MISSED,
        TargetStatus.ERROR,
    }
)
#: Only posts whose (non-cancelled) targets are in these states can be rescheduled.
MOVABLE_STATUSES = frozenset({TargetStatus.DRAFT, TargetStatus.SCHEDULED})

#: provider_state key holding the status a target had before its post was trashed.
TRASH_STATE_KEY = "_trash_prev_status"
#: :data:`TRASH_STATE_KEY` value of a target the trash withdrew from Facebook's/YouTube's own scheduler.
TRASH_NATIVE = "native_scheduled"
#: Targets whose notifications (assisted reminder, error, missed) become moot once the user edits them.
NOTIFIED_STATUSES = frozenset(
    {TargetStatus.MANUAL_PENDING, TargetStatus.ERROR, TargetStatus.MISSED, TargetStatus.UNKNOWN}
)
_MOOT_NOTIFICATION_KINDS = ("manual_publish", "error", "unknown", "missed")
CONFLICT_MESSAGE = (
    "La publicación ha cambiado de estado mientras la modificabas (puede que se esté publicando ahora "
    "mismo). Recarga la página y vuelve a intentarlo."
)
#: Option keys that are not copied by :func:`duplicate_post` (consents must be given again).
NON_COPIED_OPTIONS = frozenset({"consent"})
#: Issue codes about the date: they do not apply to "Publicar ahora".
DATE_CODES = frozenset({"date_required", "date_past"})

_WS_RE = re.compile(r"\r\n?")


# ============================================================================================ types


class PostError(Exception):
    """Invalid operation on a post. ``message`` is Spanish and user-facing.

    ``status_code`` suggests the HTTP status (404 missing, 409 conflict/state, 422 invalid input) and
    ``issues`` carries the validation result when the error comes from :func:`validate_post`.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid",
        status_code: int = 409,
        issues: dict[int, list[Issue]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.issues = issues

    def __repr__(self) -> str:
        return f"PostError(code={self.code!r}, message={self.message!r})"


@dataclass
class TargetInput:
    account_id: int
    format: str
    text_override: str | None = None
    media_override: list[int] | None = None
    options: dict = field(default_factory=dict)
    auto_publish: bool = True


@dataclass
class PostInput:
    brand_id: int
    text: str
    media_ids: list[int]
    scheduled_at: datetime | None  # aware UTC (naive = UTC) or None
    kind: str = "scheduled"
    idea_stage: str | None = None
    notes: str | None = None
    targets: list[TargetInput] = field(default_factory=list)
    source: str = PostSource.COMPOSER.value  # extension: "composer" | "csv" | "duplicate"


# ========================================================================================== helpers


def _now(clock: Clock | None) -> datetime:
    if clock is None:
        from planificador.timeutil import SystemClock

        clock = SystemClock()
    return ensure_utc(clock.now())


def _normalize_text(text: str | None) -> str:
    return _WS_RE.sub("\n", text or "")


def _clean_notes(notes: str | None) -> str | None:
    value = _normalize_text(notes).strip()
    return value or None


def _check_action(action: str) -> str:
    value = str(action or "").strip().lower()
    if value not in ACTIONS:
        raise PostError("Acción no válida.", code="invalid_action", status_code=422)
    return value


def _utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise PostError("La fecha de publicación no es válida.", code="invalid_date", status_code=422)
    return ensure_utc(value).replace(microsecond=0)


def _idea_stage(value: str | None) -> IdeaStage:
    try:
        return IdeaStage(str(value or "idea").strip().lower())
    except ValueError:
        return IdeaStage.IDEA


def _int_list(values: Iterable[Any] | None) -> list[int]:
    out: list[int] = []
    for v in values or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise PostError("Lista de archivos no válida.", code="invalid_media", status_code=422) from None
        if n not in out:
            out.append(n)
    return out


def _split_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, list | tuple) else re.split(r"[,\n]+", str(value))
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


_LIST_OPTIONS = frozenset({"collaborators", "tags"})


def normalize_options(network: str, fmt: str, options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Defaults for ``network``/``fmt`` updated with ``options``: private (``_``-prefixed) keys dropped,
    ``demo_*`` keys only for the demo network, list options as clean lists, strings stripped."""
    out = rules.default_options(network, fmt)
    for key, value in (options or {}).items():
        if not isinstance(key, str) or not key or key.startswith("_"):
            continue
        if key.startswith("demo_") and network != Network.DEMO:
            continue
        if key in _LIST_OPTIONS:
            items = _split_list(value)
            if key == "collaborators":
                items = [i.lstrip("@").strip() for i in items if i.lstrip("@").strip()]
            if key == "tags":
                items = [i.lstrip("#").strip() for i in items if i.lstrip("#").strip()]
            out[key] = items
            continue
        if isinstance(value, str):
            value = _normalize_text(value).strip()
            if key == "privacy_level" and not value:
                value = None
        out[key] = copy.deepcopy(value)
    return out


def _event(target: PostTarget, now: datetime, event: str, message: str, data: dict | None = None) -> None:
    target.events.append(
        PostEvent(ts=now, level=EventLevel.INFO, event=event[:64], message=message, data=data or None)
    )


def _wake_scheduler() -> None:
    try:
        from planificador import scheduler

        scheduler.wake()
    except Exception:  # pragma: no cover - scheduler not running / not available
        log.debug("Scheduler wake failed", exc_info=True)


def _reset_for_schedule(target: PostTarget) -> None:
    """Put ``target`` in ``scheduled`` (engine helper when available: it keeps a pending reconcile when a
    commit may already have gone out)."""
    try:
        from planificador import engine

        reset = engine.reset_for_schedule
    except (ImportError, AttributeError):  # pragma: no cover - engine not available
        reset = None
    if reset is not None:
        try:
            reset(target)
        except Exception as exc:  # engine.EngineError (busy)
            raise PostError(getattr(exc, "message", None) or str(exc), code="busy") from exc
        return
    target.status = TargetStatus.SCHEDULED  # pragma: no cover
    _clear_runtime(target)  # pragma: no cover


def _clear_runtime(target: PostTarget) -> None:
    """Forget attempts, errors and partial provider state — unless a commit may already have been sent (then
    the engine must reconcile before ever sending again, so the state is kept)."""
    target.attempts = 0
    target.next_attempt_at = None
    target.claimed_at = None
    target.last_error = None
    target.last_error_detail = None
    if target.commit_phase != CommitPhase.COMMIT_SENT:
        target.provider_state = {}
        target.commit_phase = CommitPhase.NONE


def _set_draft(target: PostTarget) -> None:
    target.status = TargetStatus.DRAFT
    _clear_runtime(target)


def _ensure_unchanged(db: Session, targets: Iterable[PostTarget]) -> None:
    """Guard against the scheduler before changing targets read in autocommit SELECTs (the checks above them
    used that snapshot): a guarded no-op ``UPDATE … WHERE status/commit_phase/next_attempt_at = <as read>``
    per target, which also takes SQLite's write lock, so no claim can interleave until this transaction
    ends. Call it **before** modifying any target. Raises :class:`PostError` (``conflict``)."""
    for target in targets:
        if target.id is None:
            continue
        seen_next = target.next_attempt_at
        stmt = (
            update(PostTarget)
            .where(
                PostTarget.id == target.id,
                PostTarget.status == target.status,
                PostTarget.commit_phase == target.commit_phase,
                PostTarget.next_attempt_at.is_(None)
                if seen_next is None
                else PostTarget.next_attempt_at == seen_next,
            )
            .values(updated_at=PostTarget.updated_at)
            .execution_options(synchronize_session=False)
        )
        if db.execute(stmt).rowcount != 1:
            raise PostError(CONFLICT_MESSAGE, code="conflict", status_code=409)


def _resolve_notifications(db: Session, target_ids: Iterable[int | None], now: datetime) -> None:
    """Mark the unread reminders/errors of these targets as read (the user has acted on them)."""
    ids = [i for i in target_ids if i is not None]
    if not ids:
        return
    db.execute(
        update(Notification)
        .where(
            Notification.target_id.in_(ids),
            Notification.read_at.is_(None),
            Notification.kind.in_(_MOOT_NOTIFICATION_KINDS),
        )
        .values(read_at=now)
        .execution_options(synchronize_session=False)
    )


def _forget_native(target: PostTarget) -> None:
    """Drop everything about a hand-off to the network's scheduler that was withdrawn (the network post no
    longer exists), so the target can be scheduled again from scratch."""
    target.provider_state = {}
    target.commit_phase = CommitPhase.NONE
    target.external_id = None
    target.external_url = None
    target.published_at = None


def _rollback(db: Session) -> None:
    try:
        db.rollback()
    except Exception:  # pragma: no cover
        log.debug("Rollback failed", exc_info=True)


def _finish(db: Session, commit: bool) -> None:
    if commit:
        db.commit()
    else:
        db.flush()


def is_demo_mode(db: Session) -> bool:
    try:
        return config.is_demo_mode(db)
    except Exception:  # pragma: no cover - settings table unavailable
        return False


# ============================================================================================ lookup


def get_post(
    db: Session, post_id: int, *, brand_id: int | None = None, include_deleted: bool = False
) -> Post:
    post = db.get(Post, int(post_id))
    if post is None or (brand_id is not None and post.brand_id != brand_id):
        raise PostError("La publicación no existe.", code="not_found", status_code=404)
    if post.deleted_at is not None and not include_deleted:
        raise PostError(
            "La publicación está en la papelera; restáurala antes de cambiarla.",
            code="deleted",
            status_code=409,
        )
    return post


def edit_lock_reason(post: Post) -> str | None:
    """Why ``post`` cannot be edited (Spanish), or ``None`` when it can."""
    if post.deleted_at is not None:
        return "La publicación está en la papelera. Restáurala para editarla."
    statuses = {t.status for t in post.targets}
    if TargetStatus.PUBLISHING in statuses:
        return "La publicación se está enviando en este momento y no se puede editar."
    if statuses & {TargetStatus.PUBLISHED, TargetStatus.MANUAL_DONE}:
        return (
            "Esta publicación ya se ha publicado en alguna red y no se puede editar. "
            "Duplícala si quieres volver a publicarla."
        )
    if TargetStatus.SENT_TO_APP in statuses:
        return "El vídeo ya se ha enviado a la app de TikTok; termina la publicación allí."
    if TargetStatus.UNKNOWN in statuses:
        return (
            "No se ha podido confirmar si la publicación salió en alguna red. "
            "Revísalo en la ficha de la publicación antes de editarla."
        )
    return None


def is_editable(post: Post) -> bool:
    return edit_lock_reason(post) is None


def _load_brand(db: Session, brand_id: int) -> Brand:
    brand = db.get(Brand, int(brand_id)) if brand_id is not None else None
    if brand is None or brand.archived_at is not None:
        raise PostError("La marca no existe o está archivada.", code="brand_missing", status_code=404)
    return brand


def _load_accounts(db: Session, data: PostInput) -> dict[int, Account]:
    accounts: dict[int, Account] = {}
    for t in data.targets:
        try:
            account_id = int(t.account_id)
        except (TypeError, ValueError):
            raise PostError("Cuenta no válida.", code="account_invalid", status_code=422) from None
        if account_id in accounts:
            raise PostError(
                "Has elegido la misma cuenta dos veces.", code="account_duplicate", status_code=422
            )
        account = db.get(Account, account_id)
        if account is None or account.brand_id != data.brand_id:
            raise PostError(
                "Una de las cuentas elegidas ya no existe en esta marca.",
                code="account_missing",
                status_code=422,
            )
        fmt = str(t.format or "")
        if rules.format_spec(str(account.network), fmt) is None:
            raise PostError(
                f"Formato no válido para {rules.network_label(str(account.network))}.",
                code="format_invalid",
                status_code=422,
            )
        accounts[account_id] = account
    return accounts


def _visible_asset(db: Session, asset_id: int, brand_id: int) -> MediaAsset | None:
    asset = db.get(MediaAsset, int(asset_id))
    if asset is None or asset.deleted_at is not None:
        return None
    if asset.brand_id not in (None, brand_id):
        return None
    return asset


def _load_media(db: Session, data: PostInput) -> dict[int, MediaAsset]:
    wanted = list(_int_list(data.media_ids))
    for t in data.targets:
        if t.media_override is not None:
            wanted.extend(i for i in _int_list(t.media_override) if i not in wanted)
        thumb = (t.options or {}).get("thumbnail_media_id")
        if thumb not in (None, ""):
            try:
                tid = int(thumb)
            except (TypeError, ValueError):
                raise PostError("Miniatura no válida.", code="invalid_media", status_code=422) from None
            if tid not in wanted:
                wanted.append(tid)
    assets: dict[int, MediaAsset] = {}
    for asset_id in wanted:
        asset = _visible_asset(db, asset_id, data.brand_id)
        if asset is None:
            raise PostError(
                "Alguno de los archivos ya no está en la biblioteca. Quítalo y vuelve a intentarlo.",
                code="media_missing",
                status_code=422,
            )
        assets[asset_id] = asset
    return assets


def ensure_demo_account(db: Session, brand_id: int, *, commit: bool = True) -> Account:
    """The brand's Demo account (created when missing; only meaningful with demo mode on)."""
    account = db.scalars(
        select(Account).where(Account.brand_id == brand_id, Account.network == Network.DEMO)
    ).first()
    if account is None:
        account = Account(
            brand_id=brand_id,
            network=Network.DEMO,
            external_id=f"demo-{brand_id}",
            name="Cuenta Demo",
            username="demo",
            account_type="demo",
            status=AccountStatus.CONNECTED,
            meta={"created_by": "composer"},
        )
        db.add(account)
    elif account.status != AccountStatus.CONNECTED:
        account.status = AccountStatus.CONNECTED
    _finish(db, commit)
    return account


# ======================================================================================= effective


def target_text(target: PostTarget) -> str:
    """Text published on ``target``: its override, else the post's "Común" text."""
    if target.text_override is not None:
        return target.text_override
    return (target.post.text if target.post is not None else "") or ""


def target_media(db: Session, target: PostTarget) -> list[MediaAsset]:
    """Media published on ``target`` in order (override, else the post's media). Missing or deleted
    library items are skipped."""
    out: list[MediaAsset] = []
    for asset_id in target.effective_media_ids:
        asset = db.get(MediaAsset, int(asset_id))
        if asset is not None and asset.deleted_at is None:
            out.append(asset)
    return out


def _media_infos(assets: Iterable[MediaAsset]) -> list[MediaInfo]:
    from planificador import media

    infos: list[MediaInfo] = []
    for asset in assets:
        try:
            infos.append(media.to_media_info(asset))
        except Exception:  # path outside the media dir (corrupt row): validated as missing
            log.warning("Media %s has an invalid path", asset.id)
    return infos


# ====================================================================================== validation


def _account_issues(db: Session, account: Account, t: TargetInput, action: str | None) -> list[Issue]:
    issues: list[Issue] = []
    label = rules.network_label(str(account.network))
    if account.status == AccountStatus.DISCONNECTED:
        issues.append(
            Issue(
                "error",
                "account_disconnected",
                f"La cuenta de {label} está desconectada. Vuelve a conectarla en «Cuentas conectadas».",
                "account",
            )
        )
    elif account.status == AccountStatus.NEEDS_RECONNECT:
        level = "error" if action == "publish_now" else "warning"
        issues.append(
            Issue(
                level,
                "account_needs_reconnect",
                f"La cuenta de {label} necesita reconexión: reconéctala en «Cuentas conectadas» antes de la "
                "hora de publicación.",
                "account",
            )
        )
    if account.network == Network.DEMO and not is_demo_mode(db):
        issues.append(
            Issue(
                "error",
                "demo_mode_off",
                "La red Demo solo funciona con el modo prueba activado (Ajustes).",
                "account",
            )
        )
    if account.network == Network.YOUTUBE and t.auto_publish and not youtube_unlocked(account):
        issues.append(
            Issue(
                "warning",
                "youtube_api_locked",
                "Tu proyecto de Google no está verificado por YouTube: los vídeos subidos automáticamente "
                "pueden quedar privados. Usa la Publicación asistida o haz la «Prueba de bloqueo» en "
                "Cuentas conectadas.",
                "auto_publish",
            )
        )
    return issues


def _composer_issues(network: str, fmt: str, options: Mapping[str, Any]) -> list[Issue]:
    """Checks of composer-only controls that :mod:`rules` does not see."""
    issues: list[Issue] = []
    if network == Network.TIKTOK and str(options.get("mode") or "direct") != "inbox":
        if options.get("commercial_content") and not (
            options.get("brand_organic_toggle") or options.get("brand_content_toggle")
        ):
            issues.append(
                Issue(
                    "error",
                    "tiktok_commercial_option",
                    "Has activado «Contenido comercial»: indica si es «Tu marca», «Contenido de marca» o "
                    "ambas.",
                    "commercial_content",
                )
            )
    return issues


def youtube_unlocked(account: Account) -> bool:
    """Automatic (API) publishing is safe for this YouTube account (lock test passed / audited project)."""
    try:
        from planificador.providers import youtube

        return bool(youtube.automatic_publishing_allowed(account.meta or {}))
    except Exception:  # pragma: no cover - provider not available
        return (account.meta or {}).get("youtube_lock_status") == "unlocked"


def default_auto_publish(account: Account) -> bool:
    """Default "Autopublicar" for a new target of ``account`` (YouTube: assisted until unlocked)."""
    if account.network == Network.YOUTUBE:
        try:
            from planificador.providers import youtube

            return bool(youtube.default_auto_publish(account.meta or {}))
        except Exception:  # pragma: no cover
            return youtube_unlocked(account)
    return True


def _spec_for(
    account: Account,
    t: TargetInput,
    data: PostInput,
    assets: Mapping[int, MediaAsset],
    when: datetime | None,
) -> TargetSpec:
    network = str(account.network)
    fmt = str(t.format)
    text = _normalize_text(t.text_override if t.text_override is not None else data.text)
    ids = _int_list(t.media_override) if t.media_override is not None else _int_list(data.media_ids)
    media = _media_infos(assets[i] for i in ids if i in assets)
    return TargetSpec(
        network=network,
        format=fmt,
        text=text,
        media=media,
        options=normalize_options(network, fmt, t.options),
        scheduled_at=when,
        auto_publish=bool(t.auto_publish),
    )


def validate_post(
    db: Session,
    data: PostInput,
    *,
    clock: Clock | None = None,
    action: str | None = None,
) -> dict[int, list[Issue]]:
    """Issues per target account id (errors first) plus post-level ones under :data:`GENERAL`.

    ``action`` refines the checks: ``publish_now`` validates as of now (no date checks) and treats an account
    that needs reconnecting as an error; ``idea`` (or ``data.kind == "idea"``) skips the date checks.
    Missing accounts/media are reported as issues here (the mutating functions raise instead).
    """
    now = _now(clock)
    result: dict[int, list[Issue]] = {}
    general: list[Issue] = []
    is_idea = action == "idea" or (action is None and str(data.kind) == PostKind.IDEA)
    publishing_now = action == "publish_now"

    if not data.targets and not is_idea:
        general.append(Issue("error", "no_targets", "Selecciona al menos una red social.", "networks"))
    when = _utc_or_none(data.scheduled_at)
    if not is_idea and not publishing_now:
        if when is None:
            general.append(
                Issue("error", "date_required", "Elige la fecha y la hora de publicación.", "scheduled_at")
            )
        elif when <= now:
            general.append(
                Issue("error", "date_past", "La fecha de publicación está en el pasado.", "scheduled_at")
            )
    spec_when = now if publishing_now else when

    assets: dict[int, MediaAsset] = {}
    missing = False
    for asset_id in _int_list(data.media_ids) + [
        i for t in data.targets if t.media_override is not None for i in _int_list(t.media_override)
    ]:
        if asset_id in assets:
            continue
        asset = _visible_asset(db, asset_id, data.brand_id)
        if asset is None:
            missing = True
        else:
            assets[asset_id] = asset
    if missing:
        general.append(
            Issue(
                "error",
                "media_missing",
                "Alguno de los archivos ya no está en la biblioteca. Quítalo de la publicación.",
                "media",
            )
        )

    for t in data.targets:
        try:
            account_id = int(t.account_id)
        except (TypeError, ValueError):
            general.append(Issue("error", "account_invalid", "Cuenta no válida.", "networks"))
            continue
        issues: list[Issue] = []
        account = db.get(Account, account_id)
        if account is None or account.brand_id != data.brand_id:
            issues.append(
                Issue(
                    "error",
                    "account_missing",
                    "Esta cuenta ya no existe en la marca. Quítala de la publicación.",
                    "account",
                )
            )
            result[account_id] = issues
            continue
        network = str(account.network)
        if rules.format_spec(network, str(t.format or "")) is None:
            valid = ", ".join(f.label for f in rules.enabled_formats(network))
            issues.append(
                Issue(
                    "error",
                    "format_unknown",
                    f"Formato no válido para {rules.network_label(network)}. Elige uno de: {valid}.",
                    "format",
                )
            )
            result[account_id] = issues
            continue
        issues.extend(_account_issues(db, account, t, action))
        spec = _spec_for(account, t, data, assets, spec_when)
        try:
            issues.extend(rules.validate_for_account(db, account, spec, clock))
        except Exception:  # pragma: no cover - a rule bug must not break saving
            log.exception("Validation failed for %s/%s", network, t.format)
        if account.network == Network.DEMO:
            try:
                from planificador.providers.registry import get_provider

                issues.extend(get_provider("demo").validate(spec))
            except Exception:  # pragma: no cover
                log.debug("Demo provider validation unavailable", exc_info=True)
        issues.extend(_composer_issues(network, str(t.format), spec.options))
        result[account_id] = _sorted(issues)
    if general:
        result[GENERAL] = _sorted(general)
    return result


def _sorted(issues: Iterable[Issue]) -> list[Issue]:
    seen: set[tuple[str, str]] = set()
    unique: list[Issue] = []
    for issue in issues:
        key = (issue.code, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return [i for i in unique if i.level == "error"] + [i for i in unique if i.level != "error"]


def errors_of(result: Mapping[int, list[Issue]], *, ignore_codes: Iterable[str] = ()) -> list[Issue]:
    skip = set(ignore_codes)
    return [i for issues in result.values() for i in issues if i.level == "error" and i.code not in skip]


def has_blocking_errors(result: Mapping[int, list[Issue]], action: str = "schedule") -> bool:
    """Whether ``result`` blocks ``action`` (drafts and ideas are never blocked)."""
    if action in ("draft", "idea"):
        return False
    ignore = DATE_CODES if action == "publish_now" else ()
    return bool(errors_of(result, ignore_codes=ignore))


def _raise_if_blocked(result: dict[int, list[Issue]], action: str) -> None:
    if not has_blocking_errors(result, action):
        return
    ignore = DATE_CODES if action == "publish_now" else ()
    errors = errors_of(result, ignore_codes=ignore)
    verb = "publicar" if action == "publish_now" else "programar"
    if len(errors) == 1:
        message = errors[0].message
    else:
        message = f"Hay {len(errors)} errores que impiden {verb} la publicación."
    raise PostError(message, code="validation", status_code=422, issues=result)


# ===================================================================================== create/update


def _apply_target(
    target: PostTarget,
    account: Account,
    t: TargetInput,
    data: PostInput,
) -> None:
    network = str(account.network)
    fmt = str(t.format)
    text = _normalize_text(data.text)
    override = t.text_override
    if override is not None:
        override = _normalize_text(override)
        if override == text:
            override = None
    media_override = _int_list(t.media_override) if t.media_override is not None else None
    if media_override is not None and media_override == _int_list(data.media_ids):
        media_override = None
    target.account_id = account.id
    target.network = account.network
    target.format = fmt
    target.text_override = override
    target.media_override = media_override
    target.options = normalize_options(network, fmt, t.options)
    target.auto_publish = bool(t.auto_publish)


def _apply_status(post: Post, target: PostTarget, action: str) -> None:
    target.scheduled_at = post.scheduled_at
    if action == "schedule":
        _reset_for_schedule(target)
    else:  # draft / idea / publish_now (engine.publish_now schedules it after the commit)
        _set_draft(target)


def _post_fields(post: Post, data: PostInput, action: str) -> None:
    post.text = _normalize_text(data.text)
    post.notes = _clean_notes(data.notes)
    post.scheduled_at = _utc_or_none(data.scheduled_at)
    post.set_media(_int_list(data.media_ids))
    if action == "idea":
        post.kind = PostKind.IDEA
        post.idea_stage = _idea_stage(data.idea_stage)
    else:
        post.kind = PostKind.SCHEDULED
        post.idea_stage = None


def _status_message(post: Post, action: str, tz: str | None) -> str:
    if action == "schedule":
        return f"Programada para el {format_dt_es(post.scheduled_at, tz)}."
    if action == "idea":
        return "Guardada como idea."
    if action == "publish_now":
        return "Guardada para publicar ahora."
    return "Guardada como borrador."


def _after_commit(db: Session, post: Post, action: str, clock: Clock) -> None:
    if action == "schedule":
        _wake_scheduler()
    elif action == "publish_now":
        _publish_now(db, post, clock)


def _publish_now(db: Session, post: Post, clock: Clock) -> None:
    try:
        from planificador import engine

        publish = engine.publish_now
    except (ImportError, AttributeError):  # pragma: no cover - engine not available yet
        publish = None
    if publish is None:  # pragma: no cover
        now = _now(clock)
        post.scheduled_at = now
        for target in post.targets:
            if target.status == TargetStatus.DRAFT:
                target.scheduled_at = now
                target.status = TargetStatus.SCHEDULED
        db.commit()
        _wake_scheduler()
        return
    try:
        publish(db, post.id, clock)
    except Exception as exc:
        _rollback(db)
        message = getattr(exc, "message", None) or str(exc) or "No se ha podido publicar."
        raise PostError(
            f"{message} La publicación se ha guardado como borrador.",
            code=getattr(exc, "code", "publish_now_failed"),
        ) from exc


def create_post(db: Session, data: PostInput, action: str, clock: Clock, *, commit: bool = True) -> Post:
    """Create a post with its targets. See the module docstring for ``action``. ``publish_now`` always
    commits (the engine takes over)."""
    action = _check_action(action)
    try:
        brand = _load_brand(db, data.brand_id)
        accounts = _load_accounts(db, data)
        _load_media(db, data)
        if action in ("schedule", "publish_now"):
            _raise_if_blocked(validate_post(db, data, clock=clock, action=action), action)
        now = _now(clock)
        source = str(data.source or PostSource.COMPOSER.value)
        if source not in {s.value for s in PostSource}:
            source = PostSource.COMPOSER.value
        post = Post(brand_id=brand.id, source=source, created_at=now, updated_at=now)
        _post_fields(post, data, action)
        db.add(post)
        message = _status_message(post, action, brand.timezone)
        for t in data.targets:
            account = accounts[int(t.account_id)]
            target = PostTarget(created_at=now, updated_at=now)
            post.targets.append(target)
            _apply_target(target, account, t, data)
            _apply_status(post, target, action)
            _event(target, now, "created", f"Publicación creada. {message}")
        _finish(db, commit or action == "publish_now")
    except PostError:
        _rollback(db)
        raise
    except Exception:
        _rollback(db)
        raise
    _after_commit(db, post, action, clock)
    return post


def update_post(
    db: Session, post_id: int, data: PostInput, action: str, clock: Clock, *, commit: bool = True
) -> Post:
    """Replace the content, date and targets of an editable post (targets matched by account: kept ones are
    updated in place, removed ones deleted, new ones created)."""
    action = _check_action(action)
    try:
        post = get_post(db, post_id)
        if data.brand_id != post.brand_id:
            raise PostError("La publicación pertenece a otra marca.", code="brand_mismatch", status_code=409)
        reason = edit_lock_reason(post)
        if reason:
            raise PostError(reason, code="locked", status_code=409)
        brand = _load_brand(db, post.brand_id)
        accounts = _load_accounts(db, data)
        _load_media(db, data)
        if action in ("schedule", "publish_now"):
            _raise_if_blocked(validate_post(db, data, clock=clock, action=action), action)
        now = _now(clock)
        _ensure_unchanged(db, post.targets)
        _resolve_notifications(db, [t.id for t in post.targets if t.status in NOTIFIED_STATUSES], now)
        _post_fields(post, data, action)
        post.updated_at = now
        message = _status_message(post, action, brand.timezone)
        existing = {t.account_id: t for t in post.targets}
        wanted = {int(t.account_id) for t in data.targets}
        for account_id, target in existing.items():
            if account_id not in wanted:
                post.targets.remove(target)
                db.delete(target)
        for t in data.targets:
            account = accounts[int(t.account_id)]
            target = existing.get(account.id)
            if target is None:
                target = PostTarget(created_at=now)
                post.targets.append(target)
                _event_name = "created"
            else:
                _event_name = "updated"
            target.updated_at = now
            _apply_target(target, account, t, data)
            _apply_status(post, target, action)
            verb = "Publicación creada." if _event_name == "created" else "Publicación editada."
            _event(target, now, _event_name, f"{verb} {message}")
        _finish(db, commit or action == "publish_now")
    except PostError:
        _rollback(db)
        raise
    except Exception:
        _rollback(db)
        raise
    _after_commit(db, post, action, clock)
    return post


# ======================================================================================= other ops


def duplicate_post(db: Session, post_id: int, clock: Clock, *, commit: bool = True) -> Post:
    """Copy a post (text, media, notes, networks with formats/overrides/options) as a **draft**. The date
    is kept only when it is still in the future; consents (TikTok) must be given again."""
    try:
        src = get_post(db, post_id, include_deleted=True)
        now = _now(clock)
        copy_post = Post(
            brand_id=src.brand_id,
            kind=src.kind,
            idea_stage=src.idea_stage,
            text=src.text or "",
            notes=src.notes,
            scheduled_at=src.scheduled_at if src.scheduled_at and src.scheduled_at > now else None,
            source=PostSource.DUPLICATE.value,
            created_at=now,
            updated_at=now,
        )
        copy_post.set_media([link.media_id for link in src.media_links])
        db.add(copy_post)
        targets = [t for t in src.targets if t.status != TargetStatus.CANCELLED] or list(src.targets)
        for t in targets:
            options = {
                k: copy.deepcopy(v) for k, v in (t.options or {}).items() if k not in NON_COPIED_OPTIONS
            }
            target = PostTarget(
                account_id=t.account_id,
                network=t.network,
                format=t.format,
                text_override=t.text_override,
                media_override=list(t.media_override) if t.media_override is not None else None,
                options=options,
                auto_publish=t.auto_publish,
                status=TargetStatus.DRAFT,
                scheduled_at=copy_post.scheduled_at,
                created_at=now,
                updated_at=now,
            )
            copy_post.targets.append(target)
            _event(target, now, "duplicated", f"Copia creada a partir de la publicación #{src.id}.")
        _finish(db, commit)
    except Exception:
        _rollback(db)
        raise
    return copy_post


def _busy(target: PostTarget) -> bool:
    """The engine is working on ``target`` right now (not just waiting before a retry)."""
    return target.status == TargetStatus.PUBLISHING and (
        target.next_attempt_at is None or target.commit_phase == CommitPhase.COMMIT_SENT
    )


def _native_pending(target: PostTarget, now: datetime) -> bool:
    """Published target handed to the network's own scheduler that has not gone live yet."""
    if target.status != TargetStatus.PUBLISHED:
        return False
    state = target.provider_state or {}
    return bool(state.get("native_scheduled")) and (target.scheduled_at is None or target.scheduled_at > now)


def delete_post(db: Session, post_id: int, clock: Clock, *, commit: bool = True) -> None:
    """Move a post to the trash. Pending targets (draft, scheduled, waiting to retry, assisted reminders,
    missed, error) are cancelled and remember their status for :func:`restore_post`. Posts handed to the
    Facebook/YouTube scheduler are withdrawn there first; if that fails the post is still going to go live,
    so nothing is trashed and :class:`PostError` says why. Published history is kept."""
    try:
        post = get_post(db, post_id, include_deleted=True)
        if post.deleted_at is not None:
            return
        if any(_busy(t) for t in post.targets):
            raise PostError(
                "La publicación se está enviando en este momento; espera a que termine.", code="busy"
            )
        now = _now(clock)
        native = [t.id for t in post.targets if _native_pending(t, now)]
    except Exception:
        _rollback(db)
        raise
    withdrawn = _withdraw_native(db, native, clock)
    try:
        post = get_post(db, post_id, include_deleted=True)
        _ensure_unchanged(db, post.targets)
        cancelled: list[int] = []
        for target in post.targets:
            if target.id in withdrawn and target.status == TargetStatus.CANCELLED:
                state = dict(target.provider_state or {})
                state[TRASH_STATE_KEY] = TRASH_NATIVE
                target.provider_state = state
                target.updated_at = now
                _event(target, now, "trashed", "Publicación movida a la papelera.", {"from": TRASH_NATIVE})
            elif target.status in TRASH_CANCEL_FROM or target.status == TargetStatus.PUBLISHING:
                previous = str(target.status)
                state = dict(target.provider_state or {})
                state[TRASH_STATE_KEY] = previous
                target.provider_state = state
                target.status = TargetStatus.CANCELLED
                target.next_attempt_at = None
                target.claimed_at = None
                target.updated_at = now
                _event(target, now, "trashed", "Publicación movida a la papelera.", {"from": previous})
                if target.id is not None:
                    cancelled.append(target.id)
        post.deleted_at = now
        post.updated_at = now
        if cancelled:
            db.execute(
                update(Notification)
                .where(Notification.target_id.in_(cancelled), Notification.read_at.is_(None))
                .values(read_at=now)
                .execution_options(synchronize_session=False)
            )
        _finish(db, commit)
    except Exception:
        _rollback(db)
        raise


def _withdraw_native(db: Session, target_ids: list[int], clock: Clock) -> list[int]:
    """Withdraw natively scheduled targets from the network's scheduler (``engine.cancel_target``, which
    commits). Raises :class:`PostError` when one could not be withdrawn (it stays scheduled there)."""
    if not target_ids:
        return []
    from planificador import engine

    done: list[int] = []
    for target_id in target_ids:
        try:
            engine.cancel_target(db, target_id, clock)
        except Exception as exc:
            _rollback(db)
            message = getattr(exc, "message", None)
            if not message:
                raise
            if done:
                message += " Las demás redes programadas allí sí se han retirado."
            raise PostError(
                f"{message} La publicación no se ha movido a la papelera.",
                code=getattr(exc, "code", "native_cancel_failed"),
            ) from exc
        done.append(target_id)
    return done


def restore_post(db: Session, post_id: int, clock: Clock, *, commit: bool = True) -> None:
    """Take a post out of the trash. Targets cancelled by the trash get their status back; a ``scheduled``
    one whose time has passed meanwhile comes back as a ``draft`` (it is never published by surprise)."""
    try:
        post = get_post(db, post_id, include_deleted=True)
        if post.deleted_at is None:
            return
        now = _now(clock)
        for target in post.targets:
            state = dict(target.provider_state or {})
            previous = state.pop(TRASH_STATE_KEY, None)
            if previous is None:
                continue
            target.provider_state = state
            target.updated_at = now
            future = target.scheduled_at is not None and target.scheduled_at > now
            message = "Publicación restaurada de la papelera."
            if previous == TRASH_NATIVE:
                # It was withdrawn from the network's scheduler when trashed: schedule it again from scratch
                # (the scheduler hands it over once more) — or keep it as a draft if its time has passed.
                _forget_native(target)
                label = NETWORK_LABELS.get(str(target.network), str(target.network))
                if future and post.kind == PostKind.SCHEDULED:
                    _reset_for_schedule(target)
                    restored = TargetStatus.SCHEDULED
                    message += f" Se volverá a programar en {label}."
                else:
                    _set_draft(target)
                    restored = TargetStatus.DRAFT
                    message += " Su fecha ya ha pasado: queda como borrador."
            elif previous in (TargetStatus.SCHEDULED, TargetStatus.PUBLISHING):
                if future and post.kind == PostKind.SCHEDULED:
                    _reset_for_schedule(target)
                    restored = TargetStatus.SCHEDULED
                else:
                    _set_draft(target)
                    restored = TargetStatus.DRAFT
            elif previous in (TargetStatus.MANUAL_PENDING, TargetStatus.MISSED, TargetStatus.ERROR):
                target.status = TargetStatus(previous)
                restored = target.status
            else:
                _set_draft(target)
                restored = TargetStatus.DRAFT
            _event(target, now, "restored", message, {"status": str(restored)})
        post.deleted_at = None
        post.updated_at = now
        _finish(db, commit)
    except Exception:
        _rollback(db)
        raise
    _wake_scheduler()


def reschedule_post(
    db: Session, post_id: int, new_scheduled_at: datetime, clock: Clock, *, commit: bool = True
) -> Post:
    """Move a post to a new date (planner drag & drop). Only posts whose targets are ``draft``/``scheduled``
    (ignoring cancelled ones) and only to a future instant; statuses are kept."""
    try:
        post = get_post(db, post_id)
        if post.kind == PostKind.IDEA:
            raise PostError("Las ideas no tienen fecha: ábrela y prográmala desde el editor.", code="idea")
        active = [t for t in post.targets if t.status != TargetStatus.CANCELLED]
        if post.targets and not active:
            raise PostError("Esta publicación está cancelada.", code="cancelled")
        if any(t.status not in MOVABLE_STATUSES for t in active):
            raise PostError(
                "Solo se pueden mover publicaciones programadas o en borrador. "
                "Si quieres volver a publicarla, duplícala.",
                code="not_movable",
            )
        when = _utc_or_none(new_scheduled_at)
        now = _now(clock)
        if when is None or when <= now:
            raise PostError(
                "No se puede mover una publicación a una fecha pasada.", code="date_past", status_code=422
            )
        tz = post.brand.timezone if post.brand is not None else None
        _ensure_unchanged(db, active)
        post.scheduled_at = when
        post.updated_at = now
        for target in active:
            target.scheduled_at = when
            target.updated_at = now
            if target.status == TargetStatus.SCHEDULED:
                _reset_for_schedule(target)
            _event(target, now, "rescheduled", f"Reprogramada para el {format_dt_es(when, tz)}.")
        _finish(db, commit)
    except Exception:
        _rollback(db)
        raise
    _wake_scheduler()
    return post


# ============================================================================================ forms


def post_input_from_post(post: Post) -> PostInput:
    """The :class:`PostInput` that reproduces ``post`` (composer edit form)."""
    targets = [
        TargetInput(
            account_id=t.account_id,
            format=t.format,
            text_override=t.text_override,
            media_override=list(t.media_override) if t.media_override is not None else None,
            options=copy.deepcopy(dict(t.options or {})),
            auto_publish=bool(t.auto_publish),
        )
        for t in post.targets
        if t.status != TargetStatus.CANCELLED
    ]
    return PostInput(
        brand_id=post.brand_id,
        text=post.text or "",
        media_ids=[link.media_id for link in post.media_links],
        scheduled_at=post.scheduled_at,
        kind=str(post.kind),
        idea_stage=str(post.idea_stage) if post.idea_stage else None,
        notes=post.notes,
        targets=targets,
        source=post.source,
    )


def issue_dict(issue: Issue, account: Account | None = None) -> dict[str, Any]:
    out = {"level": issue.level, "code": issue.code, "message": issue.message, "field": issue.field}
    if account is not None:
        out["account_id"] = account.id
        out["network"] = str(account.network)
    return out


__all__ = [
    "ACTIONS",
    "GENERAL",
    "LOCKED_STATUSES",
    "PostError",
    "PostInput",
    "TargetInput",
    "create_post",
    "default_auto_publish",
    "delete_post",
    "duplicate_post",
    "edit_lock_reason",
    "ensure_demo_account",
    "errors_of",
    "get_post",
    "has_blocking_errors",
    "is_editable",
    "normalize_options",
    "post_input_from_post",
    "reschedule_post",
    "restore_post",
    "target_media",
    "target_text",
    "update_post",
    "validate_post",
    "youtube_unlocked",
]

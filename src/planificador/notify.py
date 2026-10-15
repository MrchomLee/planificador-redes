"""Notifications: DB rows + desktop notifications (SPEC §11).

STUB created by the foundation agent so imports resolve. TODO(notifications agent): implement.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from planificador.models import Notification


def notify(
    db: Session,
    kind: str,
    title: str,
    body: str,
    target_id: int | None = None,
    account_id: int | None = None,
) -> Notification:
    raise NotImplementedError("notify.notify")

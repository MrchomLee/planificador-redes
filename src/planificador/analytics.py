"""Analytics: daily snapshots, aggregations, best times (SPEC §12).

STUB created by the foundation agent so imports resolve. TODO(analytics agent): implement.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from sqlalchemy.orm import Session

from planificador.models import Account, Brand
from planificador.timeutil import Clock


def snapshot_all(db: Session, http: httpx.Client, clock: Clock) -> None:
    raise NotImplementedError("analytics.snapshot_all")


def kpis(account: Account, start: date, end: date) -> dict[str, Any]:
    raise NotImplementedError("analytics.kpis")


def series(account: Account, metric: str, start: date, end: date) -> list[tuple[date, float]]:
    raise NotImplementedError("analytics.series")


def follower_balance(account: Account, start: date, end: date) -> Any:
    raise NotImplementedError("analytics.follower_balance")


def top_posts(account: Account, start: date, end: date) -> list[Any]:
    raise NotImplementedError("analytics.top_posts")


def best_times(brand: Brand) -> list[list[float]]:
    raise NotImplementedError("analytics.best_times")

"""Background scheduler thread: due posts, retries, catch-up, periodic jobs (SPEC §10).

STUB created by the foundation agent so imports resolve. TODO(engine/scheduler agent): implement.

Contract with ``app.py``: the lifespan calls ``start(app.state)`` when the scheduler is enabled (not when
``PLANIFICADOR_NO_SCHEDULER=1``) and ``stop()`` at shutdown. ``app.state`` carries ``db`` (``db.Database``),
``settings`` (``config.AppSettings``), ``clock`` (``timeutil.Clock``), ``paths`` and ``shutdown_callbacks``.
``NotImplementedError`` is tolerated by the app.
"""

from __future__ import annotations

from typing import Any


def start(state: Any) -> None:
    raise NotImplementedError("scheduler.start")


def stop() -> None:
    raise NotImplementedError("scheduler.stop")


def wake() -> None:
    """Ask the scheduler to tick now (after "Publicar ahora" / save). No-op until implemented."""
    return None

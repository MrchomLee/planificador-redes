"""Keep the PC awake while a publish is running (SPEC §10).

On Windows this calls ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`` through ``ctypes``
and restores ``ES_CONTINUOUS`` when the work is done. It only prevents *idle* sleep: closing the lid or
pressing the power button still suspends the machine (research 04 §2.5). The flags belong to the calling
thread, so the context manager must be entered and exited on the same thread (the engine wraps each run
in one worker thread). Nested use on one thread is reference counted. Everywhere else it is a no-op.

Usage::

    with keep_awake():
        run_publish()
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_local = threading.local()


def _kernel32() -> Any | None:
    """``kernel32`` on Windows, else ``None`` (tests monkeypatch this)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return ctypes.windll.kernel32  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - only on broken Windows installs
        log.debug("kernel32 unavailable; keep-awake disabled", exc_info=True)
        return None


def _set_state(kernel32: Any, flags: int) -> bool:
    try:
        return bool(kernel32.SetThreadExecutionState(flags))
    except Exception:
        log.debug("SetThreadExecutionState(%#x) failed", flags, exc_info=True)
        return False


def is_supported() -> bool:
    return _kernel32() is not None


def depth() -> int:
    """How many nested ``keep_awake`` blocks are active on the current thread."""
    return int(getattr(_local, "depth", 0))


@contextmanager
def keep_awake() -> Iterator[bool]:
    """Prevent idle sleep for the duration of the block. Yields whether the request is active."""
    current = depth()
    if current > 0:  # already held by an outer block on this thread
        _local.depth = current + 1
        try:
            yield bool(getattr(_local, "active", False))
        finally:
            _local.depth = depth() - 1
        return

    kernel32 = _kernel32()
    active = _set_state(kernel32, ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if kernel32 is not None else False
    _local.depth = 1
    _local.active = active
    try:
        yield active
    finally:
        _local.depth = 0
        _local.active = False
        if active and kernel32 is not None:
            _set_state(kernel32, ES_CONTINUOUS)


class ThreadHold:
    """A long-lived keep-awake request owned by one thread (the scheduler loop holds it while any target is
    publishing, including the waits between steps). Call :meth:`update` and :meth:`release` from that thread
    only; repeated calls with the same value do nothing."""

    def __init__(self) -> None:
        self.active = False

    def update(self, wanted: bool) -> bool:
        kernel32 = _kernel32()
        if kernel32 is None:
            self.active = False
            return False
        if bool(wanted) == self.active:
            return self.active
        if wanted:
            self.active = _set_state(kernel32, ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        else:
            _set_state(kernel32, ES_CONTINUOUS)
            self.active = False
        return self.active

    def release(self) -> None:
        self.update(False)


__all__ = ["ES_CONTINUOUS", "ES_SYSTEM_REQUIRED", "ThreadHold", "depth", "is_supported", "keep_awake"]

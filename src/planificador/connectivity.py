"""Connectivity probe ("is there internet?") with a short cache (SPEC §10, offline-aware retries).

When a publish fails at the network level **before** its commit point (connection refused, DNS failure, no
route, connect/read timeout), the engine asks :meth:`Connectivity.check` whether the network's API host can be
reached at all:

- **unreachable** → the PC has no internet (or the network's API is down): the target waits for the connection
  (``engine`` keeps it ``publishing`` and re-checks about every minute) without spending its retry budget;
- **reachable** → a genuine API problem: the usual retry/backoff applies.

The probe is one ``HEAD`` request to a stable endpoint of the target network (any HTTP answer, even 4xx/5xx,
means "reachable"). Results are cached for :data:`CACHE_TTL` per host, so many waiting posts share one probe.
The demo network (and unknown ones) never touch the network: they always count as reachable.

Tests inject ``probe=`` (``probe(network, http) -> bool``) instead of doing real requests.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from planificador.timeutil import Clock, SystemClock

log = logging.getLogger(__name__)

#: Stable endpoints probed per network (any HTTP response counts as "reachable").
PROBE_URLS: dict[str, str] = {
    "facebook": "https://graph.facebook.com/",
    "instagram": "https://graph.facebook.com/",
    "tiktok": "https://open.tiktokapis.com/",
    "youtube": "https://www.googleapis.com/",
}
PROBE_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
CACHE_TTL = timedelta(seconds=30)

#: ``probe(network, http) -> bool`` (True = the network's API host can be reached).
Probe = Callable[[str, "httpx.Client | None"], bool]

_OFFLINE_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.NetworkError,
    httpx.TimeoutException,
    httpx.ProxyError,
    OSError,
)


def probe_url(network: str) -> str | None:
    return PROBE_URLS.get(str(network))


def default_probe(network: str, http: httpx.Client | None) -> bool:
    """One ``HEAD`` to the network's API host. Unknown networks (demo) are always reachable."""
    url = probe_url(network)
    if url is None:
        return True
    own = http is None
    client = http or httpx.Client(timeout=PROBE_TIMEOUT)
    try:
        client.head(url, timeout=PROBE_TIMEOUT, follow_redirects=False)
        return True
    except _OFFLINE_ERRORS as exc:
        log.debug("Connectivity probe to %s failed: %s", url, type(exc).__name__)
        return False
    except Exception:  # anything else means something answered: let the normal error path decide
        log.debug("Connectivity probe to %s raised an unexpected error", url, exc_info=True)
        return True
    finally:
        if own:
            client.close()


class Connectivity:
    """Cached connectivity checks (thread-safe). Results live :data:`CACHE_TTL` on ``clock`` time."""

    def __init__(self, clock: Clock | None = None, probe: Probe | None = None, ttl: timedelta = CACHE_TTL):
        self.clock: Clock = clock or SystemClock()
        self.probe: Probe = probe or default_probe
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[datetime, bool]] = {}
        #: Whether an outage is being tracked in ``settings`` (``None`` = unknown: look it up once).
        self.outage_active: bool | None = None

    @staticmethod
    def _key(network: str) -> str:
        return probe_url(network) or f"network:{network}"

    def check(self, network: str, http: httpx.Client | None = None, *, fresh: bool = False) -> bool:
        """Whether ``network``'s API host is reachable (cached unless ``fresh``)."""
        key = self._key(network)
        now = self.clock.now()
        if not fresh:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None and timedelta(0) <= now - cached[0] < self.ttl:
                return cached[1]
        try:
            online = bool(self.probe(str(network), http))
        except Exception:  # a broken probe must never block publishing
            log.debug("Connectivity probe crashed; assuming online", exc_info=True)
            online = True
        with self._lock:
            self._cache[key] = (self.clock.now(), online)
        return online

    def known_offline(self, network: str) -> bool:
        """A recent probe of ``network``'s host failed (no new probe is made)."""
        with self._lock:
            cached = self._cache.get(self._key(network))
        if cached is None:
            return False
        age = self.clock.now() - cached[0]
        return not cached[1] and timedelta(0) <= age < self.ttl

    def forget(self) -> None:
        """Drop cached results (after a sleep/wake gap the network may have changed)."""
        with self._lock:
            self._cache.clear()
        self.outage_active = None


__all__ = ["CACHE_TTL", "PROBE_URLS", "Connectivity", "Probe", "default_probe", "probe_url"]

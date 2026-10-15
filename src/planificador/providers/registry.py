"""Provider registry: ``get_provider(network)`` / ``all_providers()`` (SPEC §5).

Each provider module defines a class (``FacebookProvider``, ``InstagramProvider``, ``TikTokProvider``,
``YouTubeProvider``, ``DemoProvider``) instantiable without arguments. Instances are created lazily and
cached. The demo provider is always registered; the UI only offers it when ``demo_mode`` is on
(:func:`available_networks`). Tests can install fakes with :func:`register` and restore with :func:`reset`.
"""

from __future__ import annotations

import importlib
import logging
import threading

from planificador.providers.base import Provider

log = logging.getLogger(__name__)

NETWORK_ORDER: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube", "demo")
REAL_NETWORKS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "youtube")

_FACTORIES: dict[str, tuple[str, str]] = {
    "facebook": ("planificador.providers.facebook", "FacebookProvider"),
    "instagram": ("planificador.providers.instagram", "InstagramProvider"),
    "tiktok": ("planificador.providers.tiktok", "TikTokProvider"),
    "youtube": ("planificador.providers.youtube", "YouTubeProvider"),
    "demo": ("planificador.providers.demo", "DemoProvider"),
}

_lock = threading.RLock()
_instances: dict[str, Provider] = {}


def _load(network: str) -> Provider:
    try:
        module_name, class_name = _FACTORIES[network]
    except KeyError:
        raise KeyError(f"Unknown network: {network!r}") from None
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    provider = cls()
    if getattr(provider, "network", None) != network:
        raise RuntimeError(f"{class_name}.network must be {network!r}")
    return provider


def get_provider(network: str) -> Provider:
    """Provider instance for ``network`` (facebook/instagram/tiktok/demo). KeyError if unknown."""
    key = str(network)
    with _lock:
        provider = _instances.get(key)
        if provider is None:
            provider = _load(key)
            _instances[key] = provider
        return provider


def all_providers() -> list[Provider]:
    """Every provider that can be loaded, in display order (FB, IG, TikTok, YouTube, Demo)."""
    out: list[Provider] = []
    for network in NETWORK_ORDER:
        try:
            out.append(get_provider(network))
        except Exception:
            log.exception("Provider %s could not be loaded", network)
    return out


def available_networks(demo_mode: bool) -> list[str]:
    """Networks offered in the UI: the real ones, plus ``demo`` when demo mode is on."""
    return list(REAL_NETWORKS) + (["demo"] if demo_mode else [])


def register(provider: Provider) -> Provider:
    """Install ``provider`` for its ``network`` (replacing any cached instance). Mainly for tests."""
    with _lock:
        _instances[str(provider.network)] = provider
    return provider


def unregister(network: str) -> None:
    with _lock:
        _instances.pop(str(network), None)


def reset() -> None:
    """Forget cached/registered instances (they are re-created lazily)."""
    with _lock:
        _instances.clear()

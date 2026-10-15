"""Public media server + Cloudflare quick tunnel manager (SPEC §9).

STUB created by the foundation agent so imports resolve. TODO(public media agent): implement
``PublicMediaService`` (implements ``providers.base.PublicMedia``).
"""

from __future__ import annotations

from pathlib import Path


class PublicMediaService:
    def url_for(self, path: Path, mime: str, ttl_seconds: int = 3600) -> str:
        raise NotImplementedError("PublicMediaService.url_for")

    def release(self, url: str) -> None:
        raise NotImplementedError("PublicMediaService.release")

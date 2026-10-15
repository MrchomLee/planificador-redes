"""YouTube channel provider + Google OAuth helpers (SPEC §5, §6, §7; research 06).

STUB created by the foundation agent so imports resolve and the registry can list YouTube.
TODO(YouTube agent): implement (signatures below are the SPEC §6 contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import httpx

from planificador.providers.base import (
    AccountCreds,
    FormatSpec,
    Provider,
    ProviderSettings,
    PublishContext,
    StepResult,
)
from planificador.timeutil import Clock

SCOPES_YT: list[str] = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


@dataclass
class GoogleTokens:
    access_token: str
    expires_at: datetime | None
    refresh_token: str | None
    refresh_expires_at: datetime | None  # from refresh_token_expires_in ("Testing" projects: ~7 days)
    scopes: list[str] = field(default_factory=list)


@dataclass
class YouTubeChannel:
    id: str
    title: str
    custom_url: str | None = None
    thumbnail_url: str | None = None
    long_uploads_status: str | None = None


def redirect_uri(settings: ProviderSettings) -> str:
    return f"{settings.base_url}/conexiones/callback/google"


def new_pkce_pair() -> tuple[str, str]:
    raise NotImplementedError("youtube.new_pkce_pair")


def build_login_url(settings: ProviderSettings, redirect_uri: str, state: str, code_challenge: str) -> str:
    raise NotImplementedError("youtube.build_login_url")


def exchange_code(
    settings: ProviderSettings, http: httpx.Client, code: str, redirect_uri: str, code_verifier: str
) -> GoogleTokens:
    raise NotImplementedError("youtube.exchange_code")


def list_my_channels(
    settings: ProviderSettings, http: httpx.Client, access_token: str
) -> list[YouTubeChannel]:
    raise NotImplementedError("youtube.list_my_channels")


def revoke(settings: ProviderSettings, http: httpx.Client, token: str) -> None:
    raise NotImplementedError("youtube.revoke")


def run_lock_test(
    account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
) -> Literal["unlocked", "locked", "error"]:
    raise NotImplementedError("youtube.run_lock_test")


class YouTubeProvider(Provider):
    network = "youtube"
    display_name = "YouTube"
    formats = [
        FormatSpec("video", "Vídeo", "video"),
        FormatSpec("short", "Short", "video"),
    ]
    supports_comments = True

    def publish_step(self, ctx: PublishContext) -> StepResult:
        raise NotImplementedError("YouTubeProvider.publish_step")

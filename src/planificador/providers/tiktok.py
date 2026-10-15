"""TikTok provider + Login Kit helpers (SPEC §5, §6, §7, §10).

STUB created by the foundation agent so imports resolve. TODO(TikTok agent): implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from planificador.providers.base import (
    AccountCreds,
    FormatSpec,
    Provider,
    ProviderSettings,
    PublishContext,
    StepResult,
)


@dataclass
class TikTokTokens:
    open_id: str
    access_token: str
    expires_at: datetime | None
    refresh_token: str | None
    refresh_expires_at: datetime | None
    scopes: list[str] = field(default_factory=list)


@dataclass
class CreatorInfo:
    nickname: str | None
    username: str | None
    avatar_url: str | None
    privacy_level_options: list[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int | None


def redirect_uri(settings: ProviderSettings) -> str:
    return f"{settings.base_url}/conexiones/callback/social1"


def new_pkce_pair(encoding: str = "hex") -> tuple[str, str]:
    raise NotImplementedError("tiktok.new_pkce_pair")


def build_login_url(settings: ProviderSettings, redirect_uri: str, state: str, code_challenge: str) -> str:
    raise NotImplementedError("tiktok.build_login_url")


def exchange_code(
    settings: ProviderSettings, http: httpx.Client, code: str, redirect_uri: str, code_verifier: str
) -> TikTokTokens:
    raise NotImplementedError("tiktok.exchange_code")


def get_user_info(settings: ProviderSettings, http: httpx.Client, access_token: str) -> dict:
    raise NotImplementedError("tiktok.get_user_info")


def query_creator_info(account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> CreatorInfo:
    raise NotImplementedError("tiktok.query_creator_info")


def revoke(settings: ProviderSettings, http: httpx.Client, access_token: str) -> None:
    raise NotImplementedError("tiktok.revoke")


class TikTokProvider(Provider):
    network = "tiktok"
    display_name = "TikTok"
    formats = [
        FormatSpec("video", "Vídeo", "video"),
        FormatSpec("photo", "Fotos", "images"),
    ]
    supports_comments = False

    def publish_step(self, ctx: PublishContext) -> StepResult:
        raise NotImplementedError("TikTokProvider.publish_step")

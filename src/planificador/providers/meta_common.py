"""Shared Meta Graph API helpers: requests, error classification, Facebook Login (SPEC §6).

STUB created by the foundation agent so imports resolve. TODO(Facebook agent): implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from planificador.providers.base import ProviderError, ProviderSettings

GRAPH = "https://graph.facebook.com"

SCOPES_FB_IG: list[str] = [
    "pages_show_list",
    "pages_manage_posts",
    "pages_read_engagement",
    "read_insights",
    "pages_manage_engagement",
    "pages_read_user_content",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "instagram_manage_comments",
    "business_management",
]


@dataclass
class MetaIgAccount:
    id: str
    username: str | None = None
    name: str | None = None
    picture_url: str | None = None
    account_type: str | None = None


@dataclass
class MetaPage:
    id: str
    name: str
    access_token: str
    tasks: list[str] = field(default_factory=list)
    picture_url: str | None = None
    instagram: MetaIgAccount | None = None


def graph_url(settings: ProviderSettings, path: str) -> str:
    raise NotImplementedError("meta_common.graph_url")


def graph_request(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    headers: dict | None = None,
    timeout: float = 60.0,
) -> dict:
    raise NotImplementedError("meta_common.graph_request")


def classify_meta_error(status_code: int, body: dict | None, headers: object) -> ProviderError:
    raise NotImplementedError("meta_common.classify_meta_error")


def build_login_url(settings: ProviderSettings, redirect_uri: str, state: str) -> str:
    raise NotImplementedError("meta_common.build_login_url")


def exchange_code(
    settings: ProviderSettings, http: httpx.Client, code: str, redirect_uri: str
) -> tuple[str, datetime | None]:
    raise NotImplementedError("meta_common.exchange_code")


def list_pages(settings: ProviderSettings, http: httpx.Client, user_token: str) -> list[MetaPage]:
    raise NotImplementedError("meta_common.list_pages")


def redirect_uri(settings: ProviderSettings) -> str:
    return f"{settings.base_url}/conexiones/callback/meta"

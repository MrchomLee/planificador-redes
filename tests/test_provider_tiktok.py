"""TikTok provider tests (SPEC §5/§6, report 03). Fully offline: every HTTP call is mocked with respx."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from planificador.providers import registry, tiktok
from planificador.providers.base import (
    AccountCreds,
    ErrorKind,
    MediaInfo,
    ProviderError,
    ProviderSettings,
    PublishContext,
    StepResult,
    TargetSpec,
)
from planificador.timeutil import ManualClock

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
ACCESS = "act.SECRETACCESSTOKEN0123456789"
REFRESH = "rft.SECRETREFRESHTOKEN0123456789"
UPLOAD_HOST = "https://open-upload.tiktokapis.com/"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=111&upload_token=sig-abc"
UPLOAD_URL_2 = "https://open-upload.tiktokapis.com/video/?upload_id=222&upload_token=sig-def"
PUBLISH_ID = "v_pub_file~v2-1.7300000000000000001"
PUBLISH_ID_2 = "v_pub_file~v2-1.7300000000000000002"


# ------------------------------------------------------------------------------------------- helpers


def ok(data: dict | None = None) -> dict:
    return {"data": data or {}, "error": {"code": "ok", "message": "", "log_id": "202611030900LOG"}}


def api_error(code: str, message: str = "boom") -> dict:
    return {"data": {}, "error": {"code": code, "message": message, "log_id": "202611030900ERR"}}


def creator_json(options: tuple[str, ...] = ("SELF_ONLY",), **overrides: Any) -> dict:
    data = {
        "creator_avatar_url": "https://p16.tiktokcdn.example/avatar.jpeg",
        "creator_username": "mimarca",
        "creator_nickname": "Mi Marca",
        "privacy_level_options": list(options),
        "comment_disabled": False,
        "duet_disabled": False,
        "stitch_disabled": False,
        "max_video_post_duration_sec": 600,
    }
    data.update(overrides)
    return ok(data)


def init_json(publish_id: str = PUBLISH_ID, upload_url: str = UPLOAD_URL) -> dict:
    return ok({"publish_id": publish_id, "upload_url": upload_url})


def status_json(status: str, **extra: Any) -> dict:
    return ok({"status": status, **extra})


def parse_range(header: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", header)
    assert m, header
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


class Recorder:
    """Captures what the provider asks the engine to persist / log."""

    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.commits: list[dict] = []
        self.events: list[tuple[str, str, dict | None]] = []
        self.on_commit: Callable[[dict], None] | None = None

    def save_state(self, state: dict) -> None:
        self.saved.append(copy.deepcopy(state))

    def before_commit(self, state: dict) -> None:
        self.commits.append(copy.deepcopy(state))
        if self.on_commit:
            self.on_commit(state)

    def log(self, event: str, message: str, data: dict | None) -> None:
        self.events.append((event, message, data))

    @property
    def last(self) -> dict:
        return self.saved[-1]


class PutRecorder:
    """respx side effect for the upload URL: answers 206 / 201 like TikTok and records each chunk."""

    def __init__(self) -> None:
        self.chunks: list[tuple[int, int, int, int]] = []  # (first, last, total, body length)
        self.bodies: list[bytes] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        first, last, total = parse_range(request.headers["Content-Range"])
        body = request.content
        self.chunks.append((first, last, total, len(body)))
        self.bodies.append(body)
        self.requests.append(request)
        return httpx.Response(201 if last == total - 1 else 206)


@pytest.fixture
def settings() -> ProviderSettings:
    return ProviderSettings(
        meta_app_id=None,
        meta_app_secret=None,
        meta_api_version="v25.0",
        tiktok_client_key="ck_test_key",
        tiktok_client_secret="cs_TEST_SECRET_VALUE",
        tiktok_pkce_encoding="hex",
        base_url="http://localhost:8765",
        demo_mode=False,
    )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(tiktok, "_sleep", lambda s: calls.append(s))
    return calls


@pytest.fixture
def account() -> AccountCreds:
    return AccountCreds(
        id=7,
        network="tiktok",
        external_id="open-id-123",
        name="Mi Marca",
        account_type="creator",
        access_token=ACCESS,
        refresh_token=REFRESH,
        token_expires_at=NOW + timedelta(hours=20),
        refresh_expires_at=NOW + timedelta(days=300),
        meta={},
    )


SMALL_SIZE = 1950  # with the small_chunks fixture: 6 chunks of 300 bytes, the last one 450 bytes
SMALL_STARTS = [0, 300, 600, 900, 1200, 1500]


@pytest.fixture
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same chunking rules with tiny numbers (single chunk ≤ 1000 bytes, else 300-byte chunks)."""
    monkeypatch.setattr(tiktok, "SINGLE_CHUNK_MAX", 1000)
    monkeypatch.setattr(tiktok, "CHUNK_SIZE", 300)
    monkeypatch.setattr(tiktok, "UPLOAD_BLOCK", 64)


def make_video(
    tmp_path: Path,
    size: int,
    *,
    sparse: bool = True,
    duration: float | None = 12.0,
    mime: str = "video/mp4",
    name: str = "clip.mp4",
    media_id: int = 42,
) -> MediaInfo:
    path = tmp_path / name
    with open(path, "wb") as fh:
        if sparse:
            fh.truncate(size)
        else:
            fh.write(bytes(i % 251 for i in range(size)))
    return MediaInfo(
        id=media_id,
        kind="video",
        path=path,
        mime=mime,
        size_bytes=size,
        width=1080,
        height=1920,
        duration_s=duration,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        bitrate=4_000_000,
        is_hdr=False,
        original_name=name,
    )


_DROP = object()  # make_target(..., key=_DROP) removes a default option


def make_target(
    video: MediaInfo | list[MediaInfo], *, text: str = "Hola #tiktok", fmt: str = "video", **opts: Any
):
    media = video if isinstance(video, list) else [video]
    options = {"privacy_level": "SELF_ONLY", "consent": True}
    options.update(opts)
    options = {k: v for k, v in options.items() if v is not _DROP}
    return TargetSpec(
        network="tiktok",
        format=fmt,
        text=text,
        media=media,
        options=options,
        scheduled_at=None,
        auto_publish=True,
    )


def make_ctx(
    account: AccountCreds,
    target: TargetSpec,
    settings: ProviderSettings,
    http: httpx.Client,
    clock: ManualClock,
    *,
    state: dict | None = None,
    rec: Recorder | None = None,
) -> tuple[PublishContext, Recorder]:
    rec = rec or Recorder()
    ctx = PublishContext(
        account=account,
        target=target,
        target_id=99,
        state=state if state is not None else {},
        settings=settings,
        http=http,
        clock=clock,
        public_media=None,
        save_state=rec.save_state,
        before_commit=rec.before_commit,
        log=rec.log,
    )
    return ctx, rec


def run_until_done(
    provider: tiktok.TikTokProvider, ctx: PublishContext, max_steps: int = 80
) -> list[StepResult]:
    results: list[StepResult] = []
    for _ in range(max_steps):
        result = provider.publish_step(ctx)
        results.append(result)
        if result.status in ("published", "sent_to_app"):
            return results
    raise AssertionError(f"publish did not finish: {[r.status for r in results]}")


def mock_direct_flow(
    router: respx.MockRouter,
    *,
    creator: dict | None = None,
    statuses: list[dict] | None = None,
) -> tuple[respx.Route, respx.Route, respx.Route, PutRecorder, respx.Route]:
    creator_route = router.post(tiktok.CREATOR_INFO_URL).mock(
        return_value=httpx.Response(200, json=creator or creator_json())
    )
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    inbox_route = router.post(tiktok.INBOX_INIT_URL).mock(
        return_value=httpx.Response(200, json=init_json("v_inbox_file~v2-1.7300000000000000009"))
    )
    puts = PutRecorder()
    router.put(url__startswith=UPLOAD_HOST).mock(side_effect=puts)
    status_route = router.post(tiktok.STATUS_URL).mock(
        side_effect=[httpx.Response(200, json=s) for s in (statuses or [status_json("PUBLISH_COMPLETE")])]
    )
    return creator_route, init_route, status_route, puts, inbox_route


# ---------------------------------------------------------------------------------- OAuth helpers


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://localhost:8765", "http://localhost:8765/conexiones/callback/social1"),
        ("http://localhost:8765/", "http://localhost:8765/conexiones/callback/social1"),
        ("http://127.0.0.1:9100", "http://localhost:9100/conexiones/callback/social1"),
    ],
)
def test_redirect_uri_always_uses_localhost(settings, base_url, expected):
    assert tiktok.redirect_uri(replace(settings, base_url=base_url)) == expected


def test_pkce_hex_default_and_base64url_alternative():
    # RFC 7636 appendix B vector for the base64url form.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert tiktok.pkce_challenge(verifier, "base64url") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert tiktok.pkce_challenge(verifier) == hashlib.sha256(verifier.encode()).hexdigest()

    v1, c1 = tiktok.new_pkce_pair("hex")
    assert 43 <= len(v1) <= 128 and re.fullmatch(r"[A-Za-z0-9\-._~]+", v1)
    assert re.fullmatch(r"[0-9a-f]{64}", c1) and c1 == hashlib.sha256(v1.encode()).hexdigest()
    v2, c2 = tiktok.new_pkce_pair("base64url")
    assert v2 != v1
    assert "=" not in c2 and base64.urlsafe_b64decode(c2 + "=") == hashlib.sha256(v2.encode()).digest()
    with pytest.raises(ValueError):
        tiktok.new_pkce_pair("plain")


def test_build_login_url(settings):
    url = tiktok.build_login_url(settings, tiktok.redirect_uri(settings), "st4te", "chal")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == tiktok.AUTH_URL
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "client_key": "ck_test_key",
        "response_type": "code",
        "scope": "user.info.basic,video.publish,video.upload",
        "redirect_uri": "http://localhost:8765/conexiones/callback/social1",
        "state": "st4te",
        "code_challenge": "chal",
        "code_challenge_method": "S256",
        "disable_auto_auth": "1",
    }
    assert "cs_TEST_SECRET_VALUE" not in url

    with_stats = tiktok.build_login_url(
        settings, "http://localhost:8765/x", "s", "c", scopes=tiktok.login_scopes(include_analytics=True)
    )
    assert parse_qs(urlsplit(with_stats).query)["scope"] == [
        "user.info.basic,video.publish,video.upload,user.info.profile,user.info.stats,video.list"
    ]
    settings.tiktok_analytics_scopes = True  # optional attribute the integrator may add to ProviderSettings
    assert "user.info.stats" in tiktok.login_scopes(settings)


def test_build_login_url_requires_client_key(settings):
    with pytest.raises(ProviderError) as exc:
        tiktok.build_login_url(replace(settings, tiktok_client_key=None), "http://localhost:8765/x", "s", "c")
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "tiktok_not_configured"


def test_exchange_code_success(settings, http, router, clock):
    route = router.post(tiktok.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": ACCESS,
                "expires_in": 86400,
                "open_id": "open-id-123",
                "refresh_token": REFRESH,
                "refresh_expires_in": 31536000,
                "scope": "user.info.basic,video.publish,video.upload",
                "token_type": "Bearer",
            },
        )
    )
    tokens = tiktok.exchange_code(settings, http, "CODE*1!", "http://localhost:8765/cb", "verif", clock=clock)
    assert tokens == tiktok.TikTokTokens(
        open_id="open-id-123",
        access_token=ACCESS,
        expires_at=NOW + timedelta(hours=24),
        refresh_token=REFRESH,
        refresh_expires_at=NOW + timedelta(days=365),
        scopes=["user.info.basic", "video.publish", "video.upload"],
    )
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "client_key": "ck_test_key",
        "client_secret": "cs_TEST_SECRET_VALUE",
        "code": "CODE*1!",
        "grant_type": "authorization_code",
        "redirect_uri": "http://localhost:8765/cb",
        "code_verifier": "verif",
    }


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (
            400,
            {"error": "invalid_grant", "error_description": "expired", "log_id": "L"},
            ErrorKind.AUTH,
            "invalid_grant",
        ),
        (
            400,
            {"error": "invalid_request", "error_description": "bad verifier"},
            ErrorKind.PERMANENT,
            "invalid_request",
        ),
        (401, {"error": "invalid_client"}, ErrorKind.AUTH, "invalid_client"),
        (503, None, ErrorKind.TRANSIENT, "http_503"),
        (200, {"error": "server_error"}, ErrorKind.TRANSIENT, "server_error"),
    ],
)
def test_exchange_code_errors(settings, http, router, status, body, kind, code):
    response = (
        httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="<html>")
    )
    router.post(tiktok.TOKEN_URL).mock(return_value=response)
    with pytest.raises(ProviderError) as exc:
        tiktok.exchange_code(settings, http, "c", "http://localhost:8765/cb", "v")
    assert exc.value.kind == kind and exc.value.code == code
    if code in {"invalid_request", "invalid_grant"}:
        assert "PKCE" in exc.value.message  # hint: switch tiktok_pkce_encoding
    assert "cs_TEST_SECRET_VALUE" not in json.dumps(exc.value.to_dict())


def test_exchange_code_network_error_is_transient(settings, http, router):
    router.post(tiktok.TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError) as exc:
        tiktok.exchange_code(settings, http, "c", "http://localhost:8765/cb", "v")
    assert exc.value.kind == ErrorKind.TRANSIENT and exc.value.code == "tiktok_network"


def test_exchange_code_requires_secret(settings, http, router):
    with pytest.raises(ProviderError) as exc:
        tiktok.exchange_code(replace(settings, tiktok_client_secret=None), http, "c", "r", "v")
    assert exc.value.code == "tiktok_not_configured"


# ---------------------------------------------------------------------------------- credentials


def test_refresh_not_needed_makes_no_call(settings, http, router, clock, account):
    assert tiktok.TikTokProvider().refresh_credentials(account, settings, http, clock) is None
    assert not router.calls


def test_refresh_rotates_refresh_token_when_less_than_one_hour_left(settings, http, router, clock, account):
    acct = replace(account, token_expires_at=NOW + timedelta(minutes=50))
    route = router.post(tiktok.TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "act.NEW",
                "expires_in": 86400,
                "refresh_token": "rft.NEW",
                "refresh_expires_in": 31536000,
                "open_id": "open-id-123",
                "scope": "user.info.basic,video.publish",
            },
        )
    )
    update = tiktok.TikTokProvider().refresh_credentials(acct, settings, http, clock)
    assert update is not None
    assert update.access_token == "act.NEW" and update.token_expires_at == NOW + timedelta(days=1)
    assert update.refresh_token == "rft.NEW" and update.refresh_expires_at == NOW + timedelta(days=365)
    assert update.scopes == ["user.info.basic", "video.publish"]
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == [REFRESH]


def test_refresh_without_rotation_keeps_old_refresh_token(settings, http, router, clock, account):
    acct = replace(account, token_expires_at=None)  # unknown expiry → refresh
    router.post(tiktok.TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "act.N"}))
    update = tiktok.TikTokProvider().refresh_credentials(acct, settings, http, clock)
    assert update.access_token == "act.N"
    assert update.refresh_token is None and update.refresh_expires_at is None  # apply_token_update keeps old
    assert update.scopes is None


def test_refresh_rotation_applies_to_account_model(settings, http, router, clock, account):
    from planificador.models import Account

    model = Account(brand_id=1, network="tiktok", external_id="open-id-123")
    model.set_tokens(ACCESS, REFRESH, token_expires_at=NOW + timedelta(minutes=10))
    router.post(tiktok.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "act.B", "refresh_token": "rft.B"})
    )
    update = tiktok.TikTokProvider().refresh_credentials(model.to_creds(), settings, http, clock)
    model.apply_token_update(update)
    assert model.access_token == "act.B" and model.refresh_token == "rft.B"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"refresh_token": None}, "tiktok_no_refresh_token"),
        ({"refresh_expires_at": NOW - timedelta(seconds=1)}, "tiktok_refresh_expired"),
    ],
)
def test_refresh_impossible_is_auth(settings, http, router, clock, account, changes, code):
    acct = replace(account, token_expires_at=NOW + timedelta(minutes=5), **changes)
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().refresh_credentials(acct, settings, http, clock)
    assert exc.value.kind == ErrorKind.AUTH and exc.value.code == code
    assert not router.calls


def test_refresh_invalid_grant_is_auth(settings, http, router, clock, account):
    acct = replace(account, token_expires_at=NOW - timedelta(hours=1))
    router.post(tiktok.TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
    )
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().refresh_credentials(acct, settings, http, clock)
    assert exc.value.kind == ErrorKind.AUTH
    assert "Vuelve a conectar" in exc.value.message
    assert REFRESH not in json.dumps(exc.value.to_dict())


def test_revoke(settings, http, router):
    route = router.post(tiktok.REVOKE_URL).mock(return_value=httpx.Response(200, json={}))
    tiktok.revoke(settings, http, ACCESS)
    form = {k: v[0] for k, v in parse_qs(route.calls.last.request.content.decode()).items()}
    assert form == {"client_key": "ck_test_key", "client_secret": "cs_TEST_SECRET_VALUE", "token": ACCESS}


def test_revoke_is_best_effort_unless_strict(settings, http, router):
    router.post(tiktok.REVOKE_URL).mock(return_value=httpx.Response(500, text="oops"))
    tiktok.revoke(settings, http, ACCESS)  # swallowed
    with pytest.raises(ProviderError) as exc:
        tiktok.revoke(settings, http, ACCESS, strict=True)
    assert exc.value.kind == ErrorKind.TRANSIENT
    tiktok.revoke(settings, http, "")  # nothing to revoke: no call


def test_check_account(settings, http, router, account):
    route = router.get(tiktok.USER_INFO_URL).mock(
        side_effect=[
            httpx.Response(200, json=ok({"user": {"open_id": "open-id-123"}})),
            httpx.Response(401, json=api_error("access_token_invalid")),
            httpx.Response(401, text=""),
        ]
    )
    provider = tiktok.TikTokProvider()
    provider.check_account(account, settings, http)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"
    for _ in range(2):
        with pytest.raises(ProviderError) as exc:
            provider.check_account(account, settings, http)
        assert exc.value.kind == ErrorKind.AUTH


# ----------------------------------------------------------------------------------- user / creator


def test_get_user_info_with_profile_scope(settings, http, router):
    route = router.get(tiktok.USER_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json=ok(
                {
                    "user": {
                        "open_id": "open-id-123",
                        "union_id": "union-1",
                        "display_name": "Mi Marca",
                        "username": "mimarca",
                        "avatar_url": "https://p16.example/a.jpg",
                        "is_verified": False,
                    }
                }
            ),
        )
    )
    info = tiktok.get_user_info(settings, http, ACCESS)
    assert info["open_id"] == "open-id-123" and info["username"] == "mimarca"
    assert info["display_name"] == "Mi Marca" and info["avatar_url"] == "https://p16.example/a.jpg"
    assert info["profile_available"] is True and info["is_verified"] is False
    fields = route.calls.last.request.url.params["fields"].split(",")
    assert {"open_id", "display_name", "avatar_url", "username"} <= set(fields)


def test_get_user_info_falls_back_without_profile_scope(settings, http, router):
    route = router.get(tiktok.USER_INFO_URL).mock(
        side_effect=[
            httpx.Response(401, json=api_error("scope_not_authorized")),
            httpx.Response(200, json=ok({"user": {"open_id": "open-id-123", "display_name": "Mi Marca"}})),
        ]
    )
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    info = tiktok.get_user_info(settings, http, ACCESS)
    assert info["profile_available"] is False
    assert info["username"] == "mimarca"  # from creator_info
    assert "username" not in route.calls.last.request.url.params["fields"]


def test_get_user_info_auth_error(settings, http, router):
    router.get(tiktok.USER_INFO_URL).mock(
        return_value=httpx.Response(401, json=api_error("access_token_invalid"))
    )
    with pytest.raises(ProviderError) as exc:
        tiktok.get_user_info(settings, http, ACCESS)
    assert exc.value.kind == ErrorKind.AUTH


def test_query_creator_info(settings, http, router, account):
    route = router.post(tiktok.CREATOR_INFO_URL).mock(
        return_value=httpx.Response(
            200, json=creator_json(("PUBLIC_TO_EVERYONE", "SELF_ONLY"), duet_disabled=True)
        )
    )
    info = tiktok.query_creator_info(account, settings, http)
    assert info == tiktok.CreatorInfo(
        nickname="Mi Marca",
        username="mimarca",
        avatar_url="https://p16.tiktokcdn.example/avatar.jpeg",
        privacy_level_options=["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        comment_disabled=False,
        duet_disabled=True,
        stitch_disabled=False,
        max_video_post_duration_sec=600,
    )
    assert info.privacy_choices() == [("PUBLIC_TO_EVERYONE", "Público"), ("SELF_ONLY", "Solo yo")]
    assert not info.is_unaudited
    assert tiktok.CreatorInfo.from_dict(info.to_dict()) == info
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {ACCESS}" and json.loads(request.content) == {}


@pytest.mark.parametrize(
    ("status", "code", "kind", "retry_after"),
    [
        (200, "spam_risk_too_many_posts", ErrorKind.RATE_LIMITED, tiktok.DAILY_CAP_RETRY_SECONDS),
        (200, "reached_active_user_cap", ErrorKind.RATE_LIMITED, tiktok.DAILY_CAP_RETRY_SECONDS),
        (200, "spam_risk_user_banned_from_posting", ErrorKind.PERMANENT, None),
        (401, "access_token_invalid", ErrorKind.AUTH, None),
        (429, "rate_limit_exceeded", ErrorKind.RATE_LIMITED, 60),
    ],
)
def test_query_creator_info_reads_error_code_even_on_http_200(
    settings, http, router, account, status, code, kind, retry_after
):
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(status, json=api_error(code)))
    with pytest.raises(ProviderError) as exc:
        tiktok.query_creator_info(account, settings, http)
    assert exc.value.kind == kind and exc.value.code == code and exc.value.retry_after == retry_after
    assert exc.value.detail["log_id"] == "202611030900ERR"
    assert ACCESS not in json.dumps(exc.value.to_dict())


# ------------------------------------------------------------------------------------- chunk math


@pytest.mark.parametrize(
    ("size", "chunk_size", "count", "last_len"),
    [
        (1, 1, 1, 1),
        (4_194_304, 4_194_304, 1, 4_194_304),  # < 5 MB must be uploaded whole
        (5_000_000, 5_000_000, 1, 5_000_000),  # exactly 5 MB
        (5 * 1024 * 1024, 5 * 1024 * 1024, 1, 5 * 1024 * 1024),
        (64_000_000, 64_000_000, 1, 64_000_000),  # largest single chunk
        (64_000_001, 10_000_000, 6, 14_000_001),  # first size that must be chunked
        (130_000_000, 10_000_000, 13, 10_000_000),  # 130 MB
        (130 * 1024 * 1024, 10_000_000, 13, 16_314_880),  # 130 MiB
        (50_000_123 + 64_000_000, 10_000_000, 11, 14_000_123),
    ],
)
def test_plan_chunks(size, chunk_size, count, last_len):
    plan = tiktok.plan_chunks(size)
    assert (plan.video_size, plan.chunk_size, plan.total_chunks) == (size, chunk_size, count)
    assert plan.chunk_length(count - 1) == last_len
    ranges = [plan.chunk_range(i) for i in range(count)]
    assert ranges[0][0] == 0 and ranges[-1][1] == size - 1
    assert all(a[1] + 1 == b[0] for a, b in zip(ranges, ranges[1:], strict=False))
    assert sum(plan.chunk_length(i) for i in range(count)) == size
    if count > 1:  # TikTok rules: 5–64 MB chunks, last chunk ≤ 128 MB
        assert all(5_000_000 <= plan.chunk_length(i) <= 64_000_000 for i in range(count - 1))
        assert plan.chunk_length(count - 1) <= 128_000_000
    with pytest.raises(IndexError):
        plan.chunk_range(count)


def test_plan_chunks_rejects_empty_and_too_many_chunks():
    with pytest.raises(ValueError):
        tiktok.plan_chunks(0)
    assert tiktok.plan_chunks(1000 * 10_000_000 + 9_999_999).total_chunks == 1000
    with pytest.raises(ValueError):
        tiktok.plan_chunks(1001 * 10_000_000)


# ------------------------------------------------------------------------------------- publishing


def test_direct_post_happy_path(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 1500, sparse=False, duration=20.0)
    target = make_target(
        video,
        disable_comment=False,
        is_aigc=True,
        brand_organic_toggle=True,
        video_cover_timestamp_ms=1500,
    )
    creator_route, init_route, status_route, puts, inbox_route = mock_direct_flow(
        router,
        creator=creator_json(("SELF_ONLY",), duet_disabled=True),
        statuses=[status_json("PROCESSING_UPLOAD"), status_json("PUBLISH_COMPLETE")],
    )
    ctx, rec = make_ctx(account, target, settings, http, clock)
    rec.on_commit = lambda st: (
        None if init_route.call_count == 0 else pytest.fail("init sent before before_commit")
    )
    provider = tiktok.TikTokProvider()

    step = provider.publish_step(ctx)
    assert step.status == "continue"
    assert creator_route.call_count == 1 and init_route.call_count == 1
    assert len(rec.commits) == 1
    assert "publish_id" not in rec.commits[0] and rec.commits[0]["phase"] == "init_sent"
    assert rec.last["publish_id"] == PUBLISH_ID and rec.last["phase"] == "uploading"
    body = json.loads(init_route.calls.last.request.content)
    assert body == {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": 1500,
            "chunk_size": 1500,
            "total_chunk_count": 1,
        },
        "post_info": {
            "title": "Hola #tiktok",
            "privacy_level": "SELF_ONLY",
            "disable_comment": False,
            "disable_duet": True,  # creator has duets disabled
            "disable_stitch": True,  # not explicitly allowed → disabled
            "is_aigc": True,
            "brand_organic_toggle": True,
            "brand_content_toggle": False,
            "video_cover_timestamp_ms": 1500,
        },
    }
    assert init_route.calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"

    step = provider.publish_step(ctx)  # single chunk upload
    assert step.status == "wait" and step.wait_seconds == 5
    assert puts.chunks == [(0, 1499, 1500, 1500)]
    put_request = puts.requests[0]
    assert put_request.headers["content-type"] == "video/mp4"
    assert put_request.headers["content-length"] == "1500"
    assert "authorization" not in put_request.headers
    assert puts.bodies[0] == video.path.read_bytes()

    step = provider.publish_step(ctx)
    assert step.status == "wait" and step.wait_seconds == 10
    step = provider.publish_step(ctx)
    assert step.status == "published"
    assert step.external_id == PUBLISH_ID  # SELF_ONLY posts never get a public id
    assert step.external_url == "https://www.tiktok.com/@mimarca"
    assert "Solo yo" in step.message
    assert json.loads(status_route.calls.last.request.content) == {"publish_id": PUBLISH_ID}
    assert inbox_route.call_count == 0
    assert len(rec.commits) == 1
    events = [e[0] for e in rec.events]
    assert events[:2] == ["tiktok_creator", "tiktok_init"] and "tiktok_uploaded" in events
    assert "sig-abc" not in json.dumps([e[2] for e in rec.events])  # never log the upload URL

    # Re-entrant once done: same result, no HTTP.
    calls = len(router.calls)
    again = provider.publish_step(ctx)
    assert (again.status, again.external_id) == ("published", PUBLISH_ID) and len(router.calls) == calls


def test_public_post_waits_for_public_id(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 2000)
    target = make_target(video, privacy_level="PUBLIC_TO_EVERYONE")
    mock_direct_flow(
        router,
        creator=creator_json(("PUBLIC_TO_EVERYONE", "FOLLOWER_OF_CREATOR", "SELF_ONLY")),
        statuses=[
            status_json("PUBLISH_COMPLETE"),
            status_json("PUBLISH_COMPLETE", publicaly_available_post_id=[7300000000000000123]),
        ],
    )
    ctx, rec = make_ctx(account, target, settings, http, clock)
    results = run_until_done(tiktok.TikTokProvider(), ctx)
    assert [(r.status, r.wait_seconds) for r in results] == [
        ("continue", 0),
        ("wait", 5),
        ("wait", tiktok.PUBLIC_ID_WAIT_SECONDS),
        ("published", 0),
    ]
    assert results[-1].external_id == "7300000000000000123"
    assert results[-1].external_url == "https://www.tiktok.com/@mimarca/video/7300000000000000123"
    assert "Solo yo" not in results[-1].message


def test_multi_chunk_upload_64000001_bytes(tmp_path, settings, http, router, clock, account):
    size = 64_000_001
    video = make_video(tmp_path, size, mime="video/quicktime", name="clip.mov")
    target = make_target(video)
    _, init_route, _, puts, _ = mock_direct_flow(router)
    ctx, rec = make_ctx(account, target, settings, http, clock)
    results = run_until_done(tiktok.TikTokProvider(), ctx)
    source = json.loads(init_route.calls.last.request.content)["source_info"]
    assert source == {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": 10_000_000,
        "total_chunk_count": 6,
    }
    assert puts.chunks == [
        (0, 9_999_999, size, 10_000_000),
        (10_000_000, 19_999_999, size, 10_000_000),
        (20_000_000, 29_999_999, size, 10_000_000),
        (30_000_000, 39_999_999, size, 10_000_000),
        (40_000_000, 49_999_999, size, 10_000_000),
        (50_000_000, 64_000_000, size, 14_000_001),
    ]
    assert {r.headers["content-type"] for r in puts.requests} == {"video/quicktime"}
    assert [r.headers["content-length"] for r in puts.requests][-1] == "14000001"
    assert [r.status for r in results] == ["continue"] * 6 + ["wait", "published"]
    progress = [s["next_chunk"] for s in rec.saved if s.get("phase") == "uploading" and "next_chunk" in s]
    assert progress[:6] == [0, 1, 2, 3, 4, 5]
    assert rec.saved[-1]["phase"] == "done"


def test_chunk_bytes_are_exact(tmp_path, settings, http, router, clock, account, small_chunks):
    video = make_video(tmp_path, 1150, sparse=False)
    _, _, _, puts, _ = mock_direct_flow(router)
    ctx, _ = make_ctx(account, make_target(video), settings, http, clock)
    run_until_done(tiktok.TikTokProvider(), ctx)
    assert [c[:2] for c in puts.chunks] == [(0, 299), (300, 599), (600, 1149)]
    assert b"".join(puts.bodies) == video.path.read_bytes()


def test_exactly_5mb_is_one_chunk(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 5_000_000)
    _, _, _, puts, _ = mock_direct_flow(router)
    ctx, _ = make_ctx(account, make_target(video), settings, http, clock)
    run_until_done(tiktok.TikTokProvider(), ctx)
    assert puts.chunks == [(0, 4_999_999, 5_000_000, 5_000_000)]


def test_inbox_mode_sends_draft_to_app(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    target = make_target(video, mode="inbox", privacy_level=_DROP, consent=_DROP)
    creator_route, init_route, _, puts, inbox_route = mock_direct_flow(
        router, statuses=[status_json("PROCESSING_UPLOAD"), status_json("SEND_TO_USER_INBOX")]
    )
    ctx, rec = make_ctx(account, target, settings, http, clock)
    results = run_until_done(tiktok.TikTokProvider(), ctx)
    assert creator_route.call_count == 0 and init_route.call_count == 0 and inbox_route.call_count == 1
    assert json.loads(inbox_route.calls.last.request.content) == {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": 3000,
            "chunk_size": 3000,
            "total_chunk_count": 1,
        }
    }
    final = results[-1]
    assert final.status == "sent_to_app" and final.external_id == "v_inbox_file~v2-1.7300000000000000009"
    assert final.external_url is None and "bandeja de entrada" in final.message
    assert len(rec.commits) == 1


def test_inbox_pending_share_limit(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.INBOX_INIT_URL).mock(
        return_value=httpx.Response(403, json=api_error("spam_risk_too_many_pending_share"))
    )
    ctx, rec = make_ctx(account, make_target(video, mode="inbox"), settings, http, clock)
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().publish_step(ctx)
    assert exc.value.kind == ErrorKind.PERMANENT
    assert "borradores pendientes" in exc.value.message


def test_direct_post_diverted_to_inbox(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    mock_direct_flow(router, statuses=[status_json("SEND_TO_USER_INBOX")])
    ctx, _ = make_ctx(account, make_target(video), settings, http, clock)
    final = run_until_done(tiktok.TikTokProvider(), ctx)[-1]
    assert final.status == "sent_to_app" and "borrador" in final.message


@pytest.mark.parametrize(
    ("reason", "kind", "needle"),
    [
        ("file_format_check_failed", ErrorKind.PERMANENT, "formato"),
        ("duration_check_failed", ErrorKind.PERMANENT, "duración"),
        ("frame_rate_check_failed", ErrorKind.PERMANENT, "fps"),
        ("picture_size_check_failed", ErrorKind.PERMANENT, "resolución"),
        ("publish_cancelled", ErrorKind.PERMANENT, "canceló"),
        ("auth_removed", ErrorKind.AUTH, "conectar"),
        ("internal", ErrorKind.TRANSIENT, "interno"),
        ("spam_risk_too_many_posts", ErrorKind.RATE_LIMITED, "límite diario"),
        ("spam_risk_text", ErrorKind.PERMANENT, "spam"),
        ("brand_new_reason", ErrorKind.PERMANENT, "brand_new_reason"),
    ],
)
def test_failed_status_maps_fail_reason(
    tmp_path, settings, http, router, clock, account, reason, kind, needle
):
    video = make_video(tmp_path, 3000)
    mock_direct_flow(router, statuses=[status_json("FAILED", fail_reason=reason)])
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == kind and needle in exc.value.message
    assert exc.value.detail["fail_reason"] == reason
    # FAILED is definitive: the state is reset so a later retry starts with a fresh init.
    assert rec.last["phase"] == "new" and "publish_id" not in rec.last
    assert rec.last["abandoned"][-1]["publish_id"] == PUBLISH_ID


def test_retry_after_failed_starts_fresh(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(
        side_effect=[
            httpx.Response(200, json=init_json(PUBLISH_ID, UPLOAD_URL)),
            httpx.Response(200, json=init_json(PUBLISH_ID_2, UPLOAD_URL_2)),
        ]
    )
    router.put(url__startswith=UPLOAD_HOST).mock(side_effect=PutRecorder())
    router.post(tiktok.STATUS_URL).mock(
        side_effect=[
            httpx.Response(200, json=status_json("FAILED", fail_reason="internal")),
            httpx.Response(200, json=status_json("PUBLISH_COMPLETE")),
        ]
    )
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)
    with pytest.raises(ProviderError):
        provider.publish_step(ctx)
    final = run_until_done(provider, ctx)[-1]
    assert init_route.call_count == 2 and len(rec.commits) == 2
    assert final.external_id == PUBLISH_ID_2


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"privacy_level": _DROP}, "tiktok_privacy_required"),
        ({"privacy_level": "EVERYBODY"}, "tiktok_privacy_invalid"),
        ({"brand_content_toggle": True}, "tiktok_branded_private"),
        ({"consent": False}, "tiktok_consent"),
        ({"text": "😀" * 1101}, "tiktok_caption_too_long"),  # 2202 UTF-16 units, 1101 code points
        ({"fmt": "photo"}, "tiktok_photo_unsupported"),
    ],
)
def test_invalid_settings_fail_before_any_call(
    tmp_path, settings, http, router, clock, account, changes, code
):
    video = make_video(tmp_path, 3000)
    creator_route, init_route, *_ = mock_direct_flow(router)
    target = make_target(video, **changes)
    ctx, rec = make_ctx(account, target, settings, http, clock)
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().publish_step(ctx)
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == code
    assert not rec.commits and init_route.call_count == 0 and creator_route.call_count == 0


@pytest.mark.parametrize(
    ("creator", "changes", "code", "needles"),
    [
        (
            creator_json(("SELF_ONLY",)),
            {"privacy_level": "PUBLIC_TO_EVERYONE"},
            "tiktok_privacy_not_allowed",
            ["Solo yo", "privada"],
        ),
        (
            creator_json(("PUBLIC_TO_EVERYONE",)),
            {"privacy_level": "SELF_ONLY"},
            "tiktok_privacy_not_allowed",
            ["Solo yo"],
        ),
        (creator_json(max_video_post_duration_sec=180), {}, "tiktok_too_long", ["181", "180"]),
    ],
)
def test_creator_info_constraints_fail_before_commit(
    tmp_path, settings, http, router, clock, account, creator, changes, code, needles
):
    video = make_video(tmp_path, 3000, duration=181.2)
    creator_route, init_route, *_ = mock_direct_flow(router, creator=creator)
    ctx, rec = make_ctx(account, make_target(video, **changes), settings, http, clock)
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().publish_step(ctx)
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == code
    assert all(n in exc.value.message for n in needles)
    assert creator_route.call_count == 1 and init_route.call_count == 0 and not rec.commits


def test_caption_limit_counts_utf16_units(tmp_path):
    video = make_video(tmp_path, 10)
    assert tiktok.utf16_len("😀" * 1100) == 2200
    ok_target = make_target(video, text="😀" * 1100)
    assert not [i for i in tiktok.check_post_settings(ok_target) if i.level == "error"]
    assert (
        tiktok.check_post_settings(make_target(video, text="a" * 2201))[0].code == "tiktok_caption_too_long"
    )


def test_media_problems(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    other = make_video(tmp_path, 3000, name="b.mp4", media_id=43)
    provider = tiktok.TikTokProvider()
    for target, code in [
        (make_target([video, other]), "tiktok_one_video"),
        (make_target(replace(video, kind="image", mime="image/jpeg")), "tiktok_one_video"),
        (make_target(replace(video, path=tmp_path / "missing.mp4")), "tiktok_file_missing"),
    ]:
        ctx, rec = make_ctx(account, target, settings, http, clock)
        with pytest.raises(ProviderError) as exc:
            provider.publish_step(ctx)
        assert exc.value.code == code and exc.value.kind == ErrorKind.PERMANENT and not rec.commits
    assert not router.calls


def test_unaudited_warning_and_post_info_defaults(tmp_path):
    video = make_video(tmp_path, 10, duration=4.0)
    info = tiktok.CreatorInfo("N", "u", None, ["SELF_ONLY"], True, False, False, 600)
    issues = tiktok.check_post_settings(make_target(video), info)
    assert [i.code for i in issues] == ["tiktok_unaudited"] and issues[0].level == "warning"
    post_info = tiktok.build_post_info(make_target(video, text="", video_cover_timestamp_ms=99_999), info)
    assert "title" not in post_info
    assert post_info["disable_comment"] is True  # creator disabled comments
    assert post_info["disable_duet"] is True and post_info["disable_stitch"] is True  # unchecked by default
    assert post_info["video_cover_timestamp_ms"] == 3999  # clamped to the video duration
    allowed = tiktok.build_post_info(
        make_target(video, disable_duet="false", disable_stitch=0, brand_content_toggle="si"), None
    )
    assert allowed["disable_duet"] is False and allowed["disable_stitch"] is False
    assert allowed["brand_content_toggle"] is True


@pytest.mark.parametrize(
    ("status", "code", "kind", "needles", "headers"),
    [
        (
            403,
            "unaudited_client_can_only_post_to_private_accounts",
            ErrorKind.PERMANENT,
            ["Solo yo", "privada", "auditoría"],
            {},
        ),
        (429, "rate_limit_exceeded", ErrorKind.RATE_LIMITED, ["limitado"], {"Retry-After": "30"}),
        (500, "internal_error", ErrorKind.TRANSIENT, ["temporal"], {}),
        (401, "access_token_invalid", ErrorKind.AUTH, ["conectar"], {}),
        (403, "spam_risk_too_many_posts", ErrorKind.RATE_LIMITED, ["límite diario"], {}),
        (403, "reached_active_user_cap", ErrorKind.RATE_LIMITED, ["24 horas"], {}),
        (400, "privacy_level_option_mismatch", ErrorKind.PERMANENT, ["visibilidad"], {}),
        (400, "invalid_param", ErrorKind.PERMANENT, ["parámetro"], {}),
        (403, "scope_not_authorized", ErrorKind.PERMANENT, ["permiso"], {}),
        (403, "spam_risk_user_banned_from_posting", ErrorKind.PERMANENT, ["spam"], {}),
        (200, "spam_risk_text", ErrorKind.PERMANENT, ["texto"], {}),
        (400, "invalid_file_upload", ErrorKind.PERMANENT, ["fragmentos"], {}),
        (418, "something_new", ErrorKind.PERMANENT, ["something_new"], {}),
    ],
)
def test_init_errors_are_classified_and_safe_to_retry(
    tmp_path, settings, http, router, clock, account, status, code, kind, needles, headers
):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.CREATOR_INFO_URL).mock(
        return_value=httpx.Response(200, json=creator_json(("PUBLIC_TO_EVERYONE", "SELF_ONLY")))
    )
    router.post(tiktok.VIDEO_INIT_URL).mock(
        return_value=httpx.Response(status, json=api_error(code), headers=headers)
    )
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == kind and exc.value.code == code
    assert all(n in exc.value.message for n in needles), exc.value.message
    if headers:
        assert exc.value.retry_after == 30
    if code == "spam_risk_too_many_posts":
        assert exc.value.retry_after == tiktok.DAILY_CAP_RETRY_SECONDS
    assert len(rec.commits) == 1  # the init was attempted after before_commit...
    assert rec.last["phase"] == "new" and "publish_id" not in rec.last  # ...but nothing can go live
    assert provider.reconcile(ctx).outcome == "not_published"
    assert ACCESS not in json.dumps(exc.value.to_dict())


def test_init_network_error_is_transient_and_reconciles_as_not_published(
    tmp_path, settings, http, router, clock, account
):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    router.post(tiktok.VIDEO_INIT_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    ctx, _ = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.TRANSIENT and exc.value.code == "tiktok_timeout"
    assert provider.reconcile(ctx).outcome == "not_published"


def test_creator_info_refusal_happens_before_commit(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.CREATOR_INFO_URL).mock(
        return_value=httpx.Response(200, json=api_error("spam_risk_too_many_posts"))
    )
    init_route = router.post(tiktok.VIDEO_INIT_URL)
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().publish_step(ctx)
    assert exc.value.kind == ErrorKind.RATE_LIMITED
    assert not rec.commits and init_route.call_count == 0


def test_missing_token_is_auth(tmp_path, settings, http, router, clock, account):
    ctx, _ = make_ctx(
        replace(account, access_token=None), make_target(make_video(tmp_path, 10)), settings, http, clock
    )
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().publish_step(ctx)
    assert exc.value.kind == ErrorKind.AUTH


# --------------------------------------------------------------------------------- resume / retry


def test_resume_upload_after_crash(tmp_path, settings, http, router, clock, account, small_chunks):
    video = make_video(tmp_path, SMALL_SIZE, sparse=False)
    target = make_target(video)
    _, init_route, _, puts, _ = mock_direct_flow(router)
    ctx, rec = make_ctx(account, target, settings, http, clock)
    provider = tiktok.TikTokProvider()
    for _ in range(3):  # init + 2 chunks, then the process "crashes"
        assert provider.publish_step(ctx).status == "continue"
    saved = copy.deepcopy(rec.last)
    assert saved["next_chunk"] == 2 and saved["publish_id"] == PUBLISH_ID

    puts.chunks.clear()
    puts.bodies.clear()
    ctx2, rec2 = make_ctx(account, target, settings, http, clock, state=saved)
    results = run_until_done(tiktok.TikTokProvider(), ctx2)
    assert init_route.call_count == 1 and not rec2.commits  # never re-initialised
    assert [c[0] for c in puts.chunks] == SMALL_STARTS[2:]
    assert b"".join(puts.bodies) == video.path.read_bytes()[600:]
    assert results[-1].status == "published" and results[-1].external_id == PUBLISH_ID


def test_crash_after_init_before_publish_id_was_saved(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    _, init_route, *_ = mock_direct_flow(router)
    state = {"phase": "init_sent", "mode": "direct", "video_size": 3000, "init_attempts": 1}
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock, state=state)
    provider = tiktok.TikTokProvider()
    rec_result = provider.reconcile(ctx)
    assert rec_result.outcome == "not_published"  # FILE_UPLOAD: no bytes → nothing can be live
    assert not router.calls
    final = run_until_done(provider, ctx)[-1]
    assert init_route.call_count == 1 and final.status == "published"
    assert len(rec.commits) == 1 and rec.last["init_attempts"] == 2


def test_chunk_network_error_retries_same_chunk(
    tmp_path, settings, http, router, clock, account, small_chunks
):
    video = make_video(tmp_path, SMALL_SIZE)
    puts = PutRecorder()
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] in (2, 3):  # second chunk fails twice
            raise httpx.WriteError("reset")
        return puts(request)

    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    router.put(url__startswith=UPLOAD_HOST).mock(side_effect=flaky)
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    results = run_until_done(tiktok.TikTokProvider(), ctx)
    waits = [r.wait_seconds for r in results if r.status == "wait"]
    assert waits[:2] == [5, 15]
    assert [c[0] for c in puts.chunks] == SMALL_STARTS
    assert init_route.call_count == 1 and len(rec.commits) == 1


def test_chunk_errors_exhausted_raise_transient_then_resume(
    tmp_path, settings, http, router, clock, account, small_chunks
):
    video = make_video(tmp_path, SMALL_SIZE)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    put_route = router.put(url__startswith=UPLOAD_HOST).mock(return_value=httpx.Response(503))
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    assert provider.publish_step(ctx).status == "continue"
    for delay in tiktok.CHUNK_RETRY_DELAYS:
        step = provider.publish_step(ctx)
        assert (step.status, step.wait_seconds) == ("wait", delay)
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.TRANSIENT and exc.value.code == "tiktok_upload_network"
    # The final chunk was never sent: the post cannot be live, and the upload can be resumed.
    assert provider.reconcile(ctx).outcome == "not_published"
    puts = PutRecorder()
    put_route.mock(side_effect=puts)
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    final = run_until_done(provider, ctx)[-1]
    assert final.status == "published" and init_route.call_count == 1
    assert puts.chunks[0][0] == 0 and len(puts.chunks) == 6


def test_final_chunk_uncertain_asks_status_instead_of_reinit(
    tmp_path, settings, http, router, clock, account
):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    router.put(url__startswith=UPLOAD_HOST).mock(side_effect=httpx.ReadTimeout("no answer"))
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    provider.publish_step(ctx)  # init
    assert rec.last["final_chunk_started"] is False
    results = run_until_done(provider, ctx)
    assert [r.status for r in results] == ["wait"] * (len(tiktok.CHUNK_RETRY_DELAYS) + 1) + ["published"]
    assert init_route.call_count == 1
    assert any(s.get("final_chunk_started") and s.get("final_chunk_uncertain") for s in rec.saved)


def test_final_chunk_4xx_after_uncertain_attempt_checks_status(
    tmp_path, settings, http, router, clock, account
):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    router.put(url__startswith=UPLOAD_HOST).mock(
        side_effect=[httpx.ReadTimeout("lost answer"), httpx.Response(416)]
    )
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    ctx, _ = make_ctx(account, make_target(video), settings, http, clock)
    final = run_until_done(tiktok.TikTokProvider(), ctx)[-1]
    assert final.status == "published" and init_route.call_count == 1


def test_expired_upload_url_restarts_with_new_init(
    tmp_path, settings, http, router, clock, account, small_chunks
):
    video = make_video(tmp_path, SMALL_SIZE)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(
        side_effect=[
            httpx.Response(200, json=init_json(PUBLISH_ID, UPLOAD_URL)),
            httpx.Response(200, json=init_json(PUBLISH_ID_2, UPLOAD_URL_2)),
        ]
    )
    puts = PutRecorder()
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "upload_id=111" in str(request.url) and len(seen_urls) == 2:
            return httpx.Response(403)  # upload URL expired after the first chunk
        return puts(request)

    router.put(url__startswith=UPLOAD_HOST).mock(side_effect=handler)
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    results = run_until_done(tiktok.TikTokProvider(), ctx)
    assert init_route.call_count == 2 and len(rec.commits) == 2
    assert results[-1].external_id == PUBLISH_ID_2
    assert rec.last["abandoned"][0]["publish_id"] == PUBLISH_ID
    assert [c[0] for c in puts.chunks][1:] == SMALL_STARTS
    assert any(e[0] == "tiktok_upload_restart" for e in rec.events)


def test_upload_rejected_too_often_is_permanent(tmp_path, settings, http, router, clock, account):
    video = make_video(tmp_path, 3000)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    router.put(url__startswith=UPLOAD_HOST).mock(return_value=httpx.Response(403))
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    with pytest.raises(ProviderError) as exc:
        for _ in range(20):
            provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "tiktok_upload_rejected"
    assert init_route.call_count == tiktok.UPLOAD_RESTART_LIMIT + 1
    assert "publish_id" not in rec.last and rec.last["upload_restarts"] == 0


def test_file_changed_during_upload(tmp_path, settings, http, router, clock, account, small_chunks):
    video = make_video(tmp_path, SMALL_SIZE)
    _, init_route, status_route, puts, _ = mock_direct_flow(router)
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    provider.publish_step(ctx)  # init
    provider.publish_step(ctx)  # chunk 1
    video.path.write_bytes(b"truncated")
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "tiktok_file_changed"
    assert "publish_id" not in rec.last  # the incomplete upload can never go live → fresh start next time

    # Same situation after an uncertain final chunk: never give up on the publish_id, ask TikTok instead.
    state = _uploaded_state(
        phase="uploading", next_chunk=0, final_chunk_uncertain=True, upload_url=UPLOAD_URL
    )
    ctx2, _ = make_ctx(account, make_target(video), settings, http, clock, state=state)
    step = provider.publish_step(ctx2)
    assert step.status == "wait" and ctx2.state["phase"] == "processing"
    assert provider.publish_step(ctx2).status == "published"
    assert init_route.call_count == 1 and len(puts.chunks) == 1 and status_route.call_count == 1


# ---------------------------------------------------------------------------------------- polling


def _uploaded_state(**extra: Any) -> dict:
    state = {
        "phase": "processing",
        "mode": "direct",
        "publish_id": PUBLISH_ID,
        "privacy_level": "SELF_ONLY",
        "creator_username": "mimarca",
        "video_size": 3000,
        "chunk_size": 3000,
        "total_chunks": 1,
        "next_chunk": 1,
        "final_chunk_started": True,
        "uploaded_at": NOW.isoformat(),
        "polls": 0,
    }
    state.update(extra)
    return state


def test_polling_cadence_and_24h_timeout(tmp_path, settings, http, router, clock, account):
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PROCESSING_UPLOAD"))
    )
    ctx, rec = make_ctx(
        account, make_target(make_video(tmp_path, 10)), settings, http, clock, state=_uploaded_state()
    )
    provider = tiktok.TikTokProvider()
    delays = [provider.publish_step(ctx).wait_seconds for _ in range(5)]
    assert delays == [10, 20, 30, 60, 60]
    clock.advance(minutes=31)
    assert provider.publish_step(ctx).wait_seconds == tiktok.POLL_SLOW_INTERVAL
    clock.advance(hours=24)
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.AMBIGUOUS and exc.value.code == "tiktok_status_timeout"
    statuses = [e for e in rec.events if e[0] == "tiktok_status"]
    assert len(statuses) == 1  # logged once per status change


def test_status_poll_transient_errors_wait_then_succeed(tmp_path, settings, http, router, clock, account):
    router.post(tiktok.STATUS_URL).mock(
        side_effect=[
            httpx.Response(500, json=api_error("internal_error")),
            httpx.ConnectError("down"),
            httpx.Response(401, json=api_error("access_token_invalid")),
            httpx.Response(200, json=status_json("PUBLISH_COMPLETE")),
        ]
    )
    ctx, _ = make_ctx(
        account, make_target(make_video(tmp_path, 10)), settings, http, clock, state=_uploaded_state()
    )
    results = run_until_done(tiktok.TikTokProvider(), ctx)
    assert [(r.status, r.wait_seconds) for r in results] == [
        ("wait", 15),
        ("wait", 30),
        ("wait", 45),
        ("published", 0),
    ]


def test_status_poll_errors_exhausted_is_ambiguous(tmp_path, settings, http, router, clock, account):
    router.post(tiktok.STATUS_URL).mock(return_value=httpx.Response(503))
    ctx, _ = make_ctx(
        account, make_target(make_video(tmp_path, 10)), settings, http, clock, state=_uploaded_state()
    )
    provider = tiktok.TikTokProvider()
    for _ in range(tiktok.POLL_ERROR_LIMIT):
        assert provider.publish_step(ctx).status == "wait"
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.AMBIGUOUS


def test_status_invalid_publish_id_is_ambiguous(tmp_path, settings, http, router, clock, account):
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(400, json=api_error("invalid_publish_id"))
    )
    ctx, _ = make_ctx(
        account, make_target(make_video(tmp_path, 10)), settings, http, clock, state=_uploaded_state()
    )
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().publish_step(ctx)
    assert exc.value.kind == ErrorKind.AMBIGUOUS and exc.value.code == "invalid_publish_id"
    assert exc.value.detail["publish_id"] == PUBLISH_ID


# -------------------------------------------------------------------------------------- reconcile


@pytest.mark.parametrize(
    ("responses", "outcome", "external_id", "sleeps_expected"),
    [
        ([status_json("PUBLISH_COMPLETE", publicaly_available_post_id=["7301"])], "published", "7301", []),
        ([status_json("PROCESSING_UPLOAD"), status_json("PUBLISH_COMPLETE")], "published", PUBLISH_ID, [5]),
        ([status_json("FAILED", fail_reason="duration_check_failed")], "not_published", None, []),
        ([status_json("SEND_TO_USER_INBOX")], "unknown", None, []),
        ([status_json("PROCESSING_UPLOAD")] * 4, "unknown", None, [5, 10, 15]),
    ],
)
def test_reconcile_uses_saved_publish_id(
    tmp_path, settings, http, router, clock, account, sleeps, responses, outcome, external_id, sleeps_expected
):
    route = router.post(tiktok.STATUS_URL).mock(side_effect=[httpx.Response(200, json=r) for r in responses])
    ctx, rec = make_ctx(
        account, make_target(make_video(tmp_path, 10)), settings, http, clock, state=_uploaded_state()
    )
    result = tiktok.TikTokProvider().reconcile(ctx)
    assert result.outcome == outcome and result.external_id == external_id
    assert result.message
    assert sleeps == sleeps_expected
    assert json.loads(route.calls.last.request.content) == {"publish_id": PUBLISH_ID}
    if outcome == "published" and external_id == "7301":
        assert result.external_url == "https://www.tiktok.com/@mimarca/video/7301"
    if outcome == "not_published":
        assert "publish_id" not in rec.last and "duración" in result.message
    if responses[0]["data"]["status"] == "SEND_TO_USER_INBOX":
        assert "bandeja de entrada" in result.message and rec.last["result"] == "sent_to_app"


def test_reconcile_without_network_answers(tmp_path, settings, http, router, clock, account):
    provider = tiktok.TikTokProvider()
    target = make_target(make_video(tmp_path, 10))
    # Final chunk never started → the upload is incomplete → not published, no HTTP needed.
    ctx, _ = make_ctx(
        account, target, settings, http, clock, state=_uploaded_state(final_chunk_started=False)
    )
    assert provider.reconcile(ctx).outcome == "not_published"
    # Already done.
    done = _uploaded_state(phase="done", result="published", external_id="7301", external_url="u")
    ctx, _ = make_ctx(account, target, settings, http, clock, state=done)
    assert (provider.reconcile(ctx).outcome, provider.reconcile(ctx).external_id) == ("published", "7301")
    # No token → cannot check.
    ctx, _ = make_ctx(
        replace(account, access_token=None), target, settings, http, clock, state=_uploaded_state()
    )
    assert provider.reconcile(ctx).outcome == "unknown"
    assert not router.calls


def test_reconcile_status_error_is_unknown(tmp_path, settings, http, router, clock, account):
    router.post(tiktok.STATUS_URL).mock(return_value=httpx.Response(500))
    ctx, _ = make_ctx(
        account, make_target(make_video(tmp_path, 10)), settings, http, clock, state=_uploaded_state()
    )
    result = tiktok.TikTokProvider().reconcile(ctx)
    assert result.outcome == "unknown" and "TikTok" in result.message


# -------------------------------------------------------------------------------------- analytics


def test_account_snapshot(settings, http, router, account):
    route = router.get(tiktok.USER_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json=ok(
                {
                    "user": {
                        "open_id": "open-id-123",
                        "follower_count": 1520,
                        "following_count": 33,
                        "likes_count": 98765,
                        "video_count": 41,
                    }
                }
            ),
        )
    )
    snap = tiktok.TikTokProvider().fetch_account_snapshot(account, settings, http, date(2026, 11, 3))
    assert snap.day == date(2026, 11, 3)
    assert snap.metrics == {"followers": 1520, "following": 33, "likes_total": 98765, "video_count": 41}
    assert snap.notice is None
    fields = route.calls.last.request.url.params["fields"].split(",")
    assert {"follower_count", "following_count", "likes_count", "video_count"} <= set(fields)


def test_account_snapshot_without_stats_scope(settings, http, router, account):
    router.get(tiktok.USER_INFO_URL).mock(
        return_value=httpx.Response(401, json=api_error("scope_not_authorized"))
    )
    provider = tiktok.TikTokProvider()
    snap = provider.fetch_account_snapshot(account, settings, http, date(2026, 11, 3))
    assert snap.metrics == {} and "user.info.stats" in snap.notice
    # Scopes recorded on the account: no request at all.
    calls = len(router.calls)
    known = replace(account, meta={"scopes": ["user.info.basic", "video.publish", "video.upload"]})
    snap = provider.fetch_account_snapshot(known, settings, http, date(2026, 11, 3))
    assert snap.metrics == {} and snap.notice and len(router.calls) == calls


def test_account_snapshot_dead_token_is_auth(settings, http, router, account):
    router.get(tiktok.USER_INFO_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    with pytest.raises(ProviderError) as exc:
        tiktok.TikTokProvider().fetch_account_snapshot(account, settings, http, date(2026, 11, 3))
    assert exc.value.kind == ErrorKind.AUTH


def _video(vid: str, created: datetime, views: int = 1000, **extra: Any) -> dict:
    data = {
        "id": vid,
        "create_time": int(created.timestamp()),
        "cover_image_url": f"https://p16.example/{vid}.jpeg?x-expires=1",
        "share_url": f"https://www.tiktok.com/@mimarca/video/{vid}?utm_source=ck_test_key",
        "video_description": f"Vídeo {vid}",
        "view_count": views,
        "like_count": 80,
        "comment_count": 15,
        "share_count": 5,
    }
    data.update(extra)
    return data


def test_recent_posts_paginates_until_since(settings, http, router, account):
    since = NOW - timedelta(days=30)
    route = router.post(tiktok.VIDEO_LIST_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=ok(
                    {
                        "videos": [
                            _video("7303", NOW - timedelta(days=1)),
                            _video("7302", NOW - timedelta(days=5)),
                        ],
                        "cursor": 1761000000000,
                        "has_more": True,
                    }
                ),
            ),
            httpx.Response(
                200,
                json=ok(
                    {
                        "videos": [
                            _video("7301", NOW - timedelta(days=20), views=0),
                            _video("7300", NOW - timedelta(days=40)),
                        ],
                        "cursor": 1750000000000,
                        "has_more": True,
                    }
                ),
            ),
        ]
    )
    posts = tiktok.TikTokProvider().fetch_recent_posts(account, settings, http, since)
    assert [p.external_id for p in posts] == ["7303", "7302", "7301"]
    assert route.call_count == 2  # stopped: page 2 reached videos older than `since`
    first = posts[0]
    assert first.published_at == datetime.fromtimestamp(int((NOW - timedelta(days=1)).timestamp()), UTC)
    assert first.permalink == "https://www.tiktok.com/@mimarca/video/7303"  # query (client key) stripped
    assert first.caption == "Vídeo 7303" and first.media_type == "VIDEO"
    assert first.metrics == {
        "views": 1000,
        "likes": 80,
        "comments": 15,
        "shares": 5,
        "interactions": 100,
        "engagement": 10.0,
    }
    assert posts[2].metrics["engagement"] == 0.0
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies == [{"max_count": 20}, {"max_count": 20, "cursor": 1761000000000}]
    fields = route.calls.last.request.url.params["fields"].split(",")
    assert {"id", "create_time", "view_count", "like_count", "comment_count", "share_count"} <= set(fields)


def test_recent_posts_without_scope_or_with_expired_cursor(settings, http, router, account):
    provider = tiktok.TikTokProvider()
    no_scope = replace(account, meta={"granted_scopes": "user.info.basic,video.publish"})
    posts = provider.fetch_recent_posts(no_scope, settings, http, NOW - timedelta(days=30))
    assert posts == [] and "video.list" in posts.notice
    assert not router.calls

    route = router.post(tiktok.VIDEO_LIST_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=ok({"videos": [_video("7303", NOW - timedelta(days=1))], "cursor": 5, "has_more": True}),
            ),
            httpx.Response(400, json=api_error("invalid_params")),
        ]
    )
    posts = provider.fetch_recent_posts(
        account, settings, http, (NOW - timedelta(days=30)).replace(tzinfo=None)
    )
    assert [p.external_id for p in posts] == ["7303"] and route.call_count == 2

    route.mock(side_effect=[httpx.Response(403, json=api_error("scope_not_authorized"))])
    posts = provider.fetch_recent_posts(account, settings, http, NOW - timedelta(days=30))
    assert posts == [] and posts.notice


# ------------------------------------------------------------------------------------------ misc


def test_provider_registration_and_flags():
    provider = registry.get_provider("tiktok")
    assert isinstance(provider, tiktok.TikTokProvider)
    assert provider.supports_comments is False and provider.supports_analytics is True
    assert [f.key for f in provider.formats] == ["video", "photo"]
    assert {o.key for o in provider.option_specs} >= {
        "mode",
        "privacy_level",
        "disable_comment",
        "disable_duet",
        "disable_stitch",
        "is_aigc",
        "brand_organic_toggle",
        "brand_content_toggle",
        "video_cover_timestamp_ms",
        "consent",
    }


def test_classify_tiktok_error_fallbacks():
    err = tiktok.classify_tiktok_error(502, None, op="status_fetch")
    assert err.kind == ErrorKind.TRANSIENT and err.code == "http_502"
    err = tiktok.classify_tiktok_error(429, {}, {"retry-after": "12"})
    assert err.kind == ErrorKind.RATE_LIMITED and err.retry_after == 12
    err = tiktok.classify_tiktok_error(
        401, {}, op="video_init"
    )  # bare 401 on a posting endpoint ≠ dead token
    assert err.kind == ErrorKind.PERMANENT
    err = tiktok.classify_tiktok_error(401, {}, op="user_info")
    assert err.kind == ErrorKind.AUTH
    err = tiktok.classify_tiktok_error(200, {"data": {}}, op="video_init")
    assert err.kind == ErrorKind.TRANSIENT


def test_publish_mode_and_urls():
    assert tiktok.publish_mode({"mode": "inbox"}) == "inbox"
    assert tiktok.publish_mode({"mode": "INBOX"}) == "inbox"
    assert tiktok.publish_mode({}) == "direct" and tiktok.publish_mode(None) == "direct"
    assert tiktok.post_url("@mimarca", "1") == "https://www.tiktok.com/@mimarca/video/1"
    assert tiktok.post_url(None, "1") is None and tiktok.profile_url(None) is None


def test_forward_compatible_account_fields(tmp_path, settings, http, router, clock, account):
    """If AccountCreds gains ``scopes`` / ``username`` (requested from the integrator), they are honoured."""
    acct = replace(account)
    acct.scopes = ["user.info.basic", "video.publish", "video.upload"]  # type: ignore[attr-defined]
    acct.username = "otra_cuenta"  # type: ignore[attr-defined]
    snap = tiktok.TikTokProvider().fetch_account_snapshot(acct, settings, http, date(2026, 11, 3))
    assert snap.metrics == {} and snap.notice
    assert not router.calls
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    state = _uploaded_state(creator_username=None)
    ctx, _ = make_ctx(acct, make_target(make_video(tmp_path, 10)), settings, http, clock, state=state)
    step = tiktok.TikTokProvider().publish_step(ctx)
    assert step.status == "published" and step.external_url == "https://www.tiktok.com/@otra_cuenta"


# ------------------------------------------------------------------- review regressions (tiktok-conformance)


class _Crash(BaseException):
    """The app dies in the middle of a request."""


def test_resent_final_chunk_4xx_checks_status_instead_of_reinit(
    tmp_path, settings, http, router, clock, account, small_chunks
):
    """The app stopped during the final-chunk PUT (TikTok may hold every byte). Re-sending it after a restart
    and getting a 4xx (upload already complete / expired URL) must ask status/fetch, never re-init."""
    video = make_video(tmp_path, SMALL_SIZE)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    sent = {"final": 0}

    def put(request: httpx.Request) -> httpx.Response:
        first, last, total = parse_range(request.headers["Content-Range"])
        if last == total - 1:
            sent["final"] += 1
            if sent["final"] == 1:
                request.read()
                raise _Crash()
            return httpx.Response(400)
        return httpx.Response(206)

    router.put(url__startswith=UPLOAD_HOST).mock(side_effect=put)
    status_route = router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    with pytest.raises(_Crash):
        for _ in range(20):
            provider.publish_step(ctx)
    saved = copy.deepcopy(rec.last)
    assert saved["final_chunk_started"] is True and not saved.get("final_chunk_uncertain")

    ctx2, rec2 = make_ctx(account, make_target(video), settings, http, clock, state=saved)
    final = run_until_done(tiktok.TikTokProvider(), ctx2)[-1]
    assert final.status == "published" and final.external_id == PUBLISH_ID
    assert init_route.call_count == 1 and status_route.called
    assert not rec2.last.get("abandoned")
    assert not any(e[0] == "tiktok_upload_restart" for e in rec2.events)


def test_reconcile_not_published_mid_upload_reasserts_commit_before_next_chunk(
    tmp_path, settings, http, router, clock, account, small_chunks
):
    """The engine resets the commit phase on "not_published"; the provider re-asserts it before resuming the
    upload of the same publish_id, so a later crash leads to a reconcile instead of a blind resume."""
    video = make_video(tmp_path, SMALL_SIZE)
    router.post(tiktok.CREATOR_INFO_URL).mock(return_value=httpx.Response(200, json=creator_json()))
    init_route = router.post(tiktok.VIDEO_INIT_URL).mock(return_value=httpx.Response(200, json=init_json()))
    put_route = router.put(url__startswith=UPLOAD_HOST).mock(return_value=httpx.Response(503))
    router.post(tiktok.STATUS_URL).mock(
        return_value=httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))
    )
    ctx, rec = make_ctx(account, make_target(video), settings, http, clock)
    provider = tiktok.TikTokProvider()
    provider.publish_step(ctx)  # init (first before_commit)
    for _ in tiktok.CHUNK_RETRY_DELAYS:
        provider.publish_step(ctx)
    with pytest.raises(ProviderError):
        provider.publish_step(ctx)
    assert provider.reconcile(ctx).outcome == "not_published"
    assert ctx.state["reassert_commit"] is True and len(rec.commits) == 1
    put_route.mock(side_effect=PutRecorder())
    assert provider.publish_step(ctx).status == "continue"
    assert len(rec.commits) == 2 and rec.commits[-1]["publish_id"] == PUBLISH_ID
    assert "reassert_commit" not in rec.commits[-1] and "reassert_commit" not in ctx.state
    final = run_until_done(provider, ctx)[-1]
    assert final.status == "published" and init_route.call_count == 1 and len(rec.commits) == 2


def test_public_post_without_id_is_resolved_by_the_follow_up(
    tmp_path, settings, http, router, clock, account
):
    """Moderation longer than the short in-run waits: the target finishes with the publish_id, then the
    follow-up job keeps asking status/fetch (hourly for 24 h, then daily up to 30 days)."""
    video = make_video(tmp_path, 2000)
    target = make_target(video, privacy_level="PUBLIC_TO_EVERYONE")
    _, _, status_route, _, _ = mock_direct_flow(
        router,
        creator=creator_json(("PUBLIC_TO_EVERYONE", "SELF_ONLY")),
        statuses=[status_json("PUBLISH_COMPLETE")] * (tiktok.PUBLIC_ID_WAITS + 1),
    )
    ctx, rec = make_ctx(account, target, settings, http, clock)
    provider = tiktok.TikTokProvider()
    for _ in range(20):
        result = provider.publish_step(ctx)
        if result.status == "published":
            break
        clock.advance(seconds=result.wait_seconds)
    assert result.status == "published" and result.external_id == PUBLISH_ID
    followup = ctx.state["followup"]
    assert followup["kind"] == "public_id"
    assert tiktok._parse_iso(followup["next_at"]) == clock.now() + timedelta(hours=1)

    status_route.side_effect = [httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))]
    clock.advance(hours=1)
    pending = provider.follow_up(ctx)
    assert pending is not None and pending.next_at == clock.now() + timedelta(hours=1)
    clock.advance(hours=24)
    status_route.side_effect = [httpx.Response(200, json=status_json("PUBLISH_COMPLETE"))]
    later = provider.follow_up(ctx)
    assert later is not None and later.next_at == clock.now() + timedelta(days=1)  # daily after 24 h

    status_route.side_effect = [
        httpx.Response(
            200, json=status_json("PUBLISH_COMPLETE", publicaly_available_post_id=[7300000000000000999])
        )
    ]
    done = provider.follow_up(ctx)
    assert done is not None and done.next_at is None
    assert done.external_id == "7300000000000000999"
    assert done.external_url == "https://www.tiktok.com/@mimarca/video/7300000000000000999"
    assert ctx.state["public_post_id"] == "7300000000000000999"
    assert provider.follow_up(ctx) is None  # nothing left to check


def test_public_id_follow_up_gives_up_after_30_days(tmp_path, settings, http, router, clock, account):
    state = {
        "phase": "done",
        "result": "published",
        "publish_id": PUBLISH_ID,
        "finished_at": (NOW - timedelta(days=31)).isoformat(),
        "followup": {"kind": "public_id", "next_at": NOW.isoformat()},
    }
    ctx, _ = make_ctx(account, make_target(make_video(tmp_path, 2000)), settings, http, clock, state=state)
    assert tiktok.TikTokProvider().follow_up(ctx) is None
    assert not router.calls


def test_private_posts_need_no_public_id_follow_up(tmp_path, settings, http, router, clock, account):
    mock_direct_flow(router)
    ctx, _ = make_ctx(account, make_target(make_video(tmp_path, 2000)), settings, http, clock)
    results = run_until_done(tiktok.TikTokProvider(), ctx)
    assert results[-1].status == "published" and "followup" not in ctx.state

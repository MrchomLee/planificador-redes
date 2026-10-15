"""YouTube provider tests (SPEC §5/§6/§7, report 06). Fully offline: every HTTP call is mocked with respx."""

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

from planificador.providers import registry, youtube
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
CHANNEL = "UCabcdefghijklmnopqrstuv"
UPLOADS = "UUabcdefghijklmnopqrstuv"
ACCESS = "ya29.SECRETACCESSTOKEN0123456789"
ACCESS_2 = "ya29.NEWACCESSTOKEN9876543210"
REFRESH = "1//SECRETREFRESHTOKEN0123456789"
VIDEO_ID = "dQw4w9WgXcQ"
UPLOAD_HOST = "www.googleapis.com"
UPLOAD_PATH = "/upload/youtube/v3/videos"
KIB256 = 256 * 1024
TOTAL = 700_000  # 3 chunks of 256 KiB: 262144 + 262144 + 175712
API = youtube.API_BASE


# ------------------------------------------------------------------------------------------- helpers


def gerr(code: int, reason: str, message: str = "boom", status: str | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "status": status,
            "errors": [{"reason": reason, "domain": "youtube.api", "message": message}],
        }
    }


def video_resource(privacy: str = "public", upload_status: str = "uploaded", **status: Any) -> dict:
    return {
        "kind": "youtube#video",
        "id": VIDEO_ID,
        "snippet": {"title": "Mi vídeo", "channelId": CHANNEL},
        "status": {"privacyStatus": privacy, "uploadStatus": upload_status, **status},
    }


def parse_range(header: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", header)
    assert m, header
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def qs(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


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

    def event_names(self) -> list[str]:
        return [e[0] for e in self.events]


class FakeSession:
    """One resumable upload session, answering like Google (308 + Range / 201 + video resource)."""

    def __init__(self, fake: FakeYouTube, upload_id: str, total: int) -> None:
        self.fake = fake
        self.upload_id = upload_id
        self.total = total
        self.received = 0
        self.chunks: list[tuple[int, int, int]] = []  # (first, last, body length)
        self.put_requests: list[httpx.Request] = []
        self.queries = 0
        self.gone = False
        self.put_script: list[Any] = []  # overrides for the next data PUTs
        self.query_script: list[Any] = []  # overrides for the next status queries

    @property
    def uri(self) -> str:
        return f"https://{UPLOAD_HOST}{UPLOAD_PATH}?uploadType=resumable&upload_id={self.upload_id}"

    def _status_response(self) -> httpx.Response:
        if self.received >= self.total:
            return httpx.Response(201, json=self.fake.video)
        headers = {"Range": f"bytes=0-{self.received - 1}"} if self.received else {}
        return httpx.Response(308, headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        content_range = request.headers["Content-Range"]
        if content_range.startswith("bytes */"):
            self.queries += 1
            assert request.headers["Content-Length"] == "0"
            if self.query_script:
                action = self.query_script.pop(0)
                if isinstance(action, Exception):
                    raise action
                return action
            if self.gone:
                return httpx.Response(404)
            return self._status_response()
        if self.put_script:
            action = self.put_script.pop(0)
            if isinstance(action, Exception):
                raise action
            if callable(action):
                return action(request, self)
            return action
        return self.accept(request)

    def accept(self, request: httpx.Request, limit: int | None = None) -> httpx.Response:
        if self.gone:
            return httpx.Response(404)
        first, last, total = parse_range(request.headers["Content-Range"])
        body = request.content
        assert total == self.total
        assert len(body) == last - first + 1 == int(request.headers["Content-Length"])
        assert "transfer-encoding" not in request.headers
        assert first == self.received, (first, self.received)
        self.chunks.append((first, last, len(body)))
        self.put_requests.append(request)
        self.received = first + (len(body) if limit is None else limit)
        return self._status_response()


class FakeYouTube:
    """respx routes for the upload endpoints + videos.list processing answers."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.sessions: list[FakeSession] = []
        self.init_requests: list[httpx.Request] = []
        self.init_script: list[httpx.Response] = []
        self.video = video_resource()
        self.processing: list[dict] = [
            video_resource(upload_status="uploaded", processingDetails=None),
            video_resource(upload_status="processed"),
        ]
        self.videos_calls: list[httpx.Request] = []
        router.route(method="POST", host=UPLOAD_HOST, path=UPLOAD_PATH).mock(side_effect=self._init)
        router.route(method="PUT", host=UPLOAD_HOST, path=UPLOAD_PATH).mock(side_effect=self._put)
        self.videos_route = router.get(f"{API}/videos").mock(side_effect=self._videos)

    @property
    def session(self) -> FakeSession:
        return self.sessions[-1]

    def _init(self, request: httpx.Request) -> httpx.Response:
        self.init_requests.append(request)
        if self.init_script:
            return self.init_script.pop(0)
        total = int(request.headers["X-Upload-Content-Length"])
        session = FakeSession(self, f"S{len(self.sessions) + 1}", total)
        self.sessions.append(session)
        return httpx.Response(200, headers={"Location": session.uri})

    def _put(self, request: httpx.Request) -> httpx.Response:
        upload_id = qs(request)["upload_id"]
        for session in self.sessions:
            if session.upload_id == upload_id:
                return session(request)
        return httpx.Response(404)

    def _videos(self, request: httpx.Request) -> httpx.Response:
        self.videos_calls.append(request)
        if not self.processing:
            return httpx.Response(200, json={"items": []})
        item = self.processing.pop(0) if len(self.processing) > 1 else self.processing[0]
        return httpx.Response(200, json={"items": [item] if item else []})

    def init_json(self, index: int = -1) -> dict:
        return json.loads(self.init_requests[index].content)


@pytest.fixture
def settings() -> ProviderSettings:
    return ProviderSettings(
        meta_app_id=None,
        meta_app_secret=None,
        meta_api_version="v25.0",
        tiktok_client_key=None,
        tiktok_client_secret=None,
        tiktok_pkce_encoding="hex",
        base_url="http://localhost:8765",
        demo_mode=False,
        google_client_id="123-abc.apps.googleusercontent.com",
        google_client_secret="GOCSPX-SECRETVALUE",
    )


@pytest.fixture
def clk() -> ManualClock:
    return ManualClock(NOW)


def make_account(**overrides: Any) -> AccountCreds:
    acc = AccountCreds(
        id=7,
        network="youtube",
        external_id=CHANNEL,
        name="Mi Canal",
        account_type="channel",
        access_token=ACCESS,
        refresh_token=REFRESH,
        token_expires_at=NOW + timedelta(hours=1),
        refresh_expires_at=None,
        meta={"uploads_playlist_id": UPLOADS, "granted_scopes": list(youtube.SCOPES_YT)},
    )
    return replace(acc, **overrides)


@pytest.fixture
def account() -> AccountCreds:
    return make_account()


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as r:
        yield r


@pytest.fixture
def http(router: respx.MockRouter) -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


@pytest.fixture
def fake(router: respx.MockRouter) -> FakeYouTube:
    return FakeYouTube(router)


@pytest.fixture
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "CHUNK_SIZE", KIB256)
    monkeypatch.setattr(youtube, "CHUNKS_PER_STEP", 2)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(youtube, "_sleep", lambda s: calls.append(s))
    return calls


def write_video(tmp_path: Path, size: int = TOTAL, name: str = "clip.mp4") -> MediaInfo:
    path = tmp_path / name
    path.write_bytes((bytes(range(256)) * (size // 256 + 1))[:size])
    return MediaInfo(
        id=11,
        kind="video",
        path=path,
        mime="video/mp4",
        size_bytes=size,
        width=1920,
        height=1080,
        duration_s=42.0,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        bitrate=None,
        is_hdr=False,
        original_name=name,
    )


def make_target(media: list[MediaInfo], fmt: str = "video", **options: Any) -> TargetSpec:
    opts = {
        "title": "Mi vídeo",
        "privacy_status": "public",
        "made_for_kids": False,
    }
    opts.update(options)
    return TargetSpec(
        network="youtube",
        format=fmt,
        text="Descripción del vídeo\nCon dos líneas",
        media=media,
        options=opts,
        scheduled_at=NOW,
        auto_publish=True,
    )


def make_ctx(
    account: AccountCreds,
    target: TargetSpec,
    settings: ProviderSettings,
    http: httpx.Client,
    clock: ManualClock,
    rec: Recorder,
    state: dict | None = None,
) -> PublishContext:
    return PublishContext(
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


def run(
    provider: youtube.YouTubeProvider,
    ctx: PublishContext,
    clock: ManualClock,
    *,
    until: Callable[[StepResult], bool] = lambda r: r.status == "published",
    max_steps: int = 40,
) -> list[StepResult]:
    results: list[StepResult] = []
    for _ in range(max_steps):
        result = provider.publish_step(ctx)
        results.append(result)
        if until(result):
            return results
        if result.status == "wait":
            clock.advance(seconds=result.wait_seconds)
    raise AssertionError(f"did not finish: {[r.status for r in results]}")


# --------------------------------------------------------------------------------------------- OAuth


def test_registry_returns_youtube_provider() -> None:
    provider = registry.get_provider("youtube")
    assert isinstance(provider, youtube.YouTubeProvider)
    assert [f.key for f in provider.formats] == ["video", "short"]
    assert provider.supports_comments and provider.supports_analytics


def test_redirect_uri_uses_localhost(settings: ProviderSettings) -> None:
    assert youtube.redirect_uri(settings) == "http://localhost:8765/conexiones/callback/google"
    assert youtube.redirect_uri(replace(settings, base_url="http://localhost:9000/")) == (
        "http://localhost:9000/conexiones/callback/google"
    )


def test_new_pkce_pair_is_s256_base64url_without_padding() -> None:
    verifier, challenge = youtube.new_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    assert "=" not in challenge and len(challenge) == 43
    assert youtube.new_pkce_pair()[0] != verifier


def test_build_login_url(settings: ProviderSettings) -> None:
    url = youtube.build_login_url(settings, youtube.redirect_uri(settings), "st4te", "chall3nge")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == youtube.AUTH_URL
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert params["client_id"] == settings.google_client_id
    assert params["redirect_uri"] == "http://localhost:8765/conexiones/callback/google"
    assert params["response_type"] == "code"
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent select_account"
    assert params["state"] == "st4te"
    assert params["code_challenge"] == "chall3nge"
    assert params["code_challenge_method"] == "S256"
    assert params["scope"].split(" ") == youtube.SCOPES_YT
    assert "%20" in url and "+" not in url  # spaces encoded as %20
    assert "GOCSPX" not in url  # never the secret


def test_build_login_url_requires_client_id(settings: ProviderSettings) -> None:
    with pytest.raises(ProviderError) as exc:
        youtube.build_login_url(replace(settings, google_client_id=None), "http://x", "s", "c")
    assert exc.value.code == "google_not_configured"
    assert "Ajustes" in exc.value.message


def test_exchange_code_production(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    route = router.post(youtube.TOKEN_URL).respond(
        200,
        json={
            "access_token": ACCESS,
            "expires_in": 3599,
            "refresh_token": REFRESH,
            "scope": " ".join(youtube.SCOPES_YT),
            "token_type": "Bearer",
        },
    )
    tokens = youtube.exchange_code(
        settings, http, "4/CODE", youtube.redirect_uri(settings), "verifier-123", clock=clk
    )
    form = parse_qs(route.calls.last.request.content.decode())
    assert form == {
        "code": ["4/CODE"],
        "client_id": [settings.google_client_id],
        "client_secret": [settings.google_client_secret],
        "redirect_uri": ["http://localhost:8765/conexiones/callback/google"],
        "grant_type": ["authorization_code"],
        "code_verifier": ["verifier-123"],
    }
    assert tokens.access_token == ACCESS
    assert tokens.refresh_token == REFRESH
    assert tokens.expires_at == NOW + timedelta(seconds=3599)
    assert tokens.refresh_expires_at is None
    assert not tokens.is_testing and tokens.warning is None
    assert tokens.scopes == youtube.SCOPES_YT


def test_exchange_code_detects_testing_project(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    router.post(youtube.TOKEN_URL).respond(
        200,
        json={
            "access_token": ACCESS,
            "expires_in": 3599,
            "refresh_token": REFRESH,
            "refresh_token_expires_in": 604799,
            "scope": youtube.SCOPE_UPLOAD,
        },
    )
    tokens = youtube.exchange_code(settings, http, "c", "http://localhost:8765/x", "v", clock=clk)
    assert tokens.is_testing
    assert tokens.refresh_expires_at == NOW + timedelta(seconds=604799)
    assert "7 días" in (tokens.warning or "")
    # granular consent: only the upload scope was granted
    assert youtube.missing_features(tokens.scopes) == ["read", "playlist", "comments", "analytics"]
    assert len(youtube.scope_warnings(tokens.scopes)) == 4
    meta = youtube.account_meta_from_channel(youtube.YouTubeChannel(id=CHANNEL, title="X"), tokens)
    assert meta["client_status_hint"] == "testing"
    assert meta["youtube_publish_mode"] == "assisted" and meta["youtube_lock_status"] == "unknown"


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (
            400,
            {"error": "invalid_grant", "error_description": "Bad Request"},
            ErrorKind.PERMANENT,
            "google_invalid_grant",
        ),
        (
            401,
            {"error": "invalid_client", "error_description": "Unauthorized"},
            ErrorKind.AUTH,
            "google_invalid_client",
        ),
        (400, {"error": "redirect_uri_mismatch"}, ErrorKind.PERMANENT, "google_redirect_uri_mismatch"),
        (503, {"error": "temporarily_unavailable"}, ErrorKind.TRANSIENT, "google_server_error"),
    ],
)
def test_exchange_code_errors(
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    status: int,
    body: dict,
    kind: ErrorKind,
    code: str,
) -> None:
    router.post(youtube.TOKEN_URL).respond(status, json=body)
    with pytest.raises(ProviderError) as exc:
        youtube.exchange_code(settings, http, "c", youtube.redirect_uri(settings), "v")
    assert exc.value.kind == kind
    assert exc.value.code == code
    if code == "google_redirect_uri_mismatch":
        assert "http://localhost:8765/conexiones/callback/google" in exc.value.message


def test_exchange_code_requires_client_secret(http: httpx.Client, settings: ProviderSettings) -> None:
    with pytest.raises(ProviderError) as exc:
        youtube.exchange_code(replace(settings, google_client_secret=None), http, "c", "u", "v")
    assert exc.value.code == "google_not_configured"


def test_list_my_channels(router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings) -> None:
    route = router.get(f"{API}/channels").respond(
        200,
        json={
            "items": [
                {
                    "id": CHANNEL,
                    "snippet": {
                        "title": "Mi Canal",
                        "customUrl": "@micanal",
                        "country": "ES",
                        "defaultLanguage": "es",
                        "thumbnails": {
                            "default": {"url": "https://yt3.example/d.jpg"},
                            "medium": {"url": "https://yt3.example/m.jpg"},
                        },
                    },
                    "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS}},
                    "statistics": {"subscriberCount": "1230", "videoCount": "12", "viewCount": "45678"},
                    "status": {"longUploadsStatus": "allowed", "privacyStatus": "public"},
                }
            ]
        },
    )
    channels = youtube.list_my_channels(settings, http, ACCESS)
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {ACCESS}"
    assert qs(request)["mine"] == "true"
    assert qs(request)["part"] == "snippet,contentDetails,statistics,status"
    ch = channels[0]
    assert (ch.id, ch.title, ch.custom_url, ch.handle) == (CHANNEL, "Mi Canal", "@micanal", "micanal")
    assert ch.thumbnail_url == "https://yt3.example/m.jpg"
    assert ch.uploads_playlist_id == UPLOADS
    assert ch.long_uploads_status == "allowed"
    assert (ch.subscriber_count, ch.video_count, ch.view_count) == (1230, 12, 45678)
    meta = ch.to_meta()
    assert meta["uploads_playlist_id"] == UPLOADS and meta["channel_id"] == CHANNEL


def test_list_my_channels_without_channel(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    router.get(f"{API}/channels").respond(200, json={"items": []})
    with pytest.raises(ProviderError) as exc:
        youtube.list_my_channels(settings, http, ACCESS)
    assert exc.value.code == "youtube_no_channel"
    assert "no tiene canal" in exc.value.message


def test_revoke_best_effort(router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings) -> None:
    route = router.post(youtube.REVOKE_URL).mock(
        side_effect=[
            httpx.Response(200),
            httpx.Response(400, json={"error": "invalid_token"}),
            httpx.Response(500),
            httpx.Response(500),
        ]
    )
    youtube.revoke(settings, http, REFRESH)
    assert parse_qs(route.calls.last.request.content.decode()) == {"token": [REFRESH]}
    youtube.revoke(settings, http, REFRESH)  # already revoked → fine
    youtube.revoke(settings, http, REFRESH)  # swallowed
    with pytest.raises(ProviderError):
        youtube.revoke(settings, http, REFRESH, strict=True)
    youtube.revoke(settings, http, "")  # nothing to do
    assert route.call_count == 4


# ---------------------------------------------------------------------------------------- credentials


def test_refresh_credentials_not_needed(
    http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    provider = youtube.YouTubeProvider()
    acc = make_account(token_expires_at=NOW + timedelta(minutes=6))
    assert provider.refresh_credentials(acc, settings, http, clk) is None


def test_refresh_credentials_when_less_than_5_minutes(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    route = router.post(youtube.TOKEN_URL).respond(
        200, json={"access_token": ACCESS_2, "expires_in": 3599, "scope": " ".join(youtube.SCOPES_YT)}
    )
    provider = youtube.YouTubeProvider()
    acc = make_account(token_expires_at=NOW + timedelta(minutes=4))
    update = provider.refresh_credentials(acc, settings, http, clk)
    assert update is not None
    assert update.access_token == ACCESS_2
    assert update.token_expires_at == NOW + timedelta(seconds=3599)
    assert update.refresh_token is None  # Google does not rotate it: keep the old one
    assert update.scopes == youtube.SCOPES_YT
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == [REFRESH]
    assert form["client_secret"] == [settings.google_client_secret]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (
            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
            "Vuelve a conectar",
        ),
        ({"error": "invalid_grant", "error_subtype": "invalid_rapt"}, "Workspace"),
    ],
)
def test_refresh_credentials_invalid_grant_is_auth(
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    body: dict,
    fragment: str,
) -> None:
    router.post(youtube.TOKEN_URL).respond(400, json=body)
    acc = make_account(token_expires_at=NOW - timedelta(minutes=1))
    with pytest.raises(ProviderError) as exc:
        youtube.YouTubeProvider().refresh_credentials(acc, settings, http, clk)
    assert exc.value.kind == ErrorKind.AUTH
    assert fragment in exc.value.message
    assert REFRESH not in json.dumps(exc.value.to_dict())


def test_refresh_credentials_testing_expired_and_missing(
    http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    provider = youtube.YouTubeProvider()
    expired = make_account(token_expires_at=NOW, refresh_expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(ProviderError) as exc:
        provider.refresh_credentials(expired, settings, http, clk)
    assert exc.value.kind == ErrorKind.AUTH and "7 días" in exc.value.message
    with pytest.raises(ProviderError) as exc:
        provider.refresh_credentials(
            make_account(token_expires_at=NOW, refresh_token=None), settings, http, clk
        )
    assert exc.value.kind == ErrorKind.AUTH


def test_check_account(router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings) -> None:
    route = router.get(f"{API}/channels").mock(
        side_effect=[
            httpx.Response(200, json={"items": [{"id": CHANNEL}]}),
            httpx.Response(200, json={"items": [{"id": "UCother"}]}),
        ]
    )
    provider = youtube.YouTubeProvider()
    provider.check_account(make_account(), settings, http)
    assert qs(route.calls.last.request) == {"part": "id", "mine": "true"}
    with pytest.raises(ProviderError) as exc:
        provider.check_account(make_account(), settings, http)
    assert exc.value.kind == ErrorKind.AUTH


def test_api_401_refreshes_once_and_retries(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    token_route = router.post(youtube.TOKEN_URL).respond(
        200, json={"access_token": ACCESS_2, "expires_in": 3599}
    )
    route = router.get(f"{API}/channels").mock(
        side_effect=[
            httpx.Response(401, json=gerr(401, "authError", "Invalid Credentials", "UNAUTHENTICATED")),
            httpx.Response(200, json={"items": [{"id": CHANNEL}]}),
        ]
    )
    acc = make_account()
    youtube.YouTubeProvider().check_account(acc, settings, http)
    assert token_route.call_count == 1
    assert route.calls[-1].request.headers["Authorization"] == f"Bearer {ACCESS_2}"
    assert acc.access_token == ACCESS_2  # applied in memory


def test_api_401_without_refresh_token_is_auth(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    router.get(f"{API}/channels").respond(401, json=gerr(401, "authError", status="UNAUTHENTICATED"))
    with pytest.raises(ProviderError) as exc:
        youtube.YouTubeProvider().check_account(make_account(refresh_token=None), settings, http)
    assert exc.value.kind == ErrorKind.AUTH
    assert exc.value.code == "youtube_unauthorized"


# --------------------------------------------------------------------------------------- lists


def test_list_categories_only_assignable(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    route = router.get(f"{API}/videoCategories").respond(
        200,
        json={
            "items": [
                {"id": "1", "snippet": {"title": "Cine y animación", "assignable": True}},
                {"id": "18", "snippet": {"title": "Cortos", "assignable": False}},
                {"id": "22", "snippet": {"title": "Gente y blogs", "assignable": True}},
            ]
        },
    )
    cats = youtube.list_categories(make_account(), settings, http)
    assert cats == [("1", "Cine y animación"), ("22", "Gente y blogs")]
    assert qs(route.calls.last.request) == {"part": "snippet", "regionCode": "ES", "hl": "es"}
    youtube.list_categories(make_account(), settings, http, region="MX", hl="es-419")
    assert qs(route.calls.last.request)["regionCode"] == "MX"


def test_list_playlists_paginates(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    route = router.get(f"{API}/playlists").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"items": [{"id": "PL1", "snippet": {"title": "Recetas"}}], "nextPageToken": "P2"},
            ),
            httpx.Response(200, json={"items": [{"id": "PL2", "snippet": {"title": "Viajes"}}]}),
        ]
    )
    assert youtube.list_playlists(make_account(), settings, http) == [("PL1", "Recetas"), ("PL2", "Viajes")]
    assert qs(route.calls[0].request)["mine"] == "true"
    assert qs(route.calls[1].request)["pageToken"] == "P2"


# ------------------------------------------------------------------------------------ metadata


def test_build_upload_plan_sends_every_option(tmp_path: Path) -> None:
    target = make_target(
        [write_video(tmp_path)],
        tags=["recetas", "cocina fácil", "#vegano"],
        category_id="26",
        language="es",
        synthetic_media=True,
        paid_promotion=True,
        embeddable=False,
        license="creativeCommon",
        notify_subscribers=False,
        privacy_status="unlisted",
    )
    plan = youtube.build_upload_plan(target, now=NOW)
    assert plan.parts == ["snippet", "status", "paidProductPlacementDetails"]
    assert plan.notify_subscribers is False
    assert plan.body == {
        "snippet": {
            "title": "Mi vídeo",
            "description": "Descripción del vídeo\nCon dos líneas",
            "categoryId": "26",
            "tags": ["recetas", "cocina fácil", "vegano"],
            "defaultLanguage": "es",
            "defaultAudioLanguage": "es",
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
            "embeddable": False,
            "license": "creativeCommon",
        },
        "paidProductPlacementDetails": {"hasPaidProductPlacement": True},
    }
    assert not plan.native_scheduled and plan.publish_at is None


def test_build_upload_plan_defaults_and_title_fallback(tmp_path: Path) -> None:
    target = make_target([write_video(tmp_path)], title="", made_for_kids="si")
    target.options.pop("privacy_status")
    plan = youtube.build_upload_plan(target, now=NOW)
    assert plan.title == "Descripción del vídeo"  # first line of the text
    status = plan.body["status"]
    assert status["privacyStatus"] == "private"  # unknown lock status → private, always explicit
    assert status["selfDeclaredMadeForKids"] is True
    assert status["containsSyntheticMedia"] is False and status["embeddable"] is True
    assert status["license"] == "youtube"
    assert plan.body["snippet"]["categoryId"] == "22"
    assert plan.notify_subscribers is True
    assert youtube.privacy_for(target, {"youtube_lock_status": "unlocked"}) == "public"


def test_build_upload_plan_native_schedule(tmp_path: Path) -> None:
    target = make_target([write_video(tmp_path)], native_schedule=True)
    target.scheduled_at = NOW + timedelta(hours=2, seconds=30, microseconds=5)
    plan = youtube.build_upload_plan(target, now=NOW)
    assert plan.native_scheduled
    assert plan.requested_privacy == "public"
    assert plan.body["status"]["privacyStatus"] == "private"
    assert plan.body["status"]["publishAt"] == "2026-11-03T11:00:30Z"
    # already due → plain upload with the requested privacy
    target.scheduled_at = NOW
    plan = youtube.build_upload_plan(target, now=NOW)
    assert not plan.native_scheduled and plan.body["status"]["privacyStatus"] == "public"
    assert "publishAt" not in plan.body["status"]


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"made_for_kids": None}, "youtube_made_for_kids_required"),
        ({"title": "x" * 101}, "youtube_title_too_long"),
        ({"title": "a <b>"}, "youtube_title_chars"),
        ({"tags": ["t" * 300, "u" * 201]}, "youtube_tags_too_long"),
        ({"privacy_status": "secreto"}, "youtube_privacy"),
    ],
)
def test_build_upload_plan_rejects_invalid_metadata(tmp_path: Path, options: dict, code: str) -> None:
    target = make_target([write_video(tmp_path)], **options)
    with pytest.raises(ProviderError) as exc:
        youtube.build_upload_plan(target, now=NOW)
    assert exc.value.kind == ErrorKind.PERMANENT
    assert exc.value.code == code


def test_check_target_short_and_description(tmp_path: Path) -> None:
    video = replace(write_video(tmp_path), width=1920, height=1080, duration_s=200.0)
    target = make_target([video], fmt="short")
    target.text = "é" * 2600  # 5200 bytes
    codes = {i.code for i in youtube.check_target(target)}
    assert {"youtube_short_too_long", "youtube_short_aspect", "youtube_description_too_long"} <= codes
    long_video = replace(video, duration_s=20 * 60)
    codes = {
        i.code for i in youtube.check_target(make_target([long_video]), {"long_uploads_status": "eligible"})
    }
    assert "youtube_long_upload" in codes
    assert youtube.check_target(make_target([long_video]), {"long_uploads_status": "allowed"}) == []
    assert youtube.tags_length(["a b", "cd"]) == 3 + 2 + 2 + 1


def test_validate_falls_back_to_provider_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from planificador import rules

    def not_ready(target: TargetSpec) -> list:
        raise NotImplementedError

    monkeypatch.setattr(rules, "validate_target", not_ready)
    issues = youtube.YouTubeProvider().validate(make_target([write_video(tmp_path)], made_for_kids=None))
    assert [i.code for i in issues] == ["youtube_made_for_kids_required"]


# --------------------------------------------------------------------------------------- publishing


def test_publish_happy_path_resumable_upload(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    received_at_commit: list[int] = []
    rec.on_commit = lambda state: received_at_commit.append(fake.session.received)
    target = make_target([write_video(tmp_path)], tags=["recetas"])
    ctx = make_ctx(account, target, settings, http, clk, rec)
    provider = youtube.YouTubeProvider()

    results = run(provider, ctx, clk)

    # init: resumable session with explicit metadata
    init = fake.init_requests[0]
    params = qs(init)
    assert params == {
        "uploadType": "resumable",
        "part": "snippet,status,paidProductPlacementDetails",
        "notifySubscribers": "true",
    }
    assert init.headers["X-Upload-Content-Length"] == str(TOTAL)
    assert init.headers["X-Upload-Content-Type"] == "video/mp4"
    assert init.headers["Content-Type"].startswith("application/json")
    body = fake.init_json()
    assert body["status"]["privacyStatus"] == "public"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["containsSyntheticMedia"] is False
    assert body["snippet"]["tags"] == ["recetas"]
    # chunks: multiples of 256 KiB except the last; the server's Range drives the offsets
    assert fake.session.chunks == [(0, 262143, KIB256), (262144, 524287, KIB256), (524288, 699999, 175712)]
    for first, _last, length in fake.session.chunks[:-1]:
        assert first % KIB256 == 0 and length % KIB256 == 0
    # before_commit exactly once, right before the final chunk
    assert len(rec.commits) == 1
    assert received_at_commit == [524288]
    assert rec.commits[0]["commit_sent"] is True
    assert rec.commits[0]["session_uri"] == fake.session.uri
    # session URI persisted right after init (first save)
    assert rec.saved[0]["session_uri"] == fake.session.uri
    assert rec.saved[0]["uploaded_bytes"] == 0
    # step statuses: init, 2 chunks, final chunk, post→wait, poll→wait, poll→published
    assert [r.status for r in results] == ["continue", "continue", "continue", "wait", "wait", "published"]
    assert [r.wait_seconds for r in results if r.status == "wait"] == [30, 30]
    final = results[-1]
    assert final.external_id == VIDEO_ID
    assert final.external_url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert final.message == "Publicado en YouTube."
    assert qs(fake.videos_calls[0])["part"] == "status,processingDetails"
    assert ctx.state["phase"] == "done"
    assert ctx.state[youtube.STATE_ACCOUNT_META] == {
        "youtube_lock_status": "unlocked",
        "upload_lock_suspected": False,
    }
    assert "youtube_upload_progress" in rec.event_names()
    # re-entrant after done: same answer, no HTTP
    calls = len(router.calls)
    again = provider.publish_step(ctx)
    assert (again.status, again.external_id) == ("published", VIDEO_ID)
    assert len(router.calls) == calls


def test_upload_sends_bearer_and_streams_with_content_length(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    run(youtube.YouTubeProvider(), ctx, clk)
    for request in fake.session.put_requests:
        assert request.headers["Authorization"] == f"Bearer {ACCESS}"
        assert request.headers["Content-Type"] == "video/mp4"
        assert "transfer-encoding" not in request.headers
    data = (tmp_path / "clip.mp4").read_bytes()
    assert b"".join(r.content for r in fake.session.put_requests) == data


def test_308_without_range_restarts_from_zero_and_partial_range_resumes(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    assert provider.publish_step(ctx).status == "continue"  # init
    session = fake.session

    def nothing_stored(request: httpx.Request, s: FakeSession) -> httpx.Response:
        return httpx.Response(308)  # no Range header: nothing received

    def half_stored(request: httpx.Request, s: FakeSession) -> httpx.Response:
        return s.accept(request, limit=KIB256 // 2)  # only 128 KiB kept

    session.put_script = [nothing_stored, half_stored]
    provider.publish_step(ctx)
    # first chunk was "lost" (resent from 0), then only half of it was stored → next offset 131072
    assert ctx.state["uploaded_bytes"] == KIB256 // 2
    results = run(provider, ctx, clk)
    assert results[-1].status == "published"
    assert session.chunks[1][0] == KIB256 // 2  # resumed exactly at the server's offset
    assert session.received == TOTAL


def test_crash_resume_trusts_server_range(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    target = make_target([write_video(tmp_path)])
    ctx = make_ctx(account, target, settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)  # init
    provider.publish_step(ctx)  # 2 chunks → 524288
    saved = copy.deepcopy(rec.last)
    assert saved["uploaded_bytes"] == 2 * KIB256
    # The server actually kept less than what we recorded (e.g. last chunk lost on its side).
    fake.session.received = KIB256
    # "Crash": new process (fresh provider instance), state restored from the DB.
    rec2 = Recorder()
    ctx2 = make_ctx(make_account(), target, settings, http, clk, rec2, state=saved)
    provider2 = youtube.YouTubeProvider()
    results = run(provider2, ctx2, clk)
    assert fake.session.queries == 1  # status query first
    assert len(fake.init_requests) == 1  # never a second session
    assert fake.session.chunks[2] == (KIB256, 2 * KIB256 - 1, KIB256)  # resent from the server's offset
    assert results[-1].external_id == VIDEO_ID
    assert len(rec2.commits) == 1


def test_crash_after_before_commit_finds_completed_video(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    target = make_target([write_video(tmp_path)])
    ctx = make_ctx(account, target, settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)

    def crash_after_upload(request: httpx.Request, s: FakeSession) -> httpx.Response:
        s.accept(request)  # Google stored the last bytes and created the video…
        raise RuntimeError("process killed")  # …but we died before reading the answer

    fake.session.put_script = [crash_after_upload]
    with pytest.raises(RuntimeError):
        provider.publish_step(ctx)
    committed = copy.deepcopy(rec.commits[-1])
    assert committed["commit_sent"] is True and "video_id" not in committed

    # The engine calls reconcile first: status query → 201 → published.
    rec2 = Recorder()
    ctx2 = make_ctx(make_account(), target, settings, http, clk, rec2, state=copy.deepcopy(committed))
    result = youtube.YouTubeProvider().reconcile(ctx2)
    assert result.outcome == "published"
    assert result.external_id == VIDEO_ID
    assert ctx2.state["video_id"] == VIDEO_ID

    # Or, if publish_step is re-entered from the committed state, it never re-sends a byte.
    rec3 = Recorder()
    ctx3 = make_ctx(make_account(), target, settings, http, clk, rec3, state=copy.deepcopy(committed))
    puts_before = len(fake.session.chunks)
    results = run(youtube.YouTubeProvider(), ctx3, clk)
    assert len(fake.session.chunks) == puts_before
    assert len(fake.init_requests) == 1
    assert results[-1].external_id == VIDEO_ID


def test_session_gone_before_commit_restarts_upload(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    fake.session.gone = True
    result = provider.publish_step(ctx)
    assert result.status == "continue"
    assert "session_uri" not in ctx.state and ctx.state["restarts"] == 1
    assert "youtube_session_restarted" in rec.event_names()
    results = run(provider, ctx, clk)
    assert len(fake.init_requests) == 2
    assert results[-1].status == "published"


def test_session_gone_after_commit_is_ambiguous(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)
    fake.session.put_script = [httpx.Response(410)]
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    assert len(fake.init_requests) == 1


def test_final_chunk_timeout_but_server_completed(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)

    def accept_then_timeout(request: httpx.Request, s: FakeSession) -> httpx.Response:
        s.accept(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    fake.session.put_script = [accept_then_timeout]
    result = provider.publish_step(ctx)
    assert result.status == "continue"
    assert ctx.state["video_id"] == VIDEO_ID
    assert fake.session.queries == 1


def test_final_chunk_timeout_and_query_fails_is_ambiguous(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)
    fake.session.put_script = [httpx.ReadTimeout("read timed out")]
    fake.session.query_script = [httpx.ConnectError("no route")]
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    assert exc.value.code == "youtube_upload_ambiguous"
    assert ctx.state["commit_sent"] is True and ctx.state["resync"] is True


def test_final_chunk_5xx_server_kept_nothing_waits_and_resumes(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)
    fake.session.put_script = [httpx.Response(503, json=gerr(503, "backendError"))]
    result = provider.publish_step(ctx)
    assert (result.status, result.wait_seconds) == ("wait", 10)
    assert ctx.state["commit_sent"] is False  # server confirmed 524288 bytes: no video yet
    results = run(provider, ctx, clk)
    assert results[-1].status == "published"
    assert len(rec.commits) == 2  # commit point passed again when the final chunk was re-sent


def test_non_final_chunk_errors_wait_then_resync(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    fake.session.put_script = [httpx.ConnectError("reset"), httpx.Response(500)]
    first = provider.publish_step(ctx)
    assert (first.status, first.wait_seconds) == ("wait", 10)
    second = provider.publish_step(ctx)  # resync query (308, nothing) then the 500
    assert (second.status, second.wait_seconds) == ("wait", 30)
    assert fake.session.queries == 1
    assert "youtube_upload_retry" in rec.event_names()
    assert not rec.commits
    results = run(provider, ctx, clk)
    assert results[-1].status == "published"
    assert ctx.state["chunk_failures"] == 0


def test_non_final_chunk_errors_exhausted_raise_transient(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    fake.session.put_script = [httpx.Response(503) for _ in range(5)]
    for delay in youtube.CHUNK_RETRY_DELAYS:
        assert provider.publish_step(ctx).wait_seconds == delay
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.TRANSIENT
    assert ctx.state["chunk_failures"] == 0  # the engine's retry starts a fresh cycle on the same session


def test_chunk_401_refreshes_token_and_retries(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    token_route = router.post(youtube.TOKEN_URL).respond(
        200, json={"access_token": ACCESS_2, "expires_in": 3599}
    )
    rec = Recorder()
    persisted: list[Any] = []
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    ctx.update_tokens = persisted.append  # type: ignore[attr-defined]  # optional engine hook
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    fake.session.put_script = [httpx.Response(401, json=gerr(401, "authError", status="UNAUTHENTICATED"))]
    results = run(provider, ctx, clk)
    assert results[-1].status == "published"
    assert token_route.call_count == 1
    assert fake.session.put_requests[0].headers["Authorization"] == f"Bearer {ACCESS_2}"
    assert [u.access_token for u in persisted] == [ACCESS_2]


def test_token_refreshed_mid_upload_when_expiring(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    small_chunks: None,
) -> None:
    token_route = router.post(youtube.TOKEN_URL).respond(
        200, json={"access_token": ACCESS_2, "expires_in": 3599}
    )
    acc = make_account(token_expires_at=NOW + timedelta(minutes=10))
    rec = Recorder()
    ctx = make_ctx(acc, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    clk.advance(minutes=6)  # < 5 min left now
    provider.publish_step(ctx)
    assert token_route.call_count == 1
    assert fake.session.put_requests[-1].headers["Authorization"] == f"Bearer {ACCESS_2}"


def test_init_errors_are_mapped(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    provider = youtube.YouTubeProvider()
    cases = [
        (
            httpx.Response(
                403,
                json=gerr(
                    403,
                    "quotaExceeded",
                    "The request cannot be completed because you have exceeded your "
                    '<a href="/youtube/v3/getting-started#quota">quota</a>.',
                ),
            ),
            ErrorKind.RATE_LIMITED,
            "youtube_quota_exceeded",
        ),
        (
            httpx.Response(400, json=gerr(400, "uploadLimitExceeded")),
            ErrorKind.RATE_LIMITED,
            "youtube_uploadLimitExceeded",
        ),
        (httpx.Response(400, json=gerr(400, "invalidTitle")), ErrorKind.PERMANENT, "youtube_invalidTitle"),
        (
            httpx.Response(403, json=gerr(403, "insufficientPermissions")),
            ErrorKind.AUTH,
            "youtube_insufficient_scope",
        ),
        (httpx.Response(500, json=gerr(500, "backendError")), ErrorKind.TRANSIENT, "youtube_backendError"),
        (
            httpx.Response(401, json=gerr(401, "youtubeSignupRequired")),
            ErrorKind.PERMANENT,
            "youtube_youtubeSignupRequired",
        ),
    ]
    for response, kind, code in cases:
        fake.init_script = [response]
        rec = Recorder()
        ctx = make_ctx(
            make_account(refresh_token=None), make_target([write_video(tmp_path)]), settings, http, clk, rec
        )
        with pytest.raises(ProviderError) as exc:
            provider.publish_step(ctx)
        assert (exc.value.kind, exc.value.code) == (kind, code)
        assert not rec.commits and "session_uri" not in ctx.state
    quota = cases[0]
    fake.init_script = [quota[0]]
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(
            make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, Recorder())
        )
    # retry at the next Pacific midnight (+5 min): 2026-11-04 00:05 PST = 08:05 UTC
    assert exc.value.retry_after == int((datetime(2026, 11, 4, 8, 5, tzinfo=UTC) - NOW).total_seconds())
    assert "09:05" in exc.value.message and "subidas de vídeo" in exc.value.message
    assert "<a" not in exc.value.detail["message"]


def test_precheck_errors_do_not_call_youtube(
    tmp_path: Path, fake: FakeYouTube, http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    provider = youtube.YouTubeProvider()
    ctx = make_ctx(
        make_account(),
        make_target([write_video(tmp_path)], made_for_kids=None),
        settings,
        http,
        clk,
        Recorder(),
    )
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.code == "youtube_made_for_kids_required"
    missing = replace(write_video(tmp_path), path=tmp_path / "nope.mp4")
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(
            make_ctx(make_account(), make_target([missing]), settings, http, clk, Recorder())
        )
    assert exc.value.code == "youtube_file_missing"
    no_upload = make_account(meta={"granted_scopes": [youtube.SCOPE_READONLY]})
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(
            make_ctx(no_upload, make_target([write_video(tmp_path)]), settings, http, clk, Recorder())
        )
    assert exc.value.kind == ErrorKind.AUTH
    assert fake.init_requests == []


def test_native_schedule_returns_published_after_upload(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    fake.video = video_resource(privacy="private", publishAt="2026-11-03T18:00:00Z")
    rec = Recorder()
    target = make_target([write_video(tmp_path)], native_schedule=True, first_comment="¡Hola!")
    target.scheduled_at = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    ctx = make_ctx(account, target, settings, http, clk, rec)
    results = run(youtube.YouTubeProvider(), ctx, clk)
    status = fake.init_json()["status"]
    assert status["privacyStatus"] == "private" and status["publishAt"] == "2026-11-03T18:00:00Z"
    assert results[-1].status == "published" and results[-1].external_id == VIDEO_ID
    assert "Programado en YouTube" in (results[-1].message or "")
    assert ctx.state["native_scheduled"] is True
    assert not ctx.state.get("lock_detected")
    assert fake.videos_calls == []  # no processing poll for natively scheduled videos
    assert "youtube_first_comment_skipped" in rec.event_names()


def test_lock_detected_when_public_upload_comes_back_private(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    fake.video = video_resource(privacy="private")
    fake.processing = [video_resource(privacy="private", upload_status="processed")]
    comments = router.post(f"{API}/commentThreads").respond(200, json={"id": "Ugx1"})
    rec = Recorder()
    target = make_target([write_video(tmp_path)], first_comment="Primer comentario")
    ctx = make_ctx(account, target, settings, http, clk, rec)
    results = run(youtube.YouTubeProvider(), ctx, clk)
    final = results[-1]
    assert final.status == "published" and final.external_id == VIDEO_ID
    assert "privado" in (final.message or "") and "auditoría" in (final.message or "")
    assert ctx.state["lock_detected"] is True
    assert ctx.state[youtube.STATE_ACCOUNT_META] == {
        "youtube_lock_status": "locked",
        "upload_lock_suspected": True,
        "youtube_publish_mode": "assisted",
    }
    assert "youtube_lock_detected" in rec.event_names()
    assert comments.call_count == 0  # no first comment on a locked private video


def test_detect_lock_helper() -> None:
    now = NOW
    assert youtube.detect_lock(video_resource("private"), "public")
    assert youtube.detect_lock(video_resource("private"), "unlisted")
    assert not youtube.detect_lock(video_resource("private"), "private")
    assert not youtube.detect_lock(video_resource("public"), "public")
    assert not youtube.detect_lock(None, "public")
    scheduled = video_resource("private", publishAt="2026-11-03T08:00:00Z")
    assert not youtube.detect_lock(scheduled, "public")  # scheduled: private on purpose
    assert not youtube.detect_lock(scheduled, "public", now=now - timedelta(hours=2))
    assert youtube.detect_lock(scheduled, "public", now=now)  # 1 h after publishAt and still private
    assert youtube.lock_status({"youtube_lock_status": "locked"}) == "locked"
    assert youtube.lock_status({"youtube_lock_status": "raro"}) == "unknown"
    assert not youtube.default_auto_publish({})
    assert youtube.default_auto_publish({"youtube_lock_status": "unlocked"})
    assert youtube.default_auto_publish({"youtube_project_audited": True})
    assert not youtube.default_auto_publish(
        {"youtube_lock_status": "unlocked", "youtube_publish_mode": "assisted"}
    )


def test_post_upload_extras(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    from PIL import Image

    thumb_path = tmp_path / "miniatura.webp"
    Image.new("RGB", (2000, 1125), (200, 30, 30)).save(thumb_path, "WEBP")
    thumb = MediaInfo(
        55,
        "image",
        thumb_path,
        "image/webp",
        thumb_path.stat().st_size,
        2000,
        1125,
        None,
        None,
        None,
        None,
        None,
        False,
        "m.webp",
    )
    thumb_route = router.post(youtube.THUMBNAILS_URL).respond(
        200, json={"items": [{"default": {"url": "x"}}]}
    )
    playlist_route = router.post(f"{API}/playlistItems").respond(200, json={"id": "PLI1"})
    comment_route = router.post(f"{API}/commentThreads").respond(200, json={"id": "Ugz-first"})
    rec = Recorder()
    target = make_target(
        [write_video(tmp_path), thumb],
        thumbnail_media_id=55,
        playlist_id="PLabc",
        first_comment="¿Qué os parece?",
    )
    ctx = make_ctx(account, target, settings, http, clk, rec)
    results = run(youtube.YouTubeProvider(), ctx, clk)
    assert results[-1].status == "published"
    thumb_req = thumb_route.calls.last.request
    assert qs(thumb_req) == {"videoId": VIDEO_ID, "uploadType": "media"}
    assert thumb_req.headers["Content-Type"] == "image/jpeg"  # WebP re-encoded
    assert len(thumb_req.content) <= youtube.THUMB_MAX_BYTES
    with Image.open(__import__("io").BytesIO(thumb_req.content)) as img:
        assert max(img.size) == 1280
    assert json.loads(playlist_route.calls.last.request.content) == {
        "snippet": {"playlistId": "PLabc", "resourceId": {"kind": "youtube#video", "videoId": VIDEO_ID}}
    }
    assert json.loads(comment_route.calls.last.request.content) == {
        "snippet": {"videoId": VIDEO_ID, "topLevelComment": {"snippet": {"textOriginal": "¿Qué os parece?"}}}
    }
    st = ctx.state
    assert st["thumbnail_applied"] and st["playlist_applied"] and st["first_comment_id"] == "Ugz-first"
    assert st["thumbnail_done"] and st["playlist_done"] and st["first_comment_done"]


def test_post_upload_failures_are_warnings_and_idempotent(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    thumb_path = tmp_path / "t.jpg"
    from PIL import Image

    Image.new("RGB", (1280, 720), (0, 0, 0)).save(thumb_path, "JPEG")
    thumb = MediaInfo(
        56,
        "image",
        thumb_path,
        "image/jpeg",
        thumb_path.stat().st_size,
        1280,
        720,
        None,
        None,
        None,
        None,
        None,
        False,
        "t.jpg",
    )
    router.post(youtube.THUMBNAILS_URL).respond(
        403,
        json=gerr(
            403,
            "forbidden",
            "The authenticated user doesn't have permissions to upload and set custom video thumbnails.",
        ),
    )
    # Playlist insert already happened before a crash: the existence check finds it, no second insert.
    list_route = router.get(f"{API}/playlistItems").respond(200, json={"items": [{"id": "PLI1"}]})
    insert_route = router.post(f"{API}/playlistItems").respond(200, json={"id": "PLI2"})
    rec = Recorder()
    target = make_target([write_video(tmp_path), thumb], thumbnail_media_id=56, playlist_id="PLabc")
    state = {
        "phase": "post",
        "video_id": VIDEO_ID,
        "requested_privacy": "public",
        "returned_privacy": "public",
        "playlist_attempted": True,
    }
    ctx = make_ctx(account, target, settings, http, clk, rec, state=state)
    results = run(youtube.YouTubeProvider(), ctx, clk)
    assert results[-1].status == "published"
    assert "youtube_thumbnail_failed" in rec.event_names()
    msg = next(m for e, m, _ in rec.events if e == "youtube_thumbnail_failed")
    assert "verificado" in msg
    assert list_route.call_count == 1 and insert_route.call_count == 0
    assert qs(list_route.calls.last.request)["videoId"] == VIDEO_ID
    assert ctx.state["playlist_applied"] is True


def test_post_upload_transient_extra_waits_then_retries(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    route = router.post(f"{API}/playlistItems").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"id": "PLI1"})]
    )
    router.get(f"{API}/playlistItems").respond(200, json={"items": []})
    state = {"phase": "post", "video_id": VIDEO_ID, "requested_privacy": "public"}
    ctx = make_ctx(
        account,
        make_target([write_video(tmp_path)], playlist_id="PL1"),
        settings,
        http,
        clk,
        Recorder(),
        state=state,
    )
    provider = youtube.YouTubeProvider()
    first = provider.publish_step(ctx)
    assert (first.status, first.wait_seconds) == ("wait", youtube.EXTRA_RETRY_WAIT)
    results = run(provider, ctx, clk)
    assert results[-1].status == "published"
    assert route.call_count == 2 and ctx.state["playlist_applied"]


def test_thumbnail_resolved_from_library_db(
    tmp_path: Path,
    db: Any,
    data_dir: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    from PIL import Image

    from planificador.models import MediaAsset, MediaKind

    rel = "originals/ab/abcdef.png"
    path = data_dir / "media" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1280, 720), (10, 200, 10)).save(path, "PNG")
    asset = MediaAsset(
        kind=MediaKind.IMAGE,
        original_name="mini.png",
        rel_path=rel,
        sha256="ab" * 32,
        size_bytes=path.stat().st_size,
        mime="image/png",
        width=1280,
        height=720,
    )
    db.add(asset)
    db.commit()
    thumb_route = router.post(youtube.THUMBNAILS_URL).respond(200, json={})
    state = {"phase": "post", "video_id": VIDEO_ID, "requested_privacy": "public"}
    ctx = make_ctx(
        account,
        make_target([write_video(tmp_path)], thumbnail_media_id=asset.id),
        settings,
        http,
        clk,
        Recorder(),
        state=state,
    )
    run(youtube.YouTubeProvider(), ctx, clk)
    request = thumb_route.calls.last.request
    assert request.headers["Content-Type"] == "image/png"
    assert request.content == path.read_bytes()


def test_processing_rejected_is_permanent(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    fake.processing = [video_resource(upload_status="rejected", rejectionReason="duplicate")]
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    with pytest.raises(ProviderError) as exc:
        run(provider, ctx, clk)
    assert exc.value.kind == ErrorKind.PERMANENT
    assert exc.value.message == "YouTube lo rechazó por ser un vídeo duplicado."
    assert exc.value.detail["video_id"] == VIDEO_ID
    assert ctx.state["phase"] == "failed"
    # A manual retry starts a brand new upload instead of polling the rejected video again.
    fake.processing = [video_resource(upload_status="processed")]
    results = run(provider, ctx, clk)
    assert results[-1].status == "published"
    assert len(fake.init_requests) == 2
    assert "youtube_retry_new_upload" in rec.event_names()


def test_processing_failure_reason_message() -> None:
    err = youtube.processing_error(
        {
            "id": VIDEO_ID,
            "status": {"uploadStatus": "uploaded"},
            "processingDetails": {"processingStatus": "failed", "processingFailureReason": "transcodeFailed"},
        }
    )
    assert err.kind == ErrorKind.PERMANENT and "transcodificar" in err.message


def test_processing_timeout_publishes_with_message(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    fake.processing = [video_resource(upload_status="uploaded")]
    rec = Recorder()
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, rec)
    results = run(youtube.YouTubeProvider(), ctx, clk)
    waits = [r.wait_seconds for r in results if r.status == "wait"]
    assert waits[:4] == [30, 30, 60, 120]
    assert results[-1].status == "published"
    assert "sigue procesándolo" in (results[-1].message or "")
    assert sum(waits) >= 30 * 60


def test_processing_poll_errors_never_fail_an_uploaded_video(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    fake.videos_route.mock(side_effect=lambda request: httpx.Response(500))
    ctx = make_ctx(account, make_target([write_video(tmp_path)]), settings, http, clk, Recorder())
    results = run(youtube.YouTubeProvider(), ctx, clk)
    assert results[-1].status == "published"
    assert "no se pudo confirmar el procesamiento" in (results[-1].message or "")


# ------------------------------------------------------------------------------------------ reconcile


def _session_state(fake: FakeYouTube, total: int = TOTAL, **extra: Any) -> dict:
    session = FakeSession(fake, "R1", total)
    fake.sessions.append(session)
    state = {
        "phase": "uploading",
        "session_uri": session.uri,
        "session_started_at": NOW.isoformat(),
        "total_bytes": total,
        "uploaded_bytes": total - 1000,
        "title": "Mi vídeo",
        "commit_sent": True,
        "requested_privacy": "public",
    }
    state.update(extra)
    return state


def test_reconcile_session_complete(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    state = _session_state(fake)
    fake.session.received = TOTAL
    ctx = make_ctx(
        account,
        make_target([write_video(tmp_path)], playlist_id="PL1"),
        settings,
        http,
        clk,
        Recorder(),
        state=state,
    )
    result = youtube.YouTubeProvider().reconcile(ctx)
    assert result.outcome == "published" and result.external_id == VIDEO_ID
    assert "lista de reproducción" in (result.message or "")  # extras could not be applied


def test_reconcile_session_incomplete(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    state = _session_state(fake)
    fake.session.received = 2 * KIB256
    ctx = make_ctx(
        account, make_target([write_video(tmp_path)]), settings, http, clk, Recorder(), state=state
    )
    result = youtube.YouTubeProvider().reconcile(ctx)
    assert result.outcome == "not_published"
    assert ctx.state["uploaded_bytes"] == 2 * KIB256
    assert ctx.state["commit_sent"] is False and ctx.state["resync"] is True


def test_reconcile_session_gone_finds_video_by_title(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    state = _session_state(fake)
    fake.session.gone = True
    playlist = router.get(f"{API}/playlistItems").respond(
        200,
        json={
            "items": [
                {"contentDetails": {"videoId": "old00000000"}},
                {"contentDetails": {"videoId": VIDEO_ID}},
            ]
        },
    )
    fake.videos_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "old00000000",
                        "snippet": {"title": "Mi vídeo", "publishedAt": "2026-10-01T10:00:00Z"},
                    },
                    {"id": VIDEO_ID, "snippet": {"title": "Mi vídeo", "publishedAt": "2026-11-03T09:02:00Z"}},
                ]
            },
        )
    )
    ctx = make_ctx(
        account, make_target([write_video(tmp_path)]), settings, http, clk, Recorder(), state=state
    )
    result = youtube.YouTubeProvider().reconcile(ctx)
    assert result.outcome == "published" and result.external_id == VIDEO_ID
    assert qs(playlist.calls.last.request)["playlistId"] == UPLOADS


def test_reconcile_session_gone_not_found_is_unknown(
    tmp_path: Path,
    fake: FakeYouTube,
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    state = _session_state(fake)
    fake.session.gone = True
    router.get(f"{API}/playlistItems").respond(200, json={"items": []})
    ctx = make_ctx(
        account, make_target([write_video(tmp_path)]), settings, http, clk, Recorder(), state=state
    )
    result = youtube.YouTubeProvider().reconcile(ctx)
    assert result.outcome == "unknown"
    assert "YouTube Studio" in (result.message or "")


def test_reconcile_other_cases(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    _no_sleep: list[float],
) -> None:
    provider = youtube.YouTubeProvider()
    target = make_target([write_video(tmp_path)])
    # nothing sent
    assert (
        provider.reconcile(make_ctx(account, target, settings, http, clk, Recorder(), state={})).outcome
        == "not_published"
    )
    # video id known and present
    ctx = make_ctx(account, target, settings, http, clk, Recorder(), state={"video_id": VIDEO_ID})
    assert provider.reconcile(ctx).outcome == "published"
    # video id known but gone
    fake.processing = [None]  # type: ignore[list-item]
    assert (
        provider.reconcile(
            make_ctx(account, target, settings, http, clk, Recorder(), state={"video_id": VIDEO_ID})
        ).outcome
        == "unknown"
    )
    # status query keeps failing → unknown after one short retry
    state = _session_state(fake)
    fake.session.query_script = [httpx.ConnectError("down"), httpx.ConnectError("down")]
    result = provider.reconcile(make_ctx(account, target, settings, http, clk, Recorder(), state=state))
    assert result.outcome == "unknown"
    assert _no_sleep == [youtube.RECONCILE_RETRY_DELAY]


# ------------------------------------------------------------------------------------------ analytics


def test_fetch_account_snapshot(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    channels = router.get(f"{API}/channels").respond(
        200,
        json={
            "items": [
                {
                    "id": CHANNEL,
                    "statistics": {
                        "subscriberCount": "1230",
                        "videoCount": "12",
                        "viewCount": "45678",
                        "hiddenSubscriberCount": False,
                    },
                }
            ]
        },
    )
    analytics = router.get(youtube.ANALYTICS_URL).respond(
        200,
        json={
            "columnHeaders": [
                {"name": "day"},
                {"name": "views"},
                {"name": "estimatedMinutesWatched"},
                {"name": "likes"},
                {"name": "comments"},
                {"name": "shares"},
                {"name": "subscribersGained"},
                {"name": "subscribersLost"},
            ],
            "rows": [
                ["2026-10-31", 120, 300.5, 10, 2, 1, 4, 1],
                ["2026-11-01", 80, 150, 5, 0, 0, 2, 0],
                ["2026-11-03", 7, 9, 1, 0, 0, 0, 0],
            ],
        },
    )
    snap = youtube.YouTubeProvider().fetch_account_snapshot(make_account(), settings, http, date(2026, 11, 3))
    assert qs(channels.calls.last.request) == {"part": "statistics", "id": CHANNEL}
    params = qs(analytics.calls.last.request)
    assert params["ids"] == "channel==MINE"
    assert (params["startDate"], params["endDate"]) == ("2026-10-27", "2026-11-03")
    assert params["dimensions"] == "day" and params["sort"] == "day"
    assert (
        params["metrics"]
        == "views,estimatedMinutesWatched,likes,comments,shares,subscribersGained,subscribersLost"
    )
    assert snap.day == date(2026, 11, 3)
    assert snap.metrics == {
        "followers": 1230,
        "posts_count": 12,
        "views_total": 45678,
        "views": 7,
        "watch_minutes": 9,
        "likes": 1,
        "comments": 0,
        "shares": 0,
        "subs_gained": 0,
        "subs_lost": 0,
    }
    assert [h.day for h in snap.history] == [date(2026, 10, 31), date(2026, 11, 1)]
    assert snap.history[0].metrics["watch_minutes"] == 300.5
    assert snap.history[0].metrics["subs_lost"] == 1
    assert "engagement" not in snap.metrics and "interactions" not in snap.metrics
    assert snap.notice is None


def test_fetch_account_snapshot_analytics_disabled_and_hidden_subs(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    router.get(f"{API}/channels").respond(
        200,
        json={
            "items": [
                {
                    "id": CHANNEL,
                    "statistics": {"videoCount": "3", "viewCount": "10", "hiddenSubscriberCount": True},
                }
            ]
        },
    )
    router.get(youtube.ANALYTICS_URL).respond(
        403,
        json=gerr(
            403,
            "accessNotConfigured",
            "YouTube Analytics API has not been used in project 123 before or it is disabled.",
            "PERMISSION_DENIED",
        ),
    )
    snap = youtube.YouTubeProvider().fetch_account_snapshot(make_account(), settings, http, date(2026, 11, 3))
    assert snap.metrics == {"posts_count": 3, "views_total": 10}
    assert snap.notice and "YouTube Analytics API" in snap.notice


def test_fetch_recent_posts(router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings) -> None:
    playlist = router.get(f"{API}/playlistItems").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "contentDetails": {
                                "videoId": "vid00000001",
                                "videoPublishedAt": "2026-11-02T10:00:00Z",
                            }
                        },
                        {
                            "contentDetails": {
                                "videoId": "vid00000002",
                                "videoPublishedAt": "2026-10-20T10:00:00Z",
                            }
                        },
                    ],
                    "nextPageToken": "NEXT",
                },
            ),
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "contentDetails": {
                                "videoId": "vid00000003",
                                "videoPublishedAt": "2026-09-01T10:00:00Z",
                            }
                        }
                    ],
                    "nextPageToken": "MORE",
                },
            ),
        ]
    )
    videos = router.get(f"{API}/videos").respond(
        200,
        json={
            "items": [
                {
                    "id": "vid00000001",
                    "snippet": {
                        "title": "Receta rápida",
                        "description": "d",
                        "publishedAt": "2026-11-02T10:00:00Z",
                        "thumbnails": {"medium": {"url": "https://i.ytimg.example/1.jpg"}},
                    },
                    "statistics": {"viewCount": "1500", "likeCount": "90", "commentCount": "12"},
                    "status": {"privacyStatus": "public"},
                    "contentDetails": {"duration": "PT1M5S"},
                },
                {
                    "id": "vid00000002",
                    "snippet": {"title": "Viaje", "publishedAt": "2026-10-20T10:00:00Z"},
                    "statistics": {"viewCount": "300"},
                    "status": {"privacyStatus": "unlisted"},
                    "contentDetails": {"duration": "PT12M"},
                },
            ]
        },
    )
    analytics = router.get(youtube.ANALYTICS_URL).respond(
        200,
        json={
            "columnHeaders": [
                {"name": n}
                for n in [
                    "video",
                    "estimatedMinutesWatched",
                    "averageViewDuration",
                    "averageViewPercentage",
                    "shares",
                    "subscribersGained",
                ]
            ],
            "rows": [["vid00000001", 600, 24, 37.5, 8, 3]],
        },
    )
    since = datetime(2026, 10, 4, tzinfo=UTC)
    posts = youtube.YouTubeProvider().fetch_recent_posts(make_account(), settings, http, since)
    assert playlist.call_count == 2  # stopped at the first video older than `since`
    assert qs(playlist.calls[0].request)["playlistId"] == UPLOADS
    assert qs(videos.calls.last.request) == {
        "part": "snippet,statistics,status,contentDetails",
        "id": "vid00000001,vid00000002",
    }
    assert qs(analytics.calls.last.request)["filters"] == "video==vid00000001,vid00000002"
    assert [p.external_id for p in posts] == ["vid00000001", "vid00000002"]
    first = posts[0]
    assert first.permalink == "https://www.youtube.com/watch?v=vid00000001"
    assert first.caption == "Receta rápida" and first.thumbnail_url == "https://i.ytimg.example/1.jpg"
    assert first.metrics == {
        "views": 1500,
        "likes": 90,
        "comments": 12,
        "watch_minutes": 600,
        "avg_view_duration": 24,
        "avg_view_pct": 37.5,
        "shares": 8,
        "subs_gained": 3,
    }
    assert first.duration_s == 65 and first.privacy_status == "public"
    assert posts[1].metrics == {"views": 300}
    assert posts.notice is None


def test_fetch_recent_posts_without_meta_playlist(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    router.get(f"{API}/channels").respond(
        200,
        json={"items": [{"id": CHANNEL, "contentDetails": {"relatedPlaylists": {"uploads": "UUfromapi"}}}]},
    )
    playlist = router.get(f"{API}/playlistItems").respond(200, json={"items": []})
    posts = youtube.YouTubeProvider().fetch_recent_posts(
        make_account(meta={}), settings, http, NOW - timedelta(days=30)
    )
    assert list(posts) == []
    assert qs(playlist.calls.last.request)["playlistId"] == "UUfromapi"


# ------------------------------------------------------------------------------------------ comments


def _comment(cid: str, author: str, text: str, published: str, **extra: Any) -> dict:
    return {
        "id": cid,
        "snippet": {
            "videoId": VIDEO_ID,
            "textDisplay": text,
            "textOriginal": text,
            "authorDisplayName": author,
            "authorChannelId": {"value": "UC" + author},
            "publishedAt": published,
            **extra,
        },
    }


def test_fetch_comments(router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings) -> None:
    own_reply = _comment("Ugx1.r2", "x", "¡Gracias!", "2026-11-03T08:30:00Z", parentId="Ugx1")
    own_reply["snippet"]["authorChannelId"] = {"value": CHANNEL}
    threads = router.get(f"{API}/commentThreads").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "Ugx1",
                            "snippet": {
                                "videoId": VIDEO_ID,
                                "canReply": True,
                                "totalReplyCount": 3,
                                "isPublic": True,
                                "topLevelComment": _comment(
                                    "Ugx1",
                                    "ana",
                                    "Me encanta",
                                    "2026-11-03T08:00:00Z",
                                    imageUrl="https://signed.example/img",
                                ),
                            },
                            "replies": {"comments": [own_reply]},
                        },
                        {
                            "id": "Ugx0",
                            "snippet": {
                                "videoId": VIDEO_ID,
                                "totalReplyCount": 0,
                                "topLevelComment": _comment(
                                    "Ugx0", "luis", "Antiguo", "2026-10-01T08:00:00Z"
                                ),
                            },
                        },
                    ],
                    "nextPageToken": "NEXT",
                },
            )
        ]
    )
    replies = router.get(f"{API}/comments").respond(
        200,
        json={
            "items": [
                _comment("Ugx1.r1", "eva", "¿Dónde está?", "2026-11-03T08:10:00Z", parentId="Ugx1"),
                own_reply,
                _comment("Ugx1.r3", "eva", "Vale", "2026-11-03T08:40:00Z", parentId="Ugx1"),
            ]
        },
    )
    since = datetime(2026, 11, 2, tzinfo=UTC)
    comments = youtube.YouTubeProvider().fetch_comments(make_account(), settings, http, since)
    params = qs(threads.calls.last.request)
    assert params["allThreadsRelatedToChannelId"] == CHANNEL
    assert (
        params["part"] == "snippet,replies"
        and params["order"] == "time"
        and params["textFormat"] == "plainText"
    )
    assert threads.call_count == 1  # oldest thread older than since-2h → no second page
    assert qs(replies.calls.last.request)["parentId"] == "Ugx1"
    by_id = {c.external_id: c for c in comments}
    assert set(by_id) == {"Ugx1", "Ugx1.r1", "Ugx1.r2", "Ugx1.r3"}
    top = by_id["Ugx1"]
    assert top.parent_external_id is None and top.object_external_id == VIDEO_ID
    assert top.object_permalink == f"https://www.youtube.com/watch?v={VIDEO_ID}&lc=Ugx1"
    assert (top.author_name, top.author_id, top.text) == ("ana", "UCana", "Me encanta")
    assert top.created_time == datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    assert not top.from_us
    assert "imageUrl" not in top.raw and top.raw["has_image"] is True and top.raw["can_reply"] is True
    assert by_id["Ugx1.r2"].from_us and by_id["Ugx1.r2"].parent_external_id == "Ugx1"


def test_fetch_comments_requires_force_ssl(http: httpx.Client, settings: ProviderSettings) -> None:
    acc = make_account(meta={"granted_scopes": [youtube.SCOPE_READONLY, youtube.SCOPE_UPLOAD]})
    with pytest.raises(ProviderError) as exc:
        youtube.YouTubeProvider().fetch_comments(acc, settings, http, NOW)
    assert exc.value.kind == ErrorKind.AUTH


def test_reply_comment(router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings) -> None:
    route = router.post(f"{API}/comments").respond(200, json={"id": "Ugx1.new"})
    provider = youtube.YouTubeProvider()
    assert (
        provider.reply_comment(make_account(), settings, http, "Ugx1.r1", "  ¡Gracias, Eva!  ") == "Ugx1.new"
    )
    request = route.calls.last.request
    assert qs(request) == {"part": "snippet"}
    assert json.loads(request.content) == {"snippet": {"parentId": "Ugx1", "textOriginal": "¡Gracias, Eva!"}}
    provider.reply_comment(make_account(), settings, http, "Ugx2", "Hola")
    assert json.loads(route.calls.last.request.content)["snippet"]["parentId"] == "Ugx2"
    with pytest.raises(ProviderError) as exc:
        provider.reply_comment(make_account(), settings, http, "Ugx2", "   ")
    assert exc.value.code == "youtube_reply_empty"


def test_reply_comment_errors(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    router.post(f"{API}/comments").mock(
        side_effect=[
            httpx.Response(403, json=gerr(403, "commentsDisabled")),
            httpx.Response(400, json=gerr(400, "operationNotSupported")),
        ]
    )
    provider = youtube.YouTubeProvider()
    with pytest.raises(ProviderError) as exc:
        provider.reply_comment(make_account(), settings, http, "Ugx1", "hola")
    assert exc.value.message == "Los comentarios están desactivados en este vídeo."
    with pytest.raises(ProviderError) as exc:
        provider.reply_comment(make_account(), settings, http, "Ugx1", "hola")
    assert exc.value.kind == ErrorKind.PERMANENT and "responder" in exc.value.message


# ---------------------------------------------------------------------------------------- error table


@pytest.mark.parametrize(
    ("status", "body", "op", "url", "kind", "fragment"),
    [
        (401, gerr(401, "authError", status="UNAUTHENTICATED"), "api", None, ErrorKind.AUTH, "conectar"),
        (403, gerr(403, "insufficientPermissions"), "api", None, ErrorKind.AUTH, "Faltan permisos"),
        (
            403,
            gerr(403, "quotaExceeded"),
            "api",
            f"{API}/videos",
            ErrorKind.RATE_LIMITED,
            "la API de YouTube",
        ),
        (403, gerr(403, "dailyLimitExceeded"), "api", None, ErrorKind.RATE_LIMITED, "cuota diaria"),
        (403, gerr(403, "rateLimitExceeded"), "api", None, ErrorKind.RATE_LIMITED, "más despacio"),
        (429, {}, "api", None, ErrorKind.RATE_LIMITED, "más despacio"),
        (429, gerr(429, "uploadRateLimitExceeded"), "thumbnail", None, ErrorKind.RATE_LIMITED, "miniaturas"),
        (
            400,
            gerr(400, "uploadLimitExceeded"),
            "upload_init",
            None,
            ErrorKind.RATE_LIMITED,
            "límite diario de subidas",
        ),
        (
            403,
            gerr(403, "accessNotConfigured", "YouTube Data API v3 has not been used in project 1"),
            "api",
            f"{API}/videos",
            ErrorKind.PERMANENT,
            "YouTube Data API v3",
        ),
        (403, gerr(403, "forbidden"), "thumbnail", None, ErrorKind.PERMANENT, "verificado"),
        (403, gerr(403, "forbiddenPrivacySetting"), "upload_init", None, ErrorKind.PERMANENT, "visibilidad"),
        (400, gerr(400, "imageTooWide"), "thumbnail", None, ErrorKind.PERMANENT, "ancha"),
        (404, gerr(404, "videoNotFound"), "api", None, ErrorKind.PERMANENT, "ya no existe"),
        (500, gerr(500, "backendError"), "api", None, ErrorKind.TRANSIENT, "temporal"),
        (503, None, "api", None, ErrorKind.TRANSIENT, "temporal"),
        (400, gerr(400, "processingFailure"), "reply", None, ErrorKind.TRANSIENT, "procesar"),
        (400, gerr(400, "somethingNew", "Weird thing"), "api", None, ErrorKind.PERMANENT, "Weird thing"),
    ],
)
def test_classify_google_error(
    status: int, body: Any, op: str, url: str | None, kind: ErrorKind, fragment: str
) -> None:
    err = youtube.classify_google_error(status, body, {}, op=op, url=url, now=NOW)
    assert err.kind == kind
    assert fragment.lower() in err.message.lower()
    assert err.detail["http_status"] == status


def test_retry_after_header_and_scrubbing() -> None:
    err = youtube.classify_google_error(
        503,
        gerr(503, "backendError", "oops access_token=ya29.LEAKED123456 here"),
        {"Retry-After": "42"},
        now=NOW,
    )
    assert err.retry_after == 42
    assert "ya29.LEAKED123456" not in json.dumps(err.to_dict())


def test_transport_errors_are_transient(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    router.get(f"{API}/channels").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderError) as exc:
        youtube.YouTubeProvider().check_account(make_account(), settings, http)
    assert exc.value.kind == ErrorKind.TRANSIENT and exc.value.code == "youtube_timeout"


# ------------------------------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://studio.youtube.com/video/dQw4w9WgXcQ/edit", None),
        ("https://evil.example/watch?v=dQw4w9WgXcQ", None),
        ("", None),
    ],
)
def test_parse_video_id(value: str, expected: str | None) -> None:
    assert youtube.parse_video_id(value) == expected


def test_small_helpers() -> None:
    assert youtube.next_offset_from_range("bytes=0-524287") == 524288
    assert youtube.next_offset_from_range(None) == 0
    assert youtube.parse_duration("PT1H2M3S") == 3723
    assert youtube.parse_duration("P1DT2S") == 86402
    assert youtube.parse_duration("bad") is None
    assert youtube.top_level_comment_id("Ugx1.abc") == "Ugx1"
    assert youtube.studio_upload_url(CHANNEL).startswith("https://studio.youtube.com/channel/UC")
    assert youtube.studio_upload_url(None) == "https://studio.youtube.com/"
    assert youtube.quota_reset_at(datetime(2026, 7, 1, 6, 0, tzinfo=UTC)) == datetime(
        2026, 7, 1, 7, 5, tzinfo=UTC
    )
    assert youtube.rfc3339(datetime(2026, 11, 3, 10, 0, 0, 123, tzinfo=UTC)) == "2026-11-03T10:00:00Z"


# ------------------------------------------------------------------------------------------ lock test


def test_make_test_video_is_vertical_two_seconds(tmp_path: Path) -> None:
    import av

    path = youtube.make_test_video(tmp_path / "t.mp4")
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height) == youtube.LOCK_TEST_SIZE
        assert stream.width < stream.height
        assert container.duration is not None
        assert 1.8 <= container.duration / 1_000_000 <= 2.2
    other = youtube.make_test_video(tmp_path / "u.mp4")
    assert path.read_bytes() != other.read_bytes()  # unique clips (no duplicate rejection)


def _lock_routes(router: respx.MockRouter, privacy_after: str, returned: str = "unlisted") -> dict[str, Any]:
    sessions: dict[str, Any] = {"init": [], "puts": []}

    def init(request: httpx.Request) -> httpx.Response:
        sessions["init"].append(request)
        return httpx.Response(200, headers={"Location": f"https://{UPLOAD_HOST}{UPLOAD_PATH}?upload_id=LOCK"})

    def put(request: httpx.Request) -> httpx.Response:
        sessions["puts"].append(request)
        return httpx.Response(
            201, json={"id": "lockvid0001", "status": {"privacyStatus": returned, "uploadStatus": "uploaded"}}
        )

    router.route(method="POST", host=UPLOAD_HOST, path=UPLOAD_PATH).mock(side_effect=init)
    router.route(method="PUT", host=UPLOAD_HOST, path=UPLOAD_PATH).mock(side_effect=put)
    sessions["videos"] = router.get(f"{API}/videos").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "lockvid0001",
                            "status": {"privacyStatus": returned, "uploadStatus": "uploaded"},
                            "processingDetails": {"processingStatus": "processing"},
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "lockvid0001",
                            "status": {"privacyStatus": privacy_after, "uploadStatus": "processed"},
                            "processingDetails": {"processingStatus": "succeeded"},
                        }
                    ]
                },
            ),
        ]
    )
    sessions["delete"] = router.delete(f"{API}/videos").respond(204)
    return sessions


def test_run_lock_test_unlocked(
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    _no_sleep: list[float],
) -> None:
    routes = _lock_routes(router, "unlisted")
    result = youtube.lock_test(make_account(), settings, http, clk)
    assert result.status == "unlocked" and result.deleted and result.privacy_status == "unlisted"
    init = routes["init"][0]
    params = qs(init)
    assert params["notifySubscribers"] == "false" and params["uploadType"] == "resumable"
    body = json.loads(init.content)
    assert body["snippet"]["title"] == youtube.LOCK_TEST_TITLE
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    put = routes["puts"][0]
    assert put.headers["Content-Range"].endswith(f"/{len(put.content)}")
    assert put.content[4:8] == b"ftyp"  # a real MP4 was uploaded
    assert qs(routes["delete"].calls.last.request) == {"id": "lockvid0001"}
    assert _no_sleep == [youtube.LOCK_TEST_POLL_DELAY]


def test_run_lock_test_locked(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    routes = _lock_routes(router, "private", returned="private")
    assert youtube.run_lock_test(make_account(), settings, http, clk) == "locked"
    assert routes["delete"].call_count == 1


def test_run_lock_test_error(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings, clk: ManualClock
) -> None:
    router.route(method="POST", host=UPLOAD_HOST, path=UPLOAD_PATH).respond(
        403, json=gerr(403, "quotaExceeded")
    )
    result = youtube.lock_test(make_account(), settings, http, clk)
    assert result.status == "error" and "cuota" in result.message
    assert youtube.run_lock_test(make_account(), settings, http, clk) == "error"


# ------------------------------------------------------------------------------------ extra edge cases


def test_final_chunk_401_without_refresh_is_auth_not_ambiguous(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    small_chunks: None,
) -> None:
    rec = Recorder()
    ctx = make_ctx(
        make_account(refresh_token=None), make_target([write_video(tmp_path)]), settings, http, clk, rec
    )
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    provider.publish_step(ctx)
    fake.session.put_script = [httpx.Response(401, json=gerr(401, "authError", status="UNAUTHENTICATED"))]
    with pytest.raises(ProviderError) as exc:
        provider.publish_step(ctx)
    assert exc.value.kind == ErrorKind.AUTH
    assert fake.session.queries == 0  # a 401 answer proves nothing was stored: no status query needed
    assert ctx.state["resync"] is True


def test_media_changed_before_commit_restarts_session(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    small_chunks: None,
) -> None:
    rec = Recorder()
    target = make_target([write_video(tmp_path)])
    ctx = make_ctx(account, target, settings, http, clk, rec)
    provider = youtube.YouTubeProvider()
    provider.publish_step(ctx)
    target.media = [replace(write_video(tmp_path, size=400_000, name="otro.mp4"), id=12)]
    assert provider.publish_step(ctx).status == "continue"
    assert "session_uri" not in ctx.state
    assert "youtube_session_restarted" in rec.event_names()
    results = run(provider, ctx, clk)
    assert results[-1].status == "published"
    assert fake.session.total == 400_000 and len(fake.init_requests) == 2


def test_reconcile_rejected_video_is_not_reported_as_published(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    fake.processing = [video_resource(upload_status="rejected", rejectionReason="copyright")]
    ctx = make_ctx(
        account,
        make_target([write_video(tmp_path)]),
        settings,
        http,
        clk,
        Recorder(),
        state={"video_id": VIDEO_ID},
    )
    result = youtube.YouTubeProvider().reconcile(ctx)
    assert result.outcome == "unknown"
    assert "derechos de autor" in (result.message or "")


def test_naive_since_is_accepted(
    router: respx.MockRouter, http: httpx.Client, settings: ProviderSettings
) -> None:
    router.get(f"{API}/playlistItems").respond(
        200,
        json={
            "items": [
                {"contentDetails": {"videoId": "vid00000001", "videoPublishedAt": "2026-11-02T10:00:00Z"}}
            ]
        },
    )
    router.get(f"{API}/videos").respond(
        200, json={"items": [{"id": "vid00000001", "snippet": {"publishedAt": "2026-11-02T10:00:00Z"}}]}
    )
    router.get(youtube.ANALYTICS_URL).respond(200, json={"columnHeaders": [], "rows": []})
    router.get(f"{API}/commentThreads").respond(200, json={"items": []})
    provider = youtube.YouTubeProvider()
    naive = datetime(2026, 10, 1)
    assert [p.external_id for p in provider.fetch_recent_posts(make_account(), settings, http, naive)] == [
        "vid00000001"
    ]
    assert provider.fetch_comments(make_account(), settings, http, naive) == []


# ------------------------------------------------------------------- integration: early claims and cancel


def test_upload_never_starts_before_its_time_unless_native(
    tmp_path: Path,
    fake: FakeYouTube,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
) -> None:
    """The engine claims ``native_schedule`` targets ahead of time; an unlisted video cannot use
    ``publishAt``, so the provider waits until the scheduled time instead of uploading early."""
    target = make_target([write_video(tmp_path)], native_schedule=True, privacy_status="unlisted")
    target.scheduled_at = NOW + timedelta(hours=1)
    ctx = make_ctx(account, target, settings, http, clk, Recorder())
    result = youtube.YouTubeProvider().publish_step(ctx)
    assert result.status == "wait" and result.wait_seconds == 3600
    assert fake.init_requests == [] and ctx.state == {}


def test_cancel_native_deletes_the_private_video(
    router: respx.MockRouter,
    http: httpx.Client,
    settings: ProviderSettings,
    clk: ManualClock,
    account: AccountCreds,
    tmp_path: Path,
) -> None:
    route = router.delete(f"{API}/videos").mock(return_value=httpx.Response(204))
    target = make_target([write_video(tmp_path)], native_schedule=True)
    ctx = make_ctx(
        account,
        target,
        settings,
        http,
        clk,
        Recorder(),
        state={"video_id": VIDEO_ID, "native_scheduled": True},
    )
    youtube.YouTubeProvider().cancel_native(ctx)
    assert route.called and qs(route.calls.last.request)["id"] == VIDEO_ID
    assert ctx.state["native_scheduled"] is False

    ctx = make_ctx(account, target, settings, http, clk, Recorder(), state={})
    with pytest.raises(ProviderError) as exc:
        youtube.YouTubeProvider().cancel_native(ctx)
    assert exc.value.code == "youtube_native_no_id"
